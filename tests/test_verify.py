"""Testes da verificação periódica (Fase 6a): relatório, plano e notificação.

Nada aqui toca o Office: as "fontes" são pastas temporárias com bancos sintéticos.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from office_chat_keeper import cli
from office_chat_keeper.archive import Archive
from office_chat_keeper.snapshot import SnapshotInfo, choose_snapshots_to_delete

from idb_builder import build_sample_storage, sample_chat

CHAT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"
IDB = Path("EBWebView") / "Default" / "IndexedDB"
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    return tmp_path / "dados", tmp_path / "office"


def run(data: Path, root: Path, *command: str) -> int:
    return cli.main(["--data-dir", str(data), "--source-root", str(root), *command])


def reports(data: Path, pattern: str) -> list[Path]:
    return sorted((data / "reports").glob(pattern))


def test_dia_normal_grava_relatorio_sem_perda(env, capsys, notifications):
    data, root = env
    build_sample_storage(root / "a_ADAL" / "2" / "0" / IDB).write()
    assert run(data, root, "run") == 0
    assert "verificação: sem sinais de perda" in capsys.readouterr().out

    report = json.loads((data / "reports" / "ultima-verificacao.json").read_text(encoding="utf-8"))
    assert report["loss"] is False and report["absent_conversations"] == []
    assert [s["status"] for s in report["sources"]] == ["nova"]
    assert "sem sinais de perda" in (data / "reports" / "ultima-verificacao.md").read_text(encoding="utf-8")
    assert reports(data, "perda-*.md") == [] and reports(data, "plano-restauracao-*.md") == []
    assert notifications == []

    assert run(data, root, "status") == 0
    assert "(sem sinais de perda)" in capsys.readouterr().out


def test_office_mudou_de_pasta_e_o_banco_novo_veio_vazio(env, capsys, notifications):
    data, root = env
    old = root / "a_ADAL" / "2" / "0"
    build_sample_storage(old / IDB).write()
    assert run(data, root, "run") == 0
    capsys.readouterr()

    # A atualização: a pasta 2\0 some e aparece 2\1 com o banco vazio.
    old.rename(root / "a_ADAL" / "antiga-removida")
    (root / "a_ADAL" / "antiga-removida" / IDB / "https_pivot.claude.ai_0.indexeddb.leveldb").rename(
        root / "a_ADAL" / "antiga-removida" / "nao-e-mais-fonte")
    build_sample_storage(root / "a_ADAL" / "2" / "1" / IDB, chats=[]).write()
    assert run(data, root, "run") == 0
    out = capsys.readouterr().out

    assert "ALERTA" in out and "Possível perda de histórico" in out
    assert "Relatório:" in out and "Plano de restauração (simulação; nada foi executado)" in out
    # Um único alerta para o mesmo incidente, e uma única notificação.
    with Archive(data / "archive" / "conversas.sqlite") as arch:
        assert [a.kind for a in arch.open_alerts()] == ["source_smaller_than_archive"]
    assert len(notifications) == 1 and "possível perda" in notifications[0][0]

    report = json.loads((data / "reports" / "ultima-verificacao.json").read_text(encoding="utf-8"))
    assert report["loss"] is True
    assert sorted(s["status"] for s in report["sources"]) == ["nova", "sumiu"]
    assert len(report["moved"]) == 1 and "2\\0" in report["moved"][0]["from"]
    absent = report["absent_conversations"]
    assert [(c["id"], c["app"], c["messages"], c["snapshot_available"]) for c in absent] == [
        (CHAT_ID, "Excel", 5, True)]
    assert absent[0]["title"] == "Conciliação de teste"

    md = reports(data, "perda-*.md")[0].read_text(encoding="utf-8")
    assert "POSSÍVEL PERDA DE HISTÓRICO" in md and "SUMIU" in md and "NOVA" in md
    assert "Possível mudança de pasta" in md and CHAT_ID in md

    plan = reports(data, "plano-restauracao-*.md")[0].read_text(encoding="utf-8")
    assert "Isto é só um plano" in plan and "nunca restaura sozinha" in plan
    assert "## Estratégia A" in plan and "## Estratégia B" in plan and "## Caminho seguro" in plan
    assert "contém 1 das conversas" in plan
    # O banco novo está vazio: a identidade atual é desconhecida, e o plano diz o que fazer.
    assert "identidade atual" in plan and "é desconhecida" in plan
    assert "conversa.md" in plan  # caminho do Markdown para o caminho seguro

    # Execuções seguintes com o mesmo problema: não repetem relatório, plano nem aviso.
    assert run(data, root, "run") == 0
    out = capsys.readouterr().out
    assert "1 alerta(s) aberto(s)" in out and "Relatório:" not in out
    assert len(reports(data, "perda-*.md")) == 1 and len(notifications) == 1

    # Depois de marcado como visto, a verificação volta a ficar limpa,
    # mas a conversa continua listada como ausente (e guardada).
    assert run(data, root, "alerts", "--ack", "all") == 0
    assert run(data, root, "verify") == 0
    out = capsys.readouterr().out
    assert "Resultado: sem sinais de perda" in out
    assert "ausentes das fontes atuais: 1" in out and "11111111" in out
    with Archive(data / "archive" / "conversas.sqlite") as arch:
        assert len(arch.load_conversation(CHAT_ID).messages) == 5


def test_plano_diz_se_a_identidade_bate(env, capsys):
    data, root = env
    folder = root / "a_ADAL" / "2" / "0" / IDB
    other = sample_chat(OTHER_ID, title="Outra conversa")
    build_sample_storage(folder, chats=[sample_chat(), other]).write()
    assert run(data, root, "run") == 0

    # A conversa principal é apagada na origem; a outra continua lá (mesmo usuário/organização).
    for path in sorted(folder.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    build_sample_storage(folder, chats=[other]).write()
    assert run(data, root, "run") == 0
    capsys.readouterr()

    plan = reports(data, "plano-restauracao-*.md")[0].read_text(encoding="utf-8")
    assert "mesma identidade das conversas a inserir" in plan
    assert "`userId`/`orgId` NÃO seriam alterados" in plan
    with Archive(data / "archive" / "conversas.sqlite") as arch:
        # O teste apaga e recria os arquivos do banco. Se isso cair em outro segundo
        # do relógio, a recriação também é acusada; por isso ela é opcional aqui.
        kinds = {a.kind for a in arch.open_alerts()}
        assert "conversations_missing" in kinds
        assert kinds <= {"conversations_missing", "database_recreated"}


def test_fonte_antiga_some_mas_nada_se_perde(env, capsys, notifications):
    # A pasta muda e a conversa está inteira na pasta nova: é só mudança de pasta.
    data, root = env
    old = root / "a_ADAL" / "2" / "0"
    build_sample_storage(old / IDB).write()
    assert run(data, root, "run") == 0
    (root / "a_ADAL" / "2" / "1").parent.mkdir(parents=True, exist_ok=True)
    old.rename(root / "a_ADAL" / "2" / "1")
    assert run(data, root, "run") == 0
    out = capsys.readouterr().out
    assert "verificação: sem sinais de perda" in out and notifications == []
    report = json.loads((data / "reports" / "ultima-verificacao.json").read_text(encoding="utf-8"))
    assert report["loss"] is False and report["absent_conversations"] == []
    assert len(report["moved"]) == 1


def test_verify_nao_tira_snapshot_nem_cria_pasta(env, capsys):
    data, root = env
    root.mkdir()
    assert run(data, root, "verify") == 0
    assert "ainda não existe" in capsys.readouterr().out and not data.exists()

    build_sample_storage(root / "a_ADAL" / "2" / "0" / IDB).write()
    assert run(data, root, "run") == 0
    before = sorted(p.name for p in (data / "snapshots").rglob("manifest.json"))
    assert run(data, root, "verify") == 0
    out = capsys.readouterr().out
    assert "Resultado: sem sinais de perda" in out and "Relatório completo:" in out
    assert sorted(p.name for p in (data / "snapshots").rglob("manifest.json")) == before


def test_notificacao_de_teste(env, capsys, notifications):
    data, root = env
    assert run(data, root, "verify", "--test-notification") == 0
    assert "aceitou o pedido de notificação" in capsys.readouterr().out
    assert notifications == [("office-chat-keeper: teste",
                              "Se você está vendo este aviso, as notificações funcionam.")]


# ------------------------------------ retenção: cópia bruta necessária fica
def test_retencao_nao_apaga_snapshot_que_e_a_ultima_copia_de_uma_conversa():
    snaps = [SnapshotInfo(Path((T0 - timedelta(days=d)).strftime("%Y%m%d-%H%M%S")),
                          {"taken_at": (T0 - timedelta(days=d)).isoformat(),
                           "db_created_at": "g1", "stable": True})
             for d in (60, 59, 58, 0)]
    pinned = {snaps[1].path.name}
    doomed = choose_snapshots_to_delete(snaps, now=T0, keep_last=1, keep_daily_days=1,
                                        protected=pinned)
    assert [s.path.name for s in doomed] == [snaps[0].path.name, snaps[2].path.name]


def test_arquivo_informa_os_snapshots_que_precisam_ficar(env):
    data, root = env
    build_sample_storage(root / "a_ADAL" / "2" / "0" / IDB).write()
    assert run(data, root, "run") == 0
    with Archive(data / "archive" / "conversas.sqlite") as arch:
        pinned = arch.pinned_snapshots()
    (source_id, names), = pinned.items()
    assert len(names) == 1 and (data / "snapshots" / source_id / next(iter(names))).is_dir()

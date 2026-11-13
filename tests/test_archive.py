"""Testes do arquivo permanente: a regra "só acrescenta" e os alertas de perda."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from office_chat_keeper import cli
from office_chat_keeper.archive import Archive
from office_chat_keeper.conversation import conversation_from_record
from office_chat_keeper.pipeline import ingest_snapshot
from office_chat_keeper.snapshot import SnapshotInfo

from idb_builder import PNG_1X1, build_sample_storage, sample_chat

CHAT_ID = "11111111-2222-3333-4444-555555555555"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # quando o "snapshot" foi tirado
TOKEN = "TOKEN-SUPER-SECRETO-NAO-EXPORTAR"


def chat(extra: int = 0, minute: int = 10, title: str = "Conciliação de teste",
         drop: tuple[str, ...] = (), edit: dict[str, str] | None = None) -> dict:
    """Uma versão da conversa de teste. ``minute`` controla o updatedAt."""
    record = sample_chat(title=title, extra_messages=extra)
    record["updatedAt"] = datetime(2026, 11, 1, 12, minute, tzinfo=timezone.utc).timestamp() * 1000
    messages = record["snapshot"]["ui"]["messages"]
    messages[:] = [m for m in messages if m["uuid"] not in drop]
    for message in messages:
        if edit and message["uuid"] in edit:
            message["content"] = edit[message["uuid"]]
    return record


def ingest(archive: Archive, record: dict, *, when: datetime = T0, source: str = "fonte-a"):
    return archive.ingest_conversation(conversation_from_record(record), source_id=source,
                                       snapshot=when.strftime("%Y%m%d-%H%M%S"), seen_at=when)


def texts(messages) -> list[str | None]:
    return [m.text for m in messages if m.kind in ("user", "assistant")]


@pytest.fixture
def archive(tmp_path):
    with Archive(tmp_path / "arquivo" / "conversas.sqlite") as arch:
        yield arch


# ------------------------------------------------------- ida e volta básica
def test_conversa_nova_e_guardada_e_reconstruida(archive):
    outcome = ingest(archive, chat())
    assert (outcome.outcome, outcome.added) == ("new", 5)

    conv = archive.load_conversation(CHAT_ID)
    assert (conv.title, conv.surface, conv.app) == ("Conciliação de teste", "sheet", "Excel")
    assert [m.kind for m in conv.messages] == ["user", "thinking", "tool", "tool", "assistant"]
    assert conv.messages[0].timestamp == datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert conv.messages[2].tool_input["explanation"] == "Lê os cabeçalhos da aba Março"
    assert conv.messages[2].images[0].data == PNG_1X1
    assert conv.messages[3].tool_error == "Intervalo inválido"
    assert conv.absent_messages == [] and conv.missing_since is None


def test_mesma_versao_de_novo_nao_muda_nada(archive):
    ingest(archive, chat())
    again = ingest(archive, chat(), when=T0 + timedelta(minutes=15))
    assert again.outcome == "unchanged"
    assert archive.stats()["versions"] == 1


def test_conversa_que_cresce_ganha_nova_versao(archive):
    ingest(archive, chat())
    grown = ingest(archive, chat(extra=2, minute=20), when=T0 + timedelta(hours=1))

    assert (grown.outcome, grown.added, grown.removed) == ("updated", 2, 0)
    assert len(archive.load_conversation(CHAT_ID).messages) == 7
    history = archive.versions(CHAT_ID)
    assert [(v["version"], v["status"], v["message_count"], v["added"]) for v in history] == [
        (1, "accepted", 5, 5), (2, "accepted", 7, 2)]
    assert history[0]["updated_at"] < history[1]["updated_at"]


# ---------------------------------------------- regra B: versão vazia/menor
def test_versao_vazia_nao_sobrescreve_a_conversa_boa(archive):
    ingest(archive, chat(extra=3))
    empty = chat(minute=30, drop=("u-1", "t-1", "f-1", "f-2", "a-1"))
    outcome = ingest(archive, empty, when=T0 + timedelta(hours=1))

    assert outcome.outcome == "rejected"
    conv = archive.load_conversation(CHAT_ID)
    assert len(conv.messages) == 8 and conv.absent_messages == []
    assert conv.updated_at == datetime(2026, 11, 1, 12, 10, tzinfo=timezone.utc)  # cabeça intacta
    assert archive.versions(CHAT_ID)[-1]["status"] == "rejected"


def test_versao_muito_menor_e_rejeitada_mas_mensagem_inedita_e_aproveitada(archive):
    ingest(archive, chat())
    # Chega uma versão sem 4 das 5 mensagens, porém com 1 mensagem inédita.
    shrunk = chat(extra=1, minute=30, drop=("t-1", "f-1", "f-2", "a-1"), title="Outro título")
    outcome = ingest(archive, shrunk, when=T0 + timedelta(hours=1))

    assert (outcome.outcome, outcome.added, outcome.removed) == ("rejected", 1, 4)
    conv = archive.load_conversation(CHAT_ID)
    assert conv.title == "Conciliação de teste"  # o título da versão suspeita não entra
    assert len(conv.messages) == 6  # as 5 originais + a inédita (regra A)
    assert "Mensagem extra 0" in texts(conv.messages)


# ----------------------------------- regra A: nada some, só é marcado ausente
def test_mensagem_removida_na_origem_fica_guardada_como_ausente(archive):
    ingest(archive, chat(extra=3))
    outcome = ingest(archive, chat(extra=3, minute=30, drop=("x-2",)), when=T0 + timedelta(hours=1))

    assert (outcome.outcome, outcome.removed) == ("updated", 1)
    conv = archive.load_conversation(CHAT_ID)
    assert len(conv.messages) == 7
    assert texts(conv.absent_messages) == ["Mensagem extra 2"]
    assert archive.stats()["messages"] == 8  # nenhuma linha saiu do banco

    # Se a mensagem reaparece, volta a ser ativa.
    ingest(archive, chat(extra=3, minute=40), when=T0 + timedelta(hours=2))
    conv = archive.load_conversation(CHAT_ID)
    assert len(conv.messages) == 8 and conv.absent_messages == []


def test_mensagem_editada_guarda_o_texto_anterior(archive):
    ingest(archive, chat())
    outcome = ingest(archive, chat(minute=20, edit={"a-1": "Texto corrigido."}),
                     when=T0 + timedelta(hours=1))

    assert (outcome.outcome, outcome.changed) == ("updated", 1)
    assert archive.load_conversation(CHAT_ID).messages[-1].text == "Texto corrigido."
    old = archive._db.execute("SELECT payload FROM message_versions WHERE uuid='a-1'").fetchall()
    assert len(old) == 1 and "3 lançamentos conciliados" in old[0]["payload"]


# -------------------------------------------- regra C: versão mais antiga
def test_versao_mais_antiga_nao_rebaixa_a_atual(archive):
    ingest(archive, chat(extra=3, minute=40))
    older = ingest(archive, chat(minute=10), when=T0 + timedelta(hours=1))

    assert older.outcome == "unchanged"
    conv = archive.load_conversation(CHAT_ID)
    assert len(conv.messages) == 8
    assert conv.updated_at.minute == 40
    assert archive.open_alerts() == []


# ---------------------------------------------- regra E: trava no banco
@pytest.mark.parametrize("table", ["conversations", "messages", "conversation_versions", "blobs"])
def test_o_proprio_banco_recusa_delete(archive, table):
    ingest(archive, chat())
    before = archive._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert before > 0
    with pytest.raises(sqlite3.DatabaseError, match="DELETE não é permitido"):
        archive._db.execute(f"DELETE FROM {table}")
    assert archive._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == before


def test_arquivo_persiste_entre_aberturas(tmp_path):
    path = tmp_path / "conversas.sqlite"
    with Archive(path) as arch:
        ingest(arch, chat())
    with Archive(path) as arch:
        assert arch.conversation_ids() == {CHAT_ID}
        assert ingest(arch, chat()).outcome == "unchanged"


# ------------------------------------------- alertas de perda (por snapshot)
def snap(tmp_path: Path, name: str, *, chats: list[dict] | None = None, source: str = "fonte-a",
         when: datetime = T0, stable: bool = True,
         created: str = "2026-10-01T00:00:00+00:00") -> SnapshotInfo:
    """Um snapshot sintético, com o manifesto mínimo que o pipeline usa."""
    path = tmp_path / "snaps" / source / name
    build_sample_storage(path, chats=chats).write()
    return SnapshotInfo(path, {"taken_at": when.isoformat(), "stable": stable,
                               "source_id": source, "db_created_at": created})


def test_fonte_nova_e_vazia_gera_alerta_e_nao_perde_nada(archive, tmp_path):
    ingest_snapshot(archive, snap(tmp_path, "s1"))
    report = ingest_snapshot(archive, snap(tmp_path, "s2", chats=[], source="fonte-nova",
                                           when=T0 + timedelta(hours=1)))

    assert [a.kind for a in report.alerts] == ["source_smaller_than_archive"]
    assert "Possível perda de histórico" in report.alerts[0].message
    assert report.alerts[0].details["absent_ids"] == [CHAT_ID]
    assert len(archive.load_conversation(CHAT_ID).messages) == 5
    assert len(archive.open_alerts()) == 1


def test_mesma_fonte_esvaziada_gera_alerta_de_conversas_ausentes(archive, tmp_path):
    ingest_snapshot(archive, snap(tmp_path, "s1"))
    report = ingest_snapshot(archive, snap(tmp_path, "s2", chats=[], when=T0 + timedelta(hours=1),
                                           created="2026-11-02T09:30:00+00:00"))

    kinds = sorted(a.kind for a in report.alerts)
    assert kinds == ["conversations_missing", "database_recreated"]
    missing = next(a for a in report.alerts if a.kind == "conversations_missing")
    assert missing.details["conversations"] == [{"id": CHAT_ID, "title": "Conciliação de teste"}]
    conv = archive.load_conversation(CHAT_ID)
    assert len(conv.messages) == 5 and conv.missing_since == T0 + timedelta(hours=1)

    # O mesmo problema, visto de novo, não repete o alerta.
    again = ingest_snapshot(archive, snap(tmp_path, "s3", chats=[], when=T0 + timedelta(hours=2),
                                          created="2026-11-02T09:30:00+00:00"))
    assert again.alerts == [] and len(archive.open_alerts()) == 2


def test_conversa_que_volta_resolve_o_alerta(archive, tmp_path):
    ingest_snapshot(archive, snap(tmp_path, "s1"))
    ingest_snapshot(archive, snap(tmp_path, "s2", chats=[], when=T0 + timedelta(hours=1)))
    assert [a.kind for a in archive.open_alerts()] == ["conversations_missing"]

    ingest_snapshot(archive, snap(tmp_path, "s3", when=T0 + timedelta(hours=2)))
    assert archive.open_alerts() == []
    assert archive.load_conversation(CHAT_ID).missing_since is None


def test_snapshot_instavel_nao_acusa_ausencia(archive, tmp_path):
    ingest_snapshot(archive, snap(tmp_path, "s1"))
    report = ingest_snapshot(archive, snap(tmp_path, "s2", chats=[], stable=False,
                                           when=T0 + timedelta(hours=1)))
    assert report.alerts == []
    assert any("instável" in w for w in report.warnings)
    assert archive.load_conversation(CHAT_ID).missing_since is None


def test_conversa_encolhida_gera_alerta(archive, tmp_path):
    ingest_snapshot(archive, snap(tmp_path, "s1", chats=[chat(extra=3)]))
    report = ingest_snapshot(archive, snap(
        tmp_path, "s2", when=T0 + timedelta(hours=1),
        chats=[chat(minute=30, drop=("u-1", "t-1", "f-1", "f-2", "a-1"))]))
    assert [a.kind for a in report.alerts] == ["conversation_shrunk"]
    assert len(archive.load_conversation(CHAT_ID).messages) == 8


def test_skills_e_memoria_ficam_no_arquivo_com_versoes(archive, tmp_path):
    ingest_snapshot(archive, snap(tmp_path, "s1"))
    rows = archive.latest_extra_records()
    assert [(r["database"], r["store"], r["key"]) for r in rows] == [
        ("claude-local-skills", "skills", "minha-skill"), ("claude-memory", "notes", "nota-1")]
    # Uma nota alterada vira uma segunda versão; a primeira continua lá.
    archive.store_extra_record("claude-memory", "notes", "nota-1",
                               {"id": "nota-1", "text": "Prefere gráficos."},
                               T0 + timedelta(hours=1))
    assert archive._db.execute("SELECT COUNT(*) FROM extra_records WHERE key='nota-1'").fetchone()[0] == 2
    latest = [r for r in archive.latest_extra_records() if r["key"] == "nota-1"][0]
    assert "gráficos" in latest["payload"]


# ------------------------------- simulação de perda, de ponta a ponta (CLI)
@pytest.fixture
def no_icacls(monkeypatch):
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))


def all_bytes(folder: Path) -> bytes:
    return b"\n".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


def test_simulacao_de_perda_pelo_cli(tmp_path, capsys, no_icacls):
    data = tmp_path / "dados"
    idb = Path("EBWebView") / "Default" / "IndexedDB"

    # 1) Dia normal: a fonte tem a conversa.
    good = tmp_path / "office-antes"
    build_sample_storage(good / "aaaa_ADAL" / "2" / "0" / idb).write()
    assert cli.main(["--data-dir", str(data), "--source-root", str(good), "run"]) == 0
    out = capsys.readouterr().out
    assert "1 nova(s)" in out and "ALERTA" not in out
    exported = next((data / "exports" / "conversas").glob("*/*/*/conversa.md"))
    before = exported.read_bytes()

    # 2) Depois da atualização: o Office abre uma pasta nova (2\1) com o banco vazio.
    lost = tmp_path / "office-depois"
    build_sample_storage(lost / "aaaa_ADAL" / "2" / "1" / idb, chats=[]).write()
    assert cli.main(["--data-dir", str(data), "--source-root", str(lost), "run"]) == 0
    out = capsys.readouterr().out
    assert "ALERTA" in out and "Possível perda de histórico" in out
    assert "Nada foi apagado" in out

    with Archive(data / "archive" / "conversas.sqlite") as arch:
        assert arch.conversation_ids() == {CHAT_ID}
        assert len(arch.load_conversation(CHAT_ID).messages) == 5
        assert [a.kind for a in arch.open_alerts()] == ["source_smaller_than_archive"]
    assert exported.read_bytes() == before  # a exportação não foi tocada

    # 3) Pior caso: nenhuma fonte no disco.
    nothing = tmp_path / "office-sumiu"
    nothing.mkdir()
    assert cli.main(["--data-dir", str(data), "--source-root", str(nothing), "run"]) == 0
    out = capsys.readouterr().out
    assert "nenhuma fonte pivot.claude.ai foi encontrada" in out
    assert exported.read_bytes() == before

    # 4) O status mostra o arquivo intacto e os alertas abertos.
    assert cli.main(["--data-dir", str(data), "--source-root", str(nothing), "status"]) == 0
    out = capsys.readouterr().out
    assert "Arquivo permanente: 1 conversa(s), 5 mensagem(ns)" in out
    assert "Alertas abertos: 2" in out

    # 5) Os tokens dos conectores não foram para o arquivo, nem para as
    #    exportações, nem para o log (ficam só no snapshot bruto).
    for folder in ("archive", "exports", "logs"):
        assert TOKEN.encode() not in all_bytes(data / folder)
    assert TOKEN.encode() in all_bytes(data / "snapshots")
    # Skills e memória foram exportadas a partir do arquivo.
    assert (data / "exports" / "skills" / "skills" / "minha-skill" / "content.md").is_file()
    assert (data / "exports" / "skills" / "skills" / "minha-skill" / "archive.zip").is_file()
    assert (data / "exports" / "memoria" / "notes" / "nota-1" / "text.md").read_text(
        encoding="utf-8") == "Prefere tabelas."
    # O log só tem ids e contagens.
    log_text = (data / "logs" / "office-chat-keeper.log").read_text(encoding="utf-8")
    assert "ALERTA source_smaller_than_archive" in log_text and CHAT_ID in log_text
    assert "Conciliação" not in log_text

"""Testes das travas de segurança da configuração e da CLI."""

from __future__ import annotations

import pytest

from office_chat_keeper import cli
from office_chat_keeper.config import (Config, ConfigError, check_destination_is_safe,
                                       load_config, onedrive_root_for)

from conftest import make_source
from idb_builder import build_sample_storage


def test_recusa_destino_dentro_da_origem(tmp_path):
    root = tmp_path / "Wef" / "webview2"
    root.mkdir(parents=True)
    with pytest.raises(ConfigError):
        check_destination_is_safe(Config(data_dir=root / "backup", source_root=root))


def test_recusa_destino_dentro_de_wef_webview2_mesmo_com_outra_origem(tmp_path):
    inside = tmp_path / "Office" / "16.0" / "Wef" / "webview2" / "x"
    with pytest.raises(ConfigError):
        check_destination_is_safe(Config(data_dir=inside, source_root=tmp_path / "outra"))


def test_aceita_destino_fora_da_origem(tmp_path):
    check_destination_is_safe(Config(data_dir=tmp_path / "dados", source_root=tmp_path / "origem"))


def test_detecta_pasta_dentro_do_onedrive(tmp_path, monkeypatch):
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)
    assert onedrive_root_for(tmp_path / "dados") is None

    by_name = tmp_path / "OneDrive - Empresa" / "Documentos" / "dados"
    assert onedrive_root_for(by_name).name == "OneDrive - Empresa"

    custom = tmp_path / "nuvem"
    monkeypatch.setenv("OneDriveCommercial", str(custom))
    assert onedrive_root_for(custom / "dados") == custom


def test_variaveis_de_ambiente_trocam_as_pastas(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_CHAT_KEEPER_HOME", str(tmp_path / "a"))
    monkeypatch.setenv("OFFICE_CHAT_KEEPER_SOURCE_ROOT", str(tmp_path / "b"))
    cfg = load_config()
    assert cfg.data_dir == tmp_path / "a" and cfg.source_root == tmp_path / "b"
    # Argumento explícito vence a variável de ambiente.
    assert load_config(data_dir=tmp_path / "c").data_dir == tmp_path / "c"


@pytest.fixture
def no_icacls(monkeypatch):
    """Nos testes não mexemos em permissões de verdade."""
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))


def test_cli_run_e_status(tmp_path, capsys, no_icacls):
    root = tmp_path / "webview2"
    build_sample_storage(root / "aaaa_ADAL" / "2" / "0" / "EBWebView" / "Default" / "IndexedDB").write()
    data = tmp_path / "dados"
    args = ["--data-dir", str(data), "--source-root", str(root)]

    assert cli.main([*args, "status"]) == 0
    assert not data.exists()  # status não cria nada
    assert "encontradas: 1" in capsys.readouterr().out

    assert cli.main([*args, "run"]) == 0
    out = capsys.readouterr().out
    assert "snapshot" in out and "1 conversa(s) (1 nova(s)" in out and "gravada:" in out
    assert "5 mensagens, 1 imagens, 1 anexos" in out
    exported = list((data / "exports" / "conversas").glob("*/*/*/conversa.md"))
    assert len(exported) == 1

    # Segunda execução: nada mudou, então não copia nem reexporta.
    assert cli.main([*args, "run"]) == 0
    out = capsys.readouterr().out
    assert "sem mudanças" in out and "exportação" not in out

    # 'export' refaz a exportação mesmo sem snapshot novo.
    assert cli.main([*args, "export", "--include-thinking"]) == 0
    assert "gravada:" in capsys.readouterr().out
    assert "Preciso ler a planilha" in exported[0].read_text(encoding="utf-8")

    assert cli.main([*args, "status"]) == 0
    out = capsys.readouterr().out
    assert "snapshots:         1" in out and "sem mudanças" in out
    assert "Arquivo permanente: 1 conversa(s), 5 mensagem(ns)" in out
    assert "Alertas abertos: 0" in out

    # O log tem ids e contagens, nunca texto de conversa.
    log_text = (data / "logs" / "office-chat-keeper.log").read_text(encoding="utf-8")
    assert "snapshot criado" in log_text and "arquivo:" in log_text and "exportacao:" in log_text
    assert "conciliação" not in log_text.lower() and "Pronto" not in log_text


def test_cli_snapshot_ilegivel_e_guardado_mas_sinalizado(tmp_path, capsys, no_icacls):
    # Fonte com arquivos que não são um LevelDB de verdade: o snapshot (cópia
    # bruta) é feito do mesmo jeito, mas a leitura falha e o comando avisa.
    root = tmp_path / "webview2"
    make_source(root)
    data = tmp_path / "dados"
    code = cli.main(["--data-dir", str(data), "--source-root", str(root), "run"])
    out = capsys.readouterr().out
    assert code == 1
    assert "snapshot" in out and "FALHA ao ler o snapshot" in out
    assert len(list((data / "snapshots").glob("*/*/manifest.json"))) == 1
    # A falha fica registrada: a execução seguinte não tenta ler de novo.
    assert cli.main(["--data-dir", str(data), "--source-root", str(root), "run"]) == 0
    assert "FALHA" not in capsys.readouterr().out


def test_cli_sem_fontes_nao_e_erro(tmp_path, capsys, no_icacls):
    empty = tmp_path / "vazio"
    empty.mkdir()
    code = cli.main(["--data-dir", str(tmp_path / "dados"), "--source-root", str(empty), "run"])
    assert code == 0
    assert "Nenhuma fonte" in capsys.readouterr().out


def test_cli_recusa_destino_perigoso(tmp_path, capsys):
    root = tmp_path / "webview2"
    root.mkdir()
    code = cli.main(["--data-dir", str(root / "dentro"), "--source-root", str(root), "status"])
    assert code == 2
    assert "ERRO" in capsys.readouterr().err

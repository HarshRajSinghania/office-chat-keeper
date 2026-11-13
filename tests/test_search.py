"""Testes da busca (FTS5) e dos comandos list / search / alerts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from office_chat_keeper import cli
from office_chat_keeper.archive import Archive, build_match
from office_chat_keeper.conversation import conversation_from_record

from idb_builder import build_sample_storage, sample_chat

CHAT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "99999999-8888-7777-6666-555555555555"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def ingest(archive: Archive, record: dict, when: datetime = T0):
    return archive.ingest_conversation(conversation_from_record(record), source_id="fonte-a",
                                       snapshot=when.strftime("%Y%m%d-%H%M%S"), seen_at=when)


@pytest.fixture
def archive(tmp_path):
    with Archive(tmp_path / "conversas.sqlite") as arch:
        ingest(arch, sample_chat())
        other = sample_chat(OTHER_ID, title="Orçamento de viagem")
        other["snapshot"]["ui"]["messages"][0]["content"] = "Monte o orçamento da viagem a Recife."
        other["snapshot"]["ui"]["messages"][0]["uuid"] = "u-outra"
        ingest(arch, other)
        yield arch


def kinds_and_ids(hits):
    return [(h.kind, h.conversation_id[:8]) for h in hits]


# --------------------------------------------------------------- consulta
def test_build_match_trata_o_texto_como_literal():
    assert build_match("fluxo de caixa") == '"fluxo" "de" "caixa"'
    assert build_match("fluxo de caixa", phrase=True) == '"fluxo de caixa"'
    assert build_match("concilia*") == '"concilia"*'
    assert build_match('diz "oi" AND NOT x') == '"diz" """oi""" "AND" "NOT" "x"'
    assert build_match("   ") == "" and build_match("*") == ""


def test_busca_ignora_acentos_e_maiusculas(archive):
    hits, total = archive.search("CONCILIACAO marco")
    assert total >= 1
    assert ("user", "11111111") in kinds_and_ids(hits)
    assert any("[conciliação]" in h.snippet for h in hits)  # o trecho destaca o termo


def test_varias_palavras_precisam_estar_na_mesma_mensagem(archive):
    assert archive.search("orçamento Recife")[1] == 1
    assert archive.search("orçamento conciliados")[0] == []  # cada uma em uma conversa


def test_frase_exata_e_prefixo(archive):
    assert archive.search("aba conciliação")[1] >= 1  # palavras soltas: acha
    assert archive.search("aba conciliação", phrase=True)[0] == []  # nessa ordem: não existe
    assert archive.search("conciliação da aba", phrase=True)[1] == 1
    assert archive.search("lançam*", conversation="1111")[1] == 1


def test_titulo_da_conversa_e_pesquisavel_e_vem_primeiro(archive):
    hits, _total = archive.search("viagem")
    assert hits[0].kind == "title" and hits[0].conversation_id == OTHER_ID
    assert {h.kind for h in hits} == {"title", "user"}


def test_ferramentas_sao_indexadas_e_filtro_por_tipo(archive):
    hits, _ = archive.search("cabeçalhos", kinds=["tool"])
    assert [h.kind for h in hits] == ["tool", "tool"]  # uma por conversa
    assert archive.search("SEGREDO_DO_CODIGO", conversation="1111")[1] == 1  # o código também
    assert archive.search("Intervalo inválido", kinds=["tool"], phrase=True)[1] == 2  # o erro
    assert archive.search("cabeçalhos", kinds=["user", "assistant"])[0] == []


def test_conversa_vem_antes_de_raciocinio_e_de_ferramenta(archive):
    # "março" aparece na mensagem do usuário e na explicação de uma ferramenta.
    hits, _ = archive.search("março", conversation="1111")
    assert [h.kind for h in hits][:2] == ["user", "tool"]
    hits, _ = archive.search("planilha", conversation="1111")
    assert [h.kind for h in hits] == ["thinking"]


def test_filtro_por_conversa_e_limite(archive):
    hits, total = archive.search("planilha", conversation="9999")
    assert total == 1 and hits[0].conversation_id == OTHER_ID
    hits, total = archive.search("aba", limit=1)
    assert len(hits) == 1 and total > 1


def test_caracteres_especiais_nao_quebram_a_busca(archive):
    for query in ['"', "a-b:c (x)", "NEAR(", "col:valor", "'", "^", "{}", "AND OR NOT", "---"]:
        hits, total = archive.search(query)
        assert isinstance(hits, list) and total >= 0


def test_mensagem_editada_e_reindexada(archive):
    edited = sample_chat()
    edited["updatedAt"] += 60_000
    edited["snapshot"]["ui"]["messages"][-1]["content"] = "Foram 7 divergências encontradas."
    ingest(archive, edited, T0 + timedelta(hours=1))
    assert archive.search("divergências", conversation="1111")[1] == 1
    assert archive.search("lançamentos conciliados", conversation="1111")[0] == []


def test_mensagem_ausente_continua_pesquisavel(archive):
    grown = sample_chat(extra_messages=6)
    grown["updatedAt"] += 60_000
    ingest(archive, grown, T0 + timedelta(hours=1))
    shrunk = sample_chat(extra_messages=5)  # a "Mensagem extra 5" sumiu da origem
    shrunk["updatedAt"] += 120_000
    ingest(archive, shrunk, T0 + timedelta(hours=2))
    hits, total = archive.search('"extra 5"', phrase=True)
    assert total == 1 and hits[0].absent is True


def test_arquivo_antigo_sem_indice_e_migrado_ao_abrir(tmp_path):
    path = tmp_path / "antigo.sqlite"
    with Archive(path) as arch:
        ingest(arch, sample_chat())
        # Simula um arquivo da Fase 3: sem tabelas de busca, esquema versão 1.
        arch._db.execute("DROP TABLE search_index")
        arch._db.execute("DROP TABLE title_index")
        arch._db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    with Archive(path) as arch:
        assert arch.search("conciliação")[1] >= 1
        assert arch._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
        assert len(arch.load_conversation(CHAT_ID).messages) == 5  # o conteúdo não mudou


def test_indice_fora_de_sincronia_e_reconstruido_ao_abrir(tmp_path):
    path = tmp_path / "arquivo.sqlite"
    with Archive(path) as arch:
        ingest(arch, sample_chat())
        arch._db.execute("DELETE FROM search_index")  # o índice é derivado: pode ser apagado
        assert arch.search("conciliação", kinds=["user"])[0] == []
    with Archive(path) as arch:
        assert arch.search("conciliação", kinds=["user"])[1] == 1


# -------------------------------------------------------------------- CLI
@pytest.fixture
def data_dir(tmp_path, monkeypatch, capsys):
    """Pasta de dados já com um 'run' feito sobre um armazenamento sintético."""
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    root = tmp_path / "webview2"
    build_sample_storage(root / "aaaa_ADAL" / "2" / "0" / "EBWebView" / "Default" / "IndexedDB").write()
    data = tmp_path / "dados"
    assert cli.main(["--data-dir", str(data), "--source-root", str(root), "run"]) == 0
    capsys.readouterr()
    return data


def test_cli_list(data_dir, capsys):
    assert cli.main(["--data-dir", str(data_dir), "list"]) == 0
    out = capsys.readouterr().out
    assert "ATUALIZADA" in out and "Excel" in out and "11111111" in out
    assert "Conciliação de teste" in out and "1 conversa(s) no arquivo" in out

    assert cli.main(["--data-dir", str(data_dir), "list", "--app", "excel"]) == 0
    assert "11111111" in capsys.readouterr().out
    assert cli.main(["--data-dir", str(data_dir), "list", "--app", "word"]) == 0
    assert "Nenhuma conversa do app 'word'" in capsys.readouterr().out


def test_cli_search(data_dir, capsys):
    assert cli.main(["--data-dir", str(data_dir), "search", "conciliacao", "marco"]) == 0
    out = capsys.readouterr().out
    assert "resultado(s) para: conciliacao marco" in out
    assert "Usuário" in out and "[conciliação]" in out and "(11111111)" in out
    assert str(Path("conversas") / "excel" / "2026") in out and "conversa.md" in out

    assert cli.main(["--data-dir", str(data_dir), "search", "termo-que-nao-existe"]) == 0
    assert "Nenhum resultado para: termo-que-nao-existe" in capsys.readouterr().out

    assert cli.main(["--data-dir", str(data_dir), "search", "--kind", "tool", "cabeçalhos"]) == 0
    assert "Ferramenta" in capsys.readouterr().out


def test_cli_sem_arquivo_orienta_a_rodar_run(tmp_path, capsys):
    for command in (["list"], ["search", "x"], ["alerts"], ["export"]):
        assert cli.main(["--data-dir", str(tmp_path / "vazio"), *command]) == 0
        assert "ainda não existe" in capsys.readouterr().out
    assert not (tmp_path / "vazio").exists()  # comandos de consulta não criam nada


def test_cli_alerts_marcar_como_visto(data_dir, tmp_path, capsys):
    empty = tmp_path / "sem-fonte"
    empty.mkdir()
    assert cli.main(["--data-dir", str(data_dir), "--source-root", str(empty), "run"]) == 0
    assert "ALERTA #1" in capsys.readouterr().out

    assert cli.main(["--data-dir", str(data_dir), "alerts"]) == 0
    out = capsys.readouterr().out
    assert "Alertas abertos: 1" in out and "alerts --ack" in out

    assert cli.main(["--data-dir", str(data_dir), "alerts", "--ack", "1"]) == 0
    out = capsys.readouterr().out
    assert "1 alerta(s) marcado(s) como visto(s)" in out and "Alertas abertos: 0" in out

    assert cli.main(["--data-dir", str(data_dir), "alerts", "--all"]) == 0
    out = capsys.readouterr().out
    assert "Histórico de alertas: 1" in out and "visto em" in out

    assert cli.main(["--data-dir", str(data_dir), "alerts", "--ack", "abc"]) == 2
    capsys.readouterr()
    # O arquivo continua inteiro depois de tudo isso.
    assert cli.main(["--data-dir", str(data_dir), "status"]) == 0
    out = capsys.readouterr().out
    assert "Arquivo permanente: 1 conversa(s), 5 mensagem(ns)" in out and "Último snapshot:" in out

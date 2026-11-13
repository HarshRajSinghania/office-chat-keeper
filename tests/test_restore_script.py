"""Testes do lado Python da estratégia B (pacote de restauração seletiva).

O script JavaScript roda em um navegador e não é executado pelo pytest; aqui
ele é conferido de forma ESTÁTICA (o que contém e o que não pode conter). O
ensaio em navegador está descrito no README.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from office_chat_keeper import cli
from office_chat_keeper.reader import UNDEFINED, SnapshotReader, TypedArray
from office_chat_keeper.restore_script import UnsupportedValue, encode_value

from idb_builder import CHAT_HISTORY_SCHEMA, FakeIndexedDB, build_sample_storage, sample_chat, v8_value

CHAT_A = "11111111-2222-3333-4444-555555555555"
CHAT_B = "99999999-8888-7777-6666-555555555555"
IDB = Path("EBWebView") / "Default" / "IndexedDB"
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "office_chat_keeper"


# ------------------------------------------------------- formato do arquivo
def test_encode_preserva_os_tipos_do_javascript():
    when = datetime(2026, 11, 1, 12, 0, 0, 864000, tzinfo=timezone.utc)
    value = {"texto": "conciliação 🎉", "n": 3, "f": 1.5, "nulo": None, "indef": UNDEFINED,
             "ok": True, "quando": when, "bytes": b"\x00\x01\xff",
             "u8": TypedArray("Uint8Array", b"\x01\x02"), "lista": [1, UNDEFINED, {"a": None}]}
    assert encode_value(value) == {
        "texto": "conciliação 🎉", "n": 3, "f": 1.5, "nulo": None,
        "indef": {"$t": "undefined"}, "ok": True,
        "quando": {"$t": "date", "v": 1793534400864},
        "bytes": {"$t": "arraybuffer", "v": base64.b64encode(b"\x00\x01\xff").decode()},
        "u8": {"$t": "typedarray", "k": "Uint8Array", "v": "AQI="},
        "lista": [1, {"$t": "undefined"}, {"a": None}],
    }


def test_encode_casos_especiais():
    assert encode_value(float("nan")) == {"$t": "number", "v": "NaN"}
    assert encode_value(float("-inf")) == {"$t": "number", "v": "-Infinity"}
    # Um objeto que já tem um campo "$t" é embrulhado, para não virar marca.
    assert encode_value({"$t": "date", "v": 1}) == {"$t": "object", "v": {"$t": "date", "v": 1}}
    for bad in ({1, 2}, 2 ** 60, TypedArray("desconhecido:x", b"")):
        with pytest.raises(UnsupportedValue):
            encode_value({"campo": bad})


def test_leitor_preserva_array_tipado(tmp_path):
    # Uint8Array no formato do V8: ArrayBuffer ('B') seguido da visão ('V', tipo 'B').
    class U8(bytes):
        pass

    real = v8_value

    def with_view(value):
        if isinstance(value, U8):
            return (b"B" + bytes([len(value)]) + bytes(value)
                    + b"V" + b"B" + b"\x00" + bytes([len(value)]) + b"\x00")
        return real(value)

    import idb_builder
    idb_builder.v8_value = with_view
    try:
        db = FakeIndexedDB(tmp_path).add_database("claude-chat-history", CHAT_HISTORY_SCHEMA, 11)
        db.put("claude-chat-history", "blobs", [CHAT_A, "img"],
               {"chatId": CHAT_A, "name": "img", "bytes": U8(b"\x89PNG")})
        db.write()
    finally:
        idb_builder.v8_value = real
    with SnapshotReader(db.leveldb_dir, db.blob_dir) as reader:
        (record,) = reader.records("claude-chat-history", "blobs")
    assert record.value["bytes"] == TypedArray("Uint8Array", b"\x89PNG")


# ---------------------------------------------------------- geração do pacote
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("office_chat_keeper.protect.restrict_to_current_user",
                        lambda path: (True, "teste"))
    data, root = tmp_path / "dados", tmp_path / "office"
    folder = root / "a_ADAL" / "2" / "0" / IDB

    def run(*command: str) -> int:
        return cli.main(["--data-dir", str(data), "--source-root", str(root), *command])

    def rewrite(chats):
        if folder.exists():
            shutil.rmtree(folder)
        build_sample_storage(folder, chats=chats, version=11).write()

    rewrite([sample_chat(), sample_chat(CHAT_B, title="Outra conversa")])
    assert run("run") == 0
    return data, root, run, rewrite


def bundle_dir(data: Path) -> Path:
    (only,) = (data / "restore").iterdir()
    return only


def test_nada_faltando_nao_gera_pacote(env, capsys):
    data, _root, run, _rewrite = env
    capsys.readouterr()
    assert run("restore-script") == 1
    assert "Nada a restaurar" in capsys.readouterr().out
    assert not (data / "restore").exists()


def test_pacote_tem_so_as_conversas_que_faltam(env, capsys):
    data, root, run, rewrite = env
    rewrite([sample_chat(CHAT_B, title="Outra conversa")])  # a conversa A some da origem
    assert run("run") == 0
    capsys.readouterr()
    before = {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}

    assert run("restore-script") == 0
    out = capsys.readouterr().out
    assert "NADA foi gravado no Office" in out and "Conversas no pacote (1):" in out
    assert "11111111  5 mensagens  Conciliação de teste" in out
    assert "a mesma da fonte de destino" in out and "comece pelo ENSAIO" in out
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()} == before  # origem intacta

    folder = bundle_dir(data)
    assert sorted(p.name for p in folder.iterdir()) == [
        "PASSOS.md", "dados-restauracao.json", "ensaio.html", "script-para-colar-no-console.txt"]
    # O script do pacote é o do projeto, sem alteração.
    assert (folder / "script-para-colar-no-console.txt").read_bytes() == (PACKAGE / "restore_script.js").read_bytes()
    raw = (folder / "dados-restauracao.json").read_bytes()
    assert max(raw) < 128  # ASCII puro: qualquer texto sobrevive à viagem
    payload = json.loads(raw)
    assert payload["format"] == 1 and payload["database"] == "claude-chat-history"
    assert payload["schema"]["version"] == 11
    assert payload["schema"]["stores"]["chats"]["key_path"] == "id"
    assert sorted(payload["schema"]["stores"]["chats"]["indexes"]) == sorted(
        CHAT_HISTORY_SCHEMA["chats"]["indexes"])
    (entry,) = payload["conversations"]
    assert (entry["id"], entry["title"], entry["messages"]) == (CHAT_A, "Conciliação de teste", 5)
    assert (entry["userId"], entry["orgId"]) == ("user-teste", "org-teste")

    # O registro é o BRUTO, completo e com os tipos marcados.
    value = entry["value"]
    assert value["snapshot"]["api"]["transcript"][0]["role"] == "user"  # estado interno incluído
    messages = value["snapshot"]["ui"]["messages"]
    assert messages[0]["timestamp"] == {"$t": "date", "v": 1793534400000}
    assert messages[0]["files"] == {"$t": "undefined"}
    assert messages[-1]["content"].endswith("🎉")
    assert "SEGREDO_DO_CODIGO" in messages[2]["input"]["code"]  # nada é resumido aqui
    (blob,) = entry["blobs"]
    assert blob["key"] == [CHAT_A, "dados"]
    assert base64.b64decode(blob["value"]["bytes"]["v"]) == b'{"linhas": 3}'
    # O token dos conectores (outro banco) não entra no pacote.
    assert b"TOKEN-SUPER-SECRETO" not in raw

    steps = (folder / "PASSOS.md").read_text(encoding="utf-8")
    assert "Parte 1: ensaio" in steps and "Parte 2: no suplemento" in steps and CHAT_A in steps
    # Os passos deixam claro qual arquivo se cola e qual se escolhe.
    assert "é o PROGRAMA" in steps and "são os DADOS" in steps and "allow pasting" in steps
    assert "script-para-colar-no-console.txt" in steps and "Unexpected token" in steps


def test_conversation_inclui_uma_conversa_presente_para_ensaio(env, capsys):
    data, _root, run, _rewrite = env
    capsys.readouterr()
    assert run("restore-script", "--conversation", "9999") == 0
    out = capsys.readouterr().out
    assert "99999999" in out and "[já está na fonte: o script vai pular]" in out
    payload = json.loads((bundle_dir(data) / "dados-restauracao.json").read_text(encoding="ascii"))
    assert [c["id"] for c in payload["conversations"]] == [CHAT_B]

    assert run("restore-script", "--conversation", "nao-existe") == 1
    assert "Nenhuma conversa com esse id" in capsys.readouterr().out


def test_sem_copia_bruta_a_conversa_fica_de_fora(env, capsys):
    data, _root, run, rewrite = env
    rewrite([sample_chat(CHAT_B, title="Outra conversa")])
    assert run("run") == 0
    for manifest in (data / "snapshots").rglob("manifest.json"):  # some com os snapshots brutos
        shutil.rmtree(manifest.parent)
    capsys.readouterr()
    assert run("restore-script", "--conversation", "1111") == 1
    assert "não há mais cópia bruta" in capsys.readouterr().out


# ------------------------------------------------- script JS: checagem estática
def js() -> str:
    return (PACKAGE / "restore_script.js").read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """Remove comentários, para as checagens não esbarrarem em explicações."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}\s])//.*$", "", text)


def test_script_nao_tem_rede_nem_execucao_dinamica():
    code = code_only(js())
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource",
                      "importScripts", "eval(", "new Function", "innerHTML", "outerHTML",
                      "document.write", "localStorage", "sessionStorage", "document.cookie",
                      "http://", "https://"):
        assert forbidden not in code, forbidden


def test_script_nunca_sobrescreve_nem_cria_banco():
    code = code_only(js())
    assert ".put(" not in code  # só add(): falha se a chave já existir
    assert code.count(".add(") == 2  # conversa e anexo
    assert "createObjectStore" not in code and "deleteObjectStore" not in code
    assert "deleteDatabase" not in code and ".clear(" not in code
    # O único delete é o do desfazer, sobre o que o próprio script inseriu.
    assert code.count(".delete(") == 1 and "async function rollback" in code
    # Não abre o banco pedindo versão (o que dispararia uma migração).
    assert "indexedDB.open(DB_NAME)" in code and "indexedDB.open(DB_NAME," not in code
    assert "transaction.abort()" in code  # se o banco não existia, aborta a criação
    assert "CONFIRM_WORD = 'GRAVAR'" in code


def test_pagina_de_ensaio_e_local():
    html = (PACKAGE / "restore_test_page.html").read_text(encoding="utf-8")
    for forbidden in ("fetch(", "XMLHttpRequest", "http://", "https://", "<link", "src="):
        assert forbidden not in html, forbidden
    assert "claude-chat-history" in html and "createObjectStore" in html

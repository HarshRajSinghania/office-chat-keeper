"""Testes do modelo de conversa e da exportação (Markdown, JSON, imagens)."""

from __future__ import annotations

import base64
import json
from datetime import timezone
from pathlib import Path

from office_chat_keeper.conversation import conversation_from_record, load_conversations
from office_chat_keeper.export import (JSON_NAME, MARKDOWN_NAME, export_conversation,
                                       redact_secrets, slugify, sniff_extension)
from office_chat_keeper.reader import SnapshotReader

from idb_builder import PNG_1X1, build_sample_storage, sample_chat

UTC = timezone.utc


def make_snapshot(tmp_path: Path, **kwargs) -> Path:
    """Cria um 'snapshot' sintético (as duas pastas lado a lado)."""
    snap = tmp_path / "snapshot"
    build_sample_storage(snap, **kwargs).write()
    return snap


def export_all(snapshot: Path, exports: Path, **kwargs):
    """Lê as conversas de um snapshot sintético e exporta cada uma."""
    with SnapshotReader.from_snapshot(snapshot) as reader:
        conversations, _warnings = load_conversations(reader)
    return [export_conversation(c, exports, tz=UTC, **kwargs) for c in conversations]


# ------------------------------------------------------------------ modelo
def test_carrega_conversa_com_mensagens_imagens_e_anexos(tmp_path):
    with SnapshotReader.from_snapshot(make_snapshot(tmp_path)) as reader:
        conversations, warnings = load_conversations(reader)

    assert warnings == []
    conv = conversations[0]
    assert (conv.id, conv.surface, conv.app) == (
        "11111111-2222-3333-4444-555555555555", "sheet", "Excel")
    assert conv.title == "Conciliação de teste"
    assert conv.created_at.isoformat() == "2026-11-01T12:00:00+00:00"
    assert conv.message_source == "ui"
    assert [m.kind for m in conv.messages] == ["user", "thinking", "tool", "tool", "assistant"]
    tool = conv.messages[2]
    assert (tool.tool_name, tool.tool_status) == ("execute_office_js", "complete")
    assert tool.images[0].data == PNG_1X1 and tool.images[0].media_type == "image/png"
    assert conv.messages[3].tool_error == "Intervalo inválido"
    assert conv.messages[0].files is None  # undefined vira None
    assert [(a.name, a.data) for a in conv.attachments] == [("dados", b'{"linhas": 3}')]


def test_sem_ui_messages_usa_o_transcript_da_api():
    image = base64.b64encode(PNG_1X1).decode()
    record = {
        "id": "c1", "surface": "sheet", "title": "Só transcript",
        "snapshot": {"version": 1, "api": {"transcript": [
            {"role": "user", "content": "Olá"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "pensando", "signature": "xxx"},
                {"type": "tool_use", "id": "tu1", "name": "ler", "input": {"explanation": "Lê A1"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": [
                    {"type": "text", "text": "valor 42"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": image}}]}]},
            {"role": "assistant", "content": [{"type": "text", "text": "É 42."}]},
        ]}},
    }
    conv = conversation_from_record(record)
    assert conv.message_source == "transcript"
    assert [m.kind for m in conv.messages] == ["user", "thinking", "tool", "assistant"]
    tool = conv.messages[2]
    assert (tool.tool_name, tool.tool_status, tool.tool_result) == ("ler", "complete", "valor 42")
    assert tool.images[0].data == PNG_1X1


def test_registro_estranho_nao_derruba():
    conv = conversation_from_record({"snapshot": "isto não é um objeto"}, key="chave-x")
    assert conv.id == "chave-x" and conv.messages == [] and conv.message_source == "none"
    conv = conversation_from_record({"id": "c2", "title": 123, "createdAt": "ontem",
                                     "snapshot": {"ui": {"messages": ["texto solto", {}]}}})
    assert conv.title is None and conv.created_at is None
    assert [m.kind for m in conv.messages] == ["desconhecido"]


# --------------------------------------------------------------- exportação
def test_exporta_markdown_json_imagens_e_anexos(tmp_path):
    exports = tmp_path / "exports"
    results = export_all(make_snapshot(tmp_path), exports)

    assert [c.status for c in results] == ["written"]
    folder = results[0].path
    assert folder == (exports / "conversas" / "excel" / "2026"
                      / "2026-11-01_conciliacao-de-teste_11111111")

    md = (folder / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert md.startswith("# Conciliação de teste\n")
    assert "**App:** Excel (`sheet`)" in md
    assert "## Usuário · 01/11/2026 12:00" in md
    assert "Faça a conciliação da aba Março." in md
    assert "## Claude · 01/11/2026 12:04 · claude-teste-1" in md
    assert "Pronto: 3 lançamentos conciliados. 🎉" in md
    assert "### Atividade do Claude · 12:01 a 12:03 (1 raciocínio(s), 2 ferramenta(s))" in md
    assert "- `execute_office_js`: Lê os cabeçalhos da aba Março" in md
    assert "- `get_cell_ranges`: sheetName=Março · **erro**: Intervalo inválido" in md
    assert "![imagem](imagens/001-" in md
    # Resumo, não despejo: nem código, nem base64, nem raciocínio por padrão.
    assert "SEGREDO_DO_CODIGO" not in md
    assert base64.b64encode(PNG_1X1).decode()[:40] not in md
    assert "Preciso ler a planilha" not in md

    images = list((folder / "imagens").iterdir())
    assert len(images) == 1 and images[0].suffix == ".png"
    assert images[0].read_bytes() == PNG_1X1
    assert (folder / "anexos" / "dados.json").read_bytes() == b'{"linhas": 3}'

    data = json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))
    assert data["message_count"] == 5 and data["surface"] == "sheet"
    assert data["messages"][1]["content"] == "Preciso ler a planilha primeiro."
    assert "SEGREDO_DO_CODIGO" in data["messages"][2]["input"]["code"]  # JSON é completo
    assert data["messages"][2]["images"][0]["file"] == f"imagens/{images[0].name}"
    assert "data" not in data["messages"][2]["images"][0]  # sem base64
    assert data["messages"][0]["timestamp"] == "2026-11-01T12:00:00.000+00:00"
    assert data["attachments"][0]["file"] == "anexos/dados.json"


def test_raciocinio_entra_no_markdown_so_quando_pedido(tmp_path):
    snap = make_snapshot(tmp_path)
    results = export_all(snap, tmp_path / "exports", include_thinking=True)
    md = (results[0].path / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert "<details><summary>Raciocínio (32 caracteres)</summary>" in md
    assert "Preciso ler a planilha primeiro." in md



def test_exportar_de_novo_sem_mudanca_nao_regrava(tmp_path):
    snap = make_snapshot(tmp_path)
    first = export_all(snap, tmp_path / "exports")
    md = first[0].path / MARKDOWN_NAME
    before = md.stat().st_mtime_ns
    second = export_all(snap, tmp_path / "exports")
    assert [c.status for c in second] == ["unchanged"]
    assert md.stat().st_mtime_ns == before


def test_conversa_menor_nao_sobrescreve_a_exportada(tmp_path):
    exports = tmp_path / "exports"
    grande = conversation_from_record(sample_chat(extra_messages=3))
    pequena = conversation_from_record(sample_chat())

    first = export_conversation(grande, exports, tz=UTC)
    second = export_conversation(pequena, exports, tz=UTC)

    assert first.status == "written" and second.status == "kept-larger"
    assert second.path == first.path
    md = (first.path / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert "Mensagem extra 2" in md
    assert json.loads((first.path / JSON_NAME).read_text(encoding="utf-8"))["message_count"] == 8


def test_conversa_que_cresce_atualiza_a_mesma_pasta_mesmo_se_o_titulo_mudar(tmp_path):
    exports = tmp_path / "exports"
    first = export_conversation(conversation_from_record(sample_chat()), exports, tz=UTC)
    maior = conversation_from_record(sample_chat(title="Título novo", extra_messages=2))
    second = export_conversation(maior, exports, tz=UTC)
    assert second.status == "written" and second.path == first.path
    assert (first.path / MARKDOWN_NAME).read_text(encoding="utf-8").startswith("# Título novo")




def test_mensagens_ausentes_aparecem_em_secao_propria(tmp_path):
    # Conversa como o arquivo permanente a devolve: 1 mensagem saiu da versão atual.
    conv = conversation_from_record(sample_chat(extra_messages=2))
    conv.absent_messages = [conv.messages.pop()]
    conv.missing_since = conv.updated_at
    result = export_conversation(conv, tmp_path / "exports", tz=UTC)

    md = (result.path / MARKDOWN_NAME).read_text(encoding="utf-8")
    head, tail = md.split("# Mensagens que não estão mais na versão atual (1)")
    assert "Mensagem extra 0" in head and "Mensagem extra 1" not in head
    assert "Mensagem extra 1" in tail
    assert "**Situação:** não aparece mais na origem desde 01/11/2026 12:10" in head
    data = json.loads((result.path / JSON_NAME).read_text(encoding="utf-8"))
    assert (data["message_count"], data["absent_message_count"]) == (6, 1)
    assert data["absent_messages"][0]["content"] == "Mensagem extra 1"

    # A trava "nunca piorar" conta ativas + ausentes: reexportar não é bloqueado.
    assert export_conversation(conv, tmp_path / "exports", tz=UTC).status == "unchanged"


# ---------------------------------------------------------------- utilidades
def test_redacao_de_credenciais():
    assert "sk-ant-" not in redact_secrets("chave sk-ant-api03-AbCdEfGhIjKlMnOpQrSt fim")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    assert jwt not in redact_secrets(f"token {jwt}")
    out = redact_secrets('Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345')
    assert "abcdefghijklmnop" not in out and "Bearer" in out
    out = redact_secrets('{"access_token": "ABCDEFGHIJKLMNOPQRSTUVWX"}')
    assert "ABCDEFGHIJKLMNOP" not in out and "access_token" in out
    normal = "Conciliação da conta 12345-6: saldo R$ 1.234,56"
    assert redact_secrets(normal) == normal


def test_slugify_e_extensoes():
    assert slugify("Monte o orçamento de março para a reu…") == "monte-o-orcamento-de-marco-para-a-reu"
    assert slugify("") == "sem-titulo" and slugify("???") == "sem-titulo"
    assert len(slugify("a" * 200)) == 50
    assert sniff_extension(PNG_1X1) == ".png"
    assert sniff_extension(b'[{"a": 1}]') == ".json"
    assert sniff_extension("texto com acentuação".encode()) == ".txt"
    assert sniff_extension(b"\x00\xff\xfe\x80") == ".bin"

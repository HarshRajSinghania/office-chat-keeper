"""Fase 2c - Exportação legível: Markdown + JSON + imagens + anexos.

Estrutura gerada::

    exports\\conversas\\<app>\\<ano>\\<AAAA-MM-DD>_<titulo>_<id8>\\
        conversa.md      leitura humana
        conversa.json    tudo estruturado (inclui raciocínios e entradas de ferramenta)
        imagens\\001-ab12cd34.png
        anexos\\<nome>.json
    exports\\skills\\...   e   exports\\memoria\\...

Decisões
--------
* **Markdown enxuto.** Mensagens do usuário e do Claude aparecem inteiras. O
  que acontece entre elas (raciocínios e chamadas de ferramenta) vira um bloco
  "Atividade do Claude": uma linha por ferramenta, com nome e explicação curta.
  Código e resultados grandes ficam só no JSON.
* **Imagens fora do texto.** O suplemento guarda imagens em base64 (texto
  gigante). Aqui elas são decodificadas e salvas como arquivos; o Markdown só
  aponta para elas. Imagens repetidas são salvas uma vez (mesmo SHA-256).
* **Sem tokens.** O banco de conectores nunca é lido, e todo texto exportado
  passa por um filtro que troca padrões com cara de credencial por um aviso.
* **A fonte é o arquivo permanente.** A partir da Fase 3 a exportação é feita
  do arquivo (que só cresce), não direto do snapshot. Mensagens que sumiram da
  versão atual aparecem em uma seção própria no fim, em vez de desaparecer.
* **Nunca piorar uma exportação.** Como segunda linha de defesa, se a conversa
  tiver MENOS mensagens do que a já exportada, os arquivos antigos são mantidos.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from . import __version__
from .conversation import Conversation, Message
from .reader import UNDEFINED, TypedArray

EXPORT_FORMAT = 1
MARKDOWN_NAME = "conversa.md"
JSON_NAME = "conversa.json"

# Bancos "extras" exportados de forma genérica: nome do banco -> pasta.
EXTRA_DATABASES = {
    "claude-local-skills": "skills",
    "claude-memory": "memoria",
}

_MEDIA_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "application/pdf": ".pdf",
}

# ---------------------------------------------------------------- redação
# Padrões com "cara de credencial". Não é um detector perfeito: é uma rede de
# segurança para o caso de um token ter sido colado dentro de uma conversa.
_REDACTED = "[REMOVIDO: possível credencial]"
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)(?<=bearer )[A-Za-z0-9._~+/\-]{20,}=*"),
    re.compile(r"(?i)\b(?:access_token|refresh_token|client_secret|api[_-]?key)"
               r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9._~+/\-]{16,})"),
]


def redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS[:3]:
        text = pattern.sub(_REDACTED, text)
    # No último padrão preservamos o nome do campo e trocamos só o valor.
    return _SECRET_PATTERNS[3].sub(
        lambda m: m.group(0)[: m.start(2) - m.start(0)] + _REDACTED, text)


# -------------------------------------------------------------- utilidades
def slugify(text: str | None, max_length: int = 50) -> str:
    """'Orçamento de março…' -> 'orcamento-de-marco' (seguro p/ nome de pasta)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:max_length].strip("-") or "sem-titulo"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\-]+", "_", name).strip("._")
    return cleaned[:80] or "arquivo"


def _oneline(text: str, limit: int) -> str:
    """Junta espaços/quebras e corta em ``limit`` caracteres."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def sniff_extension(data: bytes) -> str:
    """Adivinha a extensão pelo começo do arquivo (os "bytes mágicos")."""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"%PDF"):
        return ".pdf"
    if data.startswith(b"PK\x03\x04"):
        return ".zip"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ".bin"
    if text.lstrip()[:1] in ("{", "["):
        try:
            json.loads(text)
            return ".json"
        except ValueError:
            pass
    return ".txt"


def jsonable(value: Any) -> Any:
    """Converte qualquer valor lido em algo que o ``json`` aceite.

    Bytes NÃO viram base64 (nada de despejar binário no JSON): ficam como um
    resumo com tamanho e hash. Textos passam pelo filtro de credenciais.
    """
    if value is UNDEFINED or value is None:
        return None
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    if isinstance(value, TypedArray):
        value = value.data
    if isinstance(value, bytes):
        return {"$bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return str(value)


def _write_if_changed(path: Path, data: bytes) -> bool:
    """Grava de forma atômica (arquivo temporário + troca). Devolve se gravou."""
    try:
        if path.stat().st_size == len(data) and path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def _fmt(ts: datetime | None, tz: tzinfo | None, pattern: str = "%d/%m/%Y %H:%M") -> str:
    return ts.astimezone(tz).strftime(pattern) if ts else "data desconhecida"


# ------------------------------------------------------------- Markdown
_STATUS_LABELS = {"complete": "concluída", "error": "erro"}
_ROLE_LABELS = {"user": "Usuário", "assistant": "Claude"}


def describe_tool(msg: Message) -> str:
    """Uma linha explicando a chamada de ferramenta, sem despejar código."""
    name = msg.tool_name or "ferramenta"
    parts: list[str] = []
    data = msg.tool_input if isinstance(msg.tool_input, dict) else {}
    explanation = data.get("explanation")
    if isinstance(explanation, str) and explanation.strip():
        parts.append(_oneline(explanation, 200))
    elif isinstance(data.get("summary"), str):
        parts.append("resumo: " + _oneline(data["summary"], 200))
    elif isinstance(data.get("todos"), list):
        parts.append(f"{len(data['todos'])} tarefa(s)")
    elif "questions" in data:
        parts.append("pergunta: " + _oneline(json.dumps(jsonable(data["questions"]),
                                                        ensure_ascii=False), 300))
        if msg.tool_result is not None:
            parts.append("resposta: " + _oneline(json.dumps(jsonable(msg.tool_result),
                                                            ensure_ascii=False), 300))
    else:
        small = [f"{k}={_oneline(v, 60)}" for k, v in data.items()
                 if k != "code" and isinstance(v, (str, int, float, bool))]
        if small:
            parts.append(_oneline(", ".join(small), 200))

    if msg.tool_status == "error" or msg.tool_error:
        parts.append("**erro**" + (": " + _oneline(msg.tool_error, 300) if msg.tool_error else ""))
    elif msg.tool_status and msg.tool_status != "complete":
        parts.append(_STATUS_LABELS.get(msg.tool_status, msg.tool_status))
    text = " · ".join(parts)
    return f"`{name}`" + (f": {redact_secrets(text)}" if text else "")


def _render_activity(run: list[Message], image_files: dict[str, str],
                     include_thinking: bool, tz: tzinfo | None) -> list[str]:
    thinking = sum(1 for m in run if m.kind == "thinking")
    tools = sum(1 for m in run if m.kind == "tool")
    counts = []
    if thinking:
        counts.append(f"{thinking} raciocínio(s)")
    if tools:
        counts.append(f"{tools} ferramenta(s)")
    start = next((m.timestamp for m in run if m.timestamp), None)
    end = next((m.end_timestamp or m.timestamp for m in reversed(run)
                if m.end_timestamp or m.timestamp), None)
    when = ""
    if start:
        when = _fmt(start, tz, "%H:%M")
        if end and _fmt(end, tz, "%H:%M") != when:
            when += " a " + _fmt(end, tz, "%H:%M")
        when = f" · {when}"
    lines = [f"### Atividade do Claude{when} ({', '.join(counts)})", ""]
    for m in run:
        if m.kind == "tool":
            lines.append("- " + describe_tool(m))
            for image in m.images:
                rel = image_files.get(image.sha256)
                if rel:
                    lines.append(f"  ![imagem]({rel})")
        elif include_thinking and m.text:
            lines += ["", f"<details><summary>Raciocínio ({len(m.text)} caracteres)</summary>", "",
                      redact_secrets(m.text), "", "</details>", ""]
    lines.append("")
    return lines


def render_markdown(conv: Conversation, image_files: dict[str, str],
                    attachment_files: list[str], *, include_thinking: bool = False,
                    tz: tzinfo | None = None) -> str:
    """Gera o texto do ``conversa.md``. ``tz=None`` usa o fuso do computador.

    O texto depende só da conversa (não leva a data da exportação), então
    exportar duas vezes a mesma conversa produz o mesmo arquivo.
    """
    counts = {kind: sum(1 for m in conv.messages if m.kind == kind)
              for kind in ("user", "assistant", "thinking", "tool")}
    models = sorted({m.model for m in conv.messages if m.model})
    title = redact_secrets(conv.title or "Sem título")
    surface = f" (`{conv.surface}`)" if conv.surface else ""
    lines = [
        f"# {title}", "",
        f"- **App:** {conv.app}{surface}",
        f"- **Conversa:** `{conv.id}`",
        f"- **Arquivo (fileId):** `{conv.file_id or 'não informado'}`",
        f"- **Criada em:** {_fmt(conv.created_at, tz)}",
        f"- **Atualizada em:** {_fmt(conv.updated_at, tz)}",
        f"- **Mensagens:** {counts['user']} do usuário, {counts['assistant']} do Claude, "
        f"{counts['thinking']} raciocínio(s), {counts['tool']} chamada(s) de ferramenta",
    ]
    if models:
        lines.append(f"- **Modelos:** {', '.join(models)}")
    if image_files:
        lines.append(f"- **Imagens:** {len(image_files)} (pasta `imagens/`)")
    if attachment_files:
        lines.append(f"- **Anexos:** {len(attachment_files)} (pasta `anexos/`)")
    if conv.missing_since:
        lines.append(f"- **Situação:** não aparece mais na origem desde "
                     f"{_fmt(conv.missing_since, tz)}; preservada pelo arquivo permanente")
    lines += [
        "",
        f"> Exportado por office-chat-keeper (ferramenta não oficial). Raciocínios, código e "
        f"resultados completos das ferramentas estão em `{JSON_NAME}`.",
        "", "---", "",
    ]
    lines += _render_messages(conv.messages, image_files, include_thinking, tz)
    if conv.absent_messages:
        lines += [
            "---", "",
            f"# Mensagens que não estão mais na versão atual ({len(conv.absent_messages)})", "",
            "> Existiram em versões anteriores desta conversa e sumiram da origem "
            "(apagadas, refeitas ou perdidas). O arquivo permanente as preserva.", "",
        ]
        lines += _render_messages(conv.absent_messages, image_files, include_thinking, tz)
    return "\n".join(lines).rstrip() + "\n"


def _render_messages(messages: list[Message], image_files: dict[str, str],
                     include_thinking: bool, tz: tzinfo | None) -> list[str]:
    lines: list[str] = []
    run: list[Message] = []
    for msg in messages:
        if msg.kind in ("thinking", "tool"):
            run.append(msg)
            continue
        if run:
            lines += _render_activity(run, image_files, include_thinking, tz)
            run = []
        heading = f"## {_ROLE_LABELS.get(msg.kind, msg.kind)} · {_fmt(msg.timestamp, tz)}"
        if msg.model:
            heading += f" · {msg.model}"
        lines += [heading, "", redact_secrets(msg.text or "*(sem texto)*"), ""]
        if msg.files:
            lines += ["Arquivos enviados: " + _oneline(json.dumps(jsonable(msg.files),
                                                                 ensure_ascii=False), 400), ""]
        for image in msg.images:
            rel = image_files.get(image.sha256)
            if rel:
                lines += [f"![imagem]({rel})", ""]
    if run:
        lines += _render_activity(run, image_files, include_thinking, tz)
    return lines


# ----------------------------------------------------------------- JSON
def conversation_to_json(conv: Conversation, image_files: dict[str, str],
                         attachment_files: dict[str, str]) -> dict:
    """Versão estruturada e completa da conversa, sem base64 e sem tokens."""

    def convert(m: Message) -> dict[str, Any]:
        item: dict[str, Any] = {"uuid": m.uuid, "type": m.kind,
                                "timestamp": jsonable(m.timestamp)}
        optional = {
            "end_timestamp": jsonable(m.end_timestamp), "content": jsonable(m.text),
            "model": m.model, "effort": m.effort, "api_id": m.api_id,
            "name": m.tool_name, "status": m.tool_status,
            "input": jsonable(m.tool_input), "result": jsonable(m.tool_result),
            "error": jsonable(m.tool_error), "result_chars": m.result_chars,
            "files": jsonable(m.files),
        }
        item.update({k: v for k, v in optional.items() if v is not None})
        if m.images:
            item["images"] = [{"file": image_files.get(i.sha256), "media_type": i.media_type,
                               "sha256": i.sha256, "bytes": len(i.data)} for i in m.images]
        if m.extra:
            item["extra"] = jsonable(m.extra)
        return item

    return {
        "format": EXPORT_FORMAT,
        "exported_by": f"office-chat-keeper {__version__}",
        "id": conv.id,
        "title": jsonable(conv.title),
        "surface": conv.surface,
        "app": conv.app,
        "file_id": conv.file_id,
        "user_id": conv.user_id,
        "org_id": conv.org_id,
        "host_origin": conv.host_origin,
        "created_at": jsonable(conv.created_at),
        "updated_at": jsonable(conv.updated_at),
        "message_source": conv.message_source,
        "missing_since": jsonable(conv.missing_since),
        "message_count": len(conv.messages),
        "absent_message_count": len(conv.absent_messages),
        "messages": [convert(m) for m in conv.messages],
        # Mensagens de versões anteriores que sumiram da versão atual.
        "absent_messages": [convert(m) for m in conv.absent_messages],
        "attachments": [{"name": a.name, "file": attachment_files.get(a.name),
                         "bytes": len(a.data), "sha256": hashlib.sha256(a.data).hexdigest()}
                        for a in conv.attachments],
    }


# ------------------------------------------------------ gravação em disco
@dataclass
class ConversationExport:
    conversation_id: str
    path: Path
    status: str  # "written" | "unchanged" | "kept-larger"
    messages: int = 0
    images: int = 0
    attachments: int = 0


def conversation_dir(exports_dir: Path, conv: Conversation, tz: tzinfo | None = None) -> Path:
    """Pasta da conversa: por app e por ano. Reaproveita a pasta se já existir.

    O id curto no fim do nome é o que identifica a conversa; assim, se o título
    mudar, a exportação continua indo para a mesma pasta.
    """
    base = exports_dir / "conversas"
    id8 = _safe_name(conv.id)[:8]
    existing = sorted(base.glob(f"*/*/*_{id8}")) if base.is_dir() else []
    if existing:
        return existing[0]
    created = conv.created_at.astimezone(tz) if conv.created_at else None
    year = created.strftime("%Y") if created else "sem-data"
    day = created.strftime("%Y-%m-%d") if created else "sem-data"
    return base / slugify(conv.app, 30) / year / f"{day}_{slugify(conv.title)}_{id8}"


def _existing_message_count(target: Path) -> int | None:
    try:
        data = json.loads((target / JSON_NAME).read_text(encoding="utf-8"))
        count = data.get("message_count")
        absent = data.get("absent_message_count") or 0
        return count + absent if isinstance(count, int) and isinstance(absent, int) else None
    except (OSError, ValueError, AttributeError):
        return None


def export_conversation(conv: Conversation, exports_dir: Path, *,
                        include_thinking: bool = False,
                        tz: tzinfo | None = None) -> ConversationExport:
    target = conversation_dir(exports_dir, conv, tz)
    all_messages = conv.messages + conv.absent_messages
    previous = _existing_message_count(target)
    if previous is not None and len(all_messages) < previous:
        # Regra de ouro: nunca trocar uma exportação boa por uma menor. Vindo
        # do arquivo permanente o total nunca diminui; esta trava é a segunda
        # linha de defesa (ex.: arquivo apagado e recriado do zero).
        return ConversationExport(conv.id, target, "kept-larger", previous)

    # Imagens: numeradas pela ordem em que aparecem; repetidas, uma vez só.
    image_files: dict[str, str] = {}
    image_data: dict[str, bytes] = {}
    for msg in all_messages:
        for image in msg.images:
            if image.sha256 not in image_files:
                ext = _MEDIA_EXTENSIONS.get(image.media_type) or sniff_extension(image.data)
                number = len(image_files) + 1
                image_files[image.sha256] = f"imagens/{number:03d}-{image.sha256[:8]}{ext}"
                image_data[image.sha256] = image.data

    attachment_files: dict[str, str] = {}
    attachment_data: dict[str, bytes] = {}
    used: set[str] = set()
    for att in conv.attachments:
        stem = _safe_name(att.name)
        rel = f"anexos/{stem}{sniff_extension(att.data)}"
        counter = 2
        while rel.lower() in used:
            rel = f"anexos/{stem}-{counter}{sniff_extension(att.data)}"
            counter += 1
        used.add(rel.lower())
        attachment_files[att.name] = rel
        attachment_data[rel] = att.data

    changed = False
    for sha, rel in image_files.items():
        changed |= _write_if_changed(target / rel, image_data[sha])
    for rel, data in attachment_data.items():
        changed |= _write_if_changed(target / rel, data)

    payload = conversation_to_json(conv, image_files, attachment_files)
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    changed |= _write_if_changed(target / JSON_NAME, json_bytes)
    markdown = render_markdown(conv, image_files, sorted(attachment_files.values()),
                               include_thinking=include_thinking, tz=tz)
    changed |= _write_if_changed(target / MARKDOWN_NAME, markdown.encode("utf-8"))
    return ConversationExport(conv.id, target, "written" if changed else "unchanged",
                              len(conv.messages), len(image_files), len(attachment_files))


# ------------------------------------- skills e memória (genérico)
def _extract_blobs(value: Any, folder: Path, load_blob, trail: str = "") -> int:
    """Salva como arquivo todo binário referenciado dentro de um registro.

    No arquivo permanente, um binário aparece como ``{"$blob": <hash>}``.
    """
    count = 0
    if isinstance(value, dict):
        if "$blob" in value:
            data = load_blob(value["$blob"])
            if data is not None:
                name = _safe_name(trail or "conteudo") + sniff_extension(data)
                _write_if_changed(folder / name, data)
                return 1
            return 0
        for key, item in value.items():
            count += _extract_blobs(item, folder, load_blob, f"{trail}.{key}" if trail else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            count += _extract_blobs(item, folder, load_blob, f"{trail}.{index}")
    return count


_TEXT_FIELDS = ("content", "markdown", "body", "text", "instructions", "note", "value")


def export_extras(archive, exports_dir: Path) -> dict[str, int]:
    """Exporta os bancos auxiliares (skills, memória) a partir do arquivo permanente.

    Não havia registros desses bancos no armazenamento usado para testar, então
    a exportação é GENÉRICA, sem supor o formato: para cada registro grava
    ``registro.json`` (tudo o que é texto/número), extrai os binários como
    arquivos e, se houver um campo de texto com nome típico (content,
    markdown...), grava-o em ``.md``. Exporta a versão mais recente de cada
    registro; as anteriores continuam no arquivo.
    """
    counts: dict[str, int] = {}
    for row in archive.latest_extra_records():
        folder_name = EXTRA_DATABASES.get(row["database"])
        if folder_name is None:
            continue
        value = json.loads(row["payload"])
        folder = exports_dir / folder_name / _safe_name(row["store"]) / _safe_name(row["key"])
        payload = {"database": row["database"], "store": row["store"], "key": row["key"],
                   "first_seen_at": row["first_seen_at"], "value": jsonable(value)}
        _write_if_changed(folder / "registro.json",
                          json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        _extract_blobs(value, folder, archive.load_blob)
        if isinstance(value, dict):
            for name in _TEXT_FIELDS:
                text = value.get(name)
                if isinstance(text, str) and text.strip():
                    _write_if_changed(folder / f"{name}.md", redact_secrets(text).encode("utf-8"))
        counts[row["database"]] = counts.get(row["database"], 0) + 1
    return counts

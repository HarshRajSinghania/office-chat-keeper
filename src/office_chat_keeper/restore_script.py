"""Fase 6c - Estratégia B: inserir no suplemento só as conversas que faltam.

A ideia
-------
Em vez de trocar o banco inteiro (estratégia A), devolvemos ao IndexedDB do
suplemento apenas os registros que faltam, usando a API oficial do navegador
(``indexedDB``). Quem grava é o próprio navegador, com o suplemento aberto;
nenhum arquivo do LevelDB é tocado por nós.

Este módulo NÃO grava nada no Office. Ele só prepara um pacote, na pasta de
dados, com:

* ``dados-restauracao.json`` - os registros brutos das conversas, lidos do
  snapshot, com o esquema do banco de onde vieram;
* ``script-para-colar-no-console.txt`` - o script que VOCÊ cola no Console
  das ferramentas de desenvolvedor do painel do Claude;
* ``ensaio.html``            - uma página local para ensaiar tudo em um banco
  descartável, antes de chegar perto do suplemento;
* ``PASSOS.md``              - o passo a passo.

Por que precisa de um formato próprio de arquivo
------------------------------------------------
O registro de uma conversa é um objeto JavaScript, e o JSON não sabe
representar tudo o que o JavaScript tem: ``undefined``, datas (``Date``) e
binários (``ArrayBuffer``, ``Uint8Array``) não existem em JSON. Para a conversa
voltar IDÊNTICA, esses valores vão marcados: ``{"$t": "date", "v": 1793...}``.
O script desfaz a marcação na hora de gravar e a refaz para conferir.

O registro vem do SNAPSHOT BRUTO, não do arquivo permanente: o arquivo guarda
a forma legível, e o suplemento precisa do registro completo (inclusive o
estado interno que ele usa para continuar a conversa).
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .archive import Archive
from .config import Config
from .conversation import BLOBS_STORE, CHAT_DATABASE, CHATS_STORE
from .discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME, Source
from .reader import UNDEFINED, SnapshotReader, TypedArray
from .snapshot import MANIFEST_NAME, list_snapshots

log = logging.getLogger(__name__)

PAYLOAD_FORMAT = 1
PAYLOAD_NAME = "dados-restauracao.json"
# O script vai como .txt de propósito: no Windows, um duplo clique em um .js
# tenta EXECUTÁ-LO (Windows Script Host) em vez de abri-lo. Como .txt ele abre
# no Bloco de Notas, pronto para copiar. O nome diz o que fazer com ele.
SCRIPT_NAME = "script-para-colar-no-console.txt"
TEST_PAGE_NAME = "ensaio.html"
STEPS_NAME = "PASSOS.md"
_PACKAGE_DIR = Path(__file__).parent
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SAFE_INTEGER = 2 ** 53  # acima disso o JavaScript não representa inteiros exatos


class RestoreScriptError(Exception):
    pass


class UnsupportedValue(Exception):
    """O registro tem um valor que não sabemos devolver fielmente ao navegador."""


def encode_value(value: Any) -> Any:
    """Valor lido do snapshot -> JSON com marcas ``$t`` para os tipos do JavaScript."""
    if value is UNDEFINED:
        return {"$t": "undefined"}
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise UnsupportedValue("inteiro grande demais (provável BigInt)")
        return value
    if isinstance(value, float):
        if value != value:
            return {"$t": "number", "v": "NaN"}
        if value in (float("inf"), float("-inf")):
            return {"$t": "number", "v": "Infinity" if value > 0 else "-Infinity"}
        return value
    if isinstance(value, datetime):
        delta = value - _EPOCH if value.tzinfo else value.replace(tzinfo=timezone.utc) - _EPOCH
        micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        return {"$t": "date", "v": micros // 1000 if micros % 1000 == 0 else micros / 1000}
    if isinstance(value, bytes):
        return {"$t": "arraybuffer", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, TypedArray):
        if value.kind.startswith("desconhecido"):
            raise UnsupportedValue(f"array tipado {value.kind}")
        return {"$t": "typedarray", "k": value.kind,
                "v": base64.b64encode(value.data).decode("ascii")}
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        encoded = {str(key): encode_value(item) for key, item in value.items()}
        # Um objeto que por acaso tem um campo "$t" é embrulhado, para não ser
        # confundido com uma das nossas marcas.
        return {"$t": "object", "v": encoded} if "$t" in value else encoded
    raise UnsupportedValue(f"tipo {type(value).__name__}")


@dataclass
class Candidate:
    """Uma conversa do arquivo que pode ser levada de volta ao suplemento."""

    id: str
    title: str | None
    surface: str | None
    user_id: str | None
    org_id: str | None
    messages: int
    updated_at: str | None
    snapshot_dir: Path | None
    snapshot_label: str | None
    present: bool  # já está na fonte de destino (o script só a pularia)


@dataclass
class Bundle:
    folder: Path
    included: list[Candidate] = field(default_factory=list)
    skipped: list[tuple[Candidate, str]] = field(default_factory=list)  # (conversa, motivo)
    payload_bytes: int = 0
    schema_version: int | None = None
    target_identities: list[tuple[str | None, str | None]] = field(default_factory=list)


def _present_ids(cfg: Config, archive: Archive, target: Source) -> set[str]:
    """Conversas que estão na fonte de destino, segundo o snapshot mais recente dela."""
    snaps = list_snapshots(cfg.snapshots_dir / target.source_id)
    if not snaps:
        return set()
    latest = snaps[-1].path
    blob = latest / BLOB_DIR_NAME
    with SnapshotReader(latest / LEVELDB_DIR_NAME, blob if blob.is_dir() else None) as reader:
        if CHATS_STORE not in reader.schema().get(CHAT_DATABASE, []):
            return set()
        return {key for key in reader.keys(CHAT_DATABASE, CHATS_STORE) if isinstance(key, str)}


def find_candidates(cfg: Config, archive: Archive, target: Source,
                    only: list[str] | None = None) -> list[Candidate]:
    """Sem ``only``: as conversas do arquivo que faltam na fonte.

    Com ``only`` (inícios de id): exatamente essas, mesmo que já estejam na
    fonte (útil para ensaiar; o script pula o que já existe).
    """
    present = _present_ids(cfg, archive, target)
    result: list[Candidate] = []
    for row in archive.summaries():
        if only:
            if not any(row["id"].startswith(prefix) for prefix in only):
                continue
        elif row["id"] in present:
            continue
        snapshot_dir = None
        if row["last_source_id"] and row["last_snapshot"]:
            candidate_dir = cfg.snapshots_dir / row["last_source_id"] / row["last_snapshot"]
            if (candidate_dir / MANIFEST_NAME).is_file():
                snapshot_dir = candidate_dir
        result.append(Candidate(
            id=row["id"], title=row["title"], surface=row["surface"], user_id=row["user_id"],
            org_id=row["org_id"], messages=row["active_messages"], updated_at=row["updated_at"],
            snapshot_dir=snapshot_dir,
            snapshot_label=f"{row['last_source_id']}/{row['last_snapshot']}" if snapshot_dir else None,
            present=row["id"] in present))
    return result


def _read_raw(snapshot_dir: Path, ids: set[str]) -> tuple[dict, dict[str, Any], dict[str, list]]:
    """Lê do snapshot o esquema e os registros brutos (conversa + anexos) pedidos."""
    blob = snapshot_dir / BLOB_DIR_NAME
    with SnapshotReader(snapshot_dir / LEVELDB_DIR_NAME, blob if blob.is_dir() else None) as reader:
        schema = reader.schema_details().get(CHAT_DATABASE)
        chats = {record.key: record.value
                 for record in reader.records(CHAT_DATABASE, CHATS_STORE,
                                              only=lambda k: isinstance(k, str) and k in ids)
                 if not record.deleted and isinstance(record.value, dict)}
        blobs: dict[str, list] = {}
        if schema and BLOBS_STORE in schema.get("stores", {}):
            wanted = lambda key: isinstance(key, list) and bool(key) and key[0] in ids  # noqa: E731
            for record in reader.records(CHAT_DATABASE, BLOBS_STORE, only=wanted):
                if not record.deleted and record.value is not None:
                    blobs.setdefault(record.key[0], []).append((record.key, record.value))
    return schema or {}, chats, blobs


def _message_count(value: dict) -> int | None:
    try:
        messages = value["snapshot"]["ui"]["messages"]
        return len(messages) if isinstance(messages, list) else None
    except (KeyError, TypeError):
        return None


def build_payload(candidates: list[Candidate], now: datetime) -> tuple[dict, list[Candidate],
                                                                       list[tuple[Candidate, str]]]:
    """Monta o conteúdo do arquivo de dados. Devolve (payload, incluídas, puladas)."""
    included: list[Candidate] = []
    skipped: list[tuple[Candidate, str]] = []
    by_snapshot: dict[Path, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.snapshot_dir is None:
            skipped.append((candidate, "não há mais cópia bruta (snapshot) desta conversa"))
        else:
            by_snapshot.setdefault(candidate.snapshot_dir, []).append(candidate)

    schema: dict | None = None
    conversations = []
    for snapshot_dir, group in sorted(by_snapshot.items()):
        snap_schema, chats, blobs = _read_raw(snapshot_dir, {c.id for c in group})
        if schema is None:
            schema = snap_schema
        for candidate in group:
            if snap_schema != schema:
                skipped.append((candidate, "o snapshot dela tem um esquema de banco diferente "
                                           "do das demais; gere um pacote separado"))
                continue
            value = chats.get(candidate.id)
            if value is None:
                skipped.append((candidate, "o registro não foi encontrado no snapshot"))
                continue
            try:
                entry = {
                    "id": candidate.id,
                    "title": value.get("title") if isinstance(value.get("title"), str) else None,
                    "surface": value.get("surface") if isinstance(value.get("surface"), str) else None,
                    "userId": value.get("userId") if isinstance(value.get("userId"), str) else None,
                    "orgId": value.get("orgId") if isinstance(value.get("orgId"), str) else None,
                    "messages": _message_count(value),
                    "snapshot": candidate.snapshot_label,
                    "value": encode_value(value),
                    "blobs": [{"key": key, "value": encode_value(blob_value)}
                              for key, blob_value in sorted(blobs.get(candidate.id, []),
                                                            key=lambda item: str(item[0]))],
                }
            except UnsupportedValue as exc:
                skipped.append((candidate, f"contém um valor que não sei restaurar fielmente ({exc})"))
                continue
            conversations.append(entry)
            included.append(candidate)

    payload = {
        "format": PAYLOAD_FORMAT,
        "tool": f"office-chat-keeper {__version__}",
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "database": CHAT_DATABASE,
        "schema": schema or {"version": None, "stores": {}},
        "conversations": conversations,
    }
    return payload, included, skipped


def _steps_text(bundle: Bundle, target: Source) -> str:
    lines = [
        "# Restauração seletiva: passo a passo", "",
        "> Ferramenta não oficial. Restaurar dentro do suplemento não é suportado oficialmente "
        "e pode deixar de funcionar com uma atualização. O caminho seguro é sempre anexar o "
        "Markdown exportado a uma conversa nova.", "",
        f"Este pacote tem {len(bundle.included)} conversa(s):", "",
    ]
    for c in bundle.included:
        lines.append(f"- `{c.id}` · {c.messages} mensagens · {c.title or 'Sem título'}"
                     + ("  (já está na fonte; o script vai pular)" if c.present else ""))
    for c, reason in bundle.skipped:
        lines.append(f"- NÃO incluída: `{c.id}` · {c.title or 'Sem título'}: {reason}")
    lines += [
        "", f"Esquema esperado: banco `{CHAT_DATABASE}`, versão {bundle.schema_version}.",
        "", "Os arquivos deste pacote contêm o conteúdo das conversas. Não os compartilhe.",
        "", "## Os dois arquivos, para não confundir", "",
        f"- `{SCRIPT_NAME}` é o PROGRAMA. É o único arquivo cujo conteúdo você **cola** no "
        "Console. Ele começa com `/*` e, na segunda linha, "
        "`office-chat-keeper - restauração seletiva`.",
        f"- `{PAYLOAD_NAME}` são os DADOS. Você nunca cola o conteúdo dele: você o **escolhe** "
        "em uma janela \"Abrir\", clicando em **Escolher arquivo**. Se o conteúdo dele for colado "
        "no Console, aparece o erro `Unexpected token ':'`.",
        "", "## Parte 1: ensaio (não toca no suplemento)", "",
        f"1. Abra `{TEST_PAGE_NAME}` no Edge ou no Chrome (duplo clique).",
        f"2. Na página, clique em **Escolher arquivo**, selecione `{PAYLOAD_NAME}` e clique em "
        "**Criar banco vazio**.",
        f"3. Abra `{SCRIPT_NAME}` com um duplo clique (abre no Bloco de Notas). Pressione Ctrl+A "
        "para selecionar tudo e Ctrl+C para copiar.",
        "4. De volta à página, pressione F12 e abra a aba **Console**. Clique na linha de "
        "digitação, pressione Ctrl+V e depois Enter. Um painel aparece no canto da página.",
        "   - Na primeira vez o navegador pode bloquear a colagem e pedir que você digite "
        "`allow pasting` (ou `permitir colar`) e pressione Enter. É uma proteção contra colar "
        "código desconhecido. Digite, pressione Enter e cole de novo.",
        f"5. No PAINEL (não no Console), clique em **Escolher arquivo** e selecione "
        f"`{PAYLOAD_NAME}`. Leia a simulação.",
        "6. Digite `GRAVAR` no painel e clique em **Gravar**.",
        "7. Feche o painel e clique em **Mostrar o que há no banco**: as conversas devem estar lá.",
        "8. Para ver o \"já existe\": cole o script de novo e repita o passo 5. Agora não deve "
        "haver nada a inserir.",
        "", "## Parte 2: no suplemento (só por sua decisão)", "",
        "Só faça depois que o ensaio der certo.", "",
        "1. Rode `office-chat-keeper run`, para o estado atual ficar no backup.",
        "2. Abra o app do Office com o painel do Claude e clique dentro do painel.",
        "3. Pressione Ctrl+Shift+I para abrir as ferramentas de desenvolvedor do painel.",
        f"4. Na aba **Console**, cole o conteúdo de `{SCRIPT_NAME}` (copiado do Bloco de Notas, "
        "como no ensaio) e pressione Enter.",
        f"5. No painel que aparece, clique em **Escolher arquivo**, selecione `{PAYLOAD_NAME}` e "
        "leia a simulação. Confira principalmente a linha do esquema e os avisos de identidade.",
        "6. Se estiver de acordo, digite `GRAVAR` e clique em **Gravar**.",
        "7. Feche e reabra o painel do Claude e veja se as conversas aparecem.",
        "8. Rode `office-chat-keeper run` de novo.",
        "", "O script não apaga nem altera conversas existentes, grava tudo em uma única "
        "transação e, se a conferência final falhar, remove só o que ele mesmo inseriu.",
        "", f"Destino considerado ao gerar este pacote: fonte `{target.source_id}`.",
    ]
    return "\n".join(lines) + "\n"


def create_bundle(cfg: Config, archive: Archive, target: Source, *,
                  only: list[str] | None = None, now: datetime | None = None) -> Bundle:
    """Gera o pacote de restauração seletiva na pasta de dados. Não toca no Office."""
    now = now or datetime.now().astimezone()
    candidates = find_candidates(cfg, archive, target, only)
    if not candidates:
        raise RestoreScriptError(
            "Nenhuma conversa com esse id no arquivo." if only else
            "Nada a restaurar: todas as conversas do arquivo estão na fonte de destino.")
    payload, included, skipped = build_payload(candidates, now)
    if not included:
        reasons = "; ".join(f"{c.id[:8]}: {reason}" for c, reason in skipped)
        raise RestoreScriptError(f"Nenhuma conversa pôde entrar no pacote ({reasons}).")

    folder = cfg.data_dir / "restore" / now.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=False)
    # ensure_ascii=True: tudo que não é ASCII vira \\uXXXX. O arquivo fica maior,
    # mas qualquer texto do JavaScript sobrevive intacto.
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    (folder / PAYLOAD_NAME).write_bytes(data)
    shutil.copyfile(_PACKAGE_DIR / "restore_script.js", folder / SCRIPT_NAME)
    shutil.copyfile(_PACKAGE_DIR / "restore_test_page.html", folder / TEST_PAGE_NAME)

    bundle = Bundle(folder, included, skipped, len(data),
                    payload["schema"].get("version"), archive.identities_in_source(target.source_id))
    (folder / STEPS_NAME).write_text(_steps_text(bundle, target), encoding="utf-8")
    log.info("pacote de restauracao: pasta=%s conversas=%d puladas=%d bytes=%d",
             folder.name, len(included), len(skipped), len(data))
    return bundle

"""Fase 3 - Arquivo permanente: um SQLite que só cresce.

Por que existe
--------------
O snapshot é uma FOTO: se o Office recriar o banco vazio, a próxima foto sai
vazia. O arquivo permanente é a MEMÓRIA: tudo o que já foi visto em qualquer
foto fica guardado, por id de conversa, e nada sai de lá.

As regras (é isto que os testes verificam)
------------------------------------------
A. **União.** Toda mensagem nova é acrescentada. Nenhuma mensagem arquivada é
   apagada: se ela some da versão mais nova, é só MARCADA como ausente.
B. **Versão suspeita não vira a versão atual.** Se a conversa chegar vazia, ou
   sem mais da metade das mensagens que o arquivo tem, o arquivo fica como
   está (salvo a regra A) e um alerta é gravado.
C. **Versão mais antiga não rebaixa a atual.** Se o ``updatedAt`` que chegou é
   anterior ao arquivado (ex.: leitura de um snapshot velho), só aproveitamos
   o que houver de inédito.
D. **Histórico.** Cada mudança aceita vira uma linha em
   ``conversation_versions``; o texto antigo de uma mensagem alterada vai para
   ``message_versions``.
E. **Trava no próprio banco.** Gatilhos (triggers) do SQLite recusam qualquer
   ``DELETE`` nas tabelas de conteúdo. Mesmo um bug neste programa não
   conseguiria apagar o histórico.

Como as tabelas se relacionam
-----------------------------
``conversations`` (1 linha por conversa, a "cabeça")
  └─ ``messages`` (1 linha por mensagem, chave = conversa + uuid)
       └─ ``message_versions`` (conteúdos anteriores da mesma mensagem)
  └─ ``conversation_versions`` (linha do tempo da conversa)
  └─ ``attachments`` -> ``blobs`` (imagens e anexos, guardados uma vez por hash)
``extra_records`` (skills, memória) · ``ingested_snapshots`` · ``alerts``

Busca (Fase 4)
--------------
``search_index`` é um índice de texto completo (SQLite **FTS5**). Um índice
desses guarda, para cada palavra, em quais mensagens ela aparece - por isso a
busca é instantânea mesmo com milhares de mensagens. Cada linha do índice usa
o mesmo ``rowid`` da mensagem em ``messages``; é assim que um resultado da
busca é ligado de volta à mensagem. O tokenizador ``unicode61
remove_diacritics`` ignora maiúsculas e acentos ("conciliacao" acha
"conciliação"). O índice é um dado DERIVADO: pode ser reconstruído a qualquer
momento a partir das mensagens (``reindex``).

O arquivo guarda o conteúdo FIEL (sem filtro de credenciais); o filtro é
aplicado na exportação. Por isso ele fica na pasta de dados protegida.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .conversation import Attachment, Conversation, Image, Message
from .reader import UNDEFINED, TypedArray

SCHEMA_VERSION = 2  # 1 = Fase 3; 2 = + índice de busca (Fase 4)

_TOKENIZER = "unicode61 remove_diacritics 2"
_FTS_SCHEMA = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(body, tokenize='{_TOKENIZER}')",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS title_index USING fts5(title, tokenize='{_TOKENIZER}')",
)
_SEARCH_TEXT_LIMIT = 200_000  # caracteres indexados por mensagem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT, surface TEXT, file_id TEXT, user_id TEXT, org_id TEXT, host_origin TEXT,
    created_at TEXT, updated_at TEXT, message_source TEXT,
    version INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    last_source_id TEXT, last_snapshot TEXT,
    missing_since TEXT,
    exported_state TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    uuid TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT, timestamp TEXT,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    first_version INTEGER NOT NULL,
    absent_since_version INTEGER,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, uuid)
);

CREATE TABLE IF NOT EXISTS message_versions (
    conversation_id TEXT NOT NULL, uuid TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL, seen_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, uuid, payload_sha256)
);

CREATE TABLE IF NOT EXISTS conversation_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    version INTEGER,
    status TEXT NOT NULL,              -- accepted | rejected | older
    content_sha256 TEXT NOT NULL,
    seen_at TEXT NOT NULL, source_id TEXT, snapshot TEXT,
    updated_at TEXT, title TEXT,
    message_count INTEGER NOT NULL,
    added INTEGER NOT NULL, changed INTEGER NOT NULL, removed INTEGER NOT NULL,
    UNIQUE (conversation_id, content_sha256, status)
);

CREATE TABLE IF NOT EXISTS blobs (
    sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, media_type TEXT, data BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    conversation_id TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, name, sha256)
);

CREATE TABLE IF NOT EXISTS extra_records (
    database TEXT NOT NULL, store TEXT NOT NULL, key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    PRIMARY KEY (database, store, key, payload_sha256)
);

CREATE TABLE IF NOT EXISTS ingested_snapshots (
    source_id TEXT NOT NULL, snapshot TEXT NOT NULL,
    taken_at TEXT, db_created_at TEXT, stable INTEGER,
    ingested_at TEXT NOT NULL, conversations INTEGER, error TEXT,
    PRIMARY KEY (source_id, snapshot)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT,
    message TEXT NOT NULL, details TEXT,
    resolved_at TEXT, acknowledged_at TEXT
);
"""

# Tabelas de conteúdo: nelas, DELETE é proibido pelo próprio banco.
_APPEND_ONLY_TABLES = ("conversations", "messages", "message_versions", "conversation_versions",
                       "blobs", "attachments", "extra_records", "alerts")


class ArchiveError(Exception):
    pass


# ------------------------------------------------------------- conversões
def _iso(value: datetime | None) -> str | None:
    """Data -> texto ISO em UTC. Formato fixo: comparar textos = comparar datas."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _canonical(value: Any) -> str:
    """JSON canônico (chaves ordenadas): o mesmo conteúdo gera sempre o mesmo texto."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_plain(value: Any, store_blob: Callable[[bytes, str | None], str]) -> Any:
    """Valor lido -> algo que cabe em JSON, sem perder dados.

    Binários vão para a tabela ``blobs`` e no lugar fica uma referência
    ``{"$blob": <hash>}``; datas viram ``{"$date": <iso>}``.
    """
    if value is None or value is UNDEFINED:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, datetime):
        return {"$date": _iso(value)}
    if isinstance(value, bytes):
        return {"$blob": store_blob(value, None), "size": len(value)}
    if isinstance(value, TypedArray):
        return {"$blob": store_blob(value.data, None), "size": len(value.data),
                "typed_array": value.kind}
    if isinstance(value, dict):
        return {str(k): to_plain(v, store_blob) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v, store_blob) for v in value]
    return str(value)


def message_to_payload(msg: Message, store_blob: Callable[[bytes, str | None], str]) -> dict:
    data = {
        "uuid": msg.uuid, "kind": msg.kind,
        "timestamp": _iso(msg.timestamp), "end_timestamp": _iso(msg.end_timestamp),
        "text": msg.text, "model": msg.model, "effort": msg.effort, "api_id": msg.api_id,
        "tool_name": msg.tool_name, "tool_status": msg.tool_status,
        "tool_input": to_plain(msg.tool_input, store_blob),
        "tool_result": to_plain(msg.tool_result, store_blob),
        "tool_error": msg.tool_error, "result_chars": msg.result_chars,
        "images": [{"sha256": store_blob(i.data, i.media_type), "media_type": i.media_type,
                    "size": len(i.data)} for i in msg.images],
        "files": to_plain(msg.files, store_blob),
        "extra": to_plain(msg.extra, store_blob),
    }
    return {k: v for k, v in data.items() if v is not None and v != [] and v != {}}


def message_from_payload(data: dict, load_blob: Callable[[str], bytes | None]) -> Message:
    images = []
    for ref in data.get("images") or []:
        blob = load_blob(ref["sha256"])
        if blob is not None:
            images.append(Image(ref.get("media_type") or "application/octet-stream",
                                blob, ref["sha256"]))
    return Message(
        kind=data.get("kind") or "desconhecido", uuid=data.get("uuid"),
        timestamp=_parse(data.get("timestamp")), end_timestamp=_parse(data.get("end_timestamp")),
        text=data.get("text"), model=data.get("model"), effort=data.get("effort"),
        api_id=data.get("api_id"), tool_name=data.get("tool_name"),
        tool_status=data.get("tool_status"), tool_input=data.get("tool_input"),
        tool_result=data.get("tool_result"), tool_error=data.get("tool_error"),
        result_chars=data.get("result_chars"), images=images, files=data.get("files"),
        extra=data.get("extra") or {},
    )


def _collect_strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        if "$blob" not in value:
            for item in value.values():
                _collect_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, out)


def message_search_text(payload: dict) -> str:
    """O texto de uma mensagem que entra no índice de busca.

    Usuário, Claude e raciocínio: o próprio texto. Ferramenta: nome, erro e
    todo texto que houver na entrada e no resultado (explicação, código,
    dados lidos da planilha).
    """
    parts: list[str] = []
    for key in ("text", "tool_name", "tool_error"):
        if isinstance(payload.get(key), str):
            parts.append(payload[key])
    _collect_strings(payload.get("tool_input"), parts)
    _collect_strings(payload.get("tool_result"), parts)
    return "\n".join(parts)[:_SEARCH_TEXT_LIMIT]


def build_match(text: str, *, phrase: bool = False) -> str:
    """Transforma o que o usuário digitou em uma consulta FTS5 segura.

    O FTS5 tem sintaxe própria (AND, OR, NEAR, aspas, ``coluna:``...). Para o
    usuário não tropeçar nela, cada palavra vai entre aspas (vira texto
    literal). Sem ``phrase``: todas as palavras, em qualquer ordem. Com
    ``phrase``: a frase exata. Um ``*`` no fim da palavra busca por prefixo.
    """
    words = text.split()
    if phrase:
        return '"' + " ".join(words).replace('"', '""') + '"' if words else ""
    parts = []
    for word in words:
        prefix = word.endswith("*")
        core = word.rstrip("*").replace('"', '""')
        if core:
            parts.append(f'"{core}"' + ("*" if prefix else ""))
    return " ".join(parts)


# ------------------------------------------------------------- resultados
@dataclass
class SearchHit:
    conversation_id: str
    title: str | None
    surface: str | None
    kind: str  # user | assistant | thinking | tool | title
    uuid: str | None
    timestamp: str | None
    snippet: str
    absent: bool = False  # a mensagem não está mais na versão atual


@dataclass
class ConversationIngest:
    conversation_id: str
    outcome: str  # new | updated | unchanged | older | rejected
    added: int = 0
    changed: int = 0
    removed: int = 0
    archived_messages: int = 0  # mensagens ativas no arquivo depois desta ingestão


@dataclass
class Alert:
    kind: str
    message: str
    source_id: str | None = None
    details: dict = field(default_factory=dict)
    created_at: str | None = None
    id: int | None = None


@dataclass
class _Prepared:
    uuid: str
    position: int
    kind: str
    timestamp: str | None
    payload: str
    sha: str
    search_text: str = ""


# ---------------------------------------------------------------- arquivo
class Archive:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: nós controlamos BEGIN/COMMIT (ver transaction()).
        self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self.fts = False
        self._create_schema()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _create_schema(self) -> None:
        with self.transaction():
            for statement in _SCHEMA.split(";\n"):
                if statement.strip():
                    self._db.execute(statement)
            for table in _APPEND_ONLY_TABLES:
                self._db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'arquivo permanente: DELETE não é permitido'); END")
            row = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            stored = int(row["value"]) if row else None
            if stored is not None and stored > SCHEMA_VERSION:
                raise ArchiveError(
                    f"O arquivo foi criado por uma versão mais nova da ferramenta "
                    f"(esquema {stored}). Atualize o office-chat-keeper.")
            try:
                for statement in _FTS_SCHEMA:
                    self._db.execute(statement)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False  # este SQLite não tem FTS5: tudo funciona, menos a busca
            # Migração (arquivo criado antes da busca existir) e autoconserto
            # (índice fora de sincronia): nos dois casos, reconstrói o índice.
            if self.fts and (stored != SCHEMA_VERSION or self._index_out_of_sync()):
                self._reindex()
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Tudo ou nada: se algo falhar no meio, o banco volta ao estado anterior."""
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    # ------------------------------------------------------------- blobs
    def store_blob(self, data: bytes, media_type: str | None = None) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self._db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?, ?, ?)",
                         (sha, len(data), media_type, data))
        return sha

    def load_blob(self, sha: str) -> bytes | None:
        row = self._db.execute("SELECT data FROM blobs WHERE sha256=?", (sha,)).fetchone()
        return bytes(row["data"]) if row else None

    # ---------------------------------------------------------- ingestão
    def _prepare(self, conv: Conversation) -> list[_Prepared]:
        prepared: list[_Prepared] = []
        seen: set[str] = set()
        for position, msg in enumerate(conv.messages):
            data = message_to_payload(msg, self.store_blob)
            payload = _canonical(data)
            # Sem uuid (formato reserva): a posição serve de identidade.
            uuid = msg.uuid or f"pos-{position:05d}"
            while uuid in seen:
                uuid += "#dup"
            seen.add(uuid)
            prepared.append(_Prepared(uuid, position, msg.kind, _iso(msg.timestamp),
                                      payload, _sha(payload), message_search_text(data)))
        return prepared

    def ingest_conversation(self, conv: Conversation, *, source_id: str, snapshot: str,
                            seen_at: datetime) -> ConversationIngest:
        """Junta uma conversa lida ao arquivo, aplicando as regras A a D."""
        with self.transaction():
            return self._ingest(conv, source_id, snapshot, _iso(seen_at))

    def _ingest(self, conv: Conversation, source_id: str, snapshot: str,
                seen_at: str) -> ConversationIngest:
        db = self._db
        incoming = self._prepare(conv)
        updated_at = _iso(conv.updated_at)
        content_sha = _sha(_canonical([conv.title, conv.surface, updated_at,
                                       [[m.uuid, m.sha] for m in incoming]]))
        for att in conv.attachments:
            sha = self.store_blob(att.data)
            db.execute("INSERT INTO attachments VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(conversation_id, name, sha256) "
                       "DO UPDATE SET last_seen_at=excluded.last_seen_at",
                       (conv.id, att.name, sha, seen_at, seen_at))

        head = db.execute("SELECT rowid, * FROM conversations WHERE id=?", (conv.id,)).fetchone()

        def insert_message(m: _Prepared, version: int, absent: int | None, position: int) -> None:
            cursor = db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (conv.id, m.uuid, position, m.kind, m.timestamp, m.payload, m.sha,
                                 version, absent, seen_at, seen_at))
            self._index_message(cursor.lastrowid, m.search_text)

        def record_version(version: int | None, status: str, added: int, changed: int,
                           removed: int) -> None:
            db.execute(
                "INSERT OR IGNORE INTO conversation_versions (conversation_id, version, status, "
                "content_sha256, seen_at, source_id, snapshot, updated_at, title, message_count, "
                "added, changed, removed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conv.id, version, status, content_sha, seen_at, source_id, snapshot,
                 updated_at, conv.title, len(incoming), added, changed, removed))

        # ---- conversa inédita
        if head is None:
            cursor = db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conv.id, conv.title, conv.surface, conv.file_id, conv.user_id, conv.org_id,
                 conv.host_origin, _iso(conv.created_at), updated_at, conv.message_source,
                 1, content_sha, seen_at, seen_at, source_id, snapshot, None, None))
            self._index_title(cursor.lastrowid, conv.title)
            for m in incoming:
                insert_message(m, 1, None, m.position)
            record_version(1, "accepted", len(incoming), 0, 0)
            return ConversationIngest(conv.id, "new", added=len(incoming),
                                      archived_messages=len(incoming))

        # ---- exatamente a mesma versão já arquivada
        if head["content_sha256"] == content_sha:
            db.execute("UPDATE conversations SET last_seen_at=?, last_source_id=?, last_snapshot=?, "
                       "missing_since=NULL WHERE id=?", (seen_at, source_id, snapshot, conv.id))
            return ConversationIngest(conv.id, "unchanged",
                                      archived_messages=self._active_count(conv.id))

        known = {r["uuid"]: r for r in db.execute(
            "SELECT rowid, uuid, payload, payload_sha256, absent_since_version FROM messages "
            "WHERE conversation_id=?", (conv.id,))}
        active = {u for u, r in known.items() if r["absent_since_version"] is None}
        incoming_ids = {m.uuid for m in incoming}
        added = [m for m in incoming if m.uuid not in known]
        changed = [m for m in incoming
                   if m.uuid in known and known[m.uuid]["payload_sha256"] != m.sha]
        missing = active - incoming_ids
        version = head["version"]
        next_position = (db.execute("SELECT MAX(position) FROM messages WHERE conversation_id=?",
                                    (conv.id,)).fetchone()[0] or 0) + 1

        def keep_variants() -> None:
            # Conteúdo diferente para a mesma mensagem: guardado como variante.
            for m in changed:
                db.execute("INSERT OR IGNORE INTO message_versions VALUES (?, ?, ?, ?, ?)",
                           (conv.id, m.uuid, m.sha, m.payload, seen_at))

        # ---- regra C: versão mais antiga que a arquivada
        if updated_at and head["updated_at"] and updated_at < head["updated_at"]:
            for offset, m in enumerate(added):
                insert_message(m, version, version, next_position + offset)
            keep_variants()
            record_version(None, "older", len(added), len(changed), 0)
            return ConversationIngest(conv.id, "older" if added or changed else "unchanged",
                                      added=len(added), changed=len(changed),
                                      archived_messages=len(active))

        # ---- regra B: versão vazia ou muito menor -> não vira a atual
        if active and (not incoming or len(missing) * 2 > len(active)):
            for offset, m in enumerate(added):  # regra A continua valendo
                insert_message(m, version, None, next_position + offset)
            keep_variants()
            record_version(None, "rejected", len(added), len(changed), len(missing))
            return ConversationIngest(conv.id, "rejected", added=len(added), changed=len(changed),
                                      removed=len(missing),
                                      archived_messages=len(active) + len(added))

        # ---- versão nova aceita
        version += 1
        for m in added:
            insert_message(m, version, None, m.position)
        for m in changed:
            old = known[m.uuid]
            db.execute("INSERT OR IGNORE INTO message_versions VALUES (?, ?, ?, ?, ?)",
                       (conv.id, m.uuid, old["payload_sha256"], old["payload"], seen_at))
            db.execute("UPDATE messages SET payload=?, payload_sha256=? "
                       "WHERE conversation_id=? AND uuid=?", (m.payload, m.sha, conv.id, m.uuid))
            self._index_message(old["rowid"], m.search_text)
        db.executemany(
            "UPDATE messages SET position=?, kind=?, timestamp=?, last_seen_at=?, "
            "absent_since_version=NULL WHERE conversation_id=? AND uuid=?",
            [(m.position, m.kind, m.timestamp, seen_at, conv.id, m.uuid) for m in incoming])
        db.executemany(
            "UPDATE messages SET absent_since_version=? WHERE conversation_id=? AND uuid=?",
            [(version, conv.id, uuid) for uuid in missing])
        db.execute(
            "UPDATE conversations SET title=?, surface=?, file_id=?, user_id=?, org_id=?, "
            "host_origin=?, created_at=COALESCE(?, created_at), updated_at=?, message_source=?, "
            "version=?, content_sha256=?, last_seen_at=?, last_source_id=?, last_snapshot=?, "
            "missing_since=NULL WHERE id=?",
            (conv.title, conv.surface, conv.file_id, conv.user_id, conv.org_id, conv.host_origin,
             _iso(conv.created_at), updated_at, conv.message_source, version, content_sha,
             seen_at, source_id, snapshot, conv.id))
        self._index_title(head["rowid"], conv.title)
        record_version(version, "accepted", len(added), len(changed), len(missing))
        return ConversationIngest(conv.id, "updated", added=len(added), changed=len(changed),
                                  removed=len(missing), archived_messages=len(incoming))

    # ------------------------------------------------------------ busca
    def _index_message(self, rowid: int, text: str) -> None:
        if self.fts:
            self._db.execute("DELETE FROM search_index WHERE rowid=?", (rowid,))
            self._db.execute("INSERT INTO search_index(rowid, body) VALUES (?, ?)", (rowid, text))

    def _index_title(self, rowid: int, title: str | None) -> None:
        if self.fts:
            self._db.execute("DELETE FROM title_index WHERE rowid=?", (rowid,))
            self._db.execute("INSERT INTO title_index(rowid, title) VALUES (?, ?)",
                             (rowid, title or ""))

    def _index_out_of_sync(self) -> bool:
        count = lambda table: self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: E731
        return (count("search_index") != count("messages")
                or count("title_index") != count("conversations"))

    def _reindex(self) -> None:
        """Reconstrói o índice de busca inteiro a partir das mensagens."""
        db = self._db
        db.execute("DELETE FROM search_index")
        db.execute("DELETE FROM title_index")
        last = 0
        while True:  # em lotes, para não carregar o arquivo todo na memória
            rows = db.execute("SELECT rowid, payload FROM messages WHERE rowid > ? "
                              "ORDER BY rowid LIMIT 500", (last,)).fetchall()
            if not rows:
                break
            db.executemany("INSERT INTO search_index(rowid, body) VALUES (?, ?)",
                           [(r["rowid"], message_search_text(json.loads(r["payload"])))
                            for r in rows])
            last = rows[-1]["rowid"]
        db.execute("INSERT INTO title_index(rowid, title) "
                   "SELECT rowid, COALESCE(title, '') FROM conversations")

    def reindex(self) -> None:
        if not self.fts:
            raise ArchiveError("Este Python veio com um SQLite sem FTS5; a busca não está disponível.")
        with self.transaction():
            self._reindex()

    def search(self, text: str, *, phrase: bool = False, kinds: list[str] | None = None,
               conversation: str | None = None, limit: int = 20) -> tuple[list[SearchHit], int]:
        """Busca por termo. Devolve (resultados, total de mensagens encontradas).

        Ordem: primeiro as mensagens do usuário e do Claude, depois os
        raciocínios, por último as chamadas de ferramenta (que são muitas e
        costumam repetir os termos). Dentro de cada grupo, por relevância
        (``bm25``: quanto mais raro o termo no arquivo e mais presente na
        mensagem, melhor a posição).
        """
        if not self.fts:
            raise ArchiveError("Este Python veio com um SQLite sem FTS5; a busca não está disponível.")
        match = build_match(text, phrase=phrase)
        if not match:
            return [], 0
        where = ["search_index MATCH ?"]
        params: list[Any] = [match]
        if kinds:
            where.append(f"m.kind IN ({', '.join('?' * len(kinds))})")
            params += kinds
        if conversation:
            where.append("m.conversation_id LIKE ?")
            params.append(conversation + "%")
        clause = " AND ".join(where)
        base = ("FROM search_index JOIN messages m ON m.rowid = search_index.rowid "
                "JOIN conversations c ON c.id = m.conversation_id WHERE " + clause)
        try:
            hits: list[SearchHit] = []
            # Conversas cujo TÍTULO contém os termos vêm primeiro.
            if not kinds or "title" in kinds:
                title_sql = ("SELECT c.id, c.title, c.surface, c.updated_at, "
                             "snippet(title_index, 0, '[', ']', '…', 16) AS snip "
                             "FROM title_index JOIN conversations c ON c.rowid = title_index.rowid "
                             "WHERE title_index MATCH ?")
                title_params: list[Any] = [match]
                if conversation:
                    title_sql += " AND c.id LIKE ?"
                    title_params.append(conversation + "%")
                for r in self._db.execute(title_sql + " ORDER BY c.updated_at DESC LIMIT ?",
                                          (*title_params, limit)):
                    hits.append(SearchHit(r["id"], r["title"], r["surface"], "title", None,
                                          r["updated_at"], r["snip"]))
            total = self._db.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
            rows = self._db.execute(
                "SELECT m.conversation_id, m.uuid, m.kind, m.timestamp, "
                "m.absent_since_version IS NOT NULL AS absent, c.title, c.surface, "
                "snippet(search_index, 0, '[', ']', '…', 16) AS snip "
                + base + " ORDER BY CASE m.kind WHEN 'user' THEN 0 WHEN 'assistant' THEN 0 "
                "WHEN 'thinking' THEN 1 ELSE 2 END, bm25(search_index), m.timestamp LIMIT ?",
                (*params, max(0, limit - len(hits)))).fetchall()
        except sqlite3.OperationalError as exc:
            raise ArchiveError(f"Não consegui interpretar a busca ({exc}).") from exc
        for r in rows:
            hits.append(SearchHit(r["conversation_id"], r["title"], r["surface"], r["kind"],
                                  r["uuid"], r["timestamp"], r["snip"], bool(r["absent"])))
        return hits, total + sum(1 for h in hits if h.kind == "title")

    # ---------------------------------------------------- alertas (gestão)
    def all_alerts(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM alerts ORDER BY id").fetchall()

    def acknowledge_alerts(self, ids: list[int] | None, when: datetime) -> int:
        """Marca alertas abertos como vistos. ``ids=None`` = todos os abertos.

        O alerta não é apagado: continua no histórico, só deixa de ser cobrado.
        """
        sql = ("UPDATE alerts SET acknowledged_at=? WHERE acknowledged_at IS NULL "
               "AND resolved_at IS NULL")
        params: list[Any] = [_iso(when)]
        if ids is not None:
            if not ids:
                return 0
            sql += f" AND id IN ({', '.join('?' * len(ids))})"
            params += ids
        with self.transaction():
            return self._db.execute(sql, params).rowcount

    def _active_count(self, conv_id: str) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND absent_since_version IS NULL",
            (conv_id,)).fetchone()[0]

    # ----------------------------------------------- skills, memória etc.
    def store_extra_record(self, database: str, store: str, key: Any, value: Any,
                           seen_at: datetime) -> None:
        """Guarda um registro de banco auxiliar. Cada conteúdo diferente é uma versão."""
        payload = _canonical(to_plain(value, self.store_blob))
        key_text = key if isinstance(key, str) else _canonical(to_plain(key, self.store_blob))
        self._db.execute(
            "INSERT INTO extra_records VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(database, store, key, payload_sha256) "
            "DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (database, store, key_text, _sha(payload), payload, _iso(seen_at), _iso(seen_at)))

    def latest_extra_records(self) -> list[sqlite3.Row]:
        """A versão vista por último de cada registro auxiliar."""
        return self._db.execute(
            "SELECT e.* FROM extra_records e WHERE e.rowid = (SELECT x.rowid FROM extra_records x "
            "WHERE x.database=e.database AND x.store=e.store AND x.key=e.key "
            "ORDER BY x.last_seen_at DESC, x.rowid DESC LIMIT 1) "
            "ORDER BY e.database, e.store, e.key").fetchall()

    # ------------------------------------------------------- snapshots
    def is_ingested(self, source_id: str, snapshot: str) -> bool:
        return self._db.execute("SELECT 1 FROM ingested_snapshots WHERE source_id=? AND snapshot=?",
                                (source_id, snapshot)).fetchone() is not None

    def mark_ingested(self, source_id: str, snapshot: str, *, taken_at: str | None,
                      db_created_at: str | None, stable: bool, conversations: int | None,
                      error: str | None = None) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO ingested_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, snapshot, taken_at, db_created_at, int(stable),
             _iso(datetime.now(timezone.utc)), conversations, error))

    def last_ingested(self, source_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM ingested_snapshots WHERE source_id=? AND error IS NULL "
            "ORDER BY snapshot DESC LIMIT 1", (source_id,)).fetchone()

    # ---------------------------------------------------------- alertas
    def add_alert(self, alert: Alert, dedup_key: str, created_at: datetime) -> Alert | None:
        """Grava o alerta e o devolve, a menos que o mesmo alerta já esteja aberto.

        ``dedup_key`` identifica "o mesmo problema": sem isso, uma execução a
        cada 15 minutos repetiria o mesmo aviso sem parar. Se o problema tinha
        sido resolvido e voltou, o alerta é reaberto.
        """
        existing = self._db.execute("SELECT id, resolved_at FROM alerts WHERE dedup_key=?",
                                    (dedup_key,)).fetchone()
        if existing is not None and existing["resolved_at"] is None:
            return None
        values = (_iso(created_at), alert.kind, alert.source_id, alert.message,
                  _canonical(alert.details))
        if existing is not None:
            self._db.execute("UPDATE alerts SET created_at=?, kind=?, source_id=?, message=?, "
                             "details=?, resolved_at=NULL, acknowledged_at=NULL WHERE id=?",
                             (*values, existing["id"]))
            alert.id = existing["id"]
        else:
            alert.id = self._db.execute(
                "INSERT INTO alerts (created_at, kind, source_id, message, details, dedup_key) "
                "VALUES (?, ?, ?, ?, ?, ?)", (*values, dedup_key)).lastrowid
        alert.created_at = _iso(created_at)
        return alert

    def open_alerts(self, *, include_acknowledged: bool = False) -> list[Alert]:
        """Alertas não resolvidos. Por padrão, só os que você ainda não marcou como vistos."""
        sql = "SELECT * FROM alerts WHERE resolved_at IS NULL"
        if not include_acknowledged:
            sql += " AND acknowledged_at IS NULL"
        rows = self._db.execute(sql + " ORDER BY id").fetchall()
        return [Alert(r["kind"], r["message"], r["source_id"], json.loads(r["details"] or "{}"),
                      r["created_at"], r["id"]) for r in rows]

    def resolve_alerts(self, kind: str, when: datetime, *, dedup_key: str | None = None) -> int:
        sql = "UPDATE alerts SET resolved_at=? WHERE kind=? AND resolved_at IS NULL"
        params: list[Any] = [_iso(when), kind]
        if dedup_key is not None:
            sql += " AND dedup_key=?"
            params.append(dedup_key)
        return self._db.execute(sql, params).rowcount

    def check_source(self, *, source_id: str, snapshot: str, db_created_at: str | None,
                     present_ids: set[str], ids_before: set[str], seen_at: datetime,
                     check_absences: bool = True, expect_recreated: bool = False) -> list[Alert]:
        """Compara o que a fonte tem agora com o arquivo e gera alertas de perda.

        ``ids_before`` = conversas que o arquivo já tinha ANTES desta ingestão.
        Nada aqui apaga coisa alguma: só marca e avisa.
        """
        db = self._db
        created: list[Alert] = []
        previous = self.last_ingested(source_id)

        def emit(alert: Alert, key: str) -> None:
            new = self.add_alert(alert, key, seen_at)
            if new:
                created.append(new)

        # 1) O banco desta fonte foi recriado (data de criação do LOCK mudou).
        # (Se a troca foi feita pela nossa própria restauração, é esperada: sem alerta.)
        if previous and previous["db_created_at"] and db_created_at \
                and previous["db_created_at"] != db_created_at and not expect_recreated:
            emit(Alert("database_recreated",
                       f"Possível perda de histórico: o banco da fonte {source_id} foi recriado "
                       f"(criado em {db_created_at}; antes, {previous['db_created_at']}).",
                       source_id, {"before": previous["db_created_at"], "now": db_created_at}),
                 f"recreated:{source_id}:{db_created_at}")

        if not check_absences:
            return created

        # 2) Fonte nunca vista, sem a maior parte do que o arquivo já tem.
        absent_from_source = ids_before - present_ids
        if previous is None and ids_before and len(absent_from_source) * 2 > len(ids_before):
            emit(Alert("source_smaller_than_archive",
                       f"Possível perda de histórico: a fonte nova {source_id} tem "
                       f"{len(present_ids)} conversa(s); {len(absent_from_source)} de "
                       f"{len(ids_before)} conversa(s) do arquivo não estão nela. "
                       f"Nada foi apagado do arquivo.",
                       source_id, {"source_conversations": len(present_ids),
                                   "archived_conversations": len(ids_before),
                                   "absent_ids": sorted(absent_from_source)}),
                 f"smaller-source:{source_id}")

        # 3) Conversas que estavam NESTA fonte e não estão mais.
        rows = db.execute("SELECT id, title, missing_since FROM conversations "
                          "WHERE last_source_id=?", (source_id,)).fetchall()
        gone = [r for r in rows if r["id"] not in present_ids]
        newly_gone = [r for r in gone if r["missing_since"] is None]
        if newly_gone:
            db.executemany("UPDATE conversations SET missing_since=? WHERE id=?",
                           [(_iso(seen_at), r["id"]) for r in newly_gone])
            ids = sorted(r["id"] for r in newly_gone)
            emit(Alert("conversations_missing",
                       f"Possível perda de histórico: {len(ids)} conversa(s) do arquivo não estão "
                       f"mais na fonte {source_id}. Elas continuam guardadas no arquivo.",
                       source_id, {"conversations": [{"id": r["id"], "title": r["title"]}
                                                     for r in newly_gone]}),
                 f"missing:{source_id}:{_sha(_canonical(ids))[:16]}")

        # 4) Alertas de ausência cujas conversas reapareceram: resolvidos.
        for row in db.execute("SELECT id, details FROM alerts WHERE kind='conversations_missing' "
                              "AND resolved_at IS NULL").fetchall():
            ids = [c["id"] for c in json.loads(row["details"] or "{}").get("conversations", [])]
            if ids and all(db.execute("SELECT missing_since FROM conversations WHERE id=?",
                                      (i,)).fetchone()["missing_since"] is None for i in ids):
                db.execute("UPDATE alerts SET resolved_at=? WHERE id=?", (_iso(seen_at), row["id"]))
        return created

    # ------------------------------------------------- verificação (Fase 6)
    def ingested_snapshots(self) -> dict[tuple[str, str], sqlite3.Row]:
        """(fonte, snapshot) -> linha de ``ingested_snapshots``."""
        return {(r["source_id"], r["snapshot"]): r
                for r in self._db.execute("SELECT * FROM ingested_snapshots")}

    def ingested_count(self, source_id: str) -> int:
        return self._db.execute("SELECT COUNT(*) FROM ingested_snapshots WHERE source_id=?",
                                (source_id,)).fetchone()[0]

    def conversations_outside(self, current_source_ids: set[str]) -> list[sqlite3.Row]:
        """Conversas do arquivo que não estão em nenhuma das fontes atuais.

        Uma conversa "está" na fonte em que foi vista por último, a menos que
        já tenha sido marcada como ausente dela.
        """
        return [r for r in self.summaries()
                if r["missing_since"] or r["last_source_id"] not in current_source_ids]

    def identities_in_source(self, source_id: str) -> list[tuple[str | None, str | None]]:
        """Pares (usuário, organização) das conversas presentes em uma fonte."""
        rows = self._db.execute(
            "SELECT DISTINCT user_id, org_id FROM conversations "
            "WHERE last_source_id=? AND missing_since IS NULL", (source_id,)).fetchall()
        return [(r["user_id"], r["org_id"]) for r in rows]

    def pinned_snapshots(self) -> dict[str, set[str]]:
        """Snapshots que a retenção não pode apagar: fonte -> nomes.

        O snapshot em que uma conversa foi vista por último é a cópia BRUTA
        mais recente dela, a única de onde uma restauração fiel pode partir.
        Para conversas ainda presentes esse snapshot é sempre o mais novo; só
        as que sumiram "seguram" um snapshot antigo.
        """
        pinned: dict[str, set[str]] = {}
        for r in self._db.execute("SELECT DISTINCT last_source_id, last_snapshot FROM conversations "
                                  "WHERE last_source_id IS NOT NULL AND last_snapshot IS NOT NULL"):
            pinned.setdefault(r["last_source_id"], set()).add(r["last_snapshot"])
        return pinned

    # ----------------------------------------------------------- leitura
    def conversation_ids(self) -> set[str]:
        return {r["id"] for r in self._db.execute("SELECT id FROM conversations")}

    def summaries(self) -> list[sqlite3.Row]:
        """Uma linha por conversa, com contagens (para status/list)."""
        return self._db.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id "
            "   AND m.absent_since_version IS NULL) AS active_messages, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id "
            "   AND m.absent_since_version IS NOT NULL) AS absent_messages "
            "FROM conversations c ORDER BY c.updated_at DESC, c.id").fetchall()

    def stats(self) -> dict:
        one = lambda sql: self._db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "conversations": one("SELECT COUNT(*) FROM conversations"),
            "messages": one("SELECT COUNT(*) FROM messages"),
            "versions": one("SELECT COUNT(*) FROM conversation_versions WHERE status='accepted'"),
            "blobs": one("SELECT COUNT(*) FROM blobs"),
            "blob_bytes": one("SELECT COALESCE(SUM(size), 0) FROM blobs"),
            "last_ingest": one("SELECT MAX(ingested_at) FROM ingested_snapshots"),
            "missing": one("SELECT COUNT(*) FROM conversations WHERE missing_since IS NOT NULL"),
        }

    def versions(self, conv_id: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM conversation_versions WHERE conversation_id=? "
                                "ORDER BY id", (conv_id,)).fetchall()

    def load_conversation(self, conv_id: str) -> Conversation | None:
        """Reconstrói a conversa a partir do arquivo (para exportar)."""
        head = self._db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if head is None:
            return None
        conv = Conversation(
            id=head["id"], title=head["title"], surface=head["surface"], file_id=head["file_id"],
            user_id=head["user_id"], org_id=head["org_id"], host_origin=head["host_origin"],
            created_at=_parse(head["created_at"]), updated_at=_parse(head["updated_at"]),
            message_source=head["message_source"] or "none",
            missing_since=_parse(head["missing_since"]),
        )
        rows = self._db.execute(
            "SELECT payload, absent_since_version FROM messages WHERE conversation_id=? "
            "ORDER BY absent_since_version IS NOT NULL, position, timestamp, uuid",
            (conv_id,)).fetchall()
        for row in rows:
            msg = message_from_payload(json.loads(row["payload"]), self.load_blob)
            (conv.messages if row["absent_since_version"] is None
             else conv.absent_messages).append(msg)
        # Anexos: a versão vista por último de cada nome.
        latest: dict[str, str] = {}
        for row in self._db.execute(
                "SELECT name, sha256 FROM attachments WHERE conversation_id=? "
                "ORDER BY last_seen_at, rowid", (conv_id,)):
            latest[row["name"]] = row["sha256"]
        for name in sorted(latest):
            data = self.load_blob(latest[name])
            if data is not None:
                conv.attachments.append(Attachment(name, data))
        return conv

    # -------------------------------------------------------- exportação
    def export_state(self, conv_id: str) -> str:
        """Resumo do que influencia a exportação; se mudar, é hora de reexportar."""
        head = self._db.execute("SELECT content_sha256, version, missing_since FROM conversations "
                                "WHERE id=?", (conv_id,)).fetchone()
        counts = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(absent_since_version IS NOT NULL), 0) FROM messages "
            "WHERE conversation_id=?", (conv_id,)).fetchone()
        atts = self._db.execute("SELECT COUNT(*) FROM attachments WHERE conversation_id=?",
                                (conv_id,)).fetchone()[0]
        return _sha(_canonical([head["content_sha256"], head["version"], head["missing_since"],
                                counts[0], counts[1], atts]))

    def pending_exports(self, force: bool = False) -> list[str]:
        ids = []
        for row in self._db.execute("SELECT id, exported_state FROM conversations ORDER BY id"):
            if force or row["exported_state"] != self.export_state(row["id"]):
                ids.append(row["id"])
        return ids

    def mark_exported(self, conv_id: str) -> None:
        self._db.execute("UPDATE conversations SET exported_state=? WHERE id=?",
                         (self.export_state(conv_id), conv_id))

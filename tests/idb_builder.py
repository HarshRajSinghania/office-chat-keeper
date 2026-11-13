"""Gerador de um IndexedDB SINTÉTICO, só para testes.

Para testar o leitor de ponta a ponta sem nenhum dado real, este módulo
escreve, por código, um banco no mesmo formato de disco que o Chromium usa:

* um arquivo ``000003.log`` do LevelDB (o "diário" de gravações);
* chaves no esquema do IndexedDB (prefixo banco/store + chave do usuário);
* valores serializados no formato V8 Structured Clone;
* opcionalmente, um valor "externo" gravado na pasta ``.blob``.

ATENÇÃO: isto é um escritor de brinquedo, usado apenas em pastas temporárias
de teste. A ferramenta em si NUNCA escreve em um LevelDB.
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

from office_chat_keeper.discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME
from office_chat_keeper.reader import UNDEFINED

ORIGIN = "https_pivot.claude.ai_0@1"
_BLOCK = 32768  # o diário do LevelDB é dividido em blocos de 32 KB


def varint(n: int) -> bytes:
    """Inteiro de tamanho variável: 7 bits por byte, bit alto = "continua"."""
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _utf16be(text: str) -> bytes:
    return varint(len(text)) + text.encode("utf-16-be")


# ------------------------------------------------------ V8 Structured Clone
def v8_value(value) -> bytes:
    """Serializa um valor Python no formato do V8 (subconjunto suficiente)."""
    if value is UNDEFINED:
        return b"_"
    if value is None:
        return b"0"
    if value is True:
        return b"T"
    if value is False:
        return b"F"
    if isinstance(value, int):
        zigzag = (value << 1) ^ (value >> 63)
        return b"I" + varint(zigzag)
    if isinstance(value, float):
        return b"N" + struct.pack("<d", value)
    if isinstance(value, str):
        try:
            raw = value.encode("latin-1")  # texto "de um byte" (inclui acentos)
            return b'"' + varint(len(raw)) + raw
        except UnicodeEncodeError:
            raw = value.encode("utf-16-le")  # texto "de dois bytes" (emoji, …)
            return b"c" + varint(len(raw)) + raw
    if isinstance(value, datetime):
        return b"D" + struct.pack("<d", value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, bytes):
        return b"B" + varint(len(value)) + value  # ArrayBuffer
    if isinstance(value, (list, tuple)):
        body = b"".join(v8_value(v) for v in value)
        return b"A" + varint(len(value)) + body + b"$" + varint(0) + varint(len(value))
    if isinstance(value, dict):
        body = b"".join(v8_value(str(k)) + v8_value(v) for k, v in value.items())
        return b"o" + body + b"{" + varint(len(value))
    raise TypeError(f"tipo não suportado no gerador de teste: {type(value)}")


def v8_serialize(value) -> bytes:
    return b"\xff\x0f" + v8_value(value)  # cabeçalho: marca de versão + versão 15


# ----------------------------------------------------------- chaves do IDB
def idb_key(key) -> bytes:
    if isinstance(key, str):
        return b"\x01" + _utf16be(key)
    if isinstance(key, (list, tuple)):
        return b"\x04" + varint(len(key)) + b"".join(idb_key(k) for k in key)
    raise TypeError("chave de teste deve ser texto ou lista de textos")


def _prefix(db_id: int, store_id: int, index_id: int) -> bytes:
    return bytes([0, db_id, store_id, index_id])


def key_path_bytes(path) -> bytes:
    """keyPath no formato do IndexedDB: 0x00 0x00 + tipo + texto(s)."""
    if path is None:
        return b"\x00\x00\x00"
    if isinstance(path, str):
        return b"\x00\x00\x01" + _utf16be(path)
    return b"\x00\x00\x02" + varint(len(path)) + b"".join(_utf16be(p) for p in path)


# O esquema real do banco de conversas do suplemento (confirmado em nov/2026).
CHAT_HISTORY_SCHEMA = {
    "chats": {"key_path": "id", "indexes": {
        "userId": "userId", "[userId+fileId]": ["userId", "fileId"],
        "[userId+orgId+surface]": ["userId", "orgId", "surface"], "updatedAt": "updatedAt"}},
    "blobs": {"key_path": ["chatId", "name"], "indexes": {"chatId": "chatId"}},
}


class FakeIndexedDB:
    """Monta um armazenamento falso no layout real do WebView2."""

    def __init__(self, idb_dir: Path):
        self.leveldb_dir = Path(idb_dir) / LEVELDB_DIR_NAME
        self.blob_dir = Path(idb_dir) / BLOB_DIR_NAME
        self._entries: list[tuple[int, bytes, bytes]] = []  # (estado, chave, valor)
        self._databases: dict[str, tuple[int, dict[str, int]]] = {}
        self._next_blob = 1

    def add_database(self, name: str, stores: list[str] | dict[str, dict],
                     version: int | None = None) -> "FakeIndexedDB":
        """Cria um banco. ``stores`` pode ser só a lista de nomes ou um dicionário
        ``{nome: {"key_path": ..., "indexes": {nome_do_indice: key_path}}}``."""
        db_id = len(self._databases) + 1
        base = _prefix(db_id, 0, 0)
        # Metadado global: (origem, nome do banco) -> id do banco.
        self._put(b"\x00\x00\x00\x00\xc9" + _utf16be(ORIGIN) + _utf16be(name), bytes([db_id]))
        # Metadados do banco: maior id de object store e versão.
        self._put(base + b"\x03", bytes([len(stores)]))
        if version is not None:
            self._put(base + b"\x04", varint(version))
        specs = stores if isinstance(stores, dict) else {store: {} for store in stores}
        store_ids = {}
        for store_id, (store, spec) in enumerate(specs.items(), start=1):
            meta = base + bytes([50]) + varint(store_id)
            self._put(meta + b"\x00", store.encode("utf-16-be"))  # nome
            if "key_path" in spec:
                self._put(meta + b"\x01", key_path_bytes(spec["key_path"]))
                self._put(meta + b"\x02", b"\x00")  # autoIncrement = não
            for index_id, (index, path) in enumerate(spec.get("indexes", {}).items(), start=31):
                imeta = base + bytes([100]) + varint(store_id) + varint(index_id)
                self._put(imeta + b"\x00", index.encode("utf-16-be"))
                self._put(imeta + b"\x01", b"\x00")  # único = não
                self._put(imeta + b"\x02", key_path_bytes(path))
                self._put(imeta + b"\x03", b"\x00")  # multiEntry = não
            store_ids[store] = store_id
        self._databases[name] = (db_id, store_ids)
        return self

    def _put(self, key: bytes, value: bytes) -> None:
        self._entries.append((1, key, value))

    def _data_key(self, database: str, store: str, key) -> tuple[int, int, bytes]:
        db_id, stores = self._databases[database]
        return db_id, stores[store], idb_key(key)

    def put(self, database: str, store: str, key, value, *, external: bool = False) -> "FakeIndexedDB":
        """Grava um registro. Regravar a mesma chave cria uma versão mais nova."""
        db_id, store_id, raw_key = self._data_key(database, store, key)
        # Envelope do Blink: 0xFF + versão 20 (sem "trailer"), depois o objeto V8.
        wrapped = b"\xff\x14" + v8_serialize(value)
        if external:
            # Valor grande: vai para um arquivo na pasta .blob e o registro
            # guarda só "substituir pelo blob de índice 0" (0xFF 0x11 0x01).
            number = self._next_blob
            self._next_blob += 1
            path = self.blob_dir / f"{db_id:x}" / f"{number >> 8:02x}" / f"{number:x}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wrapped)
            info = b"\x00" + varint(number) + _utf16be("application/vnd.blink-idb-value-wrapper") \
                + varint(len(wrapped))
            self._put(_prefix(db_id, store_id, 3) + raw_key, info)
            wrapped = b"\xff\x11\x01" + varint(len(wrapped)) + varint(0)
        self._put(_prefix(db_id, store_id, 1) + raw_key, varint(0) + wrapped)
        return self

    def delete(self, database: str, store: str, key) -> "FakeIndexedDB":
        db_id, store_id, raw_key = self._data_key(database, store, key)
        self._entries.append((0, _prefix(db_id, store_id, 1) + raw_key, b""))
        return self

    def write(self) -> Path:
        """Grava os arquivos e devolve a pasta .leveldb."""
        self.leveldb_dir.mkdir(parents=True, exist_ok=True)
        out = bytearray()
        for seq, (state, key, value) in enumerate(self._entries, start=1):
            # Lote de gravação: nº de sequência, contagem, depois as entradas.
            batch = struct.pack("<QI", seq, 1) + bytes([state]) + varint(len(key)) + key
            if state == 1:
                batch += varint(len(value)) + value
            self._append_record(out, batch)
        (self.leveldb_dir / "000003.log").write_bytes(bytes(out))
        (self.leveldb_dir / "CURRENT").write_text("MANIFEST-000001\n")
        (self.leveldb_dir / "LOCK").write_bytes(b"")
        (self.leveldb_dir / "LOG").write_text("teste\n")
        return self.leveldb_dir

    @staticmethod
    def _append_record(out: bytearray, payload: bytes) -> None:
        """Formato do diário: cabeçalho de 7 bytes (crc, tamanho, tipo) + dados.

        Um registro que não cabe no bloco é quebrado em fragmentos:
        tipo 1 = inteiro, 2 = primeiro, 3 = meio, 4 = último.
        """
        position, first = 0, True
        while True:
            left = _BLOCK - (len(out) % _BLOCK)
            if left < 7:
                out += b"\x00" * left
                left = _BLOCK
            fragment = payload[position:position + left - 7]
            position += len(fragment)
            last = position >= len(payload)
            kind = 1 if first and last else 2 if first else 4 if last else 3
            out += struct.pack("<IHB", 0, len(fragment), kind) + fragment
            first = False
            if last:
                return


# ------------------------------------------------------------ dados de teste
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def sample_chat(chat_id: str = "11111111-2222-3333-4444-555555555555", *,
                title: str = "Conciliação de teste", extra_messages: int = 0) -> dict:
    """Uma conversa inventada, no mesmo formato do suplemento (versão 1)."""
    import base64

    t0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    def at(minutes: int) -> datetime:
        return t0.replace(minute=minutes)

    image = base64.b64encode(PNG_1X1).decode("ascii")
    messages = [
        {"uuid": "u-1", "timestamp": at(0), "type": "user",
         "content": "Faça a conciliação da aba Março.", "files": UNDEFINED,
         "conductorFrom": UNDEFINED, "conductorFileName": UNDEFINED},
        {"uuid": "t-1", "timestamp": at(1), "type": "thinking",
         "content": "Preciso ler a planilha primeiro.", "endTimestamp": at(1)},
        {"uuid": "f-1", "timestamp": at(2), "type": "tool", "name": "execute_office_js",
         "status": "complete",
         "input": {"code": "const SEGREDO_DO_CODIGO = 1;\n" * 50,
                   "explanation": "Lê os cabeçalhos da aba Março"},
         "result": {"success": True, "result": {"rows": [["a", "b"]]}},
         "resultChars": 40, "images": [{"data": image, "mediaType": "image/png"}]},
        {"uuid": "f-2", "timestamp": at(3), "type": "tool", "name": "get_cell_ranges",
         "status": "error", "input": {"sheetName": "Março", "ranges": ["A1:B2"]},
         "error": "Intervalo inválido"},
        {"uuid": "a-1", "timestamp": at(4), "type": "assistant",
         "content": "Pronto: 3 lançamentos conciliados. 🎉", "apiId": "msg_teste",
         "model": "claude-teste-1", "effort": "max"},
    ]
    for i in range(extra_messages):
        messages.append({"uuid": f"x-{i}", "timestamp": at(5 + i), "type": "user",
                         "content": f"Mensagem extra {i}", "files": UNDEFINED})
    return {
        "id": chat_id, "userId": "user-teste", "orgId": "org-teste", "fileId": "file-teste",
        "createdAt": t0.timestamp() * 1000, "updatedAt": at(10).timestamp() * 1000,
        "surface": "sheet", "hostOrigin": None, "title": title,
        "snapshot": {
            "version": 1,
            "api": {"transcript": [{"role": "user", "content": [{"type": "text", "text": "oi"}]}]},
            "agent": {"contextWindowTokens": 1000, "snipArchive": []},
            "ui": {"messages": messages, "messageFeedback": {}},
        },
    }


def build_sample_storage(idb_dir: Path, *, chats: list[dict] | None = None,
                         external: bool = True, version: int = 10) -> FakeIndexedDB:
    """Armazenamento completo de teste, com os mesmos bancos do suplemento."""
    db = FakeIndexedDB(idb_dir)
    db.add_database("claude-chat-history", CHAT_HISTORY_SCHEMA, version=version)
    db.add_database("claude-mcp-gateways", ["gateways"])
    db.add_database("claude-local-skills", ["skills"])
    db.add_database("claude-memory", ["notes"])
    for chat in chats if chats is not None else [sample_chat()]:
        db.put("claude-chat-history", "chats", chat["id"], chat, external=external)
        db.put("claude-chat-history", "blobs", [chat["id"], "dados"],
               {"chatId": chat["id"], "name": "dados", "bytes": b'{"linhas": 3}'})
    db.put("claude-mcp-gateways", "gateways", "gw-1",
           {"id": "gw-1", "accessToken": "TOKEN-SUPER-SECRETO-NAO-EXPORTAR"})
    db.put("claude-local-skills", "skills", "minha-skill",
           {"name": "minha-skill", "content": "# Skill de teste\nPasso 1.",
            "archive": b"PK\x03\x04conteudo-zip-falso"})
    db.put("claude-memory", "notes", "nota-1", {"id": "nota-1", "text": "Prefere tabelas."})
    return db

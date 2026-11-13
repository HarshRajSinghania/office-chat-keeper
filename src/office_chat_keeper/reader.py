"""Fase 2a - Leitura: transformar os arquivos do LevelDB em objetos Python.

As camadas, de baixo para cima
------------------------------
1. **LevelDB** guarda pares (chave, valor) em bytes. Cada gravação recebe um
   número de sequência crescente; gravar de novo a mesma chave NÃO apaga a
   versão antiga na hora (ela some só na compactação). Por isso podemos achar
   várias versões da mesma chave e ficamos com a de maior sequência.
2. **IndexedDB** organiza essas chaves com um prefixo: (id do banco, id do
   object store, tipo). Bancos e stores têm nomes guardados em registros de
   metadados. Ex.: banco ``claude-chat-history``, store ``chats``.
3. O **valor** de cada registro é um objeto JavaScript serializado pelo motor
   V8 no formato *Structured Clone* (o mesmo usado por ``postMessage``):
   uma sequência de marcas ("o" objeto, '"' texto, "N" número, "D" data...).
4. Valores grandes ficam **fora** do LevelDB: o registro guarda só um ponteiro
   e o conteúdo vai para um arquivo na pasta ``.blob``.

Quem faz o trabalho pesado das camadas 1 a 4 é a biblioteca open source
``ccl_chromium_reader`` (CCL Solutions, licença MIT). Este módulo é uma capa
fina sobre ela, com três cuidados nossos:

* **texto com acento**: o V8 guarda textos "de um byte" em Latin-1. A
  biblioteca devolve ``bytes`` quando há acento; aqui sempre viram ``str``;
* **só a versão mais nova de cada chave** é decodificada (uma conversa pode
  ter 50 MB; decodificar versões velhas custaria segundos à toa);
* **banco sensível**: ``claude-mcp-gateways`` (tokens de conectores) nunca é
  decodificado por este programa.

Tudo aqui abre arquivos só para leitura, e sempre a partir de um SNAPSHOT,
nunca da pasta viva do Office.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# A biblioteca vai embutida no pacote (pasta _vendor): assim não há dependência
# externa para instalar, e o código que lê os dados é uma cópia fixa.
from ._vendor.ccl_chromium_reader import ccl_chromium_indexeddb as _idb
from ._vendor.ccl_chromium_reader.serialization_formats import ccl_blink_value_deserializer as _blink
from ._vendor.ccl_chromium_reader.serialization_formats import ccl_v8_value_deserializer as _v8
from ._vendor.ccl_chromium_reader.storage_formats import ccl_leveldb as _ldb

from .discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME

# Bancos que este programa se recusa a decodificar.
SENSITIVE_DATABASES = frozenset({"claude-mcp-gateways"})


class SensitiveDatabaseError(Exception):
    """Tentativa de ler um banco que contém segredos."""


class _UndefinedType:
    """O valor ``undefined`` do JavaScript (diferente de ``null``/None)."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED = _UndefinedType()

# Marca do V8 para cada tipo de "visão" sobre um ArrayBuffer -> construtor no JavaScript.
TYPED_ARRAY_KINDS = {
    "b": "Int8Array", "B": "Uint8Array", "C": "Uint8ClampedArray", "w": "Int16Array",
    "W": "Uint16Array", "d": "Int32Array", "D": "Uint32Array", "f": "Float32Array",
    "F": "Float64Array", "q": "BigInt64Array", "Q": "BigUint64Array", "?": "DataView",
}


@dataclass(frozen=True)
class TypedArray:
    """Um array tipado do JavaScript (ex.: Uint8Array): o tipo e os bytes crus.

    Diferente de ``bytes`` puro, que representa um ArrayBuffer. Guardar o tipo
    é o que permite devolver ao navegador exatamente o que estava lá.
    """

    kind: str  # nome do construtor no JavaScript
    data: bytes


class _Latin1Deserializer(_v8.Deserializer):
    """Desserializador V8 que sempre devolve texto de um byte como ``str``.

    Na biblioteca original, um texto de um byte com acento ("conciliação")
    volta como ``bytes``. Isso o confundiria com dado binário de verdade
    (ArrayBuffer). Decodificando como Latin-1, o que sobra em ``bytes`` é
    garantidamente binário.
    """

    def _read_one_byte_string(self) -> str:
        length = self._read_le_varint()[0]
        return self._read_raw(length).decode("latin-1")

    def _wrap_js_array_buffer_view(self, raw: bytes) -> "TypedArray":
        # A biblioteca original devolveria uma tupla de números, e o tipo
        # (Uint8Array, Float32Array...) se perderia. Aqui guardamos tipo + bytes.
        tag = chr(self._read_le_varint()[0])
        offset = self._read_le_varint()[0]
        length = self._read_le_varint()[0]
        if self.version >= 14:
            self._read_le_varint()  # flags
        if not isinstance(raw, bytes) or offset + length > len(raw):
            raise ValueError("visão de ArrayBuffer inconsistente")
        return TypedArray(TYPED_ARRAY_KINDS.get(tag, f"desconhecido:{tag}"),
                          raw[offset:offset + length])


def _read_varint(data: bytes) -> tuple[int, int]:
    """Lê um inteiro de tamanho variável (varint). Devolve (valor, bytes usados).

    Varint: cada byte carrega 7 bits do número; o bit mais alto diz "tem mais".
    """
    value = 0
    for i, byte in enumerate(data[:10]):
        value |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return value, i + 1
    raise ValueError("varint inválido")


def _flag(value: bytes | None) -> bool:
    return bool(value) and value[0] != 0


def _decode_key_path(value: bytes | None) -> Any:
    """Decodifica um keyPath do IndexedDB: None, um nome de campo ou uma lista.

    Formato: 0x00 0x00 + tipo (0 = nenhum, 1 = texto, 2 = lista) + conteúdo,
    em que cada texto é "tamanho (varint) + UTF-16 big-endian".
    """
    if not value:
        return None
    if len(value) >= 3 and value[:2] == b"\x00\x00":
        kind = value[2]
        if kind == 0:
            return None

        def read_string(position: int) -> tuple[str, int]:
            length, used = _read_varint(value[position:])
            start = position + used
            return value[start:start + length * 2].decode("utf-16-be"), start + length * 2

        if kind == 1:
            return read_string(3)[0]
        if kind == 2:
            count, used = _read_varint(value[3:])
            position, parts = 3 + used, []
            for _ in range(count):
                text, position = read_string(position)
                parts.append(text)
            return parts
    return value.decode("utf-16-be", errors="replace")  # formato antigo: só o texto


def normalize(value: Any) -> Any:
    """Converte o resultado da biblioteca em tipos Python simples.

    dict / list / str / int / float / bool / None / bytes / datetime (UTC) /
    UNDEFINED. Datas do JavaScript chegam sem fuso (são UTC); marcamos o fuso
    para ninguém confundir com hora local.
    """
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if type(value).__name__ == "_Undefined":
        return UNDEFINED
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _key_value(key: "_idb.IdbKey") -> Any:
    """Chave do IndexedDB em tipos simples (chaves compostas viram listas)."""
    if isinstance(key.value, tuple):
        return [_key_value(k) for k in key.value]
    return normalize(key.value)


@dataclass(frozen=True)
class StoredRecord:
    database: str
    store: str
    key: Any
    value: Any  # None quando ``deleted``
    seq: int  # número de sequência do LevelDB (maior = mais novo)
    deleted: bool  # a versão mais nova desta chave é uma remoção
    superseded: bool  # existe versão mais nova desta mesma chave
    external: bool  # o valor estava na pasta .blob


class SnapshotReader:
    """Abre as pastas ``.leveldb``/``.blob`` de um snapshot para leitura."""

    def __init__(self, leveldb_dir: Path, blob_dir: Path | None = None):
        self.leveldb_dir = Path(leveldb_dir)
        self.blob_dir = Path(blob_dir) if blob_dir and Path(blob_dir).is_dir() else None
        self._raw = _idb.IndexedDb(self.leveldb_dir, self.blob_dir)
        self._blink = _blink.BlinkV8Deserializer()
        # Registros que não conseguimos decodificar: (banco, store, chave).
        self.errors: list[tuple[str, str, str]] = []
        self._databases = self._load_schema()

    @classmethod
    def from_snapshot(cls, snapshot_dir: Path) -> "SnapshotReader":
        snapshot_dir = Path(snapshot_dir)
        return cls(snapshot_dir / LEVELDB_DIR_NAME, snapshot_dir / BLOB_DIR_NAME)

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> "SnapshotReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------- esquema
    def _load_schema(self) -> dict[str, list[tuple[int, dict[str, int]]]]:
        databases: dict[str, list[tuple[int, dict[str, int]]]] = {}
        for dbid in self._raw.global_metadata.db_ids:
            max_store = self._raw.get_database_metadata(
                dbid.dbid_no, _idb.DatabaseMetadataType.MaximumObjectStoreId) or 0
            stores: dict[str, int] = {}
            for store_id in range(1, max_store + 1):
                name = self._raw.get_object_store_metadata(
                    dbid.dbid_no, store_id, _idb.ObjectStoreMetadataType.StoreName)
                if name:
                    stores[name] = store_id
            databases.setdefault(dbid.name, []).append((dbid.dbid_no, stores))
        return databases

    def schema(self) -> dict[str, list[str]]:
        """Nomes dos bancos e de seus object stores, como estão no snapshot."""
        result: dict[str, list[str]] = {}
        for name, entries in self._databases.items():
            stores: list[str] = []
            for _db_id, store_map in entries:
                stores.extend(s for s in store_map if s not in stores)
            result[name] = stores
        return result

    def _latest_meta(self, prefix: bytes) -> dict[bytes, bytes]:
        """Registros de metadados vivos com este prefixo: resto da chave -> valor."""
        best: dict[bytes, Any] = {}
        for record in self._raw._fetched_records:
            key = record.user_key  # sem os 8 bytes de sequência que o LevelDB anexa
            if key.startswith(prefix) and record.state != _ldb.KeyState.Deleted:
                if key not in best or best[key].seq < record.seq:
                    best[key] = record
        return {key[len(prefix):]: record.value for key, record in best.items()}

    def schema_details(self) -> dict[str, dict]:
        """Esquema completo: versão de cada banco, keyPath e índices de cada store.

        O IndexedDB guarda isso em registros de metadados no próprio LevelDB:
        ``<banco,0,0>+4`` = versão; ``+50+<store>+1`` = keyPath do store;
        ``+100+<store>+<índice>+{0,1,2,3}`` = nome, único?, keyPath, multiEntry.
        Serve para conferir, antes de qualquer restauração, se o banco de
        destino tem o mesmo "formato" do banco de origem.
        """
        details: dict[str, dict] = {}
        for name, entries in self._databases.items():
            for db_id, store_map in entries:
                base = _idb.IndexedDb.make_prefix(db_id, 0, 0)
                raw_version = self._latest_meta(base).get(b"\x04")
                store_meta: dict[int, dict[int, bytes]] = {}
                for rest, value in self._latest_meta(base + bytes([50])).items():
                    store_id, used = _read_varint(rest)
                    if len(rest) > used:
                        store_meta.setdefault(store_id, {})[rest[used]] = value
                index_meta: dict[tuple[int, int], dict[int, bytes]] = {}
                for rest, value in self._latest_meta(base + bytes([100])).items():
                    store_id, used = _read_varint(rest)
                    index_id, used2 = _read_varint(rest[used:])
                    if len(rest) > used + used2:
                        index_meta.setdefault((store_id, index_id), {})[rest[used + used2]] = value
                stores: dict[str, dict] = {}
                for store_name, store_id in store_map.items():
                    meta = store_meta.get(store_id, {})
                    indexes = {}
                    for (owner, _index_id), imeta in index_meta.items():
                        if owner == store_id and imeta.get(0) is not None:
                            indexes[imeta[0].decode("utf-16-be")] = {
                                "key_path": _decode_key_path(imeta.get(2)),
                                "unique": _flag(imeta.get(1)),
                                "multi_entry": _flag(imeta.get(3)),
                            }
                    stores[store_name] = {
                        "key_path": _decode_key_path(meta.get(1)),
                        "auto_increment": _flag(meta.get(2)),
                        "indexes": dict(sorted(indexes.items())),
                    }
                details[name] = {
                    "version": _read_varint(raw_version)[0] if raw_version else None,
                    "stores": stores,
                }
        return details

    def keys(self, database: str, store: str) -> list[Any]:
        """Chaves vivas de um store, SEM decodificar os valores (rápido).

        Para saber "quais conversas existem" não é preciso abrir cada conversa
        de 50 MB: basta a lista de chaves.
        """
        if database in SENSITIVE_DATABASES:
            raise SensitiveDatabaseError(
                f"O banco {database!r} contém segredos e não é lido por esta ferramenta.")
        result: list[Any] = []
        for db_id, store_map in self._databases.get(database, []):
            store_id = store_map.get(store)
            if store_id is None:
                continue
            prefix = _idb.IndexedDb.make_prefix(db_id, store_id, 1)
            latest: dict[bytes, tuple[Any, Any]] = {}
            for record in self._raw._fetched_records:
                if not record.key.startswith(prefix):
                    continue
                try:
                    key = _idb.IdbKey(record.key[len(prefix):])
                except Exception:
                    continue
                current = latest.get(key.raw_key)
                if current is None or record.seq > current[0].seq:
                    latest[key.raw_key] = (record, key)
            for record, key in latest.values():
                if record.state != _ldb.KeyState.Deleted and record.value:
                    result.append(_key_value(key))
        return result

    # ------------------------------------------------------------ registros
    def records(self, database: str, store: str, *, include_superseded: bool = False,
                only: Callable[[Any], bool] | None = None) -> Iterator[StoredRecord]:
        """Percorre os registros de um object store.

        Por padrão devolve só a versão mais nova de cada chave. Com
        ``include_superseded=True`` devolve também as versões antigas que
        ainda estiverem nos arquivos (úteis como histórico). ``only`` recebe
        a chave e decide se o registro interessa: os demais nem são
        decodificados (decodificar uma conversa grande leva segundos).
        """
        if database in SENSITIVE_DATABASES:
            raise SensitiveDatabaseError(
                f"O banco {database!r} contém segredos e não é lido por esta ferramenta.")
        for db_id, store_map in self._databases.get(database, []):
            store_id = store_map.get(store)
            if store_id is None:
                continue
            yield from self._records_of(database, store, db_id, store_id, include_superseded,
                                        only)

    def _records_of(self, database: str, store: str, db_id: int, store_id: int,
                    include_superseded: bool,
                    only: Callable[[Any], bool] | None = None) -> Iterator[StoredRecord]:
        # Tipo 1 no prefixo = "dados do object store".
        prefix = _idb.IndexedDb.make_prefix(db_id, store_id, 1)
        versions: dict[bytes, list[tuple[Any, Any]]] = {}
        for record in self._raw._fetched_records:
            if not record.key.startswith(prefix):
                continue
            try:
                key = _idb.IdbKey(record.key[len(prefix):])
            except Exception:
                continue
            versions.setdefault(key.raw_key, []).append((record, key))

        for group in versions.values():
            if only is not None and not only(_key_value(group[0][1])):
                continue
            group.sort(key=lambda pair: pair[0].seq, reverse=True)
            for position, (record, key) in enumerate(group):
                if position > 0 and not include_superseded:
                    break
                deleted = record.state == _ldb.KeyState.Deleted or not record.value
                value, external = None, False
                if not deleted:
                    try:
                        value, external = self._decode(db_id, store_id, key, record.value)
                    except Exception as exc:
                        self.errors.append((database, store,
                                            f"{_key_value(key)!r} ({type(exc).__name__})"))
                        continue
                yield StoredRecord(database, store, _key_value(key), value, record.seq,
                                   deleted, position > 0, external)

    def _decode(self, db_id: int, store_id: int, key: Any, raw_value: bytes) -> tuple[Any, bool]:
        # Formato do valor: varint (versão do registro) + envelope do Blink +
        # objeto V8. O envelope pode dizer "o conteúdo está no arquivo de blob
        # nº X" ou "o conteúdo está comprimido com snappy"; a biblioteca
        # resolve os dois casos em read_record_precursor.
        _version, used = _read_varint(raw_value)
        precursor = self._raw.read_record_precursor(key, db_id, store_id, raw_value[used:], None)
        if precursor is None:
            raise ValueError("valor externo não encontrado")
        _blink_version, stream, _trailer, external_path = precursor
        value = _Latin1Deserializer(stream, host_object_delegate=self._blink.read).read()
        return normalize(value), external_path not in (None, "None")

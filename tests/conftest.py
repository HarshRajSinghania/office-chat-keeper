"""Fixtures SINTÉTICAS para os testes.

Regra do projeto: nenhum dado real entra no repositório. Aqui criamos, por
código, pastas com a mesma ESTRUTURA do armazenamento do Office, mas com
conteúdo falso (bytes quaisquer). Para testar descoberta e cópia isso basta:
essas etapas não interpretam o conteúdo dos arquivos.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from office_chat_keeper.config import Config
from office_chat_keeper.discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME

DEFAULT_PROFILE = "aaaa1111_ADAL/2/0"


def make_source(root: Path, profile: str = DEFAULT_PROFILE, *, with_blob: bool = True) -> Path:
    """Cria uma fonte falsa e devolve a pasta .leveldb."""
    idb = root / profile / "EBWebView" / "Default" / "IndexedDB"
    leveldb = idb / LEVELDB_DIR_NAME
    leveldb.mkdir(parents=True)
    (leveldb / "CURRENT").write_text("MANIFEST-000001\n")
    (leveldb / "LOCK").write_bytes(b"")
    (leveldb / "LOG").write_text("diagnostico\n")
    (leveldb / "MANIFEST-000001").write_bytes(b"manifest-falso")
    (leveldb / "000003.log").write_bytes(b"diario-falso")
    (leveldb / "000005.ldb").write_bytes(b"tabela-falsa" * 100)
    if with_blob:
        blob = idb / BLOB_DIR_NAME / "1" / "00"
        blob.mkdir(parents=True)
        (blob / "17").write_bytes(b"blob-grande-falso" * 1000)
    # Vizinhos que NUNCA podem ser copiados.
    local_storage = idb.parent / "Local Storage" / "leveldb"
    local_storage.mkdir(parents=True, exist_ok=True)
    (local_storage / "000001.log").write_bytes(b"login-falso")
    other = idb / "https_outro.site_0.indexeddb.leveldb"
    other.mkdir(exist_ok=True)
    (other / "CURRENT").write_text("MANIFEST-000001\n")
    return leveldb


@pytest.fixture(autouse=True)
def notifications(monkeypatch) -> list[tuple[str, str]]:
    """Nenhum teste mostra notificação de verdade: só anotamos o que seria mostrado."""
    sent: list[tuple[str, str]] = []

    def fake_notify(title: str, message: str, **_kwargs) -> bool:
        sent.append((title, message))
        return True

    monkeypatch.setattr("office_chat_keeper.notify.notify", fake_notify)
    return sent


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "webview2"
    root.mkdir()
    return root


@pytest.fixture
def cfg(tmp_path: Path, source_root: Path) -> Config:
    # retry_delay=0: os testes não esperam de verdade.
    return Config(data_dir=tmp_path / "dados", source_root=source_root,
                  copy_attempts=3, file_attempts=3, retry_delay=0.0)

"""Fase 1a - Descoberta: achar onde o Office guardou o histórico.

Conceitos
---------
* **IndexedDB** é o banco de dados que todo navegador oferece às páginas web.
  O suplemento do Claude é uma página (https://pivot.claude.ai) rodando dentro
  do Office em um navegador embutido (WebView2, que é o Chromium/Edge).
* O Chromium guarda o IndexedDB de cada site ("origem") em duas pastas irmãs:

  - ``<origem>.indexeddb.leveldb`` -> o banco em si, no formato **LevelDB**
    (um banco chave-valor feito de arquivos ``.log`` e ``.ldb``);
  - ``<origem>.indexeddb.blob``    -> valores grandes demais para o LevelDB,
    cada um em um arquivo separado.

  A origem vira nome de pasta trocando ``://`` por ``_``:
  ``https://pivot.claude.ai`` (porta padrão 0) -> ``https_pivot.claude.ai_0``.

Por que procurar em vez de fixar o caminho
------------------------------------------
O Office muda a subpasta do perfil (``2\\0``, ``2\\1``...) nas atualizações e
abre o suplemento em uma pasta nova e vazia. Foi assim que o histórico "sumiu".
Por isso varremos ``Wef\\webview2`` inteiro e tratamos CADA pasta encontrada
como uma fonte independente.

Este módulo só LISTA pastas e lê datas. Não abre nem altera nenhum arquivo.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

ORIGIN_PREFIX = "https_pivot.claude.ai_0"
LEVELDB_DIR_NAME = ORIGIN_PREFIX + ".indexeddb.leveldb"
BLOB_DIR_NAME = ORIGIN_PREFIX + ".indexeddb.blob"


@dataclass(frozen=True)
class Source:
    """Uma fonte = um armazenamento ``pivot.claude.ai`` encontrado no disco."""

    source_id: str  # identificador curto e estável, derivado do caminho relativo
    rel_path: str  # caminho da pasta .leveldb relativo à raiz varrida
    leveldb_dir: Path
    blob_dir: Path | None  # só existe depois que algum valor grande é gravado
    # "Certidão de nascimento" do banco. O arquivo LOCK é criado uma única vez,
    # quando o banco nasce; se essa data mudar, o banco foi RECRIADO (perda).
    lock_created_at: float | None
    current_created_at: float | None

    @property
    def db_created_at(self) -> float | None:
        """Melhor estimativa de quando este banco foi criado (epoch, segundos)."""
        if self.lock_created_at is not None:
            return self.lock_created_at
        if self.current_created_at is not None:
            return self.current_created_at
        return creation_time(self.leveldb_dir)


def creation_time(path: Path) -> float | None:
    """Data de criação de um arquivo/pasta no Windows, ou None se não existir.

    No Windows, ``st_birthtime`` (Python 3.12+) e ``st_ctime`` (versões
    anteriores) representam a data de CRIAÇÃO, não a de modificação.
    """
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return getattr(st, "st_birthtime", st.st_ctime)


def make_source_id(rel_path: str) -> str:
    """Gera um id curto a partir do caminho relativo.

    Usamos um hash (SHA-256 truncado) porque o caminho real é longo e contém
    barras. O mesmo caminho gera sempre o mesmo id; se o Office mudar de
    subpasta, o id muda - e isso é desejado: é OUTRA fonte.
    """
    normalized = rel_path.replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _build_source(root: Path, leveldb_dir: Path) -> Source:
    try:
        rel_path = str(leveldb_dir.relative_to(root))
    except ValueError:
        rel_path = leveldb_dir.name
    if rel_path in ("", "."):
        rel_path = leveldb_dir.name
    blob_dir = leveldb_dir.parent / BLOB_DIR_NAME
    return Source(
        source_id=make_source_id(rel_path),
        rel_path=rel_path,
        leveldb_dir=leveldb_dir,
        blob_dir=blob_dir if blob_dir.is_dir() else None,
        lock_created_at=creation_time(leveldb_dir / "LOCK"),
        current_created_at=creation_time(leveldb_dir / "CURRENT"),
    )


def discover_sources(root: str | os.PathLike,
                     exclude: tuple[Path, ...] | list[Path] = ()) -> list[Source]:
    """Procura recursivamente todas as fontes ``pivot.claude.ai`` sob ``root``.

    ``exclude`` lista pastas que NÃO devem ser varridas. Usamos para pular a
    nossa própria pasta de snapshots: as cópias têm o mesmo nome de pasta e,
    sem isso, o backup "descobriria" a si mesmo como fonte.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    root = root.resolve()

    if root.name.lower() == LEVELDB_DIR_NAME:
        # Apontaram direto para a pasta do banco.
        return [_build_source(root.parent, root)]

    # Só vale checar exclusões que realmente ficam dentro da raiz varrida.
    excluded = [Path(p).resolve() for p in exclude]
    excluded = [p for p in excluded if p == root or root in p.parents]

    found: list[Source] = []
    # os.walk percorre a árvore de pastas. Ao editar "dirnames" durante o
    # passeio dizemos em quais subpastas ele NÃO deve entrar (poda).
    for dirpath, dirnames, _filenames in os.walk(root):
        here = Path(dirpath)
        keep: list[str] = []
        for name in dirnames:
            lower = name.lower()
            full = here / name
            if lower == LEVELDB_DIR_NAME:
                found.append(_build_source(root, full))
                continue  # achou: não precisa entrar na pasta do banco
            if lower == BLOB_DIR_NAME:
                continue  # é a irmã da .leveldb; nada a procurar dentro
            if any(full == ex or ex in full.parents for ex in excluded):
                continue
            keep.append(name)
        dirnames[:] = keep

    found.sort(key=lambda s: s.rel_path.lower())
    return found

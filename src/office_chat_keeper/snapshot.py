"""Fase 1b - Snapshot: copiar o armazenamento bruto sem tocar na origem.

O que é um snapshot
-------------------
Uma "fotografia" das pastas ``.leveldb`` e ``.blob`` em um instante, guardada
em ``<dados>\\snapshots\\<id-da-fonte>\\<AAAAMMDD-HHMMSS>\\``. É a matéria-prima
de tudo o que vem depois (leitura, arquivo permanente, restauração).

Como o LevelDB funciona (o suficiente para entender as decisões abaixo)
----------------------------------------------------------------------
* ``NNNNNN.log``  -> diário: toda gravação nova é ANEXADA aqui primeiro.
* ``NNNNNN.ldb``  -> tabelas ordenadas e IMUTÁVEIS. De tempos em tempos o
  LevelDB "compacta": junta o diário e tabelas antigas em tabelas novas e apaga
  as velhas. Um ``.ldb`` nunca é editado, só criado e apagado.
* ``MANIFEST-N``  -> o índice que diz quais ``.ldb`` valem agora.
* ``CURRENT``     -> uma linha dizendo qual MANIFEST está em uso.
* ``LOCK``        -> arquivo vazio que o programa dono segura travado.
* ``LOG``/``LOG.old`` -> texto de diagnóstico; não contém dados.

Decisões que saem disso
-----------------------
1. **Só copiar quando mudou.** Calculamos uma "impressão digital" (nomes,
   tamanhos e datas dos arquivos de dados). Igual ao último snapshot? Nada a
   fazer. ``LOG`` e ``LOCK`` ficam fora porque mudam sem que os dados mudem.
2. **Cópia consistente com o Excel aberto.** Tiramos a impressão digital antes
   e depois de copiar. Se mudou no meio (o Office gravou ou compactou), a cópia
   pode estar "rasgada": descartamos e tentamos de novo.
3. **Não gastar disco à toa.** Arquivos idênticos aos do snapshot anterior não
   são copiados de novo: criamos um *hardlink* (um segundo nome para o mesmo
   conteúdo no disco). Um blob de 50 MB ocupa 50 MB uma vez só, mesmo
   aparecendo em 40 snapshots. Apagar um snapshot não afeta os outros.
4. **Somente leitura na origem.** Os arquivos da origem são abertos apenas em
   modo ``"rb"`` (leitura binária). O ``LOCK`` nem é aberto.
5. **Retenção que nunca apaga a última foto de um banco.** Ver
   ``choose_snapshots_to_delete``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Config, is_inside
from .discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME, Source

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1
LOCK_NAME = "LOCK"
VOLATILE_NAMES = frozenset({"LOG", "LOG.old"})

_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
_TEMP_PREFIX = ".incompleto-"
_STALE_TEMP_SECONDS = 3600
_CHUNK = 1024 * 1024  # lê 1 MB por vez: pouca memória, pouca CPU


# --------------------------------------------------------------------------
# Estruturas de dados
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FileEntry:
    """Um arquivo da origem, visto sem abrir (só metadados)."""

    rel: str  # caminho dentro do snapshot, com "/" (ex.: "<pasta>.leveldb/000008.ldb")
    path: Path  # caminho real na origem
    size: int
    mtime_ns: int  # data de modificação em nanossegundos
    kind: str  # "data" | "volatile" (LOG) | "lock"


@dataclass(frozen=True)
class SnapshotInfo:
    """Um snapshot já gravado: sua pasta e o conteúdo do manifest.json."""

    path: Path
    manifest: dict

    @property
    def taken_at(self) -> datetime:
        return datetime.fromisoformat(self.manifest["taken_at"])

    @property
    def stable(self) -> bool:
        return bool(self.manifest.get("stable"))


@dataclass(frozen=True)
class SnapshotResult:
    status: str  # "created" | "unchanged" | "failed"
    source_id: str
    path: Path | None = None
    manifest: dict | None = None
    message: str = ""
    deleted: int = 0  # snapshots antigos removidos pela retenção


# --------------------------------------------------------------------------
# Olhar a origem (sem abrir arquivos)
# --------------------------------------------------------------------------
def scan_source(source: Source) -> list[FileEntry]:
    """Lista os arquivos da fonte com tamanho e data. Não lê conteúdo."""
    entries: list[FileEntry] = []
    bases = [(source.leveldb_dir, LEVELDB_DIR_NAME)]
    if source.blob_dir is not None:
        bases.append((source.blob_dir, BLOB_DIR_NAME))

    for base, snapshot_name in bases:
        for dirpath, _dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            for name in filenames:
                path = here / name
                try:
                    st = path.stat()
                except OSError:
                    continue  # sumiu entre listar e olhar (compactação): normal
                kind = "data"
                if base is source.leveldb_dir and here == base:
                    if name == LOCK_NAME:
                        kind = "lock"
                    elif name in VOLATILE_NAMES:
                        kind = "volatile"
                rel = f"{snapshot_name}/{path.relative_to(base).as_posix()}"
                entries.append(FileEntry(rel, path, st.st_size, st.st_mtime_ns, kind))

    entries.sort(key=lambda e: e.rel)
    return entries


def fingerprint(entries: list[FileEntry]) -> str:
    """Impressão digital do ESTADO da fonte (não do conteúdo).

    É um hash de "nome|tamanho|data" de cada arquivo de dados. Barato de
    calcular (não lê os 50 MB) e suficiente para responder "mudou algo?".
    """
    h = hashlib.sha256()
    for e in entries:
        if e.kind == "data":
            h.update(f"{e.rel}|{e.size}|{e.mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------
# Snapshots já existentes
# --------------------------------------------------------------------------
def list_snapshots(source_snap_dir: Path) -> list[SnapshotInfo]:
    """Snapshots válidos de uma fonte, do mais antigo para o mais novo."""
    if not source_snap_dir.is_dir():
        return []
    result: list[SnapshotInfo] = []
    for child in sorted(source_snap_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not _SNAPSHOT_NAME_RE.match(child.name):
            continue
        try:
            manifest = json.loads((child / MANIFEST_NAME).read_text(encoding="utf-8"))
            datetime.fromisoformat(manifest["taken_at"])
        except (OSError, ValueError, KeyError, TypeError):
            # Pasta sem manifesto legível: não confiamos nela, mas também NÃO
            # a apagamos automaticamente. Fica para inspeção manual.
            continue
        result.append(SnapshotInfo(child, manifest))
    return result


# --------------------------------------------------------------------------
# Cópia
# --------------------------------------------------------------------------
def _open_for_read(path: Path):
    """Único ponto em que um arquivo da ORIGEM é aberto - sempre só leitura."""
    return open(path, "rb")


def _copy_file(src: Path, dst: Path, attempts: int, delay: float,
               sleep: Callable[[float], None]) -> tuple[str, int]:
    """Copia ``src`` -> ``dst`` calculando o SHA-256 no caminho.

    Devolve (hash, bytes copiados). Se o arquivo estiver bloqueado por outro
    programa (PermissionError), espera e tenta de novo. Se o arquivo SUMIU
    (FileNotFoundError), não adianta insistir: a fonte mudou e quem chamou
    recomeça o snapshot.
    """
    last_error: OSError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            h = hashlib.sha256()
            copied = 0
            with _open_for_read(src) as fin, open(dst, "wb") as fout:
                while chunk := fin.read(_CHUNK):
                    h.update(chunk)
                    fout.write(chunk)
                    copied += len(chunk)
            return h.hexdigest(), copied
        except FileNotFoundError:
            raise
        except OSError as exc:
            last_error = exc
            if attempt < attempts:
                sleep(delay)
    assert last_error is not None
    raise last_error


def _try_hardlink(previous: Path, dst: Path, expected_size: int) -> bool:
    """Tenta reaproveitar o arquivo do snapshot anterior via hardlink.

    Hardlink = outro nome de arquivo apontando para os MESMOS bytes no disco
    (só funciona dentro do mesmo volume NTFS). Se falhar por qualquer motivo,
    devolvemos False e o arquivo é copiado normalmente.
    """
    try:
        if previous.stat().st_size != expected_size:
            return False
        os.link(previous, dst)
        return True
    except OSError:
        return False


def _copy_entries(source: Source, entries: list[FileEntry], tmp: Path,
                  latest: SnapshotInfo | None, cfg: Config,
                  sleep: Callable[[float], None]) -> list[dict]:
    """Copia (ou linka) todos os arquivos para a pasta temporária."""
    previous_files: dict[str, dict] = {}
    if latest is not None and latest.stable:
        previous_files = {f["path"]: f for f in latest.manifest.get("files", [])}

    (tmp / LEVELDB_DIR_NAME).mkdir(parents=True, exist_ok=True)
    if source.blob_dir is not None:
        (tmp / BLOB_DIR_NAME).mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    for entry in entries:
        dst = tmp / entry.rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        if entry.kind == "lock":
            # O LOCK está travado pelo Office e é vazio. Não o abrimos: criamos
            # um arquivo vazio de mesmo nome para o snapshot ficar completo.
            dst.touch()
            continue

        old = previous_files.get(entry.rel)
        linked = False
        sha256: str | None = None
        size = entry.size
        if (entry.kind == "data" and old and old.get("sha256")
                and old.get("size") == entry.size
                and old.get("mtime_ns") == entry.mtime_ns):
            assert latest is not None
            linked = _try_hardlink(latest.path / entry.rel, dst, entry.size)
            if linked:
                sha256 = old["sha256"]

        if not linked:
            try:
                sha256, size = _copy_file(entry.path, dst, cfg.file_attempts,
                                          cfg.retry_delay, sleep)
            except OSError:
                if entry.kind == "volatile":
                    # LOG é só diagnóstico: se não deu para copiar, segue sem ele.
                    dst.unlink(missing_ok=True)
                    continue
                raise
            # Preserva a data de modificação original na cópia.
            os.utime(dst, ns=(entry.mtime_ns, entry.mtime_ns))

        files.append({
            "path": entry.rel,
            "size": size,
            "mtime_ns": entry.mtime_ns,
            "sha256": sha256,
            "kind": entry.kind,
            "linked": linked,
        })
    return files


def _clean_stale_temp(source_snap_dir: Path) -> None:
    """Remove pastas temporárias NOSSAS que sobraram de uma execução interrompida."""
    now = time.time()
    for child in source_snap_dir.glob(_TEMP_PREFIX + "*"):
        try:
            if child.is_dir() and now - child.stat().st_mtime > _STALE_TEMP_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _unique_final_dir(source_snap_dir: Path, now: datetime) -> Path:
    base = now.strftime("%Y%m%d-%H%M%S")
    candidate = source_snap_dir / base
    counter = 2
    while candidate.exists():
        candidate = source_snap_dir / f"{base}-{counter}"
        counter += 1
    return candidate


def snapshot_source(source: Source, cfg: Config, *, now: datetime | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    protected: frozenset[str] | set[str] = frozenset()) -> SnapshotResult:
    """Tira (se necessário) um snapshot de uma fonte. Nunca altera a origem.

    Passo a passo:
      1. olha a origem e calcula a impressão digital;
      2. se for igual à do último snapshot estável -> "unchanged";
      3. copia para uma pasta temporária (``.incompleto-...``);
      4. olha a origem de novo: se mudou durante a cópia, tenta outra vez;
      5. grava o ``manifest.json`` e RENOMEIA a pasta temporária para o nome
         final. Renomear é instantâneo, então nunca existe um snapshot "pela
         metade" com nome de snapshot válido;
      6. aplica a retenção.
    """
    now = now or datetime.now().astimezone()
    source_snap_dir = cfg.snapshots_dir / source.source_id
    source_snap_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale_temp(source_snap_dir)

    existing = list_snapshots(source_snap_dir)
    latest = existing[-1] if existing else None

    entries = scan_source(source)
    fp = fingerprint(entries)
    if latest is not None and latest.stable and latest.manifest.get("fingerprint") == fp:
        return SnapshotResult("unchanged", source.source_id, latest.path, latest.manifest)

    files: list[dict] | None = None
    tmp: Path | None = None
    stable = False
    error = ""
    attempt = 0
    total_attempts = max(1, cfg.copy_attempts)
    for attempt in range(1, total_attempts + 1):
        tmp = source_snap_dir / f"{_TEMP_PREFIX}{os.getpid()}-{attempt}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            files = _copy_entries(source, entries, tmp, latest, cfg, sleep)
        except OSError as exc:
            # Arquivo sumiu (compactação) ou ficou bloqueado até o fim.
            files = None
            error = f"{type(exc).__name__}"
            shutil.rmtree(tmp, ignore_errors=True)

        after = scan_source(source)
        fp_after = fingerprint(after)
        if files is not None and fp_after == fp:
            stable = True
            break
        if attempt == total_attempts:
            break
        # A fonte mudou durante a cópia: descarta e recomeça do estado novo.
        if files is not None:
            shutil.rmtree(tmp, ignore_errors=True)
            files = None
        entries, fp = after, fp_after
        sleep(cfg.retry_delay * attempt)

    if files is None or tmp is None:
        log.warning("snapshot falhou: fonte=%s tentativas=%d erro=%s",
                    source.source_id, attempt, error or "desconhecido")
        return SnapshotResult("failed", source.source_id,
                              message=f"não foi possível copiar ({error or 'erro desconhecido'})")

    manifest = {
        "format": MANIFEST_FORMAT,
        "tool_version": __version__,
        "source_id": source.source_id,
        "source_rel_path": source.rel_path,
        "source_leveldb_dir": str(source.leveldb_dir),
        "has_blob_dir": source.blob_dir is not None,
        "taken_at": now.isoformat(timespec="seconds"),
        "db_created_at": _iso(source.db_created_at),
        "lock_created_at": _iso(source.lock_created_at),
        "current_created_at": _iso(source.current_created_at),
        "fingerprint": fp,
        # stable=False: a fonte não parou de mudar; a cópia pode estar
        # inconsistente. Guardamos mesmo assim (melhor que nada), mas a próxima
        # execução tira outro e as fases seguintes sabem que devem desconfiar.
        "stable": stable,
        "attempts": attempt,
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "copied_bytes": sum(f["size"] for f in files if not f["linked"]),
        "files": files,
    }
    (tmp / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    final_dir = _unique_final_dir(source_snap_dir, now)
    os.replace(tmp, final_dir)

    log.info("snapshot criado: fonte=%s pasta=%s arquivos=%d bytes=%d copiados=%d estavel=%s tentativas=%d",
             source.source_id, final_dir.name, manifest["file_count"],
             manifest["total_bytes"], manifest["copied_bytes"], stable, attempt)

    deleted = prune_snapshots(source_snap_dir, cfg, now=now, protected=protected)
    return SnapshotResult("created", source.source_id, final_dir, manifest, deleted=deleted)


# --------------------------------------------------------------------------
# Retenção
# --------------------------------------------------------------------------
def choose_snapshots_to_delete(snapshots: list[SnapshotInfo], *, now: datetime,
                               keep_last: int, keep_daily_days: int,
                               protected: frozenset[str] | set[str] = frozenset()
                               ) -> list[SnapshotInfo]:
    """Decide quais snapshots podem ser apagados. Função pura (não apaga nada).

    Ficam SEMPRE:
      a) os ``keep_last`` mais recentes;
      b) o mais recente de cada dia, nos últimos ``keep_daily_days`` dias;
      c) o mais recente de cada "geração" do banco (mesma data de criação do
         LOCK). Esta é a regra de proteção contra a perda: se o Office recriar
         o banco vazio, a última foto do banco ANTIGO nunca é apagada, por mais
         snapshots novos que apareçam depois;
      d) os nomes em ``protected``: snapshots que são a cópia bruta mais
         recente de alguma conversa (o arquivo permanente diz quais são). Sem
         isso, uma conversa apagada na origem acabaria ficando sem cópia bruta
         para uma eventual restauração.
    """
    keep: set[str] = set(protected)
    if keep_last > 0:
        keep.update(s.path.name for s in snapshots[-keep_last:])

    cutoff = now - timedelta(days=keep_daily_days)
    newest_of_day: dict[object, str] = {}
    newest_of_generation: dict[object, str] = {}
    newest_stable_of_generation: dict[object, str] = {}
    for snap in snapshots:  # do mais antigo para o mais novo: o último vence
        taken = snap.taken_at
        if taken >= cutoff:
            newest_of_day[taken.date()] = snap.path.name
        generation = snap.manifest.get("db_created_at")
        newest_of_generation[generation] = snap.path.name
        if snap.stable:
            newest_stable_of_generation[generation] = snap.path.name

    keep.update(newest_of_day.values())
    keep.update(newest_of_generation.values())
    keep.update(newest_stable_of_generation.values())
    return [s for s in snapshots if s.path.name not in keep]


def prune_snapshots(source_snap_dir: Path, cfg: Config, *, now: datetime | None = None,
                    protected: frozenset[str] | set[str] = frozenset()) -> int:
    """Aplica a retenção em uma fonte. Devolve quantos snapshots removeu."""
    now = now or datetime.now().astimezone()
    doomed = choose_snapshots_to_delete(
        list_snapshots(source_snap_dir), now=now,
        keep_last=cfg.keep_last, keep_daily_days=cfg.keep_daily_days, protected=protected)
    removed = 0
    for snap in doomed:
        # Cinto de segurança: só apagamos pastas dentro da NOSSA área de snapshots.
        if snap.path.parent != source_snap_dir or not is_inside(snap.path, cfg.snapshots_dir):
            continue
        shutil.rmtree(snap.path, ignore_errors=True)
        if not snap.path.exists():
            removed += 1
    if removed:
        log.info("retencao: pasta=%s removidos=%d", source_snap_dir.name, removed)
    return removed

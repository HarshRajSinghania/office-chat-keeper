"""Testes do snapshot: cópia fiel, origem intocada, retenção."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from office_chat_keeper import snapshot as snap_mod
from office_chat_keeper.discovery import BLOB_DIR_NAME, LEVELDB_DIR_NAME, discover_sources
from office_chat_keeper.snapshot import (MANIFEST_NAME, SnapshotInfo,
                                         choose_snapshots_to_delete, list_snapshots,
                                         snapshot_source)

from conftest import make_source

T0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)


def tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    """Estado completo de uma árvore: tamanho, data e hash de cada arquivo."""
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            state[str(path.relative_to(root))] = (
                st.st_size, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return state


def no_sleep(_seconds: float) -> None:
    pass


@pytest.fixture
def source(source_root):
    make_source(source_root)
    return discover_sources(source_root)[0]


def test_snapshot_copia_as_duas_pastas_e_grava_manifesto(source, cfg):
    result = snapshot_source(source, cfg, now=T0, sleep=no_sleep)

    assert result.status == "created"
    assert result.path.name == "20261111-120000"
    assert result.path.parent == cfg.snapshots_dir / source.source_id
    for entry in snap_mod.scan_source(source):
        copied = result.path / entry.rel
        assert copied.is_file()
        if entry.kind == "data":
            assert copied.read_bytes() == entry.path.read_bytes()

    manifest = json.loads((result.path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["stable"] is True
    assert manifest["source_id"] == source.source_id
    assert manifest["db_created_at"] is not None
    blob = next(f for f in manifest["files"] if f["path"].startswith(BLOB_DIR_NAME))
    assert blob["sha256"] == hashlib.sha256((result.path / blob["path"]).read_bytes()).hexdigest()


def test_snapshot_nao_altera_a_origem(source, cfg, source_root):
    before = tree_state(source_root)
    snapshot_source(source, cfg, now=T0, sleep=no_sleep)
    assert tree_state(source_root) == before


def test_snapshot_nao_copia_local_storage_nem_outras_origens(source, cfg):
    result = snapshot_source(source, cfg, now=T0, sleep=no_sleep)
    top = sorted(p.name for p in result.path.iterdir())
    assert top == sorted([LEVELDB_DIR_NAME, BLOB_DIR_NAME, MANIFEST_NAME])
    everything = [str(p) for p in cfg.data_dir.rglob("*")]
    assert not any("Local Storage" in p or "outro.site" in p for p in everything)


def test_lock_nao_e_lido_e_vira_arquivo_vazio(source, cfg, monkeypatch):
    opened = []
    real_open = snap_mod._open_for_read

    def spy(path):
        opened.append(Path(path).name)
        return real_open(path)

    monkeypatch.setattr(snap_mod, "_open_for_read", spy)
    result = snapshot_source(source, cfg, now=T0, sleep=no_sleep)

    assert "LOCK" not in opened
    lock = result.path / LEVELDB_DIR_NAME / "LOCK"
    assert lock.is_file() and lock.stat().st_size == 0


def test_segunda_execucao_sem_mudanca_nao_copia_nada(source, cfg):
    first = snapshot_source(source, cfg, now=T0, sleep=no_sleep)
    # Mexer só no LOG (diagnóstico) não conta como mudança de dados.
    (source.leveldb_dir / "LOG").write_text("reaberto\n")
    second = snapshot_source(source, cfg, now=T0 + timedelta(minutes=15), sleep=no_sleep)

    assert second.status == "unchanged"
    assert second.path == first.path
    assert len(list_snapshots(cfg.snapshots_dir / source.source_id)) == 1


def test_mudanca_gera_novo_snapshot_e_reaproveita_arquivos_iguais(source, cfg):
    first = snapshot_source(source, cfg, now=T0, sleep=no_sleep)
    with open(source.leveldb_dir / "000003.log", "ab") as fh:  # a conversa cresceu
        fh.write(b"mais-dados")
    second = snapshot_source(source, cfg, now=T0 + timedelta(minutes=15), sleep=no_sleep)

    assert second.status == "created" and second.path != first.path
    files = {f["path"]: f for f in second.manifest["files"]}
    blob_rel = f"{BLOB_DIR_NAME}/1/00/17"
    log_rel = f"{LEVELDB_DIR_NAME}/000003.log"
    assert files[blob_rel]["linked"] is True  # não mudou: hardlink
    assert files[log_rel]["linked"] is False  # mudou: cópia nova
    assert os.path.samefile(first.path / blob_rel, second.path / blob_rel)
    assert (second.path / log_rel).read_bytes().endswith(b"mais-dados")
    assert second.manifest["copied_bytes"] < second.manifest["total_bytes"]


def test_arquivo_em_uso_tenta_de_novo(source, cfg, monkeypatch):
    real_open = snap_mod._open_for_read
    failures = {"left": 2}

    def flaky(path):
        if Path(path).name == "000003.log" and failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError("arquivo em uso por outro processo")
        return real_open(path)

    monkeypatch.setattr(snap_mod, "_open_for_read", flaky)
    result = snapshot_source(source, cfg, now=T0, sleep=no_sleep)

    assert result.status == "created"
    assert failures["left"] == 0
    assert result.manifest["stable"] is True


def test_arquivo_bloqueado_para_sempre_falha_sem_deixar_lixo(source, cfg, monkeypatch):
    def locked(path):
        raise PermissionError("bloqueado")

    monkeypatch.setattr(snap_mod, "_open_for_read", locked)
    result = snapshot_source(source, cfg, now=T0, sleep=no_sleep)

    assert result.status == "failed"
    source_snap_dir = cfg.snapshots_dir / source.source_id
    assert list_snapshots(source_snap_dir) == []
    assert list(source_snap_dir.iterdir()) == []  # nenhuma pasta .incompleto-


def test_fonte_que_muda_durante_a_copia_e_refeita(source, cfg, monkeypatch):
    real_copy = snap_mod._copy_entries
    calls = {"n": 0}

    def copy_then_office_writes(src, entries, tmp, latest, cfg_, sleep):
        files = real_copy(src, entries, tmp, latest, cfg_, sleep)
        calls["n"] += 1
        if calls["n"] == 1:  # o "Office" grava logo depois da 1ª cópia
            with open(src.leveldb_dir / "000003.log", "ab") as fh:
                fh.write(b"gravado-durante-a-copia")
        return files

    monkeypatch.setattr(snap_mod, "_copy_entries", copy_then_office_writes)
    result = snapshot_source(source, cfg, now=T0, sleep=no_sleep)

    assert result.status == "created"
    assert result.manifest["stable"] is True
    assert result.manifest["attempts"] == 2
    copied = result.path / LEVELDB_DIR_NAME / "000003.log"
    assert copied.read_bytes().endswith(b"gravado-durante-a-copia")


def test_fonte_que_nunca_para_de_mudar_e_marcada_instavel(source, cfg, monkeypatch):
    real_copy = snap_mod._copy_entries

    def always_changing(src, entries, tmp, latest, cfg_, sleep):
        files = real_copy(src, entries, tmp, latest, cfg_, sleep)
        with open(src.leveldb_dir / "000003.log", "ab") as fh:
            fh.write(b"x")
        return files

    monkeypatch.setattr(snap_mod, "_copy_entries", always_changing)
    first = snapshot_source(source, cfg, now=T0, sleep=no_sleep)
    assert first.status == "created"
    assert first.manifest["stable"] is False
    assert first.manifest["attempts"] == cfg.copy_attempts

    # Um snapshot instável nunca conta como "já está em dia".
    monkeypatch.setattr(snap_mod, "_copy_entries", real_copy)
    second = snapshot_source(source, cfg, now=T0 + timedelta(minutes=15), sleep=no_sleep)
    assert second.status == "created"
    assert second.manifest["stable"] is True


def test_fonte_sem_pasta_blob(source_root, cfg):
    make_source(source_root, "cccc3333_ADAL/2/1", with_blob=False)
    src = next(s for s in discover_sources(source_root) if s.blob_dir is None)
    result = snapshot_source(src, cfg, now=T0, sleep=no_sleep)
    assert result.status == "created"
    assert not (result.path / BLOB_DIR_NAME).exists()


# ---------------------------------------------------------------- retenção
def fake_snaps(specs: list[tuple[datetime, str, bool]]) -> list[SnapshotInfo]:
    snaps = []
    for taken, generation, stable in specs:
        name = taken.strftime("%Y%m%d-%H%M%S")
        snaps.append(SnapshotInfo(Path(name), {
            "taken_at": taken.isoformat(), "db_created_at": generation, "stable": stable}))
    return snaps


def test_retencao_mantem_ultimos_e_um_por_dia():
    # 3 dias, 4 snapshots por dia, mesma geração.
    specs = [(T0 - timedelta(days=d, hours=h), "g1", True)
             for d in (2, 1, 0) for h in (9, 6, 3, 0)]
    snaps = fake_snaps(specs)
    doomed = choose_snapshots_to_delete(snaps, now=T0, keep_last=2, keep_daily_days=30)
    kept = [s.path.name for s in snaps if s not in doomed]
    # Ficam: o mais novo de cada dia (3) + os 2 últimos (1 deles já contado).
    assert kept == ["20261109-120000", "20261110-120000", "20261111-090000", "20261111-120000"]


def test_retencao_nunca_apaga_a_ultima_foto_de_um_banco_antigo():
    # Banco antigo (g-antigo) fotografado há 90 dias; depois o Office recriou
    # o banco (g-novo) e vieram muitos snapshots novos.
    old = [(T0 - timedelta(days=90, hours=h), "g-antigo", True) for h in (2, 1)]
    new = [(T0 - timedelta(minutes=15 * i), "g-novo", True) for i in range(20, 0, -1)]
    snaps = fake_snaps(old + new)
    doomed = choose_snapshots_to_delete(snaps, now=T0, keep_last=3, keep_daily_days=30)
    doomed_names = {s.path.name for s in doomed}

    last_old = snaps[1].path.name
    assert last_old not in doomed_names  # protegido pela regra da geração
    assert snaps[0].path.name in doomed_names  # o penúltimo do banco antigo pode ir
    assert len(snaps) - len(doomed) < len(snaps)


def test_retencao_prefere_guardar_o_ultimo_estavel_da_geracao():
    specs = [(T0 - timedelta(days=60), "g1", True),
             (T0 - timedelta(days=59), "g1", False)]
    snaps = fake_snaps(specs)
    doomed = choose_snapshots_to_delete(snaps, now=T0, keep_last=0, keep_daily_days=1)
    assert doomed == []  # fica o mais novo E o último estável


def test_retencao_apaga_de_verdade_so_dentro_da_pasta_de_snapshots(source, cfg):
    from dataclasses import replace
    tight = replace(cfg, keep_last=1, keep_daily_days=0)
    log_file = source.leveldb_dir / "000003.log"
    for i in range(3):
        with open(log_file, "ab") as fh:
            fh.write(b"novo")
        snapshot_source(source, tight, now=T0 + timedelta(days=i), sleep=no_sleep)

    remaining = list_snapshots(cfg.snapshots_dir / source.source_id)
    assert [s.path.name for s in remaining] == ["20261113-120000"]
    # O hardlink não "leva junto" o conteúdo: o blob continua íntegro.
    blob = remaining[0].path / BLOB_DIR_NAME / "1" / "00" / "17"
    assert blob.read_bytes() == (source.blob_dir / "1" / "00" / "17").read_bytes()

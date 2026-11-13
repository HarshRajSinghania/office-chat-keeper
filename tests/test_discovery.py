"""Testes da descoberta de fontes."""

from __future__ import annotations

from office_chat_keeper.discovery import (BLOB_DIR_NAME, LEVELDB_DIR_NAME,
                                          discover_sources, make_source_id)

from conftest import make_source


def test_raiz_inexistente_devolve_lista_vazia(tmp_path):
    assert discover_sources(tmp_path / "nao-existe") == []


def test_acha_fontes_em_subpastas_diferentes(source_root):
    # O cenário real: o Office saiu de 2\0 e passou a usar 2\1.
    make_source(source_root, "aaaa1111_ADAL/2/0")
    make_source(source_root, "aaaa1111_ADAL/2/1", with_blob=False)
    make_source(source_root, "bbbb2222_ADAL/2/0")

    sources = discover_sources(source_root)

    assert len(sources) == 3
    assert len({s.source_id for s in sources}) == 3  # ids distintos
    assert all(s.leveldb_dir.name == LEVELDB_DIR_NAME for s in sources)
    by_blob = {s.rel_path.split("\\")[0] + "/" + s.rel_path.split("\\")[2]: s.blob_dir
               for s in sources}
    assert by_blob["aaaa1111_ADAL/0"].name == BLOB_DIR_NAME
    assert by_blob["aaaa1111_ADAL/1"] is None  # fonte nova ainda sem blobs


def test_ignora_outras_origens_e_local_storage(source_root):
    make_source(source_root)
    sources = discover_sources(source_root)
    assert len(sources) == 1
    assert "outro.site" not in sources[0].rel_path
    assert "Local Storage" not in sources[0].rel_path


def test_id_e_estavel_entre_execucoes(source_root):
    make_source(source_root)
    first = discover_sources(source_root)[0]
    second = discover_sources(source_root)[0]
    assert first.source_id == second.source_id
    assert first.source_id == make_source_id(first.rel_path)
    # Maiúsculas/minúsculas e tipo de barra não mudam o id.
    assert make_source_id("A\\B\\c") == make_source_id("a/b/C")


def test_registra_data_de_criacao_do_banco(source_root):
    make_source(source_root)
    src = discover_sources(source_root)[0]
    assert src.lock_created_at is not None
    assert src.current_created_at is not None
    assert src.db_created_at == src.lock_created_at


def test_nao_descobre_os_proprios_snapshots(tmp_path):
    # Se a pasta de snapshots estiver dentro da raiz varrida, ela é pulada.
    root = tmp_path / "raiz"
    make_source(root, "perfil_ADAL/2/0")
    snapshots = root / "dados" / "snapshots"
    make_source(snapshots, "abc123/20260101-000000")

    assert len(discover_sources(root)) == 2
    assert len(discover_sources(root, exclude=(snapshots,))) == 1


def test_aceita_apontar_direto_para_a_pasta_do_banco(source_root):
    leveldb = make_source(source_root)
    sources = discover_sources(leveldb)
    assert len(sources) == 1
    assert sources[0].leveldb_dir == leveldb.resolve()
    assert sources[0].blob_dir is not None

"""Trava de execução: só um ``run`` por vez na mesma pasta de dados.

Com o agendamento, pode acontecer de você rodar ``run`` à mão no exato momento
em que a tarefa dispara. Dois processos decodificando a mesma conversa de 50 MB
ao mesmo tempo só desperdiçam CPU. A trava é um arquivo (``state\\run.lock``)
que o primeiro processo "tranca" com o sistema operacional; o segundo percebe
e simplesmente desiste. Se o processo morrer, o Windows solta a trava sozinho.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import msvcrt  # só existe no Windows
except ImportError:  # pragma: no cover
    msvcrt = None


@contextmanager
def run_lock(state_dir: Path) -> Iterator[bool]:
    """Entrega True se conseguimos a trava, False se outra execução já a tem."""
    state_dir.mkdir(parents=True, exist_ok=True)
    handle = open(state_dir / "run.lock", "a+b")
    acquired = True
    try:
        if msvcrt is not None:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # não espera: falha na hora
            except OSError:
                acquired = False
        yield acquired
    finally:
        if acquired and msvcrt is not None:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()

"""Proteção da pasta de dados: permissão só para o usuário atual (icacls).

Conceito: ACL
-------------
No Windows (NTFS), cada pasta tem uma **ACL** (lista de controle de acesso):
uma lista de "quem pode fazer o quê". Por padrão as pastas HERDAM as permissões
da pasta-mãe. O ``icacls`` é o programa do próprio Windows para ver e alterar
essa lista.

O que fazemos, em um comando só:

* ``/inheritance:r``  -> remove as permissões herdadas;
* ``/grant:r *<SID>:(OI)(CI)F`` -> dá controle total (F) apenas ao seu usuário,
  valendo para arquivos (OI) e subpastas (CI) criados lá dentro.

Usamos o **SID** (o número de identidade da conta, tipo ``S-1-5-21-...``) em
vez do nome, porque nomes com espaço, acento ou domínio dão problema.

Por quê: os snapshots brutos contêm tokens de conectores. Mesmo estando na sua
pasta de usuário, restringir a ACL é uma camada a mais. Observação honesta: um
administrador da máquina sempre consegue retomar o acesso; isso protege contra
outros usuários comuns e programas rodando com outras contas.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_MARKER_NAME = "acl.json"
# CREATE_NO_WINDOW: não pisca uma janela preta quando rodar agendado.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _system32(exe: str) -> str:
    """Caminho completo do executável do Windows (evita pegar um falso no PATH)."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return str(Path(root) / "System32" / exe)


def current_user() -> tuple[str, str]:
    """(nome ``DOMINIO\\usuario``, SID) do usuário atual, via ``whoami /user``."""
    out = subprocess.run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    ).stdout.strip()
    row = next(csv.reader([out]))
    name, sid = row[0].strip(), row[-1].strip()
    if not sid.startswith("S-1-"):
        raise RuntimeError("SID inesperado na saída do whoami")
    return name, sid


def current_user_sid() -> str:
    """SID do usuário atual."""
    return current_user()[1]


def restrict_to_current_user(path: Path) -> tuple[bool, str]:
    """Deixa ``path`` acessível só ao usuário atual. Devolve (deu certo?, detalhe)."""
    if os.name != "nt":
        return False, "só se aplica ao Windows"
    try:
        sid = current_user_sid()
        proc = subprocess.run(
            [_system32("icacls.exe"), str(path), "/inheritance:r",
             "/grant:r", f"*{sid}:(OI)(CI)F"],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, StopIteration) as exc:
        return False, f"{type(exc).__name__}"
    if proc.returncode != 0:
        return False, f"icacls retornou {proc.returncode}"
    return True, sid


def acl_status(state_dir: Path) -> dict | None:
    """Lê o registro da última vez em que a proteção foi aplicada (ou None)."""
    try:
        return json.loads((state_dir / _MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_protected(data_dir: Path, state_dir: Path) -> tuple[bool, str]:
    """Cria a pasta de dados (se preciso) e aplica a proteção uma única vez.

    Guardamos um pequeno marcador em ``state\\acl.json`` para não chamar o
    icacls a cada 15 minutos. Apague o marcador para forçar a reaplicação.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = acl_status(state_dir)
    if marker and marker.get("ok"):
        return True, "já aplicada"
    ok, detail = restrict_to_current_user(data_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _MARKER_NAME).write_text(json.dumps({
        "ok": ok,
        "detail": detail if not ok else "restrita ao usuário atual",
        "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    if ok:
        log.info("acl: pasta de dados restrita ao usuario atual")
    else:
        log.warning("acl: nao foi possivel restringir a pasta de dados (%s)", detail)
    return ok, detail

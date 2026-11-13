"""Verificação antes de publicar: nada sensível pode entrar no repositório.

Rode antes de qualquer commit ou push:

    python tools/verificar_antes_de_publicar.py

O que procura nos arquivos que iriam para o repositório:

1. **Arquivos proibidos**: bancos, snapshots, logs, wheels, arquivos grandes.
2. **Segredos**: padrões de chaves e tokens (Anthropic, GitHub, AWS, JWT...).
3. **Dados da SUA máquina** ("canários"): seu nome de usuário, o domínio da
   empresa, o caminho da sua pasta de usuário e, se existir um backup local,
   os ids, títulos e identificadores das SUAS conversas reais. Esses valores
   são lidos na hora, do ambiente e do arquivo permanente local; não ficam
   escritos neste script.

Sai com código 1 se encontrar qualquer coisa. Não acessa a rede e não altera
nada. Sem dependências externas.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1_000_000
# Usado só quando ainda não existe repositório git (com git, vale o .gitignore).
SKIP_DIRS = {".git", "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache", ".claude",
             ".restauracao", ".venv", "venv"}
FORBIDDEN_NAMES = re.compile(
    r"(\.leveldb|\.blob)(/|$)|\.(ldb|sqlite3?|db|log|whl|pyc)$|(^|/)MANIFEST-\d+$|"
    r"(^|/)(snapshots|exports|pre-restore|fixture-real|backup-claude-excel|ClaudeOfficeBackup[^/]*)(/|$)",
    re.IGNORECASE)
SECRET_PATTERNS = {
    "chave Anthropic": re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    "token GitHub": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "chave AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{12,}"),
    "chave privada": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "caminho de usuário do Windows": re.compile(r"[A-Za-z]:\\+Users\\+(?!fulano\b|usuario\b|<)[^\\\s\"'`<>%$]+"),
}
# Trechos de teste que parecem segredo de propósito (dados sintéticos).
ALLOWED = ("sk-ant-api03-AbCdEfGhIjKlMnOpQrSt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0")


def candidate_files() -> list[Path]:
    """Arquivos que o git publicaria; sem repositório ainda, varre a pasta."""
    if (ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             cwd=ROOT, capture_output=True, check=True).stdout
        return [ROOT / name for name in out.decode("utf-8").split("\0") if name]
    files = []
    for path in ROOT.rglob("*"):
        parts = set(path.relative_to(ROOT).parts)
        if path.is_file() and not parts & SKIP_DIRS \
                and not any(p.endswith(".egg-info") for p in parts):
            files.append(path)
    return files


def canaries() -> dict[str, str]:
    """Valores desta máquina que não podem aparecer no repositório: valor -> o que é."""
    found: dict[str, str] = {}

    def add(value: str | None, label: str, minimum: int = 5) -> None:
        if value and len(value.strip()) >= minimum:
            found.setdefault(value.strip(), label)

    add(os.environ.get("USERNAME"), "nome de usuário do Windows")
    add(os.environ.get("USERDOMAIN"), "domínio/empresa")
    add(os.environ.get("USERPROFILE"), "pasta de usuário")
    add(os.environ.get("COMPUTERNAME"), "nome do computador")
    for var in ("OneDrive", "OneDriveCommercial"):
        value = os.environ.get(var)
        if value:
            add(Path(value).name, "pasta do OneDrive")

    data_dir = Path(os.environ.get("OFFICE_CHAT_KEEPER_HOME") or Path.home() / "ClaudeOfficeBackup")
    archive = data_dir / "archive" / "conversas.sqlite"
    if archive.is_file():
        db = sqlite3.connect(f"file:{archive.as_posix()}?mode=ro", uri=True)
        try:
            for row in db.execute("SELECT id, title, user_id, org_id, file_id FROM conversations"):
                add(row[0], "id de conversa real")
                add(row[0][:8] if row[0] else None, "início de id de conversa real", 8)
                add(row[1][:24] if row[1] else None, "título de conversa real", 12)
                add(row[2], "userId real")
                add(row[3], "orgId real")
                add(row[4], "fileId real")
        finally:
            db.close()
    snapshots = data_dir / "snapshots"
    if snapshots.is_dir():
        for child in snapshots.iterdir():
            add(child.name, "id de fonte real", 12)
    return found


def main() -> int:
    files = candidate_files()
    known = canaries()
    problems: list[str] = []
    for path in sorted(files):
        rel = path.relative_to(ROOT).as_posix()
        if FORBIDDEN_NAMES.search(rel):
            problems.append(f"{rel}: tipo de arquivo que não pode ser publicado")
            continue
        size = path.stat().st_size
        if size > MAX_BYTES:
            problems.append(f"{rel}: arquivo grande ({size:,} bytes)")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: arquivo binário (confira se deve mesmo ser publicado)")
            continue
        scrubbed = text
        for allowed in ALLOWED:
            scrubbed = scrubbed.replace(allowed, "")
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(scrubbed):
                line = scrubbed.count("\n", 0, match.start()) + 1
                problems.append(f"{rel}:{line}: {label}")
        lowered = text.lower()
        for value, label in known.items():
            position = lowered.find(value.lower())
            if position >= 0:
                line = text.count("\n", 0, position) + 1
                # O valor em si não é impresso por inteiro.
                problems.append(f"{rel}:{line}: {label} ({value[:3]}…)")

    print(f"arquivos verificados: {len(files)}")
    print(f"valores desta máquina procurados: {len(known)} "
          f"({'inclui dados do backup local' if any('real' in v for v in known.values()) else 'sem backup local'})")
    if problems:
        print(f"\nPROBLEMAS ({len(problems)}):")
        for problem in problems:
            print("  -", problem)
        print("\nNão publique antes de resolver.")
        return 1
    print("\nNada sensível encontrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

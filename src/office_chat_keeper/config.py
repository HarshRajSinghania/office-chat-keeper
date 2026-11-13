"""Configuração: onde LER (origem) e onde GRAVAR (destino).

Conceito central do projeto: existem dois lados que nunca se misturam.

* ORIGEM  -> a pasta do Office (``Wef\\webview2``). Só leitura, sempre.
* DESTINO -> a pasta de dados da ferramenta (``%USERPROFILE%\\ClaudeOfficeBackup``).
  É o único lugar onde este programa cria, altera ou apaga arquivos.

As funções de segurança deste módulo existem para garantir essa separação
mesmo que alguém passe um caminho errado na linha de comando.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Variáveis de ambiente que permitem trocar as pastas sem editar código.
ENV_DATA_DIR = "OFFICE_CHAT_KEEPER_HOME"
ENV_SOURCE_ROOT = "OFFICE_CHAT_KEEPER_SOURCE_ROOT"


class ConfigError(Exception):
    """Configuração perigosa ou inválida (ex.: destino dentro da pasta do Office)."""


@dataclass(frozen=True)
class Config:
    """Todas as opções da ferramenta em um só objeto.

    ``frozen=True`` torna o objeto imutável: depois de criado ninguém troca a
    pasta de destino "no meio do caminho" por engano.
    """

    data_dir: Path
    source_root: Path
    # Retenção dos snapshots (ver snapshot.choose_snapshots_to_delete).
    keep_last: int = 10
    keep_daily_days: int = 30
    # Arquivos em uso (Excel aberto): quantas vezes tentar de novo.
    copy_attempts: int = 4  # tentativas do snapshot inteiro
    file_attempts: int = 5  # tentativas por arquivo bloqueado
    retry_delay: float = 2.0  # segundos entre tentativas

    @property
    def snapshots_dir(self) -> Path:
        """Cópias brutas do LevelDB. SENSÍVEL: contém tokens de conectores."""
        return self.data_dir / "snapshots"

    @property
    def archive_path(self) -> Path:
        """O arquivo permanente (SQLite, só de acréscimo). É a memória do backup."""
        return self.data_dir / "archive" / "conversas.sqlite"

    @property
    def exports_dir(self) -> Path:
        """Markdown/JSON legíveis. Têm o conteúdo das conversas, mas não tokens."""
        return self.data_dir / "exports"

    @property
    def reports_dir(self) -> Path:
        """Relatórios de verificação e planos de restauração (simulações)."""
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"


def default_data_dir() -> Path:
    """Destino padrão: ``%USERPROFILE%\\ClaudeOfficeBackup`` (fora do OneDrive)."""
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()
    return Path.home() / "ClaudeOfficeBackup"


def default_source_root() -> Path:
    """Origem padrão: onde o Office guarda o armazenamento dos suplementos.

    Repare que paramos em ``webview2``: as subpastas seguintes
    (``<id>_ADAL\\2\\0``, ``2\\1``...) mudam com as atualizações do Office,
    então são descobertas em tempo de execução (ver discovery.py).
    """
    env = os.environ.get(ENV_SOURCE_ROOT)
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Microsoft" / "Office" / "16.0" / "Wef" / "webview2"


def load_config(data_dir: str | os.PathLike | None = None,
                source_root: str | os.PathLike | None = None) -> Config:
    """Monta a configuração. Prioridade: argumento > variável de ambiente > padrão."""
    return Config(
        data_dir=Path(data_dir).expanduser() if data_dir else default_data_dir(),
        source_root=Path(source_root).expanduser() if source_root else default_source_root(),
    )


def is_inside(child: Path, parent: Path) -> bool:
    """True se ``child`` é a própria pasta ``parent`` ou está dentro dela."""
    child = Path(child).resolve()
    parent = Path(parent).resolve()
    return child == parent or parent in child.parents


def check_destination_is_safe(cfg: Config) -> None:
    """Recusa gravar dentro da área do Office.

    Regra obrigatória do projeto: nunca escrever em ``Wef``/``webview2``.
    Se o destino estivesse lá dentro, o próprio backup alteraria a origem.
    """
    if is_inside(cfg.data_dir, cfg.source_root):
        raise ConfigError(
            f"A pasta de dados ({cfg.data_dir}) está dentro da pasta de origem "
            f"({cfg.source_root}). A origem é somente leitura; escolha outro destino."
        )
    parts = [p.lower() for p in cfg.data_dir.resolve().parts]
    if "wef" in parts and "webview2" in parts:
        raise ConfigError(
            f"A pasta de dados ({cfg.data_dir}) fica dentro de Wef\\webview2 do Office. "
            "Escolha um destino fora do armazenamento do Office."
        )


def onedrive_root_for(path: Path) -> Path | None:
    """Se ``path`` estiver em uma pasta sincronizada pelo OneDrive, devolve a raiz.

    Serve para AVISAR: snapshots têm tokens e as conversas têm dados de
    trabalho; sincronizá-los pode ferir a política da empresa.
    """
    resolved = Path(path).resolve()
    for var in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.environ.get(var)
        if value and is_inside(resolved, Path(value)):
            return Path(value)
    # Reserva: o nome padrão da pasta começa com "OneDrive" ("OneDrive - Empresa").
    for candidate in (resolved, *resolved.parents):
        if candidate.name.lower().startswith("onedrive"):
            return candidate
    return None

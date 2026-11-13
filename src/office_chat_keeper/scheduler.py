"""Fase 5 - Agendamento: rodar o backup sozinho, pelo Agendador de Tarefas.

Conceitos
---------
* **Agendador de Tarefas** (Task Scheduler) é o serviço do Windows que executa
  programas em horários ou eventos definidos. Cada "tarefa" tem:

  - **gatilhos** (triggers): QUANDO rodar. Usamos dois: ao fazer logon e a
    cada N minutos;
  - **ação**: O QUE rodar (um programa e seus argumentos);
  - **principal**: COM QUAL conta e privilégio. Aqui, a sua própria conta,
    privilégio comum (``LeastPrivilege``) e só com você logado
    (``InteractiveToken``). Por isso NÃO exige administrador nem senha;
  - **configurações**: prioridade, o que fazer se já houver uma execução em
    andamento, tempo máximo etc.

* Uma tarefa pode ser descrita em **XML**. O ``schtasks.exe`` (ferramenta de
  linha de comando do próprio Windows) cria a tarefa a partir desse XML.

Por que ``schtasks`` e não um script PowerShell
-----------------------------------------------
Em muitas empresas a política de execução do PowerShell (``AllSigned``) só
permite rodar scripts assinados digitalmente pela TI. Um script nosso seria
bloqueado, e contornar essa política não é opção. O ``schtasks.exe`` é um
executável do Windows, não um script, e cria exatamente a mesma tarefa.

"Sem janela" e "pouca CPU"
--------------------------
* **Sem janela:** a ação usa ``pythonw.exe``, a versão do Python sem console.
  Nada pisca na tela a cada execução.
* **Pouca CPU:** prioridade 7 (abaixo do normal): o backup cede a vez para o
  que você estiver usando. E o ``run`` só copia e lê quando algo mudou.
* **Uma por vez:** ``IgnoreNew`` faz o Windows pular um disparo se o anterior
  ainda estiver rodando.

O XML gerado fica salvo em ``state\\task.xml`` para você conferir.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config
from .protect import _NO_WINDOW, _system32, current_user

TASK_PREFIX = "office-chat-keeper"
DEFAULT_INTERVAL = 15
MIN_INTERVAL, MAX_INTERVAL = 5, 1440
_XML_NAME = "task.xml"
_META_NAME = "task.json"

# Códigos comuns do campo "Último resultado" de uma tarefa.
RESULT_HINTS = {
    "0": "sucesso",
    "1": "terminou com erro (veja o log)",
    "267009": "em execução agora",
    "267011": "ainda não rodou",
    "267014": "encerrada pelo usuário ou pelo limite de tempo",
}


class SchedulerError(Exception):
    pass


def default_task_name() -> str:
    """Nome da tarefa. Inclui o usuário porque os nomes são globais na máquina."""
    user = re.sub(r"[^A-Za-z0-9._-]+", "_", os.environ.get("USERNAME", "usuario"))
    return f"{TASK_PREFIX}-{user}"


def iso_duration(minutes: int) -> str:
    """15 -> 'PT15M', 90 -> 'PT1H30M' (formato de duração ISO 8601 usado no XML)."""
    hours, rest = divmod(int(minutes), 60)
    return "PT" + (f"{hours}H" if hours else "") + (f"{rest}M" if rest or not hours else "")


def background_python() -> tuple[Path, bool]:
    """O Python que a tarefa vai usar. Devolve (caminho, é sem janela?).

    ``pythonw.exe`` mora ao lado do ``python.exe`` (inclusive em ambientes
    virtuais). Se não existir, usamos o próprio Python - funciona, mas uma
    janela pode piscar.
    """
    current = Path(sys.executable)
    windowless = current.with_name("pythonw.exe")
    if windowless.is_file():
        return windowless, True
    return current, False


def build_run_arguments(cfg: Config, *, include_source_root: bool) -> str:
    """Argumentos do comando agendado: ``-P -m office_chat_keeper ... run``.

    ``-P`` (Python 3.11+) impede que a pasta atual entre no caminho de busca
    de módulos: a tarefa só executa o pacote instalado. A pasta de dados vai
    sempre explícita, para a tarefa não depender de variáveis de ambiente.
    """
    args = ["-P", "-m", "office_chat_keeper", "--data-dir", str(cfg.data_dir.resolve())]
    if include_source_root:
        args += ["--source-root", str(cfg.source_root.resolve())]
    args.append("run")
    return subprocess.list2cmdline(args)  # cuida das aspas em caminhos com espaço


def build_task_xml(*, command: str, arguments: str, working_dir: str, user: str,
                   interval_minutes: int, start: datetime, logon_trigger: bool = True) -> str:
    """Monta a definição da tarefa no formato XML do Agendador (esquema 1.2)."""
    logon = f"""
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>""" if logon_trigger else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>office-chat-keeper (ferramenta não oficial): backup local do histórico dos suplementos do Claude no Office. Para remover: office-chat-keeper uninstall-task</Description>
  </RegistrationInfo>
  <Triggers>{logon}
    <TimeTrigger>
      <Repetition>
        <Interval>{iso_duration(interval_minutes)}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    """Chama o schtasks.exe do Windows, sem abrir janela."""
    if os.name != "nt":
        raise SchedulerError("O agendamento só existe no Windows.")
    proc = subprocess.run([_system32("schtasks.exe"), *args], capture_output=True,
                          creationflags=_NO_WINDOW)
    # A saída vem na página de código do console (OEM), não em UTF-8.
    proc.stdout = proc.stdout.decode("oem", errors="replace")
    proc.stderr = proc.stderr.decode("oem", errors="replace")
    return proc


@dataclass
class InstallResult:
    task_name: str
    command: str
    arguments: str
    interval_minutes: int
    logon_trigger: bool
    windowless: bool
    xml_path: Path
    started: bool


def install_task(cfg: Config, *, interval_minutes: int = DEFAULT_INTERVAL,
                 task_name: str | None = None, include_source_root: bool = False,
                 run_now: bool = True) -> InstallResult:
    """Cria (ou substitui) a tarefa agendada do usuário atual."""
    if not MIN_INTERVAL <= interval_minutes <= MAX_INTERVAL:
        raise SchedulerError(f"O intervalo deve ficar entre {MIN_INTERVAL} e {MAX_INTERVAL} minutos.")
    name = task_name or default_task_name()
    python, windowless = background_python()
    arguments = build_run_arguments(cfg, include_source_root=include_source_root)
    user, _sid = current_user()
    start = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=2)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    xml_path = cfg.state_dir / _XML_NAME

    def create(with_logon: bool) -> subprocess.CompletedProcess:
        xml = build_task_xml(command=str(python), arguments=arguments,
                             working_dir=str(Path.home()), user=user,
                             interval_minutes=interval_minutes, start=start,
                             logon_trigger=with_logon)
        xml_path.write_bytes(xml.encode("utf-16"))  # o XML declara UTF-16 (com BOM)
        return _schtasks("/Create", "/TN", name, "/XML", str(xml_path), "/F")

    logon = True
    proc = create(True)
    if proc.returncode != 0:
        # Algumas políticas não deixam usuário comum criar gatilho de logon.
        # Sem ele a tarefa ainda roda a cada N minutos (e logo após o logon,
        # graças a StartWhenAvailable).
        first_error = (proc.stderr or proc.stdout).strip()
        logon = False
        proc = create(False)
        if proc.returncode != 0:
            raise SchedulerError(
                "O Windows recusou a criação da tarefa: "
                + ((proc.stderr or proc.stdout).strip() or first_error or "erro desconhecido")
                + " Se a mensagem for de acesso negado, a política da empresa pode impedir "
                  "tarefas agendadas por usuário; peça orientação à TI.")

    started = False
    if run_now:
        started = _schtasks("/Run", "/TN", name).returncode == 0
    (cfg.state_dir / _META_NAME).write_text(json.dumps({
        "task_name": name, "interval_minutes": interval_minutes, "logon_trigger": logon,
        "command": str(python), "arguments": arguments,
        "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return InstallResult(name, str(python), arguments, interval_minutes, logon, windowless,
                         xml_path, started)


def uninstall_task(task_name: str | None = None, cfg: Config | None = None) -> bool:
    """Remove a tarefa. Devolve False se ela não existia. Não apaga nenhum dado."""
    name = task_name or default_task_name()
    if _schtasks("/Query", "/TN", name).returncode != 0:
        return False
    proc = _schtasks("/Delete", "/TN", name, "/F")
    if proc.returncode != 0:
        raise SchedulerError("Não consegui remover a tarefa: "
                             + (proc.stderr or proc.stdout).strip())
    if cfg is not None:
        (cfg.state_dir / _META_NAME).unlink(missing_ok=True)
    return True


@dataclass
class TaskInfo:
    task_name: str
    status: str
    next_run: str
    last_run: str
    last_result: str
    interval_minutes: int | None = None

    @property
    def result_hint(self) -> str:
        return RESULT_HINTS.get(self.last_result, f"código {self.last_result}")


def installed_task_name(cfg: Config) -> str:
    """O nome usado na instalação (se houver registro), senão o nome padrão."""
    try:
        meta = json.loads((cfg.state_dir / _META_NAME).read_text(encoding="utf-8"))
        return meta.get("task_name") or default_task_name()
    except (OSError, ValueError):
        return default_task_name()


def query_task(cfg: Config, task_name: str | None = None) -> TaskInfo | None:
    """Estado da tarefa segundo o Windows, ou None se ela não existe."""
    if os.name != "nt":
        return None
    name = task_name or installed_task_name(cfg)
    proc = _schtasks("/Query", "/TN", name, "/FO", "CSV", "/V", "/NH")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    # Com /V o CSV tem colunas em ordem fixa (os TÍTULOS mudam com o idioma do
    # Windows, por isso usamos a posição): 2 = próxima execução, 3 = status,
    # 5 = última execução, 6 = último resultado.
    row = next(csv.reader(proc.stdout.strip().splitlines()), None)
    if not row or len(row) < 7:
        return None
    interval = None
    try:
        meta = json.loads((cfg.state_dir / _META_NAME).read_text(encoding="utf-8"))
        if meta.get("task_name") == name:
            interval = meta.get("interval_minutes")
    except (OSError, ValueError):
        pass
    return TaskInfo(name, row[3], row[2], row[5], row[6], interval)

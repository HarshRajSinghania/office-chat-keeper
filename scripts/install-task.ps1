<#
.SYNOPSIS
    Liga o backup automatico do office-chat-keeper (alternativa em PowerShell ao
    comando "office-chat-keeper install-task").

.DESCRIPTION
    Cria, na SUA conta e sem administrador, uma tarefa no Agendador de Tarefas
    do Windows que roda "office-chat-keeper run" ao fazer logon e a cada N
    minutos, sem janela e com prioridade baixa.

    Faz o mesmo que "office-chat-keeper install-task", mas usando os cmdlets do
    modulo ScheduledTasks em vez do schtasks.exe. Use um ou outro, nao os dois.

    Conceitos (cada cmdlet abaixo monta uma parte da tarefa):
      - Acao      (New-ScheduledTaskAction)      : O QUE rodar.
      - Gatilhos  (New-ScheduledTaskTrigger)     : QUANDO rodar.
      - Principal (New-ScheduledTaskPrincipal)   : COM QUAL conta e privilegio.
      - Ajustes   (New-ScheduledTaskSettingsSet) : COMO rodar (prioridade etc.).
      - Register-ScheduledTask junta tudo e grava a tarefa.

    ATENCAO: este script NAO foi executado na maquina onde a ferramenta foi
    desenvolvida, porque a politica de execucao de la (AllSigned) bloqueia
    scripts sem assinatura. Ele foi conferido apenas quanto a sintaxe e aos
    nomes de parametros. Teste primeiro com -TaskName de teste.

    Nada aqui acessa a rede, e nada e baixado.

.PARAMETER IntervalMinutes
    Minutos entre execucoes (5 a 1440). Padrao: 15.

.PARAMETER TaskName
    Nome da tarefa. Padrao: office-chat-keeper-<usuario>.

.PARAMETER DataDir
    Pasta de dados do backup. Padrao: %USERPROFILE%\ClaudeOfficeBackup.

.PARAMETER PythonPath
    Caminho do python.exe em que o office-chat-keeper esta instalado.
    Padrao: o "python" encontrado no PATH.

.PARAMETER NoRun
    Nao dispara a primeira execucao logo apos criar a tarefa.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -IntervalMinutes 30 -TaskName office-chat-keeper-teste -NoRun
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateRange(5, 1440)]
    [int]$IntervalMinutes = 15,

    [string]$TaskName = ('office-chat-keeper-' + ($env:USERNAME -replace '[^A-Za-z0-9._-]+', '_')),

    [string]$DataDir = (Join-Path $env:USERPROFILE 'ClaudeOfficeBackup'),

    [string]$PythonPath,

    [switch]$NoRun
)

$ErrorActionPreference = 'Stop'

function Stop-WithMessage([string]$Message) {
    Write-Host "ERRO: $Message" -ForegroundColor Red
    exit 1
}

# --- 1) Qual Python usar -----------------------------------------------------
if (-not $PythonPath) {
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $found) {
        Stop-WithMessage 'Python nao encontrado no PATH. Informe -PythonPath <caminho do python.exe>.'
    }
    # Pergunta ao proprio Python onde ele mora (resolve atalhos e ambientes virtuais).
    $PythonPath = (& $found.Source -c 'import sys; print(sys.executable)' | Select-Object -First 1).Trim()
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    Stop-WithMessage "Python nao encontrado em: $PythonPath"
}

# O pacote precisa estar instalado NESSE Python (o codigo de saida diz se esta).
& $PythonPath -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("office_chat_keeper") else 1)'
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "O pacote office-chat-keeper nao esta instalado em $PythonPath."
}

# pythonw.exe = Python sem console: nenhuma janela pisca a cada execucao.
$pythonw = Join-Path (Split-Path -Parent $PythonPath) 'pythonw.exe'
$windowless = Test-Path -LiteralPath $pythonw
if (-not $windowless) {
    $pythonw = $PythonPath
}

# --- 2) As quatro partes da tarefa -------------------------------------------
$dataDirFull = [System.IO.Path]::GetFullPath($DataDir)
# -P (Python 3.11+): a pasta atual nao entra no caminho de busca de modulos.
$arguments = '-P -m office_chat_keeper --data-dir "{0}" run' -f $dataDirFull
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# ACAO: o que rodar.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $env:USERPROFILE

# GATILHOS: ao fazer logon (com 1 minuto de folga) e a cada N minutos, sem data para acabar.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$logonTrigger.Delay = 'PT1M'
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# PRINCIPAL: a sua conta, so com voce logado (Interactive = nenhuma senha e guardada),
# sem elevacao (Limited = nao pede administrador).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# AJUSTES: roda tambem na bateria; recupera um horario perdido (StartWhenAvailable);
# uma execucao por vez (IgnoreNew); no maximo 30 minutos; prioridade 7 (abaixo do normal).
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -Priority 7

$description = 'office-chat-keeper (ferramenta nao oficial): backup local do historico dos ' +
    'suplementos do Claude no Office. Para remover: scripts\uninstall-task.ps1'

# --- 3) Gravar a tarefa --------------------------------------------------------
$withLogon = $true
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logonTrigger, $repeatTrigger `
        -Principal $principal -Settings $settings -Description $description -Force | Out-Null
}
catch {
    # Algumas politicas nao deixam usuario comum criar gatilho de logon.
    # Sem ele a tarefa ainda roda pelo intervalo.
    $withLogon = $false
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $repeatTrigger `
            -Principal $principal -Settings $settings -Description $description -Force | Out-Null
    }
    catch {
        Stop-WithMessage ("O Windows recusou a criacao da tarefa: " + $_.Exception.Message)
    }
}

# --- 4) Resumo -----------------------------------------------------------------
Write-Host "Backup automatico ligado. Tarefa: $TaskName"
if ($withLogon) {
    Write-Host "    quando:  ao fazer logon e a cada $IntervalMinutes minutos"
}
else {
    Write-Host "    quando:  a cada $IntervalMinutes minutos"
    Write-Host '    AVISO: o Windows nao aceitou o gatilho de logon; a tarefa roda so pelo intervalo.'
}
Write-Host "    comando: $pythonw $arguments"
Write-Host '    conta:   a sua, sem privilegio de administrador, so com voce logado'
if (-not $windowless) {
    Write-Host '    AVISO: pythonw.exe nao foi encontrado; uma janela pode piscar a cada execucao.'
}

if (-not $NoRun) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'A primeira execucao comecou agora, em segundo plano.'
}
Write-Host 'Para desligar: powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1'

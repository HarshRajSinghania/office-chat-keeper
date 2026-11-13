<#
.SYNOPSIS
    Desliga o backup automatico do office-chat-keeper (alternativa em PowerShell
    ao comando "office-chat-keeper uninstall-task").

.DESCRIPTION
    Remove a tarefa do Agendador de Tarefas. NAO apaga nenhum dado: snapshots,
    arquivo permanente e exportacoes continuam na pasta de dados.

    ATENCAO: este script NAO foi executado na maquina onde a ferramenta foi
    desenvolvida (politica de execucao AllSigned). Foi conferido apenas quanto
    a sintaxe e aos nomes de parametros.

.PARAMETER TaskName
    Nome da tarefa. Padrao: office-chat-keeper-<usuario>.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$TaskName = ('office-chat-keeper-' + ($env:USERNAME -replace '[^A-Za-z0-9._-]+', '_'))
)

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Nao existe tarefa agendada com o nome $TaskName. Nada a fazer."
    exit 0
}

try {
    # -Confirm:$false: nao abre a pergunta "tem certeza?" (o script ja e a confirmacao).
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
catch {
    Write-Host ("ERRO: nao consegui remover a tarefa: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

Write-Host "Backup automatico desligado (tarefa $TaskName removida)."
Write-Host 'Seus dados continuam na pasta de dados do backup.'

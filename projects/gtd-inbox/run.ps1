param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AgentArgs
)
$ErrorActionPreference = 'Stop'
$taskPythonPath = $env:GTD_AGENT_PYTHON
$taskPythonArgs = @('-B')
if (-not $taskPythonPath) {
    $taskLauncher = Get-Command py -ErrorAction SilentlyContinue
    $taskPython = Get-Command python -ErrorAction SilentlyContinue
    if ($taskLauncher) {
        $taskPythonPath = $taskLauncher.Source
        $taskPythonArgs = @('-3', '-B')
    } elseif ($taskPython) {
        $taskPythonPath = $taskPython.Source
    } else {
        throw 'Install Python 3.10+ or set GTD_AGENT_PYTHON.'
    }
}
& $taskPythonPath @taskPythonArgs (Join-Path $PSScriptRoot 'run.py') @AgentArgs
exit $LASTEXITCODE

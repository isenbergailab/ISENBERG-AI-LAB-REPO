param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Task,
    [int]$Steps = 25,
    [switch]$Headless
)

& py -3 (Join-Path $PSScriptRoot "scripts\browse.py") --url $Url --task $Task --steps $Steps @(
    if ($Headless) { "--headless" }
)

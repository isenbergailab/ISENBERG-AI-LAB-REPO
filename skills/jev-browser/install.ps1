# Copies this skill to ~/.agents/skills/jev-browser and registers a `jev` shim.
param([switch]$NoShim)

$ErrorActionPreference = "Stop"

$src = $PSScriptRoot
$dest = Join-Path $env:USERPROFILE ".agents\skills\jev-browser"

if ((Resolve-Path $src).Path -eq (Resolve-Path $dest -ErrorAction SilentlyContinue)) {
    Write-Host "Skill is already installed at $dest" -ForegroundColor Yellow
    $installDir = $src
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item -Recurse -Force -Path $src -Destination $dest -Exclude ".git"
    # Remove stale bytecode from the destination if any
    Remove-Item -Recurse -Force -Path (Join-Path $dest "scripts\__pycache__") -ErrorAction SilentlyContinue
    $installDir = $dest
    Write-Host "Copied skill to $dest" -ForegroundColor Green
}

# Prerequisite check
foreach ($pkg in @("typesafe-sdk", "playwright")) {
    py -3 -c "import $pkg" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Missing Python package: $pkg" -ForegroundColor Yellow
        Write-Host "  pip install $pkg"
    }
}
py -3 -c "import playwright; import playwright.sync_api" 2>$null
if ($LASTEXITCODE -eq 0) {
    py -3 -c "from playwright.sync_api import sync_playwright; sync_playwright().start().chromium.executable_path" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "Chromium browser not installed yet:" -ForegroundColor Yellow; Write-Host "  playwright install chromium" }
}
if (-not $env:TYPESAFE_API_KEY) {
    Write-Host "TYPESAFE_API_KEY not set in this session." -ForegroundColor Yellow
    Write-Host "  Get a key at https://console.typesafe.ai/ then:  setx TYPESAFE_API_KEY `"your-key`""
}

if ($NoShim) {
    Write-Host "Done. Run:  py -3 `"$installDir\scripts\browse.py`" --url <url> --task <task>" -ForegroundColor Green
    return
}

# Register the one-word `jev` launcher in a separate shim file, dot-sourced from the profile.
$shimPath = Join-Path (Split-Path $PROFILE) "jev.ps1"
$shimLine = "function jev { & `"$installDir\jev.ps1`" @args }"
$shimContent = "# --- Jev-Browser ---`r`n$shimLine`r`n# -------------------"
Set-Content -LiteralPath $shimPath -Value $shimContent

if (Test-Path $PROFILE) {
    $profileText = Get-Content $PROFILE -Raw
    if ($profileText -notmatch "jev\.ps1") {
        Add-Content -LiteralPath $PROFILE -Value "`r`n# Jev-Browser: source the one-word jev launcher`r`nif (Test-Path `"$shimPath`") { . `"$shimPath`" }"
        Write-Host "Added dot-source line to $PROFILE" -ForegroundColor Green
    }
} else {
    Set-Content -LiteralPath $PROFILE -Value "# Jev-Browser: source the one-word jev launcher`r`nif (Test-Path `"$shimPath`") { . `"$shimPath`" }"
    Write-Host "Created profile at $PROFILE" -ForegroundColor Green
}

Write-Host "`nDone! Open a new terminal (or run: . `$PROFILE), then:" -ForegroundColor Green
Write-Host "  jev -Url `"https://example.com`" -Task `"do something`"" -ForegroundColor White

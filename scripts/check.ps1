# Lint and tests. Run before every commit; the pre-commit hook runs this too.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Output "No .venv found. Setup steps are in CLAUDE.md."
    exit 1
}

Set-Location $root

& $python -m ruff check backend tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pytest
exit $LASTEXITCODE

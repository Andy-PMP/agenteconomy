# Lint, tests, and the independent JS check of the chain format. Run before every commit; the pre-commit hook runs this too.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Output "No .venv found. Setup steps are in CLAUDE.md."
    exit 1
}

Set-Location $root

& $python -m ruff check backend tests scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

node scripts\verify_vectors.mjs testdata\chain_vectors_v1.json
exit $LASTEXITCODE

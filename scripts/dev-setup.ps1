[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function Stop-Setup([string]$message) {
    Write-Host $message
    throw 'Local setup stopped'
}

function Test-Docker([string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell can turn redirected native stderr into an error.
        $ErrorActionPreference = 'Continue'
        & docker @Arguments *> $null
        return $LASTEXITCODE -eq 0
    }
    finally { $ErrorActionPreference = $previousPreference }
}

$previousEncoding = $OutputEncoding
$OutputEncoding = [Text.UTF8Encoding]::new($false)
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        Stop-Setup 'Create the virtualenv: py -3.14 -m venv .venv; then .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt'
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-Setup 'Install Git for Windows and retry.' }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Stop-Setup 'Install Docker Desktop and retry.' }
    if (-not (Test-Docker -Arguments @('version'))) { Stop-Setup 'Start Docker Desktop and retry.' }

    # This command verifies ignored/untracked paths before reading or writing secrets.
    $inspection = & $python -m app.dev.setup inspect
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Local configuration inspection failed.' }
    $plan = $inspection | ConvertFrom-Json
    $confirmed = $false
    if (@($plan.confirm_settings).Count -gt 0) {
        Write-Host ('Replace existing values with local defaults for: ' + ($plan.confirm_settings -join ', '))
        $confirmed = (Read-Host 'Type yes to confirm') -ceq 'yes'
        if (-not $confirmed) { Stop-Setup 'No configuration changes made.' }
    }
    $inputValues = @{ confirm_local = $confirmed }
    if ($plan.need_client_id) {
        $inputValues.OIDC_CLIENT_ID = Read-Host 'EF development OIDC client ID'
    }
    $secret = $null
    $pointer = [IntPtr]::Zero
    try {
        if ($plan.need_client_secret) {
            $secret = Read-Host 'EF development OIDC client secret' -AsSecureString
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
            $inputValues.OIDC_CLIENT_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        }
        # Payload travels through stdin, never command arguments or shell history.
        $inputValues | ConvertTo-Json -Compress | & $python -m app.dev.setup configure
        if ($LASTEXITCODE -ne 0) { Stop-Setup 'Local configuration was not saved successfully.' }
    }
    finally {
        $inputValues.Clear()
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($null -ne $secret) { $secret.Dispose() }
    }
    & $python -m app.dev.setup verify-local
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Effective configuration is not safe for local setup.' }
    if (-not (Test-Docker -Arguments @('network', 'inspect', 'creators-dev'))) {
        if (-not (Test-Docker -Arguments @('network', 'create', 'creators-dev'))) { Stop-Setup 'Could not create local Docker network.' }
    }
    & docker compose up -d db s3
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Local dependencies could not be started.' }
    $databaseReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-Docker -Arguments @('compose', 'exec', '-T', 'db', 'pg_isready', '-U', 'creators', '-d', 'creators')) { $databaseReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $databaseReady) { Stop-Setup 'Local PostgreSQL did not become ready.' }
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Migration failed.' }
    & $python -m app.dev.event --interactive
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Event setup needs operator attention; no real Event was overwritten.' }
    & $python -m app.dev.check
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Local acceptance configuration is NOT READY; resolve the reported setting names.' }
    Write-Host 'Local dependencies ready. Configuration valid.'
    Write-Host 'Start from the repository root:'
    Write-Host '  .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log'
    Write-Host 'Then open http://127.0.0.1:8000/auth/login'
}
catch {
    # Do not echo unexpected exceptions: they can contain command/input values.
    Write-Host 'Setup stopped. Review the safe diagnostics above and check local prerequisites.'
    exit 1
}
finally {
    $OutputEncoding = $previousEncoding
    Pop-Location
}

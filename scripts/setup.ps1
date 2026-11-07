# Cloud operations require OpenSSH, Terraform and the Yandex Cloud CLI.
$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Install Python and add it to PATH before running setup.'
    }
    python -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)'
    if ($LASTEXITCODE) { throw 'Python 3.12 through 3.14 is required.' }
    $poetryVersion = '2.4.1'
    $existing = Get-Command poetry -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $installed = if ($existing) { & $existing.Source --version }
    if ($installed -eq "Poetry (version $poetryVersion)") {
        $poetry = $existing.Source
    } else {
        $poetry = Join-Path $PWD '.tools/poetry/Scripts/poetry.exe'
        if (-not (Test-Path -LiteralPath '.tools/poetry/Scripts/python.exe')) {
            python -m venv .tools/poetry
            if ($LASTEXITCODE) { throw 'Cannot create the Poetry tool environment.' }
        }
        $installed = if (Test-Path -LiteralPath $poetry) { & $poetry --version }
        if ($installed -ne "Poetry (version $poetryVersion)") {
            & .\.tools\poetry\Scripts\python.exe -m pip install "poetry==$poetryVersion"
            if ($LASTEXITCODE) { throw 'Cannot install the pinned Poetry version.' }
        }
    }
    $old = $env:POETRY_VIRTUALENVS_IN_PROJECT
    $oldCache = $env:POETRY_CACHE_DIR
    $oldKeyring = $env:POETRY_KEYRING_ENABLED
    $env:POETRY_VIRTUALENVS_IN_PROJECT = 'true'
    $env:POETRY_CACHE_DIR = Join-Path $PWD '.tools/poetry-cache'
    $env:POETRY_KEYRING_ENABLED = 'false'
    try {
        if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
            python -m venv --without-pip .venv
            if ($LASTEXITCODE) { throw 'Cannot create the project environment.' }
        }
        $interpreter = python -c 'import sys; print(sys.executable)'
        & $poetry env use $interpreter
        if ($LASTEXITCODE) { throw 'Cannot select the project interpreter.' }
        $environment = & $poetry env info --path
        if ($LASTEXITCODE -or $environment -ne (Join-Path $PWD '.venv')) {
            throw 'Poetry did not select the project .venv.'
        }
        & $poetry check --lock
        if ($LASTEXITCODE) { throw 'The Poetry lock is missing or out of sync.' }
        & $poetry sync
        if ($LASTEXITCODE) { throw 'Cannot synchronize the project environment.' }
    } finally {
        $env:POETRY_VIRTUALENVS_IN_PROJECT = $old
        $env:POETRY_CACHE_DIR = $oldCache
        $env:POETRY_KEYRING_ENABLED = $oldKeyring
    }
} finally {
    Pop-Location
}

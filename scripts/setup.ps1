$ErrorActionPreference = 'Stop'

function Install-MissingTool {
    param([string]$Executable, [string]$Url, [string]$Sha256)

    $destination = Join-Path $PWD ".tools/bin/$Executable"
    if ((Get-Command $Executable -CommandType Application -ErrorAction SilentlyContinue) -or
        (Test-Path -LiteralPath $destination -PathType Leaf)) {
        return
    }

    $bin = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    $staging = Join-Path $bin ([guid]::NewGuid().ToString())
    $archive = "$staging.zip"
    $candidate = "$staging.tmp"
    try {
        Write-Host "Downloading $Executable"
        $curl = Get-Command curl.exe -CommandType Application -ErrorAction SilentlyContinue
        if ($curl) {
            & $curl.Source --fail --location --silent --show-error --retry 2 --max-time 180 --output $archive $Url
            if ($LASTEXITCODE) { throw "Cannot download $Executable." }
        } else {
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $archive -TimeoutSec 180
        }
        if ($Sha256 -and (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Sha256) {
            throw "SHA-256 mismatch for $Executable."
        }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
        try {
            $entry = $zip.GetEntry($Executable)
            if (-not $entry) { throw "The archive does not contain $Executable." }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $candidate)
        } finally {
            $zip.Dispose()
        }
        [System.IO.File]::Move($candidate, $destination)
    } finally {
        foreach ($file in @($archive, $candidate)) {
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
        }
    }
}

Push-Location (Join-Path $PSScriptRoot '..')
try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Install Python and add it to PATH before running setup.'
    }
    python -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)'
    if ($LASTEXITCODE) { throw 'Python 3.12 through 3.14 is required.' }

    $terraformVersion = '1.13.5'
    if ([Environment]::Is64BitOperatingSystem) {
        $architecture = 'amd64'
        $terraformHash = '73f97943c93f268ae2c645b2a737d552175aa64d00a5c8b8f5ccc5831c033c8a'
    } else {
        $architecture = '386'
        $terraformHash = 'dd530788dff2c132bab59e29c00c81b8d2a9aa4b0c9832addd0777bd07b4b6cf'
    }
    Install-MissingTool terraform.exe "https://releases.hashicorp.com/terraform/$terraformVersion/terraform_${terraformVersion}_windows_${architecture}.zip" $terraformHash
    Install-MissingTool yc.exe "https://storage.yandexcloud.net/yandexcloud-yc/release/yc_windows_${architecture}.zip"

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

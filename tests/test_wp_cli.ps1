$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../scripts/wp-cli.ps1"
$root = Join-Path ([IO.Path]::GetTempPath()) ("dz08-wp-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path "$root/config" -Force | Out-Null
$script:calls = 0
$script:failDownload = $false
$script:corruptDownload = $false
function Invoke-WebRequest {
    param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing, $ErrorAction)
    $script:calls++
    [IO.File]::WriteAllText($OutFile, 'fixture')
    if ($script:failDownload) { throw 'download failure' }
    if ($script:corruptDownload) { [IO.File]::WriteAllText($OutFile, 'broken') }
}
function Start-Sleep { param($Seconds) }
try {
    $sha = [Convert]::ToHexString([Security.Cryptography.SHA512]::HashData([Text.Encoding]::UTF8.GetBytes('fixture'))).ToLowerInvariant()
    @{version='2.12.0';sha512=$sha} | ConvertTo-Json | Set-Content "$root/config/wp-cli.json"
    Install-WpCli -Root $root
    if ($script:calls -ne 1) { throw 'First download failed' }
    Install-WpCli -Root $root
    if ($script:calls -ne 1) { throw 'Cache not reused' }
    $asset = "$root/.tools/wp-cli/wp-cli-2.12.0.phar"
    [IO.File]::WriteAllText($asset, 'broken')
    Install-WpCli -Root $root
    if ($script:calls -ne 2) { throw 'Corruption not repaired' }
    foreach ($scenario in @('network', 'checksum')) {
        Remove-Item -LiteralPath $asset
        $script:failDownload = $scenario -eq 'network'
        $script:corruptDownload = $scenario -eq 'checksum'
        $before = $script:calls
        $failed = $false
        try { Install-WpCli -Root $root } catch { $failed = $true }
        if (-not $failed -or $script:calls -ne $before + 3) { throw 'Retry limit failed' }
        if (Get-ChildItem "$root/.tools/wp-cli" -File) { throw 'Partial file retained' }
        $script:failDownload = $false
        $script:corruptDownload = $false
        Install-WpCli -Root $root
    }
    'WP-CLI preparation scenarios passed.'
} finally {
    $resolved = [IO.Path]::GetFullPath($root)
    if (-not $resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) -or
        (Split-Path $resolved -Leaf) -notlike 'dz08-wp-test-*') { throw 'Unsafe test cleanup' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

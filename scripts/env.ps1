param(
    [string]$CloudId,
    [string]$FolderId,
    [string]$ServiceAccountId,
    [switch]$Clear
)
if ($Clear) {
    foreach ($name in @('YC_TOKEN', 'YC_CLOUD_ID', 'YC_FOLDER_ID')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    return
}
if (-not $CloudId) { $CloudId = Read-Host 'YC cloud ID' }
if (-not $FolderId) { $FolderId = Read-Host 'YC folder ID' }
if (-not $ServiceAccountId) { $ServiceAccountId = Read-Host 'YC service account ID' }
if (@($CloudId, $FolderId, $ServiceAccountId) -cmatch '[^a-z0-9]') {
    throw 'YC identifiers must contain only lowercase letters and digits.'
}
if (-not ($CloudId -and $FolderId -and $ServiceAccountId)) { throw 'All three YC identifiers are required.' }
$token = & yc iam create-token --impersonate-service-account-id $ServiceAccountId
if ($LASTEXITCODE -or -not $token) { throw 'YC failed to issue a service-account IAM token.' }
$env:YC_TOKEN = $token.Trim()
$env:YC_CLOUD_ID = $CloudId
$env:YC_FOLDER_ID = $FolderId
Remove-Variable token

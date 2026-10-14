# Saves the Notion internal-integration token from the CLIPBOARD into your Windows user environment
# (COPILOT_NOTION_TOKEN). The token is never printed; the clipboard is cleared afterwards.
# It is only saved if Notion accepts the token -- and, when config/account.toml already names the trade journal
# database ([notion] database_id), only if the token can open it (database shared via ... -> Connections).
$root = Split-Path -Parent $PSScriptRoot
$databaseId = ""
$cfg = Join-Path $root "config\account.toml"
if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern '^\s*database_id\s*=\s*"([^"]*)"' | Select-Object -First 1
    if ($m) { $databaseId = $m.Matches[0].Groups[1].Value }
}
# outcome log for troubleshooting -- never contains the token itself
New-Item -ItemType Directory -Force -Path (Join-Path $root "data\logs") | Out-Null
$logFile = Join-Path $root "data\logs\notion_token_setup.log"
function Log-Result($msg) { Add-Content -Path $logFile -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $msg) }
$token = (Get-Clipboard -Raw)
if ($null -ne $token) { $token = $token.Trim() }
if (-not $token -or $token -notmatch '^(ntn_|secret_)[A-Za-z0-9]{20,}$') {
    $shape = if (-not $token) { "empty" } else { "length " + $token.Length + ", starts with ntn_/secret_: " + ($token -match '^(ntn_|secret_)') + ", other chars: " + ($token -match '[^A-Za-z0-9_]') }
    Log-Result ("FAIL clipboard not a token (" + $shape + ")")
    Write-Host "The clipboard does not contain a Notion integration token (starts with ntn_ or secret_)."
    Write-Host "Copy the 'Internal Integration Secret' first, then run this again."
    Read-Host "Press Enter to close"
    exit 1
}
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $h = @{ "Authorization" = "Bearer $token"; "Notion-Version" = "2022-06-28" }
    if ($databaseId) {
        $db = Invoke-RestMethod -Uri "https://api.notion.com/v1/databases/$databaseId" -Headers $h -TimeoutSec 15
        Write-Host "Token verified: it can open the trade journal database."
    } else {
        $me = Invoke-RestMethod -Uri "https://api.notion.com/v1/users/me" -Headers $h -TimeoutSec 15
        Write-Host "Token verified (no journal database configured yet)."
    }
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    Log-Result ("FAIL Notion verification, HTTP " + $code + " " + $_.Exception.Message)
    if ($code -eq 401) { Write-Host "Notion rejected the token (401). Copy the secret again. Nothing was saved." }
    elseif ($code -eq 404) { Write-Host "The token works, but the database is not shared with this integration (404)."
                             Write-Host "In Notion open the trade journal page -> ... menu -> Connections -> add your integration, then run this again." }
    else { Write-Host "Could not verify the token with Notion (network problem?). Nothing was saved." }
    Read-Host "Press Enter to close"
    exit 1
}
[Environment]::SetEnvironmentVariable("COPILOT_NOTION_TOKEN", $token, "User")
Log-Result "OK token verified and saved"
$token = $null
Set-Clipboard -Value " "
Write-Host "Saved, and the clipboard was cleared. Tell your AI assistant 'done'."
Read-Host "Press Enter to close"

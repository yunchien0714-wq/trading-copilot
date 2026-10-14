# Saves the Telegram bot token from the CLIPBOARD into your Windows user environment (COPILOT_TELEGRAM_TOKEN).
# Use when pasting into a terminal does not work. The token is never printed; the clipboard is cleared after.
# Optional: put your bot's username here (without @) to refuse a token of a different bot.
$expectedBot = ""
$token = (Get-Clipboard -Raw)
if ($null -ne $token) { $token = $token.Trim() }
if (-not $token -or $token -notmatch '^\d+:[A-Za-z0-9_-]{30,}$') {
    Write-Host "The clipboard does not contain a bot token. Copy the token (digits:letters) first, then run this again."
    Read-Host "Press Enter to close"
    exit 1
}
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $me = Invoke-RestMethod -Uri ("https://api.telegram.org/bot" + $token + "/getMe") -TimeoutSec 15
    if ($expectedBot -and $me.result.username -ne $expectedBot) {
        Write-Host ("This token belongs to @" + $me.result.username + ", not @" + $expectedBot + ". Nothing was saved.")
        Read-Host "Press Enter to close"
        exit 1
    }
    Write-Host ("Token verified: it belongs to @" + $me.result.username)
} catch {
    Write-Host "Could not verify the token with Telegram (network or token problem). Nothing was saved."
    Write-Host "Check the token in your browser with .../bot<TOKEN>/getMe and try again."
    Read-Host "Press Enter to close"
    exit 1
}
[Environment]::SetEnvironmentVariable("COPILOT_TELEGRAM_TOKEN", $token, "User")
$token = $null
Set-Clipboard -Value " "
Write-Host ("Saved, and the clipboard was cleared. Send any message to @" + $me.result.username + ", then tell your AI assistant 'done'.")
Read-Host "Press Enter to close"

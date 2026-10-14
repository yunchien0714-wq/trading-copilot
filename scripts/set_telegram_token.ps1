# Stores the Telegram bot token in your Windows user environment (COPILOT_TELEGRAM_TOKEN).
# The token is typed into a hidden prompt: it is not shown, logged, or written to any file.
$secure = Read-Host "Paste the bot token from @BotFather (input is hidden)" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
if ($token -notmatch '^\d+:[A-Za-z0-9_-]{30,}$') {
    Write-Host "That does not look like a bot token (expected digits:letters). Nothing was saved."
    exit 1
}
[Environment]::SetEnvironmentVariable("COPILOT_TELEGRAM_TOKEN", $token, "User")
$token = $null
Write-Host "Saved. Now send any message to your bot in Telegram, then tell your AI assistant 'done'."

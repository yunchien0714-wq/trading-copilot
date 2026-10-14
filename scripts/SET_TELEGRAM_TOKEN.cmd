@echo off
rem Double-click this file AFTER copying your bot token. It reads the token from the clipboard.
powershell -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0set_telegram_token_from_clipboard.ps1"

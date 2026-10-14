@echo off
rem Double-click this file AFTER copying your Notion integration secret. It reads the token from the clipboard.
powershell -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0set_notion_token_from_clipboard.ps1"

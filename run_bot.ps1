param(
  [string]$Token = $env:TELEGRAM_BOT_TOKEN
)

Set-Location -Path $PSScriptRoot

if (-not $Token) {
  Write-Host "Missing TELEGRAM_BOT_TOKEN. Usage:" -ForegroundColor Yellow
  Write-Host "  .\run_bot.ps1 -Token `"123:ABC...`"" -ForegroundColor Yellow
  exit 1
}

$env:TELEGRAM_BOT_TOKEN = $Token

py -3.12 -c "import sys; print('Python:', sys.version)"
py -3.12 ".\rc_bot_improved.py"


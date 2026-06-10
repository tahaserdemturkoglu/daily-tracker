$ErrorActionPreference = "Continue"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"
$App = Join-Path $AppDir "app.py"
$Log = Join-Path $AppDir "telegram_always_on.log"
$StopFile = Join-Path $AppDir "telegram_watchdog.stop"

Set-Location $AppDir
"[$(Get-Date -Format s)] Telegram watchdog basladi" | Out-File -FilePath $Log -Append -Encoding utf8
if (Test-Path $StopFile) { Remove-Item $StopFile -Force -ErrorAction SilentlyContinue }

while ($true) {
  if (Test-Path $StopFile) {
    "[$(Get-Date -Format s)] Telegram watchdog durduruldu" | Out-File -FilePath $Log -Append -Encoding utf8
    break
  }
  try {
    "[$(Get-Date -Format s)] Telegram bot kontrol/baslatma" | Out-File -FilePath $Log -Append -Encoding utf8
    $p = Start-Process -FilePath $Python -ArgumentList "`"$App`" --telegram-only" -WorkingDirectory $AppDir -WindowStyle Hidden -PassThru
    Wait-Process -Id $p.Id -ErrorAction SilentlyContinue
    "[$(Get-Date -Format s)] Telegram bot sureci kapandi, yeniden denenecek" | Out-File -FilePath $Log -Append -Encoding utf8
  } catch {
    "[$(Get-Date -Format s)] Watchdog hata: $($_.Exception.Message)" | Out-File -FilePath $Log -Append -Encoding utf8
  }
  Start-Sleep -Seconds 20
}

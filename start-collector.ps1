$root = "D:\news-aggregator"
$python = Join-Path $root ".venv\Scripts\python.exe"
$collector = Join-Path $root "collector.py"
$logDir = Join-Path $root "logs"
$outLog = Join-Path $logDir "collector.out.log"
$errLog = Join-Path $logDir "collector.err.log"

New-Item -ItemType Directory -Force $logDir | Out-Null

# If collector is already running, do nothing (prevents duplicates)
$already = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*collector.py*" }

if ($already) { exit 0 }

Set-Location $root
Start-Process -FilePath $python -ArgumentList "`"$collector`"" -WindowStyle Hidden `
  -RedirectStandardOutput $outLog -RedirectStandardError $errLog

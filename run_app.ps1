Set-Location $PSScriptRoot
$Transcript = Join-Path $PSScriptRoot "data\run_app_transcript.log"
Start-Transcript -Path $Transcript -Append | Out-Null
Write-Host "Using Python from PATH"
Write-Host "Working directory: $PWD"
python -u -m stock_screener_filter.app_server
Write-Host "Python exited with code: $LASTEXITCODE"
Stop-Transcript | Out-Null
Read-Host "Press Enter to close"

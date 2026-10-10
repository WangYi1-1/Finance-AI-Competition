$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$appPath = Join-Path $projectRoot 'app.py'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Host '项目 Python 环境尚未准备好，请先按 README 中的步骤安装依赖。' -ForegroundColor Yellow
    exit 1
}

Set-Location -LiteralPath $projectRoot
Write-Host '财报分析界面正在启动。浏览器打开 http://localhost:8501；按 Ctrl+C 停止。' -ForegroundColor Cyan
& $pythonPath -m streamlit run $appPath --server.headless true --server.address 127.0.0.1 --server.maxUploadSize 30 --browser.gatherUsageStats false
exit $LASTEXITCODE

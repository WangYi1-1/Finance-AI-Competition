param()
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$venvDirectory = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'
$requirementsFile = Join-Path $projectRoot 'requirements.lock.txt'
$installLog = Join-Path $projectRoot 'install.log'
$transcriptStarted = $false

trap {
    Write-Host ('安装未完成：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ('请回传这个安装日志以便排查：' + $installLog)
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    exit 1
}

Start-Transcript -LiteralPath $installLog -Append -Force | Out-Null
$transcriptStarted = $true
Write-Host ('安装记录将保存在：' + $installLog)

function Test-CompatiblePython {
    param([string]$Executable, [string[]]$Arguments = @())
    try {
        & $Executable @Arguments -c "import sys, struct; sys.exit(0 if sys.version_info[:2] in [(3,12),(3,13)] and struct.calcsize('P') == 8 else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $venvDirectory) {
        throw '项目内已有不完整的 .venv 文件夹，请先将它改名，再重新运行首次安装。'
    }
    $pythonCandidate = $null
    $candidateArguments = @()
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.13', '-3.12')) {
            if (Test-CompatiblePython -Executable $launcher.Source -Arguments @($version)) {
                $pythonCandidate = $launcher.Source
                $candidateArguments = @($version)
                break
            }
        }
    }
    if (-not $pythonCandidate) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($pythonCommand -and (Test-CompatiblePython -Executable $pythonCommand.Source)) {
            $pythonCandidate = $pythonCommand.Source
        }
    }
    if (-not $pythonCandidate) {
        throw '没有找到64位 Python 3.12或3.13。建议先安装 Python 3.13，并勾选 Add python.exe to PATH，然后重新运行。'
    }
    Write-Host '正在为这台电脑创建项目运行环境。'
    & $pythonCandidate @candidateArguments -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) { throw '创建运行环境失败。' }
}
if (-not (Test-CompatiblePython -Executable $venvPython)) {
    throw '现有 .venv 无法使用，或不是64位 Python 3.12/3.13。请将 .venv 改名后重新安装。'
}
Write-Host '正在安装项目依赖；首次安装需要联网，可能耗时几分钟。'
& $venvPython -m pip install -r $requirementsFile
if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成。请检查网络后重新运行首次安装；已有环境不会被删除。' }

$localEnv = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $localEnv)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $localEnv
    Write-Host '已创建空白 .env。演示和查看保存结果不需要密钥；重新分析真实财报时再填写自己的密钥。'
} else {
    Write-Host '已保留现有 .env，未改动其中的配置。'
}
Write-Host '安装完成。现在双击“启动财报分析.cmd”，浏览器会打开项目界面。' -ForegroundColor Green
Stop-Transcript | Out-Null
exit 0

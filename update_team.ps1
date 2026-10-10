param()
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\', '/')

trap {
    Write-Host ('更新未完成：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '现有密钥、上传文件和分析结果不会由此脚本删除。'
    exit 1
}

$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCommand) { throw '没有找到 Git。请先安装官方 Git for Windows，重新打开后再更新。' }
$gitExe = $gitCommand.Source
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.git'))) {
    throw '这个目录不是克隆的仓库。请先按说明用 git clone 下载；旧压缩包目录不能直接同步。'
}
$repositoryRoot = & $gitExe rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0 -or [System.IO.Path]::GetFullPath($repositoryRoot).TrimEnd('\', '/') -ne $projectRoot) {
    throw '当前目录的 Git 仓库位置不正确。'
}
$origin = & $gitExe config --get remote.origin.url
if ($LASTEXITCODE -ne 0 -or $origin -notin @(
        'https://github.com/WangYi1-1/Finance-AI-Competition.git',
        'https://github.com/WangYi1-1/Finance-AI-Competition',
        'git@github.com:WangYi1-1/Finance-AI-Competition.git')) {
    throw '当前仓库来源不是指定项目，请检查克隆地址。'
}
$branch = & $gitExe branch --show-current
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw '当前不在 main 分支。请保留你的修改后再按说明切回 main。' }
$changes = & $gitExe status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { throw '无法检查当前代码状态。' }
if ($changes) { throw '你修改过项目代码。为避免覆盖，请先保存或提交修改；本地 .env 不影响更新。' }
$previous = & $gitExe rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw '无法读取当前版本。' }
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
Write-Host '正在获取项目最新版。请先关闭运行中的财报分析窗口。' -ForegroundColor Cyan
& $gitExe pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw '同步未完成。请检查 GitHub 网络；如仓库有不同提交，请先联系项目负责人。' }
$current = & $gitExe rev-parse HEAD
if ($previous -eq $current) {
    Write-Host '已经是最新版。' -ForegroundColor Green
} else {
    $dependencyChanges = & $gitExe diff --name-only $previous $current -- requirements.txt requirements.lock.txt
    if ($dependencyChanges) {
        Write-Host '依赖版本有更新，请重新运行“首次安装.cmd”。它会保留你已经填写的 .env。' -ForegroundColor Yellow
    }
    Write-Host '代码更新完成。双击“启动财报分析.cmd”重新打开界面。' -ForegroundColor Green
}
exit 0

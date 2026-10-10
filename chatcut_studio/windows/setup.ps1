# ChatCut Studio：Windows 一键安装
# 在仓库根目录（helloworld）的 PowerShell 里运行：
#   powershell -ExecutionPolicy Bypass -File chatcut_studio\windows\setup.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\..\.."
Set-Location $Root

function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "== 1/5 检查 Python（需要 3.11 及以上）"
if (-not (Has "py") -and -not (Has "python")) {
    Write-Host "没有找到 Python，正在用 winget 安装 Python 3.12……"
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    Write-Host "装好后请关闭并重新打开 PowerShell，再运行一次本脚本。"; exit 1
}
$Py = if (Has "py") { "py -3" } else { "python" }
Invoke-Expression "$Py --version"

Write-Host "== 2/5 创建虚拟环境 .venv 并安装依赖"
if (-not (Test-Path ".venv")) { Invoke-Expression "$Py -m venv .venv" }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r chatcut_studio\requirements.txt

Write-Host "== 3/5 安装无头浏览器（JIZURA 动态排版渲染用）"
& .\.venv\Scripts\python.exe -m playwright install chromium

Write-Host "== 4/5 检查 ffmpeg（需要带 libass，用于字幕渲染和合成视频）"
if (-not (Has "ffmpeg")) {
    Write-Host "没有找到 ffmpeg，正在用 winget 安装（Gyan.FFmpeg 完整版自带 libass）……"
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    Write-Host "ffmpeg 装好后需要重新打开 PowerShell 才能找到它。"
} else {
    $f = (& ffmpeg -hide_banner -filters 2>$null) -join "`n"
    if ($f -notmatch "\bass\b") { Write-Warning "当前 ffmpeg 不带 libass，「闪光扫过」字幕渲染不了。请改装 Gyan.FFmpeg：winget install Gyan.FFmpeg" }
    else { Write-Host "ffmpeg 正常（带 libass）" }
}

Write-Host "== 5/5 可选：Claude Code（只有用「Claude Code 插件」方式时才需要）"
if (Has "claude") { & claude --version } else {
    Write-Host "未安装 Claude Code。只用 DeepSeek 等「API 模型直连」可以跳过；需要时在 PowerShell 运行："
    Write-Host "  irm https://claude.ai/install.ps1 | iex"
}

Write-Host ""
Write-Host "安装完成。启动：双击 chatcut_studio\windows\run.bat，或运行 .\.venv\Scripts\python.exe -m chatcut_studio"

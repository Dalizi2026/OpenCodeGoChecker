# 一键构建 Windows 单文件 exe
#
#   用法（在仓库根目录）：
#       pwsh -File build.ps1
#
#   产物：dist\OpenCodeGoChecker.exe
#
# 依赖：Python 3.9+，然后
#       pip install -r requirements.txt pyinstaller

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

Write-Host '==> 检查 Python' -ForegroundColor Cyan
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw '未找到 python，请先安装 Python 3.9+ 并加入 PATH' }
python -c "import sys; print('Python', sys.version.split()[0])"

Write-Host '==> 检查依赖' -ForegroundColor Cyan
python -c "import webview, yaml; print('pywebview/yaml OK')"
python -c "import PyInstaller; print('PyInstaller', PyInstaller.__version__)"

Write-Host '==> 清理旧产物' -ForegroundColor Cyan
foreach ($d in @('build', 'dist')) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
}

Write-Host '==> 开始打包（约 1-3 分钟）' -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean OpenCodeGoChecker.spec

$exe = Join-Path $PSScriptRoot 'dist\OpenCodeGoChecker.exe'
if (-not (Test-Path $exe)) { throw '构建失败：未生成 dist\OpenCodeGoChecker.exe' }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
Write-Host "==> 构建成功：$exe  ($size MB)" -ForegroundColor Green

Write-Host '==> 冒烟测试：启动 exe，确认窗口出现' -ForegroundColor Cyan
$proc = Start-Process -FilePath $exe -PassThru
$title = 'Coding Plan 额度查询'
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    $w = Get-Process -Name OpenCodeGoChecker -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowTitle -like "*$title*" }
    if ($w) { $ok = $true; break }
}
if ($ok) {
    Write-Host '==> 冒烟测试通过：窗口已出现' -ForegroundColor Green
} else {
    Write-Host '==> 警告：60 秒内未检测到窗口，请手动双击 exe 确认' -ForegroundColor Yellow
}
Get-Process -Name OpenCodeGoChecker -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host '完成。' -ForegroundColor Green

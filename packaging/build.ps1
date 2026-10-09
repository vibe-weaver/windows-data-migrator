# 打包脚本：安装依赖 → 生成图标 → PyInstaller 打包 → 输出到 dist\
# 用法：pwsh -File packaging\build.ps1 [-Cli]
param(
    [switch]$Cli
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'env\Scripts\python.exe'
$mirror = 'https://mirrors.aliyun.com/pypi/simple/'
$icon = Join-Path $PSScriptRoot 'app.ico'
$version = Join-Path $PSScriptRoot 'version_info.txt'
$dist = Join-Path $root 'dist'
$work = Join-Path $root 'build'

if (-not (Test-Path $python)) {
    throw "找不到虚拟环境解释器：$python"
}

Write-Host '== 1/5 安装依赖 =='
& $python -m pip install -i $mirror -r (Join-Path $root 'requirements.txt')
& $python -m pip install -i $mirror pyinstaller

Write-Host '== 2/5 生成图标 =='
$env:QT_QPA_PLATFORM = 'offscreen'
& $python (Join-Path $PSScriptRoot 'make_icon.py') $icon

Write-Host '== 3/5 打包图形界面版（--onefile --windowed，清单为 asInvoker）=='
# 不加 --uac-admin 时 PyInstaller 内嵌的清单就是 asInvoker，双击不会弹 UAC
& $python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name '数据迁移工具' `
    --icon $icon `
    --version-file $version `
    --paths (Join-Path $root 'src') `
    --distpath $dist `
    --workpath $work `
    --specpath $PSScriptRoot `
    --hidden-import dmig.entry `
    --hidden-import dmig.cli `
    --hidden-import dmig.ui.main_window `
    (Join-Path $root 'app.py')

if ($Cli) {
    Write-Host '== 4/5 打包命令行版（--console，供脚本调用）=='
    & $python -m PyInstaller `
        --noconfirm --clean --onefile --console `
        --name '数据迁移工具-cli' `
        --icon $icon `
        --version-file $version `
        --paths (Join-Path $root 'src') `
        --distpath $dist `
        --workpath (Join-Path $work 'cli') `
        --specpath $PSScriptRoot `
        --hidden-import dmig.entry `
        --hidden-import dmig.cli `
        (Join-Path $root 'app.py')
} else {
    Write-Host '== 4/5 跳过命令行版（加 -Cli 可一并打包）=='
}

Write-Host '== 5/5 完成 =='
Get-ChildItem $dist | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }

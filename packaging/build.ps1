# 打包脚本：安装依赖 → 生成图标 → PyInstaller 打包 → 压缩 → 输出到 dist\
# 用法：pwsh -File packaging\build.ps1 [-Cli]
#
# 用 --onedir 而不是 --onefile。本机同一套测法实测「从启动到窗口出现」：
#   onedir   1.1 秒
#   onefile  6.9 秒（要先把 111 MB 解到 %TEMP%\_MEI<随机>\ 才能跑）
# 而且 onefile 被从任务管理器强杀 / 崩溃 / 断电时，那 111 MB 不会被清理，
# 永久留在 %TEMP%（实测单次泄漏 111.3 MB，本机一度积了 19 个共 2.06 GB）。
# onedir 直接在原地跑，完全不碰 %TEMP%。
# 代价是产物从「一个文件」变成「一个文件夹」，分发要打包成 zip —— 本脚本顺手做了。
param(
    [switch]$Cli
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'env\Scripts\python.exe'
$mirror = 'https://mirrors.aliyun.com/pypi/simple/'
$icon = Join-Path $PSScriptRoot 'app.ico'
# 品牌主图要跟着程序走：界面的品牌栏与窗口图标都从它缩放出来
$assets = Join-Path $root 'src\dmig\assets'
$version = Join-Path $PSScriptRoot 'version_info.txt'
$dist = Join-Path $root 'dist'
$work = Join-Path $root 'build'
$appName = 'Windows数据迁移助手'
$appVersion = '1.0.0'

if (-not (Test-Path $python)) {
    throw "找不到虚拟环境解释器：$python"
}

# 外部程序的失败不会让 PowerShell 抛异常：$ErrorActionPreference='Stop' 只管 cmdlet。
# 实测漏掉的后果很严重——程序正开着时 PyInstaller 会失败于
# "PermissionError: [WinError 5] ... dist\<应用名>"，而脚本照旧打印「== 完成 ==」和
# 那张 dist 列表，让人以为构建成功了，发的其实还是上一次的产物。
function Assert-Step([string] $step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$step 失败（退出码 $LASTEXITCODE）。dist 里可能还是上一次的产物。"
    }
}

Write-Host '== 1/6 安装依赖 =='
& $python -m pip install -i $mirror -r (Join-Path $root 'requirements.txt')
Assert-Step '安装依赖'
& $python -m pip install -i $mirror pyinstaller
Assert-Step '安装 PyInstaller'

Write-Host '== 2/6 生成图标 =='
$env:QT_QPA_PLATFORM = 'offscreen'
& $python (Join-Path $PSScriptRoot 'make_icon.py') $icon
Assert-Step '生成图标'

Write-Host '== 3/6 清掉上一次可能留下的单文件产物 =='
# 改成 onedir 之前打出来的是 dist\<应用名>.exe（一个文件），它会和新生成的
# dist\<应用名>\（一个文件夹）并存，让人分不清该发哪个。同名同版本，直接清掉。
foreach ($stale in @("$appName.exe", "$appName-cli.exe")) {
    $path = Join-Path $dist $stale
    if (Test-Path $path -PathType Leaf) {
        Remove-Item $path -Force
        Write-Host "  已清掉旧的单文件产物：$stale"
    }
}
Write-Host '  （无则跳过）'

Write-Host '== 4/6 打包图形界面版（--onedir --windowed，清单为 asInvoker）=='
# 不加 --uac-admin 时 PyInstaller 内嵌的清单就是 asInvoker，双击不会弹 UAC
#
# --optimize 2 会剥掉文档字符串（和 assert）。**这里的文档字符串写得很详细**——
# 含设计理由与实测数字，等于把开发笔记一起发出去：实测产物里 30 个模块有 28 个
# 带着文档字符串，共约 2.8 万字符。
#
# 用它是安全的，已确认：src 里没有任何 assert（-OO 会移除 assert，靠它校验就会出事），
# 也没有代码读 __doc__（-OO 之后是 None）。
#
# 说清楚它**不是**代码保护：字节码照样能反编译。它降低的是「随手一看就看到全部
# 设计笔记」的门槛，不是加密。
& $python -m PyInstaller `
    --noconfirm --clean --onedir --windowed --optimize 2 `
    --name $appName `
    --icon $icon `
    --version-file $version `
    --paths (Join-Path $root 'src') `
    --distpath $dist `
    --workpath $work `
    --specpath $PSScriptRoot `
    --add-data "$assets;dmig/assets" `
    --hidden-import dmig.entry `
    --hidden-import dmig.cli `
    --hidden-import dmig.ui.main_window `
    (Join-Path $root 'app.py')
Assert-Step '打包图形界面版'

if ($Cli) {
    Write-Host '== 5/6 打包命令行版（--onedir --console，供脚本调用）=='
    & $python -m PyInstaller `
        --noconfirm --clean --onedir --console --optimize 2 `
        --name "$appName-cli" `
        --icon $icon `
        --version-file $version `
        --paths (Join-Path $root 'src') `
        --distpath $dist `
        --workpath (Join-Path $work 'cli') `
        --specpath $PSScriptRoot `
        --add-data "$assets;dmig/assets" `
        --hidden-import dmig.entry `
        --hidden-import dmig.cli `
        (Join-Path $root 'app.py')
    Assert-Step '打包命令行版'
} else {
    Write-Host '== 5/6 跳过命令行版（加 -Cli 可一并打包）=='
}

Write-Host '== 6/6 压缩成可直接发送的 zip =='
# 收件人需要的是整个文件夹（单独把 exe 拿出去跑不起来），
# 所以分发的单位是 zip 而不是 exe。
$appDir = Join-Path $dist $appName
$zip = Join-Path $dist "$appName-$appVersion-win64.zip"

# 闸门放在压缩【之前】：万一 PyInstaller 以 0 退出却没有真正刷新产物
# （缓存、并发写入、路径搞错……），退出码检查发现不了，比时间戳能兜住。
# 先校验再打包，免得留下一个用旧内容打的 zip 让人误发出去。
# 实测踩过一次：改了源码、构建照报完成，dist 里其实还是上一次的 exe。
Write-Host '== 校验产物确实是这次打的 =='
$builtExe = Join-Path $appDir "$appName.exe"
if (-not (Test-Path $builtExe)) {
    throw "产物不存在：$builtExe"
}
$exeTime = (Get-Item $builtExe).LastWriteTime
$newestSource = Get-ChildItem (Join-Path $root 'src') -Recurse -File -Filter '*.py' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newestSource -and $newestSource.LastWriteTime -gt $exeTime) {
    throw ("产物比源码旧——构建没有真正生效。" +
           "exe：$($exeTime.ToString('HH:mm:ss'))，" +
           "源码：$($newestSource.LastWriteTime.ToString('HH:mm:ss')) " +
           "($($newestSource.Name))。" +
           "最常见的原因是程序正开着，exe 被占用导致 PyInstaller 无法覆盖。")
}
Write-Host "  exe $($exeTime.ToString('HH:mm:ss')) 比最新的源码文件新，通过"

# 使用说明书跟着程序一起发出去。
# **必须在这里拷，不能手工往 dist 里放**：dist 是构建产物，每次打包都被整个替换，
# 手工放进去的文件下一次就没了。源在 docs/manual，由 docs/manual/build.py 生成
# （渲染截图 + 内嵌成自包含 HTML）。
$manualSource = Join-Path $root 'docs\manual\使用说明书.html'
if (Test-Path $manualSource) {
    Copy-Item $manualSource (Join-Path $appDir '使用说明书.html') -Force
    $manualSize = [math]::Round((Get-Item (Join-Path $appDir '使用说明书.html')).Length / 1KB)
    Write-Host "  已附上使用说明书（$manualSize KB）"
} else {
    Write-Host "  （没有找到使用说明书，跳过：先跑 docs\manual\build.py）"
}


if (Test-Path $zip) {
    Remove-Item $zip -Force
}
# 不用 Compress-Archive：它依赖 Microsoft.PowerShell.Archive 模块，
# 在受限环境里会加载失败（本机实测 AuthorizationManager check failed）。
& $python (Join-Path $PSScriptRoot 'make_zip.py') $appDir $zip
Assert-Step '压缩 zip'

Write-Host ''
Write-Host '== 完成 =='
Get-ChildItem $dist | ForEach-Object {
    if ($_.PSIsContainer) {
        $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object Length -Sum).Sum
        [pscustomobject]@{ Name = "$($_.Name)\"; MB = [math]::Round($size / 1MB, 1) }
    } else {
        [pscustomobject]@{ Name = $_.Name; MB = [math]::Round($_.Length / 1MB, 1) }
    }
} | Format-Table -AutoSize
Write-Host "发给别人的是这个 zip：解压后双击里面的 $appName.exe"



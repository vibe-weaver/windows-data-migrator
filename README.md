# 数据目录迁移工具

把 C 盘上占空间的数据目录搬到其他盘，并在原路径留下链接，让程序继续照常访问。

## 迁移流程

```
预扫描 → 复制 → 校验 → 源目录改名为备份 → 建立链接 → 确认后删除备份
```

校验不通过**绝不删除源目录**；任何一步失败都可以回滚。

## 特性

- **正确处理嵌套软连接**：目录里的 junction / 符号链接只在目标端重建链接本身，不把远端仓库（如 pnpm、npm 缓存）的数据复制过来
- **链接目标重写**：相对路径原样保留；指向源树内部的绝对路径自动改写为新位置；指向树外的保留，并给出归属报告让你决定是否映射
- **权限不足可恢复**：预扫描就列出无权限路径，一键以管理员身份重试后自动接管属主
- **文件被占用不放弃**：自动重试并查出占用进程，关掉程序后可断点续传
- **长路径安全**：全程 `\\?\` 前缀，`node_modules` 这类深层目录不会失败

## 图形界面

直接双击 `dist\数据迁移工具.exe`，或开发时运行：

```powershell
& '.\env\Scripts\python.exe' app.py
```

四步向导：

1. **选择目录**——源目录、目标目录（支持拖拽），实时显示两个卷的剩余空间与前置校验结果
2. **迁移计划**——真实数据量、文件数、目录数、链接数、需提权目录数、预计耗时；
   链接目标归属表列出所有指向源树之外的链接，可逐条填写「重写为」的新路径后
   点「应用映射并重新扫描」；无权限时点「以管理员身份重试」
3. **执行迁移**——总进度、当前文件、速度、剩余时间、滚动日志；可暂停/取消；
   失败清单显示占用进程名与 PID，双击可定位
4. **结果报告**——成功/失败/跳过统计、未完成清单、导出 CSV、一键回滚、删除备份

启动参数：

- `--elevated`——由「以管理员身份重试」自动带上，无需手输
- `--session <id>`——恢复指定会话（提权重启时用于把任务带回界面）

## 打包

```powershell
pwsh -File packaging\build.ps1          # 只出图形界面版
pwsh -File packaging\build.ps1 -Cli     # 额外出一个命令行版
```

产物在 `dist\`：

- `数据迁移工具.exe`——单文件图形界面版，内嵌 `asInvoker` 清单，双击不弹 UAC
- `数据迁移工具-cli.exe`——加 `-Cli` 时才有；`--windowed` 的 exe 没有控制台，
  想在终端里用子命令请用这个版本

## 命令行用法

```powershell
# 只看计划，不动任何数据
python app.py scan --source D:\myapp\data --target E:\myapp\data

# 执行迁移
python app.py migrate --source D:\a --target E:\b --yes

# 联动迁移：把链接指向的仓库一起搬走并重写目标
python app.py migrate --source D:\a --target E:\b --yes ^
  --map "C:\Users\me\AppData\Local\npm-cache=E:\npm-cache"

# 启用逐文件哈希校验
python app.py migrate --source D:\a --target E:\b --yes --sha256
```

## 开发

```powershell
& '.\env\Scripts\python.exe' -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
& '.\env\Scripts\python.exe' -m pytest -q
```


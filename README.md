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
& '.\env\Scripts\python.exe' -m pip install -i https://mirrors.aliyun.com/pypi/simple/ pytest
& '.\env\Scripts\python.exe' -m pytest -q
```

## 设计文档

- 设计：[docs/superpowers/specs/2026-10-09-windows-data-migration-tool-design.md](docs/superpowers/specs/2026-10-09-windows-data-migration-tool-design.md)
- 计划：[docs/superpowers/plans/2026-10-09-migration-core.md](docs/superpowers/plans/2026-10-09-migration-core.md)

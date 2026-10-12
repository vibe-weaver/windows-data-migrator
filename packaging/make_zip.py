"""把打包好的目录压成 zip：分发单位是整个文件夹，不是单个 exe。

用 Python 的 `zipfile` 而不是 PowerShell 的 `Compress-Archive`——
后者依赖 `Microsoft.PowerShell.Archive` 模块，在受限环境里会加载失败
（本机实测报 `AuthorizationManager check failed`，同 `Get-AuthenticodeSignature`）。

zip 内的路径保留顶层目录名（`Windows数据迁移助手/...`），这样收件人解压出来是一个文件夹，
不会把几百个文件直接摊在下载目录里。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def main(src_dir, out_zip) -> int:
    src = Path(src_dir)
    out = Path(out_zip)
    if not src.is_dir():
        print(f"找不到目录：{src}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    raw = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            zf.write(path, path.relative_to(src.parent))
            count += 1
            raw += path.stat().st_size

    print(f"已生成：{out}")
    print(f"  {count} 个文件，压缩前 {raw / 2**20:.1f} MB，"
          f"压缩后 {out.stat().st_size / 2**20:.1f} MB")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：make_zip.py <目录> <输出.zip>")
    raise SystemExit(main(sys.argv[1], sys.argv[2]))


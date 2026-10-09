"""程序入口，供 PyInstaller 打包与命令行调用。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dmig.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

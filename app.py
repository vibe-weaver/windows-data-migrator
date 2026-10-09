"""PyInstaller 与命令行共用的入口脚本。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dmig.entry import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

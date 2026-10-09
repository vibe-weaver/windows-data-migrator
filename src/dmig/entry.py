"""统一入口：无子命令时开图形界面，带子命令时走命令行。"""
from __future__ import annotations

import sys

CLI_COMMANDS = ("scan", "migrate", "resume", "rollback")
CLI_HELP_FLAGS = ("-h", "--help")
ELEVATED_FLAG = "--elevated"
SESSION_FLAG = "--session"


def is_cli_invocation(argv: list[str]) -> bool:
    """首个非选项参数命中子命令就走 CLI；--session 的值不会被误判。

    `-h` / `--help` 也必须走 CLI：否则 `app.py --help` 会去开图形界面并阻塞，
    用户永远看不到用法说明（这是个真实踩到过的问题）。
    """
    tokens = list(argv)[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in CLI_HELP_FLAGS:
            return True
        if token == SESSION_FLAG:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token in CLI_COMMANDS
    return False


def parse_launch_args(argv: list[str]) -> dict:
    """解析提权重启时会带的两个参数，未知参数忽略。"""
    result: dict = {"elevated": False, "session": None}
    tokens = list(argv)[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == ELEVATED_FLAG:
            result["elevated"] = True
        elif token == SESSION_FLAG and index + 1 < len(tokens):
            result["session"] = tokens[index + 1]
            index += 1
        index += 1
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if is_cli_invocation(argv):
        from .cli import main as cli_main
        return cli_main(argv[1:])
    from .ui.main_window import run_gui
    return run_gui(argv)

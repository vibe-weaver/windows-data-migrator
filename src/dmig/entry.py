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


def force_utf8_stdio(streams=None) -> None:
    r"""把**被重定向**的标准输出/错误切到 UTF-8。

    背景：frozen 的 `--windowed` exe 在 stdout 被重定向时默认用系统 ANSI 代码页
    （中文系统是 cp936），而 `scan` 输出的计划 JSON 含中文路径，
    按 UTF-8 解码的消费方会直接报 `invalid continuation byte`。

    但**交互式控制台不能动**：中文控制台本身就是 cp936，
    强行改成 UTF-8 会让中文显示成乱码。所以只处理非 tty 的流。
    GUI 模式下 stdout 是 None，跳过。
    """
    for stream in (sys.stdout, sys.stderr) if streams is None else streams:
        if stream is None:
            continue
        try:
            if stream.isatty():
                continue
        except Exception:
            pass
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if is_cli_invocation(argv):
        force_utf8_stdio()
        from .cli import main as cli_main
        return cli_main(argv[1:])
    from .ui.main_window import run_gui
    return run_gui(argv)

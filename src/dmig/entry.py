"""统一入口：无子命令时开图形界面，带子命令时走命令行。"""
from __future__ import annotations

import sys

CLI_COMMANDS = ("scan", "migrate", "resume", "rollback", "discard")
CLI_HELP_FLAGS = ("-h", "--help")
ELEVATED_FLAG = "--elevated"
SESSION_FLAG = "--session"


def unknown_command(argv: list[str]) -> str | None:
    """找出「看起来像子命令、但不在 CLI_COMMANDS 里」的第一个位置参数。

    用来把「没带子命令」（→ 开图形界面）与「带了不认识的词」（→ 报错）区分开。
    `app.py migrat ...` 这种拼错，原来会被当成没带子命令，于是一声不吭弹出一个
    图形窗口；在脚本或 CI 里表现为进程挂住不返回。

    `--session` 的值（会话 id）要跳过——它长得就像个子命令。
    """
    tokens = list(argv)[1:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == SESSION_FLAG:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return None if token in CLI_COMMANDS else token
    return None


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
    unknown = unknown_command(argv)
    if unknown is not None:
        # 拼错子命令时给出纠错，而不是弹一个窗口让人莫名其妙
        force_utf8_stdio()
        print(f"未知子命令：{unknown}")
        print(f"可用子命令：{'、'.join(CLI_COMMANDS)}")
        print("不带子命令直接运行会打开图形界面，用法见 -h")
        return 2
    from .ui.main_window import run_gui
    return run_gui(argv)

"""界面用的格式化函数，纯计算、不依赖 Qt。"""
from __future__ import annotations

KB = 1024
MB = KB * 1024
GB = MB * 1024

# 耗时估算用的粗略假设：顺序写入约 120 MB/s，每个文件约 0.3 ms 固定开销
ASSUMED_BYTES_PER_SECOND = 120 * MB
SECONDS_PER_FILE = 0.0003


def human_bytes(n: int) -> str:
    n = int(n)
    if n < KB:
        return f"{n} B"
    if n < MB:
        return f"{n / KB:.1f} KB"
    if n < GB:
        return f"{n / MB:.1f} MB"
    return f"{n / GB:.2f} GB"


def human_duration(seconds: float) -> str:
    seconds = max(float(seconds), 0.0)
    if seconds < 1:
        return "<1 秒"
    total = int(round(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minute = divmod(minutes, 60)
    return f"{hours} 小时 {minute} 分"


def human_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    return f"{human_bytes(int(bytes_per_second))}/s"


def estimate_seconds(required_bytes: int, files: int) -> float:
    """按固定吞吐与每文件开销估算耗时，仅用于给用户一个量级。"""
    return required_bytes / ASSUMED_BYTES_PER_SECOND + files * SECONDS_PER_FILE

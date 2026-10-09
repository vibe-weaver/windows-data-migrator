from dmig.ui import format as fmt


def test_human_bytes_scales():
    assert fmt.human_bytes(0) == "0 B"
    assert fmt.human_bytes(512) == "512 B"
    assert fmt.human_bytes(2048) == "2.0 KB"
    assert fmt.human_bytes(1024 * 1024) == "1.0 MB"
    assert fmt.human_bytes(3 * 1024 ** 3) == "3.00 GB"


def test_human_duration_scales():
    assert fmt.human_duration(0) == "<1 秒"
    assert fmt.human_duration(0.4) == "<1 秒"
    assert fmt.human_duration(45) == "45 秒"
    assert fmt.human_duration(125) == "2 分 5 秒"
    assert fmt.human_duration(3725) == "1 小时 2 分"


def test_human_duration_clamps_negative():
    assert fmt.human_duration(-10) == "<1 秒"


def test_human_speed():
    assert fmt.human_speed(0) == "-"
    assert fmt.human_speed(-1) == "-"
    assert fmt.human_speed(120 * 1024 * 1024) == "120.0 MB/s"


def test_estimate_seconds_uses_throughput_and_per_file_cost():
    assert fmt.estimate_seconds(0, 0) == 0.0
    assert fmt.estimate_seconds(fmt.ASSUMED_BYTES_PER_SECOND, 0) == 1.0
    assert fmt.estimate_seconds(0, 1000) == 0.3

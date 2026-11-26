"""任务历史磁盘替身：长度、字段校验、拒绝覆盖，以及配置重建的逐字节等价。"""

import gzip
import json

import pytest

from fabcosim.cosim.task_history import DiskTaskHistory


def make_config(position: int, compact: bool = True) -> dict:
    """构造一个含非ASCII键、浮点与空数组的配置，并让tasks落在指定位置。"""
    entries = [
        ("lots", [{"id": "lot0", "route": "R1", "wafers": 25}]),
        ("performance", {"compact_json": compact, "event_compression": "gzip"}),
        ("任务说明", {"来源": "光刻", "比例": 0.1, "阈值": 1e-09}),
        ("seed", 7),
        ("empty", []),
    ]
    entries.insert(position, ("tasks", None))
    return dict(entries)


def make_row(index: int) -> dict:
    return {
        "id": f"t{index}",
        "lot_id": f"批次{index}",
        "source": "设备A" if index % 2 else "设备B",
        "target": "缓冲口",
    }


def reference_bytes(config: dict, tasks: list) -> bytes:
    """验收口径：整份配置连同完整任务列表交给json.dumps的字节。"""
    full = dict(config)
    full["tasks"] = tasks
    return json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def read_rows(path) -> list:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.loads(stream.read())


def attach(config: dict, history: DiskTaskHistory) -> dict:
    config["tasks"] = history
    return config


def test_empty_history_writes_empty_array(tmp_path):
    path = tmp_path / "task-history.json.gz"
    history = DiskTaskHistory(path)
    assert len(history) == 0
    history.close()
    assert read_rows(path) == []


def test_append_counts_and_round_trips_rows(tmp_path):
    path = tmp_path / "task-history.json.gz"
    history = DiskTaskHistory(path)
    rows = [make_row(i) for i in range(3)]
    for row in rows:
        history.append(row)
    assert len(history) == 3
    history.close()
    assert read_rows(path) == rows


@pytest.mark.parametrize("position", [0, 2, 5])
def test_write_config_is_byte_identical_and_keeps_key_order(tmp_path, position):
    config = attach(make_config(position), None)
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    config = attach(config, history)
    rows = [make_row(i) for i in range(4)]
    for row in rows:
        history.append(row)

    written = history.write_config(config, tmp_path / "config.json")

    assert written.read_bytes() == reference_bytes(make_config(position), rows)
    assert list(json.loads(written.read_text("utf-8"))) == [key for key, _ in config.items()]


def test_write_config_after_close_and_without_rows(tmp_path):
    config = attach(make_config(3), None)
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    config = attach(config, history)
    history.close()

    written = history.write_config(config, tmp_path / "config.json")

    assert written.read_bytes() == reference_bytes(make_config(3), [])


def test_encoding_failure_does_not_increment_and_file_stays_valid(tmp_path):
    path = tmp_path / "task-history.json.gz"
    history = DiskTaskHistory(path)
    history.append(make_row(0))
    broken = make_row(1)
    broken["source"] = object()

    with pytest.raises(TypeError):
        history.append(broken)

    assert len(history) == 1
    history.close()
    assert read_rows(path) == [make_row(0)]


@pytest.mark.parametrize(
    "row",
    [
        {"id": "t0", "lot_id": "lot0", "source": "a"},
        {"id": "t0", "lot_id": "lot0", "source": "a", "target": "b", "extra": 1},
        ["t0", "lot0", "a", "b"],
    ],
)
def test_schema_violations_rejected_without_counting(tmp_path, row):
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    with pytest.raises((TypeError, ValueError)):
        history.append(row)
    assert len(history) == 0
    history.close()


def test_append_after_close_rejected(tmp_path):
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    history.close()
    with pytest.raises(RuntimeError):
        history.append(make_row(0))


def test_close_is_idempotent_and_leaves_valid_array(tmp_path):
    path = tmp_path / "task-history.json.gz"
    history = DiskTaskHistory(path)
    history.append(make_row(0))
    history.close()
    history.close()
    assert read_rows(path) == [make_row(0)]


def test_existing_history_path_refused(tmp_path):
    path = tmp_path / "task-history.json.gz"
    path.write_bytes("旧产物".encode("utf-8"))
    with pytest.raises(FileExistsError):
        DiskTaskHistory(path)
    assert path.read_bytes() == "旧产物".encode("utf-8")


def test_existing_config_destination_refused(tmp_path):
    config = attach(make_config(2), None)
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    config = attach(config, history)
    destination = tmp_path / "config.json"
    destination.write_text("已有配置", encoding="utf-8")

    with pytest.raises(FileExistsError):
        history.write_config(config, destination)

    assert destination.read_text("utf-8") == "已有配置"


def test_non_compact_config_rejected(tmp_path):
    config = attach(make_config(2, compact=False), None)
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    config = attach(config, history)
    with pytest.raises(ValueError):
        history.write_config(config, tmp_path / "config.json")
    assert not (tmp_path / "config.json").exists()


def test_config_must_hold_this_history(tmp_path):
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    config = make_config(2)
    config["tasks"] = []
    with pytest.raises(ValueError):
        history.write_config(config, tmp_path / "config.json")
    history.close()


@pytest.mark.parametrize("level", [True, -1, 10, 1.5])
def test_level_validated_before_file_creation(tmp_path, level):
    path = tmp_path / "task-history.json.gz"
    with pytest.raises(ValueError):
        DiskTaskHistory(path, level)  # type: ignore[arg-type]
    assert not path.exists()


def test_many_rows_not_retained_in_memory(tmp_path):
    config = attach(make_config(2), None)
    history = DiskTaskHistory(tmp_path / "task-history.json.gz")
    config = attach(config, history)
    for index in range(5000):
        history.append(make_row(index))

    assert len(history) == 5000
    # 类里只允许路径、压缩等级、写出口、计数与关闭标记；没有任何行列表。
    assert [name for name, value in vars(history).items() if isinstance(value, (list, dict, set))] == []

    written = history.write_config(config, tmp_path / "config.json")
    assert written.read_bytes() == reference_bytes(
        make_config(2), [make_row(index) for index in range(5000)]
    )

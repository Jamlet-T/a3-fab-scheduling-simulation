"""检查点专项测试：真实 SimPy 运行图、损坏检测、元数据拒绝与旧代回退。"""

from __future__ import annotations

import hashlib
import json
import random
import threading

import pytest
import simpy

from fabcosim.cosim.checkpoint import (
    CheckpointCorruptError,
    CheckpointError,
    CheckpointMetadataMismatch,
    load_checkpoint,
    save_checkpoint,
)

ROUNDS = 4
SPLIT = 8.0


def test_corrupt_external_asset_falls_back_before_unpickling(tmp_path):
    path = tmp_path / "checkpoint.json"
    first = tmp_path / "part-1.json"
    second = tmp_path / "part-2.json"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    def metadata(parts):
        return {"tag": "asset", "checkpoint_assets": [
            {"path": str(p), "size": p.stat().st_size,
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in parts
        ]}

    save_checkpoint(path, {"generation": 1}, metadata([first]))
    save_checkpoint(path, {"generation": 2}, metadata([first, second]))
    second.write_bytes(b"broken")
    payload, manifest = load_checkpoint(path, {"tag": "asset"})
    assert payload == {"generation": 1}
    assert len(manifest["metadata"]["checkpoint_assets"]) == 1
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(path, {"tag": "asset"}, fallback=False)


class _Routine:
    """普通 Routine：实现 send/throw，由原生 simpy.Process 驱动，不持有执行帧。"""

    def __iter__(self):
        return self

    def __next__(self):
        return self.send(None)

    def send(self, value):
        raise NotImplementedError

    def throw(self, error, value=None, traceback=None):
        if isinstance(error, BaseException):
            raise error
        raise error() if value is None else error(value)

    def close(self):
        return None


def _make_world(seed: int = 7) -> dict:
    """小型模型：资源等待、同刻事件、本地闭包与独立随机流。"""
    env = simpy.Environment()
    log: list[tuple] = []
    machine = simpy.Resource(env, capacity=1)
    rng = random.Random(seed)
    shared = {"hits": 0, "ticks": 0}

    def hold_time() -> float:
        # 本地闭包：捕获 rng，恢复后必须继续同一随机流。
        return float(rng.randint(1, 3))

    class Job(_Routine):
        def __init__(self, name: str, rounds: int) -> None:
            self.name = name
            self.rounds = rounds
            self.stage = 0
            self.request = None
            self.hold_time = hold_time

        def send(self, value):
            while True:
                if self.stage == 0:
                    self.stage = 1
                    self.request = machine.request()
                    log.append((env.now, self.name, "request"))
                    return self.request
                if self.stage == 1:
                    self.stage = 2
                    log.append((env.now, self.name, "acquired"))
                    return env.timeout(self.hold_time())
                if self.stage == 2:
                    self.stage = 0
                    shared["hits"] += 1
                    log.append((env.now, self.name, "done", shared["hits"], rng.random()))
                    machine.release(self.request)
                    self.request = None
                    self.rounds -= 1
                    if self.rounds <= 0:
                        raise StopIteration(None)
                    # 同一时刻：新进程的 Initialize 是 URGENT，零延迟 Timeout 是 NORMAL。
                    env.process(Tick())
                    return env.timeout(0)

    class Tick(_Routine):
        def __init__(self) -> None:
            self.fired = False

        def send(self, value):
            if self.fired:
                raise StopIteration(None)
            self.fired = True
            shared["ticks"] += 1
            log.append((env.now, "tick", shared["ticks"], rng.random()))
            return env.timeout(0)

    workers = [env.process(Job(f"w{index}", ROUNDS)) for index in range(3)]
    return {
        "env": env,
        "log": log,
        "machine": machine,
        "rng": rng,
        "shared": shared,
        "workers": workers,
        "hold_time": hold_time,
        "pending_marker": simpy.events.PENDING,
    }


def _queue_snapshot(env: simpy.Environment) -> list[tuple]:
    return [(time, priority, eid) for time, priority, eid, _ in env._queue]


def test_resume_matches_continuous_run(tmp_path):
    reference = _make_world()
    reference["env"].run()
    reference_trace = list(reference["log"])
    reference_rng = reference["rng"].getstate()

    world = _make_world()
    world["env"].run(until=SPLIT)
    queue_before = _queue_snapshot(world["env"])
    assert queue_before
    # 切面确实包含同一时刻的多个事件（同刻队列）。
    assert len({entry[0] for entry in queue_before}) < len(queue_before)
    users_before = len(world["machine"].users)
    waiting_before = len(world["machine"].queue)
    assert users_before == 1 and waiting_before > 0

    path = tmp_path / "checkpoint.json"
    written = save_checkpoint(
        path,
        world,
        {"scenario": "unit", "seed": 7, "split_time": SPLIT},
    )
    # 保存不得推进活对象：时间、队列与事件序号保持不变。
    assert world["env"].now == SPLIT
    assert _queue_snapshot(world["env"]) == queue_before

    restored, manifest = load_checkpoint(
        path,
        {"scenario": "unit", "seed": 7},
    )
    assert manifest == written
    renv = restored["env"]
    assert renv.now == SPLIT
    assert _queue_snapshot(renv) == queue_before
    assert all(event.env is renv for _, _, _, event in renv._queue)
    assert renv.process.__self__ is renv
    # 事件序号计数器从同一位置继续（不仅是队列里已有序号相同）。
    assert repr(renv._eid) == repr(world["env"]._eid)
    assert restored["pending_marker"] is simpy.events.PENDING
    # 资源占用与等待请求同样属于运行图，必须原样保留。
    assert len(restored["machine"].users) == users_before
    assert len(restored["machine"].queue) == waiting_before

    prefix = len(world["log"])
    renv.run()
    assert restored["log"][:prefix] == reference_trace[:prefix]
    assert restored["log"][prefix:] == [entry for entry in reference_trace if entry[0] >= SPLIT]
    assert restored["log"] == reference_trace
    assert renv.now == reference["env"].now
    assert restored["rng"].getstate() == reference_rng
    assert restored["shared"] == reference["shared"]
    assert not restored["machine"].users
    assert not restored["machine"].queue


@pytest.mark.parametrize("damage", ["flip", "truncate", "missing"])
def test_corrupt_state_blob_is_rejected(tmp_path, damage):
    path = tmp_path / "checkpoint.json"
    manifest = save_checkpoint(path, {"generation": 1}, {"tag": "a"})
    blob = tmp_path / manifest["state"]["file"]
    original = blob.read_bytes()
    if damage == "flip":
        broken = bytearray(original)
        broken[len(broken) // 2] ^= 0xFF
        blob.write_bytes(bytes(broken))
        expected = bytes(broken)
    elif damage == "truncate":
        blob.write_bytes(original[: len(original) // 2])
        expected = original[: len(original) // 2]
    else:
        blob.unlink()
        expected = None

    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(path, {"tag": "a"}, fallback=False)
    # 读取失败不得改动损坏证据。
    if expected is not None:
        assert blob.read_bytes() == expected
    else:
        assert not blob.exists()


def test_corrupt_main_falls_back_to_previous_generation(tmp_path):
    path = tmp_path / "checkpoint.json"
    first = save_checkpoint(path, {"generation": 1}, {"tag": "a"})
    save_checkpoint(path, {"generation": 2}, {"tag": "a"})
    previous_path = tmp_path / "checkpoint.previous.json"
    assert previous_path.exists()
    assert json.loads(previous_path.read_text(encoding="utf-8")) == first

    current = json.loads(path.read_text(encoding="utf-8"))
    blob = tmp_path / current["state"]["file"]
    blob.write_bytes(b"broken")

    payload, manifest = load_checkpoint(path, {"tag": "a"})
    assert payload == {"generation": 1}
    assert manifest["state"]["file"] == first["state"]["file"]
    # 损坏证据原样保留，且不影响带 fallback=False 的显式拒绝。
    assert blob.read_bytes() == b"broken"
    with pytest.raises(CheckpointCorruptError):
        load_checkpoint(path, {"tag": "a"}, fallback=False)


def test_corrupt_manifest_falls_back_to_previous_generation(tmp_path):
    path = tmp_path / "checkpoint.json"
    first = save_checkpoint(path, {"generation": 1}, {"tag": "a"})
    save_checkpoint(path, {"generation": 2}, {"tag": "a"})
    path.write_text("{not-json", encoding="utf-8")

    payload, manifest = load_checkpoint(path, {"tag": "a"})
    assert payload == {"generation": 1}
    assert manifest["state"]["file"] == first["state"]["file"]
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_metadata_change_is_rejected_without_silent_fallback(tmp_path):
    path = tmp_path / "checkpoint.json"
    save_checkpoint(
        path,
        {"generation": 1},
        {"source": "calendar-a", "assets": [{"name": "events.json", "sha256": "aa"}]},
    )
    save_checkpoint(
        path,
        {"generation": 2},
        {"source": "calendar-b", "assets": [{"name": "events.json", "sha256": "bb"}]},
    )

    # 上一代仍是 calendar-a，但主清单元数据不符时必须直接拒绝，而不是回退。
    with pytest.raises(CheckpointMetadataMismatch):
        load_checkpoint(path, {"source": "calendar-a"})
    with pytest.raises(CheckpointMetadataMismatch):
        load_checkpoint(
            path,
            {"source": "calendar-b", "assets": [{"name": "events.json", "sha256": "aa"}]},
        )
    with pytest.raises(CheckpointMetadataMismatch):
        load_checkpoint(path, {"source": "calendar-b", "split": 1})

    payload, _ = load_checkpoint(
        path,
        {"source": "calendar-b", "assets": [{"name": "events.json", "sha256": "bb"}]},
    )
    assert payload == {"generation": 2}


def test_incomplete_generation_is_not_promoted_to_previous(tmp_path):
    path = tmp_path / "checkpoint.json"
    first = save_checkpoint(path, {"generation": 1}, {"tag": "a"})
    second = save_checkpoint(path, {"generation": 2}, {"tag": "a"})

    # 主代 blob 位翻转（大小不变），保存下一代时不能被当成"上一完整代"。
    blob = tmp_path / second["state"]["file"]
    broken = bytearray(blob.read_bytes())
    broken[0] ^= 0xFF
    blob.write_bytes(bytes(broken))
    save_checkpoint(path, {"generation": 3}, {"tag": "a"})

    previous = json.loads((tmp_path / "checkpoint.previous.json").read_text(encoding="utf-8"))
    assert previous["state"]["file"] == first["state"]["file"]
    evidence = sorted(tmp_path.glob("checkpoint.corrupt-*.json"))
    assert len(evidence) == 1
    assert (
        json.loads(evidence[0].read_text(encoding="utf-8"))["state"]["file"]
        == second["state"]["file"]
    )

    # 再破坏当前代：回退目标是完整的第 1 代，而不是已损坏的第 2 代。
    current = json.loads(path.read_text(encoding="utf-8"))
    (tmp_path / current["state"]["file"]).write_bytes(b"broken")
    payload, manifest = load_checkpoint(path, {"tag": "a"})
    assert payload == {"generation": 1}
    assert manifest["state"]["file"] == first["state"]["file"]


def test_save_over_corrupt_main_preserves_evidence_and_previous(tmp_path):
    path = tmp_path / "checkpoint.json"
    save_checkpoint(path, {"generation": 1}, {"tag": "a"})
    save_checkpoint(path, {"generation": 2}, {"tag": "a"})
    previous_before = (tmp_path / "checkpoint.previous.json").read_bytes()

    path.write_text("{broken", encoding="utf-8")
    written = save_checkpoint(path, {"generation": 3}, {"tag": "a"})

    evidence = sorted(tmp_path.glob("checkpoint.corrupt-*.json"))
    assert len(evidence) == 1
    assert evidence[0].read_text(encoding="utf-8") == "{broken"
    # 损坏的主清单不能顶掉上一完整代。
    assert (tmp_path / "checkpoint.previous.json").read_bytes() == previous_before

    payload, manifest = load_checkpoint(path, {"tag": "a"})
    assert payload == {"generation": 3}
    assert manifest == written


class _Sink:
    """持有不可 pickle 的锁，模拟日志句柄/数据库连接等外部资源。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: list[str] = []


def test_externals_restore_by_identity(tmp_path):
    sink = _Sink()
    path = tmp_path / "checkpoint.json"

    with pytest.raises(CheckpointError):
        save_checkpoint(path, {"sink": sink, "value": 3}, {"tag": "ext"})
    assert not path.exists()

    written = save_checkpoint(
        path,
        {"sink": sink, "value": 3},
        {"tag": "ext"},
        externals={"sink": sink},
    )
    assert written["externals"] == ["sink"]

    restored, manifest = load_checkpoint(path, {"tag": "ext"}, externals={"sink": sink})
    assert restored["sink"] is sink
    assert manifest["externals"] == ["sink"]

    with pytest.raises(CheckpointError):
        load_checkpoint(path, {"tag": "ext"})

"""检查点保留策略：只留可加载的两代，其余 blob 与 metrics 备份由 prune 回收。

测试直接用 ``save_checkpoint`` 生成小 payload 的多代快照，并按 ``CheckpointControl``
的方式把每代的 metrics 备份登记进 ``checkpoint_assets``；不启动仿真，也不读真实 run。

约束（与主控实现约定一致）：

* 只清理当前清单目录下形如 ``<stem>.state-<32hex>.pkl`` 与 ``metrics-<32hex>.sqlite3`` 的文件；
* 两个保留代（``latest.json`` / ``latest.previous.json``）引用的状态与 asset 一律不删；
* 两代的 blob 与本目录 asset 的大小/sha256 未全部通过时抛 ``CheckpointError`` 且不删任何文件；
* 比保留代更新的未引用 blob 暂留，避免误删尚未提交的那一代；
* 不递归、不碰外部目录、未知文件与符号链接。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from fabcosim.cosim.checkpoint import (
    CheckpointError,
    load_checkpoint,
    prune_checkpoint_generations,
    save_checkpoint,
)

TAG = {"tag": "retention"}


def _asset(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _generation(directory: Path, manifest_path: Path, number: int) -> tuple[str, str]:
    """写一代快照，返回 ``(状态 blob 名, metrics 备份名)``。"""
    metrics = directory / f"metrics-{uuid.uuid4().hex}.sqlite3"
    metrics.write_bytes(f"metrics-generation-{number}".encode())
    manifest = save_checkpoint(
        manifest_path,
        {"generation": number},
        {**TAG, "checkpoint_assets": [_asset(metrics)]},
    )
    return manifest["state"]["file"], metrics.name


def test_prune_keeps_two_loadable_generations_and_removes_older_blobs(tmp_path):
    directory = tmp_path / "checkpoints"
    directory.mkdir()
    manifest_path = directory / "latest.json"

    states: list[str] = []
    metrics: list[str] = []
    for number in range(1, 5):
        state, metric = _generation(directory, manifest_path, number)
        states.append(state)
        metrics.append(metric)

    # 保留代之外的干扰文件：未知文件、别的 stem、子目录、外部旧 run。
    unknown = directory / "notes.txt"
    unknown.write_text("keep me", "utf-8")
    other_stem = directory / f"manual.state-{uuid.uuid4().hex}.pkl"
    other_stem.write_bytes(b"manual")
    nested = directory / "events.parts" / f"latest.state-{uuid.uuid4().hex}.pkl"
    nested.parent.mkdir()
    nested.write_bytes(b"nested")
    external = tmp_path / "old-run" / "checkpoints"
    external.mkdir(parents=True)
    external_state = external / f"latest.state-{uuid.uuid4().hex}.pkl"
    external_state.write_bytes(b"external")
    external_metrics = external / f"metrics-{uuid.uuid4().hex}.sqlite3"
    external_metrics.write_bytes(b"external")

    doomed = [
        directory / states[0], directory / metrics[0],
        directory / states[1], directory / metrics[1],
    ]
    sizes = {path.name: path.stat().st_size for path in doomed}

    result = prune_checkpoint_generations(manifest_path)

    assert {Path(name).name for name in result["deleted_files"]} == set(sizes)
    assert result["deleted_bytes"] == sum(sizes.values())

    # 两个保留代的状态与 asset 必须还在，且旧代被清掉。
    for name in (states[2], states[3], metrics[2], metrics[3],
                 "latest.json", "latest.previous.json"):
        assert (directory / name).is_file(), name
    for name in sizes:
        assert not (directory / name).exists(), name

    # 目录外、子目录、未知文件名一概不碰。
    for path in (unknown, other_stem, nested, external_state, external_metrics):
        assert path.is_file(), path

    # 主清单可用；主 blob 损坏后必须还能回退到上一代，说明保留的两代都完整。
    payload, _ = load_checkpoint(manifest_path, TAG)
    assert payload == {"generation": 4}
    latest_blob = directory / states[3]
    latest_blob.write_bytes(latest_blob.read_bytes() + b"\x00")
    payload, manifest = load_checkpoint(manifest_path, TAG)
    assert payload == {"generation": 3}
    assert manifest["state"]["file"] == states[2]


def test_prune_refuses_to_delete_when_a_retained_blob_is_corrupt(tmp_path):
    directory = tmp_path / "checkpoints"
    directory.mkdir()
    manifest_path = directory / "latest.json"

    states: list[str] = []
    metrics: list[str] = []
    for number in range(1, 4):
        state, metric = _generation(directory, manifest_path, number)
        states.append(state)
        metrics.append(metric)

    # 上一代（第 2 代）的 blob 被改坏：保留代不再完整，prune 必须整体放弃。
    broken = directory / states[1]
    broken.write_bytes(broken.read_bytes() + b"\x00")

    with pytest.raises(CheckpointError):
        prune_checkpoint_generations(manifest_path)

    for name in (*states, *metrics):
        assert (directory / name).is_file(), name

    # 主代仍然可加载，失败只影响回收，不破坏恢复链。
    payload, _ = load_checkpoint(manifest_path, TAG)
    assert payload == {"generation": 3}


def test_prune_keeps_unreferenced_blob_newer_than_retained_generations(tmp_path):
    directory = tmp_path / "checkpoints"
    directory.mkdir()
    manifest_path = directory / "latest.json"

    states: list[str] = []
    for number in range(1, 3):
        state, _ = _generation(directory, manifest_path, number)
        states.append(state)

    # 模拟"刚写完、清单还没替换"的一代：名字合法，但比两个保留代都新。
    inflight = directory / f"latest.state-{uuid.uuid4().hex}.pkl"
    inflight.write_bytes(b"in-flight generation")
    newest = max((directory / name).stat().st_mtime_ns for name in states)
    stamp = newest + 1_000_000_000
    os.utime(inflight, ns=(stamp, stamp))

    result = prune_checkpoint_generations(manifest_path)

    assert inflight.is_file()
    assert Path(inflight).name not in {Path(name).name for name in result["deleted_files"]}
    assert result["deleted_bytes"] == 0


def test_prune_is_noop_for_single_generation_and_ignores_links_and_subdirs(tmp_path):
    directory = tmp_path / "checkpoints"
    directory.mkdir()
    manifest_path = directory / "latest.json"
    state, metric = _generation(directory, manifest_path, 1)

    nested = directory / "segments" / f"latest.state-{uuid.uuid4().hex}.pkl"
    nested.parent.mkdir()
    nested.write_bytes(b"segment")

    # Windows 未开开发者模式时建不了符号链接，此时跳过该断言而不是让用例失败。
    target = tmp_path / "outside.sqlite3"
    target.write_bytes(b"outside")
    link = directory / f"metrics-{uuid.uuid4().hex}.sqlite3"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        link = None

    result = prune_checkpoint_generations(manifest_path)

    assert result["deleted_files"] == []
    assert result["deleted_bytes"] == 0
    assert (directory / state).is_file()
    assert (directory / metric).is_file()
    assert nested.is_file()
    if link is not None:
        assert link.is_symlink() and target.is_file()

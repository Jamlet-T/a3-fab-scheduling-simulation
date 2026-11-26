"""lot RNG 只在投片后存在：投片时刻创建、完工回收，summary 用ID索引取release_s。

旧实现把每个lot的3个流在初始化时一次建好（即使投片在很久以后），且summary对每个lot
用next()线性扫config["lots"]（O(N²)）。这里直接观察random_streams的生命周期，并核对
summary的release_s与声明一致；不复制生产代码。
"""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from fabcosim.cosim import full
from fabcosim.data.full_route import compile_full_routes

ROOT = Path(__file__).resolve().parents[1]
PURPOSES = ("sampling", "rework", "processing")


def scenario(releases: list[float]) -> dict:
    """两步短路线 + 指定投片时刻；构造方式与test_completed_task_retention一致。"""
    config = json.loads((ROOT / "configs/smat_full_product1.json").read_text("utf-8"))
    resolved = compile_full_routes(config, ROOT)
    operations = [
        copy.deepcopy(resolved["routes"]["Route_Product_1"]["operations"][index])
        for index in (369, 370)
    ]
    for source_step, op in enumerate(operations, 1):
        op.update(
            source_step=source_step,
            processing_s=1,
            offset_s=0,
            interval_s=0,
            sample_probability=1,
            rework_probability=0,
            rework_step=0,
            dedication_step=0,
            cqt_step=0,
        )
    resolved["routes"]["Route_Product_1"]["operations"] = operations
    template = resolved["lots"][0]
    resolved["lots"] = [
        dict(template, id=f"L{index}", release_s=release) for index, release in enumerate(releases)
    ]
    resolved["max_time_s"] = 200000
    resolved["observation"] = None
    resolved["replay"] = {"enabled": False}
    resolved["performance"] = {
        "stream_events": False,
        "route_event_geometry": False,
        "compact_json": True,
    }
    return resolved


def install_probe(monkeypatch, resolved: dict) -> dict:
    """替换full.random.Random：记录建流时刻，并在每次抽样时核对生命周期。

    种子到(lot, purpose)的映射由同一SHA256公式独立算出，确保建出来的流就是原来的流。
    """
    seeds = {
        hashlib.sha256(f"{resolved['seed']}:{definition['id']}:{purpose}".encode()).digest(): (
            definition["id"],
            purpose,
        )
        for definition in resolved["lots"]
        for purpose in PURPOSES
    }
    lot_ids = [definition["id"] for definition in resolved["lots"]]
    observed = {"created": {}, "order": [], "streams": None, "env": None, "states": None}

    def audit() -> None:
        streams, states = observed["streams"], observed["states"]
        for lot_id in lot_ids:
            keys = [key for key in streams if key[0] == lot_id]
            state = states.get(lot_id)
            if state is None:
                assert not keys, f"{lot_id} 状态未建却有RNG：{keys}"
            elif state["done"].triggered:
                assert not keys, f"{lot_id} 完工后仍保留RNG：{keys}"
            elif state.get("actual_release_s") is not None:
                assert len(keys) == len(PURPOSES), f"{lot_id} 已投片但RNG不完整：{keys}"
            else:
                assert not keys, f"{lot_id} 尚未投片却已有RNG：{keys}"

    class ProbeRandom:
        def __init__(self, seed):
            frame = sys._getframe(1)
            while frame is not None and "random_streams" not in frame.f_locals:
                frame = frame.f_back
            assert frame is not None, "RNG不在可观察的帧内创建"
            observed["streams"] = frame.f_locals["random_streams"]
            observed["env"] = frame.f_locals["env"]
            observed["states"] = frame.f_locals["states"]
            self.key = seeds[seed]
            observed["created"].setdefault(self.key, []).append(observed["env"].now)
            observed["order"].append(self.key)

        def random(self):
            audit()
            return 0.5

        def uniform(self, low, high):
            audit()
            return (low + high) / 2

    monkeypatch.setattr(full.random, "Random", ProbeRandom)
    return observed


def test_rng_is_created_at_release_and_reclaimed_at_completion(monkeypatch, tmp_path):
    releases = [0, 5, 20]
    resolved = scenario(releases)
    observed = install_probe(monkeypatch, resolved)
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kwargs: resolved)

    summary = full.run_full(copy.deepcopy(resolved), tmp_path / "lifecycle")
    results = {item["lot_id"]: item for item in summary["lot_results"]}

    assert summary["completed_lots"] == len(releases)
    for index, release in enumerate(releases):
        lot_id = f"L{index}"
        result = results[lot_id]
        assert result["phase"] == "completed"
        assert result["release_s"] == release
        assert result["actual_release_s"] >= release
        # 不是初始化时建流：每个purpose恰好在其投片时刻创建一次。
        for purpose in PURPOSES:
            assert observed["created"][(lot_id, purpose)] == [result["actual_release_s"]]
        # 每lot仍按sampling/rework/processing的顺序建流（现有测试依赖该顺序）。
        assert [key for key in observed["order"] if key[0] == lot_id] == [
            (lot_id, purpose) for purpose in PURPOSES
        ]

    # 全部完工后三个流全部回收，表中不留任何lot的RNG。
    assert observed["streams"] == {}


def test_never_released_lot_never_gets_an_rng(monkeypatch, tmp_path):
    resolved = scenario([0, 1_000_000_000])
    resolved["max_time_s"] = 200000
    observed = install_probe(monkeypatch, resolved)
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kwargs: resolved)

    with pytest.raises(TimeoutError):
        full.run_full(copy.deepcopy(resolved), tmp_path / "horizon")

    # L0已完工并回收，L1在仿真期内从未投片，因此从未建流。
    assert {lot_id for lot_id, _ in observed["order"]} == {"L0"}
    assert observed["streams"] == {}


def test_duplicate_lot_ids_are_still_rejected(monkeypatch, tmp_path):
    resolved = scenario([0, 0])
    resolved["lots"].append(dict(resolved["lots"][0]))
    monkeypatch.setattr(full, "compile_full_routes", lambda *args, **kwargs: resolved)

    with pytest.raises(ValueError, match="lot ID重复"):
        full.run_full(copy.deepcopy(resolved), tmp_path / "duplicate")

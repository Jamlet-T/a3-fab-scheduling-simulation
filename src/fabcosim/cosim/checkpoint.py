"""保存与恢复真实 SimPy 运行图的检查点。

这不是"从随机种子重放历史"，也不是把仿真状态重新描述成业务结构后再重建事件。
检查点在两次 ``env.step()`` 之间直接序列化当前对象图：原生 :class:`simpy.Process`
（其 ``_generator`` 是本项目 :class:`fabcosim.production.routine.Routine` 之类的
普通对象，不持有 Python 生成器帧）、``env._queue`` 中的待处理事件及其序号、
``Resource`` 的 ``users`` / ``queues``、本地闭包、以及 ``random.Random`` 的完整状态。
恢复后由原生 SimPy 继续推进，事件后缀与连续运行逐项一致。

适用范围（重要）
----------------
* 仅用于**本项目、本机、可信**的快照。反序列化会执行 pickle 载荷，不能读取来源不明的文件。
* 保存点必须在两次 ``env.step()`` 之间；若 ``Environment._active_proc`` 非 ``None``
  （正在 step 或事件回调内部），保存会直接拒绝，避免截到半个事件。
* 主控负责把运行中的 Python 生成器改成 ``Routine`` 对象。若对象图里仍有活动生成器，
  序列化会失败并给出提示，本模块不做隐式转换。
* 外部资源（日志句柄、数据库连接、SUMO 实例等）不可直接 pickle 时，由调用方通过
  ``externals`` 以"对象身份 → 持久 ID"传入；保存时按其 ``id()`` 替换为持久 ID，
  读取时用同名对象还原。调用方负责在保存前旋转/备份这些资源，并在读取时重建同名对象。

文件格式
--------
``path`` 是 JSON 清单，最后原子替换；状态是一个独立且唯一的 ``*.pkl`` blob，
先写临时文件、``fsync`` 后 ``rename``。清单记录格式版本、创建时间、Python / SimPy /
cloudpickle 版本、blob 的 sha256 与大小、实际引用的 ``externals`` 名称以及调用方元数据。
上一代清单保留为 ``<stem>.previous.json``；主清单损坏时可回退到上一完整代。
读取时先校验清单结构、运行时版本、调用方元数据和全部 blob 的 sha256/大小，再反序列化。
损坏的主清单不会被覆盖：保存时若发现主清单不可用，会另存为
``<stem>.corrupt-<时间戳>-<uuid>.json`` 并保留原 ``.previous.json``。
旧 blob 暂不清理，便于回溯。
"""

from __future__ import annotations

import datetime
import hashlib
import io
import itertools
import json
import os
import pickle
import platform
import random
import re
import struct
import sys
import uuid
import warnings
from collections import ChainMap
from pathlib import Path
from typing import Any, Callable

import cloudpickle
import simpy
from simpy.core import BoundClass, Environment
from simpy.events import PENDING

FORMAT = "fabcosim-checkpoint"
FORMAT_VERSION = 1
_PICKLE_PROTOCOL = 5

# simpy 4.1.2 的 Environment.__init__ 会通过 BoundClass.bind_early 把下列工厂方法缓存
# 到实例字典里。保存时丢掉这些缓存、读取时重新绑定，避免把绑定方法当成状态。
_CACHED_FACTORIES = ("process", "timeout", "event", "all_of", "any_of")
_ENVIRONMENT_FIELDS = ("_now", "_queue", "_eid", "_active_proc")

_PENDING_PID = ("simpy.events", "PENDING")
_EXTERNAL_TAG = "fabcosim.external"

_COUNT_REPR = re.compile(r"^count\((-?\d+)(?:, (-?\d+))?\)$")
_RNG_STATE_WORDS = 625


class CheckpointError(RuntimeError):
    """检查点保存或读取失败。"""


class CheckpointCorruptError(CheckpointError):
    """清单或状态 blob 缺失、损坏或校验不通过。"""


class CheckpointIncompatibleError(CheckpointError):
    """检查点由不兼容的格式或运行时写入，不能安全恢复。"""


class CheckpointMetadataMismatch(CheckpointError):
    """调用方元数据与清单不一致（来源可能已变化），拒绝加载。"""


# ---------------------------------------------------------------------------
# 序列化 reducers
# ---------------------------------------------------------------------------


def _new_environment(cls: type) -> Environment:
    return cls.__new__(cls)


def _set_environment_state(env: Environment, state: dict) -> None:
    """还原 Environment 并重新执行 BoundClass.bind_early。"""
    core = state["core"]
    for key, value in state["extra"].items():
        setattr(env, key, value)
    env._now = core["_now"]
    env._queue = core["_queue"]
    env._eid = itertools.count(core["_eid_next"])
    env._active_proc = core["_active_proc"]
    for name in _CACHED_FACTORIES:
        env.__dict__.pop(name, None)
    BoundClass.bind_early(env)


def _peek_counter(counter: Any, queue: list) -> int:
    """读取 itertools.count 的下一值而不推进原计数器。"""
    match = _COUNT_REPR.match(repr(counter))
    if match is not None:
        return int(match.group(1))
    with warnings.catch_warnings():
        # CPython 3.12 起对 itertools 的 reduce/copy 发出弃用警告。
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            factory, args = counter.__reduce__()
        except Exception:
            factory, args = None, ()
    if factory is itertools.count and args:
        return int(args[0])
    # 保守退化：队列中已发出的最大序号之后。相对顺序不变，但未来序号可能比连续运行小。
    return max((entry[2] for entry in queue), default=-1) + 1


def _reduce_environment(env: Environment) -> tuple[Any, ...]:
    """把 Environment 拆成无参外壳 + 状态，避免队列经事件反向引用造成 reduce 递归。

    SimPy 4.1.2 在 CPython 3.12 下缓存到实例上的 BoundClass 绑定方法本身可以 pickle，
    但这里仍统一丢弃并在读取时 ``BoundClass.bind_early`` 重建：既不依赖该实现细节，
    也不依赖 ``itertools.count`` 已弃用的 pickle 支持。
    """
    if env._active_proc is not None:
        raise CheckpointError(
            "保存点位于事件处理过程中（_active_proc 非空）；请在两次 env.step() 之间保存。"
        )
    extra = {
        key: value
        for key, value in env.__dict__.items()
        if key not in _ENVIRONMENT_FIELDS and key not in _CACHED_FACTORIES
    }
    state = {
        "core": {
            "_now": env._now,
            "_queue": env._queue,
            "_eid_next": _peek_counter(env._eid, env._queue),
            "_active_proc": None,
        },
        "extra": extra,
    }
    # 第 6 项 state_setter 让 pickle 在 memo 之后才写状态，环形引用才能正确还原。
    return (_new_environment, (type(env),), state, None, None, _set_environment_state)


def _rebuild_random(cls: type, version: int, packed: bytes, gauss: Any) -> random.Random:
    rng = random.Random.__new__(cls)
    rng.setstate((version, struct.unpack(f"<{len(packed) // 4}I", packed), gauss))
    return rng


def _reduce_random(rng: random.Random) -> tuple[Callable, tuple]:
    """625 个 uint32 打包成 bytes，避免 pickle 为每个状态整数保留 memo 条目。"""
    version, state, gauss = rng.getstate()
    if version != 3 or len(state) != _RNG_STATE_WORDS:
        raise CheckpointError(f"不认识的 random.Random 状态：version={version}, words={len(state)}")
    packed = struct.pack(f"<{len(state)}I", *state)
    return (_rebuild_random, (type(rng), version, packed, gauss))


class _CheckpointPickler(cloudpickle.CloudPickler):
    dispatch_table = ChainMap(
        {Environment: _reduce_environment, random.Random: _reduce_random},
        cloudpickle.CloudPickler.dispatch_table,
    )

    def __init__(self, file: io.BytesIO, externals: dict[str, Any]) -> None:
        super().__init__(file, protocol=_PICKLE_PROTOCOL)
        self._external_ids = {id(value): key for key, value in externals.items()}
        self.used_externals: list[str] = []

    def persistent_id(self, obj: Any) -> Any:
        if obj is PENDING:
            return _PENDING_PID
        key = self._external_ids.get(id(obj))
        if key is None:
            return None
        if key not in self.used_externals:
            self.used_externals.append(key)
        return (_EXTERNAL_TAG, key)


class _CheckpointUnpickler(pickle.Unpickler):
    def __init__(self, file: io.BytesIO, externals: dict[str, Any]) -> None:
        super().__init__(file)
        self._externals = externals

    def persistent_load(self, pid: Any) -> Any:
        if pid == _PENDING_PID:
            return PENDING
        if isinstance(pid, tuple) and len(pid) == 2 and pid[0] == _EXTERNAL_TAG:
            key = pid[1]
            if key not in self._externals:
                raise CheckpointError(f"缺少外部对象 externals[{key!r}]，拒绝反序列化。")
            return self._externals[key]
        raise pickle.UnpicklingError(f"未知的 persistent id：{pid!r}")


def _dumps(payload: Any, externals: dict[str, Any]) -> tuple[bytes, list[str]]:
    buffer = io.BytesIO()
    pickler = _CheckpointPickler(buffer, externals)
    try:
        pickler.dump(payload)
    except CheckpointError:
        raise
    except Exception as exc:
        hint = ""
        if "generator" in str(exc).lower():
            hint = "；运行中的 Python 生成器不能保存，请先替换为普通 Routine 对象"
        else:
            hint = "；不可序列化的外部资源请通过 externals 传入"
        raise CheckpointError(f"无法序列化检查点：{exc}{hint}") from exc
    return buffer.getvalue(), pickler.used_externals


def _loads(data: bytes, externals: dict[str, Any]) -> Any:
    return _CheckpointUnpickler(io.BytesIO(data), externals).load()


# ---------------------------------------------------------------------------
# 清单与文件
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _previous_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.previous{path.suffix}")


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")


def _fsync_dir(directory: Path) -> None:
    """尽力同步目录项；Windows 不允许以只读方式打开目录，失败时忽略。"""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(temp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise CheckpointError(f"写入 {path} 失败：{exc}") from exc
    _fsync_dir(path.parent)


def _assert_string_keys(value: Any, where: str = "metadata") -> None:
    """JSON 会把非字符串键静默转成字符串；这里显式拒绝，避免来源信息被悄悄改名。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CheckpointError(f"{where} 的键必须是字符串：{key!r}")
            _assert_string_keys(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_string_keys(item, f"{where}[{index}]")


def _validate_externals(externals: dict[str, Any] | None) -> dict[str, Any]:
    if externals is None:
        return {}
    if not isinstance(externals, dict):
        raise TypeError("externals 必须是 {名称: 对象} 字典")
    for key in externals:
        if not isinstance(key, str) or not key:
            raise TypeError("externals 的键必须是非空字符串")
    if len({id(value) for value in externals.values()}) != len(externals):
        raise ValueError("externals 中同一个对象不能对应多个持久 ID")
    return externals


def _manifest_bytes(manifest: dict) -> bytes:
    try:
        text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CheckpointError(f"清单无法写成 JSON：{exc}") from exc
    return (text + "\n").encode("utf-8")


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise CheckpointCorruptError("清单不是 JSON 对象")
    if manifest.get("format") != FORMAT:
        raise CheckpointCorruptError(f"清单格式标识不是 {FORMAT!r}")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise CheckpointIncompatibleError(
            f"检查点格式版本是 {manifest.get('format_version')!r}，当前只支持 {FORMAT_VERSION}"
        )
    if not isinstance(manifest.get("created_at"), str):
        raise CheckpointCorruptError("清单缺少 created_at")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or not all(
        isinstance(runtime.get(name), str) for name in ("python", "simpy", "cloudpickle")
    ):
        raise CheckpointCorruptError("清单缺少完整的 runtime 版本信息")
    state = manifest.get("state")
    if not isinstance(state, dict):
        raise CheckpointCorruptError("清单缺少 state")
    name = state.get("file")
    if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
        raise CheckpointCorruptError(f"非法的状态文件名：{name!r}")
    digest = state.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise CheckpointCorruptError("清单中的 sha256 非法")
    if not isinstance(state.get("size"), int) or isinstance(state.get("size"), bool):
        raise CheckpointCorruptError("清单中的 blob 大小非法")
    if state["size"] < 0:
        raise CheckpointCorruptError("清单中的 blob 大小为负")
    if not isinstance(manifest.get("metadata"), dict):
        raise CheckpointCorruptError("清单缺少 metadata 对象")
    used = manifest.get("externals")
    if not isinstance(used, list) or not all(isinstance(name, str) for name in used):
        raise CheckpointCorruptError("清单中的 externals 列表非法")


def _validate_runtime(runtime: dict) -> None:
    try:
        saved_python = tuple(int(part) for part in runtime["python"].split(".")[:2])
        saved_cloudpickle = tuple(int(part) for part in runtime["cloudpickle"].split(".")[:2])
    except (KeyError, ValueError) as exc:
        raise CheckpointCorruptError(f"清单中的运行版本无法解析：{exc}") from exc
    if saved_python != sys.version_info[:2]:
        raise CheckpointIncompatibleError(
            f"检查点由 Python {runtime['python']} 写入，当前为 {platform.python_version()}"
        )
    if runtime["simpy"] != simpy.__version__:
        raise CheckpointIncompatibleError(
            f"检查点由 SimPy {runtime['simpy']} 写入，当前为 {simpy.__version__}"
        )
    current_cloudpickle = tuple(int(part) for part in cloudpickle.__version__.split(".")[:2])
    if saved_cloudpickle != current_cloudpickle:
        raise CheckpointIncompatibleError(
            f"检查点由 cloudpickle {runtime['cloudpickle']} 写入，当前为 {cloudpickle.__version__}"
        )


def _read_manifest(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CheckpointCorruptError(f"无法读取清单 {path}：{exc}") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointCorruptError(f"清单 {path} 不是合法 JSON：{exc}") from exc
    _validate_manifest(manifest)
    _validate_runtime(manifest["runtime"])
    return manifest


def _read_state_blob(manifest_path: Path, manifest: dict) -> bytes:
    info = manifest["state"]
    blob_path = manifest_path.parent / info["file"]
    try:
        data = blob_path.read_bytes()
    except OSError as exc:
        raise CheckpointCorruptError(f"无法读取状态 blob {blob_path}：{exc}") from exc
    if len(data) != info["size"]:
        raise CheckpointCorruptError(
            f"状态 blob {blob_path} 大小不符：{len(data)} != {info['size']}"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != info["sha256"]:
        raise CheckpointCorruptError(
            f"状态 blob {blob_path} 的 sha256 不符：{digest} != {info['sha256']}"
        )
    return data


def _check_metadata(actual: dict, expected: dict, where: str) -> None:
    """expected_metadata 是"子集断言"：给出的键必须存在且逐类型一致。"""
    problem = _metadata_difference(expected, actual, "$")
    if problem is not None:
        raise CheckpointMetadataMismatch(f"{where} 的元数据与调用方期望不一致：{problem}")


def _metadata_difference(expected: Any, actual: Any, path: str) -> str | None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{path} 期望对象，实际是 {type(actual).__name__}"
        for key, value in expected.items():
            if key not in actual:
                return f"{path}.{key} 缺失"
            problem = _metadata_difference(value, actual[key], f"{path}.{key}")
            if problem is not None:
                return problem
        return None
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, list):
            return f"{path} 期望列表，实际是 {type(actual).__name__}"
        if len(expected) != len(actual):
            return f"{path} 长度不一致：{len(actual)} != {len(expected)}"
        for index, value in enumerate(expected):
            problem = _metadata_difference(value, actual[index], f"{path}[{index}]")
            if problem is not None:
                return problem
        return None
    if type(expected) is not type(actual):
        return f"{path} 类型不一致：{type(actual).__name__} != {type(expected).__name__}"
    if expected != actual:
        return f"{path} 值不一致：{actual!r} != {expected!r}"
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rotate_previous(path: Path) -> None:
    """保存前把当前主清单留作上一代；损坏的主清单另存证据，不覆盖可用旧代。

    只有当主清单结构与 blob 的 sha256/大小都通过时才晋升为 ``.previous.json``，
    否则旧代保持不动，损坏清单另存为 ``.corrupt-*``。代价是每次保存要完整读一遍
    上一代 blob；对于大状态文件这是换取"上一代确实完整"的必要开销。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CheckpointError(f"无法读取现有清单 {path}：{exc}") from exc
    usable = False
    try:
        manifest = json.loads(raw.decode("utf-8"))
        _validate_manifest(manifest)
        info = manifest["state"]
        blob = path.parent / info["file"]
        usable = (
            blob.is_file()
            and blob.stat().st_size == info["size"]
            and _file_sha256(blob) == info["sha256"]
        )
    except Exception:
        usable = False
    if usable:
        _atomic_write(_previous_path(path), raw)
        return
    evidence = path.with_name(f"{path.stem}.corrupt-{_stamp()}-{uuid.uuid4().hex[:8]}{path.suffix}")
    _atomic_write(evidence, raw)


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def save_checkpoint(
    path: Path,
    payload: Any,
    metadata: dict,
    *,
    externals: dict[str, Any] | None = None,
) -> dict:
    """把 ``payload``（通常包含 ``env``）原子保存到 ``path``，返回写入的清单。

    * ``metadata`` 必须是 JSON 可序列化字典；若含 ``assets`` 列表，会原样记录，
      供主控附日志/数据库等产物的 sha256。
    * ``externals`` 的键是持久 ID、值是当前进程中的对象；保存时按对象身份替换，
      读取时由调用方用同名对象还原。
    """
    if not isinstance(metadata, dict):
        raise TypeError("metadata 必须是字典")
    path = Path(path)
    externals = _validate_externals(externals)
    _assert_string_keys(metadata)
    try:
        # 先做一次 JSON 往返：尽早拒绝不可序列化元数据，并统一 tuple/list 形态。
        metadata = json.loads(json.dumps(metadata, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise CheckpointError(f"metadata 必须是 JSON 可序列化的字典：{exc}") from exc

    data, used_externals = _dumps(payload, externals)
    path.parent.mkdir(parents=True, exist_ok=True)
    state_name = f"{path.stem}.state-{uuid.uuid4().hex}.pkl"
    _atomic_write(path.parent / state_name, data)

    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": _utc_now(),
        "runtime": {
            "python": platform.python_version(),
            "simpy": simpy.__version__,
            "cloudpickle": cloudpickle.__version__,
            "pickle_protocol": _PICKLE_PROTOCOL,
            "platform": sys.platform,
        },
        "state": {
            "file": state_name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "externals": used_externals,
        "metadata": metadata,
    }
    if path.exists():
        _rotate_previous(path)
    _atomic_write(path, _manifest_bytes(manifest))
    return manifest


def load_checkpoint(
    path: Path,
    expected_metadata: dict,
    *,
    externals: dict[str, Any] | None = None,
    fallback: bool = True,
) -> tuple[Any, dict]:
    """校验清单、元数据与 blob 后反序列化，返回 ``(payload, manifest)``。

    ``expected_metadata`` 是子集断言：给出的键必须存在且类型、值完全一致；
    不一致时**立即拒绝**，不会回退到上一代，避免静默接受来源变化。
    仅当主清单或主 blob 损坏/缺失时，``fallback=True`` 才尝试
    ``<stem>.previous.json``。
    """
    if not isinstance(expected_metadata, dict):
        raise TypeError("expected_metadata 必须是字典")
    path = Path(path)
    externals = _validate_externals(externals)

    candidates = [(path, True)]
    if fallback:
        candidates.append((_previous_path(path), False))

    problems: list[str] = []
    for candidate, is_primary in candidates:
        try:
            manifest = _read_manifest(candidate)
        except CheckpointIncompatibleError:
            raise
        except CheckpointCorruptError as exc:
            problems.append(f"{candidate.name}: {exc}")
            if is_primary and not fallback:
                raise
            continue

        _check_metadata(manifest["metadata"], expected_metadata, candidate.name)
        missing = [name for name in manifest["externals"] if name not in externals]
        if missing:
            raise CheckpointError(
                f"{candidate.name} 需要外部对象 {sorted(missing)}；请通过 externals 提供同名对象"
            )
        try:
            data = _read_state_blob(candidate, manifest)
            for asset in manifest["metadata"].get("checkpoint_assets", []):
                try:
                    asset_path = Path(asset["path"])
                    valid = (asset_path.is_file() and asset_path.stat().st_size == asset["size"]
                             and _file_sha256(asset_path) == asset["sha256"])
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise CheckpointCorruptError(f"快照引用的文件无效：{asset!r}") from exc
                if not valid:
                    raise CheckpointCorruptError(f"快照引用的文件损坏或缺失：{asset_path}")
        except CheckpointCorruptError as exc:
            problems.append(f"{candidate.name}: {exc}")
            if is_primary and not fallback:
                raise
            continue
        try:
            payload = _loads(data, externals)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointCorruptError(
                f"反序列化 {candidate.name} 的状态 blob 失败：{exc}"
            ) from exc
        return payload, manifest

    detail = "；".join(problems) if problems else "没有可读的清单"
    raise CheckpointCorruptError(f"没有可用检查点：{detail}")


def prune_checkpoint_generations(path: Path) -> dict:
    """只回收本目录中不再被最新两代引用的状态与统计备份。

    先校验所有保留代，再删除；不递归、不碰事件分段、外部恢复链或未知文件。
    只供单写入者在save_checkpoint成功后调用。更新的未引用blob暂留，避免触及未提交代。
    """
    path = Path(path).resolve()
    directory = path.parent
    keep: set[Path] = set()
    state_times = []
    for manifest_path in (path, _previous_path(path)):
        if manifest_path != path and not manifest_path.exists():
            continue
        manifest = _read_manifest(manifest_path)
        info = manifest["state"]
        state_path = directory / info["file"]
        assets = [{"path": str(state_path), "size": info["size"], "sha256": info["sha256"]}]
        assets.extend(manifest["metadata"].get("checkpoint_assets", []))
        for asset in assets:
            candidate = Path(asset["path"]).resolve()
            keep.add(candidate)
            # 外部日志分段/输入已由保存协议保护；此处只校验会参与本目录回收的状态和备份。
            if candidate.parent == directory:
                if (not candidate.is_file() or candidate.stat().st_size != asset["size"]
                        or _file_sha256(candidate) != asset["sha256"]):
                    raise CheckpointCorruptError(f"保留代文件损坏，取消本次回收：{candidate}")
        state_times.append(state_path.stat().st_mtime_ns)
    pattern = re.compile(rf"(?:{re.escape(path.stem)}\.state-[0-9a-f]{{32}}\.pkl|metrics-[0-9a-f]{{32}}\.sqlite3)")
    victims = []
    for candidate in directory.iterdir():
        if (not pattern.fullmatch(candidate.name) or candidate.is_symlink()
                or not candidate.is_file() or candidate.resolve().parent != directory):
            continue
        if candidate.resolve() in keep or candidate.stat().st_mtime_ns >= min(state_times):
            continue
        victims.append((candidate, candidate.stat().st_size))
    deleted, size = [], 0
    for candidate, length in victims:
        candidate.unlink()
        deleted.append(str(candidate))
        size += length
    return {"deleted_files": deleted, "deleted_bytes": size}

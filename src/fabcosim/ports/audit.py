"""增量物料审计：只复检可能变动的lot，其余沿用上次通过的不变量。

``port_index=False``（默认）与历史行为完全一致：每个边界全量扫描全部
``Port.lot``与全部车辆cargo，只把脏lot、当前/上次被端口持有的lot、当前/上次
在车辆上的lot交给注入的原``_check_material``做完整位置归属校验。

``port_index=True``时只扫描被``Port``回调标记为脏的端口，其余端口沿用上次
已验证的快照（``port -> lot``与全局lot持有数按差量维护），候选为脏端口的
旧/新持有者、脏lot以及前次/当前cargo；全部车辆cargo每边界仍全量扫描，额外
持有数交叉核对也仍按全局计数进行。本模式依赖"物料指针只经属性赋值、
``put``/``take``变更"这一契约：用``object.__setattr__``或直接改``__dict__``
绕过回调会漏检；日检/终态的``full=True``始终真全量扫描并重建缓存，用于兜底。
本模块不复制检查函数的判定。

调用方创建审计器后应把引擎的lot注册表绑定到``audit.lots``（只读映射），
``port_index=True``时把端口注册表绑定到``audit.ports``，并在日检/终态调用
``check(..., full=True)``；默认``full``审计场景不创建本对象。``close()``
解除lot与port上的回调，避免整轮运行结束后仍与审计器互相持有。
"""

from types import MappingProxyType
from typing import Callable, Mapping

from fabcosim.ports.model import Lot, Port

_PORT_SUFFIX = ".port"


class IncrementalMaterialAudit:
    """在初始化后固定的lot/port注册表上做增量物料审计。"""

    def __init__(
        self,
        lots: Mapping[str, Lot],
        ports: Mapping[str, Port],
        checker: Callable[..., None],
        port_index: bool = False,
    ) -> None:
        self._lots: dict[str, Lot] = dict(lots)
        self.lots: Mapping[str, Lot] = MappingProxyType(self._lots)
        self._registered = {id(lot): lot for lot in self._lots.values()}
        # 端口注册表在构造时快照：引擎只改port.lot，不替换端口对象。
        self._ports: dict[str, Port] = dict(ports)
        self.ports: Mapping[str, Port] = MappingProxyType(self._ports)
        self._checker = checker
        self._port_index = port_index
        self._dirty: dict[int, Lot] = {}
        self._held: dict[int, Lot] = {}
        self._cargo: dict[int, Lot] = {}
        # 仅在port_index模式下有意义：上次提交的端口->lot快照与全局持有数。
        self._owner: dict[int, Lot] = {}
        self._holders: dict[int, int] = {}
        self._dirty_ports: dict[int, Port] = {}
        self.checks = 0
        self.full_checks = 0
        self.checked_lots = 0
        self.ports_examined = 0
        self._closed = False
        self._callback = self._mark_dirty
        self._port_callback = self._mark_dirty_port
        if port_index:
            self._validate_port_registry()
        if any(lot._location_callback is not None for lot in self._lots.values()):
            raise ValueError("lot已有位置观察器，不能覆盖现有物料审计")
        if port_index and any(port._lot_callback is not None for port in self._ports.values()):
            raise ValueError("端口已有物料观察器，不能覆盖现有物料审计")
        try:
            for lot in self._lots.values():
                lot._location_callback = self._callback
            if port_index:
                for port in self._ports.values():
                    port._lot_callback = self._port_callback
            self.check({}, None, full=True)
        except BaseException:
            self.close()
            raise

    def _validate_port_registry(self) -> None:
        """固定端口注册表：key与端口ID一一对应、同一对象不重复登记。"""
        by_identity: dict[int, str] = {}
        for key, port in self._ports.items():
            if not isinstance(port, Port):
                raise ValueError(f"端口注册表只接受Port对象：{key!r}")
            previous = by_identity.get(id(port))
            if previous is not None:
                raise ValueError(f"端口注册表重复：{previous!r}与{key!r}指向同一对象")
            by_identity[id(port)] = key
            if port.id != key + _PORT_SUFFIX:
                raise ValueError(f"端口注册表不一致：{key!r} / {port.id!r}")

    def _mark_dirty(self, lot: Lot) -> None:
        """位置变更回调：只登记待复检lot，不验证、不改变赋值顺序、不抛模型错误。"""
        self._dirty[id(lot)] = lot

    def _mark_dirty_port(self, port: Port) -> None:
        """端口物料指针变更回调：只按对象身份登记，不在此读取lot。"""
        self._dirty_ports[id(port)] = port

    def _require_registered(self, lot: Lot, where: str) -> None:
        if self._registered.get(id(lot)) is not lot or self._lots.get(lot.id) is not lot:
            raise RuntimeError(f"物料审计注册表不一致：{where} / {getattr(lot, 'id', lot)!r}")

    def _require_registered_port(self, port: Port) -> None:
        if not isinstance(port, Port):
            raise RuntimeError(f"物料审计注册表不一致：端口{port!r}")
        key = port.id.removesuffix(_PORT_SUFFIX) if port.id.endswith(_PORT_SUFFIX) else None
        if key is None or self._ports.get(key) is not port:
            raise RuntimeError(f"物料审计注册表不一致：端口{port.id}")

    def _expected_ports(self, lot: Lot) -> int:
        location = lot.location
        if not location.endswith(_PORT_SUFFIX):
            return 0
        return int(location[: -len(_PORT_SUFFIX)] in self._ports)

    def check(
        self,
        vehicles: Mapping[str, dict],
        location_cache: dict | None = None,
        full: bool = False,
    ) -> int:
        """复检一个审计边界，返回交给原检查函数的lot数。"""
        if self._closed:
            raise RuntimeError("物料审计已关闭，不能继续检查")
        self.checks += 1
        candidates: dict[int, Lot] = {}
        if full:
            self.full_checks += 1
            candidates.update(self._registered)

        cargo: dict[int, Lot] = {}
        for vehicle_id, vehicle in vehicles.items():
            lot = vehicle["cargo"]
            if lot is None:
                continue
            self._require_registered(lot, f"车辆{vehicle_id}")
            cargo[id(lot)] = lot

        if self._port_index and not full:
            examined, owner, holders, touched = self._scan_dirty_ports()
            # 索引模式只复检脏端口的旧/新持有者，未变端口沿用上次快照。
            candidates.update(touched)
        else:
            examined, owner, holders = self._scan_all_ports()
            candidates.update({id(lot): lot for lot in owner.values()})
            if not full:
                candidates.update(self._held)

        if not full:
            candidates.update(self._dirty)
            candidates.update(cargo)
            candidates.update(self._cargo)

        if candidates:
            for lot in candidates.values():
                self._require_registered(lot, "待复检物料")
            self._checker(
                {lot.id: lot for lot in candidates.values()},
                self._ports,
                vehicles,
                location_cache,
            )
            for ident, lot in candidates.items():
                actual = holders.get(ident, 0)
                expected = self._expected_ports(lot)
                if actual != expected:
                    raise RuntimeError(f"物料审计端口持有数错误：{lot.id} / {actual} != {expected}")
        # 只有本边界全部通过才提交新快照；失败后重试仍须检查同一物料。
        self._commit(owner, holders, cargo)
        self.ports_examined += examined
        self.checked_lots += len(candidates)
        return len(candidates)

    def _commit(
        self, owner: dict[int, Lot], holders: dict[int, int], cargo: dict[int, Lot]
    ) -> None:
        self._dirty = {}
        self._dirty_ports = {}
        self._held = {id(lot): lot for lot in owner.values()}
        self._cargo = cargo
        self._owner = owner
        self._holders = holders

    def _scan_all_ports(self) -> tuple[int, dict[int, Lot], dict[int, int]]:
        holders: dict[int, int] = {}
        owner: dict[int, Lot] = {}
        for port in self._ports.values():
            lot = port.lot
            if lot is None:
                continue
            self._require_registered(lot, f"端口{port.id}")
            owner[id(port)] = lot
            holders[id(lot)] = holders.get(id(lot), 0) + 1
        return len(self._ports), owner, holders

    def _scan_dirty_ports(self) -> tuple[int, dict[int, Lot], dict[int, int], dict[int, Lot]]:
        """在快照副本上应用脏端口差量；失败时不污染已提交快照。

        返回（扫描端口数、新端口->lot快照、新全局持有数、需复检物料）。复检
        物料同时包含脏端口的旧持有者与当前持有者：``port.lot = None``不改
        location时，被摘下的lot只能靠旧持有者进入候选。
        """
        owner = dict(self._owner)
        holders = dict(self._holders)
        touched: dict[int, Lot] = {}
        dirty = self._dirty_ports
        for port in dirty.values():
            self._require_registered_port(port)
        for ident, port in dirty.items():
            previous = self._owner.get(ident)
            if previous is not None:
                touched[id(previous)] = previous
                remaining = holders.get(id(previous), 0) - 1
                if remaining > 0:
                    holders[id(previous)] = remaining
                else:
                    holders.pop(id(previous), None)
                owner.pop(ident, None)
            current = port.lot
            if current is None:
                continue
            self._require_registered(current, f"端口{port.id}")
            touched[id(current)] = current
            owner[ident] = current
            holders[id(current)] = holders.get(id(current), 0) + 1
        return len(dirty), owner, holders, touched

    def close(self) -> None:
        """解除lot与port回调，断开审计器与整轮运行之间的循环引用。"""
        if self._closed:
            return
        for lot in self._lots.values():
            if lot._location_callback is self._callback:
                lot._location_callback = None
        if self._port_index:
            for port in self._ports.values():
                if port._lot_callback is self._port_callback:
                    port._lot_callback = None
        self._dirty.clear()
        self._dirty_ports.clear()
        self._held.clear()
        self._cargo.clear()
        self._owner.clear()
        self._holders.clear()
        self._closed = True

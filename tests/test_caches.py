"""缓存不得改变有向回路、精确偏移或原始表内容；源变更必须失效。"""

import openpyxl
import sumolib

from fabcosim.data.workbook import read_tables
from fabcosim.transport.fleet import build_merge_network
from fabcosim.transport.sumo import _route_between_uncached, route_between


def test_workbook_cache_isolates_rows_and_invalidates_changed_source(tmp_path):
    source = tmp_path / "source.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "table"
    ws.append(["id", "distance", None])
    ws.append(["a", 0.12345678901234567, "last"])
    wb.save(source)
    reference = read_tables(source, {"table": None}, False)
    assert read_tables(source, {"table": None}) == reference
    poisoned = read_tables(source, {"table": None})
    poisoned["table"][0]["distance"] = 999
    assert read_tables(source, {"table": None}) == reference
    ws["B2"] = 2
    wb.save(source)
    assert read_tables(source, {"table": None})["table"][0]["distance"] == 2


def test_exact_route_cache_preserves_upstream_loop_and_does_not_reuse_nearby_position(tmp_path):
    config = {"oht": {"max_speed_m_s": 1.5}}
    net = sumolib.net.readNet(str(build_merge_network(config, tmp_path)))
    goal = {"edge": "e0", "position_m": 2}
    for position in (5, 5.0000000001, 1, 1.0):
        start = {"edge": "e0", "position_m": position}
        expected = _route_between_uncached(net, start, goal)
        assert route_between(net, start, goal) == expected
        assert type(route_between(net, start, goal)[1]) is type(expected[1])
        cached = route_between(net, start, goal)
        assert cached == expected
        cached[0].clear()
        assert route_between(net, start, goal) == expected
        if position > 2:
            assert expected[0][0] == expected[0][-1] == "e0" and len(expected[0]) > 1

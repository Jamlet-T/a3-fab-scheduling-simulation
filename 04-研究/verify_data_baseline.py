"""Verify frozen artifacts without regenerating the hash manifest."""
from pathlib import Path
import csv
import hashlib
import json
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '03-数据/基准-v1'
manifest = json.loads((BASE/'manifest.json').read_text(encoding='utf-8'))
for entry in manifest['sources'] + manifest['derived']:
    path = ROOT / entry['path']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256'], entry['path']
def read(name):
    with (BASE/name).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))
mapping = read('toolgroup_mapping.csv')
physical = [r for r in mapping if r['action'] != 'virtual_buffer_stage_not_physical_tool']
assert len(physical) == 105
assert sum(int(r['production_count']) for r in physical) == 1068
assert all(r['production_count'] == r['spatial_count'] for r in physical)
assert [(r['production_group'],r['spatial_group']) for r in mapping if r['action']=='explicit_alias'] == [('DefMEt_FE_118','DefMet_FE_118')]
operations = read('route_operations.csv')
route_names = {r['_source_sheet'] for r in operations}
assert len(route_names) == 13
assert len({(r['_source_sheet'],r['STEP']) for r in operations}) == len(operations)
assert all(r['_mapped_toolgroup'] in {p['spatial_group'] for p in physical}|{'Delay_32'} for r in operations)
events = []
for name, expected in [('production_release.csv',167129),('engineering_release.csv',33438)]:
    rows = read(name)
    assert len(rows) == expected
    assert len({r['_event_id'] for r in rows}) == len(rows)
    assert all(r['ROUTE NAME'] in route_names for r in rows)
    assert all(r['START DATE'] <= r['DUE DATE'] for r in rows)
    if name.startswith('engineering'):
        assert all(r['ROUTE NAME'] == 'Route_Product_E'+r['PRODUCT NAME'].split('_')[-1] for r in rows)
        assert all(r['_raw_route_name'] == 'Route_Product_'+r['PRODUCT NAME'].split('_')[-1] for r in rows)
    events.extend(rows)
assert len({r['_event_id'] for r in events}) == len(events)
audit = json.loads((BASE/'audit.json').read_text(encoding='utf-8'))
assert not any(audit[k] for k in ('mapping_errors','unmapped_spatial_groups','route_error_tables','release_errors'))
assert all(r['repeat_group_visits_beyond_first'] > 0 for r in audit['routes'].values())
result = {'baseline_id':manifest['baseline_id'],'source_and_derived_hashes_verified':len(manifest['sources'])+len(manifest['derived']),
    'physical_groups':105,'physical_tools':1068,'routes':13,'operations':len(operations),
    'release_events':len(events),'result':'verified',
    'scope':'source integrity, mapping, route references, fixed release selection; not a simulation correctness test'}
(BASE/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))

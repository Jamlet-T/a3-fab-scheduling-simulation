"""Freeze source hashes and audit the full official Dataset 4 against SMAT2022."""
from pathlib import Path
import collections
import csv
import hashlib
import json
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from openpyxl.utils.datetime import from_excel

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '03-数据/基准-v1'
OUT.mkdir(parents=True, exist_ok=True)
PROD = next((ROOT / '03-数据/SMT2020-official/extracted').rglob('SMT_2020_Model_Data_-_LVHM_E.xlsx'))
AMHS = ROOT / '03-数据/SMAT2022/General Data/dataset 4/SMAT_2022_Model_Data_-_LVHM_E.xlsx'
PART = ROOT / '03-数据/SMT2020-official/extracted/SMT_2020 - Final/AutoSched/dataset 4/LVHM_E_Model/LVHM_E_Model.asd/part.txt'
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
M = '{' + NS['m'] + '}'

def read_tables(path):
    """Read stored values, not Excel dimension hints or formatting-only cells."""
    tables = {}
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            for _, element in ET.iterparse(z.open('xl/sharedStrings.xml'), events=('end',)):
                if element.tag == M + 'si':
                    strings.append(''.join(n.text or '' for n in element.iter(M + 't')))
                    element.clear()
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        paths = {r.attrib['Id']: posixpath.normpath(posixpath.join('xl', r.attrib['Target']))
                 if not r.attrib['Target'].startswith('/') else r.attrib['Target'].lstrip('/') for r in rels}
        workbook = ET.fromstring(z.read('xl/workbook.xml'))
        props = workbook.find('m:workbookPr', NS)
        assert props is None or props.attrib.get('date1904', '0') in ('0', 'false'), 'Unsupported Excel epoch'
        for sheet in workbook.find('m:sheets', NS):
            name = sheet.attrib['name']
            rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            rows = []
            with z.open(paths[rid]) as stream:
                for _, element in ET.iterparse(stream, events=('end',)):
                    if element.tag != M + 'row':
                        continue
                    values = {}
                    for cell in element:
                        coordinate = cell.attrib.get('r', '')
                        letters = ''.join(c for c in coordinate if c.isalpha())
                        col = 0
                        for letter in letters:
                            col = 26*col + ord(letter)-64
                        value = cell.find('m:v', NS)
                        typ = cell.attrib.get('t')
                        if typ == 'inlineStr':
                            v = ''.join(t.text or '' for t in cell.iter(M + 't'))
                        elif value is None or value.text is None:
                            continue
                        elif typ == 's':
                            v = strings[int(value.text)]
                        elif typ in ('str', 'e'):
                            v = value.text
                        else:
                            v = float(value.text)
                            v = int(v) if v.is_integer() else v
                        if v != '':
                            values[col-1] = v
                    if values:
                        rows.append((int(element.attrib['r']), values))
                    element.clear()
            header_row, h = rows[0]
            header = [h.get(i) for i in range(max(h)+1)]
            # Keep both OFFSET columns with explicit ordinal suffixes.
            counts, keys = collections.Counter(), []
            for i, v in enumerate(header):
                v = str(v) if v is not None else f'unnamed_{i+1}'
                counts[v] += 1
                keys.append(v if counts[v] == 1 else f'{v}__{counts[v]}')
            records = [{**{keys[i]: v for i, v in vals.items() if i < len(keys)}, '_source_row': row}
                       for row, vals in rows[1:]]
            tables[name] = {'columns': keys, 'records': records}
            print(name, len(records), flush=True)
    return tables

part_encoding = 'utf-16' if PART.read_bytes()[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'
with PART.open(encoding=part_encoding, newline='') as f:
    part_rows = list(csv.DictReader(f, delimiter='\t'))
assert {(r['PART'], r['ROUTE']) for r in part_rows if r['PARTGRP'] == 'Engineering'} == {
    ('part_E1','r_E1'), ('part_E2','r_E2'), ('part_E3','r_E3')}
production, amhs = read_tables(PROD), read_tables(AMHS)
alias = {'DefMEt_FE_118': 'DefMet_FE_118'}
equipment = amhs['Equipment']['records']
spatial_counts = collections.Counter(r['TOOL_GROUP'] for r in equipment)
tools = [r for r in production['Toolgroups']['records'] if r.get('TOOLGROUP')]
mapping = []
for r in tools:
    group = r['TOOLGROUP']
    target = alias.get(group, group)
    virtual = group == 'Delay_32'
    mapping.append({'production_group': group, 'spatial_group': '' if virtual else target,
        'production_count': r['NUMBER OF TOOLS'], 'spatial_count': spatial_counts.get(target, 0),
        'action': 'virtual_buffer_stage_not_physical_tool' if virtual else ('explicit_alias' if group in alias else 'identity')})
bad_mapping = [r for r in mapping if r['action'] != 'virtual_buffer_stage_not_physical_tool'
               and r['production_count'] != r['spatial_count']]
physical_prod = {r['spatial_group'] for r in mapping if r['action'] != 'virtual_buffer_stage_not_physical_tool'}
physical_spatial = set(spatial_counts) - {'STOCKER', 'COMMIT', 'COMPLETE'}
route_summary, route_errors = {}, []
valid_routes = {}
for name, table in production.items():
    if not name.startswith('Route_Product_'):
        continue
    records = [r for r in table['records'] if r.get('ROUTE') and r.get('STEP') is not None]
    valid_routes[name] = records
    steps = [str(r['STEP']) for r in records]
    groups = [alias.get(r.get('TOOLGROUP'), r.get('TOOLGROUP')) for r in records]
    unknown = sorted(set(groups)-physical_prod-{'Delay_32'})
    duplicates = [k for k, n in collections.Counter(steps).items() if n > 1]
    references = []
    for r in records:
        for field in ('STEP FOR LTL DEDICATION', 'STEP FOR REWORK', 'STEP FOR CRITICAL QUEUE TIME'):
            v = r.get(field)
            if v is not None and str(v) not in steps:
                references.append({'source_row': r['_source_row'], 'field': field, 'value': v})
    physical = [g for g in groups if g != 'Delay_32']
    counts = collections.Counter(physical)
    route_summary[name] = {'operations': len(records), 'virtual_delay_operations': groups.count('Delay_32'),
        'physical_operations': len(physical), 'distinct_physical_groups': len(counts),
        'repeat_group_visits_beyond_first': sum(n-1 for n in counts.values()),
        'unknown_groups': unknown, 'duplicate_steps': duplicates, 'invalid_step_references': references,
        'processing_units': dict(collections.Counter(r.get('PROCESSING UNIT') for r in records)),
        'processing_distributions': dict(collections.Counter(r.get('PROCESSINGTIME DISTRIBUTION') for r in records))}
    if unknown or duplicates or references:
        route_errors.append(name)

# Explicit fixed release sheets; summary Lotrelease is not expanded as an additional demand stream.
release_summary = {}
release_errors = []
for sheet in ('Lotrelease - variable due dates', 'Lotrelease - Engineering'):
    rows = production[sheet]['records']
    substantive = [r for r in rows if r.get('LOT NAME/TYPE') and r.get('START DATE') is not None]
    corrected = []
    route_overrides = collections.Counter()
    for r in substantive:
        row = dict(r)
        source_route = row.get('ROUTE NAME')
        if sheet == 'Lotrelease - Engineering':
            # Dataset summary explicitly assigns Product_1/2/3 engineering streams to E1/E2/E3.
            product = row.get('PRODUCT NAME')
            target = {'Product_1': 'Route_Product_E1', 'Product_2': 'Route_Product_E2', 'Product_3': 'Route_Product_E3'}.get(product)
            if target:
                row['ROUTE NAME'] = target
                row['_raw_route_name'] = source_route
                route_overrides[f'{source_route} -> {target}'] += 1
        if row.get('ROUTE NAME') not in valid_routes:
            release_errors.append({'sheet': sheet, 'row': row['_source_row'], 'issue': 'unknown_route'})
        for col in ('START DATE', 'DUE DATE'):
            v = row.get(col)
            if isinstance(v, (int, float)):
                row[col] = from_excel(v).isoformat()
        if row.get('DUE DATE') and row['DUE DATE'] < row['START DATE']:
            release_errors.append({'sheet': sheet, 'row': row['_source_row'], 'issue': 'due_before_release'})
        row['_source_sheet'] = sheet
        row['_event_id'] = f"{'eng' if sheet.endswith('Engineering') else 'prod'}:{row['_source_row']}"
        corrected.append(row)
    path = OUT / ('engineering_release.csv' if sheet.endswith('Engineering') else 'production_release.csv')
    columns = list(dict.fromkeys(k for r in corrected for k in r))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(corrected)
    counts = collections.Counter(r['LOT NAME/TYPE'] for r in corrected)
    release_summary[sheet] = {'records': len(corrected), 'min_release': min(r['START DATE'] for r in corrected),
        'max_release': max(r['START DATE'] for r in corrected), 'products': dict(collections.Counter(r['PRODUCT NAME'] for r in corrected)),
        'priority': dict(collections.Counter(str(r.get('PRIORITY')) for r in corrected)),
        'wafer_counts': dict(collections.Counter(str(r.get('WAFERS PER LOT')) for r in corrected)),
        'route_overrides': dict(route_overrides), 'duplicate_lot_names': sum(n-1 for n in counts.values())}

def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)
write_csv(OUT/'toolgroup_mapping.csv', mapping)
all_operations = []
for name, rows in valid_routes.items():
    for r in rows:
        all_operations.append({**r, '_source_sheet': name, '_mapped_toolgroup': alias.get(r.get('TOOLGROUP'), r.get('TOOLGROUP')),
            '_is_virtual_stage': r.get('TOOLGROUP') == 'Delay_32'})
write_csv(OUT/'route_operations.csv', all_operations)
audit = {'production_sheets': {n: {'stored_nonempty_rows': len(t['records']), 'columns': t['columns']} for n,t in production.items()},
    'amhs_counts': {n:len(t['records']) for n,t in amhs.items()},
    'mapping_errors': bad_mapping, 'unmapped_spatial_groups': sorted(physical_spatial-physical_prod),
    'routes': route_summary, 'route_error_tables': route_errors, 'releases': release_summary,
    'release_errors': release_errors, 'physical_group_count': len(physical_prod),
    'physical_tool_count': sum(r['production_count'] for r in mapping if r['action'] != 'virtual_buffer_stage_not_physical_tool')}
(OUT/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
sources = [PROD, AMHS, PART, ROOT/'03-数据/SMT2020-official/SMT2020.zip']
sources += list((ROOT/'03-数据/SMT2020-official/extracted/SMT_2020 - Final').glob('*.docx'))
sources += list((ROOT/'03-数据/SMAT2022').glob('*.docx'))
derived = [OUT/n for n in ('toolgroup_mapping.csv','route_operations.csv','production_release.csv','engineering_release.csv','audit.json','README.md')]
derived += [ROOT/'04-研究/freeze_data_baseline.py', ROOT/'04-研究/verify_data_baseline.py']
manifest = {'baseline_id':'SMAT2022-SMT2020-LVHM_E-v1', 'date':'2026-09-30',
    'sources': [{'path':str(p.relative_to(ROOT)).replace('\\','/'), 'size_bytes':p.stat().st_size,
                 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sources],
    'derived': [{'path':str(p.relative_to(ROOT)).replace('\\','/'), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in derived],
    'urls': {'production':'https://p2schedgen.fernuni-hagen.de/fileadmin/ess/uploads_p2schedgen/SMT2020/SMT2020.zip',
             'amhs':'https://github.com/kwoo-lee/SMAT2022'},
    'amhs_commit':'a6df7fef99745fa49cf347306abaeb4b19002169',
    'release_selection':['Lotrelease - variable due dates','Lotrelease - Engineering'],
    'toolgroup_aliases':alias,
    'release_engineering_route_override':'Product_1/2/3 engineering rows map to E1/E2/E3 according to Lotrelease summary; raw value preserved',
    'virtual_group':'Delay_32 is not a physical equipment group; retain source operations for later buffer semantics',
    'internal_units': {'time':'second','distance':'metre','wafer_quantity':'wafer','batch_capacity':'wafer'},
    'unresolved_simulation_fields':['spatial length units confirmation','Rail.MAX_SPEED unit/interpretation','curved rail arc geometry',
        'OHT loading/unloading duration','ZCU exact runtime semantics','stocker and track buffer operating rules',
        'equipment process loading/unloading and cascade overlap','failure interruption policy'],
    'status':'source_data_frozen; simulation_semantics_not_yet_ready'}
(OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in audit.items() if k not in ('production_sheets','routes')},ensure_ascii=False,indent=2))
assert not bad_mapping and not audit['unmapped_spatial_groups']
assert not route_errors and not release_errors

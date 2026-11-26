"""只缓存原始表格；源内容哈希失效，返回独立行字典，不保存仿真状态。"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import openpyxl

SMAT_SHEETS = dict.fromkeys(("Address", "Rail", "Equipment", "Port", "Vehicle", "VehicleType"))


def read_tables(source: Path, sheets: dict[str, int | None], enabled: bool = True) -> dict:
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    signature = {"version": 1, "source": source_hash, "sheets": sheets}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    directory = Path(__file__).resolve().parents[3] / "tmp/workbook-cache"
    cache = directory / (key + ".json")
    cells = None
    if enabled:
        try:
            envelope = json.loads(cache.read_text("utf-8"))
            body = envelope["body"]
            if (
                envelope["signature"] == signature
                and hashlib.sha256(body.encode()).hexdigest() == envelope["sha256"]
            ):
                cells = json.loads(body)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    if cells is None:
        wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
        try:
            cells = {}
            for name, count in sheets.items():
                rows = wb[name].iter_rows(values_only=True, max_row=count + 1 if count else None)
                cells[name] = {
                    "header": next(rows),
                    "rows": [row for row in rows if any(v is not None for v in row)],
                }
        finally:
            wb.close()
        if enabled:
            temporary = None
            try:
                # 非JSON标量（例如日期）不做有损转换，直接返回原表。
                body = json.dumps(cells, ensure_ascii=False, allow_nan=False)
                envelope = {
                    "signature": signature,
                    "body": body,
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=directory, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(envelope, stream, ensure_ascii=False)
                os.replace(temporary, cache)
            except (OSError, TypeError, ValueError):
                pass
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
    # 保留None等原始表头类型；不把字典键经JSON转成字符串。
    return {
        name: [dict(zip(table["header"], row)) for row in table["rows"]]
        for name, table in cells.items()
    }

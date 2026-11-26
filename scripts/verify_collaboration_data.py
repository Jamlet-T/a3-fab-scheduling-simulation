"""核验随仓库提供的运行输入，不改写冻结基准或验证结果。"""

import hashlib
import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "03-数据/基准-v1/manifest.json").read_text("utf-8"))
    entries = [s for s in manifest["sources"] if s["path"].endswith(".xlsx")]
    entries += [s for s in manifest["derived"] if s["path"].endswith(".csv")]
    errors = []
    for entry in entries:
        path = root / entry["path"]
        if not path.is_file():
            errors.append(f"缺少文件：{entry['path']}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            errors.append(f"SHA256不一致：{entry['path']}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"协作运行输入核验通过：{len(entries)}个文件与冻结manifest一致。")


if __name__ == "__main__":
    main()

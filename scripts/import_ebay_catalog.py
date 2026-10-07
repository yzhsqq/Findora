"""Import a collected eBay US JSON file into an independent local snapshot."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.infrastructure.persistence.ebay_catalog import import_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/ebay_catalog.sqlite3"))
    parser.add_argument("--merge", action="store_true",
                        help="保留已有快照中的商品，只把本次文件的新记录并入（默认整批替换）")
    parser.add_argument("--skip-invalid", action="store_true",
                        help="跳过缺少商品编号或标题的记录（默认整批校验失败即中止）")
    args = parser.parse_args()
    print(json.dumps(import_snapshot(args.input, args.output, merge=args.merge,
                                     skip_invalid=args.skip_invalid), ensure_ascii=False))


if __name__ == "__main__":
    main()

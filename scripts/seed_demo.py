import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402

db.init_db()
for order in [
    ("A1001", "王小明", "已出貨", "2026-11-28", 1280),
    ("A1002", "陳小美", "處理中", "2026-12-01", 560),
]:
    db.upsert_order(*order)
print("demo data ready")

"""Rewrite assets/parts/ from the raw pack in assets/source/. Only needed if the pack or the part list changes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from agenteconomy.art.parts import PARTS_DIR, build_parts  # noqa: E402

count = build_parts()
total = sum(path.stat().st_size for path in PARTS_DIR.glob("*.png"))
print(f"Wrote {count} parts to {PARTS_DIR} ({total:,} bytes)")

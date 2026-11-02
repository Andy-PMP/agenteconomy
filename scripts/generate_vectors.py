"""Rewrite testdata/chain_vectors_v1.json. Only needed after a deliberate format change."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from agenteconomy.chain.vectors import render_vectors  # noqa: E402

path = ROOT / "testdata" / "chain_vectors_v1.json"
path.parent.mkdir(exist_ok=True)
path.write_bytes(render_vectors().encode("ascii"))
print(f"Wrote {path} ({path.stat().st_size:,} bytes)")

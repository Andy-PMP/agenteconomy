"""Rewrite the test vector files. Only needed after a deliberate format change."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from agenteconomy.chain import vectors as chain_vectors  # noqa: E402
from agenteconomy.economy import vectors as economy_vectors  # noqa: E402

(ROOT / "testdata").mkdir(exist_ok=True)
for name, module in (("chain_vectors_v1.json", chain_vectors), ("economy_vectors_v1.json", economy_vectors)):
    path = ROOT / "testdata" / name
    path.write_bytes(module.render_vectors().encode("ascii"))
    print(f"Wrote {path} ({path.stat().st_size:,} bytes)")

"""Draw a contact sheet of creatures, for tuning the tables in art/render.py by eye.

Usage: python scripts/render_sheet.py <output.png> [species|all|shapes] [count] [salt]
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from PIL import Image, ImageDraw  # noqa: E402

from agenteconomy.art.parts import CANVAS_PIXELS  # noqa: E402
from agenteconomy.art.render import render  # noqa: E402
from agenteconomy.chain.creature import SHAPES, SPECIES, hatch  # noqa: E402

output = sys.argv[1]
which = sys.argv[2] if len(sys.argv) > 2 else "all"
count = int(sys.argv[3]) if len(sys.argv) > 3 else 36
salt = sys.argv[4] if len(sys.argv) > 4 else "sheet"

if which == "all":
    pools = [(species,) for species in SPECIES]
elif which == "shapes":
    pools = [(f"blue{shape}",) for shape in SHAPES]
else:
    pools = [(which,)]

columns = 6
rows = (count + columns - 1) // columns
sheet = Image.new("RGBA", (columns * CANVAS_PIXELS, rows * (CANVAS_PIXELS + 14)), (238, 240, 244, 255))
draw = ImageDraw.Draw(sheet)
for index in range(count):
    block_hash = hashlib.sha256(f"{salt}-{index}".encode()).hexdigest()
    creature = hatch(block_hash, pools[index % len(pools)])
    x, y = index % columns * CANVAS_PIXELS, index // columns * (CANVAS_PIXELS + 14)
    sheet.alpha_composite(render(creature), (x, y + 14))
    label = f"{creature.species} a{creature.arms} l{creature.legs} {creature.detail or ''}"
    draw.text((x + 4, y + 1), label, fill="black")
sheet.save(output)
print(output)

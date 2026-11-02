"""Assembles a creature image from parts. Kenney's pack has no attachment points, so these tables are hand-tuned.

All coordinates are in the raw pack's pixels: x and y from the centre of the body, y growing downward. Tune them by
looking at the output of scripts/render_sheet.py.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from ..chain.creature import Creature
from .parts import CANVAS_PIXELS, PARTS_DIR, scaled

BODY_CENTRE = (256, 215)


@dataclass(frozen=True)
class ShapeLayout:
    shoulder: tuple[int, int]  # where the right arm's joint goes; the left mirrors it
    hip: tuple[int, int]
    head: tuple[int, int]  # where the right detail's base goes
    eye_y: int
    nose_y: int
    mouth_y: int


LAYOUTS = {
    "A": ShapeLayout(shoulder=(72, -5), hip=(45, 68), head=(50, -72), eye_y=-18, nose_y=12, mouth_y=44),
    "B": ShapeLayout(shoulder=(88, -5), hip=(50, 84), head=(55, -80), eye_y=-25, nose_y=8, mouth_y=46),
    "C": ShapeLayout(shoulder=(62, -15), hip=(30, 85), head=(40, -85), eye_y=-35, nose_y=2, mouth_y=38),
    "D": ShapeLayout(shoulder=(70, 10), hip=(50, 78), head=(35, -80), eye_y=-25, nose_y=8, mouth_y=42),
    "E": ShapeLayout(shoulder=(58, -30), hip=(35, 115), head=(35, -115), eye_y=-60, nose_y=-25, mouth_y=10),
    "F": ShapeLayout(shoulder=(62, -25), hip=(40, 105), head=(35, -108), eye_y=-50, nose_y=-15, mouth_y=20),
}

# Where each right-facing part attaches, in the part's own pixels.
ARM_JOINTS = {"A": (25, 24), "B": (18, 17), "C": (28, 28), "D": (30, 30), "E": (28, 18)}
LEG_JOINTS = {"A": (25, 25), "B": (16, 15), "C": (21, 21), "D": (20, 20), "E": (24, 24)}
DETAIL_BASES = {"antenna_large": (6, 54), "antenna_small": (4, 34), "ear": (10, 40), "ear_round": (20, 42),
                "eye": (8, 72), "horn_large": (8, 38), "horn_small": (8, 22)}

EYE_SPACING = {1: ((0, 0),), 2: ((-34, 0), (34, 0)), 3: ((-36, 8), (36, 8), (0, -32))}
EYEBROW_RAISE = 40


class _Canvas:
    def __init__(self, parts_dir: Path):
        self.image = Image.new("RGBA", (CANVAS_PIXELS, CANVAS_PIXELS), (0, 0, 0, 0))
        self._parts_dir = parts_dir

    def place(self, name: str, x: int, y: int, anchor: tuple[int, int] | None = None, mirror: bool = False) -> None:
        """Put a part so `anchor` lands on (x, y) from the body's centre. No anchor means centre the part."""
        part = _load(self._parts_dir, name)
        if anchor is None:
            anchor_x, anchor_y = part.width // 2, part.height // 2
        else:
            anchor_x, anchor_y = scaled(anchor[0]), scaled(anchor[1])
        if mirror:
            part = ImageOps.mirror(part)
            anchor_x = part.width - anchor_x
        self.image.alpha_composite(part, (scaled(BODY_CENTRE[0] + x) - anchor_x, scaled(BODY_CENTRE[1] + y) - anchor_y))

    def place_pair(self, name: str, x: int, y: int, anchor: tuple[int, int]) -> None:
        self.place(name, x, y, anchor)
        self.place(name, -x, y, anchor, mirror=True)


@lru_cache(maxsize=512)
def _load(parts_dir: Path, name: str) -> Image.Image:
    with Image.open(parts_dir / f"{name}.png") as image:
        return image.convert("RGBA")


def render(creature: Creature, parts_dir: Path = PARTS_DIR) -> Image.Image:
    layout = LAYOUTS[creature.shape]
    colour = creature.colour
    canvas = _Canvas(parts_dir)

    # Limbs and head details go behind the body, which hides their joints.
    canvas.place_pair(f"leg_{colour}{creature.legs}", *layout.hip, LEG_JOINTS[creature.legs])
    canvas.place_pair(f"arm_{colour}{creature.arms}", *layout.shoulder, ARM_JOINTS[creature.arms])
    if creature.detail:
        canvas.place_pair(f"detail_{colour}_{creature.detail}", *layout.head, DETAIL_BASES[creature.detail])
    canvas.place(f"body_{creature.species}", 0, 0)

    canvas.place(creature.mouth, 0, layout.mouth_y)
    for x, y in EYE_SPACING[creature.eye_count]:
        canvas.place(f"eye_{creature.eyes}", x, layout.eye_y + y)
        if creature.eyebrows and y >= 0:  # a third eye sits above the others, where a brow would hide them
            canvas.place(creature.eyebrows, x, layout.eye_y + y - EYEBROW_RAISE, mirror=x > 0)
    if creature.nose:
        canvas.place(f"nose_{creature.nose}", 0, layout.nose_y)
    return canvas.image

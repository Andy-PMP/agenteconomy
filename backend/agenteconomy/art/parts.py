"""Builds assets/parts/, the processed copies the renderer uses, from the raw Kenney pack.

Raw files are gitignored, so CI and the daily run only ever see these. Run scripts/build_parts.py if the pack changes.
"""

from pathlib import Path

from PIL import Image

from ..chain.creature import ARMS, COLOURS, DETAILS, EYEBROWS, EYES, LEGS, MOUTHS, NOSES, SHAPES

SCALE_NUMERATOR, SCALE_DENOMINATOR = 1, 2
REPO_ROOT = Path(__file__).resolve().parents[3]
PARTS_DIR = REPO_ROOT / "assets" / "parts"
RAW_DIR = REPO_ROOT / "assets" / "source" / "kenney_monster-builder-pack" / "PNG" / "Default"


def scaled(value: int) -> int:
    return value * SCALE_NUMERATOR // SCALE_DENOMINATOR


CANVAS_RAW = 512
CANVAS_PIXELS = scaled(CANVAS_RAW)


def part_names() -> list[str]:
    """Every file a creature can use, and nothing else."""
    names = [f"eye_{eye}" for eye in EYES] + list(MOUTHS) + [name for name in EYEBROWS if name]
    names += [f"nose_{nose}" for nose in NOSES if nose]
    for colour in COLOURS:
        names += [f"body_{colour}{shape}" for shape in SHAPES]
        names += [f"arm_{colour}{arm}" for arm in ARMS]
        names += [f"leg_{colour}{leg}" for leg in LEGS]
        names += [f"detail_{colour}_{detail}" for detail in DETAILS if detail]
    return sorted(names)


def build_parts(raw_dir: Path = RAW_DIR, parts_dir: Path = PARTS_DIR) -> int:
    parts_dir.mkdir(parents=True, exist_ok=True)
    names = part_names()
    for name in names:
        with Image.open(raw_dir / f"{name}.png") as raw:
            image = raw.convert("RGBA")
        size = (scaled(image.width), scaled(image.height))
        image.resize(size, Image.Resampling.LANCZOS).save(parts_dir / f"{name}.png", optimize=True)
    return len(names)

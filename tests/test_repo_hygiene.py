"""The repo is public: raw assets and secrets must never be committable."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS = {
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def committable_files() -> list[str]:
    """Tracked files plus untracked ones that aren't ignored, so problems show up before the first commit."""
    result = git("ls-files", "--cached", "--others", "--exclude-standard")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_raw_assets_are_ignored():
    assert git("check-ignore", "-q", "assets/source/somepack/creature.png").returncode == 0


def test_only_the_readme_is_committable_in_asset_source():
    in_source = [f for f in committable_files() if f.startswith("assets/source/")]
    assert in_source == ["assets/source/README.md"]


def test_no_secrets_in_committable_files():
    found = []
    for name in committable_files():
        path = ROOT / name
        if not path.is_file():  # listed by git but deleted from the working tree
            continue
        content = path.read_bytes()
        found += [f"{name}: {label}" for label, pattern in SECRET_PATTERNS.items() if pattern.search(content)]
    assert not found, found

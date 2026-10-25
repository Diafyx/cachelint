"""Keep the reviewed release wheel, sdist, and hashes together."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0a2"
WHEEL = f"cachelint-{VERSION}-py3-none-any.whl"
SDIST = f"cachelint-{VERSION}.tar.gz"
FILES = {WHEEL, SDIST}


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def package(source: Path, target: Path) -> None:
    if target.exists():
        raise ValueError("Release artifact directory already exists")
    if {path.name for path in source.iterdir()} != FILES:
        raise ValueError("Expected exactly one wheel and one sdist")
    dist = target / "dist"
    dist.mkdir(parents=True)
    for name in sorted(FILES):
        shutil.copyfile(source / name, dist / name)
    lines = [f"{digest(dist / name)}  dist/{name}\n" for name in sorted(FILES)]
    (target / "SHA256SUMS").write_text("".join(lines), encoding="ascii")
    verify(target)


def verify(target: Path) -> dict[str, str]:
    if {path.name for path in target.iterdir()} != {"dist", "SHA256SUMS"}:
        raise ValueError("Release artifact has unexpected files")
    dist = target / "dist"
    if not dist.is_dir() or {path.name for path in dist.iterdir()} != FILES:
        raise ValueError("Release artifact has unexpected distributions")
    lines = (target / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    if len(lines) != 2:
        raise ValueError("Release manifest must contain two hashes")
    hashes: dict[str, str] = {}
    for line in lines:
        parts = line.split("  dist/")
        if len(parts) != 2 or not parts[0] or parts[1] not in FILES:
            raise ValueError("Release manifest has an invalid entry")
        sha, name = parts
        if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
            raise ValueError("Release manifest has an invalid SHA-256")
        if name in hashes:
            raise ValueError("Release manifest repeats a file")
        hashes[name] = sha
    if set(hashes) != FILES:
        raise ValueError("Release manifest omits a file")
    for name, expected in hashes.items():
        if digest(dist / name) != expected:
            raise ValueError(f"Release artifact hash differs: {name}")
    return hashes


def check_pypi(target: Path) -> None:
    hashes = verify(target)
    url = f"https://pypi.org/pypi/cachelint/{VERSION}/json"
    response: dict[str, object] | None = None
    for attempt in range(12):
        try:
            with urllib.request.urlopen(url, timeout=20) as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                response = loaded
                break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        if attempt < 11:
            time.sleep(15)
    if response is None:
        raise ValueError("PyPI release metadata did not become available")
    info = response.get("info")
    urls = response.get("urls")
    if (
        not isinstance(info, dict)
        or info.get("name") != "cachelint"
        or info.get("version") != VERSION
    ):
        raise ValueError("PyPI release identity differs")
    if not isinstance(urls, list) or len(urls) != 2:
        raise ValueError("PyPI release file count differs")
    by_name = {item.get("filename"): item for item in urls if isinstance(item, dict)}
    if set(by_name) != FILES:
        raise ValueError("PyPI release filenames differ")
    for name, expected in hashes.items():
        item = by_name[name]
        digests = item.get("digests")
        if not isinstance(digests, dict) or digests.get("sha256") != expected:
            raise ValueError(f"PyPI metadata hash differs: {name}")
        file_url = item.get("url")
        if not isinstance(file_url, str) or not file_url.startswith(
            "https://files.pythonhosted.org/"
        ):
            raise ValueError(f"PyPI file URL is invalid: {name}")
        with urllib.request.urlopen(file_url, timeout=60) as stream:
            downloaded = hashlib.sha256(stream.read()).hexdigest()
        if downloaded != expected:
            raise ValueError(f"PyPI downloaded file hash differs: {name}")
        print(f"Verified PyPI file: {file_url} sha256={expected}")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "package":
        package(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "verify":
        verify(Path(sys.argv[2]))
        print("Release artifact hashes verified")
    elif len(sys.argv) == 3 and sys.argv[1] == "pypi":
        check_pypi(Path(sys.argv[2]))
    else:
        raise SystemExit(
            "Usage: release_artifacts.py package DIST TARGET | "
            "verify TARGET | pypi TARGET"
        )


if __name__ == "__main__":
    main()

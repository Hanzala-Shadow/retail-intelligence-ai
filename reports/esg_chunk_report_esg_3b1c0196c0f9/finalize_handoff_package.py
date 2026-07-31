from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "outputs" / "esg_chunk_handoff_2000_esg_3b1c0196c0f9"
ZIP_PATH = ROOT / "outputs" / "esg_chunk_handoff_2000_esg_3b1c0196c0f9.zip"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    files = sorted(
        path for path in PACKAGE.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [f"{digest(path)}  {path.relative_to(PACKAGE).as_posix()}" for path in files]
    sums = PACKAGE / "SHA256SUMS"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    checked = 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        target = PACKAGE / relative
        if not target.is_file() or digest(target) != expected:
            raise SystemExit(f"Hash verification failed: {relative}")
        checked += 1

    with ZipFile(ZIP_PATH, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(p for p in PACKAGE.rglob("*") if p.is_file()):
            archive.write(path, arcname=path.relative_to(PACKAGE).as_posix())

    with ZipFile(ZIP_PATH, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit(f"ZIP verification failed: {bad}")

    print(f"hashed_files={checked}")
    print(f"zip={ZIP_PATH}")
    print(f"zip_bytes={ZIP_PATH.stat().st_size}")


if __name__ == "__main__":
    main()

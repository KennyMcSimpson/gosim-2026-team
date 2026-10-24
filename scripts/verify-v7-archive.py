#!/usr/bin/env python3
"""Check the frozen v7 ZIP and runtime files against the committed receipt.

Standard library only; this does not upload a project or call a model.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1] / "v7-merge-pro"


def verify(root: Path) -> list[str]:
    receipt = json.loads((root / "verification.json").read_text(encoding="utf-8"))
    archive = root / "v7-merge-pro.zip"
    errors = []
    archive_bytes = archive.read_bytes()
    if len(archive_bytes) != receipt["zip_bytes"]:
        errors.append("ZIP size differs from verification.json")
    if hashlib.sha256(archive_bytes).hexdigest() != receipt["zip_sha256"]:
        errors.append("ZIP SHA-256 differs from verification.json")
    with zipfile.ZipFile(archive) as packed:
        names = packed.namelist()
        expected = receipt["runtime_files"]
        if len(names) != len(set(names)):
            errors.append("ZIP contains duplicate entries")
        if set(names) != set(expected):
            errors.append("ZIP entries differ from the receipt's runtime file set")
        for name, digest in expected.items():
            source = root / "project" / name
            if not source.is_file():
                errors.append(f"Missing runtime source: {name}")
                continue
            source_bytes = source.read_bytes()
            if hashlib.sha256(source_bytes).hexdigest() != digest:
                errors.append(f"Runtime source SHA-256 differs from receipt: {name}")
            if name in names:
                packed_bytes = packed.read(name)
                if hashlib.sha256(packed_bytes).hexdigest() != digest:
                    errors.append(f"ZIP entry SHA-256 differs from receipt: {name}")
                if packed_bytes != source_bytes:
                    errors.append(f"ZIP entry differs from runtime source: {name}")
    return errors


def main() -> int:
    try:
        errors = verify(ROOT)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"FAIL: {error}")
        return 1
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    print("v7 ZIP and runtime source match verification.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

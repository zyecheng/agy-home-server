#!/usr/bin/env python3
"""Safely update KEY=VALUE entries in ~/agy-server/.env from stdin (0600, never echoes secrets)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_PATH = Path.home() / "agy-server" / ".env"


def main() -> int:
    updates: dict[str, str] = {}
    for raw in sys.stdin.read().splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k:
            updates[k] = v

    if not updates:
        print("No KEY=VALUE updates received.", file=sys.stderr)
        return 1

    existing_lines = (
        ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    )
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out_lines.append(line)

    for key, val in updates.items():
        if key not in seen:
            out_lines.append(f"{key}={val}")

    tmp_path = ENV_PATH.with_suffix(".env.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out_lines) + "\n")
    os.replace(tmp_path, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)
    print("Updated keys:", ", ".join(sorted(updates.keys())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

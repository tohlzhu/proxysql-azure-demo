#!/usr/bin/env python3
"""Load a simple KEY=value environment file as data, without shell evaluation."""

import os
import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("Usage: with-env.py ENV_FILE COMMAND [ARG ...]")
    for line in Path(sys.argv[1]).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Environment file must contain only KEY=value lines")
        os.environ.setdefault(key, value)
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()

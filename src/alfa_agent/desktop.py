"""Local desktop entry: reuse configured demo access without evaluating a shell env file."""

import os
from pathlib import Path
import sys


def load_desktop_access(root):
    path = root / ".env.render"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator and name in {"ALFA_ACCESS_PASSWORD", "ALFA_SESSION_SECRET"}:
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(name, value)


def main():
    load_desktop_access(Path(__file__).resolve().parents[2])
    sys.argv = [sys.argv[0], "serve", *sys.argv[1:]]
    from .cli import main as serve

    serve()


if __name__ == "__main__":
    main()

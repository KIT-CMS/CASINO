from __future__ import annotations

import sys


def eprint(message: str) -> None:
    print(message, file=sys.stderr)

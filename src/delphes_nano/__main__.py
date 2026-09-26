from __future__ import annotations

import sys

from .pipeline import convert


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print(
            "usage: python -m delphes_nano <delphes-tree> [<delphes-tree> ...] <nano.root>",
            file=sys.stderr,
        )
        return 2
    *inputs, output = args
    convert(inputs, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

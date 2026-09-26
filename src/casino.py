#!/usr/bin/env python3
from __future__ import annotations

from typing import Callable

from application import CasinoApplication
from infrastructure.mcm import fetch_text


def create_application(fetcher: Callable[[str, int, bool], str] = fetch_text, gridpack_checker=None) -> CasinoApplication:
    return CasinoApplication(fetcher=fetcher, gridpack_checker=gridpack_checker)


def main(argv: list[str] | None = None) -> int:
    return create_application().main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

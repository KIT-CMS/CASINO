from __future__ import annotations

import importlib


def import_law_dependencies():
    """Import law + luigi (tests swap in fakes via sys.modules) and fix bool parameter parsing."""
    try:
        law = importlib.import_module("law")
        luigi = importlib.import_module("luigi")
    except ImportError as exc:
        raise ImportError("etp_law requires the optional 'law' and 'luigi' dependencies to be installed") from exc
    force_explicit_bool_parsing(law, luigi)
    return law, luigi


def force_explicit_bool_parsing(law, luigi) -> None:
    """Switch every luigi.BoolParameter to EXPLICIT_PARSING.

    law serialises bool parameters for remote jobs as --flag=True/False, but luigi's default
    IMPLICIT_PARSING registers them as store_true, which rejects the =value form and kills the
    worker with "argument --clear-logs: ignored explicit argument 'False'".
    """
    explicit = getattr(luigi.BoolParameter, "EXPLICIT_PARSING", None)
    if explicit is None:
        return  # test doubles without parsing modes
    luigi.BoolParameter.parsing = explicit

    seen: set[type] = set()

    def walk(cls):
        yield cls
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                yield from walk(sub)

    for cls in walk(luigi.Task):
        for value in vars(cls).values():
            if isinstance(value, luigi.BoolParameter):
                value.parsing = explicit

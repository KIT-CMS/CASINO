from __future__ import annotations

from typing import Protocol


class Prompter(Protocol):
    def text(self, label: str, default: str | None = None) -> str: ...
    def choice(self, label: str, options: list[str], default: int | None = None) -> int: ...
    def confirm(self, label: str, default: bool = False) -> bool: ...


def _asked(question):
    """questionary returns None only on Ctrl-C/EOF; surface that as KeyboardInterrupt."""
    answer = question.ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


class QuestionaryPrompter:
    """Interactive prompter; questionary is imported lazily so tests run without it."""

    def text(self, label: str, default: str | None = None) -> str:
        import questionary

        return _asked(questionary.text(label, default=default or ""))

    def choice(self, label: str, options: list[str], default: int | None = None) -> int:
        import questionary

        choices = [questionary.Choice(title=option, value=index) for index, option in enumerate(options)]
        kwargs = {"default": default} if default is not None else {}
        return _asked(questionary.select(label, choices=choices, **kwargs))

    def confirm(self, label: str, default: bool = False) -> bool:
        import questionary

        return bool(_asked(questionary.confirm(label, default=default)))


class ScriptedPrompter:
    """Replays a fixed list of answers (tests and non-interactive shims)."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)

    def _next(self) -> str:
        return self._answers.pop(0).strip() if self._answers else ""

    def text(self, label: str, default: str | None = None) -> str:
        return self._next() or (default or "")

    def choice(self, label: str, options: list[str], default: int | None = None) -> int:
        raw = self._next()
        if not raw and default is not None:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        raise ValueError(f"scripted choice {raw!r} not in 1-{len(options)}")

    def confirm(self, label: str, default: bool = False) -> bool:
        raw = self._next().lower()
        return raw in ("y", "yes") if raw else default

from __future__ import annotations

import re
from pathlib import Path

from domain.configuration import ConfigLoader
from .filesystem import write_text


XROOTD_GRIDPACK_SCRIPT = "GeneratorInterface/LHEInterface/data/run_generic_tarball_xrootd.sh"


class CustomGridpackFragmentManager:
    """Rewrites the externalLHEProducer block of an McM fragment to a custom root:// gridpack."""

    EXTERNAL_LHE_PRODUCER_RE = re.compile(
        r"(?m)^[ \t]*externalLHEProducer\s*=\s*cms\.EDProducer\s*\(\s*(?P<quote>['\"])ExternalLHEProducer(?P=quote)"
    )
    GRIDPACK_ARGS_RE = re.compile(
        r"(?P<prefix>\bargs\s*=\s*cms\.vstring\()\s*(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)\s*(?P<suffix>\))"
    )
    SCRIPT_NAME_RE = re.compile(
        r"(?P<prefix>\bscriptName\s*=\s*cms\.FileInPath\()\s*(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)\s*(?P<suffix>\))"
    )

    def __init__(self, config_loader: ConfigLoader) -> None:
        self.config_loader = config_loader

    def custom_fragment_path(self, config_path: Path, repo_root: Path) -> Path:
        relative_config_dir, sample_name = self.config_loader.relative_config_location(config_path, repo_root)
        return repo_root / "configs" / relative_config_dir / "fragments" / f"{sample_name}-fragment.py"

    def ensure_custom_fragment(
        self, source_fragment_path: Path, config_path: Path, repo_root: Path, gridpack_uri: str, overwrite: bool,
    ) -> Path:
        target_path = self.custom_fragment_path(config_path, repo_root)
        if target_path.exists() and not overwrite:
            return target_path
        rendered = self.render_fragment(source_fragment_path.read_text(encoding="utf-8"), gridpack_uri)
        write_text(target_path, rendered, overwrite=True)
        return target_path

    def render_fragment(self, source_text: str, gridpack_uri: str) -> str:
        start, end = self._find_external_lhe_producer_block(source_text)
        block = source_text[start:end]
        block = self._replace_once(self.GRIDPACK_ARGS_RE, block, "args", gridpack_uri)
        block = self._replace_once(self.SCRIPT_NAME_RE, block, "scriptName", XROOTD_GRIDPACK_SCRIPT)
        return source_text[:start] + block + source_text[end:]

    def _find_external_lhe_producer_block(self, text: str) -> tuple[int, int]:
        error = ValueError("Expected exactly one externalLHEProducer block in fragment")
        matches = list(self.EXTERNAL_LHE_PRODUCER_RE.finditer(text))
        if len(matches) != 1:
            raise error
        match = matches[0]
        open_paren = text.find("(", match.start(), match.end())
        if open_paren == -1:
            raise error
        end = _balanced_call_end(text, open_paren)
        if end is None:
            raise error
        return match.start(), end

    @staticmethod
    def _replace_once(pattern: re.Pattern[str], text: str, label: str, replacement_value: str) -> str:
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one externalLHEProducer {label} assignment in fragment")
        match = matches[0]
        return text[: match.start()] + f"{match.group('prefix')}{replacement_value!r}{match.group('suffix')}" + text[match.end():]


def _balanced_call_end(text: str, open_paren: int) -> int | None:
    """Index just past the ')' closing the call at open_paren, skipping comments and strings."""
    depth = 0
    index = open_paren
    while index < len(text):
        char = text[index]
        if char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline + 1
            continue
        if char in ("'", '"'):
            index = _skip_string(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _skip_string(text: str, start: int) -> int:
    quote = text[start]
    delimiter = quote * 3 if text.startswith(quote * 3, start) else quote
    index = start + len(delimiter)
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text.startswith(delimiter, index):
            return index + len(delimiter)
        index += 1
    return len(text)

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


GRIDPACK_URI_PREFIX = "root://"
DELPHES_SIM_TOKEN = "delphes"
DELPHES_NANO_TOKEN = "delphes-nano"
REQUIRED_TOP_LEVEL_KEYS = {"total_events", "base_output_path", "steps"}
REQUIRED_STEP_KEYS = {"name", "setup_ids", "events_per_job", "cores_per_job", "memory_per_job"}


def classify_setup_id(token: str) -> tuple[str, str | None]:
    """Return ("mcm", None), ("delphes", card_or_None) or ("nano", None) for a setup_ids entry."""
    if token == DELPHES_NANO_TOKEN:
        return "nano", None
    if token == DELPHES_SIM_TOKEN:
        return "delphes", None
    if token.startswith(DELPHES_SIM_TOKEN + ":"):
        return "delphes", token[len(DELPHES_SIM_TOKEN) + 1:] or None
    return "mcm", None


def is_pseudo_stage(token: str) -> bool:
    return classify_setup_id(token)[0] != "mcm"


def step_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-") or "step"


def strip_inline_comment(line: str) -> str:
    in_single = in_double = False
    result: list[str] = []
    for character in line:
        if character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif character == "#" and not in_single and not in_double:
            break
        result.append(character)
    return "".join(result).rstrip()


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?\d+\.\d+", value):
        return float(value)
    return value


def split_mapping(text: str) -> tuple[str, str | None]:
    if ":" not in text:
        raise ValueError(f"Could not parse mapping line: {text!r}")
    key, value = (part.strip() for part in text.split(":", 1))
    if not key:
        raise ValueError(f"Empty mapping key in line: {text!r}")
    return key, value or None


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _positive_int(step: dict[str, Any], key: str, index: int) -> None:
    if not isinstance(step[key], int) or step[key] <= 0:
        raise ValueError(f"Step {index} must define a positive integer {key}")


class ConfigLoader:
    """Parser for the minimal YAML subset used by sample configs (no pyyaml dependency)."""

    def load(self, config_path: Path) -> dict[str, Any]:
        lines = self._significant_lines(config_path)
        data: dict[str, Any] = {}
        for index, (indent, text) in enumerate(lines):
            if indent != 0:
                raise ValueError(f"Unsupported indentation at top level: {text!r}")
            if text == "steps:":
                data["steps"] = self._parse_steps(lines, index + 1)
                if any(ind == 0 and not txt.startswith("- ") for ind, txt in lines[index + 1:]):
                    raise ValueError("Top-level config keys after steps are not supported")
                break
            key, value = split_mapping(text)
            data[key] = parse_scalar(value or "")
        self._validate(data)
        return data

    @staticmethod
    def _significant_lines(config_path: Path) -> list[tuple[int, str]]:
        lines = []
        for raw_line in config_path.read_text(encoding="utf-8").splitlines():
            cleaned = strip_inline_comment(raw_line)
            if cleaned.strip():
                lines.append((len(cleaned) - len(cleaned.lstrip(" ")), cleaned.strip()))
        return lines

    def _validate(self, data: dict[str, Any]) -> None:
        missing = REQUIRED_TOP_LEVEL_KEYS - data.keys()
        if missing:
            raise ValueError(f"Config is missing required keys: {sorted(missing)}")
        if not isinstance(data["total_events"], int) or data["total_events"] <= 0:
            raise ValueError("Config must define a positive integer total_events")
        if not isinstance(data["steps"], list) or not data["steps"]:
            raise ValueError("Config must define at least one step")
        if isinstance(data["base_output_path"], str):
            data["base_output_path"] = _expand(data["base_output_path"])
        if "gridpack" in data:
            data["gridpack"] = self._validate_gridpack(data["gridpack"])
        self._validate_steps(data["steps"])

    @staticmethod
    def _validate_gridpack(gridpack: Any) -> str:
        error = ValueError("Config gridpack must be a non-empty root:// URI string")
        if not isinstance(gridpack, str):
            raise error
        expanded = _expand(gridpack).strip()
        if not expanded.startswith(GRIDPACK_URI_PREFIX):
            raise error
        host, separator, path = expanded[len(GRIDPACK_URI_PREFIX):].partition("/")
        if not host.strip() or not separator or not any(seg.strip() for seg in path.split("/")):
            raise error
        return expanded

    def _validate_steps(self, steps: list[dict[str, Any]]) -> None:
        for index, step in enumerate(steps, start=1):
            missing = REQUIRED_STEP_KEYS - step.keys()
            if missing:
                raise ValueError(f"Step {index} is missing required keys: {sorted(missing)}")
            setup_ids = step["setup_ids"]
            if not isinstance(setup_ids, list) or not setup_ids:
                raise ValueError(f"Step {index} must define at least one setup_id")
            self._validate_pseudo_stages(steps, index, setup_ids)
            for key in ("events_per_job", "cores_per_job"):
                _positive_int(step, key, index)
            if not isinstance(step["memory_per_job"], (int, float)) or step["memory_per_job"] <= 0:
                raise ValueError(f"Step {index} must define a positive memory_per_job value")
            filename = step.get("filename")
            if filename is not None:
                if not isinstance(filename, str) or not filename.strip():
                    raise ValueError(f"Step {index} filename must be a non-empty string")
                if "/" in filename or "\\" in filename:
                    raise ValueError(f"Step {index} filename must not contain path separators")

    @staticmethod
    def _validate_pseudo_stages(steps: list[dict[str, Any]], index: int, setup_ids: list[str]) -> None:
        kinds = [classify_setup_id(token)[0] for token in setup_ids]
        if "nano" in kinds:
            if setup_ids != [DELPHES_NANO_TOKEN]:
                raise ValueError(
                    f"Step {index}: a 'delphes-nano' step must contain exactly the single "
                    f"'delphes-nano' stage, got {setup_ids!r}"
                )
            if index == 1:
                raise ValueError(f"Step {index} ('delphes-nano') must follow a predecessor delphes step")
            predecessor = steps[index - 2].get("setup_ids", [])
            if not any(classify_setup_id(token)[0] == "delphes" for token in predecessor):
                raise ValueError(
                    f"Step {index} ('delphes-nano'): its predecessor step must be a delphes step "
                    "(contain a 'delphes:<card>' stage)"
                )
        elif "delphes" in kinds:
            if kinds != ["mcm", "delphes"]:
                raise ValueError(
                    f"Step {index}: a delphes step must be [<generator setup_id>, delphes:<card>], "
                    f"got {setup_ids!r}"
                )
            if not classify_setup_id(setup_ids[1])[1]:
                raise ValueError(
                    f"Step {index}: the delphes stage must encode a card path, e.g. "
                    "'delphes:cards/delphes/delphes_card_CMS.tcl'"
                )

    @staticmethod
    def _parse_steps(lines: list[tuple[int, str]], start_index: int) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        index = start_index
        while index < len(lines):
            step_indent, step_text = lines[index]
            if not step_text.startswith("- "):
                if step_indent == 0:
                    break
                raise ValueError(f"Expected step list item, got: {step_text!r}")

            step: dict[str, Any] = {}
            list_key: str | None = None
            if step_text[2:].strip():
                key, value = split_mapping(step_text[2:].strip())
                step[key] = parse_scalar(value or "")

            index += 1
            while index < len(lines):
                child_indent, child_text = lines[index]
                is_item = child_text.startswith("- ")
                if child_indent == step_indent and is_item:
                    break
                if child_indent <= step_indent and not (list_key and is_item):
                    break
                if list_key and is_item:
                    step[list_key].append(parse_scalar(child_text[2:]))
                else:
                    key, value = split_mapping(child_text)
                    list_key = key if value is None else None
                    step[key] = [] if value is None else parse_scalar(value)
                index += 1
            steps.append(step)
        return steps

    @staticmethod
    def unique_setup_ids(config: dict[str, Any]) -> list[str]:
        ids = (s for step in config["steps"] for s in step.get("setup_ids", []) if not is_pseudo_stage(s))
        return list(dict.fromkeys(ids))

    @staticmethod
    def repo_root_from_path(path: Path) -> Path:
        """The directory containing the configs/ ancestor of a config file."""
        for parent in path.resolve().parents:
            if parent.name == "configs":
                return parent.parent
        raise ValueError(f"Could not infer the repository root from {path}")

    @staticmethod
    def repo_root_from_directory(start_path: Path) -> Path:
        resolved = start_path.resolve()
        for candidate in (resolved, *resolved.parents):
            if (candidate / "configs").is_dir():
                return candidate
        raise ValueError(f"Could not locate a repository root containing configs/ from {start_path}")

    @staticmethod
    def config_paths(repo_root: Path) -> list[Path]:
        config_root = repo_root / "configs"
        if not config_root.is_dir():
            raise FileNotFoundError(f"Could not find configs/ under {repo_root}")
        return sorted(path for path in config_root.rglob("*.yaml") if path.is_file())

    @staticmethod
    def relative_config_location(config_path: Path, repo_root: Path) -> tuple[Path, str]:
        config_root = repo_root / "configs"
        resolved = config_path.resolve()
        try:
            relative = resolved.relative_to(config_root)
        except ValueError as exc:
            raise ValueError(f"{config_path} is not located under {config_root}") from exc
        return relative.parent, resolved.stem

    @staticmethod
    def infer_campaign_directory(setup_ids: list[str]) -> str:
        if not setup_ids:
            return "unknown-campaign"
        shared = os.path.commonprefix(setup_ids).rstrip("-_")
        if shared and "-Run" in shared and len(shared) >= 10:
            return shared
        return setup_ids[0].rsplit("-", 1)[0]

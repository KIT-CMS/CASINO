from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from domain.cmsdriver import MONITORING_CUSTOMISE, RUNTIME_MANAGED_OPTIONS
from .mcm import normalize_mcm_payload


class SetupScriptParser:
    """Extracts the cmsDriver.py invocations from the test body embedded in an McM setup script."""

    TEST_BODY_RE = re.compile(r"cat <<'EndOfTestFile' > [^\n]+\n(?P<body>.*?)\nEndOfTestFile", re.DOTALL)
    CMSDRIVER_LINE_RE = re.compile(r"^\s*cmsDriver\.py\b.*$", re.MULTILINE)
    SCRAM_ARCH_RE = re.compile(r"^\s*export SCRAM_ARCH=([^\s]+)\s*$", re.MULTILINE)
    CMSSW_RELEASE_RE = re.compile(r"scram p CMSSW\s+([^\s]+)")
    KNOWN_FLAG_OPTIONS = {"--mc", "--data", "--no_exec", "--no_output", "--relval"}
    EVENTCONTENT_REWRITES = ((re.compile(r"NANOEDMAODSIM"), "NANOAODSIM"),)
    IGNORED_OPTION_PAIRS = {("--customise", MONITORING_CUSTOMISE)}

    def extract_test_body(self, script_text: str) -> str:
        match = self.TEST_BODY_RE.search(script_text)
        if not match:
            raise ValueError("Could not extract embedded test script from McM setup")
        return match.group("body")

    @staticmethod
    def clean_cmsdriver_line(line: str) -> str:
        cleaned = re.sub(r"\s*\|\|\s*exit\s+\$\?\s*;?\s*$", "", line.strip())
        return cleaned.rstrip(";").strip()

    def normalize_option_value(self, option_name: str, value: str | None) -> str | None:
        if option_name != "--eventcontent" or value is None:
            return value
        for pattern, replacement in self.EVENTCONTENT_REWRITES:
            value = pattern.sub(replacement, value)
        return value

    def parse_cmsdriver_command(self, command_line: str) -> dict[str, Any]:
        tokens = shlex.split(self.clean_cmsdriver_line(command_line))
        if not tokens or tokens[0] != "cmsDriver.py":
            raise ValueError(f"Unsupported cmsDriver command: {command_line!r}")

        fragment_argument: str | None = None
        index = 1
        if index < len(tokens) and not tokens[index].startswith("-"):
            fragment_argument = tokens[index]
            index += 1

        options: list[dict[str, str | None]] = []
        named: dict[str, str | None] = {}
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("--"):
                options.append({"name": None, "value": token})
                index += 1
                continue
            has_value = (
                token not in self.KNOWN_FLAG_OPTIONS
                and index + 1 < len(tokens)
                and not tokens[index + 1].startswith("--")
            )
            value = self.normalize_option_value(token, tokens[index + 1] if has_value else None)
            if token not in RUNTIME_MANAGED_OPTIONS and (token, value) not in self.IGNORED_OPTION_PAIRS:
                options.append({"name": token, "value": value})
            named[token] = value
            index += 2 if has_value else 1

        for required in ("--fileout", "--python_filename"):
            if not named.get(required):
                raise ValueError(f"cmsDriver command is missing {required}: {command_line!r}")

        return {
            "raw": command_line,
            "fragment_argument": fragment_argument,
            "options": options,
            "filein": named.get("--filein"),
            "fileout": named["--fileout"],
            "python_filename": named["--python_filename"],
        }

    def parse_setup_script(self, script_path: Path, fragment_path: Path | None) -> dict[str, Any]:
        test_body = self.extract_test_body(normalize_mcm_payload(script_path.read_text(encoding="utf-8")))
        scram_arch = self.SCRAM_ARCH_RE.search(test_body)
        if not scram_arch:
            raise ValueError(f"Could not find SCRAM_ARCH in {script_path}")
        release = self.CMSSW_RELEASE_RE.search(test_body)
        if not release:
            raise ValueError(f"Could not find the CMSSW release in {script_path}")
        commands = [self.parse_cmsdriver_command(line) for line in self.CMSDRIVER_LINE_RE.findall(test_body)]
        if not commands:
            raise ValueError(f"Could not find cmsDriver commands in {script_path}")
        return {
            "script_path": str(script_path.resolve()),
            "fragment_path": str(fragment_path.resolve()) if fragment_path else None,
            "scram_arch": scram_arch.group(1),
            "cmssw_release": release.group(1),
            "commands": commands,
        }

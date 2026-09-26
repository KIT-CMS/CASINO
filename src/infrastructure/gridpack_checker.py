from __future__ import annotations

import os
import shutil
import subprocess

# The conda/LCG Python env must not leak into gfal's own Python.
_ENV_BLOCKLIST = {"PYTHONHOME", "PYTHONPATH", "GFAL_PYTHONBIN", "GFAL_PLUGIN_DIR", "GFAL_CONFIG_DIR"}
_GFAL_IMPORT_CHECK = "import gfal2, gfal2_util"
_PREFERRED_GFAL_PYTHONS = ("/usr/bin/python3",)


class GfalGridpackChecker:
    def __init__(self, timeout: int = 10, executable: str = "gfal-stat") -> None:
        self.timeout = timeout
        self.executable = executable

    def ensure_exists(self, gridpack_uri: str) -> None:
        if shutil.which(self.executable) is None:
            raise FileNotFoundError(
                f"Cannot check custom gridpack {gridpack_uri!r}: {self.executable} is not available in PATH"
            )
        env = {key: value for key, value in os.environ.items() if key not in _ENV_BLOCKLIST}
        gfal_python = _gfal_pythonbin(env)
        if gfal_python:
            env["GFAL_PYTHONBIN"] = gfal_python
        result = subprocess.run(
            [self.executable, "-t", str(self.timeout), gridpack_uri],
            check=False, capture_output=True, env=env, text=True,
        )
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout).strip()
        message = f"Gridpack does not exist or is not accessible: {gridpack_uri}"
        raise FileNotFoundError(f"{message} ({detail})" if detail else message)


def _gfal_pythonbin(env: dict[str, str]) -> str | None:
    for candidate in _PREFERRED_GFAL_PYTHONS:
        if not os.access(candidate, os.X_OK):
            continue
        probe = subprocess.run([candidate, "-c", _GFAL_IMPORT_CHECK], check=False, capture_output=True, env=env, text=True)
        if probe.returncode == 0:
            return candidate
    return None

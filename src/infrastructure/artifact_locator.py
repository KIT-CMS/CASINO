from __future__ import annotations

from pathlib import Path

from domain.configuration import ConfigLoader
from .mcm import extract_fragment_ids, normalize_mcm_payload


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


class ArtifactLocator:
    """Finds downloaded setup scripts and fragments, tolerating older flat layouts."""

    def __init__(self, config_loader: ConfigLoader) -> None:
        self.config_loader = config_loader

    def candidate_sample_directories(self, config_path: Path, repo_root: Path, driver_root: Path) -> list[Path]:
        relative_config_dir, sample_name = self.config_loader.relative_config_location(config_path, repo_root)
        candidates = (driver_root / relative_config_dir / sample_name, driver_root / sample_name)
        return [candidate for candidate in candidates if candidate.exists()]

    @staticmethod
    def find_setup_script(setup_id: str, sample_directories: list[Path]) -> Path:
        for sample_dir in sample_directories:
            if (sample_dir / f"{setup_id}.sh").exists():
                return sample_dir / f"{setup_id}.sh"
        for sample_dir in sample_directories:
            nested = sorted(sample_dir.rglob(f"{setup_id}.sh"))
            if nested:
                return nested[0]
        for sample_dir in sample_directories:
            for script_path in sample_dir.rglob("*.sh"):
                content = _read_text(script_path)
                if content and f"get_setup/{setup_id}" in content:
                    return script_path
        raise FileNotFoundError(f"Could not locate a setup script for {setup_id}")

    @staticmethod
    def find_fragment_file(setup_id: str, sample_directories: list[Path], script_path: Path | None = None) -> Path | None:
        fragment_ids = [setup_id]
        if script_path is not None:
            fragment_ids = extract_fragment_ids(normalize_mcm_payload(script_path.read_text(encoding="utf-8"))) or fragment_ids

        for sample_dir in sample_directories:
            for fragment_id in fragment_ids:
                preferred = sample_dir / "fragments" / f"{fragment_id}-fragment.py"
                if preferred.exists():
                    return preferred
        for sample_dir in sample_directories:
            for fragment_path in sample_dir.rglob("*.py"):
                content = _read_text(fragment_path)
                if content and any(f"get_fragment/{fragment_id}" in content for fragment_id in fragment_ids):
                    return fragment_path
        if script_path is not None and script_path.with_name("fragment.py").exists():
            return script_path.with_name("fragment.py")
        return None

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from domain.configuration import ConfigLoader
from .console import eprint
from .filesystem import write_text
from .mcm import (
    MCM_FRAGMENT_URL,
    MCM_SETUP_URL,
    McMResponseError,
    extract_fragment_ids,
    fetch_text,
    validate_mcm_artifact_payload,
)


@dataclass(slots=True)
class DownloadAllResult:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def _is_valid_artifact(path: Path, kind: str, artifact_id: str) -> str | None:
    """Return the validated text of a cached McM artifact, or None if it is missing or corrupt."""
    if not path.exists():
        return None
    try:
        return validate_mcm_artifact_payload(
            path.read_text(encoding="utf-8"), resource_kind=kind, setup_id=artifact_id,
        )
    except (McMResponseError, UnicodeDecodeError):
        return None


class DownloadManager:
    """Mirrors McM setup scripts and fragments below artifacts/mcm-commands/<rel>/<sample>/."""

    def __init__(self, config_loader: ConfigLoader, fetcher: Callable[[str, int, bool], str] = fetch_text) -> None:
        self.config_loader = config_loader
        self.fetcher = fetcher

    def sample_root(self, config_path: Path, repo_root: Path, driver_root: Path) -> Path:
        relative_config_dir, sample_name = self.config_loader.relative_config_location(config_path, repo_root)
        return driver_root / relative_config_dir / sample_name

    def _layout(self, config_path: Path, driver_root: Path) -> tuple[Path, Path, list[str]]:
        repo_root = self.config_loader.repo_root_from_path(config_path)
        config = self.config_loader.load(config_path)
        sample_root = self.sample_root(config_path, repo_root, driver_root)
        setup_ids = self.config_loader.unique_setup_ids(config)
        campaign_dir = sample_root / self.config_loader.infer_campaign_directory(setup_ids)
        return sample_root, campaign_dir, setup_ids

    @staticmethod
    def _fragment_path(sample_root: Path, fragment_id: str) -> Path:
        return sample_root / "fragments" / f"{fragment_id}-fragment.py"

    def _fetch(
        self, url: str, kind: str, artifact_id: str, path: Path, *, timeout: int, overwrite: bool, verify_certs: bool,
    ) -> str:
        eprint(f"Downloading {kind} {artifact_id} from {url}")
        text = validate_mcm_artifact_payload(
            self.fetcher(url, timeout=timeout, verify_certs=verify_certs), resource_kind=kind, setup_id=artifact_id,
        )
        replace = overwrite or _is_valid_artifact(path, kind, artifact_id) is None
        write_text(path, text, overwrite=replace)
        return text

    def download(self, config_path: Path, driver_root: Path, timeout: int, overwrite: bool, verify_certs: bool) -> Path:
        sample_root, campaign_dir, setup_ids = self._layout(config_path, driver_root)
        options = dict(timeout=timeout, overwrite=overwrite, verify_certs=verify_certs)
        for setup_id in setup_ids:
            script_text = self._fetch(
                MCM_SETUP_URL.format(setup_id=setup_id), "setup", setup_id, campaign_dir / f"{setup_id}.sh", **options,
            )
            for fragment_id in extract_fragment_ids(script_text):
                self._fetch(
                    MCM_FRAGMENT_URL.format(setup_id=fragment_id), "fragment", fragment_id,
                    self._fragment_path(sample_root, fragment_id), **options,
                )
        eprint(f"Wrote downloads to {sample_root}")
        return sample_root

    def has_download_entry(self, config_path: Path, repo_root: Path, driver_root: Path) -> bool:
        del repo_root
        sample_root, campaign_dir, setup_ids = self._layout(config_path, driver_root)
        for setup_id in setup_ids:
            script_text = _is_valid_artifact(campaign_dir / f"{setup_id}.sh", "setup", setup_id)
            if script_text is None:
                return False
            for fragment_id in extract_fragment_ids(script_text):
                if _is_valid_artifact(self._fragment_path(sample_root, fragment_id), "fragment", fragment_id) is None:
                    return False
        return True

    def download_all(
        self, repo_root: Path, driver_root: Path, timeout: int, overwrite: bool, verify_certs: bool,
    ) -> DownloadAllResult:
        result = DownloadAllResult()
        for config_path in self.config_loader.config_paths(repo_root):
            try:
                if not overwrite and self.has_download_entry(config_path, repo_root, driver_root):
                    eprint(f"Skipping {config_path}: found existing download entry in {driver_root}")
                    result.skipped += 1
                    continue
                self.download(config_path, driver_root, timeout, overwrite, verify_certs)
            except Exception as exc:
                result.failed += 1
                eprint(f"Failed {config_path}: {exc}")
                continue
            result.downloaded += 1
        eprint(f"Download summary: downloaded={result.downloaded} skipped={result.skipped} failed={result.failed}")
        return result

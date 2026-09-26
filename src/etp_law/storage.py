"""WLCG storage helpers: existence checks, directory listings, stage-out and per-root filesystems.

Everything shells out to the gfal-*/xrdfs/xrdcp CLIs: the submitter Python may lack gfal2
bindings and the worker container has no usable gfal Python, while xrdfs/xrdcp ship with every
LCG view.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .compat import import_law_dependencies

law, luigi = import_law_dependencies()
law.contrib.load("wlcg")
logger = law.logger.get_logger(__name__)

WLCG_FS_CACHE: dict[str, object] = {}
WEBDAV_BASE_NAMES = ("stat", "exists", "chmod", "unlink", "rmdir", "mkdir", "listdir", "filecopy")
DIR_LISTING_TIMEOUT = 120  # a hung storage door falls over to per-branch checks instead of blocking
STAGE_OUT_RETRIES = 10
STAGE_OUT_RETRY_DELAY = 5 * 60

_XROOTD_URI_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<host>[^/]+)/+(?P<path>.*)$")
_KIT_XROOTD_RE = re.compile(r"^root://cmsdcache-kit-disk\.gridka\.de(?::\d+)?/+(?P<path>.*)$")
_KIT_WEBDAV_BASE = "davs://cmsdcache-kit-disk.gridka.de:2880/pnfs/gridka.de/cms/disk-only"
_MISSING_DIR_MARKERS = ("no such file", "does not exist", "not found", "errno 2")


def is_wlcg_target(target) -> bool:
    target_cls = getattr(getattr(law, "wlcg", None), "WLCGFileTarget", None)
    return target_cls is not None and isinstance(target, target_cls)


def xrootd_components(uri: str) -> tuple[str, str]:
    """root://host[:port]//path -> (host, /path)"""
    match = _XROOTD_URI_RE.match(uri)
    if not match:
        raise ValueError(f"Cannot parse xrootd URI: {uri!r}")
    return match.group("host"), "/" + match.group("path")


def _target_uri(target, base_name: str | None) -> str | None:
    uri_method = getattr(target, "uri", None)
    if not callable(uri_method):
        return None
    try:
        uri = uri_method(base_name=base_name) if base_name else uri_method()
    except TypeError:
        return None
    except Exception as exc:
        if base_name:
            logger.warn(f"target.uri(base_name={base_name!r}) failed; falling back to xrdfs: {exc!r}")
        return None
    return uri if isinstance(uri, str) else None


def _gfal_uri(target, base_name: str) -> str | None:
    """A non-xrootd (davs://) URI for the given law base, or None."""
    uri = _target_uri(target, base_name)
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", uri) if uri else None
    return uri if match and match.group(1).lower() != "root" else None


def _xrootd_uri(target) -> str | None:
    uri = _target_uri(target, None)
    return uri if uri and uri.startswith("root://") else None


def _gfal_exists(target) -> bool | None:
    uri = _gfal_uri(target, "stat")
    gfal_stat = shutil.which("gfal-stat") if uri else None
    if not gfal_stat:
        return None
    try:
        completed = subprocess.run([gfal_stat, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return None
    # Non-zero can be ENOENT or a transient error; let xrdfs decide rather than report "missing".
    return True if completed.returncode == 0 else None


def _xrdfs_exists(target) -> bool | None:
    uri = _xrootd_uri(target)
    xrdfs = shutil.which("xrdfs") if uri else None
    if not xrdfs:
        return None
    try:
        host, remote_path = xrootd_components(uri)
        completed = subprocess.run(
            [xrdfs, host, "stat", remote_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except (ValueError, OSError):
        return None
    return completed.returncode == 0


def target_exists(target) -> bool:
    """Existence check that never raises: gfal-stat (davs), then xrdfs stat, then law's exists()."""
    if is_wlcg_target(target):
        for probe in (_gfal_exists, _xrdfs_exists):
            result = probe(target)
            if result is not None:
                return result
    try:
        return target.exists()
    except Exception:
        return False


def _run_listing(argv: list[str]) -> set[str] | None:
    try:
        completed = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True, timeout=DIR_LISTING_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode == 0:
        return {os.path.basename(line.strip()) for line in completed.stdout.splitlines() if line.strip()}
    stderr = (completed.stderr or "").strip()
    if any(marker in stderr.lower() for marker in _MISSING_DIR_MARKERS):
        return set()
    logger.warn(f"{' '.join(argv)} failed (rc={completed.returncode}); falling back to per-branch checks: {stderr!r}")
    return None


def _gfal_listing(target) -> set[str] | None:
    uri = _gfal_uri(target, "listdir")
    gfal_ls = shutil.which("gfal-ls") if uri else None
    return _run_listing([gfal_ls, uri.rsplit("/", 1)[0]]) if gfal_ls else None


def _xrdfs_listing(target) -> set[str] | None:
    uri = _xrootd_uri(target)
    xrdfs = shutil.which("xrdfs") if uri else None
    if not xrdfs:
        return None
    try:
        host, remote_path = xrootd_components(uri)
    except ValueError:
        return None
    return _run_listing([xrdfs, host, "ls", remote_path.rsplit("/", 1)[0] or "/"])


def dir_listing(target) -> set[str] | None:
    """Basenames in the directory holding target, from one gfal-ls / xrdfs ls call.

    An absent directory yields an empty set. None means listing was impossible or failed
    transiently; callers must then fall back to per-file checks, never treat it as empty.
    """
    if not is_wlcg_target(target):
        return None
    listing = _gfal_listing(target)
    return listing if listing is not None else _xrdfs_listing(target)


def listing_backed_exists():
    """Existence predicate for a collection of WLCG outputs: one listing per directory, cached."""
    listings: dict[str, set[str] | None] = {}

    def exists(target) -> bool:
        path = getattr(target, "path", None)
        if not is_wlcg_target(target) or not isinstance(path, str):
            return target_exists(target)
        directory = os.path.dirname(path)
        if directory not in listings:
            listings[directory] = dir_listing(target)
        listing = listings[directory]
        return os.path.basename(path) in listing if listing is not None else target_exists(target)

    return exists


class CasinoTargetCollection(law.target.collection.TargetCollection):
    def _iter_state(self, *args, **kwargs):
        kwargs.setdefault("exists_func", listing_backed_exists())
        return super()._iter_state(*args, **kwargs)


def stage_out(
    target, local_path: Path, retries: int = STAGE_OUT_RETRIES, retry_delay: int = STAGE_OUT_RETRY_DELAY,
) -> None:
    """Upload local_path to target: shutil for POSIX targets, xrdfs mkdir + xrdcp for WLCG targets."""
    if not is_wlcg_target(target):
        target.copy_from_local(str(local_path), retries=retries, retry_delay=retry_delay)
        return
    dst_uri = target.uri()
    host, remote_path = xrootd_components(dst_uri)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            subprocess.run(["xrdfs", host, "mkdir", "-p", os.path.dirname(remote_path)], check=True)
            subprocess.run(["xrdcp", "-f", "--nopbar", str(local_path), dst_uri], check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_delay)
    raise RuntimeError(f"xrdcp failed to stage {local_path} -> {dst_uri} after {retries + 1} attempts: {last_error}")


def wlcg_fs_for(output_root: str | None):
    """A WLCGFileSystem pinned to output_root, or None to use law.cfg's [wlcg_fs].

    law deep-merges [wlcg_fs] into every WLCGFileSystem, so a custom root must override each
    base_* explicitly: KIT-shaped roots get the matching davs base, everything else the root itself.
    """
    if not output_root:
        return None
    fs_cls = getattr(law.wlcg, "WLCGFileSystem", None)
    if fs_cls is None:
        return None
    try:
        default_base = law.config.get_expanded("wlcg_fs", "base")
    except Exception:
        default_base = None
    if default_base and output_root.rstrip("/") == default_base.rstrip("/"):
        return None
    if output_root not in WLCG_FS_CACHE:
        match = _KIT_XROOTD_RE.match(output_root.rstrip("/"))
        base = f"{_KIT_WEBDAV_BASE}/{match.group('path').strip('/')}" if match else output_root
        WLCG_FS_CACHE[output_root] = fs_cls(base=output_root, bases={name: base for name in WEBDAV_BASE_NAMES})
    return WLCG_FS_CACHE[output_root]

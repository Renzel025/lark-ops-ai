"""
Shared draft/preview storage for multi-worker processes (Gunicorn, etc.).

When ``P0_SHARED_STATE_DIR`` is set to a writable directory, drafts and previews are
stored as JSON files with ``fcntl`` locks so every worker shares the same state.

When unset, behavior matches the original in-process dicts (single-worker only).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from typing import Any, Dict, Iterator, Optional

log = logging.getLogger("lark-ops-ai")

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore

_MEM_LOCK = threading.Lock()
_MEM_DRAFTS: Dict[str, Dict[str, Any]] = {}
_MEM_PREVIEWS: Dict[str, Dict[str, Any]] = {}

_logged_disk = False


def _reload_env() -> None:
    try:
        from . import config as _config

        _config.reload_env_runtime()
    except Exception:
        pass


def shared_state_dir() -> str:
    _reload_env()
    return (os.getenv("P0_SHARED_STATE_DIR") or "").strip()


def disk_enabled() -> bool:
    return bool(_fcntl and shared_state_dir())


def _safe_oid(oid: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in (oid or ""))[:220] or "unknown"


def _draft_path(oid: str) -> str:
    base = shared_state_dir()
    return os.path.join(base, "drafts", f"{_safe_oid(oid)}.json")


def _preview_path(oid: str) -> str:
    base = shared_state_dir()
    return os.path.join(base, "previews", f"{_safe_oid(oid)}.json")


def _log_disk_once() -> None:
    global _logged_disk
    if not _logged_disk and disk_enabled():
        _logged_disk = True
        log.info(
            "P0_SHARED_STATE_DIR=%s — draft/preview JSON is shared across workers (set this for Gunicorn -w >1).",
            shared_state_dir(),
        )


@contextlib.contextmanager
def draft_transaction(oid: str) -> Iterator[Any]:
    """Exclusive read-modify-write for one operator draft."""
    oid = (oid or "").strip()
    if not oid:
        raise ValueError("draft_transaction: empty open_id")
    if not disk_enabled():
        with _MEM_LOCK:
            yield _MemDraftOps(oid)
        return
    _log_disk_once()
    assert _fcntl is not None
    path = _draft_path(oid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lf:
        _fcntl.flock(lf, _fcntl.LOCK_EX)
        try:
            yield _FileDraftOps(path)
        finally:
            _fcntl.flock(lf, _fcntl.LOCK_UN)


@contextlib.contextmanager
def preview_transaction(oid: str) -> Iterator[Any]:
    oid = (oid or "").strip()
    if not oid:
        raise ValueError("preview_transaction: empty open_id")
    if not disk_enabled():
        with _MEM_LOCK:
            yield _MemPreviewOps(oid)
        return
    _log_disk_once()
    assert _fcntl is not None
    path = _preview_path(oid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lf:
        _fcntl.flock(lf, _fcntl.LOCK_EX)
        try:
            yield _FilePreviewOps(path)
        finally:
            _fcntl.flock(lf, _fcntl.LOCK_UN)


class _MemDraftOps:
    def __init__(self, oid: str) -> None:
        self.oid = oid

    def get(self) -> Optional[Dict[str, Any]]:
        d = _MEM_DRAFTS.get(self.oid)
        return dict(d) if d else None

    def set(self, d: Dict[str, Any]) -> None:
        _MEM_DRAFTS[self.oid] = dict(d)

    def delete(self) -> None:
        _MEM_DRAFTS.pop(self.oid, None)


class _FileDraftOps:
    def __init__(self, path: str) -> None:
        self.path = path

    def get(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return dict(data) if isinstance(data, dict) else None

    def set(self, d: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def delete(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class _MemPreviewOps:
    def __init__(self, oid: str) -> None:
        self.oid = oid

    def get(self) -> Optional[Dict[str, Any]]:
        p = _MEM_PREVIEWS.get(self.oid)
        return dict(p) if p else None

    def set(self, d: Dict[str, Any]) -> None:
        _MEM_PREVIEWS[self.oid] = dict(d)

    def delete(self) -> None:
        _MEM_PREVIEWS.pop(self.oid, None)


class _FilePreviewOps:
    def __init__(self, path: str) -> None:
        self.path = path

    def get(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return dict(data) if isinstance(data, dict) else None

    def set(self, d: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def delete(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

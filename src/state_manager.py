"""
state_manager.py — Thread-safe JSON state persistence.
Uses GID (aria2c download handle) to track each anime's download.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StateManager:
    """
    Manages per-anime download state, persisted to a JSON file.

    State schema per anime entry:
    {
        "last_episode":       1169,
        "current_gid":        "2089b05ede306abc",   ← aria2c GID
        "current_file_path":  "/downloads/...",
        "status":             "downloading" | "complete" | "idle",
        "release_title":      "[SubsPlease] ...",
        "size":               "1.3 GiB",
        "updated_at":         "2026-07-12T16:05:00Z"
    }
    """

    def __init__(self, state_file: str = "data/state.json"):
        self.state_file = state_file
        self._lock = threading.Lock()
        self._init_file()

    def _init_file(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        if not os.path.exists(self.state_file):
            self._write({})

    def _read(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _write(self, data: dict):
        """Atomic write via temp file to prevent corruption."""
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    # ── Public API ────────────────────────────────────────────────

    def get_all(self) -> dict:
        with self._lock:
            return self._read()

    def get_state(self, anime_name: str) -> Optional[dict]:
        with self._lock:
            return self._read().get(anime_name)

    def set_state(self, anime_name: str, state_dict: dict):
        with self._lock:
            data = self._read()
            data[anime_name] = state_dict
            self._write(data)

    def get_episode(self, anime_name: str) -> Optional[int]:
        s = self.get_state(anime_name)
        return s.get("last_episode") if s else None

    def update(
        self,
        anime_name: str,
        episode: int,
        gid: str,
        status: str,
        file_path: str = "",
        release_title: str = "",
        size: str = "",
    ):
        """Create or overwrite the state entry for an anime."""
        with self._lock:
            data = self._read()
            data[anime_name] = {
                "last_episode":      episode,
                "current_gid":       gid,
                "current_file_path": file_path,
                "status":            status,
                "release_title":     release_title,
                "size":              size,
                "updated_at":        datetime.now(timezone.utc).isoformat(),
            }
            self._write(data)
            logger.debug(f"State: {anime_name} → EP{episode} [{status}] gid={gid}")

    def update_status(self, anime_name: str, status: str, file_path: str = ""):
        with self._lock:
            data = self._read()
            if anime_name in data:
                data[anime_name]["status"] = status
                data[anime_name]["updated_at"] = datetime.now(timezone.utc).isoformat()
                if file_path:
                    data[anime_name]["current_file_path"] = file_path
                self._write(data)

    def remove(self, anime_name: str):
        with self._lock:
            data = self._read()
            if anime_name in data:
                del data[anime_name]
                self._write(data)
                logger.info(f"Removed state for: {anime_name}")

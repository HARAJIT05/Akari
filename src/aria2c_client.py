"""
aria2c_client.py — Wrapper around the aria2c JSON-RPC API.

aria2c is started as a Docker service with --enable-rpc.
All downloads are managed via its JSON-RPC endpoint at :6800/jsonrpc.

State key: GID (aria2c's unique download identifier, a 16-char hex string).
"""
import logging
import os
import shutil
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class Aria2Error(Exception):
    """Raised when an aria2c RPC call fails."""


class Aria2Client:
    """
    Thin JSON-RPC client for aria2c.
    Supports magnet links and .torrent URLs.
    """

    def __init__(self, host: str, port: int, secret: str):
        self.rpc_url = f"{host}:{port}/jsonrpc"
        self.secret = secret
        self._req_id = 0

    # ── RPC Core ─────────────────────────────────────────────────

    def _call(self, method: str, *params) -> object:
        """Send a JSON-RPC request and return the result."""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._req_id),
            "method": method,
            "params": [f"token:{self.secret}", *params],
        }
        try:
            resp = requests.post(self.rpc_url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise Aria2Error(f"RPC request failed ({method}): {e}") from e

        if "error" in data:
            raise Aria2Error(f"aria2c error [{method}]: {data['error']['message']}")

        return data.get("result")

    def ping(self) -> bool:
        """Return True if aria2c is reachable and the secret is valid."""
        try:
            self._call("aria2.getGlobalStat")
            return True
        except Aria2Error:
            return False

    # ── Download Management ───────────────────────────────────────

    def add_magnet(self, magnet: str, save_path: str) -> str:
        """
        Queue a magnet link for download.
        Returns the GID (download handle) assigned by aria2c.
        """
        options = {"dir": save_path, "seed-time": "0", "allow-overwrite": "true"}
        gid = self._call("aria2.addUri", [magnet], options)
        logger.info(f"Queued magnet → GID {gid}")
        return gid

    def add_torrent_url(self, torrent_url: str, save_path: str) -> str:
        """
        Download a .torrent file and queue it.
        Returns the GID.
        """
        options = {"dir": save_path, "seed-time": "0", "allow-overwrite": "true"}
        gid = self._call("aria2.addUri", [torrent_url], options)
        logger.info(f"Queued torrent URL → GID {gid}")
        return gid

    def get_status(self, gid: str) -> dict:
        """
        Return the full status dict for a download.
        Fields include: status, totalLength, completedLength,
                        downloadSpeed, files, errorMessage
        """
        try:
            return self._call("aria2.tellStatus", gid) or {}
        except Aria2Error:
            return {}

    def is_complete(self, gid: str) -> bool:
        """Return True when the download has finished (status == 'complete')."""
        status = self.get_status(gid)
        return status.get("status") == "complete"

    def get_progress(self, gid: str) -> float:
        """Return download progress as 0.0–100.0 percent."""
        s = self.get_status(gid)
        total = int(s.get("totalLength", 0))
        completed = int(s.get("completedLength", 0))
        if total == 0:
            return 0.0
        return round(completed / total * 100, 1)

    def get_file_paths(self, gid: str) -> list[str]:
        """Return a list of absolute paths for files in this download."""
        s = self.get_status(gid)
        paths = []
        for f in s.get("files", []):
            p = f.get("path", "")
            if p and not p.endswith(".aria2"):
                paths.append(p)
        return paths

    def get_primary_path(self, gid: str) -> str:
        """Return the path of the largest (main) file in the download."""
        s = self.get_status(gid)
        files = s.get("files", [])
        if not files:
            return ""
        # Pick the largest file (ignores small .nfo / subtitle extras)
        main = max(files, key=lambda f: int(f.get("length", 0)))
        return main.get("path", "")

    def pause(self, gid: str) -> bool:
        """Pause a download."""
        try:
            self._call("aria2.pause", gid)
            return True
        except Aria2Error as e:
            logger.error(f"Failed to pause {gid}: {e}")
            return False

    def unpause(self, gid: str) -> bool:
        """Resume a paused download."""
        try:
            self._call("aria2.unpause", gid)
            return True
        except Aria2Error as e:
            logger.error(f"Failed to unpause {gid}: {e}")
            return False

    def remove(self, gid: str, delete_files: bool = True, fallback_paths: list[str] | None = None) -> bool:
        """
        Remove a download from aria2c's queue/history.
        Optionally deletes the downloaded files from disk.

        If aria2 no longer knows about the GID (e.g. after a container restart
        its in-memory history is lost), `fallback_paths` are deleted directly
        from disk instead so we don't silently skip file cleanup.
        """
        # Get file paths before removing (so we can delete them)
        file_paths = self.get_file_paths(gid) if delete_files else []

        # Try to stop then remove (needed for active downloads)
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                self._call(method, gid)
            except Aria2Error:
                pass  # May already be stopped/removed

        # Delete files from disk
        if delete_files:
            # If aria2 no longer has the GID in memory (returns empty paths),
            # fall back to the paths we saved in state.json
            paths_to_delete = file_paths or (fallback_paths or [])
            for path in paths_to_delete:
                if not path or "[METADATA]" in path:
                    continue
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        logger.info(f"Deleted file: {path}")
                    else:
                        logger.debug(f"Already gone or is a directory: {path}")
                except OSError as e:
                    logger.warning(f"Could not delete {path}: {e}")

        return True

    def get_all_active(self) -> list[dict]:
        """Return all currently active downloads."""
        try:
            return self._call("aria2.tellActive") or []
        except Aria2Error:
            return []

    def get_all_waiting(self, offset: int = 0, limit: int = 50) -> list[dict]:
        """Return downloads in the waiting queue."""
        try:
            return self._call("aria2.tellWaiting", offset, limit) or []
        except Aria2Error:
            return []

    def get_global_stat(self) -> dict:
        """Return global download/upload speed and counts."""
        try:
            return self._call("aria2.getGlobalStat") or {}
        except Aria2Error:
            return {}

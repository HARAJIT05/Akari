"""
qbittorrent_client.py — Wrapper around the qBittorrent Web API v2.
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class QBittorrentError(Exception):
    """Raised when qBittorrent operations fail."""


class QBittorrentClient:
    """
    Thin client for the qBittorrent Web API v2.
    Maintains a session cookie after login.
    """

    # Torrent states that indicate the download phase is complete
    COMPLETE_STATES = frozenset({
        "uploading", "stalledUP", "pausedUP", "stoppedUP",
        "queuedUP", "forcedUP", "checkingUP",
    })

    def __init__(self, host: str, port: int, username: str, password: str):
        self.base_url = f"{host}:{port}/api/v2"
        self.session = requests.Session()
        self.username = username
        self.password = password
        self._logged_in = False

    # ── Auth ──────────────────────────────────────────────────────

    def login(self) -> bool:
        """Authenticate with qBittorrent. Returns True on success."""
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=10,
            )
            if resp.text.strip() == "Ok.":
                self._logged_in = True
                logger.info("✅ Connected to qBittorrent")
                return True
            logger.error(f"qBittorrent login failed: {resp.text.strip()!r}")
            return False
        except requests.RequestException as e:
            logger.error(f"Cannot reach qBittorrent: {e}")
            return False

    def _ensure_auth(self):
        if not self._logged_in:
            if not self.login():
                raise QBittorrentError("Not authenticated with qBittorrent")

    # ── Torrent Management ────────────────────────────────────────

    def add_magnet(self, magnet: str, save_path: str, category: str = "") -> bool:
        """Add a magnet link to the download queue."""
        self._ensure_auth()
        try:
            data = {"urls": magnet, "savepath": save_path}
            if category:
                data["category"] = category
            resp = self.session.post(
                f"{self.base_url}/torrents/add", data=data, timeout=15
            )
            if resp.text.strip() == "Ok.":
                logger.info(f"Added magnet to qBittorrent (category={category!r})")
                return True
            logger.error(f"Failed to add magnet: {resp.text.strip()!r}")
            return False
        except requests.RequestException as e:
            logger.error(f"Error adding magnet: {e}")
            return False

    def get_torrent(self, info_hash: str) -> Optional[dict]:
        """Return torrent metadata dict for the given hash, or None."""
        self._ensure_auth()
        try:
            resp = self.session.get(
                f"{self.base_url}/torrents/info",
                params={"hashes": info_hash.lower()},
                timeout=10,
            )
            data = resp.json()
            return data[0] if data else None
        except (requests.RequestException, ValueError, IndexError) as e:
            logger.error(f"Error getting torrent {info_hash[:12]}: {e}")
            return None

    def is_download_complete(self, info_hash: str) -> bool:
        """Returns True when the torrent has finished downloading."""
        torrent = self.get_torrent(info_hash)
        if not torrent:
            return False
        return torrent.get("state", "") in self.COMPLETE_STATES

    def get_progress(self, info_hash: str) -> float:
        """Return download progress as a float 0.0–1.0."""
        torrent = self.get_torrent(info_hash)
        return torrent.get("progress", 0.0) if torrent else 0.0

    def get_content_path(self, info_hash: str) -> Optional[str]:
        """Return the local path to the downloaded file/folder."""
        torrent = self.get_torrent(info_hash)
        if torrent:
            return torrent.get("content_path") or torrent.get("save_path")
        return None

    def delete_torrent(self, info_hash: str, delete_files: bool = True) -> bool:
        """Remove a torrent from qBittorrent, optionally deleting downloaded files."""
        self._ensure_auth()
        try:
            self.session.post(
                f"{self.base_url}/torrents/delete",
                data={
                    "hashes": info_hash.lower(),
                    "deleteFiles": "true" if delete_files else "false",
                },
                timeout=10,
            )
            logger.info(f"Deleted torrent {info_hash[:12]}... (delete_files={delete_files})")
            return True
        except requests.RequestException as e:
            logger.error(f"Error deleting torrent: {e}")
            return False

    def get_all_torrents(self, category: str = "") -> list[dict]:
        """Return all torrents, optionally filtered by category."""
        self._ensure_auth()
        try:
            params = {}
            if category:
                params["category"] = category
            resp = self.session.get(
                f"{self.base_url}/torrents/info", params=params, timeout=10
            )
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Error listing torrents: {e}")
            return []

    def get_transfer_info(self) -> dict:
        """Global transfer statistics (speeds, session data)."""
        self._ensure_auth()
        try:
            return self.session.get(f"{self.base_url}/transfer/info", timeout=10).json()
        except (requests.RequestException, ValueError):
            return {}

    def create_category(self, name: str, save_path: str = "") -> bool:
        """Create a qBittorrent category if it doesn't exist."""
        self._ensure_auth()
        try:
            self.session.post(
                f"{self.base_url}/torrents/createCategory",
                data={"category": name, "savePath": save_path},
                timeout=10,
            )
            return True
        except requests.RequestException:
            return False

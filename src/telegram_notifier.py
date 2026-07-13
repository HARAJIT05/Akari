"""
telegram_notifier.py — Sends formatted Telegram notifications via Bot API.
"""
import logging

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """
    Sends HTML-formatted messages to a Telegram chat via the Bot API.
    Silently no-ops if bot_token or chat_id are not configured.
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip() if bot_token else ""
        self.chat_id = str(chat_id).strip() if chat_id else ""
        self.enabled = bool(self.bot_token and self.chat_id)

    @property
    def _send_url(self) -> str:
        return f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"

    def send(self, text: str) -> bool:
        """
        Send a plain or HTML message to the configured chat.
        Returns True on success, False on failure.
        """
        if not self.enabled:
            logger.debug("Telegram not configured — skipping notification")
            return False
        try:
            resp = requests.post(
                self._send_url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    # ── Canned Notifications ──────────────────────────────────────

    def notify_started(self, anime_count: int = 0):
        count_str = f" Tracking <b>{anime_count}</b> anime." if anime_count else ""
        self.send(
            f"🤖 <b>Akari is Online!</b>\n\n"
            f"Monitoring Nyaa.si for new episodes.{count_str}\n"
            f"I'll notify you the moment a new episode finishes downloading. 🎌"
        )

    def notify_downloading(self, anime_name: str, episode: int, release_title: str):
        self.send(
            f"⬇️ <b>Downloading new episode...</b>\n\n"
            f"📺 <b>{anime_name}</b> — Episode <b>{episode}</b>\n"
            f"📦 {release_title}"
        )

    def notify_complete(
        self, anime_name: str, episode: int, release_title: str, size: str
    ):
        self.send(
            f"✅ <b>New Episode Ready!</b>\n\n"
            f"🎌 <b>{anime_name}</b> — Episode <b>{episode}</b>\n"
            f"📦 {release_title}\n"
            f"💾 Size: {size}\n\n"
            f"Enjoy watching! 🍿"
        )

    def notify_error(self, anime_name: str, error: str):
        self.send(
            f"❌ <b>Error</b> — {anime_name}\n\n"
            f"<code>{error[:400]}</code>"
        )

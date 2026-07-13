import logging
import threading
import time
import requests
from pathlib import Path
from aria2c_client import Aria2Client
from state_manager import StateManager
import yaml
import os

def load_config() -> dict:
    import yaml
    import os
    cfg_path = os.environ.get("CONFIG_PATH", "config.yaml")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}

logger = logging.getLogger(__name__)

class TelegramCommandBot:
    def __init__(self, config: dict, aria2: Aria2Client, state: StateManager):
        self.config = config
        self.aria2 = aria2
        self.state = state
        self.tg_cfg = config.get("telegram", {})
        self.bot_token = self.tg_cfg.get("bot_token", "")
        self.chat_id = str(self.tg_cfg.get("chat_id", ""))
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.offset = 0
        self.running = False
        self.check_now_flag = Path("data/.check_now")

    def _send_message(self, text: str):
        if not self.bot_token or not self.chat_id:
            return
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        try:
            requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Telegram reply failed: {e}")

    def _handle_command(self, text: str):
        cmd = text.split()[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        if cmd == "/start":
            self._send_message(
                "🤖 <b>Akari Controller</b>\n\n"
                "Available commands:\n"
                "• /status - View active downloads\n"
                "• /check - Force a check for new episodes\n"
                "• /list - View tracked anime list"
            )
        elif cmd == "/status":
            try:
                active = self.aria2.get_all_active()
                waiting = self.aria2.get_all_waiting()
                torrents = active + waiting
                if not torrents:
                    self._send_message("✅ No active downloads.")
                    return
                
                lines = ["📊 <b>Active Downloads</b>\n"]
                for t in torrents:
                    name = t.get("files", [{}])[0].get("path", "").split("/")[-1] if t.get("files") else t.get("gid", "?")
                    total = int(t.get("totalLength", 1))
                    completed = int(t.get("completedLength", 0))
                    pct = round((completed / max(total, 1)) * 100, 1)
                    speed = int(t.get("downloadSpeed", 0)) / 1_048_576
                    state = "⏸️" if t.get("status") == "paused" else "⏳"
                    lines.append(f"{state} <b>{name}</b>\n    {pct}% @ {speed:.1f} MB/s")
                
                self._send_message("\n\n".join(lines))
            except Exception as e:
                self._send_message(f"❌ Error getting status: {e}")

        elif cmd == "/check":
            self.check_now_flag.touch()
            self._send_message("⚡ Manual check triggered! The bot is scanning Nyaa.si now.")

        elif cmd == "/list":
            anime_list = self.config.get("anime", [])
            if not anime_list:
                self._send_message("No anime currently tracked.")
                return
                
            all_states = self.state.get_all()
            lines = ["📺 <b>Tracked Anime</b>\n"]
            for a in anime_list:
                name = a["name"]
                s = all_states.get(name, {})
                ep = s.get("last_episode", "None")
                lines.append(f"• <b>{name}</b> (Latest: EP{ep})")
            
            self._send_message("\n".join(lines))
        else:
            self._send_message("❓ Unknown command. Type /start for a list of commands.")

    def _poll(self):
        logger.info("Telegram command listener started.")
        while self.running:
            try:
                # Reload config to keep token/chat_id fresh if changed in dashboard
                self.config = load_config()
                new_token = self.config.get("telegram", {}).get("bot_token", "")
                new_chat = str(self.config.get("telegram", {}).get("chat_id", ""))
                
                if not new_token or not new_chat:
                    time.sleep(10)
                    continue
                    
                if new_token != self.bot_token:
                    self.bot_token = new_token
                    self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
                self.chat_id = new_chat

                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self.offset, "timeout": 30},
                    timeout=40
                )
                data = resp.json()
                
                if data.get("ok"):
                    for update in data.get("result", []):
                        self.offset = update["update_id"] + 1
                        msg = update.get("message")
                        if not msg:
                            continue
                        
                        # Only respond to the configured chat_id for security
                        if str(msg.get("chat", {}).get("id")) != self.chat_id:
                            continue
                            
                        text = msg.get("text", "")
                        if text.startswith("/"):
                            self._handle_command(text)
            except requests.exceptions.Timeout:
                pass # Long polling timeout, normal
            except Exception as e:
                logger.error(f"Telegram polling error: {e}")
                time.sleep(5)

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._poll, daemon=True).start()

    def stop(self):
        self.running = False

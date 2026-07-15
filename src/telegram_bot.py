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

# ── Progress bar helper ───────────────────────────────────────────────────────

_BAR_LEN   = 16          # number of filled/empty blocks in the bar
_BAR_FULL  = "█"
_BAR_HALF  = "▓"
_BAR_EMPTY = "░"

def _progress_bar(pct: float) -> str:
    """Return a Unicode progress bar like: ████████░░░░░░░░ 50.0%"""
    filled = int(_BAR_LEN * pct / 100)
    half   = 1 if ((_BAR_LEN * pct / 100) - filled) >= 0.5 else 0
    empty  = _BAR_LEN - filled - half
    bar    = _BAR_FULL * filled + _BAR_HALF * half + _BAR_EMPTY * empty
    return f"{bar} {pct:.1f}%"

def _fmt_speed(bps: int) -> str:
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps} B/s"

def _fmt_size(b: int) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.2f} GiB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MiB"
    if b >= 1024:
        return f"{b / 1024:.0f} KiB"
    return f"{b} B"

def _fmt_eta(secs: int) -> str:
    if not secs or secs < 0 or secs > 8_640_000:
        return "∞"
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def _torrent_display_name(t: dict, state_map: dict) -> str:
    """
    Try to return the anime name from state (pretty), then fall back
    to the file name from aria2's files list.
    """
    gid = t.get("gid", "")
    # Match GID against tracked state entries
    for anime_name, s in state_map.items():
        if s.get("current_gid") == gid:
            ep = s.get("last_episode", "?")
            return f"{anime_name}  EP{ep}"
    # Fallback: last path component of the first file
    files = t.get("files", [])
    if files:
        path = files[0].get("path", "")
        name = path.split("/")[-1]
        if name and "[METADATA]" not in name:
            return name[:50]
    return gid or "?"

def _build_status_text(torrents: list, state_map: dict, tick: int) -> str:
    """
    Build the full status message.  `tick` cycles 0-3 to animate a spinner.
    """
    spinners = ["⠋", "⠙", "⠹", "⠸"]
    spin     = spinners[tick % len(spinners)]

    if not torrents:
        return "✅ <b>No active downloads right now.</b>"

    lines = [f"{spin} <b>Live Download Status</b>\n"]
    for t in torrents:
        total     = int(t.get("totalLength", 0))
        completed = int(t.get("completedLength", 0))
        speed     = int(t.get("downloadSpeed", 0))
        eta_raw   = int(t.get("eta", 0))
        status    = t.get("status", "unknown")

        pct  = round(completed / max(total, 1) * 100, 1) if total else 0.0
        name = _torrent_display_name(t, state_map)

        if status == "paused":
            icon = "⏸️"
            bar_line = f"{_BAR_EMPTY * _BAR_LEN} {pct:.1f}%"
            speed_line = "  ⏸️ Paused"
        elif status == "waiting":
            icon = "🕐"
            bar_line = f"{_BAR_EMPTY * _BAR_LEN} {pct:.1f}%"
            speed_line = "  🕐 Queued"
        else:
            icon = "⬇️"
            bar_line = _progress_bar(pct)
            speed_line = (
                f"  ⚡ {_fmt_speed(speed)}"
                + (f"  •  ⏱️ ETA {_fmt_eta(eta_raw)}" if speed > 0 else "")
                + (f"  •  💾 {_fmt_size(completed)}/{_fmt_size(total)}" if total else "")
            )

        lines.append(
            f"{icon} <b>{name}</b>\n"
            f"  <code>{bar_line}</code>\n"
            f"{speed_line}"
        )

    lines.append(f"\n<i>Updates every 1s — auto-stops after 60s</i>")
    return "\n\n".join(lines)


# ── Bot class ─────────────────────────────────────────────────────────────────

class TelegramCommandBot:
    def __init__(self, config: dict, aria2: Aria2Client, state: StateManager):
        self.config   = config
        self.aria2    = aria2
        self.state    = state
        self.tg_cfg   = config.get("telegram", {})
        self.bot_token = self.tg_cfg.get("bot_token", "")
        self.chat_id   = str(self.tg_cfg.get("chat_id", ""))
        self.base_url  = f"https://api.telegram.org/bot{self.bot_token}"
        self.offset    = 0
        self.running   = False
        self.check_now_flag = Path("data/.check_now")

    # ── Telegram API helpers ──────────────────────────────────────────────────

    def _send_message(self, text: str) -> int | None:
        """Send a message; return the message_id on success."""
        if not self.bot_token or not self.chat_id:
            return None
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
        return None

    def _edit_message(self, message_id: int, text: str) -> bool:
        """Edit an existing message in-place."""
        if not self.bot_token or not self.chat_id:
            return False
        try:
            resp = requests.post(
                f"{self.base_url}/editMessageText",
                json={
                    "chat_id":    self.chat_id,
                    "message_id": message_id,
                    "text":       text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            return resp.json().get("ok", False)
        except Exception as e:
            logger.error(f"Telegram edit failed: {e}")
            return False

    # ── /status live updater ──────────────────────────────────────────────────

    def _live_status(self):
        """
        Send an initial status message, then edit it every 3 seconds
        for up to 60 seconds showing animated progress bars.
        Runs in its own daemon thread so it never blocks the poll loop.
        """
        try:
            active   = self.aria2.get_all_active()
            waiting  = self.aria2.get_all_waiting()
            torrents = active + waiting

            state_map = self.state.get_all()
            text      = _build_status_text(torrents, state_map, tick=0)
            msg_id    = self._send_message(text)

            if not msg_id or not torrents:
                return  # nothing to animate

            tick     = 1
            deadline = time.time() + 60      # animate for max 60 s
            interval = 1                     # refresh every 1 s

            while time.time() < deadline:
                time.sleep(interval)
                try:
                    active   = self.aria2.get_all_active()
                    waiting  = self.aria2.get_all_waiting()
                    torrents = active + waiting
                    state_map = self.state.get_all()
                    new_text  = _build_status_text(torrents, state_map, tick)
                    self._edit_message(msg_id, new_text)
                    tick += 1

                    # Stop animating when everything is done
                    if not torrents:
                        final = "✅ <b>All downloads complete!</b>"
                        self._edit_message(msg_id, final)
                        break
                except Exception as e:
                    logger.warning(f"Status update error: {e}")
                    break

            # Final static update — drop the spinner + "updates every 3s" footer
            try:
                active   = self.aria2.get_all_active()
                waiting  = self.aria2.get_all_waiting()
                torrents = active + waiting
                state_map = self.state.get_all()
                if torrents:
                    # Still running — mark as stopped updating
                    final_lines = _build_status_text(torrents, state_map, tick).rsplit("\n", 1)[0]
                    self._edit_message(msg_id, final_lines + "\n\n<i>Snapshot — use /status to refresh</i>")
            except Exception:
                pass

        except Exception as e:
            self._send_message(f"❌ Error getting status: {e}")

    # ── Command dispatcher ────────────────────────────────────────────────────

    def _handle_command(self, text: str):
        cmd = text.split()[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]

        if cmd == "/start":
            self._send_message(
                "🤖 <b>Akari Controller</b>\n\n"
                "Available commands:\n"
                "• /status — Live download progress\n"
                "• /check  — Force a check for new episodes\n"
                "• /list   — View tracked anime list"
            )

        elif cmd == "/status":
            # Spin up in a background thread so polling isn't blocked
            threading.Thread(target=self._live_status, daemon=True).start()

        elif cmd == "/check":
            self.check_now_flag.touch()
            self._send_message("⚡ Manual check triggered! The bot is scanning Nyaa.si now.")

        elif cmd == "/list":
            self.config = load_config()
            anime_list  = self.config.get("anime", [])
            if not anime_list:
                self._send_message("No anime currently tracked.")
                return
            all_states = self.state.get_all()
            lines = ["📺 <b>Tracked Anime</b>\n"]
            for a in anime_list:
                name = a["name"]
                s    = all_states.get(name, {})
                ep   = s.get("last_episode", "—")
                st   = s.get("status", "idle")
                icon = {"downloading": "⬇️", "complete": "✅", "error": "❌", "seeding": "🌱"}.get(st, "💤")
                lines.append(f"{icon} <b>{name}</b>  (EP{ep})")
            self._send_message("\n".join(lines))

        else:
            self._send_message("❓ Unknown command. Type /start for help.")

    # ── Long-poll loop ────────────────────────────────────────────────────────

    def _poll(self):
        logger.info("Telegram command listener started.")
        while self.running:
            try:
                self.config = load_config()
                new_token = self.config.get("telegram", {}).get("bot_token", "")
                new_chat  = str(self.config.get("telegram", {}).get("chat_id", ""))

                if not new_token or not new_chat:
                    time.sleep(10)
                    continue

                if new_token != self.bot_token:
                    self.bot_token = new_token
                    self.base_url  = f"https://api.telegram.org/bot{self.bot_token}"
                self.chat_id = new_chat

                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self.offset, "timeout": 30},
                    timeout=40,
                )
                data = resp.json()

                if data.get("ok"):
                    for update in data.get("result", []):
                        self.offset = update["update_id"] + 1
                        msg = update.get("message")
                        if not msg:
                            continue
                        if str(msg.get("chat", {}).get("id")) != self.chat_id:
                            continue
                        text = msg.get("text", "")
                        if text.startswith("/"):
                            self._handle_command(text)

            except requests.exceptions.Timeout:
                pass  # Long-polling timeout is normal
            except Exception as e:
                logger.error(f"Telegram polling error: {e}")
                time.sleep(5)

    def start(self):
        if not self.running:
            self.running = True
            threading.Thread(target=self._poll, daemon=True).start()

    def stop(self):
        self.running = False

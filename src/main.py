#!/usr/bin/env python3
"""
main.py — Autonomous Anime Downloader Bot
=========================================
Polls Nyaa.si RSS on a configurable schedule, detects new episodes,
downloads them via aria2c, deletes old episodes, and sends
Telegram notifications.
"""
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from aria2c_client import Aria2Client, Aria2Error
from episode_cleaner import delete_previous_episode
from release_picker import group_releases_by_episode, pick_best_release
from rss_poller import fetch_releases
from state_manager import StateManager
from telegram_notifier import TelegramNotifier

CONFIG_PATH    = os.environ.get("CONFIG_PATH", "config.yaml")
CHECK_NOW_FLAG = Path("data/.check_now")


# ── Folder Helpers ──────────────────────────────────────────────────────────

def sanitize_folder_name(name: str) -> str:
    """
    Strip characters that are illegal in folder names on Linux/Windows.
    Preserves unicode letters (Japanese, etc.) and common punctuation.
    """
    # Remove control characters and filesystem-unsafe chars
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Strip leading/trailing dots and spaces
    safe = safe.strip('. ')
    return safe or "Unknown"


def anime_save_path(base_path: str, anime_name: str) -> str:
    """
    Return (and pre-create) a per-anime subfolder inside base_path.
    e.g. /downloads/Futsutsuka na Akujo dewa Gozaimasu ga/
    """
    folder = sanitize_folder_name(anime_name)
    path   = os.path.join(base_path, folder)
    os.makedirs(path, exist_ok=True)
    return path


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging():
    os.makedirs("data/logs", exist_ok=True)
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/logs/bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


logger = logging.getLogger("akari")


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.critical(f"config.yaml not found at {CONFIG_PATH}")
        sys.exit(1)


def build_aria2_client(config: dict) -> Aria2Client:
    a2 = config.get("aria2", {})
    return Aria2Client(
        host=a2.get("host", "http://aria2"),
        port=int(a2.get("port", 6800)),
        secret=a2.get("secret", ""),
    )


def wait_for_aria2(config_loader) -> Aria2Client:
    """
    Retry aria2c connection indefinitely, re-reading config each attempt.
    Gives the user time to set credentials via the dashboard if needed.
    """
    attempt = 0
    delay   = 5

    while True:
        attempt += 1
        cfg    = config_loader()
        client = build_aria2_client(cfg)

        logger.info(f"[Attempt {attempt}] Connecting to aria2c at {client.rpc_url}…")
        if client.ping():
            logger.info("✅ Connected to aria2c!")
            return client

        logger.warning(
            f"[Attempt {attempt}] Could not reach aria2c. "
            f"Retrying in {delay}s… "
            "(Is the aria2 container running? Check docker-compose logs)"
        )
        time.sleep(delay)
        delay = min(delay + 5, 30)


# ── Download Monitoring ───────────────────────────────────────────────────────

def _get_real_file_path(dl_status: dict) -> str:
    """
    Extract the real download file path from an aria2 status dict.
    Filters out [METADATA] placeholder entries aria2 creates for magnet links.
    """
    files = dl_status.get("files", [])
    real_files = [
        f for f in files
        if f.get("path") and
           not f["path"].endswith(".aria2") and
           "[METADATA]" not in f["path"]
    ]
    if not real_files:
        return ""
    main = max(real_files, key=lambda f: int(f.get("length", 0)))
    return main.get("path", "")


def check_in_progress_downloads(
    aria2: Aria2Client,
    state: StateManager,
    notifier: TelegramNotifier,
    config: dict,
):
    """Check if any in-progress downloads have completed."""
    tg_cfg     = config.get("telegram", {})
    all_states = state.get_all()

    for anime_name, anime_state in all_states.items():
        if anime_state.get("status") != "downloading":
            continue

        gid = anime_state.get("current_gid", "")
        if not gid:
            continue

        try:
            dl_status  = aria2.get_status(gid)
            status_str = dl_status.get("status", "unknown") if dl_status else "unknown"
            episode    = anime_state.get("last_episode", "?")

            if status_str == "active":
                total     = int(dl_status.get("totalLength", 0))
                completed = int(dl_status.get("completedLength", 0))
                speed     = int(dl_status.get("downloadSpeed", 0))
                pct       = round(completed / total * 100, 1) if total else 0
                speed_mb  = speed / 1_048_576
                logger.info(f"⏳ {anime_name} EP{episode}: {pct}% @ {speed_mb:.1f} MB/s")

                # ── Opportunistically save real file path while aria2 still
                # has the record — so we have it even after it purges the GID.
                real_path = _get_real_file_path(dl_status)
                if real_path and real_path != anime_state.get("current_file_path"):
                    state.update_status(anime_name, "downloading", real_path)
                    logger.debug(f"  Saved file path: {real_path}")

            elif status_str == "error":
                err = dl_status.get("errorMessage", "unknown error")
                logger.error(f"❌ {anime_name} EP{episode} download error: {err}")
                state.update_status(anime_name, "error")

            else:
                # status is "complete", "removed", "unknown", or aria2 returned empty dict.
                
                # If this was a magnet metadata download, aria2 spawns a new download
                # for the actual files. It provides the new GID in 'followedBy'.
                if dl_status and dl_status.get("followedBy"):
                    new_gid = dl_status["followedBy"][0]
                    logger.info(f"🔄 Magnet resolved for {anime_name} EP{episode}, following new GID {new_gid}...")
                    # Update state with the new GID but keep status as downloading
                    state.set_state(anime_name, {
                        **anime_state,
                        "current_gid": new_gid,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    })
                    continue

                # Use saved path from state as fallback.
                file_path = _get_real_file_path(dl_status) if dl_status else ""
                if not file_path:
                    file_path = anime_state.get("current_file_path", "")

                logger.info(f"✅ {anime_name} EP{episode} complete! → {file_path or '(path unknown)'}")
                state.update_status(anime_name, "complete", file_path)

                # ── Send Telegram notification ──────────────────────────────
                if tg_cfg.get("send_on_new_episode", True):
                    notifier.notify_complete(
                        anime_name=anime_name,
                        episode=episode,
                        release_title=anime_state.get("release_title", ""),
                        size=anime_state.get("size", "?"),
                    )

        except Exception as e:
            logger.error(f"Error monitoring {anime_name}: {e}", exc_info=True)



# ── New Episode Detection ─────────────────────────────────────────────────────

def check_anime(
    anime_cfg: dict,
    aria2: Aria2Client,
    state: StateManager,
    notifier: TelegramNotifier,
    config: dict,
) -> bool:
    """Check Nyaa RSS for a new episode and start downloading if found."""
    name         = anime_cfg["name"]
    query        = anime_cfg.get("nyaa_query", name)
    season       = anime_cfg.get("season", "").strip()
    if season:
        if season.isdigit():
            season = f"S{int(season):02d}"
        query = f"{query} {season}"
    category     = anime_cfg.get("category", "1_2")
    preferred_res= anime_cfg.get("preferred_resolution", "1080p")
    trusted_only = config.get("trusted_only", True)
    base_path    = config.get("downloads", {}).get("save_path", "/downloads")
    save_path    = anime_save_path(base_path, name)   # ← per-anime subfolder
    tg_cfg       = config.get("telegram", {})

    logger.info(f"Checking '{name}'…")
    releases = fetch_releases(query, category)

    if not releases:
        return False

    episodes = group_releases_by_episode(releases)
    if not episodes:
        logger.info(f"  → Could not parse episode numbers for '{name}'")
        return False

    latest_ep  = max(episodes.keys())
    current_ep = state.get_episode(name)

    logger.info(f"  → latest={latest_ep}, current={current_ep or 'none'}")

    current_state = state.get_state(name)
    current_status = current_state.get("status") if current_state else None
    
    # If the latest episode is less than or equal to what we have tracked,
    # we usually skip. But if it errored, we should retry!
    if current_ep is not None and latest_ep <= current_ep:
        if current_status != "error":
            return False

    # Skip if already downloading this episode
    current_state = state.get_state(name)
    if (
        current_state
        and current_state.get("last_episode") == latest_ep
        and current_state.get("status") == "downloading"
    ):
        logger.info(f"  → EP{latest_ep} already downloading, skipping")
        return False

    # Pick best release
    best = pick_best_release(episodes[latest_ep], preferred_res, trusted_only)
    if not best:
        logger.warning(f"  → No suitable release for '{name}' EP{latest_ep}")
        return False

    logger.info(
        f"  → New: EP{latest_ep} | {best.title} | {best.seeders} seeders | {best.size}"
    )

    # Delete previous episode
    prev_state = state.get_state(name)
    if prev_state and prev_state.get("current_gid"):
        delete_previous_episode(aria2, prev_state)

    # Send to aria2c — prefer magnet, fall back to .torrent URL
    try:
        if best.magnet:
            gid = aria2.add_magnet(best.magnet, save_path)
        else:
            gid = aria2.add_torrent_url(best.torrent_url, save_path)
    except Aria2Error as e:
        logger.error(f"  → aria2c error adding download: {e}")
        return False

    # Save state
    state.update(
        anime_name=name,
        episode=latest_ep,
        gid=gid,
        status="downloading",
        release_title=best.title,
        size=best.size,
    )

    # Telegram: notify download started
    if tg_cfg.get("send_on_new_episode", True):
        notifier.notify_downloading(name, latest_ep, best.title)

    return True


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run_cycle(aria2: Aria2Client, state: StateManager, config: dict):
    tg_cfg   = config.get("telegram", {})
    notifier = TelegramNotifier(
        bot_token=tg_cfg.get("bot_token", ""),
        chat_id=tg_cfg.get("chat_id", ""),
    )

    check_in_progress_downloads(aria2, state, notifier, config)

    for anime_cfg in config.get("anime", []):
        try:
            check_anime(anime_cfg, aria2, state, notifier, config)
        except Exception as e:
            logger.error(
                f"Unhandled error for '{anime_cfg.get('name', '?')}': {e}",
                exc_info=True,
            )


def main():
    setup_logging()

    logger.info("=" * 60)
    logger.info("  🤖  Akari — Starting Up  (aria2c edition)")
    logger.info("=" * 60)

    # Connect to aria2c (retries forever, re-reads config each attempt)
    aria2  = wait_for_aria2(load_config)
    config = load_config()
    state  = StateManager("data/state.json")

    anime_count = len(config.get("anime", []))
    poll_mins   = config.get("poll_interval_minutes", 15)
    logger.info(f"Tracking {anime_count} anime | Poll every {poll_mins} min")

    # Telegram startup notification
    tg_cfg = config.get("telegram", {})
    notifier = TelegramNotifier(
        bot_token=tg_cfg.get("bot_token", ""),
        chat_id=tg_cfg.get("chat_id", ""),
    )
    if tg_cfg.get("send_on_start", True):
        notifier.notify_started(anime_count)
        
    from telegram_bot import TelegramCommandBot
    tg_bot = TelegramCommandBot(config, aria2, state)
    tg_bot.start()

    while True:
        try:
            config    = load_config()
            poll_mins = config.get("poll_interval_minutes", 15)

            # Refresh aria2c client from latest config
            a2_cfg        = config.get("aria2", {})
            aria2.rpc_url = f"{a2_cfg.get('host', 'http://aria2')}:{a2_cfg.get('port', 6800)}/jsonrpc"
            aria2.secret  = a2_cfg.get("secret", "")

            run_cycle(aria2, state, config)

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

        CHECK_NOW_FLAG.unlink(missing_ok=True)
        logger.info(f"💤  Sleeping {poll_mins} min…")

        for _ in range(poll_mins * 12):   # check flag every 5 seconds
            time.sleep(5)
            
            # Check download progress frequently so UI and Telegram update quickly
            try:
                check_in_progress_downloads(aria2, state, notifier, config)
            except Exception as e:
                logger.error(f"Error checking downloads: {e}")

            if CHECK_NOW_FLAG.exists():
                logger.info("⚡ Manual check triggered from dashboard!")
                break


if __name__ == "__main__":
    main()

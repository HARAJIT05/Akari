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


import urllib.request
import json

def ensure_poster_exists(anime_name: str, folder_path: str):
    """
    Fetch the anime cover from AniList and save it as 'poster.jpg' for Jellyfin/Plex.
    """
    poster_path = os.path.join(folder_path, "poster.jpg")
    if os.path.exists(poster_path):
        return

    query = f'''query{{Media(search:"{anime_name}",type:ANIME){{coverImage{{large}}}}}}'''
    try:
        req = urllib.request.Request(
            'https://graphql.anilist.co',
            data=json.dumps({"query": query}).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (compatible; Akari/1.0)',
                'Accept': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read())
            
        media = data.get('data', {}).get('Media')
        if media:
            image_url = media.get('coverImage', {}).get('large')
            if image_url:
                req_img = urllib.request.Request(image_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req_img, timeout=10) as response, open(poster_path, 'wb') as out_file:
                    out_file.write(response.read())
                os.chmod(poster_path, 0o666)  # ensure read/write permissions
                logging.getLogger("akari").info(f"🖼️ Downloaded poster for '{anime_name}'")
    except Exception as e:
        logging.getLogger("akari").debug(f"Failed to fetch poster for '{anime_name}': {e}")


def anime_save_path(base_path: str, anime_name: str) -> str:
    """
    Return (and pre-create) a per-anime subfolder inside base_path.
    e.g. /downloads/Futsutsuka na Akujo dewa Gozaimasu ga/
    """
    folder = sanitize_folder_name(anime_name)
    path = os.path.join(base_path, folder)
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o777)
    
    ensure_poster_exists(anime_name, path)
    
    return path


# ── Batch Poster Scanner ────────────────────────────────────────────────────

def scan_and_download_posters(base_path: str):
    """
    Walk every subfolder in base_path.
    For each folder missing a poster.jpg, download the cover art from AniList
    using the folder name as the anime title.
    Already-existing posters are skipped silently.
    """
    log = logging.getLogger("akari")
    if not os.path.isdir(base_path):
        log.warning(f"Poster scan: downloads folder not found: {base_path}")
        return

    folders = [
        f for f in os.listdir(base_path)
        if os.path.isdir(os.path.join(base_path, f))
    ]

    if not folders:
        return

    log.info(f"🖼️  Poster scan: checking {len(folders)} folder(s) in {base_path}")
    downloaded, skipped = 0, 0

    for folder_name in sorted(folders):
        folder_path = os.path.join(base_path, folder_name)
        poster_path = os.path.join(folder_path, "poster.jpg")

        if os.path.exists(poster_path):
            skipped += 1
            log.debug(f"  ⏭️  {folder_name}: poster exists, skipping")
            continue

        log.info(f"  ⬇️  {folder_name}: downloading poster…")
        try:
            ensure_poster_exists(folder_name, folder_path)
            if os.path.exists(poster_path):
                downloaded += 1
                log.info(f"  ✅  {folder_name}: poster saved")
            else:
                log.warning(f"  ⚠️  {folder_name}: not found on AniList")
        except Exception as e:
            log.warning(f"  ❌  {folder_name}: failed — {e}")

    log.info(f"🖼️  Poster scan complete: {downloaded} downloaded, {skipped} skipped")


# ── Orphaned Episode Cleanup ────────────────────────────────────────────────

# Video file extensions to consider as episode files
_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}


def cleanup_orphaned_episodes(state: StateManager, config: dict):
    """
    Scan each anime's download folder and delete any video files that are NOT
    the currently tracked episode.  Runs at the start of every poll cycle so
    orphaned files from a failed previous cleanup are always caught — even
    across container restarts (when aria2 has forgotten the old GID).

    Skips anime that are currently downloading to avoid touching in-progress files.
    """
    base_path  = config.get("downloads", {}).get("save_path", "/downloads")
    anime_list = config.get("anime", [])
    all_states = state.get_all()

    for anime_cfg in anime_list:
        name         = anime_cfg["name"]
        anime_state  = all_states.get(name, {})
        current_file = anime_state.get("current_file_path", "")
        status       = anime_state.get("status", "")

        # Don't touch the folder while a download is in progress
        if status == "downloading":
            continue

        folder = os.path.join(base_path, sanitize_folder_name(name))
        if not os.path.isdir(folder):
            continue

        for fname in os.listdir(folder):
            fpath = os.path.join(folder, fname)
            if not os.path.isfile(fpath):
                continue
            if os.path.splitext(fname)[1].lower() not in _VIDEO_EXTS:
                continue
            if fpath == current_file:
                continue  # this is the episode we want to keep

            try:
                os.remove(fpath)
                logger.info(f"🗑️  [cleanup] Removed orphaned episode: {fname}")
            except OSError as e:
                logger.warning(f"[cleanup] Could not delete {fpath}: {e}")


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
    prefer_uncen = anime_cfg.get("prefer_uncensored", False)
    trusted_only = config.get("trusted_only", True)
    base_path    = config.get("downloads", {}).get("save_path", "/downloads")
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

    current_state  = state.get_state(name)
    current_status = current_state.get("status") if current_state else None

    # ── File existence check ──────────────────────────────────────────────────
    # If the episode file we saved in state no longer exists on disk (e.g. it
    # was manually deleted, or the disk was wiped), re-download it.
    # We skip this check while a download is in progress because:
    #   • "downloading": the file is partial / not yet on disk
    #   • "seeding":     the file exists but aria2 still holds it open
    # We also skip it when the saved path is an aria2 [METADATA] placeholder —
    # that only appears briefly while a magnet link is resolving; the real
    # file path isn't known yet so we can't verify it.
    _saved_path   = (current_state or {}).get("current_file_path", "")
    _file_missing = (
        bool(_saved_path)                                    # we have a saved path
        and "[METADATA]" not in _saved_path                 # it's a real path, not a placeholder
        and current_status not in ("downloading", "seeding", None)  # not in-progress
        and not os.path.isfile(_saved_path)                 # and the file is actually gone
    )

    if _file_missing:
        logger.warning(
            f"  → EP{current_ep} file missing from disk: {_saved_path!r}\n"
            f"     Scheduling re-download…"
        )

    # If the latest episode is less than or equal to what we have tracked,
    # skip — unless it errored or the file has gone missing from disk.
    if current_ep is not None and latest_ep <= current_ep:
        if current_status != "error" and not _file_missing:
            return False

    # Skip if already downloading this episode
    if (
        current_state
        and current_state.get("last_episode") == latest_ep
        and current_state.get("status") == "downloading"
    ):
        logger.info(f"  → EP{latest_ep} already downloading, skipping")
        return False

    # Pick best release
    best = pick_best_release(episodes[latest_ep], preferred_res, trusted_only, prefer_uncen)
    if not best:
        logger.warning(f"  → No suitable release for '{name}' EP{latest_ep}")
        return False

    logger.info(
        f"  → New: EP{latest_ep} | {best.title} | {best.seeders} seeders | {best.size}"
    )

    # Delete previous episode (skip if the file was already missing — nothing to remove)
    prev_state = state.get_state(name)
    if prev_state and prev_state.get("current_gid") and not _file_missing:
        delete_previous_episode(aria2, prev_state)

    # Send to aria2c — prefer magnet, fall back to .torrent URL
    save_path = anime_save_path(base_path, name)   # ← per-anime subfolder
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

    # Clean up any orphaned episode files from previous failed deletions
    cleanup_orphaned_episodes(state, config)

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

    # Scan all download folders and fetch missing posters at startup
    # Prefer DOWNLOAD_DIR env var (set from .env via docker-compose) over config.yaml
    base_path = os.environ.get("DOWNLOAD_DIR") or config.get("downloads", {}).get("save_path", "/downloads")
    logger.info(f"📂 Download path: {base_path}")
    scan_and_download_posters(base_path)

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

"""
dashboard/app.py — FastAPI backend for the Akari Web Dashboard.
Serves the SPA and provides a REST API for config/state management.
"""
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, List

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Ensure bot src modules are importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from state_manager import StateManager
from telegram_notifier import TelegramNotifier
from aria2c_client import Aria2Client, Aria2Error
from rss_poller import fetch_releases
from release_picker import extract_episode_number, pick_best_release, group_releases_by_episode

logger = logging.getLogger(__name__)


# ── Folder Helpers ─────────────────────────────────────────────────────

def sanitize_folder_name(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    return safe.strip('. ') or "Unknown"


from main import ensure_poster_exists

def anime_save_path(base_path: str, anime_name: str) -> str:
    """Return (and pre-create) a per-anime subfolder inside base_path."""
    folder = sanitize_folder_name(anime_name)
    path   = os.path.join(base_path, folder)
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o777)
    
    ensure_poster_exists(anime_name, path)
    
    return path


# ── App Setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Akari Dashboard", docs_url=None, redoc_url=None)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
STATE_PATH = Path("data/state.json")
LOG_PATH = Path("data/logs/bot.log")
CHECK_NOW_FLAG = Path("data/.check_now")
BUILD_TIME = str(int(os.path.getmtime(__file__)))  # changes on every rebuild

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
state_manager = StateManager(str(STATE_PATH))


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_download_path() -> str:
    """
    Resolve the download base path.
    Prefers DOWNLOAD_DIR env var (set from .env via docker-compose),
    falls back to config.yaml downloads.save_path, then /downloads.
    """
    return (
        os.environ.get("DOWNLOAD_DIR")
        or load_config().get("downloads", {}).get("save_path", "/downloads")
    )


def save_config(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def get_aria2() -> Aria2Client | None:
    config = load_config()
    a2_cfg = config.get("aria2", {})
    if not a2_cfg.get("secret"):
        return None
    client = Aria2Client(
        host=a2_cfg.get("host", "http://aria2"),
        port=int(a2_cfg.get("port", 6800)),
        secret=a2_cfg.get("secret", ""),
    )
    return client if client.ping() else None


# ── Page ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"build_time": BUILD_TIME}
    )


# ── Config API ────────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_get_config():
    return load_config()


@app.post("/api/config")
async def api_save_config(request: Request):
    body = await request.json()
    save_config(body)
    return {"ok": True, "message": "Configuration saved successfully"}


# ── State API ─────────────────────────────────────────────────────────────────

@app.get("/api/state")
def api_get_state():
    return state_manager.get_all()


# ── Status API ────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    config = load_config()
    state = state_manager.get_all()
    return {
        "anime_count": len(config.get("anime", [])),
        "downloading": sum(1 for s in state.values() if s.get("status") == "downloading"),
        "seeding": sum(1 for s in state.values() if s.get("status") == "seeding"),
        "poll_interval": config.get("poll_interval_minutes", 15),
        "trusted_only": config.get("trusted_only", True),
    }


# ── Anime CRUD ────────────────────────────────────────────────────────────────

class AnimeEntry(BaseModel):
    name: str
    nyaa_query: str
    season: Optional[str] = ""
    preferred_resolution: str = "1080p"
    category: str = "1_2"


@app.post("/api/anime")
def api_add_anime(entry: AnimeEntry):
    config = load_config()
    anime_list = config.get("anime", [])
    if any(a["name"].lower() == entry.name.lower() for a in anime_list):
        raise HTTPException(400, f"'{entry.name}' is already being tracked")
    anime_list.append(entry.dict())
    config["anime"] = anime_list
    save_config(config)
    return {"ok": True, "message": f"'{entry.name}' added to watchlist"}


@app.put("/api/anime/{name}")
def api_update_anime(name: str, entry: AnimeEntry):
    config = load_config()
    anime_list = config.get("anime", [])
    for i, a in enumerate(anime_list):
        if a["name"] == name:
            anime_list[i] = entry.dict()
            config["anime"] = anime_list
            save_config(config)
            return {"ok": True, "message": f"'{name}' updated"}
    raise HTTPException(404, f"'{name}' not found in watchlist")


@app.delete("/api/anime/{name}")
def api_delete_anime(name: str):
    config = load_config()
    anime_list = config.get("anime", [])
    new_list = [a for a in anime_list if a["name"] != name]
    if len(new_list) == len(anime_list):
        raise HTTPException(404, f"'{name}' not found")
    config["anime"] = new_list
    save_config(config)

    # Cancel any active aria2 download for this anime (without deleting files)
    anime_state = state_manager.get_state(name)
    if anime_state:
        gid = anime_state.get("current_gid")
        if gid and anime_state.get("status") in ("downloading", "seeding", "paused"):
            aria2 = get_aria2()
            if aria2:
                try:
                    aria2.remove(gid, delete_files=False)
                    logger.info(f"Cancelled aria2 download for '{name}' (gid={gid})")
                except Exception as e:
                    logger.warning(f"Could not cancel download for '{name}': {e}")

    state_manager.remove(name)
    return {"ok": True, "message": f"'{name}' removed from watchlist"}


# ── Poster API ───────────────────────────────────────────────────────────────

@app.get("/api/anime/{name}/poster")
def api_get_anime_poster(name: str):
    """
    Serve the anime poster image.
    Checks the local download folder for poster.jpg first;
    if missing, fetches it from AniList on-demand.
    """
    config = load_config()
    base_path = get_download_path()
    folder = sanitize_folder_name(name)
    folder_path = os.path.join(base_path, folder)
    poster_path = os.path.join(folder_path, "poster.jpg")

    # Try to fetch if not present
    if not os.path.exists(poster_path):
        os.makedirs(folder_path, exist_ok=True)
        try:
            ensure_poster_exists(name, folder_path)
        except Exception:
            pass

    if os.path.exists(poster_path):
        return FileResponse(poster_path, media_type="image/jpeg")

    # Fallback: proxy AniList image directly
    import urllib.request as _ur
    query = f'query{{Media(search:"{name}",type:ANIME){{coverImage{{large}}}}}}'
    try:
        req = _ur.Request(
            'https://graphql.anilist.co',
            data=json.dumps({"query": query}).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 (compatible; Akari/1.0)',
                'Accept': 'application/json',
            },
        )
        with _ur.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        media = data.get('data', {}).get('Media')
        if media:
            image_url = media.get('coverImage', {}).get('large')
            if image_url:
                img_req = _ur.Request(image_url, headers={'User-Agent': 'Mozilla/5.0'})
                with _ur.urlopen(img_req, timeout=10) as img_resp:
                    img_data = img_resp.read()
                # Save to disk for future requests
                try:
                    os.makedirs(folder_path, exist_ok=True)
                    with open(poster_path, 'wb') as pf:
                        pf.write(img_data)
                    os.chmod(poster_path, 0o666)
                    logger.info(f"\U0001f5bc\ufe0f Saved poster for '{name}' via dashboard proxy")
                    return FileResponse(poster_path, media_type="image/jpeg")
                except Exception:
                    pass
                return StreamingResponse(
                    iter([img_data]),
                    media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
    except Exception as e:
        logger.warning(f"Could not fetch poster for '{name}': {e}")

    raise HTTPException(404, "Poster not available")


@app.get("/api/anime/{name}/files")
def api_get_anime_files(name: str):
    config = load_config()
    base_path = get_download_path()
    folder = sanitize_folder_name(name)
    path = os.path.join(base_path, folder)
    if not os.path.exists(path):
        return {"files": []}
    
    files = []
    for f in os.listdir(path):
        fpath = os.path.join(path, f)
        if os.path.isfile(fpath):
            files.append({
                "name": f,
                "size": os.path.getsize(fpath)
            })
    # Sort files naturally (so EP2 comes before EP10)
    files.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x["name"])])
    return {"files": files}


class DeleteFileBody(BaseModel):
    filename: str

@app.post("/api/anime/{name}/files/delete")
def api_delete_anime_file(name: str, body: DeleteFileBody):
    config = load_config()
    base_path = get_download_path()
    folder = sanitize_folder_name(name)
    
    if ".." in body.filename or "/" in body.filename or "\\" in body.filename:
        raise HTTPException(400, "Invalid filename")
        
    path = os.path.join(base_path, folder, body.filename)
    if os.path.exists(path) and os.path.isfile(path):
        try:
            os.remove(path)
            
            # Also clear the state if the deleted file was the current tracked file
            state = state_manager.get_state(name)
            if state and state.get("current_file_path") == path:
                state["current_file_path"] = ""
                state["status"] = "deleted"
                state_manager.update_state(name, state)
                
            return {"ok": True, "message": f"Deleted {body.filename}"}
        except Exception as e:
            raise HTTPException(500, f"Could not delete file: {e}")
    raise HTTPException(404, "File not found")


# ── Manual Episode Download ───────────────────────────────────────────────────

class EpisodeDownloadBody(BaseModel):
    episode: int
    trusted_only: Optional[bool] = None   # overrides config if provided


@app.get("/api/anime/{name}/search-episode")
def api_search_episode(name: str, episode: int):
    """
    Preview releases available for a specific episode number.
    Returns up to 10 candidates so the user can confirm before downloading.
    """
    config = load_config()
    anime_list = config.get("anime", [])
    entry = next((a for a in anime_list if a["name"] == name), None)
    if not entry:
        raise HTTPException(404, f"'{name}' not found in watchlist")
    # Append season if it exists
    base_query = entry["nyaa_query"]
    season = entry.get("season", "").strip()
    if season:
        if season.isdigit():
            season = f"S{int(season):02d}"
        base_query = f"{base_query} {season}"

    # Append episode number to query to search deep for older episodes
    query = f'{base_query} {episode:02d}' if isinstance(episode, int) else f'{base_query} {episode}'
    releases = fetch_releases(query, entry.get("category", "1_2"))
    
    # Also fetch without the episode number just in case the group doesn't use standard formatting
    # or if we want to catch batches (which might not have the exact episode number in the title)
    # But usually for manual specific-episode search, the first query is better.
    by_ep = group_releases_by_episode(releases)
    candidates = by_ep.get(episode, [])

    if not candidates:
        # Fallback to general query
        releases = fetch_releases(base_query, entry.get("category", "1_2"))
        by_ep = group_releases_by_episode(releases)
        candidates = by_ep.get(episode, [])

    if not candidates:
        return {"found": False, "episode": episode, "results": []}

    results = [
        {
            "title":       r.title,
            "seeders":     r.seeders,
            "leechers":    r.leechers,
            "size":        r.size,
            "trusted":     r.trusted,
            "torrent_url": r.torrent_url,
            "magnet":      r.magnet,
        }
        for r in sorted(candidates, key=lambda r: r.seeders, reverse=True)[:10]
    ]
    return {"found": True, "episode": episode, "results": results}


@app.post("/api/anime/{name}/download-episode")
def api_download_episode(name: str, body: EpisodeDownloadBody):
    """
    Search Nyaa for a specific episode of an anime, pick the best release,
    and queue it in aria2c immediately.
    """
    config = load_config()
    anime_list = config.get("anime", [])
    entry = next((a for a in anime_list if a["name"] == name), None)
    if not entry:
        raise HTTPException(404, f"'{name}' not found in watchlist")

    aria2 = get_aria2()
    if not aria2:
        raise HTTPException(503, "aria2c is not reachable — check your connection settings")

    # Append season if it exists
    base_query = entry["nyaa_query"]
    season = entry.get("season", "").strip()
    if season:
        base_query = f"{base_query} {season}"

    # Fetch and filter releases with episode number for deep search
    query = f'{base_query} {body.episode:02d}' if isinstance(body.episode, int) else f'{base_query} {body.episode}'
    releases = fetch_releases(query, entry.get("category", "1_2"))
    by_ep = group_releases_by_episode(releases)
    candidates = by_ep.get(body.episode, [])

    if not candidates:
        # Fallback to general query
        releases = fetch_releases(base_query, entry.get("category", "1_2"))
        by_ep = group_releases_by_episode(releases)
        candidates = by_ep.get(body.episode, [])

    if not candidates:
        raise HTTPException(
            404,
            f"No releases found for '{name}' episode {body.episode} on Nyaa.si"
        )

    trusted_only = body.trusted_only if body.trusted_only is not None else config.get("trusted_only", True)
    preferred_res = entry.get("preferred_resolution", "1080p")
    best = pick_best_release(candidates, preferred_res=preferred_res, trusted_only=trusted_only)

    if not best:
        raise HTTPException(404, f"Could not pick a suitable release for episode {body.episode}")

    # Queue in aria2c — put in per-anime subfolder
    base_path = get_download_path()
    save_path = anime_save_path(base_path, name)
    link = best.magnet or best.torrent_url
    try:
        gid = aria2.add_magnet(link, save_path) if best.magnet else aria2.add_torrent_url(link, save_path)
    except Aria2Error as e:
        raise HTTPException(500, f"Failed to queue download: {e}")

    logger.info(f"Manual download queued: '{name}' EP{body.episode} — {best.title} (GID {gid})")
    return {
        "ok":      True,
        "message": f"Episode {body.episode} queued!",
        "release": best.title,
        "gid":     gid,
        "seeders": best.seeders,
        "size":    best.size,
    }


# ── Telegram API ──────────────────────────────────────────────────────────────

class TelegramTestBody(BaseModel):
    bot_token: str
    chat_id: str


@app.post("/api/telegram/test")
def api_test_telegram(body: TelegramTestBody):
    notifier = TelegramNotifier(body.bot_token, body.chat_id)
    ok = notifier.send(
        "✅ <b>Akari Test!</b>\n\n"
        "Your Telegram notifications are working correctly! 🎌"
    )
    if ok:
        return {"ok": True, "message": "Test message sent successfully!"}
    raise HTTPException(400, "Failed to send test message. Check your bot token and chat ID.")


# ── qBittorrent Test API ──────────────────────────────────────────────────────

class Aria2TestBody(BaseModel):
    host: str
    port: int
    secret: str


@app.post("/api/aria2/test")
def api_test_aria2(body: Aria2TestBody):
    client = Aria2Client(body.host, body.port, body.secret)
    if client.ping():
        return {"ok": True, "message": "Connected to aria2c successfully!"}
    raise HTTPException(400, "Could not connect to aria2c. Check host, port, and secret token.")


# ── Downloads API (proxy to qBittorrent) ─────────────────────────────────────

@app.get("/api/downloads")
def api_get_downloads():
    aria2 = get_aria2()
    if not aria2:
        return {"torrents": [], "error": "aria2c not reachable (check config)"}
    try:
        active  = aria2.get_all_active()
        waiting = aria2.get_all_waiting()
        all_dl  = active + waiting
        simplified = [
            {
                "name":      (t.get("files", [{}])[0].get("path", "").split("/")[-1]
                              if t.get("files") else t.get("gid", "?")),
                "gid":       t.get("gid", ""),
                "status":    t.get("status", ""),
                "progress":  round(
                    int(t.get("completedLength", 0)) /
                    max(int(t.get("totalLength", 1)), 1) * 100, 1
                ),
                "dlspeed":   int(t.get("downloadSpeed", 0)),
                "upspeed":   int(t.get("uploadSpeed", 0)),
                "size":      int(t.get("totalLength", 0)),
                "eta":       (
                    int(
                        (int(t.get("totalLength", 0)) - int(t.get("completedLength", 0)))
                        / max(int(t.get("downloadSpeed", 1)), 1)
                    )
                    if int(t.get("downloadSpeed", 0)) > 0 else -1
                ),
                "num_seeds": int(t.get("numSeeders", 0)),
            }
            for t in all_dl
        ]
        return {"torrents": simplified}
    except Exception as e:
        return {"torrents": [], "error": str(e)}

@app.post("/api/downloads/{gid}/pause")
def api_pause_download(gid: str):
    aria2 = get_aria2()
    if not aria2:
        raise HTTPException(500, "aria2c not reachable")
    if aria2.pause(gid):
        return {"ok": True}
    raise HTTPException(500, "Failed to pause download")

@app.post("/api/downloads/{gid}/resume")
def api_resume_download(gid: str):
    aria2 = get_aria2()
    if not aria2:
        raise HTTPException(500, "aria2c not reachable")
    if aria2.unpause(gid):
        return {"ok": True}
    raise HTTPException(500, "Failed to resume download")

@app.post("/api/downloads/{gid}/cancel")
def api_cancel_download(gid: str):
    aria2 = get_aria2()
    if not aria2:
        raise HTTPException(500, "aria2c not reachable")
    if aria2.remove(gid, delete_files=True):
        return {"ok": True}
    raise HTTPException(500, "Failed to cancel download")


# ── Log API ───────────────────────────────────────────────────────────────────

@app.get("/api/logs")
def api_get_logs(lines: int = 300):
    if not LOG_PATH.exists():
        return {"logs": []}
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return {"logs": [l.rstrip() for l in all_lines[-lines:]]}


# ── Check-Now Trigger ─────────────────────────────────────────────────────────

@app.post("/api/check-now")
def api_check_now():
    """Signal the bot to run an immediate poll cycle."""
    CHECK_NOW_FLAG.touch()
    return {"ok": True, "message": "Immediate check triggered! Bot will poll shortly."}


# ── Poster Scan Trigger ────────────────────────────────────────────────────────

from main import scan_and_download_posters
import threading

@app.post("/api/scan-posters")
def api_scan_posters():
    """
    Trigger a background scan of all download folders.
    Downloads poster.jpg for any folder that is missing one.
    """
    config = load_config()
    base_path = get_download_path()

    def _run():
        try:
            scan_and_download_posters(base_path)
        except Exception as e:
            logger.error(f"Poster scan error: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"ok": True, "message": f"Poster scan started for '{base_path}'. Check logs for progress."}


# ── Download History ──────────────────────────────────────────────────────────

@app.get("/api/history")
def api_get_history():
    """Return all anime that have a recorded download state (any status)."""
    all_states = state_manager.get_all()
    config     = load_config()
    anime_map  = {a["name"]: a for a in config.get("anime", [])}

    history = []
    for name, s in all_states.items():
        # Convert container path → host path for display
        container_path = s.get("current_file_path", "")
        host_path      = _container_to_host_path(container_path)

        history.append({
            "name":         name,
            "episode":      s.get("last_episode"),
            "status":       s.get("status", "unknown"),
            "release_title":s.get("release_title", ""),
            "size":         s.get("size", ""),
            "updated_at":   s.get("updated_at", ""),
            "file_path":    container_path,
            "host_path":    host_path,
            "resolution":   anime_map.get(name, {}).get("preferred_resolution", ""),
        })

    history.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    return {"history": history}


@app.delete("/api/history/{name}")
def api_delete_history(name: str):
    """Delete a specific anime's history log from the state."""
    if state_manager.get_state(name):
        state_manager.remove(name)
        return {"ok": True, "message": f"History for '{name}' removed"}
    raise HTTPException(404, f"No history found for '{name}'")


@app.delete("/api/history")
def api_delete_all_history():
    """Clear all download history logs."""
    state = state_manager.get_all()
    for name in list(state.keys()):
        state_manager.remove(name)
    return {"ok": True, "message": "All history cleared"}


def _container_to_host_path(container_path: str) -> str:
    """
    Convert an in-container /downloads/... path to the corresponding
    host path using the volume mapping from docker-compose.
    """
    if not container_path:
        return ""
    if "[METADATA]" in container_path:
        return "Not available (metadata only)"
        
    config = load_config()
    host_dl = os.environ.get("HOST_DOWNLOAD_DIR", "/home/kazuha/Downloads")
    container_dl = get_download_path()
    if container_path.startswith(container_dl):
        return host_dl + container_path[len(container_dl):]
    return container_path


class OpenFolderBody(BaseModel):
    path: str


@app.post("/api/open-folder")
def api_open_folder(body: OpenFolderBody):
    """
    Attempt to open the folder containing the given file in the host file manager.
    Since this service runs in Docker, we try xdg-open on the mapped path.
    Always returns the host path so the frontend can show/copy it.
    """
    host_path = body.path
    folder    = str(Path(host_path).parent) if host_path else ""

    opened = False
    if folder:
        try:
            import platform
            if platform.system() == "Windows":
                os.startfile(folder)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(
                    ["xdg-open", folder],
                    env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            opened = True
        except Exception:
            pass

    return {"ok": True, "opened": opened, "folder": folder, "host_path": host_path}


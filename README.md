# Akari — Autonomous Anime Downloader for Homeservers

> A fully autonomous anime downloader bot designed for **Homeservers** and seedboxes. It monitors Nyaa.si, downloads new episodes via `aria2c`, sorts them into neat folders, and notifies you via a Telegram Bot — all managed through a beautiful, self-hosted web dashboard.

## 🚀 Features
- **Aria2c Backend**: Ultra-fast, lightweight downloading daemon.
- **Web Dashboard**: Modern SPA to manage settings, view active downloads, and history.
- **Smart Folder Sorting**: Automatically creates subfolders for each anime.
- **Manual Downloads**: Search Nyaa for specific (or older) episodes right from the dashboard.
- **Control Active Downloads**: Pause, Resume, and Cancel downloads visually.
- **One-Click Folder Access**: Open downloaded folders natively on your host machine from the History tab.
- **Telegram Control & Alerts**: Get notified exactly when episodes finish downloading and control the bot directly from Telegram.
- **Live Progress**: The `/status` command in Telegram shows real-time animated download progress bars.
- **Auto-Healing**: The bot automatically re-downloads missing files and cleans up orphaned episodes on startup.

## 🚀 Quick Start

### 1. Prerequisites
- [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/install/)
- A [Telegram bot](https://t.me/BotFather) (optional, for notifications and remote control)

### 2. Configure and Start

1. Create a `.env` file in the root directory to set your download path (this makes it easy to move the bot to a new PC):
```env
DOWNLOAD_DIR=/path/to/your/custom/folder
```

2. Start the containers:
```bash
docker compose up -d --build
```

This starts three containers:
| Container | URL | Purpose |
|---|---|---|
| `akari-dashboard` | http://localhost:5000 | **Web GUI** — configure everything and manage downloads here |
| `aria2` | (backend) | The fast download client, handles magnets and torrents |
| `akari` | (background) | The bot itself — polls Nyaa.si, handles Telegram commands, and manages state |

### 3. First-Time Setup (via Dashboard)

1. Open **http://localhost:5000** on your Homeserver.
2. Go to **⚙️ Settings** → Review Aria2c settings (usually works out of the box).
3. Go to **📱 Telegram** → Paste your bot token + chat ID → click **Send Test**.
4. Go to **📺 Anime** → Click **+ Add Anime** → Add the shows you want to track (Optionally assign a Season).
5. Done! The bot polls automatically based on your configured interval.

---

## 📁 File Structure

```text
akari/
├── config.yaml          ← Edited via Dashboard (or manually)
├── docker-compose.yml   ← Starts all services
├── Dockerfile
├── requirements.txt
├── src/                 ← Bot Python modules (poller, telegram bot, aria2 interface)
├── dashboard/           ← Web GUI (FastAPI + HTML/CSS/JS)
└── data/
    ├── aria2-config/     ← aria2c settings persistence
    ├── logs/bot.log      ← Bot activity log
    └── state.json        ← Download state (auto-managed)
```

**Where do my downloads go?**
Downloads are mapped directly to the folder you specify in your `.env` file (e.g. `DOWNLOAD_DIR=/home/kazuha/Videos`), and sorted into per-anime subfolders. If no `.env` is set, it defaults to `./downloads` inside the Akari directory.

---

## 📱 Telegram Commands
Once your Telegram Bot Token and Chat ID are configured, you can control the bot directly from Telegram:
- `/start` — Show welcome message and available commands
- `/status` — View all currently active downloads with an **animated live progress bar** that updates every second
- `/check` — Force an immediate manual check of Nyaa.si for new episodes
- `/list` — View a list of all your tracked anime, their latest downloaded episode, and current status (⬇️, ✅, 🌱, ❌)

---

## 🔧 Manual Config Editing

You can also edit `config.yaml` directly:

```yaml
anime:
  - name: "One Piece"
    nyaa_query: "One Piece"
    season: ""
    preferred_resolution: "1080p"
    category: "1_2"

poll_interval_minutes: 30
trusted_only: true

aria2:
  host: "http://aria2"
  port: 6800
  secret: "akarisecret"

downloads:
  save_path: "/downloads"

telegram:
  bot_token: "your_token"
  chat_id: "your_chat_id"
```

Changes take effect on the next poll cycle (no restart needed).

## 🛠️ Troubleshooting

### 1. "docker-credential-desktop": executable file not found in $PATH
**Symptom:** When running `docker compose up -d`, you get an error saying `error listing credentials - err: exec: "docker-credential-desktop": executable file not found`.
**Solution:** This is a common Docker Desktop artifact on Linux. Open `~/.docker/config.json` and completely remove the `"credsStore": "desktop"` line.

### 2. "Permission denied" errors from aria2c
**Symptom:** The bot logs show `❌ [Anime Name] EP[X] download error: Failed to open the file ... cause: Permission denied`.
**Solution:** This happens if the anime's subfolder was manually created or previously owned by `root` with strict `755` permissions, preventing the `aria2c` daemon (which runs as a secure user, UID 1000) from writing the video file. 
* **Fix:** Manually grant write permissions to the specific folder on your host machine: `chmod 777 -R /path/to/your/download/folder/"Anime Name"`.
*(Note: Akari automatically handles folder creation and applies 777 permissions for all new anime going forward!)*

---

## 💖 Credits and Open Source Tools

This project wouldn't be possible without the incredible work of the open-source community. Massive thanks to:

- **[aria2](https://aria2.github.io/)**: The ultra-fast, multi-protocol download utility powering the backend.
- **[Nyaa.si](https://nyaa.si/)**: The ultimate anime torrent tracker.
- **[FastAPI](https://fastapi.tiangolo.com/)**: For powering our lightning-fast web dashboard backend.
- **[Docker](https://www.docker.com/)**: For making deployment a breeze on any homeserver.
- **[Python](https://www.python.org/)**: The glue that holds the entire automation logic together.
- **[Telegram API](https://core.telegram.org/bots/api)**: For providing an excellent platform for remote notifications and control.
- **[P3TERX's aria2-pro](https://github.com/P3TERX/aria2.conf)**: For the perfectly tuned aria2 Docker image.

*A big thank you to all the developers and maintainers of these tools!*

"""
rss_poller.py — Fetches and parses Nyaa.si RSS feed.
"""
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

NYAA_RSS_URL = "https://nyaa.si/?page=rss"
NYAA_NS = "https://nyaa.si/xmlns/nyaa"

# Public trackers to boost magnet link connectivity
TRACKERS = [
    "http://nyaa.tracker.wf:7777/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
]


@dataclass
class Release:
    title: str
    torrent_url: str
    info_hash: str
    magnet: str
    seeders: int
    leechers: int
    size: str
    trusted: bool
    pub_date: datetime


def build_magnet(info_hash: str, title: str) -> str:
    """Construct a magnet URI from torrent info hash and title."""
    encoded = quote(title)
    tr = "&".join(f"tr={t}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{info_hash}&dn={encoded}&{tr}"


def fetch_releases(query: str, category: str = "1_2") -> list[Release]:
    """
    Fetch all releases matching the query from Nyaa RSS.

    Args:
        query: Search term (e.g. "One Piece")
        category: Nyaa category ID (default 1_2 = Anime English-translated)

    Returns:
        List of Release dataclass instances, newest first.
    """
    params = {"page": "rss", "c": category, "f": "0", "q": query}
    try:
        resp = requests.get(NYAA_RSS_URL, params=params, timeout=20, headers={
            "User-Agent": "Akari/1.0 (github.com/akari)"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch RSS for '{query}': {e}")
        return []

    releases: list[Release] = []
    try:
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        if channel is None:
            logger.warning(f"RSS channel element missing for query '{query}'")
            return []

        for item in channel.findall("item"):

            def get(tag: str, ns: str | None = None) -> str:
                el = item.find(f"{{{ns}}}{tag}" if ns else tag)
                return (el.text or "").strip() if el is not None else ""

            title = get("title")
            torrent_url = get("link")
            info_hash = get("infoHash", NYAA_NS)
            seeders = int(get("seeders", NYAA_NS) or 0)
            leechers = int(get("leechers", NYAA_NS) or 0)
            size = get("size", NYAA_NS) or "?"
            trusted = get("trusted", NYAA_NS).lower() == "yes"
            pub_date_text = get("pubDate")

            try:
                pub_date = parsedate_to_datetime(pub_date_text)
            except Exception:
                pub_date = datetime.utcnow()

            magnet = build_magnet(info_hash, title) if info_hash else ""

            releases.append(Release(
                title=title,
                torrent_url=torrent_url,
                info_hash=info_hash,
                magnet=magnet,
                seeders=seeders,
                leechers=leechers,
                size=size,
                trusted=trusted,
                pub_date=pub_date,
            ))

    except ET.ParseError as e:
        logger.error(f"Failed to parse RSS XML for '{query}': {e}")

    logger.debug(f"Fetched {len(releases)} releases for '{query}'")
    return releases

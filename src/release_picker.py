"""
release_picker.py — Episode number extraction and best-release selection logic.
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Ordered from most to least specific
EPISODE_PATTERNS = [
    r"S\d{1,2}E(\d{2,4})",                  # S01E12
    r"[-–\s](\d{2,4})\s*[\[\(]",            # " - 1169 [" or " - 1169 ("
    r"[-–\s](\d{2,4})\s*(?:END)?\s*$",      # " - 1169" at end of string
    r"EP?(\d{2,4})(?:\D|$)",                 # "EP1169" or "E1169"
    r"#(\d{2,4})\b",                         # "#1169"
    r"(?:^|\s)(\d{2,4})(?:\s|$)",           # standalone 2-4 digit number
]


def extract_episode_number(title: str) -> Optional[int]:
    """
    Attempt to extract an episode number from a release title.
    Returns None if no episode number can be found.
    """
    # Ignore year-like numbers (1920-2099) to avoid false positives
    for pattern in EPISODE_PATTERNS:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            num = int(match.group(1))
            if 1900 <= num <= 2099:       # Likely a year, skip
                continue
            return num
    return None


def pick_best_release(
    releases: list,
    preferred_res: str = "1080p",
    trusted_only: bool = True,
    prefer_uncensored: bool = False,
) -> Optional[object]:
    """
    Select the best release from a list of Release objects.

    Strategy (in order):
    1. Prefer trusted uploaders (if trusted_only=True and any exist)
    2. Prefer uncensored (if prefer_uncensored=True and any exist)
    3. Prefer the specified resolution
    4. Return the one with the highest seeder count
    """
    candidates = list(releases)
    if not candidates:
        return None

    # Step 1: filter by trusted
    if trusted_only:
        trusted = [r for r in candidates if r.trusted]
        if trusted:
            candidates = trusted
        else:
            logger.warning(
                "No trusted releases found — using all releases as fallback"
            )

    # Step 2: filter by uncensored (if requested)
    if prefer_uncensored:
        uncensored_filtered = [
            r for r in candidates 
            if "uncensored" in r.title.lower() or "uncen" in r.title.lower()
        ]
        if uncensored_filtered:
            candidates = uncensored_filtered

    # Step 3: filter by preferred resolution
    if preferred_res:
        res_filtered = [r for r in candidates if preferred_res.lower() in r.title.lower()]
        if res_filtered:
            candidates = res_filtered
        # If nothing matches the resolution, keep the unfiltered set

    # Step 3: highest seeders
    best = max(candidates, key=lambda r: r.seeders)
    logger.debug(f"Picked: '{best.title}' ({best.seeders} seeders)")
    return best


def group_releases_by_episode(releases: list) -> dict[int, list]:
    """
    Group a list of Release objects by extracted episode number.
    Releases whose episode number cannot be determined are discarded.
    """
    groups: dict[int, list] = {}
    for r in releases:
        ep = extract_episode_number(r.title)
        if ep is not None:
            groups.setdefault(ep, []).append(r)
    return groups

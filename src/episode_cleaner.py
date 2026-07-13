"""
episode_cleaner.py — Removes previous episode from aria2c and disk.
"""
import logging

logger = logging.getLogger(__name__)


def delete_previous_episode(aria2_client, anime_state: dict) -> bool:
    """
    Remove the previous episode's download from aria2c (including files on disk).

    Args:
        aria2_client: An Aria2Client instance.
        anime_state:  The state dict for this anime (from StateManager).

    Returns:
        True if deletion succeeded (or nothing to delete).
    """
    old_gid   = anime_state.get("current_gid", "")
    old_ep    = anime_state.get("last_episode", "?")
    old_title = anime_state.get("release_title", "unknown")
    old_path  = anime_state.get("current_file_path", "")

    if not old_gid:
        logger.debug("No previous episode GID recorded — nothing to delete")
        return True

    logger.info(f"🗑️  Deleting EP{old_ep}: {old_title}")
    if old_path:
        logger.info(f"   File: {old_path}")

    success = aria2_client.remove(old_gid, delete_files=True)
    if success:
        logger.info(f"✅ EP{old_ep} removed")
    else:
        logger.warning(f"⚠️  Could not fully remove EP{old_ep}")

    return success

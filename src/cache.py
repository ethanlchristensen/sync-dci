import os
import json
from datetime import datetime, timezone

# Stable location under %APPDATA% (Windows) / ~/.config (Linux/macOS)
_APP_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), ".config")),
    "SyncDCIFolder",
)
CACHE_FILE = os.path.join(_APP_DIR, "sync_cache.json")

# Legacy path — used for one-time migration
_LEGACY_CACHE = os.path.join(os.getcwd(), "sync_cache.json")


def _read_candidate(path: str) -> dict | None:
    """Try to read and parse a JSON cache file.  Returns None on failure."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def load_cache():
    """Loads state, folder path cache, configuration settings, and historical run metrics.

    Checks the legacy working-directory cache first for backward compatibility,
    then falls back to the stable app-data cache.
    """
    cache = {
        "last_run": None,
        "history": [],
        "average_speed_bps": None,
        "folder_cache": {},
        "folder_id": None,
        "dest_path": None,
        "start_page_token": None,
    }

    loaded = _read_candidate(_LEGACY_CACHE)
    if loaded is None:
        loaded = _read_candidate(CACHE_FILE)

    if loaded is not None:
        cache.update(loaded)

    return cache


def save_cache(cache):
    """Saves state, folder path cache, configurations, and metrics to the stable location."""
    os.makedirs(_APP_DIR, exist_ok=True)
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        import sys
        print(f"Warning: failed to save cache: {e}", file=sys.stderr)


def update_cache_history(cache, duration, bytes_downloaded, files_downloaded):
    """Updates the run cache history, recalculates average speed, and sets last_run to now."""
    if bytes_downloaded > 0 and duration > 0:
        run_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "duration_seconds": duration,
            "bytes_downloaded": bytes_downloaded,
            "files_downloaded": files_downloaded,
        }
        cache["history"].append(run_entry)
        cache["history"] = cache["history"][-10:]

        total_bytes = sum(r["bytes_downloaded"] for r in cache["history"])
        total_duration = sum(r["duration_seconds"] for r in cache["history"])
        if total_duration > 0:
            cache["average_speed_bps"] = total_bytes / total_duration

    cache["last_run"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    save_cache(cache)
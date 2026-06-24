import os
import json
from datetime import datetime, timezone

CACHE_FILE = "sync_cache.json"

def load_cache():
    """Loads state, folder path cache, configuration settings, and historical run metrics."""
    cache = {
        "last_run": None, 
        "history": [], 
        "average_speed_bps": None, 
        "folder_cache": {},
        "folder_id": None,
        "dest_path": None,
        "start_page_token": None  # Drive API token for tracking changes natively
    }
    
    # Load from unified cache file if it exists
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                loaded = json.load(f)
                cache.update(loaded)
        except Exception:
            pass
                
    return cache

def save_cache(cache):
    """Saves state, folder path cache, configurations, and metrics."""
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        # Fallback print if cache saving fails
        import sys
        print(f"Warning: failed to save cache: {e}", file=sys.stderr)

def update_cache_history(cache, duration, bytes_downloaded, files_downloaded):
    """Updates the run cache history, recalculates average speed, and sets last_run to now."""
    if bytes_downloaded > 0 and duration > 0:
        run_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "duration_seconds": duration,
            "bytes_downloaded": bytes_downloaded,
            "files_downloaded": files_downloaded
        }
        cache["history"].append(run_entry)
        cache["history"] = cache["history"][-10:] # Keep last 10 runs
        
        # Recalculate average speed from history
        total_bytes = sum(r["bytes_downloaded"] for r in cache["history"])
        total_duration = sum(r["duration_seconds"] for r in cache["history"])
        if total_duration > 0:
            cache["average_speed_bps"] = total_bytes / total_duration
            
    # Set last_run to the current UTC timestamp
    cache["last_run"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    save_cache(cache)

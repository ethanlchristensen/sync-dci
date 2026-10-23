from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
import csv


class EventStatus(Enum):
    """Status of a single download event."""
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class SyncMode(Enum):
    """Why a full or incremental scan was chosen for this run."""
    INCREMENTAL = "incremental"
    FULL_REQUESTED = "full_requested"
    FULL_MISSING_CACHE = "full_missing_cache"
    FULL_CHANGES_API_FALLBACK = "full_changes_api_fallback"
    FULL_INITIAL = "full_initial"


@dataclass
class DownloadEvent:
    """Immutable record of one file's download outcome."""
    relative_path: str
    status: EventStatus
    size: int = 0
    completion_time: str = ""
    reason: str = ""


def format_size(size_bytes: int) -> str:
    """Human-readable file size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def generate_manifests(events: list[DownloadEvent], dest_path: str,
                       sync_mode: SyncMode, summary: dict) -> tuple[str, str]:
    """Generate .txt and .csv manifests in *dest_path*.  Returns (txt_path, csv_path)."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = os.path.join(dest_path, f"sync-manifest-{timestamp}.txt")
    csv_path = os.path.join(dest_path, f"sync-manifest-{timestamp}.csv")

    status_labels = {
        EventStatus.DOWNLOADED: "[OK]",
        EventStatus.SKIPPED: "[SKIP]",
        EventStatus.FAILED: "[FAIL]",
        EventStatus.DRY_RUN: "[DRY RUN]",
    }

    # ---- .txt ----
    lines = [
        "=== Google Drive Sync Manifest ===",
        f"Generated: {datetime.now().isoformat()}",
        f"Destination: {dest_path}",
        f"Sync Mode: {sync_mode.value.replace('_', ' ').title()}",
        f"Downloaded: {summary.get('downloaded', 0)}",
        f"Skipped: {summary.get('skipped', 0)}",
        f"Failed: {summary.get('failed', 0)}",
        f"Duration: {summary.get('duration', 0):.1f}s",
    ]
    if summary.get("total_bytes", 0) > 0 and summary.get("duration", 0) > 0:
        speed_mb = (summary["total_bytes"] / summary["duration"]) / (1024 * 1024)
        lines.append(f"Speed: {speed_mb:.2f} MB/s")
    if summary.get("sync_reason"):
        lines.append(f"Sync reason: {summary['sync_reason']}")
    lines.append("")
    lines.append("--- Files ---")
    lines.append("")

    for event in events:
        label = status_labels.get(event.status, "[???]")
        time_str = f" @ {event.completion_time}" if event.completion_time else ""
        parts = [f"  {label}", event.relative_path]
        if event.completion_time:
            parts.append(f"@ {event.completion_time}")
        if event.size:
            parts.append(format_size(event.size))
        if event.reason:
            parts.append(f"({event.reason})")
        lines.append(" ".join(parts))

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # ---- .csv ----
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["status", "relative_path", "size_bytes", "reason", "completion_time"])
        for event in events:
            writer.writerow([
                event.status.value,
                event.relative_path,
                event.size,
                event.reason,
                event.completion_time,
            ])

    return txt_path, csv_path
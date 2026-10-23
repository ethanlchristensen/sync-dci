import os
import sys
import argparse
import time
import threading
import concurrent.futures
from datetime import datetime, timezone

from src.auth import authenticate, get_service
import src.cache as cache_mod
from src.cache import load_cache, save_cache, update_cache_history
from src.drive_sync import (
    traverse_and_download,
    get_changes_since,
    download_file,
)
from src.tui import TUIManager, SyncApp, SyncSummary, run_demo
from src.tui import (
    _Status,
    _Log,
    _InitProgress,
    _ProgressBytes,
    _FileDone,
    _Speed,
    _SyncEvent,
    _SyncComplete,
)
from src.models import (
    DownloadEvent,
    EventStatus,
    SyncMode,
    generate_manifests,
)


def main():
    parser = argparse.ArgumentParser(
        description="Sync Google Drive folder with smart caching."
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full scan to check and redownload any missing local files.",
    )
    parser.add_argument(
        "--folder-id",
        type=str,
        help="Configure or update the Google Drive Folder ID to sync.",
    )
    parser.add_argument(
        "--dest", type=str, help="Configure or update the local destination path."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Traverse and diff without downloading any files.",
    )
    parser.add_argument(
        "--cache-file", type=str, help="Override the default sync_cache.json path."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Preview the dashboard offline; no Drive or cache access.",
    )
    parser.add_argument(
        "--frames", type=int, help="Stop the offline demo after this many frames."
    )
    args = parser.parse_args()
    if args.frames is not None and (not args.demo or args.frames < 1):
        parser.error("--frames requires --demo and a positive integer")
    if args.demo:
        run_demo(frames=args.frames)
        return 0

    # Allow overriding the cache path (e.g. for testing)
    if args.cache_file:
        cache_mod.CACHE_FILE = os.path.abspath(os.path.expanduser(args.cache_file))
        cache_mod._APP_DIR = os.path.dirname(cache_mod.CACHE_FILE)
        cache_mod._LEGACY_CACHE = cache_mod.CACHE_FILE

    cache = load_cache()

    # -- config updates --------------------------------------------------
    config_updated = False
    if args.folder_id:
        cache["folder_id"] = args.folder_id.strip()
        config_updated = True
    if args.dest:
        cache["dest_path"] = os.path.abspath(os.path.expanduser(args.dest.strip()))
        config_updated = True
    if config_updated:
        save_cache(cache)

    # -- interactive config prompt ---------------------------------------
    if not cache.get("folder_id") or not cache.get("dest_path"):
        print("Configuration Setup\n")
        if not cache.get("folder_id"):
            folder_id_input = input("Enter your Google Drive Folder ID: ").strip()
            while not folder_id_input:
                folder_id_input = input(
                    "Folder ID cannot be empty. Enter Folder ID: "
                ).strip()
            cache["folder_id"] = folder_id_input

        if not cache.get("dest_path"):
            dest_path_input = input("Enter local Destination Directory Path: ").strip()
            while not dest_path_input:
                dest_path_input = input(
                    "Path cannot be empty. Enter local Destination Path: "
                ).strip()
            cache["dest_path"] = os.path.abspath(os.path.expanduser(dest_path_input))

        save_cache(cache)
        print(f"Configuration saved to {cache_mod.CACHE_FILE}\n")

    folder_id = cache["folder_id"]
    dest_path = cache["dest_path"]

    last_run_str = cache.get("last_run")
    start_page_token = cache.get("start_page_token")

    # -- determine sync mode ---------------------------------------------
    force_full = bool(
        args.full
        or not last_run_str
        or not start_page_token
        or not cache.get("folder_cache")
    )

    if args.full:
        sync_mode = SyncMode.FULL_REQUESTED
        sync_reason = "Full scan (--full flag)"
    elif not last_run_str or not start_page_token or not cache.get("folder_cache"):
        sync_mode = SyncMode.FULL_INITIAL
        sync_reason = "Full scan (missing cache data — first run or migrated cache)"
    else:
        sync_mode = SyncMode.INCREMENTAL
        sync_reason = "Incremental (Changes API)"

    avg_speed = cache.get("average_speed_bps")

    # OAuth may print instructions/open a browser: finish before taking over the terminal.
    print("Authenticating with Google Drive...")
    try:
        creds, service = authenticate()
    except Exception as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1

    tui = TUIManager(folder_id, dest_path, avg_speed=avg_speed)

    # Sync work runs off the bruhanimate presentation thread.
    def sync_runner(app: SyncApp) -> None:
        nonlocal sync_mode, sync_reason, force_full

        app.post_message(_Log(f"Cache file: {cache_mod.CACHE_FILE}", "dim white"))
        app.post_message(_Log(f"Sync mode: {sync_reason}", "bold cyan"))
        app.post_message(_Log("Successfully authenticated", "green"))

        # -- incremental vs full -----------------------------------------
        tasks = []
        skips = []

        if not force_full:
            app.post_message(_Status("Checking for changes..."))
            app.post_message(_Log("Querying Drive Changes API...", "blue"))
            try:
                tasks, skips, new_token = get_changes_since(
                    service, start_page_token, cache, tui=_TuiBridge(app)
                )
                if new_token:
                    cache["start_page_token"] = new_token
            except Exception as e:
                sync_mode = SyncMode.FULL_CHANGES_API_FALLBACK
                sync_reason = f"Full scan (Changes API fallback: {e})"
                app.post_message(
                    _Log(
                        f"Changes API failed: {e} — falling back to full scan",
                        "yellow",
                    )
                )
                force_full = True

        if force_full:
            last_run_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
            if args.full:
                app.post_message(_Status("Full Scan (Forced)..."))
                app.post_message(
                    _Log("Forcing full scan. Rebuilding folder cache...", "yellow")
                )
            else:
                app.post_message(_Status("Full Scan (Fallback/Initial)..."))
                app.post_message(
                    _Log("Running full scan to rebuild cache...", "yellow")
                )

            cache["folder_cache"] = {}
            tasks, skips = traverse_and_download(
                service, folder_id, dest_path, last_run_dt, cache, tui=_TuiBridge(app)
            )
            try:
                token_resp = (
                    service.changes()
                    .getStartPageToken(supportsAllDrives=True)
                    .execute()
                )
                cache["start_page_token"] = token_resp.get("startPageToken")
                app.post_message(
                    _Log(
                        "Initialized change tracking token for future syncs",
                        "dim white",
                    )
                )
            except Exception as e:
                app.post_message(
                    _Log(f"Could not fetch start page token: {e}", "yellow")
                )

        if app.stop_requested:
            app.post_message(
                _Log("Stopped after scanning; sync checkpoint unchanged", "yellow")
            )
            return

        # -- log skip summary ---------------------------------------------
        if skips:
            skips_by_reason = {}
            for name, reason in skips:
                skips_by_reason[reason] = skips_by_reason.get(reason, 0) + 1
            for reason, count in sorted(skips_by_reason.items()):
                app.post_message(_Log(f"Skipped {count} files: {reason}", "dim white"))

        download_count = 0
        failed_count = 0
        total_downloaded_bytes = 0
        duration = 0.0
        cancelled = False
        download_events: list[DownloadEvent] = []

        if tasks:
            total_bytes = sum(t[3] for t in tasks)
            total_files = len(tasks)
            dest_path_abs = os.path.abspath(dest_path)

            app.post_message(_InitProgress(total_files, total_bytes))
            mode_label = "[DRY RUN] " if args.dry_run else ""
            app.post_message(_Status(f"{mode_label}Downloading {total_files} files..."))
            app.post_message(
                _Log(
                    f"{mode_label}Found {total_files} updates ({total_bytes / (1024 * 1024):.2f} MB)",
                    "yellow" if args.dry_run else "green",
                )
            )

            if args.dry_run:
                for task in sorted(tasks, key=lambda t: t[1]):
                    if app.stop_requested:
                        break
                    file_name = os.path.basename(task[1])
                    app.post_message(
                        _Log(f"[DRY RUN] Would download: {file_name}", "yellow")
                    )
                    rel = _relative_path(task[1], dest_path_abs)
                    download_events.append(
                        DownloadEvent(
                            relative_path=rel or task[1],
                            status=EventStatus.DRY_RUN,
                            size=task[3],
                        )
                    )
                    app.post_message(_SyncEvent(download_events[-1]))
                    app.post_message(_FileDone())
                download_count = 0
                failed_count = 0
                total_downloaded_bytes = total_bytes
                duration = 0.0
            else:
                max_workers = 10
                start_time = time.time()

                lock = threading.Lock()

                def make_progress_callback():
                    def callback(delta_bytes):
                        nonlocal total_downloaded_bytes
                        with lock:
                            total_downloaded_bytes += delta_bytes
                        app.post_message(_ProgressBytes(delta_bytes))

                    return callback

                def custom_worker(
                    creds,
                    file_id,
                    file_path,
                    mime_type,
                    est_size,
                    remote_mtime_epoch=None,
                ):
                    nonlocal download_count, failed_count
                    if app.stop_requested:
                        return None
                    callback = make_progress_callback()
                    thread_service = get_service(creds)

                    file_name = os.path.basename(file_path)
                    app.post_message(_Log(f"Downloading: {file_name}", "cyan"))

                    success = download_file(
                        thread_service,
                        file_id,
                        file_path,
                        mime_type,
                        est_size,
                        callback,
                    )
                    if success and remote_mtime_epoch is not None:
                        try:
                            os.utime(
                                file_path, (remote_mtime_epoch, remote_mtime_epoch)
                            )
                        except Exception:
                            pass

                    rel = _relative_path(file_path, dest_path_abs)
                    now_ts = datetime.now().strftime("%H:%M:%S")
                    with lock:
                        if success:
                            download_count += 1
                            app.post_message(_Log(f"Finished: {file_name}", "green"))
                            download_events.append(
                                DownloadEvent(
                                    relative_path=rel or file_name,
                                    status=EventStatus.DOWNLOADED,
                                    size=est_size,
                                    completion_time=now_ts,
                                )
                            )
                        else:
                            failed_count += 1
                            app.post_message(_Log(f"Failed: {file_name}", "bold red"))
                            download_events.append(
                                DownloadEvent(
                                    relative_path=rel or file_name,
                                    status=EventStatus.FAILED,
                                    size=est_size,
                                    completion_time=now_ts,
                                )
                            )
                        app.post_message(_SyncEvent(download_events[-1]))
                        app.post_message(_FileDone())

                        elapsed = time.time() - start_time
                        if elapsed > 0 and total_downloaded_bytes > 0:
                            app.post_message(_Speed(total_downloaded_bytes / elapsed))

                    return success

                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers
                )
                try:
                    futures = {
                        executor.submit(
                            custom_worker,
                            creds,
                            task[0],
                            task[1],
                            task[2],
                            task[3],
                            task[4] if len(task) > 4 else None,
                        ): task
                        for task in tasks
                        if not app.stop_requested
                    }
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            task = futures[future]
                            event = DownloadEvent(
                                _relative_path(task[1], dest_path_abs),
                                EventStatus.FAILED,
                                size=task[3],
                                reason=str(e),
                            )
                            with lock:
                                failed_count += 1
                                download_events.append(event)
                            app.post_message(_SyncEvent(event))
                            app.post_message(_FileDone())
                            app.post_message(_Log(f"Worker exception: {e}", "bold red"))
                finally:
                    # Active requests finish; queued workers observe stop_requested.
                    executor.shutdown(wait=True)

                duration = time.time() - start_time
        else:
            app.post_message(_Log("All files are already up-to-date!", "bold green"))

        cancelled = app.stop_requested
        if not cancelled and not args.dry_run and not failed_count:
            update_cache_history(
                cache, duration, total_downloaded_bytes, download_count
            )
        else:
            app.post_message(
                _Log(
                    "Sync checkpoint unchanged (cancelled, dry run, or failed files)",
                    "yellow",
                )
            )

        # -- add skip events ----------------------------------------------
        for name, reason in skips:
            download_events.append(
                DownloadEvent(
                    relative_path=name,
                    status=EventStatus.SKIPPED,
                    reason=reason,
                )
            )

            app.post_message(_SyncEvent(download_events[-1]))

        # -- generate manifests -------------------------------------------
        summary: SyncSummary = {
            "cancelled": cancelled,
            "downloaded": download_count,
            "skipped": len(skips),
            "failed": failed_count,
            "duration": duration,
            "total_bytes": total_downloaded_bytes,
            "sync_reason": sync_reason,
        }
        try:
            txt_path, csv_path = generate_manifests(
                download_events,
                dest_path,
                sync_mode,
                summary,
            )
            summary["manifest_txt"] = txt_path
            summary["manifest_csv"] = csv_path
        except Exception as e:
            app.post_message(_Log(f"Manifest generation failed: {e}", "yellow"))

        # -- finalise -----------------------------------------------------
        app.post_message(_SyncComplete(**summary))

    # A bridge that adapts the old TUIManager.log/add_log interface
    # for callers in drive_sync that accept an optional `tui` arg.
    class _TuiBridge:
        def __init__(self, app):
            self._app = app

        def add_log(self, text, style=None):
            self._app.post_message(_Log(text, style or ""))

        def set_status(self, msg):
            self._app.post_message(_Status(msg))

    tui.set_sync_func(sync_runner)
    tui.run()
    return 0


def _relative_path(abs_path: str, base: str) -> str:
    """Return a relative path if *abs_path* is under *base*, else the basename."""
    try:
        return os.path.relpath(abs_path, base)
    except ValueError:
        return os.path.basename(abs_path)


if __name__ == "__main__":
    sys.exit(main())

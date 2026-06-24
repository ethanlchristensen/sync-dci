import os
import argparse
import time
import threading
import concurrent.futures
from datetime import datetime, timezone

from src.auth import authenticate, get_service
from src.cache import load_cache, save_cache, update_cache_history
from src.drive_sync import parse_iso_datetime, traverse_and_download, get_changes_since, download_file
from src.tui import TUIManager

def main():
    parser = argparse.ArgumentParser(description="Sync Google Drive folder with smart caching.")
    parser.add_argument(
        "--full", 
        action="store_true", 
        help="Force a full scan to check and redownload any missing local files."
    )
    parser.add_argument(
        "--folder-id",
        type=str,
        help="Configure or update the Google Drive Folder ID to sync."
    )
    parser.add_argument(
        "--dest",
        type=str,
        help="Configure or update the local destination path."
    )
    args = parser.parse_args()
    
    # Load unified state/cache
    cache = load_cache()
    
    # Apply CLI configuration updates if provided
    config_updated = False
    if args.folder_id:
        cache["folder_id"] = args.folder_id.strip()
        config_updated = True
    if args.dest:
        cache["dest_path"] = os.path.abspath(os.path.expanduser(args.dest.strip()))
        config_updated = True
        
    if config_updated:
        save_cache(cache)
        
    # Interactive Onboarding Prompt if config is missing
    if not cache.get("folder_id") or not cache.get("dest_path"):
        print("Configuration Setup\n")
        if not cache.get("folder_id"):
            folder_id_input = input("Enter your Google Drive Folder ID: ").strip()
            while not folder_id_input:
                folder_id_input = input("Folder ID cannot be empty. Enter Folder ID: ").strip()
            cache["folder_id"] = folder_id_input
            
        if not cache.get("dest_path"):
            dest_path_input = input("Enter local Destination Directory Path: ").strip()
            while not dest_path_input:
                dest_path_input = input("Path cannot be empty. Enter local Destination Path: ").strip()
            cache["dest_path"] = os.path.abspath(os.path.expanduser(dest_path_input))
            
        save_cache(cache)
        print("Configuration successfully saved!\n")
        
    folder_id = cache["folder_id"]
    dest_path = cache["dest_path"]
    
    last_run_str = cache.get("last_run")
    start_page_token = cache.get("start_page_token")
    
    # We must do a full scan if explicitly requested, if there's no last run,
    # if there is no startPageToken, or if the folder_cache is empty.
    force_full = args.full or not last_run_str or not start_page_token or not cache.get("folder_cache")
    
    # Initialize full-screen TUI
    avg_speed = cache.get("average_speed_bps")
    with TUIManager(folder_id, dest_path, avg_speed=avg_speed) as tui:
        try:
            tui.set_status("Authenticating...")
            creds, service = authenticate()
            tui.add_log("Successfully authenticated with Google Drive API", style="green")
            
            if force_full:
                last_run = datetime(1970, 1, 1, tzinfo=timezone.utc)
                if args.full:
                    tui.set_status("Full Scan (Forced)...")
                    tui.add_log("Forcing full scan. Rebuilding folder cache...", style="yellow")
                else:
                    tui.set_status("Full Scan (Initial)...")
                    tui.add_log("Initial sync or empty cache. Rebuilding cache...", style="yellow")
                    
                # Rebuild folder path cache from scratch during traversal
                cache["folder_cache"] = {}
                tasks, skips = traverse_and_download(
                    service, folder_id, dest_path, last_run, cache, filter_by_date=False, tui=tui
                )
                # Save immediately after traversal to record the new start_page_token
                save_cache(cache)
            else:
                last_run = parse_iso_datetime(last_run_str)
                tui.set_status("Checking for changes...")
                tui.add_log("Querying native Google Drive Changes API...", style="blue")
                
                tasks, skips, new_token = get_changes_since(service, start_page_token, cache, tui=tui)
                if new_token:
                    cache["start_page_token"] = new_token
                    save_cache(cache)

            # Log skips summary
            if skips:
                skips_by_reason = {}
                for name, reason in skips:
                    skips_by_reason[reason] = skips_by_reason.get(reason, 0) + 1
                for reason, count in skips_by_reason.items():
                    tui.add_log(f"Skipped {count} files: {reason}", style="dim white")

            download_count = 0
            total_downloaded_bytes = 0
            duration = 0.0
            
            if tasks:
                total_bytes = sum(t[3] for t in tasks)
                total_files = len(tasks)
                
                tui.init_progress(total_files, total_bytes)
                tui.set_status(f"Downloading {total_files} files...")
                tui.add_log(f"Found {total_files} updates to download ({total_bytes / (1024*1024):.2f} MB)", style="green")
                
                max_workers = 10
                start_time = time.time()
                
                lock = threading.Lock()
                
                def make_progress_callback(est_size):
                    def callback(delta_bytes):
                        nonlocal total_downloaded_bytes
                        with lock:
                            total_downloaded_bytes += delta_bytes
                        tui.advance_bytes(delta_bytes)
                    return callback
                
                def custom_worker(creds, file_id, file_path, mime_type, est_size):
                    nonlocal download_count
                    callback = make_progress_callback(est_size)
                    thread_service = get_service(creds)
                    
                    file_name = os.path.basename(file_path)
                    tui.add_log(f"Downloading: {file_name}", style="cyan")
                    
                    success = download_file(thread_service, file_id, file_path, mime_type, est_size, callback)
                    with lock:
                        if success:
                            download_count += 1
                            tui.add_log(f"Finished: {file_name}", style="green")
                        else:
                            tui.add_log(f"Failed: {file_name}", style="bold red")
                        tui.advance_files(1)
                    return success
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(custom_worker, creds, task[0], task[1], task[2], task[3]): task
                        for task in tasks
                    }
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            tui.add_log(f"Worker Exception: {e}", style="bold red")
                
                duration = time.time() - start_time
                update_cache_history(cache, duration, total_downloaded_bytes, download_count)
            else:
                tui.add_log("All files are already up-to-date!", style="bold green")
                update_cache_history(cache, 0, 0, 0)
                
            tui.set_status("Completed")
            summary_msg = (
                f"Sync Completed! Updated: {download_count} files. "
                f"Skips: {len(skips)}. Duration: {duration:.1f}s."
            )
            tui.add_log(summary_msg, style="bold green")
            time.sleep(3)
            
        except KeyboardInterrupt:
            tui.add_log("Sync cancelled by user.", style="bold yellow")
            tui.set_status("Cancelled")
            time.sleep(2)
        except Exception as e:
            tui.add_log(f"Sync failed with error: {e}", style="bold red")
            tui.set_status("Failed")
            time.sleep(5)

if __name__ == "__main__":
    main()

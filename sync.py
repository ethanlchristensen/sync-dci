from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import os
import io
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
import concurrent.futures
import threading
from pathvalidate import sanitize_filepath, sanitize_filename

# Rich library imports
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn

# Define the scopes
SCOPES = ['https://www.googleapis.com/auth/drive']

# Thread-local storage for safe multi-threaded Google API client usage
thread_local = threading.local()

# Console instance for clean rich output
console = Console()

CACHE_FILE = "sync_cache.json"

# Google Workspace MIME types and their export targets (mimeType, extension)
EXPORT_MAPPING = {
    'application/vnd.google-apps.document': (
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.docx'
    ),
    'application/vnd.google-apps.spreadsheet': (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xlsx'
    ),
    'application/vnd.google-apps.presentation': (
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        '.pptx'
    ),
    'application/vnd.google-apps.drawing': (
        'image/png',
        '.png'
    ),
}

def parse_iso_datetime(dt_str):
    """Parses ISO 8601 string into a timezone-aware datetime object (UTC)."""
    if not dt_str:
        return None
    normalized = dt_str.replace('Z', '+00:00')
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def load_cache():
    """Loads state, folder path cache, configuration settings, and historical run metrics."""
    cache = {
        "last_run": None, 
        "history": [], 
        "average_speed_bps": None, 
        "folder_cache": {},
        "folder_id": None,
        "dest_path": None
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
        console.print(f"[yellow]Warning: failed to save cache: {e}[/yellow]")

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

def estimate_file_size(mime_type, raw_size_str):
    """Estimates download file size. Standardizes Google Workspace formats."""
    if raw_size_str:
        try:
            return int(raw_size_str)
        except ValueError:
            pass
    # Google Workspace export size estimations
    if mime_type == 'application/vnd.google-apps.document':
        return 100 * 1024  # docx ~100 KB
    elif mime_type == 'application/vnd.google-apps.spreadsheet':
        return 150 * 1024  # xlsx ~150 KB
    elif mime_type == 'application/vnd.google-apps.presentation':
        return 500 * 1024  # pptx ~500 KB
    elif mime_type == 'application/vnd.google-apps.drawing':
        return 200 * 1024  # png ~200 KB
    return 10 * 1024  # Default 10KB fallback

def authenticate():
    """Authenticates the user and returns credentials and service."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=8080)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    service = build('drive', 'v3', credentials=creds)
    return creds, service

def get_service(creds):
    """Returns a thread-local service instance to ensure thread safety."""
    if not hasattr(thread_local, "service"):
        thread_local.service = build('drive', 'v3', credentials=creds)
    return thread_local.service

def download_file(service, file_id, file_path, mime_type=None, estimated_size=0, progress_callback=None):
    """Downloads a single file or exports a Google Workspace file."""
    try:
        if mime_type and mime_type.startswith('application/vnd.google-apps.'):
            if mime_type in EXPORT_MAPPING:
                export_mime, ext = EXPORT_MAPPING[mime_type]
                if not file_path.lower().endswith(ext):
                    file_path += ext
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                return False
        else:
            request = service.files().get_media(fileId=file_id)
        
        parent_dir = os.path.dirname(file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
            
        fh = io.FileIO(file_path, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        last_bytes = 0
        while not done:
            status, done = downloader.next_chunk()
            tot_size = getattr(status, "total_size", 0) or 0
            if tot_size > 0:
                current_bytes = int(status.progress() * tot_size)
                delta = current_bytes - last_bytes
                if delta > 0:
                    if progress_callback:
                        progress_callback(delta)
                    last_bytes = current_bytes
        
        # Handle cases where GSuite exports didn't report exact size
        if last_bytes == 0 and progress_callback and estimated_size > 0:
            progress_callback(estimated_size)
            
        return True
    except Exception as e:
        # Gracefully handle download failure and complete progress allocation if needed
        return False

def resolve_folder_path(service, folder_id, cache):
    """
    Self-healing helper: Walks up the parent chain of a folder.
    If it reaches a folder already in the local cache, maps all intermediate paths and returns resolved local path.
    """
    current_id = folder_id
    chain = []
    root_id = cache.get("folder_id")
    
    try:
        while current_id:
            if current_id in cache["folder_cache"]:
                # Reconstruct and cache paths down the chain
                path = cache["folder_cache"][current_id]
                for f_id, f_name in reversed(chain):
                    sanitized_name = sanitize_filename(f_name, platform="windows")
                    path = os.path.join(path, sanitized_name)
                    cache["folder_cache"][f_id] = path
                return path
                
            if current_id == root_id:
                return None
                
            meta = service.files().get(fileId=current_id, fields="id, name, parents").execute()
            parents = meta.get('parents', [])
            name = meta.get('name', 'unnamed')
            
            chain.append((current_id, name))
            current_id = parents[0] if parents else None
    except Exception:
        pass
        
    return None

def traverse_and_download(service, folder_id, dest_path, last_run_dt, cache, filter_by_date=True):
    """
    Full scan/initial scan: Traverses folders recursively, compiling download tasks, skips,
    and building the local folder ID-to-path cache mapping.
    """
    dest_path = sanitize_filepath(dest_path, platform="windows")
    path_parts = [p.strip() for p in Path(dest_path).parts]
    dest_path = os.path.join(*path_parts)
    
    os.makedirs(dest_path, exist_ok=True)
    
    # Cache this folder path
    cache["folder_cache"][folder_id] = dest_path
    
    tasks = []
    skips = []
    page_token = None
    
    last_run_str = last_run_dt.isoformat().replace("+00:00", "Z")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    
    while True:
        if filter_by_date and last_run_dt > epoch:
            query = (
                f"'{folder_id}' in parents and trashed=false and "
                f"(mimeType = 'application/vnd.google-apps.folder' or "
                f"modifiedTime > '{last_run_str}' or createdTime > '{last_run_str}')"
            )
        else:
            query = f"'{folder_id}' in parents and trashed=false"
            
        results = service.files().list(
            q=query,
            spaces='drive',
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, createdTime, size)",
            pageToken=page_token
        ).execute()
        
        items = results.get('files', [])
        for item in items:
            mime_type = item.get('mimeType')
            item_name = item.get('name', 'unnamed')
            sanitized_name = sanitize_filename(item_name, platform="windows")
            file_path = os.path.join(dest_path, sanitized_name)
            
            if mime_type != 'application/vnd.google-apps.folder':
                final_file_path = file_path
                if mime_type.startswith('application/vnd.google-apps.'):
                    if mime_type in EXPORT_MAPPING:
                        _, ext = EXPORT_MAPPING[mime_type]
                        if not final_file_path.lower().endswith(ext):
                            final_file_path += ext
                    else:
                        skips.append((item_name, f"Unsupported GSuite format: {mime_type}"))
                        continue
                
                should_download = False
                if not os.path.exists(final_file_path):
                    should_download = True
                else:
                    mod_dt = parse_iso_datetime(item.get('modifiedTime'))
                    cre_dt = parse_iso_datetime(item.get('createdTime'))
                    
                    if mod_dt and mod_dt > last_run_dt:
                        should_download = True
                    elif cre_dt and cre_dt > last_run_dt:
                        should_download = True
                
                if should_download:
                    raw_size = item.get('size')
                    est_size = estimate_file_size(mime_type, raw_size)
                    tasks.append((item['id'], file_path, mime_type, est_size))
                else:
                    skips.append((item_name, "Already up-to-date"))
            else:
                sub_tasks, sub_skips = traverse_and_download(
                    service, item['id'], file_path, last_run_dt, cache, filter_by_date
                )
                tasks.extend(sub_tasks)
                skips.extend(sub_skips)
                
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    return tasks, skips

def get_changes_since(service, last_run_dt, cache):
    """
    Lightning-fast Change Sync: Instantly queries Google Drive for ALL modified/created items
    since last_run_dt, routing them instantly using the local folder path cache.
    No directory traversal needed!
    """
    last_run_str = last_run_dt.isoformat().replace("+00:00", "Z")
    tasks = []
    skips = []
    
    page_token = None
    while True:
        # Get all modified items in the entire Drive in one single query
        query = f"trashed=false and (modifiedTime > '{last_run_str}' or createdTime > '{last_run_str}')"
        results = service.files().list(
            q=query,
            spaces='drive',
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, createdTime, size, parents)",
            pageToken=page_token
        ).execute()
        
        items = results.get('files', [])
        
        # Process folders first to ensure newly created subfolders are cached before we route files inside them
        folders = [i for i in items if i.get('mimeType') == 'application/vnd.google-apps.folder']
        files = [i for i in items if i.get('mimeType') != 'application/vnd.google-apps.folder']
        
        for folder in folders:
            folder_id = folder['id']
            folder_name = folder.get('name', 'unnamed')
            parents = folder.get('parents', [])
            
            if parents:
                parent_id = parents[0]
                if parent_id in cache["folder_cache"]:
                    parent_path = cache["folder_cache"][parent_id]
                    sanitized_name = sanitize_filename(folder_name, platform="windows")
                    local_path = os.path.join(parent_path, sanitized_name)
                    cache["folder_cache"][folder_id] = local_path
                    os.makedirs(local_path, exist_ok=True)
                else:
                    # Parent is not directly cached; try self-healing resolution
                    resolved_path = resolve_folder_path(service, folder_id, cache)
                    if resolved_path:
                        cache["folder_cache"][folder_id] = resolved_path
                        os.makedirs(resolved_path, exist_ok=True)
                        
        for file in files:
            file_id = file['id']
            file_name = file.get('name', 'unnamed')
            mime_type = file.get('mimeType')
            parents = file.get('parents', [])
            
            if parents:
                parent_id = parents[0]
                # If the parent folder belongs inside our synced tree
                if parent_id in cache["folder_cache"]:
                    dest_path = cache["folder_cache"][parent_id]
                    sanitized_name = sanitize_filename(file_name, platform="windows")
                    file_path = os.path.join(dest_path, sanitized_name)
                    
                    final_file_path = file_path
                    if mime_type.startswith('application/vnd.google-apps.'):
                        if mime_type in EXPORT_MAPPING:
                            _, ext = EXPORT_MAPPING[mime_type]
                            if not final_file_path.lower().endswith(ext):
                                final_file_path += ext
                        else:
                            skips.append((file_name, f"Unsupported GSuite format: {mime_type}"))
                            continue
                    
                    should_download = False
                    if not os.path.exists(final_file_path):
                        should_download = True
                    else:
                        mod_dt = parse_iso_datetime(file.get('modifiedTime'))
                        cre_dt = parse_iso_datetime(file.get('createdTime'))
                        if mod_dt and mod_dt > last_run_dt:
                            should_download = True
                        elif cre_dt and cre_dt > last_run_dt:
                            should_download = True
                            
                    if should_download:
                        raw_size = file.get('size')
                        est_size = estimate_file_size(mime_type, raw_size)
                        tasks.append((file_id, file_path, mime_type, est_size))
                    else:
                        skips.append((file_name, "Already up-to-date"))
                        
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    return tasks, skips

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
    
    console.print(Panel("[bold cyan]Google Drive Sync DC Folder[/bold cyan]", border_style="cyan"))
    
    # Load unified state/cache
    cache = load_cache()
    
    # Apply CLI configuration updates if provided
    config_updated = False
    if args.folder_id:
        cache["folder_id"] = args.folder_id.strip()
        config_updated = True
        console.print(f"[green]Google Drive Folder ID configured to: {cache['folder_id']}[/green]")
    if args.dest:
        cache["dest_path"] = os.path.abspath(os.path.expanduser(args.dest.strip()))
        config_updated = True
        console.print(f"[green]Local destination path configured to: {cache['dest_path']}[/green]")
        
    if config_updated:
        save_cache(cache)
        
    # Interactive Onboarding Prompt if config is missing
    if not cache.get("folder_id") or not cache.get("dest_path"):
        console.print(Panel(
            "[bold yellow]Configuration Setup[/bold yellow]\n\n"
            "This is your first run, or configuration is missing. "
            "Please provide your sync settings below.",
            border_style="yellow"
        ))
        
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
        console.print("[bold green]Configuration successfully saved to sync_cache.json![/bold green]\n")
        
    folder_id = cache["folder_id"]
    dest_path = cache["dest_path"]
    
    last_run_str = cache.get("last_run")
    filter_by_date = not args.full
    
    # We must do a full scan if explicitly requested, if there's no last run,
    # or if the folder_cache is completely empty.
    force_full = args.full or not last_run_str or not cache.get("folder_cache")
    
    if force_full:
        last_run = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if args.full:
            console.print("[yellow]Forcing full scan. Rebuilding folder cache and checking all files...[/yellow]")
        else:
            console.print("[yellow]Initial sync or empty folder cache. Doing a full scan to build cache...[/yellow]")
    else:
        last_run = parse_iso_datetime(last_run_str)
        console.print(f"Syncing changes since [bold white]{last_run.isoformat()}[/bold white]...")

    try:
        creds, service = authenticate()
        
        if force_full:
            # Rebuild folder path cache from scratch during traversal
            cache["folder_cache"] = {}
            with console.status("[bold blue]Traversing Google Drive folder structure (Full Scan)...[/bold blue]", spinner="dots") as status:
                tasks, skips = traverse_and_download(service, folder_id, dest_path, last_run, cache, filter_by_date=False)
        else:
            # Lightning-fast changed-only scan using single Drive query and folder cache
            with console.status("[bold blue]Querying Google Drive for changes...[/bold blue]", spinner="dots") as status:
                tasks, skips = get_changes_since(service, last_run, cache)
        
        # Print skips table summary
        if skips:
            table = Table(title="Skipped Files Summary", title_style="bold magenta")
            table.add_column("Reason", style="cyan")
            table.add_column("Count", style="green", justify="right")
            
            skips_by_reason = {}
            for _, reason in skips:
                skips_by_reason[reason] = skips_by_reason.get(reason, 0) + 1
                
            for reason, count in skips_by_reason.items():
                table.add_row(reason, str(count))
            console.print(table)
            
            # Print detailed unsupported/error skips specifically
            unsupported_skips = [s for s in skips if "Already up-to-date" not in s[1]]
            if unsupported_skips:
                console.print("\n[bold yellow]Specifically Skipped Formats (Unsupported):[/bold yellow]")
                for name, reason in unsupported_skips:
                    console.print(f" • [yellow]{name}[/yellow]: {reason}")
                console.print("")

        download_count = 0
        total_downloaded_bytes = 0
        duration = 0.0
        
        if tasks:
            total_bytes = sum(t[3] for t in tasks)
            total_files = len(tasks)
            
            # Retrieve average speed from cache
            avg_speed = cache.get("average_speed_bps")
            
            if avg_speed and avg_speed > 0:
                estimated_seconds = total_bytes / avg_speed
                if estimated_seconds >= 60:
                    est_str = f"{int(estimated_seconds // 60)}m {int(estimated_seconds % 60)}s"
                else:
                    est_str = f"{estimated_seconds:.1f}s"
                speed_mb = avg_speed / (1024 * 1024)
                console.print(f"[bold green]Historical download speed: {speed_mb:.2f} MB/s[/bold green]")
                console.print(f"[bold green]Estimated completion time: {est_str}[/bold green]")
            else:
                fallback_speed = 2 * 1024 * 1024  # 2.0 MB/s default
                estimated_seconds = total_bytes / fallback_speed
                if estimated_seconds >= 60:
                    est_str = f"{int(estimated_seconds // 60)}m {int(estimated_seconds % 60)}s"
                else:
                    est_str = f"{estimated_seconds:.1f}s"
                console.print(f"[yellow]No historical speed data. Estimated duration: {est_str} (assuming 2.0 MB/s)[/yellow]")
            
            max_workers = 10
            console.print(f"\n[bold white]Starting sync ({total_files} files, {total_bytes / (1024*1024):.2f} MB)...[/bold white]\n")
            
            start_time = time.time()
            
            # Progress bar visualization using rich
            with Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console
            ) as progress:
                
                byte_task = progress.add_task("[green]Total Bytes", total=total_bytes)
                file_task = progress.add_task("[cyan]Total Files", total=total_files)
                
                lock = threading.Lock()
                
                def make_progress_callback(est_size):
                    def callback(delta_bytes):
                        nonlocal total_downloaded_bytes
                        with lock:
                            total_downloaded_bytes += delta_bytes
                        progress.advance(byte_task, delta_bytes)
                    return callback
                
                def custom_worker(creds, file_id, file_path, mime_type, est_size):
                    nonlocal download_count
                    callback = make_progress_callback(est_size)
                    service = get_service(creds)
                    success = download_file(service, file_id, file_path, mime_type, est_size, callback)
                    with lock:
                        if success:
                            download_count += 1
                        progress.advance(file_task, 1)
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
                            progress.console.print(f"[red]Task generated exception: {e}[/red]")
            
            duration = time.time() - start_time
            update_cache_history(cache, duration, total_downloaded_bytes, download_count)
        else:
            console.print("\n[bold green]All files are already up-to-date![/bold green]\n")
            # Only update the last_run timestamp
            update_cache_history(cache, 0, 0, 0)
            
        # Display elegant final report panel
        summary_text = (
            f"[bold green]Sync completed successfully![/bold green]\n\n"
            f"• Files downloaded/updated: [bold white]{download_count}[/bold white]\n"
            f"• Files checked and skipped: [bold white]{len(skips)}[/bold white]\n"
        )
        if download_count > 0:
            speed_mb = (total_downloaded_bytes / (1024 * 1024)) / duration if duration > 0 else 0
            summary_text += (
                f"• Total downloaded size: [bold white]{total_downloaded_bytes / (1024*1024):.2f} MB[/bold white]\n"
                f"• Duration: [bold white]{duration:.1f} seconds[/bold white]\n"
                f"• Actual download speed: [bold white]{speed_mb:.2f} MB/s[/bold white]"
            )
        console.print(Panel(summary_text, title="Sync Execution Report", border_style="green"))
        
    except Exception as e:
        console.print(f"\n[bold red]Sync failed with error: {e}[/bold red]")
        if 'download_count' in locals():
            console.print(f"[yellow]Downloaded {download_count} files before failure.[/yellow]")

if __name__ == "__main__":
    main()

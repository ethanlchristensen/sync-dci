import os
import io
from pathlib import Path
from datetime import datetime, timezone
from pathvalidate import sanitize_filepath, sanitize_filename
from googleapiclient.http import MediaIoBaseDownload

# Mapping of GSuite items
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
    except Exception:
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
                # If root is reached, populate the root directory cache and reconstruct path
                dest_path = cache.get("dest_path")
                if dest_path:
                    cache["folder_cache"][root_id] = dest_path
                    path = dest_path
                    for f_id, f_name in reversed(chain):
                        sanitized_name = sanitize_filename(f_name, platform="windows")
                        path = os.path.join(path, sanitized_name)
                        cache["folder_cache"][f_id] = path
                    return path
                return None
                
            meta = service.files().get(fileId=current_id, fields="id, name, parents").execute()
            parents = meta.get('parents', [])
            name = meta.get('name', 'unnamed')
            
            chain.append((current_id, name))
            current_id = parents[0] if parents else None
    except Exception:
        pass
        
    return None

def traverse_and_download(service, folder_id, dest_path, last_run_dt, cache, filter_by_date=True, tui=None):
    """
    Full scan/initial scan: Traverses folders recursively, compiling download tasks, skips,
    and building the local folder ID-to-path cache mapping.
    Also fetches and caches current startPageToken for future native change syncs.
    """
    dest_path = sanitize_filepath(dest_path, platform="windows")
    path_parts = [p.strip() for p in Path(dest_path).parts]
    dest_path = os.path.join(*path_parts)
    
    os.makedirs(dest_path, exist_ok=True)
    
    # Cache this folder path
    cache["folder_cache"][folder_id] = dest_path
    
    # Try to capture starting page token at the beginning of full traversal
    if "start_page_token" not in cache or cache["start_page_token"] is None:
        try:
            token_res = service.changes().getStartPageToken().execute()
            cache["start_page_token"] = token_res.get('startPageToken')
            if tui:
                tui.add_log(f"Initialized Drive changes startPageToken: {cache['start_page_token']}", style="dim white")
        except Exception as e:
            if tui:
                tui.add_log(f"Warning: Failed to fetch changes starting token: {e}", style="yellow")
                
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
                    service, item['id'], file_path, last_run_dt, cache, filter_by_date, tui
                )
                tasks.extend(sub_tasks)
                skips.extend(sub_skips)
                
        page_token = results.get('nextPageToken')
        if not page_token:
            break
            
    return tasks, skips

def get_changes_since(service, start_page_token, cache, tui=None):
    """
    Robust Incremental Sync using Google Drive's native Changes API.
    Uses self-healing path resolution to ensure files in newly created folders are routed.
    """
    tasks = []
    skips = []
    
    current_token = start_page_token
    if not current_token:
        try:
            current_token = service.changes().getStartPageToken().execute().get('startPageToken')
            cache["start_page_token"] = current_token
        except Exception as e:
            if tui:
                tui.add_log(f"Failed to fetch start page token: {e}", style="red")
            return [], [], None
            
    next_page_token = current_token
    new_start_page_token = None
    
    while next_page_token:
        try:
            results = service.changes().list(
                pageToken=next_page_token,
                spaces='drive',
                fields="nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, modifiedTime, createdTime, size, parents, trashed))"
            ).execute()
        except Exception as e:
            if tui:
                tui.add_log(f"Error fetching changes from Drive API: {e}", style="red")
            break
            
        changes = results.get('changes', [])
        
        # Split folders and files to process folders first (optimization)
        folders = []
        files = []
        for change in changes:
            if change.get('removed'):
                continue
            item = change.get('file')
            if not item or item.get('trashed'):
                continue
            if item.get('mimeType') == 'application/vnd.google-apps.folder':
                folders.append(item)
            else:
                files.append(item)
                
        # 1. Process folders to populate local cache
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
                    # Self-healing parent resolution
                    resolved_path = resolve_folder_path(service, folder_id, cache)
                    if resolved_path:
                        cache["folder_cache"][folder_id] = resolved_path
                        os.makedirs(resolved_path, exist_ok=True)
                        
        # 2. Process files
        for file in files:
            file_id = file['id']
            file_name = file.get('name', 'unnamed')
            mime_type = file.get('mimeType')
            parents = file.get('parents', [])
            
            if parents:
                parent_id = parents[0]
                # If parent not in cache, resolve it dynamically (handles files processed before folder parent)
                if parent_id not in cache["folder_cache"]:
                    resolved_path = resolve_folder_path(service, parent_id, cache)
                    if resolved_path:
                        cache["folder_cache"][parent_id] = resolved_path
                        
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
                        # Drive Changes API signals modification, we always update modified files
                        should_download = True
                        
                    if should_download:
                        raw_size = file.get('size')
                        est_size = estimate_file_size(mime_type, raw_size)
                        tasks.append((file_id, file_path, mime_type, est_size))
                    else:
                        skips.append((file_name, "Already up-to-date"))
                        
        next_page_token = results.get('nextPageToken')
        new_start_page_token = results.get('newStartPageToken')
        if not next_page_token:
            break
            
    final_token = new_start_page_token or next_page_token or current_token
    return tasks, skips, final_token

import os
import io
from pathlib import Path
from datetime import datetime, timezone
from pathvalidate import sanitize_filepath, sanitize_filename
from googleapiclient.http import MediaIoBaseDownload

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
    if mime_type == 'application/vnd.google-apps.document':
        return 100 * 1024
    elif mime_type == 'application/vnd.google-apps.spreadsheet':
        return 150 * 1024
    elif mime_type == 'application/vnd.google-apps.presentation':
        return 500 * 1024
    elif mime_type == 'application/vnd.google-apps.drawing':
        return 200 * 1024
    return 10 * 1024

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
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

        parent_dir = os.path.dirname(file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with io.FileIO(file_path, 'wb') as fh:
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

        # GSuite exports don't report chunk progress — smooth the jump
        if last_bytes == 0 and progress_callback and estimated_size > 0:
            chunk_size = max(estimated_size // 4, 1)
            remaining = estimated_size
            while remaining > chunk_size:
                progress_callback(chunk_size)
                remaining -= chunk_size
            if remaining > 0:
                progress_callback(remaining)

        return True
    except Exception:
        return False


def _resolve_path_from_cache(file_name, parents, folder_cache, dest_path):
    """Resolve local filesystem path using cached folder paths. Returns None if unresolvable."""
    if not parents:
        return os.path.join(dest_path, sanitize_filename(file_name))
    for p in parents:
        if p in folder_cache:
            return os.path.join(folder_cache[p], sanitize_filename(file_name))
    return None


def _resolve_path_via_api(file_name, parents, folder_cache, dest_path, service):
    """Walk up the parent chain via API calls to resolve a full local path."""
    parent_id = parents[0] if parents else None
    if not parent_id:
        return os.path.join(dest_path, sanitize_filename(file_name))

    current_id = parent_id
    folder_names = []
    visited = set()

    while current_id and current_id not in visited:
        visited.add(current_id)
        if current_id in folder_cache:
            return os.path.join(
                folder_cache[current_id],
                *reversed(folder_names),
                sanitize_filename(file_name)
            )
        try:
            info = service.files().get(
                fileId=current_id,
                fields='id,name,parents',
                supportsAllDrives=True
            ).execute()
            folder_name = sanitize_filename(info.get('name', 'unnamed'))
            folder_names.append(folder_name)
            # Cache this folder if its parent is known
            grand_parents = info.get('parents', [])
            if grand_parents and grand_parents[0] in folder_cache:
                folder_cache[current_id] = os.path.join(
                    folder_cache[grand_parents[0]], folder_name
                )
            current_id = grand_parents[0] if grand_parents else None
        except Exception:
            return None

    if folder_names:
        return os.path.join(dest_path, *reversed(folder_names), sanitize_filename(file_name))
    return os.path.join(dest_path, sanitize_filename(file_name))


def _resolve_path(file_name, parents, folder_cache, dest_path, service=None):
    """Resolve local path, falling back to API resolution if cache miss."""
    path = _resolve_path_from_cache(file_name, parents, folder_cache, dest_path)
    if path is not None:
        return path
    if service is not None:
        return _resolve_path_via_api(file_name, parents, folder_cache, dest_path, service)
    return None


def _compare_local_file(file_name, file_path, mime_type, item, tui=None):
    """
    Compare a remote Drive item with its local copy.
    Returns (should_download, final_file_path, est_size, remote_mtime_epoch, skip_reason).
    skip_reason is None if should_download=True, else a (name, reason) tuple.
    """
    final_file_path = file_path
    is_gsuite = mime_type.startswith('application/vnd.google-apps.')

    if is_gsuite:
        if mime_type in EXPORT_MAPPING:
            _, ext = EXPORT_MAPPING[mime_type]
            if not final_file_path.lower().endswith(ext):
                final_file_path += ext
        else:
            return False, final_file_path, 0, None, (file_name, f"Unsupported GSuite format: {mime_type}")

    remote_mtime = parse_iso_datetime(item.get('modifiedTime'))
    remote_mtime_epoch = remote_mtime.timestamp() if remote_mtime else None
    raw_size = item.get('size')
    est_size = estimate_file_size(mime_type, raw_size)

    if not os.path.exists(final_file_path):
        return True, final_file_path, est_size, remote_mtime_epoch, None

    local_size = os.path.getsize(final_file_path)
    local_mtime_epoch = os.path.getmtime(final_file_path)
    local_mtime = datetime.fromtimestamp(local_mtime_epoch, timezone.utc)

    if is_gsuite:
        if remote_mtime and remote_mtime > local_mtime:
            return True, final_file_path, est_size, remote_mtime_epoch, None
    else:
        remote_size = est_size
        size_differs = local_size != remote_size
        mtime_differs = remote_mtime and abs((remote_mtime - local_mtime).total_seconds()) > 1.0
        if size_differs or mtime_differs:
            if tui:
                tui.add_log(
                    f"Diff: {file_name} | size_diff={size_differs} ({local_size} vs {remote_size}) | "
                    f"mtime_diff={mtime_differs} ({local_mtime.strftime('%H:%M:%S')} vs {remote_mtime.strftime('%H:%M:%S')})",
                    style="yellow"
                )
            return True, final_file_path, est_size, remote_mtime_epoch, None

    return False, final_file_path, 0, None, (file_name, "Already up-to-date")


def _list_folder_page(service, folder_id, page_token=None):
    """List files in a single folder. Returns (items, next_page_token)."""
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        spaces='drive',
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
        pageToken=page_token
    ).execute()
    return results.get('files', []), results.get('nextPageToken')


def get_changes_since(service, start_page_token, cache, tui=None, service_factory=None):
    """
    Use the Drive Changes API for true incremental sync.
    Only downloads files that have changed since the last sync.
    """
    dest_path = cache.get("dest_path")
    folder_id = cache.get("folder_id")
    folder_cache = cache.setdefault("folder_cache", {})

    if not folder_id or not dest_path:
        return [], [], start_page_token

    tasks = []
    skips = []
    new_folder_ids = {}  # folder_id -> resolved local path

    page_token = start_page_token
    new_start_page_token = start_page_token

    if tui:
        tui.add_log("Querying Drive Changes API for incremental updates...", style="blue")

    while True:
        try:
            response = service.changes().list(
                pageToken=page_token,
                spaces='drive',
                fields='changes(fileId,file(id,name,mimeType,modifiedTime,size,parents,trashed),removed),'
                       'nextPageToken,newStartPageToken',
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                includeRemoved=True,
            ).execute()
        except Exception as e:
            if tui:
                tui.add_log(f"Changes API call failed: {e}", style="red")
            # Fall back by raising so caller retries with full scan
            raise

        new_start_page_token = response.get('newStartPageToken', start_page_token)

        # Two-pass: first collect folder changes to update cache, then process files
        folder_changes = []
        file_changes = []

        for change in response.get('changes', []):
            file_data = change.get('file')
            if not file_data:
                continue
            if change.get('removed') or file_data.get('trashed'):
                continue

            file_id = file_data['id']
            mime_type = file_data.get('mimeType', '')
            parents = file_data.get('parents', [])

            in_tree = folder_id in parents or any(p in folder_cache for p in parents)
            if not in_tree:
                continue

            if mime_type == 'application/vnd.google-apps.folder':
                folder_changes.append((file_id, file_data))
            else:
                file_changes.append((file_id, file_data))

        # Pass 1: resolve folder paths, track new folders
        for f_id, f_data in folder_changes:
            name = f_data.get('name', 'unnamed')
            parents_p = f_data.get('parents', [])
            local_path = _resolve_path(name, parents_p, folder_cache, dest_path, service)
            if local_path is None:
                if tui:
                    tui.add_log(f"Skipping folder: unable to resolve path for {name}", style="yellow")
                continue
            folder_cache[f_id] = local_path
            if f_id not in new_folder_ids:
                new_folder_ids[f_id] = local_path

        # Pass 2: diff files against local copies
        for f_id, f_data in file_changes:
            name = f_data.get('name', 'unnamed')
            mime = f_data.get('mimeType', '')
            parents_p = f_data.get('parents', [])
            local_path = _resolve_path(name, parents_p, folder_cache, dest_path, service)
            if local_path is None:
                skips.append((name, "Unable to resolve local path"))
                continue

            should_dl, final_path, est_size, mtime_epoch, skip = _compare_local_file(
                name, local_path, mime, f_data, tui
            )
            if should_dl:
                tasks.append((f_id, final_path, mime, est_size, mtime_epoch))
            elif skip:
                skips.append(skip)

        page_token = response.get('nextPageToken')
        if not page_token:
            break

    # Traverse any newly discovered folders to find their contents
    # (handles folders moved into the watched tree where internal files
    #  wouldn't show up as individual changes in the Changes API)
    if new_folder_ids:
        if tui:
            tui.add_log(f"Scanning {len(new_folder_ids)} new folder(s) discovered via changes...", style="blue")
        new_tasks, new_skips = _traverse_folders(
            service, new_folder_ids, folder_cache, cache,
            tui, service_factory
        )
        tasks.extend(new_tasks)
        skips.extend(new_skips)

    cache["start_page_token"] = new_start_page_token
    return tasks, skips, new_start_page_token


def _traverse_folders(service, new_folder_ids, folder_cache, cache, tui=None, service_factory=None):
    """Breadth-first traversal of newly discovered folders to find all files within."""
    import threading

    tasks = []
    skips = []
    task_lock = threading.Lock()
    skip_lock = threading.Lock()

    dest_path = cache.get("dest_path")
    queue = list(new_folder_ids.items())  # [(folder_id, local_path), ...]
    visited = set(folder_id for folder_id, _ in queue)

    for folder_id, local_path in queue:
        if tui:
            folder_name = os.path.basename(local_path)
            tui.add_log(f"Traversing: {folder_name}", style="dim cyan")

        os.makedirs(local_path, exist_ok=True)

        page_token = None
        while True:
            try:
                items, page_token = _list_folder_page(service, folder_id, page_token)
            except Exception as e:
                if tui:
                    tui.add_log(f"Failed to list folder {folder_id}: {e}", style="red")
                break

            for item in items:
                mime_type = item.get('mimeType')
                item_name = item.get('name', 'unnamed')
                sanitized_name = sanitize_filename(item_name, platform="windows")
                file_path = os.path.join(local_path, sanitized_name)

                if mime_type == 'application/vnd.google-apps.folder':
                    if item['id'] not in visited:
                        visited.add(item['id'])
                        folder_cache[item['id']] = file_path
                        queue.append((item['id'], file_path))
                else:
                    should_dl, final_path, est_size, mtime_epoch, skip = _compare_local_file(
                        item_name, file_path, mime_type, item, tui
                    )
                    if should_dl:
                        with task_lock:
                            tasks.append((item['id'], final_path, mime_type, est_size, mtime_epoch))
                    elif skip:
                        with skip_lock:
                            skips.append(skip)

            if not page_token:
                break

    return tasks, skips


def traverse_and_download(service, folder_id, dest_path, last_run_dt, cache,
                          filter_by_date=False, tui=None, creds=None,
                          service_factory=None):
    """
    Recursive traversal using ThreadPoolExecutor for parallel folder scanning.
    Use service_factory to supply thread-safe service instances; falls back to
    get_service(creds) or the passed service.
    """
    import concurrent.futures
    import threading
    from src.auth import get_service, Credentials

    if service_factory is None:
        if creds is None and os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file(
                'token.json', ['https://www.googleapis.com/auth/drive']
            )
        if creds is not None:
            service_factory = lambda c=creds: get_service(c)
        else:
            # If no creds and no factory, use the passed service directly
            service_factory = lambda: service

    dest_path = sanitize_filepath(dest_path, platform="windows")
    path_parts = [p.strip() for p in Path(dest_path).parts]
    dest_path = os.path.join(*path_parts)

    os.makedirs(dest_path, exist_ok=True)

    tasks = []
    skips = []

    visited = set([folder_id])
    visited_lock = threading.Lock()

    folder_paths = {folder_id: dest_path}
    folder_paths_lock = threading.Lock()

    tasks_lock = threading.Lock()
    skips_lock = threading.Lock()

    max_workers = 10

    def process_folder(curr_folder_id, curr_local_dir):
        thread_service = service_factory()

        os.makedirs(curr_local_dir, exist_ok=True)

        discovered_subfolders = []
        page_token = None

        while True:
            try:
                items, page_token = _list_folder_page(thread_service, curr_folder_id, page_token)
            except Exception as e:
                if tui:
                    tui.add_log(f"Failed to list folder children for {curr_folder_id}: {e}", style="red")
                raise

            for item in items:
                mime_type = item.get('mimeType')
                item_name = item.get('name', 'unnamed')
                sanitized_name = sanitize_filename(item_name, platform="windows")
                file_path = os.path.join(curr_local_dir, sanitized_name)

                if mime_type == 'application/vnd.google-apps.folder':
                    with visited_lock:
                        if item['id'] not in visited:
                            visited.add(item['id'])
                            with folder_paths_lock:
                                folder_paths[item['id']] = file_path
                            discovered_subfolders.append((item['id'], file_path))
                else:
                    should_dl, final_path, est_size, mtime_epoch, skip = _compare_local_file(
                        item_name, file_path, mime_type, item, tui
                    )
                    if should_dl:
                        with tasks_lock:
                            tasks.append((item['id'], final_path, mime_type, est_size, mtime_epoch))
                    elif skip:
                        with skips_lock:
                            skips.append(skip)

            if not page_token:
                break

        return discovered_subfolders

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_folder, folder_id, dest_path): (folder_id, dest_path)}

        while futures:
            done, _ = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            next_futures = {}
            for future in done:
                try:
                    subfolders = future.result()
                    for sf_id, sf_path in subfolders:
                        f = executor.submit(process_folder, sf_id, sf_path)
                        next_futures[f] = (sf_id, sf_path)
                except Exception as e:
                    if tui:
                        tui.add_log(f"Parallel traverse error: {e}", style="bold red")
                del futures[future]
            futures.update(next_futures)

    with folder_paths_lock:
        cache["folder_cache"].update(folder_paths)

    return tasks, skips

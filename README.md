# Google Drive Sync DC Folder

An advanced, high-performance command-line utility to recursively synchronize a Google Drive folder to a local directory. Built with Python, modern concurrent features, and interactive CLI progress visualization.

---

## 🚀 Key Features

* **⚡ Lightning-Fast Changetracking (Folder Caching)**: Replaces traditional slow, recursive DFS/BFS API traversals with an advanced folder caching architecture. By storing directory IDs locally, the script retrieves updates in a **single Google Drive API query**, cutting subsequent check times from **15 minutes down to under 2 seconds**.
* **Interactive bruhanimate dashboard**: Resizable panels for session details, byte/file progress, transfer speed, activity, and file results. Keyboard focus, scrolling, and status filters keep long syncs readable.
* **📂 Google Workspace Exports**: Automatically detects Google Workspace files (Docs, Sheets, Slides, Drawings) and exports them on-the-fly to standard formats (`.docx`, `.xlsx`, `.pptx`, `.png`).
* **🧵 Thread-Safe Concurrency**: Uses a multithreaded `ThreadPoolExecutor` (configured up to 10 workers) with thread-local API client storage to download files in parallel with absolute thread safety.
* **🛡️ Windows Path Sanitization**: Automatically strips or replaces invalid Windows path characters using `pathvalidate`, protecting against path traversals or file system crashes.
* **🔧 Self-Healing Engine**: If a new folder is created deep inside the synced folder, the script automatically resolves its hierarchy on Google Drive, maps it to the local tree, and updates the local cache without user intervention.
* **💻 Interactive Onboarding**: No hardcoded paths! On first run, the script prompts the user to enter their parameters or accepts them via clean CLI configuration flags, storing them safely in a single cache file.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
* Python 3.13+ (required by bruhanimate 1.2.0)
* [Poetry](https://python-poetry.org/) (Python package manager)

### 2. Install Dependencies
Clone this repository, navigate to the folder, and run:
```bash
poetry install
```

The app uses the published `bruhanimate==1.2.0` package. `poetry.lock` pins the
environment; Textual and Rich are no longer dependencies. To remove packages left
over from an older environment, run `poetry sync`.

Preview the dashboard without credentials, network requests, cache access, or
writing destination files:

```bash
poetry run python sync.py --demo
```

For a preview that exits automatically, add `--frames 120` (about four seconds).
Use a terminal such as Windows Terminal; resize it while the preview is running.

### 3. Google API Credentials Setup
To allow the script to connect to Google Drive:
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project and enable the **Google Drive API**.
3. Create an **OAuth 2.0 Client ID** (Desktop Application type).
4. Download the credentials JSON file and rename it to **`credentials.json`**.
5. Place **`credentials.json`** in the root directory of this project.

On your first run, the script will open your web browser to authorize access. It will then securely save your authorization token as **`token.json`** in the project root so you don't have to authenticate again.

Configuration prompts and authentication finish before the dashboard opens.

---

## 📖 Usage Guide

### 1. Configure and Run (Onboarding)
There is **no need to edit the python files**. You can configure your settings interactively or via CLI flags:

* **Interactive Setup (Recommended)**: Simply run:
  ```bash
  poetry run python sync.py
  ```
  If it's your first run, the script will guide you through entering your **Google Drive Folder ID** and local **Destination Path** right in the console!

* **CLI Setup**: Alternatively, you can configure or update your settings directly using flags:
  ```bash
  poetry run python sync.py --folder-id "YOUR_DRIVE_FOLDER_ID" --dest "C:\path\to\transcriptions"
  ```

### 2. First Sync (Build Cache)
Your first sync must traverse the Drive once to build your local folder ID path mapping:
```bash
poetry run python sync.py --full
```
*(This will take a few minutes depending on folder size—but is the **last time** you'll ever wait!)*

### 3. Instant Incremental Sync (Default)
On all subsequent runs, simply execute the command normally. It updates in **1–3 seconds**:
```bash
poetry run python sync.py
```

### 4. Force File Integrity Check
If you delete local files and want the script to check Google Drive and download missing local files, force a full scan:
```bash
poetry run python sync.py --full
```

---

## 🗃️ Under the Hood: Unified State File (`sync_cache.json`)

The app stores **`sync_cache.json`** under `%APPDATA%/SyncDCIFolder` on Windows or
`~/.config/SyncDCIFolder` elsewhere. A legacy project-root cache is read first when
present. `--cache-file PATH` explicitly selects a different cache. It tracks:
* **`folder_id`**: Your configured Google Drive Folder ID.
* **`dest_path`**: Your configured local destination path.
* **`last_run`**: The precise ISO-8601 UTC timestamp of your last sync.
* **`folder_cache`**: A dictionary mapping Google Drive Folder IDs to their corresponding local directory paths.
* **`history`**: Chronological duration and size details of your last 10 downloads, allowing the script to calculate your historical average download speed to **estimate the exact time remaining** before a sync starts.

## Dashboard controls

| Key | Action |
| --- | --- |
| Tab / Shift+Tab | Focus activity, files, or session details |
| Up / Down, Page Up / Page Down | Scroll the focused pane |
| Left / Right | Scroll long file paths or session details horizontally |
| Home / End | Jump to the start/end; End resumes following new log/file rows |
| f | Cycle files: all, downloaded, skipped, failed, dry run |
| y (files pane) | Show the top visible file's details in the activity log |
| q / Escape / Ctrl+C | Request stop, or exit after completion |

Scrolling away from the end pauses automatic following. Resize preserves sync
progress, events, and filters. Very small terminals clip panes; enlarge the
terminal to see them again. The dashboard initially focuses the activity pane.

The activity log retains 1,000 lines. Files shows the latest 1,000 matching
events; filtering searches the complete event list. Full results remain in the
TXT/CSV manifests written to the destination. The dashboard stays open after
completion so you can inspect the results. `y` inspects a row; it does not copy
to the clipboard.

Quitting requests cooperative cancellation. Scanning and already active network
requests may take time to finish; queued downloads stop before starting. The app
joins its worker before closing. Cancelled runs, dry runs, and runs with failed
downloads do not advance the sync checkpoint, so outstanding changes can be
retried. `--dry-run` still queries Drive and writes manifests; use `--demo` for
a preview with no external effects.

Control characters, wide glyphs, and combining marks in displayed paths are
replaced with `?` to fit bruhanimate's single-cell rendering model. Actual paths
and manifest data are preserved.

## Development

`src/tui.py` implements a typed bruhanimate animation using `Panel`, `Row`,
`Column`, `Label`, `StatusBar`, `ScrollView`, `LogView`, and `FocusGroup`. The sync
and download workers enqueue typed messages; the presentation thread applies
them during `update()`. `draw()` only renders. `close()` cancels and joins the
worker before releasing the layout.

```bash
poetry run python -m unittest discover -v
```

Tests use headless buffers and mocked Drive services. Cache/manifests tests use
temporary directories. They do not authenticate or sync your real files; native
terminal interaction can be checked separately with `--demo`.

# Google Drive Sync DC Folder

An advanced, high-performance command-line utility to recursively synchronize a Google Drive folder to a local directory. Built with Python, modern concurrent features, and interactive CLI progress visualization.

---

## 🚀 Key Features

* **⚡ Lightning-Fast Changetracking (Folder Caching)**: Replaces traditional slow, recursive DFS/BFS API traversals with an advanced folder caching architecture. By storing directory IDs locally, the script retrieves updates in a **single Google Drive API query**, cutting subsequent check times from **15 minutes down to under 2 seconds**.
* **✨ Beautiful Rich CLI**: Powered by the `rich` library. Offers real-time directory traversal spinners, clean tabular skipped summaries, dual progress bars tracking both total download size (bytes) and file count concurrently, and elegant final report panels.
* **📂 Google Workspace Exports**: Automatically detects Google Workspace files (Docs, Sheets, Slides, Drawings) and exports them on-the-fly to standard formats (`.docx`, `.xlsx`, `.pptx`, `.png`).
* **🧵 Thread-Safe Concurrency**: Uses a multithreaded `ThreadPoolExecutor` (configured up to 10 workers) with thread-local API client storage to download files in parallel with absolute thread safety.
* **🛡️ Windows Path Sanitization**: Automatically strips or replaces invalid Windows path characters using `pathvalidate`, protecting against path traversals or file system crashes.
* **🔧 Self-Healing Engine**: If a new folder is created deep inside the synced folder, the script automatically resolves its hierarchy on Google Drive, maps it to the local tree, and updates the local cache without user intervention.
* **💻 Interactive Onboarding**: No hardcoded paths! On first run, the script prompts the user to enter their parameters or accepts them via clean CLI configuration flags, storing them safely in a single cache file.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
* Python 3.11+
* [Poetry](https://python-poetry.org/) (Python package manager)

### 2. Install Dependencies
Clone this repository, navigate to the folder, and run:
```bash
poetry install
```

### 3. Google API Credentials Setup
To allow the script to connect to Google Drive:
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project and enable the **Google Drive API**.
3. Create an **OAuth 2.0 Client ID** (Desktop Application type).
4. Download the credentials JSON file and rename it to **`credentials.json`**.
5. Place **`credentials.json`** in the root directory of this project.

On your first run, the script will open your web browser to authorize access. It will then securely save your authorization token as **`token.json`** in the project root so you don't have to authenticate again.

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

The project consolidates configurations, state, and metrics into a single, clean **`sync_cache.json`** file in your project root. It tracks:
* **`folder_id`**: Your configured Google Drive Folder ID.
* **`dest_path`**: Your configured local destination path.
* **`last_run`**: The precise ISO-8601 UTC timestamp of your last sync.
* **`folder_cache`**: A dictionary mapping Google Drive Folder IDs to their corresponding local directory paths.
* **`history`**: Chronological duration and size details of your last 10 downloads, allowing the script to calculate your historical average download speed to **estimate the exact time remaining** before a sync starts.

import os
from unittest.mock import patch
import src.cache as cache_mod
import unittest
import tempfile
import shutil
from datetime import timezone

from src.drive_sync import parse_iso_datetime, estimate_file_size
from src.cache import load_cache, save_cache, update_cache_history
from src.models import (
    DownloadEvent, EventStatus, SyncMode, generate_manifests, format_size,
)


class TestSyncFolder(unittest.TestCase):
    def test_parse_iso_datetime(self):
        dt = parse_iso_datetime("2026-06-23T12:34:56Z")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 6)
        self.assertEqual(dt.day, 23)
        self.assertEqual(dt.hour, 12)
        self.assertEqual(dt.minute, 34)
        self.assertEqual(dt.second, 56)
        self.assertEqual(dt.tzinfo, timezone.utc)

        dt2 = parse_iso_datetime("2026-06-23T12:34:56+00:00")
        self.assertEqual(dt2, dt)

    def test_estimate_file_size(self):
        self.assertEqual(estimate_file_size("application/vnd.google-apps.document", None), 100 * 1024)
        self.assertEqual(estimate_file_size("application/vnd.google-apps.spreadsheet", None), 150 * 1024)
        self.assertEqual(estimate_file_size(None, "5000"), 5000)
        self.assertEqual(estimate_file_size(None, "invalid"), 10 * 1024)

    def test_cache_operations(self):
        """Cache tests never touch the user's configured or legacy cache."""
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            cache_mod, _APP_DIR=directory,
            CACHE_FILE=os.path.join(directory, "cache.json"),
            _LEGACY_CACHE=os.path.join(directory, "legacy.json"),
        ):
            cache = load_cache()
            self.assertIsNone(cache["last_run"])
            self.assertEqual(cache["history"], [])

            cache["folder_id"] = "test-folder"
            save_cache(cache)

            reloaded = load_cache()
            self.assertEqual(reloaded["folder_id"], "test-folder")

            update_cache_history(reloaded, duration=10.0, bytes_downloaded=1048576, files_downloaded=1)
            final_cache = load_cache()
            self.assertIsNotNone(final_cache["last_run"])
            self.assertEqual(len(final_cache["history"]), 1)
            self.assertEqual(final_cache["history"][0]["bytes_downloaded"], 1048576)
            self.assertIsNotNone(final_cache["average_speed_bps"])


    def test_traverse_and_download(self):
        from unittest.mock import MagicMock
        from src.drive_sync import traverse_and_download

        service = MagicMock()

        def mock_list_execute(q, **kwargs):
            if "root-folder-id" in q:
                return {
                    "files": [
                        {"id": "subfolder-id", "name": "subfolder",
                         "mimeType": "application/vnd.google-apps.folder"},
                        {"id": "file1-id", "name": "file1.txt",
                         "mimeType": "text/plain", "size": "500",
                         "modifiedTime": "2026-06-23T12:00:00Z"},
                    ],
                    "nextPageToken": None,
                }
            elif "subfolder-id" in q:
                return {
                    "files": [
                        {"id": "file2-id", "name": "file2.txt",
                         "mimeType": "text/plain", "size": "1000",
                         "modifiedTime": "2026-06-23T12:00:00Z"},
                    ],
                    "nextPageToken": None,
                }
            return {"files": [], "nextPageToken": None}

        def mock_list(q, **kwargs):
            mock_call = MagicMock()
            mock_call.execute.side_effect = lambda: mock_list_execute(q, **kwargs)
            return mock_call

        service.files().list.side_effect = mock_list

        temp_dir = tempfile.mkdtemp()

        try:
            cache = {
                "folder_cache": {},
                "folder_id": "root-folder-id",
                "dest_path": temp_dir,
                "start_page_token": None,
            }

            tasks, skips = traverse_and_download(
                service, "root-folder-id", temp_dir, None, cache,
                service_factory=lambda: service,
            )

            self.assertEqual(len(tasks), 2)

            task_dict = {t[0]: t for t in tasks}
            self.assertIn("file1-id", task_dict)
            self.assertIn("file2-id", task_dict)
            self.assertEqual(task_dict["file1-id"][1], os.path.join(temp_dir, "file1.txt"))
            self.assertEqual(
                task_dict["file2-id"][1],
                os.path.join(temp_dir, "subfolder", "file2.txt"),
            )
            self.assertEqual(cache["folder_cache"]["root-folder-id"], temp_dir)
            self.assertEqual(
                cache["folder_cache"]["subfolder-id"],
                os.path.join(temp_dir, "subfolder"),
            )

        finally:
            shutil.rmtree(temp_dir)


class TestModels(unittest.TestCase):
    """Unit tests for DownloadEvent / SyncMode / manifest generation."""

    def test_event_status_values(self):
        self.assertEqual(EventStatus.DOWNLOADED.value, "downloaded")
        self.assertEqual(EventStatus.SKIPPED.value, "skipped")
        self.assertEqual(EventStatus.FAILED.value, "failed")
        self.assertEqual(EventStatus.DRY_RUN.value, "dry_run")

    def test_sync_mode_values(self):
        self.assertEqual(SyncMode.INCREMENTAL.value, "incremental")
        self.assertEqual(SyncMode.FULL_REQUESTED.value, "full_requested")
        self.assertEqual(SyncMode.FULL_MISSING_CACHE.value, "full_missing_cache")
        self.assertEqual(SyncMode.FULL_CHANGES_API_FALLBACK.value, "full_changes_api_fallback")
        self.assertEqual(SyncMode.FULL_INITIAL.value, "full_initial")

    def test_format_size(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(500), "500 B")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(1048576), "1.0 MB")
        self.assertEqual(format_size(1073741824), "1.0 GB")

    def test_download_event_defaults(self):
        ev = DownloadEvent(relative_path="a/b.txt", status=EventStatus.DOWNLOADED)
        self.assertEqual(ev.size, 0)
        self.assertEqual(ev.completion_time, "")
        self.assertEqual(ev.reason, "")

    def test_generate_manifests_txt_and_csv(self):
        events = [
            DownloadEvent(relative_path="file1.txt", status=EventStatus.DOWNLOADED,
                          size=1024, completion_time="12:00:00"),
            DownloadEvent(relative_path="sub\\file2.docx", status=EventStatus.SKIPPED,
                          reason="Already up-to-date"),
            DownloadEvent(relative_path="file3.pdf", status=EventStatus.FAILED,
                          size=2048, completion_time="12:01:00"),
            DownloadEvent(relative_path="file4.png", status=EventStatus.DRY_RUN,
                          size=512),
        ]

        temp_dir = tempfile.mkdtemp()
        try:
            summary = {
                "downloaded": 1, "skipped": 1, "failed": 1,
                "duration": 5.5, "total_bytes": 3584,
                "sync_reason": "Incremental (Changes API)",
            }
            txt_path, csv_path = generate_manifests(
                events, temp_dir, SyncMode.INCREMENTAL, summary,
            )

            self.assertTrue(os.path.exists(txt_path))
            self.assertTrue(os.path.exists(csv_path))
            self.assertTrue(txt_path.endswith(".txt"))
            self.assertTrue(csv_path.endswith(".csv"))

            with open(txt_path, "r", encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("=== Google Drive Sync Manifest ===", txt)
            self.assertIn("Sync Mode: Incremental", txt)
            self.assertIn("[OK] file1.txt @ 12:00:00 1.0 KB", txt)
            self.assertIn("[SKIP] sub\\file2.docx (Already up-to-date)", txt)
            self.assertIn("[FAIL] file3.pdf @ 12:01:00 2.0 KB", txt)
            self.assertIn("[DRY RUN] file4.png 512 B", txt)

            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                import csv
                reader = list(csv.reader(f))
            self.assertEqual(reader[0], ["status", "relative_path", "size_bytes", "reason", "completion_time"])
            self.assertEqual(reader[1], ["downloaded", "file1.txt", "1024", "", "12:00:00"])
            self.assertEqual(reader[2], ["skipped", "sub\\file2.docx", "0", "Already up-to-date", ""])
            self.assertEqual(reader[3], ["failed", "file3.pdf", "2048", "", "12:01:00"])
            self.assertEqual(reader[4], ["dry_run", "file4.png", "512", "", ""])

        finally:
            shutil.rmtree(temp_dir)

    def test_manifest_empty_events(self):
        temp_dir = tempfile.mkdtemp()
        try:
            txt_path, csv_path = generate_manifests(
                [], temp_dir, SyncMode.FULL_INITIAL,
                {"downloaded": 0, "skipped": 0, "failed": 0, "duration": 0, "total_bytes": 0},
            )
            self.assertTrue(os.path.exists(txt_path))
            self.assertTrue(os.path.exists(csv_path))
        finally:
            shutil.rmtree(temp_dir)

    def test_filters_applied_to_events(self):
        """Ensure that filtering by status only yields matching events."""
        events = [
            DownloadEvent("a", EventStatus.DOWNLOADED),
            DownloadEvent("b", EventStatus.SKIPPED),
            DownloadEvent("c", EventStatus.DOWNLOADED),
            DownloadEvent("d", EventStatus.FAILED),
        ]

        downloaded = [e for e in events if e.status == EventStatus.DOWNLOADED]
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(downloaded[0].relative_path, "a")
        self.assertEqual(downloaded[1].relative_path, "c")

        skipped = [e for e in events if e.status == EventStatus.SKIPPED]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].relative_path, "b")


if __name__ == "__main__":
    unittest.main()
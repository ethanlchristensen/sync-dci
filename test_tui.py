"""Headless dashboard and sync integration tests; no credentials or network."""

import threading
import unittest
from unittest.mock import MagicMock, patch

from bruhanimate import Buffer, Screen

import sync
from src.models import DownloadEvent, EventStatus
from src.tui import (
    SyncApp,
    TUIManager,
    _FileDone,
    _InitProgress,
    _Log,
    _ProgressBytes,
    _Speed,
    _Status,
    _SyncComplete,
    _SyncEvent,
    run_demo,
    display_text,
    progress_bar,
)


class DashboardTests(unittest.TestCase):
    def test_demo_plays_headlessly_and_joins_its_worker(self):
        screen = MagicMock()
        screen.width, screen.height = 100, 28
        screen.has_resized.return_value = False
        screen.get_event.return_value = None
        with patch.object(Screen, "show", side_effect=lambda run: run(screen)):
            self.assertEqual(run_demo(frames=3), 3)
        self.assertEqual(screen.flush_frame.call_count, 3)
        self.assertTrue(screen.print_at.called)
        self.assertFalse(any(t.name == "drive-sync" for t in threading.enumerate()))

    def app(self, runner=None):
        app = SyncApp("folder", "destination", sync_func=runner)
        self.addCleanup(app.close)
        app.build(Buffer(28, 100))
        app.start()
        return app

    def test_construction_does_not_start_worker(self):
        runner = MagicMock()
        app = SyncApp("folder", "destination", sync_func=runner)
        app.close()
        app.close()
        runner.assert_not_called()

    def test_messages_wait_for_update_and_draw_is_pure(self):
        app = self.app()
        for message in (
            _InitProgress(2, 100),
            _ProgressBytes(25),
            _FileDone(),
            _Speed(5),
            _Status("Downloading"),
            _Log("hello"),
        ):
            app.post_message(message)
        buffer = Buffer(28, 100)
        app.draw(buffer)
        self.assertEqual(app.bytes_done, 0)
        app.update(0.1)
        self.assertEqual(
            (app.files_done, app.bytes_done, app.current_speed), (1, 25, 5)
        )
        self.assertEqual(app.status, "Downloading")
        app.draw(buffer)
        self.assertEqual(app.bytes_done, 25)

    def test_focus_filter_inspection_and_resize_preserve_state(self):
        app = self.app()
        self.assertEqual(app.focus.focused.name, "activity")
        app.handle_event(9)
        self.assertEqual(app.focus.focused.name, "files")
        events = [
            DownloadEvent("alpha", EventStatus.DOWNLOADED),
            DownloadEvent("beta", EventStatus.FAILED),
        ]
        for event in events:
            app.post_message(_SyncEvent(event))
        app.post_message(_ProgressBytes(25))
        app.update(0.1)
        app.handle_event(ord("f"))
        self.assertEqual(app._visible_events, events[:1])
        app.handle_event(ord("y"))
        app.handle_event(-302)
        self.assertEqual(app.focus.focused.name, "activity")
        for width, height in ((50, 15), (1, 1), (5, 3), (100, 28)):
            app.resize((width, height))
            app.update(0.1)
            app.draw(Buffer(height, width))
        self.assertEqual(app.download_events, events)
        self.assertEqual(app.bytes_done, 25)
        self.assertEqual(app.filter_status, EventStatus.DOWNLOADED)

    def test_log_history_stays_paused_until_end(self):
        app = self.app()
        for index in range(50):
            app.post_message(_Log(f"line {index}"))
        app.update(0.1)
        app.handle_event(-200)  # Home
        self.assertFalse(app.log.follow_tail)
        offset = app.log.offset
        app.post_message(_Log("new line"))
        app.update(0.1)
        self.assertEqual(app.log.offset, offset)
        app.handle_event(-201)  # End
        self.assertTrue(app.log.follow_tail)

    def test_file_retention_filter_and_empty_inspection(self):
        app = self.app()
        for index in range(1005):
            app.post_message(
                _SyncEvent(DownloadEvent(str(index), EventStatus.DOWNLOADED))
            )
        for _ in range(3):
            app.update(0.1)
        self.assertEqual(len(app._visible_events), 1000)
        self.assertEqual(app._visible_events[0].relative_path, "5")
        app.cycle_filter()
        app.cycle_filter()  # skipped: empty
        self.assertEqual(app._visible_events, [])
        app.inspect_top()

    def test_text_is_safe_for_single_cell_rendering(self):
        app = self.app()
        text = (
            "wide \u96ea emoji \U0001f600 mark e\u0301 variation \ufe0f \x1b[2J\nline"
        )
        app.post_message(_Log(text))
        app.post_message(_SyncEvent(DownloadEvent(text, EventStatus.DOWNLOADED)))
        app.update(0.1)
        app.draw(Buffer(28, 100))
        self.assertNotIn("\x1b", display_text(text))
        self.assertEqual(app.download_events[0].relative_path, text)

    def test_quit_waits_for_cooperative_worker_and_close_joins(self):
        entered, release = threading.Event(), threading.Event()

        def runner(app):
            entered.set()
            release.wait(3)

        app = self.app(runner)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        self.assertFalse(app.handle_event(ord("q")))
        self.assertTrue(app.stop_requested)
        release.set()
        app._thread.join(2)
        app.update(0.1)
        self.assertTrue(app.finished)
        app.close()
        self.assertFalse(app._thread.is_alive())

    def test_worker_failure_is_visible(self):
        def runner(app):
            raise RuntimeError("example failure")

        app = self.app(runner)
        app._thread.join(2)
        app.update(0.1)
        self.assertEqual(app.status, "Failed")
        self.assertTrue(app.handle_event(ord("q")))

    def test_completion_stays_open_for_review(self):
        app = self.app()
        app.post_message(_SyncComplete(downloaded=2, duration=1.5))
        app.update(0.1)
        self.assertEqual(app.status, "Completed")
        self.assertFalse(app.finished)
        self.assertTrue(app.handle_event(27))

    def test_progress_handles_unknown_and_overflow(self):
        self.assertTrue(progress_bar(0, 0).endswith("0%"))
        self.assertTrue(progress_bar(20, 10).endswith("100%"))

    def test_partial_start_failure_closes_widgets(self):
        app = SyncApp("folder", "destination")
        app.build(Buffer(28, 100))
        with patch.object(app._layout, "start", side_effect=RuntimeError("start")):
            with self.assertRaisesRegex(RuntimeError, "start"):
                app.start()
        self.assertTrue(app._closed)
        app.close()


class CliTests(unittest.TestCase):
    def test_demo_bypasses_cache_auth_and_prompts(self):
        with (
            patch("sys.argv", ["sync.py", "--demo", "--frames", "3"]),
            patch.object(sync, "load_cache") as cache,
            patch.object(sync, "authenticate") as auth,
            patch.object(sync, "run_demo") as demo,
            patch("builtins.input") as prompt,
        ):
            self.assertEqual(sync.main(), 0)
        demo.assert_called_once_with(frames=3)
        cache.assert_not_called()
        auth.assert_not_called()
        prompt.assert_not_called()

    def run_sync(
        self,
        *,
        dry_run=False,
        cancel=False,
        fail=False,
        cancel_download=False,
        worker_error=False,
    ):
        cache = {
            "folder_id": "folder",
            "dest_path": "fake-destination",
            "folder_cache": {"folder": "fake-destination"},
            "last_run": "2026-01-01",
            "start_page_token": "old",
        }
        manager = TUIManager("folder", "fake-destination")
        app = manager.app
        self.addCleanup(app.close)
        tasks = [("id", "fake-destination/file.txt", "text/plain", 100)]

        def run_headless():
            app.build(Buffer(28, 100))
            app.start()
            app._thread.join(3)
            self.assertFalse(app._thread.is_alive())
            app.update(0.1)

        def scan(*args, **kwargs):
            if cancel:
                app.cancel_event.set()
            return tasks, [], "new"

        def download(*args):
            if cancel_download:
                app.cancel_event.set()
            if worker_error:
                raise RuntimeError("worker failure")
            args[-1](100)
            return not fail

        with (
            patch("sys.argv", ["sync.py"] + (["--dry-run"] if dry_run else [])),
            patch.object(sync, "load_cache", return_value=cache),
            patch.object(sync, "save_cache") as save,
            patch.object(sync, "update_cache_history") as history,
            patch.object(sync, "authenticate", return_value=(object(), MagicMock())),
            patch.object(sync, "get_service"),
            patch.object(sync, "get_changes_since", side_effect=scan),
            patch.object(sync, "download_file", side_effect=download) as downloader,
            patch.object(
                sync,
                "generate_manifests",
                return_value=("manifest.txt", "manifest.csv"),
            ),
            patch.object(sync, "TUIManager", return_value=manager),
            patch.object(manager, "run", side_effect=run_headless),
        ):
            self.assertEqual(sync.main(), 0)
        save.assert_not_called()
        return app, history, downloader

    def test_success_posts_file_events_and_updates_checkpoint(self):
        app, history, download = self.run_sync()
        history.assert_called_once()
        download.assert_called_once()
        self.assertEqual(app.files_done, 1)
        self.assertEqual(app.download_events[0].status, EventStatus.DOWNLOADED)

    def test_dry_run_does_not_advance_checkpoint(self):
        app, history, download = self.run_sync(dry_run=True)
        history.assert_not_called()
        download.assert_not_called()
        self.assertEqual(app.download_events[0].status, EventStatus.DRY_RUN)

    def test_cancel_after_scan_does_not_download_or_advance_checkpoint(self):
        app, history, download = self.run_sync(cancel=True)
        history.assert_not_called()
        download.assert_not_called()
        self.assertEqual(app.status, "Stopped")

    def test_failed_download_does_not_advance_checkpoint(self):
        app, history, download = self.run_sync(fail=True)
        history.assert_not_called()
        self.assertEqual(app.download_events[0].status, EventStatus.FAILED)

    def test_cancel_during_download_finishes_active_file_without_checkpoint(self):
        app, history, download = self.run_sync(cancel_download=True)
        history.assert_not_called()
        self.assertEqual(app.files_done, 1)
        self.assertEqual(app.status, "Cancelled")

    def test_worker_exception_records_failed_file_without_checkpoint(self):
        app, history, download = self.run_sync(worker_error=True)
        history.assert_not_called()
        self.assertEqual(app.files_done, 1)
        self.assertEqual(app.download_events[0].reason, "worker failure")
        self.assertEqual(app.download_events[0].status, EventStatus.FAILED)


if __name__ == "__main__":
    unittest.main()

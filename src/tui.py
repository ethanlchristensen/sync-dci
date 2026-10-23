"""bruhanimate dashboard; worker threads only enqueue plain data messages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from queue import Empty, SimpleQueue
from threading import Event, Thread
from typing import TypedDict, Unpack
import unicodedata

from bruhanimate import (
    Buffer,
    Column,
    Fixed,
    Flex,
    FocusGroup,
    FocusTarget,
    KeyBinding,
    KeyBindings,
    Label,
    LogView,
    Panel,
    ResizableAnimation,
    Row,
    ScrollView,
    Slot,
    StatusBar,
    TextStyle,
    play,
)

from .models import DownloadEvent, EventStatus, format_size


@dataclass(frozen=True)
class _SyncEvent:
    event: DownloadEvent


@dataclass(frozen=True)
class _ProgressBytes:
    delta: int


@dataclass(frozen=True)
class _FileDone:
    pass


@dataclass(frozen=True)
class _Speed:
    bps: float


@dataclass(frozen=True)
class _Status:
    msg: str


@dataclass(frozen=True)
class _Log:
    text: str
    style: str = ""


@dataclass(frozen=True)
class _InitProgress:
    total_files: int
    total_bytes: int


class SyncSummary(TypedDict, total=False):
    downloaded: int
    skipped: int
    failed: int
    duration: float
    total_bytes: int
    sync_reason: str
    manifest_txt: str
    manifest_csv: str
    cancelled: bool


class _SyncComplete:
    def __init__(self, **summary: Unpack[SyncSummary]) -> None:
        self.summary = summary


@dataclass(frozen=True)
class _RunnerFinished:
    error: str | None = None


UiMessage = (
    _SyncEvent
    | _ProgressBytes
    | _FileDone
    | _Speed
    | _Status
    | _Log
    | _InitProgress
    | _SyncComplete
    | _RunnerFinished
)
SyncRunner = Callable[["SyncApp"], None]


def display_text(value: object) -> str:
    """Sanitize display cells without changing actual paths or manifest data."""
    text = str(value).replace("\t", "    ").replace("\r", " ").replace("\n", " ")
    return "".join(
        char
        if char.isprintable()
        and not unicodedata.category(char).startswith("M")
        and unicodedata.east_asian_width(char) not in ("W", "F")
        else "?"
        for char in text
    )


def progress_bar(done: int, total: int, width: int = 16) -> str:
    fraction = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
    filled = int(width * fraction)
    return f"[{'#' * filled}{'-' * (width - filled)}] {fraction:.0%}"


class SyncApp:
    """Single-use Animation owning its dashboard and one cooperative worker.

    Build with build(buffer), then let Player own start/update/draw/close.
    post_message is thread-safe; every widget mutation happens during update.
    Quit requests stop and keeps presenting while active requests finish.
    close joins the worker. Network calls are not forcibly terminated.
    Constructors do not open resources or start background work.
    """

    def __init__(
        self,
        folder_id: str,
        dest_path: str,
        avg_speed: float | None = None,
        sync_func: SyncRunner | None = None,
    ) -> None:
        self.folder_id, self.dest_path = folder_id, dest_path
        self.avg_speed = avg_speed
        self.sync_func = sync_func
        self.cancel_event = Event()
        self._messages: SimpleQueue[UiMessage] = SimpleQueue()
        self._thread: Thread | None = None
        self._runner_done = sync_func is None
        self._closed = self._started = self._quit_requested = False
        self._layout: ResizableAnimation | None = None
        self.status = "Starting..."
        self.completed = False
        self.current_speed: float | None = None
        self.byte_total = self.file_total = self.bytes_done = self.files_done = 0
        self.download_events: list[DownloadEvent] = []
        self.filter_status: EventStatus | None = None
        self._visible_events: list[DownloadEvent] = []
        self.log = LogView(size=(1, 1), max_lines=1000, wrap="character")
        self.files = LogView(size=(1, 1), max_lines=1000, wrap="none")
        self.session = ScrollView(
            Label(self._session_text, fill=False),
            size=(1, 1),
            content_size=(256, 6),
            background=None,
        )
        self.session_target = FocusTarget("session", self.session.bindings)
        self.log_target = FocusTarget("activity", self.log.bindings)
        self.files_target = FocusTarget(
            "files",
            KeyBindings(
                *self.files.bindings.bindings,
                KeyBinding.character("f", self.cycle_filter, description="filter"),
                KeyBinding.character("y", self.inspect_top, description="inspect"),
            ),
        )
        self.focus = FocusGroup(
            self.log_target,
            self.files_target,
            self.session_target,
            global_bindings=KeyBindings(
                KeyBinding.character("q", self.request_quit),
                KeyBinding.character("Q", self.request_quit),
                KeyBinding("escape", self.request_quit),
                KeyBinding("ctrl+c", self.request_quit),
                KeyBinding.character("f", self.cycle_filter, description="filter"),
            ),
        )

    def post_message(self, message: UiMessage) -> None:
        """Enqueue data without touching widgets; safe for sync/download workers."""
        if not self._closed:
            self._messages.put(message)

    @property
    def stop_requested(self) -> bool:
        return self.cancel_event.is_set()

    def _session_text(self) -> str:
        return "\n".join(
            (
                f"Folder: {display_text(self.folder_id)}",
                f"Destination: {display_text(self.dest_path)}",
                f"Status: {display_text(self.status)}",
                f"Cached speed: {format_size(int(self.avg_speed))}/s"
                if self.avg_speed
                else "Cached speed: --",
                "Tab / Shift+Tab changes pane",
                "Arrows / PgUp / PgDn / Home / End scroll",
            )
        )

    def _progress_text(self) -> str:
        speed = (
            f"{format_size(int(self.current_speed))}/s" if self.current_speed else "--"
        )
        remaining = max(0, self.byte_total - self.bytes_done)
        eta = f"{remaining / self.current_speed:.0f}s" if self.current_speed else "--"
        return (
            f"Bytes {progress_bar(self.bytes_done, self.byte_total)}\n"
            f"{format_size(self.bytes_done)} / {format_size(self.byte_total)}\n"
            f"Files {self.files_done}/{self.file_total} {progress_bar(self.files_done, self.file_total, 10)}\n"
            f"Speed {speed}  ETA {eta}"
        )

    def _files_title(self) -> str:
        label = self.filter_status.value if self.filter_status else "all"
        return f"Files [{label}] | {len(self._visible_events)} shown / {len(self.download_events)} total | y inspects top row"

    def _status_text(self) -> str:
        selected = self.focus.focused
        name = selected.name if selected else "no pane"
        view = self.log if selected is self.log_target else self.files
        return (
            f"{name} {'LIVE' if view.follow_tail else 'HISTORY'}"
            if selected is not self.session_target
            else name
        )

    def build(self, buffer: Buffer) -> SyncApp:
        """Allocate once; used as play(effect_factory=app.build)."""
        if self._layout is not None or self._closed:
            raise RuntimeError("SyncApp is single-use")
        self._layout = Column(
            Slot(
                Label(
                    "GOOGLE DRIVE SYNC | bruhanimate",
                    style=TextStyle(foreground=117, bold=True),
                ),
                size=Fixed(1),
            ),
            Slot(
                Row(
                    Slot(
                        Column(
                            Slot(
                                Panel(
                                    self.session,
                                    size=(1, 1),
                                    fit=True,
                                    title="SESSION",
                                    focus=self.session_target,
                                    border_color=117,
                                )
                            ),
                            Slot(
                                Label(
                                    self._progress_text, style=TextStyle(foreground=150)
                                ),
                                size=Fixed(4),
                            ),
                        ),
                        size=Flex(2),
                    ),
                    Slot(
                        Panel(
                            self.log,
                            size=(1, 1),
                            fit=True,
                            title="ACTIVITY",
                            focus=self.log_target,
                            border_color=213,
                        ),
                        size=Flex(3),
                    ),
                    gap=1,
                ),
                size=Flex(2),
            ),
            Slot(
                Column(
                    Slot(
                        Label(self._files_title, style=TextStyle(foreground=117)),
                        size=Fixed(1),
                    ),
                    Slot(
                        Panel(
                            self.files,
                            size=(1, 1),
                            fit=True,
                            title="STATUS | PATH | SIZE | REASON",
                            focus=self.files_target,
                            border_color=117,
                        )
                    ),
                )
            ),
            Slot(
                StatusBar(
                    self._status_text,
                    center=self.focus.help_text,
                    right="q quit",
                    style=TextStyle(background=24),
                ),
                size=Fixed(1),
            ),
            gap=1,
        ).build(buffer)
        return self

    def start(self) -> None:
        if self._closed or self._layout is None:
            raise RuntimeError(
                "Build SyncApp before starting; closed apps cannot restart"
            )
        if self._started:
            return
        self._started = True
        try:
            self._layout.start()
            self.focus.focus("activity")
            self._add_log("Dashboard ready")
            if self.sync_func is not None:
                self._runner_done = False
                self._thread = Thread(
                    target=self._run_worker, name="drive-sync", daemon=False
                )
                self._thread.start()
        except BaseException:
            self.close()
            raise

    def _run_worker(self) -> None:
        error = None
        try:
            if self.sync_func is not None:
                self.sync_func(self)
        except Exception as exc:
            error = str(exc)
        finally:
            self.post_message(_RunnerFinished(error))

    def _add_log(self, text: str, style: str = "") -> None:
        level = "ERROR " if "red" in style else "WARN " if "yellow" in style else ""
        self.log.append(f"{datetime.now():%H:%M:%S} {level}{display_text(text)}")

    def _rebuild_files(self) -> None:
        self._visible_events = [
            event
            for event in self.download_events
            if self.filter_status is None or event.status == self.filter_status
        ][-1000:]
        self.files.clear()
        if self._visible_events:
            self.files.append(
                "\n".join(self._event_text(event) for event in self._visible_events)
            )

    @staticmethod
    def _event_text(event: DownloadEvent) -> str:
        return display_text(
            f"{event.status.value:10} | {event.relative_path} | {format_size(event.size)} | {event.reason}"
        )

    def cycle_filter(self) -> None:
        choices = [None, *EventStatus]
        self.filter_status = choices[
            (choices.index(self.filter_status) + 1) % len(choices)
        ]
        self._rebuild_files()

    def inspect_top(self) -> None:
        if not self._visible_events:
            self._add_log("No matching file to inspect")
            return
        event = self._visible_events[self.files.offset[1]]
        self._add_log(self._event_text(event))
        if event.completion_time:
            self._add_log(f"Completed at {event.completion_time}")

    def _apply(self, message: UiMessage) -> None:
        if isinstance(message, _Log):
            self._add_log(message.text, message.style)
        elif isinstance(message, _Status):
            self.status = message.msg
        elif isinstance(message, _InitProgress):
            self.file_total, self.byte_total = message.total_files, message.total_bytes
            self.files_done = self.bytes_done = 0
        elif isinstance(message, _ProgressBytes):
            self.bytes_done += message.delta
        elif isinstance(message, _FileDone):
            self.files_done += 1
        elif isinstance(message, _Speed):
            self.current_speed = message.bps
        elif isinstance(message, _SyncEvent):
            self.download_events.append(message.event)
            if self.filter_status is None or message.event.status == self.filter_status:
                self._visible_events.append(message.event)
                self._visible_events = self._visible_events[-1000:]
                self.files.append(self._event_text(message.event))
        elif isinstance(message, _SyncComplete):
            self.completed = True
            s = message.summary
            self.status = "Cancelled" if s.get("cancelled") else "Completed"
            self._add_log(
                f"Downloaded: {s.get('downloaded', 0)} | Skipped: {s.get('skipped', 0)} | Failed: {s.get('failed', 0)}"
            )
            self._add_log(f"Duration: {s.get('duration', 0):.1f}s")
            for key in ("sync_reason", "manifest_txt", "manifest_csv"):
                value = s.get(key)
                if value:
                    self._add_log(f"{key}: {value}")
            self._add_log("Press q to exit; f filters files")
        elif isinstance(message, _RunnerFinished):
            self._runner_done = True
            if message.error is not None:
                self.status = "Failed"
                self._add_log(message.error, "red")
            elif self.stop_requested and not self.completed:
                self.status = "Stopped"

    def update(self, dt: float) -> None:
        if self._layout is None or not self._started or self._closed:
            raise RuntimeError("Dashboard must be started")
        for _ in range(500):
            try:
                message = self._messages.get_nowait()
            except Empty:
                break
            self._apply(message)
        self._layout.update(dt)

    def draw(self, buffer: Buffer) -> None:
        if self._layout is None or not self._started or self._closed:
            raise RuntimeError("Dashboard must be started")
        self._layout.draw(buffer)

    def resize(self, size: tuple[int, int]) -> None:
        if self._layout is None:
            raise RuntimeError("Build dashboard before resizing")
        self._layout.resize(size)

    def request_quit(self) -> None:
        if not self._quit_requested:
            self._quit_requested = True
            self.cancel_event.set()
            if not self._runner_done:
                self.status = "Stopping after active requests finish..."
                self._add_log(self.status, "yellow")

    def handle_event(self, event: int) -> bool:
        self.focus.handle_event(event)
        return self._quit_requested and self._runner_done

    @property
    def finished(self) -> bool:
        return self._quit_requested and self._runner_done

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_event.set()
        try:
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join()
        finally:
            if self._layout is not None:
                self._layout.close()
            else:
                self.session.close()
                self.log.close()
                self.files.close()

    def run(self, *, frames: int | None = None) -> int:
        return play(
            effect_factory=self.build,
            event_handler=self.handle_event,
            fps=30,
            updates_per_second=30,
            on_resize="resize",
            on_complete="exit",
            frames=frames,
        )


class TUIManager:
    """App entry point retaining the sync runner's data-message boundary."""

    def __init__(
        self, folder_id: str, dest_path: str, avg_speed: float | None = None
    ) -> None:
        self.app = SyncApp(folder_id, dest_path, avg_speed=avg_speed)

    def set_sync_func(self, func: SyncRunner) -> None:
        self.app.sync_func = func

    def run(self) -> int:
        return self.app.run()


def run_demo(*, frames: int | None = None) -> int:
    """Offline UI preview: no credentials, cache access, or file downloads."""

    def simulate(app: SyncApp) -> None:
        app.post_message(_Status("Demo sync"))
        app.post_message(_InitProgress(20, 20 * 1024 * 1024))
        for index in range(20):
            if app.cancel_event.wait(0.15):
                return
            app.post_message(_Log(f"Downloaded demo/score-{index + 1:02}.pdf"))
            app.post_message(_ProgressBytes(1024 * 1024))
            app.post_message(_FileDone())
            app.post_message(_Speed(7 * 1024 * 1024))
            app.post_message(
                _SyncEvent(
                    DownloadEvent(
                        f"demo/score-{index + 1:02}.pdf",
                        EventStatus.DOWNLOADED,
                        1024 * 1024,
                    )
                )
            )
        app.post_message(
            _SyncComplete(
                downloaded=20,
                skipped=0,
                failed=0,
                duration=3,
                sync_reason="Offline demo",
            )
        )

    return SyncApp(
        "demo-folder", "demo destination (no files written)", sync_func=simulate
    ).run(frames=frames)

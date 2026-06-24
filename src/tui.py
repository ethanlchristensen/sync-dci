from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text
from rich.align import Align
from datetime import datetime
import collections

class TUIManager:
    def __init__(self, folder_id, dest_path, total_files=0, total_bytes=0, avg_speed=None):
        self.console = Console()
        self.folder_id = folder_id
        self.dest_path = dest_path
        self.logs = collections.deque(maxlen=30)  # limit logs to prevent bloating the screen
        self.status_msg = "Initializing..."
        self.avg_speed = avg_speed
        
        # Build layout
        self.layout = Layout()
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        self.layout["main"].split_row(
            Layout(name="stats", ratio=1),
            Layout(name="logs", ratio=1)
        )
        self.layout["main"]["stats"].split(
            Layout(name="session_info", ratio=1),
            Layout(name="progress_info", size=8)
        )
        
        # Setup separate progress bars
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self.console,
            expand=True
        )
        
        self.byte_task = None
        self.file_task = None
        self.total_files = total_files
        self.total_bytes = total_bytes
        
        self.live = None

    def __enter__(self):
        self.live = Live(self.layout, console=self.console, refresh_per_second=4, screen=True)
        self.live.start()
        self.update_layout()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.live:
            self.live.stop()

    def add_log(self, text, style=None):
        timestamp = datetime.now().strftime("%H:%M:%S")
        styled_text = Text(f"[{timestamp}] ")
        if style:
            styled_text.append(text, style=style)
        else:
            styled_text.append(text)
        self.logs.append(styled_text)
        self.update_layout()

    def set_status(self, msg):
        self.status_msg = msg
        self.update_layout()

    def init_progress(self, total_files, total_bytes):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.byte_task = self.progress.add_task("[green]Total Bytes", total=total_bytes)
        self.file_task = self.progress.add_task("[cyan]Total Files", total=total_files)
        self.update_layout()

    def advance_bytes(self, delta):
        if self.byte_task is not None:
            self.progress.advance(self.byte_task, delta)
            self.update_layout()

    def advance_files(self, delta=1):
        if self.file_task is not None:
            self.progress.advance(self.file_task, delta)
            self.update_layout()

    def update_layout(self):
        # 1. Header
        header_text = Text("Google Drive Sync DC Folder", style="bold cyan")
        self.layout["header"].update(
            Panel(Align.center(header_text, vertical="middle"), border_style="cyan")
        )
        
        # 2. Session Info
        stats_table = Table.grid(padding=1)
        stats_table.add_column("Key", style="bold white", width=15)
        stats_table.add_column("Value", style="green")
        stats_table.add_row("Folder ID:", self.folder_id)
        stats_table.add_row("Destination:", self.dest_path)
        stats_table.add_row("Status:", f"[bold yellow]{self.status_msg}[/bold yellow]")
        
        if self.avg_speed:
            speed_mb = self.avg_speed / (1024 * 1024)
            stats_table.add_row("Avg Speed:", f"{speed_mb:.2f} MB/s (cached)")
            
        self.layout["main"]["stats"]["session_info"].update(
            Panel(stats_table, title="Session Info", border_style="blue")
        )
        
        # Progress Info
        self.layout["main"]["stats"]["progress_info"].update(
            Panel(self.progress, title="Sync Progress", border_style="green")
        )
        
        # 3. Logs
        log_content = Text("\n").join(self.logs)
        self.layout["main"]["logs"].update(
            Panel(log_content, title="Real-Time Sync Logs", border_style="magenta")
        )
        
        # 4. Footer
        footer_text = Text("Press Ctrl+C to abort safely | Designed for fast syncs", style="dim white")
        self.layout["footer"].update(
            Panel(Align.center(footer_text, vertical="middle"), border_style="cyan")
        )
        
        if self.live:
            self.live.refresh()

"""Desktop app for Rover Image Scraper.

Run:  python rover_scraper/gui.py

All downloading and scanning runs on a background thread. The window polls shared state on a timer, so it stays
responsive, and Stop works like Ctrl+C in the command line: finished photos are kept, and starting again resumes.
"""
from __future__ import annotations

import collections
import math
import os
import queue
import sys
import tempfile
import threading
import time
import traceback

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QActionGroup, QColor, QDesktopServices, QFont, QIntValidator, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QToolButton,
                               QVBoxLayout, QWidget)

from api import SUPPORTED_ROVERS, ApiError, Cancelled, MarsVistaClient, load_api_key, save_api_key
from pds import PdsClient, RequestCounter, RoverSources, is_pds_rover, latest_sol
from download import NASA_STATUS
from plan import last_sol_on_disk
from ratelimit import RequestTracker
from scan import RoverStatus, ScanStore, full_check, quick_scan
from scraper import Report, Scraper
from dataclasses import asdict
from settings import Settings
from state import Unavailable, default_state_dir

STATE_DIR = default_state_dir()
SETTINGS_PATH = os.path.join(STATE_DIR, "settings.json")
SCANS_PATH = os.path.join(STATE_DIR, "scans.json")
REQUESTS_PATH = os.path.join(STATE_DIR, "requests.json")
UNAVAILABLE_PATH = os.path.join(STATE_DIR, "unavailable.json")
PAUSE_PATH = os.path.join(STATE_DIR, "paused.json")      # no longer written; removed if an old version left one

AVG_PHOTO_MB = 0.2          # observed average across Curiosity and Perseverance downloads
ALL_ROVERS = SUPPORTED_ROVERS
MAIN_ROVERS = ("Curiosity", "Perseverance")          # always shown in Library status


# ---- formatting ------------------------------------------------------------------------------------------

def fmt(n) -> str:
    return "N/A" if n is None else f"{n:,}"


def fmt_size(num_bytes) -> str:
    if num_bytes is None:
        return "N/A"
    gb = num_bytes / 1024 ** 3
    return f"{gb:,.1f} GB" if gb >= 1 else f"{num_bytes / 1024 ** 2:,.0f} MB"


DATA_UNITS = ("auto", "KB", "MB", "GB", "TB")
SPEED_UNITS = ("auto", "KB/s", "MB/s", "Mbit/s")
SPEED_WINDOWS = (5, 10, 30, 60)
_POWERS = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4}


def fmt_bytes(num_bytes: float, unit: str = "auto") -> str:
    """Size in the chosen unit (1 KB = 1024 bytes, as Windows Explorer shows sizes)."""
    if unit == "auto":
        unit = "B"
        for name in ("KB", "MB", "GB", "TB"):
            if num_bytes >= 1024 ** _POWERS[name]:
                unit = name
    value = num_bytes / 1024 ** _POWERS[unit]
    if unit == "B":
        return f"{value:,.0f} B"
    return f"{value:,.2f} {unit}" if value < 100 else f"{value:,.1f} {unit}"


def fmt_rate(bytes_per_s: float, unit: str = "auto") -> str:
    """Speed in the chosen unit. Mbit/s uses network convention (1 Mbit = 1,000,000 bits)."""
    if unit == "Mbit/s":
        return f"{bytes_per_s * 8 / 1_000_000:,.1f} Mbit/s"
    if unit == "auto":
        return fmt_bytes(bytes_per_s, "auto") + "/s"
    return fmt_bytes(bytes_per_s, unit[:-2]) + "/s"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_when(ts) -> str:
    if not ts:
        return "never"
    if time.strftime("%Y%m%d", time.localtime(ts)) == time.strftime("%Y%m%d"):
        return time.strftime("today %H:%M", time.localtime(ts))
    return time.strftime("%d %b %Y, %H:%M", time.localtime(ts))


# ---- pause checkpoint ------------------------------------------------------------------------------------

def report_to_dict(r: Report) -> dict:
    d = asdict(r)
    d["actions"] = dict(r.actions)
    return d


def report_from_dict(d: dict) -> Report:
    d = dict(d)
    d["actions"] = collections.Counter(d.get("actions", {}))
    d["cancelled"] = False
    known = Report.__dataclass_fields__
    return Report(**{k: v for k, v in d.items() if k in known})


def remove_old_pause_file() -> None:
    """Earlier versions saved a paused download to disk. Pausing now lasts only while the app is open."""
    try:
        os.remove(PAUSE_PATH)
    except OSError:
        pass


# ---- theme -----------------------------------------------------------------------------------------------

LIGHT = dict(bg="#F4F5F7", card="#FFFFFF", border="#E1E4E8", text="#1F2328", muted="#667085",
             accent="#2563EB", accent_hover="#1D4ED8", accent_text="#FFFFFF", input="#FFFFFF",
             ok="#15803D", ok_bg="#DCFCE7", warn="#92400E", warn_bg="#FEF3C7", warn_border="#F59E0B",
             err="#B91C1C", err_bg="#FEE2E2", track="#E9ECF1", log_bg="#F8F9FB")
DARK = dict(bg="#15171C", card="#1E2128", border="#2D313A", text="#E6E8EB", muted="#9BA3AF",
            accent="#3B82F6", accent_hover="#60A5FA", accent_text="#FFFFFF", input="#171A20",
            ok="#4ADE80", ok_bg="#14301F", warn="#FCD34D", warn_bg="#3A2E12", warn_border="#B7791F",
            err="#FCA5A5", err_bg="#3B1717", track="#2A2F38", log_bg="#171A20")


def system_is_dark(app: QApplication) -> bool:
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:      # Qt older than 6.5
        return False


def _check_icon() -> str:
    """Path to the white tick SVG for checkbox indicators (Qt style sheets need a file). It ships with the
    code, so nothing is written at run time."""
    old = os.path.join(tempfile.gettempdir(), "rover_scraper_check.svg")    # written by earlier versions
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    # In the packaged app, data files live in the bundle's folder (sys._MEIPASS), not next to the code.
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "check.svg").replace("\\", "/")


def apply_theme(app: QApplication, t: dict) -> None:
    pal = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "input"),
                      (QPalette.ColorRole.AlternateBase, "card"), (QPalette.ColorRole.Text, "text"),
                      (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Button, "card"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
                      (QPalette.ColorRole.HighlightedText, "accent_text"), (QPalette.ColorRole.ToolTipBase, "card"),
                      (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
                      (QPalette.ColorRole.Mid, "border"), (QPalette.ColorRole.Link, "accent"),
                      (QPalette.ColorRole.LinkVisited, "accent")):
        pal.setColor(role, QColor(t[key]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t["muted"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(t["muted"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t["muted"]))
    app.setPalette(pal)
    app.setStyleSheet(f"""
        QWidget {{ font-family: "Segoe UI"; font-size: 10pt; }}
        QMainWindow, QWidget#root {{ background: {t['bg']}; }}
        QMenuBar {{ background: {t['card']}; padding: 2px 8px; }}
        QMenuBar::item {{ padding: 5px 10px; border-radius: 4px; background: transparent; }}
        QMenuBar::item:selected {{ background: {t['track']}; }}
        QMenu {{ background: {t['card']}; border: 1px solid {t['border']}; padding: 4px; }}
        QMenu::item {{ padding: 6px 28px 6px 26px; border-radius: 4px; color: {t['text']}; }}
        QMenu::item:selected {{ background: {t['accent']}; color: {t['accent_text']}; }}
        QMenu::item:disabled {{ color: {t['muted']}; }}
        QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
        QFrame#header, QFrame#bottomBar {{ background: {t['card']}; border: none; }}
        QFrame#header {{ border-bottom: 1px solid {t['border']}; }}
        QFrame#bottomBar {{ border-top: 1px solid {t['border']}; }}
        QFrame#card {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 8px; }}
        QFrame#card QLabel, QFrame#card QCheckBox {{ background: transparent; }}
        QLabel#appTitle {{ font-size: 13pt; font-weight: 600; }}
        QLabel#cardTitle {{ color: {t['muted']}; font-size: 8.5pt; font-weight: 700; letter-spacing: 0.6px; }}
        QLabel#muted {{ color: {t['muted']}; }}
        QLabel#big {{ font-size: 11pt; font-weight: 600; }}
        QLabel#value {{ font-weight: 600; }}
        QLabel#rowHead {{ color: {t['muted']}; }}
        QLabel#section {{ color: {t['muted']}; font-size: 8pt; font-weight: 700; letter-spacing: 0.5px;
                         padding-top: 6px; }}
        QLabel#colHead {{ font-weight: 600; }}
        QLineEdit, QSpinBox {{ background: {t['input']}; border: 1px solid {t['border']}; border-radius: 6px;
                              padding: 5px 8px; }}
        QLineEdit:focus, QSpinBox:focus {{ border: 1px solid {t['accent']}; }}
        QLineEdit[missing="true"] {{ border: 1px solid {t['warn_border']}; background: {t['warn_bg']}; }}
        QLineEdit:disabled, QSpinBox:disabled {{ color: {t['muted']}; }}
        QPushButton {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 6px;
                      padding: 6px 14px; }}
        QPushButton:hover {{ border-color: {t['accent']}; }}
        QPushButton:disabled {{ color: {t['muted']}; border-color: {t['border']}; }}
        QPushButton#primary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
                              font-weight: 600; padding: 7px 22px; }}
        QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
        QPushButton#primary:disabled {{ background: {t['track']}; color: {t['muted']}; }}
        QToolButton#link {{ border: none; color: {t['accent']}; padding: 2px 0; background: transparent; }}
        QToolButton#link:disabled {{ color: {t['muted']}; }}
        QProgressBar {{ background: {t['track']}; border: none; border-radius: 4px; }}
        QProgressBar::chunk {{ background: {t['accent']}; border-radius: 4px; }}
        QProgressBar[paused="true"]::chunk {{ background: {t['warn_border']}; }}
        QPlainTextEdit {{ background: {t['log_bg']}; border: 1px solid {t['border']}; border-radius: 6px;
                         font-family: "Cascadia Mono", Consolas, monospace; font-size: 9pt; padding: 4px; }}
        QFrame#banner {{ background: {t['warn_bg']}; border: 1px solid {t['warn_border']}; border-radius: 6px; }}
        QFrame#banner QLabel {{ color: {t['warn']}; background: transparent; }}
        QLabel#pill {{ border-radius: 11px; padding: 3px 12px; font-weight: 600; }}
        QLabel#pill[tone="ok"] {{ color: {t['ok']}; background: {t['ok_bg']}; }}
        QLabel#pill[tone="warn"] {{ color: {t['warn']}; background: {t['warn_bg']}; }}
        QLabel#pill[tone="err"] {{ color: {t['err']}; background: {t['err_bg']}; }}
        QLabel#pill[tone="idle"] {{ color: {t['muted']}; background: {t['track']}; }}
        QLabel[tone="err"] {{ color: {t['err']}; }}
        QLabel[tone="warn"] {{ color: {t['warn']}; }}
        QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
        QCheckBox {{ spacing: 8px; }}
        QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t['muted']}; border-radius: 4px;
                               background: {t['input']}; }}
        QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
        QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']};
                                       image: url({_check_icon()}); }}
        QCheckBox::indicator:disabled {{ background: {t['track']}; border-color: {t['border']}; }}
        QCheckBox::indicator:checked:disabled {{ background: {t['muted']}; border-color: {t['muted']}; }}
    """)


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


# ---- small widgets ---------------------------------------------------------------------------------------

def card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    if title:
        lay.addWidget(label(title.upper(), "cardTitle"))
    return frame, lay


class WrapLabel(QLabel):
    """A word-wrapped label that always gets the height its text needs at its current width. Qt layouts
    (grids especially) otherwise give it a single line and cut off the rest."""

    def __init__(self, text: str = "", min_width: int = 0):
        super().__init__(text)
        self.setWordWrap(True)
        if min_width:
            self.setMinimumWidth(min_width)

    def _fit(self) -> None:
        need = self.heightForWidth(max(1, self.width()))
        if need > 0 and self.minimumHeight() != need:
            self.setMinimumHeight(need)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def setText(self, text: str) -> None:
        super().setText(text)
        self._fit()

    def changeEvent(self, event) -> None:     # font or style changes (theme switch) change the height needed
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit()


def label(text: str = "", name: str | None = None, wrap: bool = False, min_width: int = 0) -> QLabel:
    lbl = WrapLabel(text, min_width) if wrap else QLabel(text)
    if name:
        lbl.setObjectName(name)
    return lbl


def link_button(text: str) -> QToolButton:
    b = QToolButton()
    b.setObjectName("link")
    b.setText(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def meter() -> QProgressBar:
    bar = QProgressBar()
    bar.setTextVisible(False)
    bar.setFixedHeight(8)
    bar.setRange(0, 1000)
    return bar


class CameraPanel(QWidget):
    """Expandable list of camera checkboxes for one rover."""

    def __init__(self, on_change):
        super().__init__()
        self.on_change = on_change
        self.boxes: dict[str, QCheckBox] = {}
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(4)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 2, 0, 6)
        row = QHBoxLayout()
        self.all_btn, self.none_btn = link_button("All"), link_button("None")
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn.clicked.connect(lambda: self._set_all(False))
        row.addWidget(self.all_btn)
        row.addWidget(label("·", "muted"))
        row.addWidget(self.none_btn)
        row.addStretch()
        lay.addLayout(row)
        lay.addLayout(self.grid)

    def set_cameras(self, cameras: list[str], excluded: set[str]) -> None:
        for box in self.boxes.values():
            box.deleteLater()
        self.boxes = {}
        for i, cam in enumerate(cameras):
            box = QCheckBox(cam)
            box.setChecked(cam not in excluded)
            box.toggled.connect(lambda _=False: self.on_change())
            self.boxes[cam] = box
            self.grid.addWidget(box, i // 2, i % 2)

    def _set_all(self, on: bool) -> None:
        self.set_checked(lambda cam: on)
        self.on_change()

    def set_checked(self, ticked) -> None:
        """Tick each camera for which ticked(name) is true, without calling on_change."""
        for cam, box in self.boxes.items():
            box.blockSignals(True)
            box.setChecked(bool(ticked(cam)))
            box.blockSignals(False)

    def excluded(self) -> list[str]:
        return [c for c, b in self.boxes.items() if not b.isChecked()]

    def counts(self) -> tuple[int, int]:
        return sum(b.isChecked() for b in self.boxes.values()), len(self.boxes)


# ---- main window -----------------------------------------------------------------------------------------

class MainWindow(QMainWindow):
    POLL_MS = 200

    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.settings = Settings.load(SETTINGS_PATH)
        self.tracker = RequestTracker(REQUESTS_PATH)
        self.store = ScanStore(SCANS_PATH)
        self.events: queue.Queue = queue.Queue()
        self.mode = "idle"                       # idle | download | scan | full | preview
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.report: Report | None = None
        self.rover_info: dict[str, dict] = {}    # name -> {"max_sol": int, "cameras": [...]}
        self.progress_state: dict = {}
        self.rover_started = 0.0
        self.run_started = 0.0
        self.run_ended = 0.0
        self.pausing = False
        self.run_params: dict = {}
        self.pos: dict = {}                      # where the current download is: rover, rover_index, sol, sol_min
        # A paused download, kept in memory only: it lasts until Stop, a new Start, or closing the app.
        self.checkpoint: dict | None = None
        self.pds_requests = RequestCounter()      # NASA archive requests this session (no quota)
        remove_old_pause_file()
        self._loading = False
        # Camera selection to bring back when an unticked rover is ticked again (and what's saved for it).
        self.saved_excluded: dict[str, set[str]] = {n: set(c) for n, c in self.settings.excluded_cameras.items()}
        self.speed_samples: collections.deque = collections.deque(maxlen=1000)
        self.api_error = ""
        self.last_summary = ""

        self.setWindowTitle("Rover Image Scraper")
        self.setMinimumSize(900, 560)
        # Open big enough to show everything without scrolling, but never larger than the screen.
        avail = app.primaryScreen().availableGeometry() if app.primaryScreen() else None
        self.resize(min(1200, avail.width() - 40) if avail else 1200,
                    min(1000, avail.height() - 60) if avail else 900)
        self._build()
        self._build_menus()
        self._load_settings_into_widgets()
        self._refresh_library()
        self._validate()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(self.POLL_MS)
        if self.key_edit.text().strip():
            self._load_rover_info()

    # ---- layout ------------------------------------------------------------------------------------------

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)

        header = QFrame()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 12, 20, 12)
        hl.addWidget(label("Rover Image Scraper", "appTitle"))
        hl.addStretch()
        self.pill = label("● Mars Vista: not connected", "pill")
        self.pill.setProperty("tone", "idle")
        hl.addWidget(self.pill)
        outer.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(16)
        outer.addLayout(body, 1)

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setFixedWidth(430)
        left = QWidget()
        self.left_col = QVBoxLayout(left)
        self.left_col.setContentsMargins(0, 0, 6, 0)
        self.left_col.setSpacing(12)
        left_scroll.setWidget(left)
        body.addWidget(left_scroll)

        # The right column scrolls too, so a small window or screen gets a scroll bar instead of squashed text.
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(0, 0, 6, 0)
        right.setSpacing(12)
        right_scroll.setWidget(right_w)
        body.addWidget(right_scroll, 1)

        self._build_folder_card()
        self._build_key_card()
        self._build_rovers_card()
        self._build_options_card()
        self.left_col.addStretch()

        right.addWidget(self._build_library_card())
        right.addWidget(self._build_progress_card())
        right.addWidget(self._build_usage_card())
        right.addWidget(self._build_activity_card(), 1)

        bottom = QFrame()
        bottom.setObjectName("bottomBar")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(20, 10, 20, 10)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setToolTip("Estimate what a download would add, without downloading anything")
        self.preview_btn.clicked.connect(self._preview)
        bl.addWidget(self.preview_btn)
        bl.addStretch()
        self.start_hint = label("", "muted")             # why Start is greyed out, when it is
        self.start_hint.setProperty("tone", "warn")
        bl.addWidget(self.start_hint)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setToolTip("Finish the photos in progress and remember where the download is, so Resume "
                                  "continues from the same sol without re-checking earlier sols")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._pause)
        bl.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        bl.addWidget(self.stop_btn)
        self.start_btn = QPushButton("▶  Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.setMinimumWidth(130)
        self.start_btn.clicked.connect(self._start_download)
        bl.addWidget(self.start_btn)
        outer.addWidget(bottom)

    def _build_folder_card(self) -> None:
        frame, lay = card("Photo folder")
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Choose where photos are saved")
        self.folder_edit.textChanged.connect(self._folder_changed)
        row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        lay.addLayout(row)
        self.folder_hint = label("This folder doesn't exist yet. Start, Quick scan, Full check or Open folder "
                                 "will offer to create it.", "muted", wrap=True)
        self.folder_hint.setProperty("tone", "warn")
        self.folder_hint.setVisible(False)
        lay.addWidget(self.folder_hint)
        row2 = QHBoxLayout()
        self.open_btn = link_button("Open folder")
        self.open_btn.clicked.connect(self._open_folder)
        row2.addWidget(self.open_btn)
        row2.addStretch()
        lay.addLayout(row2)
        self.locked = [self.folder_edit, browse]
        self.left_col.addWidget(frame)

    def _build_key_card(self) -> None:
        frame, lay = card("API key")
        row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your Mars Vista API key")
        self.key_edit.textChanged.connect(self._validate)
        self.key_edit.editingFinished.connect(self._key_edited)
        row.addWidget(self.key_edit, 1)
        self.show_key = QPushButton("Show")
        self.show_key.setCheckable(True)
        self.show_key.toggled.connect(lambda on: (
            self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password),
            self.show_key.setText("Hide" if on else "Show")))
        row.addWidget(self.show_key)
        lay.addLayout(row)
        hint = label('Get a free key at <a href="https://marsvista.dev">marsvista.dev</a>. '
                     "Saved in Windows Credential Manager.", "muted", wrap=True)
        hint.setOpenExternalLinks(True)
        lay.addWidget(hint)
        self.locked += [self.key_edit]
        self.left_col.addWidget(frame)

    def _build_rovers_card(self) -> None:
        frame, lay = card("Rovers")
        self.rover_boxes: dict[str, QCheckBox] = {}
        self.rover_status: dict[str, QLabel] = {}
        self.camera_toggles: dict[str, QToolButton] = {}
        self.camera_panels: dict[str, CameraPanel] = {}
        for name in ALL_ROVERS:
            row = QHBoxLayout()
            box = QCheckBox(name)
            if is_pds_rover(name):
                box.setToolTip("Photos come from NASA's Planetary Data System archive (no Mars Vista requests).")
            box.toggled.connect(lambda _=False, n=name: self._rover_toggled(n))
            box.toggled.connect(self._validate)
            box.toggled.connect(lambda _=False: self._update_library_columns())
            row.addWidget(box)
            status = label("N/A", "muted")
            status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            row.addWidget(status, 1)
            lay.addLayout(row)
            toggle = link_button("Cameras ▸")
            toggle.clicked.connect(lambda _=False, n=name: self._toggle_cameras(n))
            trow = QHBoxLayout()
            trow.setContentsMargins(26, 0, 0, 0)
            trow.addWidget(toggle)
            trow.addStretch()
            lay.addLayout(trow)
            panel = CameraPanel(lambda n=name: self._cameras_changed(n))
            panel.setVisible(False)
            lay.addWidget(panel)
            self.rover_boxes[name], self.rover_status[name] = box, status
            self.camera_toggles[name], self.camera_panels[name] = toggle, panel
            self.locked += [box, toggle, panel]
        self.left_col.addWidget(frame)

    def _build_options_card(self) -> None:
        frame, lay = card("Options")
        self.check_existing = QCheckBox("Check existing sols for missing photos")
        self.check_existing.setToolTip("On: start at sol 0 and fill gaps in sols you already have (slower).\n"
                                       "Off: only download sols after the last one in the folder.")
        lay.addWidget(self.check_existing)
        lay.addWidget(label("Slower. Turn off if your existing sols are complete and you only want new ones.",
                            "muted", wrap=True))
        row = QHBoxLayout()
        row.addWidget(label("Sols"))
        self.sol_from, self.sol_to = QLineEdit(), QLineEdit()
        for edit, hint in ((self.sol_from, "from"), (self.sol_to, "to")):
            edit.setPlaceholderText(hint)
            edit.setValidator(QIntValidator(0, 100000))
            edit.setFixedWidth(80)
        row.addWidget(self.sol_from)
        row.addWidget(label("to", "muted"))
        row.addWidget(self.sol_to)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(label("Leave blank for all sols.", "muted"))
        self.adv_toggle = link_button("Advanced ▸")
        self.adv_toggle.clicked.connect(self._toggle_advanced)
        lay.addWidget(self.adv_toggle)
        self.adv = QWidget()
        al = QHBoxLayout(self.adv)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(label("Parallel downloads"))
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        al.addWidget(self.workers)
        al.addStretch()
        self.adv.setVisible(False)
        lay.addWidget(self.adv)
        self.locked += [self.check_existing, self.sol_from, self.sol_to, self.workers, self.adv_toggle]
        self.left_col.addWidget(frame)

    def _build_library_card(self) -> QFrame:
        frame, lay = card()
        head = QHBoxLayout()
        head.addWidget(label("LIBRARY STATUS", "cardTitle"))
        head.addStretch()
        self.scanned_label = label("Not scanned yet", "muted")
        head.addWidget(self.scanned_label)
        lay.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(5)
        # Two groups, so what's in the folder isn't confused with what Mars Vista has.
        sections = [("ON DISK", ["Sols", "Photos", "Size"]),
                    ("AVAILABLE ONLINE", ["Latest sol", "To download", "Gaps in your sols"])]
        self.lib_rows = [row for _, rows in sections for row in rows]
        self.lib_cells: dict[tuple[str, str], QLabel] = {}
        self.lib_heads: dict[str, QLabel] = {}
        for c, rover in enumerate(ALL_ROVERS, 1):
            self.lib_heads[rover] = label(rover, "colHead")
            grid.addWidget(self.lib_heads[rover], 0, c)
        tip = ("Only sols that have photos are counted. Sol numbers count every Mars day since landing, but on some "
               "days the rover sent back no images (for example during solar conjunction, when the Sun blocks "
               "communication with Earth).")
        # Shown on the "To download" row, to the right of the last rover column.
        self.lib_note = label("Not every sol has photos, so this can be fewer than the latest sol.", "muted",
                              wrap=True, min_width=170)
        self.lib_note.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.lib_note.setToolTip(tip)
        note_col = len(ALL_ROVERS) + 1
        r = 1
        for title, rows in sections:
            grid.addWidget(label(title, "section"), r, 0, 1, len(ALL_ROVERS) + 1)
            r += 1
            for row_name in rows:
                grid.addWidget(label("   " + row_name, "rowHead"), r, 0)
                for c, rover in enumerate(ALL_ROVERS, 1):
                    cell = label("N/A", "value")
                    self.lib_cells[(row_name, rover)] = cell
                    grid.addWidget(cell, r, c)
                if row_name == "To download":
                    # spans this row and the next, so the wrapped note doesn't stretch one row
                    grid.addWidget(self.lib_note, r, note_col, 2, 1, Qt.AlignmentFlag.AlignTop)
                    for rover in ALL_ROVERS:
                        self.lib_cells[(row_name, rover)].setToolTip(tip)
                r += 1
        grid.setColumnStretch(note_col, 1)
        lay.addLayout(grid)
        self.lib_hint = label("Click Quick scan to check this folder.", "muted")
        lay.addWidget(self.lib_hint)
        btns = QHBoxLayout()
        btns.addStretch()
        self.quick_btn = QPushButton("Quick scan")
        self.quick_btn.setToolTip("Count what's on disk and compare with Mars Vista (2 API requests)")
        self.quick_btn.clicked.connect(self._quick_scan)
        self.full_btn = QPushButton("Full check…")
        self.full_btn.setToolTip("Also find gaps in existing sols (about one API request per sol on disk)")
        self.full_btn.clicked.connect(self._full_check)
        btns.addWidget(self.quick_btn)
        btns.addWidget(self.full_btn)
        lay.addLayout(btns)
        return frame

    def _build_progress_card(self) -> QFrame:
        frame, lay = card("Progress")
        self.phase = label("Idle", "big")
        lay.addWidget(self.phase)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        self.counters: dict[str, QLabel] = {}
        self.counter_heads: dict[str, QLabel] = {}
        items = [("Photos downloaded", 0, 0), ("Already had", 0, 2),
                 ("Data downloaded", 1, 0), ("Speed", 1, 2),
                 ("Photo rate", 2, 0), ("Average speed", 2, 2),
                 ("NASA refused", 3, 0), ("Errors", 3, 2),
                 ("Time left", 4, 0), ("Elapsed", 4, 2)]
        tips = {"Data downloaded": "Size of the photos saved to the folder in this run.",
                "Speed": "Download speed, averaged over a recent window (View > Speed averaged over).",
                "Average speed": "Average download speed since the run started, including waits.",
                "Photo rate": "Photos saved per second over the same window as Speed."}
        for name, r, c in items:
            head = label(name, "rowHead")
            head.setToolTip(tips.get(name, ""))
            self.counter_heads[name] = head
            grid.addWidget(head, r, c)
            v = label("–", "value")
            v.setAlignment(Qt.AlignmentFlag.AlignRight)
            v.setToolTip(tips.get(name, ""))
            self.counters[name] = v
            grid.addWidget(v, r, c + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        lay.addLayout(grid)
        srow = QHBoxLayout()
        self.summary = label("", wrap=True)
        self.summary.setVisible(False)
        srow.addWidget(self.summary, 1)
        self.problems_btn = link_button("Show problems")
        self.problems_btn.setVisible(False)
        self.problems_btn.clicked.connect(self._show_problems)
        srow.addWidget(self.problems_btn)
        lay.addLayout(srow)
        return frame

    def _build_usage_card(self) -> QFrame:
        frame, lay = card("Mars Vista API usage (estimated)")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.hour_bar, self.day_bar = meter(), meter()
        self.hour_lbl, self.day_lbl = label("", "value"), label("", "value")
        for r, (name, bar, lbl) in enumerate((("This hour", self.hour_bar, self.hour_lbl),
                                              ("Today", self.day_bar, self.day_lbl))):
            grid.addWidget(label(name, "rowHead"), r, 0)
            grid.addWidget(bar, r, 1)
            lbl.setMinimumWidth(150)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(lbl, r, 2)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        # Spirit and Opportunity come from NASA's archive, which has no quota, so it gets a count, not a meter.
        nasa = QHBoxLayout()
        nasa.addWidget(label("NASA archive", "rowHead"))
        nasa.addStretch()
        self.pds_lbl = label("", "value")
        nasa.addWidget(self.pds_lbl)
        lay.addLayout(nasa)
        tip = ("Mars Vista counts toward its limits (Curiosity, Perseverance). Spirit and Opportunity come from "
               "NASA's PDS archive, which needs no key and has no request limit; its requests are counted here "
               "only so you can see them. Photo files themselves come from NASA and aren't counted.")
        frame.setToolTip(tip)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QVBoxLayout(self.banner)
        bl.setContentsMargins(12, 8, 12, 8)
        self.banner_title = label("", "value")
        self.banner_text = label("", wrap=True)
        bl.addWidget(self.banner_title)
        bl.addWidget(self.banner_text)
        self.banner.setVisible(False)
        lay.addWidget(self.banner)
        return frame

    def _build_activity_card(self) -> QFrame:
        frame, lay = card()
        head = QHBoxLayout()
        head.addWidget(label("ACTIVITY", "cardTitle"))
        head.addStretch()
        self.log_toggle = link_button("Hide ▾")
        self.log_toggle.clicked.connect(self._toggle_log)
        head.addWidget(self.log_toggle)
        lay.addLayout(head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_view.setMinimumHeight(80)
        lay.addWidget(self.log_view, 1)
        return frame

    # ---- settings ----------------------------------------------------------------------------------------

    def _load_settings_into_widgets(self) -> None:
        s = self.settings
        self.folder_edit.setText(s.folder)
        try:
            self.key_edit.setText(load_api_key())
        except ApiError:
            self.key_edit.setText("")
        self._loading = True
        for name, box in self.rover_boxes.items():
            box.setChecked(name in s.rovers)
            self.camera_panels[name].set_cameras(s.known_cameras.get(name, []), set())
            self._apply_rover_cameras(name)
        self.check_existing.setChecked(s.check_existing)
        self.sol_from.setText(s.sol_from)
        self.sol_to.setText(s.sol_to)
        self.workers.setValue(s.workers)
        self._loading = False
        self._update_camera_toggles()
        self._update_library_columns()
        self._set_log_visible(s.show_log)
        if self.checkpoint:
            self._show_paused_state()

    def _collect_settings(self) -> None:
        s = self.settings
        s.folder = self.folder_edit.text().strip()
        s.rovers = [n for n, b in self.rover_boxes.items() if b.isChecked()]
        for name, panel in self.camera_panels.items():
            if panel.boxes and self.rover_boxes[name].isChecked():
                self.saved_excluded[name] = set(panel.excluded())
            if name in self.saved_excluded:          # an unticked rover keeps the selection it had
                s.excluded_cameras[name] = sorted(self.saved_excluded[name])
        s.check_existing = self.check_existing.isChecked()
        s.sol_from, s.sol_to = self.sol_from.text().strip(), self.sol_to.text().strip()
        s.workers = self.workers.value()

    def _save_settings(self) -> None:
        self._collect_settings()
        try:
            self.settings.save(SETTINGS_PATH)
        except OSError as e:
            self._log(f"Could not save settings: {e}")

    def _save_key(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            return
        try:
            save_api_key(key)
        except Exception as e:  # noqa: BLE001
            self._log(f"Could not save the API key to Credential Manager ({e}); it will be used for this session.")

    # ---- validation and state ----------------------------------------------------------------------------

    def _folder(self) -> str:
        return self.folder_edit.text().strip()

    def _key(self) -> str:
        return self.key_edit.text().strip()

    def _selected_rovers(self) -> list[str]:
        return [n for n, b in self.rover_boxes.items() if b.isChecked()]

    def _validate(self) -> None:
        folder_given = bool(self._folder())
        folder_exists = folder_given and os.path.isdir(self._folder())
        key_ok = bool(self._key())
        for edit, ok in ((self.folder_edit, folder_given), (self.key_edit, key_ok)):
            if edit.property("missing") != (not ok):
                edit.setProperty("missing", not ok)
                repolish(edit)
        self.folder_hint.setVisible(folder_given and not folder_exists)
        idle = self.mode == "idle"
        rovers_ok = bool(self._selected_rovers())
        ready = folder_given and key_ok and rovers_ok
        missing = [text for ok, text in ((folder_given, "choose a photo folder"), (key_ok, "paste your API key"),
                                         (rovers_ok, "tick at least one rover")) if not ok]
        listed = ", ".join(missing[:-1]) + " and " + missing[-1] if len(missing) > 1 else "".join(missing)
        why = f"To start, {listed}." if idle and missing and not self.checkpoint else ""
        self.start_hint.setText(why)
        self.start_hint.setVisible(bool(why))
        self.start_btn.setToolTip(why)
        self.quick_btn.setToolTip(why or "Count what's on disk and compare with Mars Vista (2 API requests)")
        self.full_btn.setToolTip(why or "Also find gaps in existing sols (about one API request per sol on disk)")
        self.preview_btn.setToolTip(
            "Estimate what a download would add, without downloading anything" if key_ok and rovers_ok else
            "To preview, " + ", ".join(t for t in missing if t != "choose a photo folder") + ".")
        self.start_btn.setEnabled(idle and (ready or bool(self.checkpoint)))
        self.start_btn.setText("▶  Resume" if self.checkpoint else "▶  Start")
        self.preview_btn.setEnabled(idle and key_ok and rovers_ok)
        self.quick_btn.setEnabled(idle and ready)
        self.full_btn.setEnabled(idle and ready)
        downloading = self.mode == "download" and not self.cancel.is_set()
        self.pause_btn.setEnabled(downloading)
        self.stop_btn.setEnabled((not idle and not self.cancel.is_set()) or (idle and bool(self.checkpoint)))
        self.stop_btn.setToolTip("End the paused download. Finished photos are kept." if idle and self.checkpoint else
                                 "End this run. Finished photos are kept; the next Start begins from the start.")
        self.open_btn.setEnabled(folder_given)
        for w in self.locked:
            w.setEnabled(idle)
        if hasattr(self, "act_choose"):
            self.act_choose.setEnabled(idle)

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode != "idle":
            self.cancel = threading.Event()
        self._validate()

    # ---- background tasks --------------------------------------------------------------------------------

    def _post(self, kind: str, payload=None) -> None:
        self.events.put((kind, payload))

    def _client(self, key: str) -> RoverSources:
        """Curiosity and Perseverance from Mars Vista; Spirit and Opportunity from NASA's PDS archive."""
        return RoverSources(MarsVistaClient(key, tracker=self.tracker, cancel=self.cancel),
                            PdsClient(cancel=self.cancel, log=lambda m: self._post("log", m),
                                      counter=self.pds_requests))

    def _run_task(self, mode: str, fn) -> None:
        """Run fn() on a background thread. fn's return value arrives as a ("done", (mode, value)) event."""
        self._set_mode(mode)

        def runner():
            try:
                value = fn()
                self._post("done", (mode, value))
            except Cancelled:
                self._post("done", (mode, None))
            except ApiError as e:
                self._post("error", (mode, self._explain_api_error(e)))
            except Exception as e:  # noqa: BLE001
                self._post("error", (mode, f"{type(e).__name__}: {e}"))
                self._post("log", traceback.format_exc())

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    @staticmethod
    def _explain_api_error(e: ApiError) -> str:
        text = str(e)
        if "HTTP 401" in text or "HTTP 403" in text:
            return "Mars Vista didn't accept the API key. Check it and try again."
        return text

    def _load_rover_info(self) -> None:
        """Fetch rover list and camera names (1 request) without blocking the window or changing mode."""
        key = self._key()

        def fetch():
            try:
                client = MarsVistaClient(key, tracker=self.tracker)
                info = {r.name: {"max_sol": latest_sol(r.name, r.max_sol), "cameras": list(r.cameras)} for r in client.rovers()}
                self._post("rovers", info)
            except ApiError as e:
                self._post("api_error", self._explain_api_error(e))
            except Exception as e:  # noqa: BLE001 - offline etc.
                self._post("api_error", f"Couldn't reach Mars Vista: {e}")

        threading.Thread(target=fetch, daemon=True).start()

    # ---- actions -----------------------------------------------------------------------------------------

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose photo folder", self._folder() or os.path.expanduser("~"))
        if folder:
            self.folder_edit.setText(os.path.normpath(folder))

    def _open_folder(self) -> None:
        if self._ensure_folder():          # a missing folder: offers to create it, as Start does
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._folder()))

    def _folder_changed(self) -> None:
        self._validate()
        self._refresh_library()

    def _key_edited(self) -> None:
        if self._key():
            self._save_key()
            self._load_rover_info()

    def _toggle_cameras(self, name: str) -> None:
        panel = self.camera_panels[name]
        panel.setVisible(not panel.isVisible())
        self._update_camera_toggles()

    def _rover_toggled(self, name: str) -> None:
        """A rover's cameras follow its checkbox: unticking it unticks all of its cameras (remembering which
        were ticked), and ticking it again brings that selection back (all cameras if there was none)."""
        if self._loading:
            return
        panel = self.camera_panels[name]
        if not self.rover_boxes[name].isChecked() and panel.counts()[0]:
            self.saved_excluded[name] = set(panel.excluded())
        self._apply_rover_cameras(name)
        self._update_camera_toggles()

    def _apply_rover_cameras(self, name: str) -> None:
        panel = self.camera_panels[name]
        if not self.rover_boxes[name].isChecked():
            panel.set_checked(lambda cam: False)
            return
        excluded = self.saved_excluded.get(name, set())
        if not set(panel.boxes) - excluded:          # nothing would be ticked: use all cameras
            excluded = set()
        panel.set_checked(lambda cam: cam not in excluded)

    def _cameras_changed(self, name: str) -> None:
        """Ticking a camera ticks its rover; unticking a rover's last camera unticks the rover."""
        on, total = self.camera_panels[name].counts()
        box = self.rover_boxes[name]
        if on and not box.isChecked():
            self.saved_excluded[name] = set(self.camera_panels[name].excluded())
            box.setChecked(True)
        elif total and not on and box.isChecked():
            box.setChecked(False)
        self._update_camera_toggles()

    def _update_camera_toggles(self) -> None:
        for name, toggle in self.camera_toggles.items():
            panel = self.camera_panels[name]
            arrow = "▾" if panel.isVisible() else "▸"
            on, total = panel.counts()
            toggle.setText(f"Cameras {arrow}  ({on} of {total})" if total else f"Cameras {arrow}  (loading…)")

    def _toggle_advanced(self) -> None:
        self.adv.setVisible(not self.adv.isVisible())
        self.adv_toggle.setText("Advanced ▾" if self.adv.isVisible() else "Advanced ▸")

    def _toggle_log(self) -> None:
        self._set_log_visible(not self.log_view.isVisible())

    def _set_log_visible(self, visible: bool) -> None:
        self.log_view.setVisible(visible)
        self.log_toggle.setText("Hide ▾" if visible else "Show ▸")
        if hasattr(self, "act_log") and self.act_log.isChecked() != visible:
            self.act_log.setChecked(visible)
        self.settings.show_log = visible

    # ---- menus -------------------------------------------------------------------------------------------

    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_m = mb.addMenu("&File")
        settings_m = mb.addMenu("&Settings")
        view_m = mb.addMenu("&View")
        help_m = mb.addMenu("&Help")

        self.act_choose = file_m.addAction("Choose photo folder…")
        self.act_choose.setShortcut(QKeySequence("Ctrl+O"))
        self.act_choose.triggered.connect(self._browse)
        open_act = file_m.addAction("Open photo folder")
        open_act.triggered.connect(self._open_folder)
        file_m.addSeparator()
        quit_act = file_m.addAction("Quit")
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(self.close)

        self._choice_menu(view_m, "Data units", [("Auto", "auto"), ("KB", "KB"), ("MB", "MB"), ("GB", "GB"),
                                                 ("TB", "TB")], "data_unit")
        self._choice_menu(view_m, "Speed units", [("Auto", "auto"), ("KB/s", "KB/s"), ("MB/s", "MB/s"),
                                                  ("Mbit/s", "Mbit/s")], "speed_unit")
        self._choice_menu(view_m, "Speed averaged over", [(f"{n} seconds", n) for n in SPEED_WINDOWS],
                          "speed_window")
        view_m.addSeparator()
        self._choice_menu(view_m, "Theme", [("Match Windows", "system"), ("Light", "light"), ("Dark", "dark")],
                          "theme", after=self._apply_theme)
        self.act_log = view_m.addAction("Show activity log")
        self.act_log.setCheckable(True)
        self.act_log.setChecked(self.settings.show_log)
        self.act_log.toggled.connect(self._set_log_visible)

        settings_m.setToolTipsVisible(True)
        self.act_retry = settings_m.addAction("Retry photos NASA refused (next run)")
        self.act_retry.setCheckable(True)
        self.act_retry.setToolTip("Photos NASA refused (HTTP 403/404) are normally skipped on later runs. "
                                  "Tick this to try them all again on the next run.")
        folder_act = settings_m.addAction("Open settings folder")
        folder_act.triggered.connect(self._open_settings_folder)

        site = help_m.addAction("Mars Vista website")
        site.triggered.connect(lambda: QDesktopServices.openUrl(QUrl("https://marsvista.dev")))
        about = help_m.addAction("About Rover Image Scraper")
        about.triggered.connect(self._about)

    def _choice_menu(self, parent, title: str, options: list, attr: str, after=None) -> None:
        """A submenu of mutually exclusive choices stored in self.settings.<attr>."""
        menu = parent.addMenu(title)
        group = QActionGroup(menu)
        group.setExclusive(True)
        for text, value in options:
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(getattr(self.settings, attr) == value)
            group.addAction(act)
            act.triggered.connect(lambda _=False, v=value: self._set_view_option(attr, v, after))

    def _set_view_option(self, attr: str, value, after=None) -> None:
        setattr(self.settings, attr, value)
        self._save_settings()
        self._update_counters()
        if after:
            after()

    def _apply_theme(self) -> None:
        choice = self.settings.theme
        dark = system_is_dark(self.app) if choice == "system" else choice == "dark"
        apply_theme(self.app, DARK if dark else LIGHT)

    def _open_settings_folder(self) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(STATE_DIR))

    def _about(self) -> None:
        QMessageBox.about(self, "About Rover Image Scraper",
                          "<b>Rover Image Scraper</b><br><br>Downloads raw photos from NASA's Mars rovers, "
                          "adding only what's missing. Curiosity and Perseverance come from the Mars Vista API; "
                          "Spirit and Opportunity from NASA's Planetary Data System archive.<br><br>"
                          'Photos: NASA/JPL-Caltech. Data: <a href="https://marsvista.dev">Mars Vista</a>.')

    def _stop(self) -> None:
        if self.mode == "idle" and self.checkpoint:     # stopping a paused download: forget where it was
            self._clear_checkpoint()
            self.phase.setText("Stopped · finished photos are kept")
            self._log("Paused download stopped. Finished photos are kept.")
            return
        self.pausing = False
        self.cancel.set()
        self.phase.setText("Stopping… finishing current photos")
        self._validate()

    def _pause(self) -> None:
        self.pausing = True
        self.cancel.set()
        self.phase.setText("Pausing… finishing current photos")
        self._validate()

    def _ensure_folder(self, folder: str | None = None) -> bool:
        """True if the photo folder exists, or the user agreed to create it and it was created."""
        folder = folder or self._folder()
        if not folder:
            QMessageBox.warning(self, "No photo folder", "Choose a folder to save photos in first.")
            return False
        if os.path.isdir(folder):
            return True
        if os.path.exists(folder):
            QMessageBox.warning(self, "Not a folder", f"{folder}\n\nThis path is a file, not a folder.")
            return False
        answer = QMessageBox.question(self, "Create folder?",
                                      f"{folder}\n\nThis folder doesn't exist. Create it?")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            os.makedirs(folder)
        except OSError as e:
            QMessageBox.warning(self, "Couldn't create the folder", f"{folder}\n\n{e}")
            return False
        self._log(f"Created folder {folder}")
        self._validate()
        self._refresh_library()
        return True

    def _update_library_columns(self) -> None:
        """Library status shows a column for each ticked rover (Curiosity and Perseverance always)."""
        if not hasattr(self, "lib_heads"):
            return
        for rover in ALL_ROVERS:
            show = rover in MAIN_ROVERS or self.rover_boxes[rover].isChecked()
            self.lib_heads[rover].setVisible(show)
            for row in self.lib_rows:
                self.lib_cells[(row, rover)].setVisible(show)

    def _camera_filter(self) -> dict[str, set[str]] | None:
        out = {}
        for name in self._selected_rovers():
            panel = self.camera_panels[name]
            if panel.boxes and panel.excluded():
                out[name] = {c for c, b in panel.boxes.items() if b.isChecked()}
        return out or None

    def _sol_bounds(self) -> tuple[int, int | None]:
        """Sol range typed by the user (read on the GUI thread, before a task starts)."""
        lo = int(self.sol_from.text()) if self.sol_from.text() else 0
        hi = int(self.sol_to.text()) if self.sol_to.text() else None
        return lo, hi

    def _sol_range(self, rover: str, bounds: tuple[int, int | None]) -> tuple[int, int]:
        lo, hi = bounds
        # NASA PDS (Spirit, Opportunity) can have sols past Mars Vista's latest, so don't cap those.
        max_sol = 10 ** 6 if is_pds_rover(rover) else self.rover_info.get(rover, {}).get("max_sol", 10 ** 6)
        return lo, (max_sol if hi is None else min(hi, max_sol))

    def _confirm_existing_folder(self) -> bool:
        folder = os.path.normcase(os.path.abspath(self._folder()))
        has_data = any(os.path.isdir(os.path.join(self._folder(), r)) for r in self._selected_rovers())
        if not has_data or folder in self.settings.confirmed_folders:
            return True
        answer = QMessageBox.question(
            self, "Folder already has photos",
            "This folder already contains rover photos.\n\nOnly missing photos will be added. Existing files are "
            "never overwritten, renamed or deleted.\n\nContinue?")
        if answer == QMessageBox.StandardButton.Yes:
            self.settings.confirmed_folders.append(folder)
            self._save_settings()
            return True
        return False

    def _run_params(self) -> dict:
        """The choices that define a download, in a form that can be saved and compared."""
        cams = self._camera_filter()
        return {"folder": os.path.normcase(os.path.abspath(self._folder())) if self._folder() else "",
                "rovers": self._selected_rovers(),
                "cameras": {r: sorted(c) for r, c in cams.items()} if cams else None,
                "new_only": not self.check_existing.isChecked(),
                "bounds": list(self._sol_bounds())}

    def _start_download(self) -> None:
        cp = self.checkpoint
        if cp:
            if cp["params"] != self._run_params():
                box = QMessageBox(self)
                box.setWindowTitle("Paused download")
                box.setText(f"You have a paused download ({cp['rover']} sol {cp['sol']:,}).\n\nYour choices have "
                            f"changed since it was paused. Resume it with its original choices, or start a new "
                            f"download with the current ones? (Starting new discards the paused position; "
                            f"downloaded photos are kept.)")
                resume_btn = box.addButton("Resume paused", QMessageBox.ButtonRole.AcceptRole)
                new_btn = box.addButton("Start new", QMessageBox.ButtonRole.DestructiveRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                if box.clickedButton() is new_btn:
                    self._clear_checkpoint()
                    cp = None
                elif box.clickedButton() is not resume_btn:
                    return
        if cp:
            self._resume_download(cp)
            return
        if not self._ensure_folder() or not self._confirm_existing_folder():
            return
        self._save_settings()
        self._save_key()
        dest, rovers, cameras = self._folder(), self._selected_rovers(), self._camera_filter()
        new_only, workers = not self.check_existing.isChecked(), self.workers.value()
        key, bounds = self._key(), self._sol_bounds()
        self.run_params = self._run_params()
        retry_refused = self.act_retry.isChecked()
        if retry_refused:
            self.act_retry.setChecked(False)      # one run only
            self._log("Retrying photos NASA refused before (Settings > Retry photos NASA refused).")
        self.report = Report()
        report = self.report
        self.summary.setVisible(False)
        self.problems_btn.setVisible(False)
        self.speed_samples.clear()
        self.pos = {}
        self.run_started, self.run_ended = time.time(), 0.0
        self._log(f"=== Download started {time.strftime('%H:%M')}: {', '.join(rovers)}"
                  + (" (new sols only)" if new_only else " (checking existing sols for gaps)"))

        def work():
            client = self._client(key)
            if not self.rover_info:
                self.rover_info = {r.name: {"max_sol": latest_sol(r.name, r.max_sol), "cameras": list(r.cameras)}
                                   for r in client.rovers()}
            unavailable = Unavailable(UNAVAILABLE_PATH, ignore_existing=retry_refused)
            scraper = Scraper(client, dest, log=lambda m: self._post("log", m), cancel=self.cancel,
                              unavailable=unavailable, workers=workers, cameras=cameras,
                              progress=lambda e: self._post("progress", e))
            for i, rover in enumerate(rovers, 1):
                if self.cancel.is_set():
                    break
                lo, hi = self._sol_range(rover, bounds)
                self._post("progress", {"event": "rover_index", "index": i, "count": len(rovers)})
                self._post("log", f"== {rover}: sols {lo}..{hi}" + (" (new sols only)" if new_only else ""))
                scraper.run(rover, lo, hi, report, new_only=new_only)
            unavailable.save()
            self.tracker.save()
            return report

        self._run_task("download", work)

    def _resume_download(self, cp: dict) -> None:
        params = cp["params"]
        dest = cp["dest"]
        if not self._ensure_folder(dest):
            return
        self._save_settings()
        self._save_key()
        rovers, new_only = params["rovers"], params["new_only"]
        cameras = {r: set(c) for r, c in params["cameras"].items()} if params["cameras"] else None
        bounds = tuple(params["bounds"])
        key, workers = self._key(), self.workers.value()
        self.run_params = params
        self.report = report_from_dict(cp["report"])
        report = self.report
        self.summary.setVisible(False)
        self.problems_btn.setVisible(False)
        self.speed_samples.clear()
        self.pos = {"rover": cp["rover"], "rover_index": cp["rover_index"], "sol": cp["sol"],
                    "sol_min": cp["sol_min"]}
        self.run_started, self.run_ended = time.time() - cp.get("elapsed", 0), 0.0
        self._log(f"=== Download resumed {time.strftime('%H:%M')} at {cp['rover']} sol {cp['sol']:,}")

        def work():
            client = self._client(key)
            if not self.rover_info:
                self.rover_info = {r.name: {"max_sol": latest_sol(r.name, r.max_sol), "cameras": list(r.cameras)}
                                   for r in client.rovers()}
            unavailable = Unavailable(UNAVAILABLE_PATH)
            scraper = Scraper(client, dest, log=lambda m: self._post("log", m), cancel=self.cancel,
                              unavailable=unavailable, workers=workers, cameras=cameras,
                              progress=lambda e: self._post("progress", e))
            for i, rover in enumerate(rovers, 1):
                if self.cancel.is_set() or i < cp["rover_index"]:
                    continue
                lo, hi = self._sol_range(rover, bounds)
                self._post("progress", {"event": "rover_index", "index": i, "count": len(rovers)})
                if i == cp["rover_index"]:
                    self._post("log", f"== {rover}: resuming at sol {cp['sol']:,} (sols {cp['sol_min']}..{hi})")
                    scraper.run(rover, cp["sol_min"], hi, report, resume_from=cp["sol"])
                else:
                    self._post("log", f"== {rover}: sols {lo}..{hi}" + (" (new sols only)" if new_only else ""))
                    scraper.run(rover, lo, hi, report, new_only=new_only)
            unavailable.save()
            self.tracker.save()
            return report

        self._clear_checkpoint()
        self._run_task("download", work)

    # ---- pause checkpoint --------------------------------------------------------------------------------

    def _save_checkpoint(self) -> None:
        if not self.pos.get("rover") or self.pos.get("sol") is None or self.report is None:
            return
        cp = {"params": self.run_params, "dest": self.run_params["folder"], "rover": self.pos["rover"],
              "rover_index": self.pos.get("rover_index", 1), "sol": self.pos["sol"],
              "sol_min": self.pos.get("sol_min", 0), "index": self.progress_state.get("index", 0),
              "total": self.progress_state.get("total", 0), "elapsed": time.time() - self.run_started,
              "report": report_to_dict(self.report), "saved_at": time.time()}
        self.checkpoint = cp

    def _clear_checkpoint(self) -> None:
        self.checkpoint = None
        self._validate()

    def _show_paused_state(self) -> None:
        cp = self.checkpoint
        total = cp.get("total") or 0
        where = f"Paused · {cp['rover']} sol {cp['sol']:,}"
        if total:
            where += f" ({cp.get('index', 0):,} of {total:,})"
            self.bar.setValue(int(1000 * cp.get("index", 0) / total))
        self.phase.setText(where + " · click Resume to continue")
        self.report = report_from_dict(cp["report"])
        self.run_ended = time.time()
        self.run_started = self.run_ended - cp.get("elapsed", 0)
        self._update_counters()
        self._validate()

    def _quick_scan(self) -> None:
        if self._ensure_folder():
            self._run_scan(full=False)

    def _full_check(self) -> None:
        if not self._ensure_folder():
            return
        rovers = self._selected_rovers()
        existing = sum((self.store.get(self._folder()).rovers.get(r) or RoverStatus()).sols_on_disk or 0
                       for r in rovers)
        if not existing:
            existing = sum(len([e for e in os.scandir(os.path.join(self._folder(), r)) if e.name.isdigit()])
                           for r in rovers if os.path.isdir(os.path.join(self._folder(), r)))
        usage = self.tracker.usage()
        answer = QMessageBox.question(
            self, "Full check",
            f"A full check compares every sol already on disk with Mars Vista to find missing photos.\n\n"
            f"It uses up to about {existing:,} API requests (you've used {usage.hour:,} of "
            f"{usage.hourly_limit:,} this hour) and can take 30-45 minutes for a large folder. Nothing is "
            f"downloaded.\n\nContinue?")
        if answer == QMessageBox.StandardButton.Yes:
            self._run_scan(full=True)

    def _run_scan(self, full: bool) -> None:
        self._save_settings()
        self._save_key()
        dest, rovers, key = self._folder(), self._selected_rovers(), self._key()
        self.report = None
        self.summary.setVisible(False)
        self.problems_btn.setVisible(False)
        self._log(f"=== {'Full check' if full else 'Quick scan'} started {time.strftime('%H:%M')}")

        def work():
            client = self._client(key)
            unavailable = Unavailable(UNAVAILABLE_PATH)
            for rover in rovers:
                self._post("progress", {"event": "scan_start", "rover": rover, "full": full})
                if full:
                    status = full_check(client, dest, rover, unavailable, log=lambda m: self._post("log", m),
                                        progress=lambda e: self._post("progress", e), cancel=self.cancel)
                else:
                    status = quick_scan(client, dest, rover, progress=lambda e: self._post("progress", e),
                                        cancel=self.cancel)
                self.store.put_rover(dest, rover, status)
                self._post("scanned", rover)
            self.tracker.save()
            return True

        self._run_task("full" if full else "scan", work)

    def _preview(self) -> None:
        self._save_settings()
        dest, rovers, check_existing = self._folder(), self._selected_rovers(), self.check_existing.isChecked()
        filtered = self._camera_filter() is not None
        folder_status = self.store.get(dest)
        key, bounds = self._key(), self._sol_bounds()
        self._preview_filtered = filtered
        self._preview_check_existing = check_existing

        def work():
            client = self._client(key)
            if not self.rover_info:
                self.rover_info = {r.name: {"max_sol": latest_sol(r.name, r.max_sol), "cameras": list(r.cameras)}
                                   for r in client.rovers()}
            rows = []
            for rover in rovers:
                manifest = client.manifest(rover)
                lo, hi = self._sol_range(rover, bounds)
                last = last_sol_on_disk(dest, rover)
                last = -1 if last is None else last
                new = {s: v for s, v in manifest.items() if s > last and lo <= s <= hi}
                existing = [s for s in manifest if s <= last and lo <= s <= hi]
                if is_pds_rover(rover):     # NASA PDS: free of Mars Vista quota; photos counted only per sol
                    row = {"rover": rover, "new_sols": len(new), "new_photos": None, "requests": 0, "pds": True}
                else:
                    row = {"rover": rover, "new_sols": len(new),
                           "new_photos": sum(v.total_photos for v in new.values()),
                           "requests": 1 + sum(math.ceil(v.total_photos / 100) for v in new.values())}
                if check_existing:
                    st = folder_status.rovers.get(rover)
                    known = st is not None and st.gap_sols is not None and not folder_status.outdated
                    row["gap_photos"] = st.gap_photos if known else None
                    if not row.get("pds"):
                        row["requests"] += len(existing) + ((st.gap_sols or 0) * 2 if known else 0)
                rows.append(row)
            return rows

        self._run_task("preview", work)

    # ---- event handling (GUI thread) ---------------------------------------------------------------------

    def _tick(self) -> None:
        lines = []
        for _ in range(2000):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                lines.append(payload)
            elif kind == "progress":
                self._on_progress(payload)
            elif kind == "done":
                self._on_done(*payload)
            elif kind == "error":
                self._on_error(*payload)
            elif kind == "rovers":
                self._on_rovers(payload)
            elif kind == "api_error":
                self.api_error = payload
                self._log(payload)
            elif kind == "scanned":
                self._refresh_library()
        if lines:
            self._log("\n".join(lines))
        self._update_live()

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_rovers(self, info: dict) -> None:
        self.api_error = ""
        self.rover_info = info
        for name in ALL_ROVERS:
            cams = info.get(name, {}).get("cameras")
            if cams:
                self.settings.known_cameras[name] = cams
                panel = self.camera_panels[name]
                if panel.boxes and self.rover_boxes[name].isChecked():
                    self.saved_excluded[name] = set(panel.excluded())
                panel.set_cameras(cams, set())
                self._apply_rover_cameras(name)
        self._update_camera_toggles()
        self._refresh_library()

    def _on_progress(self, e: dict) -> None:
        kind = e.get("event")
        ps = self.progress_state
        if kind == "rover_index":
            ps["rover_index"], ps["rover_count"] = e["index"], e["count"]
            self.pos["rover_index"] = e["index"]
        elif kind == "rover":
            ps.update(rover=e["rover"], total=e["total"], index=0, sol=None, camera=None)
            self.rover_started = time.time()
            if self.mode == "download":
                self.pos.update(rover=e["rover"], sol_min=e["sol_min"])
        elif kind == "sol":
            ps.update(rover=e["rover"], sol=e["sol"], index=e["index"], total=e["total"], camera=None)
            if self.mode == "download":
                self.pos["sol"] = e["sol"]
        elif kind == "camera":
            ps["camera"] = e["camera"]
        elif kind == "scan_start":
            ps.clear()
            ps.update(scan_rover=e["rover"], full=e["full"])
        elif kind == "scan":
            ps.update(scan_rover=e["rover"], scan_index=e["index"], scan_total=e["total"])

    def _on_done(self, mode: str, value) -> None:
        cancelled = self.cancel.is_set()
        paused = mode == "download" and cancelled and self.pausing
        if mode == "download":
            r = value or self.report
            if r is not None:
                r.cancelled = False
                self._show_summary(r, cancelled, paused)
            if self.run_params.get("folder"):
                self.store.mark_outdated(self.run_params["folder"])
            self._log(f"=== Download {'paused' if paused else 'stopped' if cancelled else 'finished'} "
                      f"{time.strftime('%H:%M')}")
        elif mode in ("scan", "full"):
            self._log(f"=== {'Full check' if mode == 'full' else 'Quick scan'} "
                      f"{'stopped' if cancelled else 'finished'} {time.strftime('%H:%M')}")
        if mode == "download":
            self._update_progress()             # final counter values
            self.run_ended = time.time()
            if paused:
                self._save_checkpoint()
            else:
                self._clear_checkpoint()
        self.pausing = False
        self.progress_state = {}
        self._set_mode("idle")
        if paused and self.checkpoint:
            self._show_paused_state()
        elif mode in ("download", "scan", "full"):
            self.phase.setText("Stopped" if cancelled else "Finished")
            self.bar.setValue(self.bar.value() if cancelled else 1000)
        else:
            self.phase.setText("Idle")
        self._refresh_library()
        if mode == "preview" and value:
            self._show_preview(value)

    def _on_error(self, mode: str, message: str) -> None:
        if mode == "download":
            self.run_ended = time.time()
            if self.pausing:
                self._save_checkpoint()
        self.pausing = False
        self.api_error = message
        self._log(f"ERROR: {message}")
        self.progress_state = {}
        self._set_mode("idle")
        self.phase.setText("Stopped: error")
        QMessageBox.warning(self, "Something went wrong", message)

    def _show_summary(self, r: Report, cancelled: bool, paused: bool = False) -> None:
        state = "Paused" if paused else "Stopped" if cancelled else "Finished"
        text = (f"<b>{state}.</b> Downloaded {r.downloaded:,} photos; {r.already_present:,} were already on disk; "
                f"NASA refused {r.newly_unavailable + r.known_unavailable:,}. {r.sols_checked:,} sols checked.")
        if r.problems:
            text += f" <span style='color:#B45309'>{len(r.problems):,} problem(s).</span>"
        self.summary.setText(text)
        self.summary.setVisible(True)
        self.problems_btn.setVisible(bool(r.problems))

    def _show_problems(self) -> None:
        if not self.report or not self.report.problems:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Problems")
        dlg.resize(760, 420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label("These photos or sols were skipped. Starting again retries them.", "muted"))
        view = QPlainTextEdit("\n".join(self.report.problems))
        view.setReadOnly(True)
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _show_preview(self, rows: list[dict]) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Preview")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(label("This run would download about:", "big"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        for c, head in enumerate(("", "New sols", "New photos", "Gap fills", "Size")):
            grid.addWidget(label(head, "rowHead"), 0, c)
        total_photos = total_requests = 0
        unknown_gaps = any_pds = False
        for r, row in enumerate(rows, 1):
            gaps = row.get("gap_photos")
            if self._preview_check_existing and gaps is None:
                unknown_gaps = True
            total_requests += row["requests"]
            gap_text = "not included" if not self._preview_check_existing else (
                "unknown" if gaps is None else f"up to {gaps:,}")
            if row["new_photos"] is None:       # NASA PDS rover: counted only while downloading
                any_pds = True
                photo_text, size_text = "counted as it goes", "–"
            else:
                photos = row["new_photos"] + (gaps or 0)
                total_photos += photos
                photo_text, size_text = f"{row['new_photos']:,}", f"≈ {fmt_size(photos * AVG_PHOTO_MB * 1024 ** 2)}"
            for c, text in enumerate((row["rover"], f"{row['new_sols']:,}", photo_text, gap_text, size_text)):
                grid.addWidget(label(text, "value" if c else "colHead"), r, c)
        lay.addLayout(grid)
        notes = [f"Needs about {total_requests:,} Mars Vista API requests (the limit is 10,000 per hour)."]
        if total_requests > 10_000:
            notes.append(f"Expect about {total_requests // 10_000} pause(s) for the rate limit. "
                         "The run continues automatically after each one.")
        if unknown_gaps:
            notes.append("Gaps in existing sols aren't known yet. Run Full check to include them in the estimate.")
        if any_pds:
            notes.append("Spirit and Opportunity come from NASA's archive, which doesn't publish photo counts "
                         "per sol, so their photos are counted as the download goes. They use no Mars Vista "
                         "requests.")
        if self._preview_filtered:
            notes.append("With some cameras unticked, photo counts are upper bounds.")
        for n in notes:
            lay.addWidget(label(n, "muted", wrap=True))
        bb = QDialogButtonBox()
        bb.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        start = bb.addButton("Start now", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setObjectName("primary")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._start_download()

    # ---- live display ------------------------------------------------------------------------------------

    def _update_live(self) -> None:
        usage = self.tracker.usage()
        self.hour_bar.setValue(int(1000 * min(1, usage.hour / usage.hourly_limit)))
        self.day_bar.setValue(int(1000 * min(1, usage.day / usage.daily_limit)))
        self.hour_lbl.setText(f"{usage.hour:,} / {usage.hourly_limit:,}")
        self.day_lbl.setText(f"{usage.day:,} / {usage.daily_limit:,}")
        self.pds_lbl.setText(f"{self.pds_requests.count:,} requests this session · no limit")

        waiting = usage.waiting and self.mode != "idle"
        nasa_busy = NASA_STATUS.busy_recently() and self.mode == "download"
        if waiting:
            left = usage.waiting_until - time.time()
            resume = time.strftime("%H:%M", time.localtime(usage.waiting_until))
            self.banner_title.setText(f"⏸  Waiting for Mars Vista rate limit ({usage.wait_reason}). "
                                      f"Resumes in {fmt_duration(left)}, about {resume}.")
            self.banner_text.setText("Mars Vista allows 10,000 requests per hour and 100,000 per day. "
                                     "This isn't an error: the run continues automatically.")
        elif nasa_busy:
            self.banner_title.setText("⏸  NASA's image servers are busy. Retrying automatically.")
            self.banner_text.setText("Some downloads are being retried. No action needed.")
        self.banner.setVisible(waiting or nasa_busy)
        if self.bar.property("paused") != waiting:
            self.bar.setProperty("paused", waiting)
            repolish(self.bar)

        if self.mode == "idle" and not self._key():
            self._set_pill("● Mars Vista: no API key", "idle")
        elif self.api_error and self.mode == "idle":
            self._set_pill("● Mars Vista: problem", "err")
            self.pill.setToolTip(self.api_error)
        elif waiting:
            self._set_pill("● Mars Vista: waiting", "warn")
        elif nasa_busy:
            self._set_pill("● NASA busy", "warn")
        elif self.rover_info or self.mode != "idle":
            self._set_pill("● Mars Vista: OK", "ok")
        else:
            self._set_pill("● Mars Vista: not connected", "idle")

        self._update_progress()

    def _set_pill(self, text: str, tone: str) -> None:
        if self.pill.text() != text:
            self.pill.setText(text)
        if self.pill.property("tone") != tone:
            self.pill.setProperty("tone", tone)
            repolish(self.pill)
        if tone != "err":
            self.pill.setToolTip("")

    def _update_progress(self) -> None:
        ps = self.progress_state
        if self.mode in ("scan", "full") and ps.get("scan_rover"):
            rover = ps.get("rover") or ps["scan_rover"]
            if ps.get("full") and ps.get("total"):
                self.phase.setText(f"Full check · {rover} · sol {fmt(ps.get('sol'))} "
                                   f"({ps.get('index', 0):,} of {ps['total']:,})")
                self.bar.setValue(int(1000 * ps.get("index", 0) / max(1, ps["total"])))
            elif ps.get("scan_total"):
                self.phase.setText(f"Scanning {ps['scan_rover']} · sol folders {ps['scan_index']:,} of "
                                   f"{ps['scan_total']:,}")
                self.bar.setValue(int(1000 * ps["scan_index"] / max(1, ps["scan_total"])))
            else:
                self.phase.setText(f"Scanning {ps['scan_rover']}…")
        elif self.mode == "download" and not self.cancel.is_set():
            if ps.get("total"):
                frac = ps.get("index", 0) / max(1, ps["total"])
                where = f"{ps.get('rover')} · sol {fmt(ps.get('sol'))} ({ps.get('index', 0):,} of {ps['total']:,})"
                if ps.get("camera"):
                    where += f" · {ps['camera']}"
                if ps.get("rover_count", 1) > 1:
                    where += f"   ·   rover {ps.get('rover_index', 1)} of {ps['rover_count']}"
                self.phase.setText(where)
                self.bar.setValue(int(1000 * frac))
            else:
                self.phase.setText("Starting…")
        elif self.mode == "preview":
            self.phase.setText("Estimating…")

        self._update_counters()

    def _update_counters(self) -> None:
        """Counters for the current download, or the last one after it ends (so unit changes still apply)."""
        r = self.report
        if r is None or self.mode in ("scan", "full", "preview"):
            return
        running = self.mode == "download"
        now = time.time() if running else (self.run_ended or time.time())
        s = self.settings
        window = s.speed_window
        self.counter_heads["Speed"].setText(f"Speed ({window} s)")
        if running:
            self.speed_samples.append((now, r.bytes_received, r.downloaded))
            while len(self.speed_samples) > 2 and now - self.speed_samples[1][0] >= window:
                self.speed_samples.popleft()
        if running and len(self.speed_samples) >= 2 and now - self.speed_samples[0][0] >= 1:
            t0, b0, d0 = self.speed_samples[0]
            span = now - t0
            byte_rate, photo_rate = (r.bytes_received - b0) / span, (r.downloaded - d0) / span
            self.counters["Speed"].setText(fmt_rate(byte_rate, s.speed_unit))
            self.counters["Photo rate"].setText(f"{photo_rate:,.1f} photos/s")
        elif not running:
            self.counters["Speed"].setText("–")
            self.counters["Photo rate"].setText("–")
        elapsed = now - self.run_started if self.run_started else 0
        self.counters["Elapsed"].setText(fmt_duration(elapsed) if elapsed else "–")
        self.counters["Average speed"].setText(fmt_rate(r.bytes_received / elapsed, s.speed_unit)
                                               if elapsed >= 1 else "–")
        self.counters["Photos downloaded"].setText(f"{r.downloaded:,}")
        self.counters["Data downloaded"].setText(fmt_bytes(r.bytes_saved, s.data_unit))
        self.counters["Already had"].setText(f"{r.already_present:,}")
        self.counters["NASA refused"].setText(f"{r.newly_unavailable + r.known_unavailable:,}")
        errors = self.counters["Errors"]
        errors.setText(f"{len(r.problems):,}")
        tone = "err" if r.problems else ""
        if errors.property("tone") != tone:
            errors.setProperty("tone", tone)
            repolish(errors)
        ps = self.progress_state
        frac = ps.get("index", 0) / max(1, ps.get("total", 0) or 1)
        rover_elapsed = now - self.rover_started if self.rover_started else 0
        if running and 0.01 < frac < 1 and rover_elapsed > 20:
            rest = " (this rover)" if ps.get("rover_count", 1) > 1 else ""
            self.counters["Time left"].setText(f"~{fmt_duration(rover_elapsed * (1 - frac) / frac)}{rest}")
        else:
            self.counters["Time left"].setText("–")

    # ---- library status ----------------------------------------------------------------------------------

    def _refresh_library(self) -> None:
        folder = self._folder()
        status = self.store.get(folder) if folder else None
        scanned = [s.scanned_at for s in (status.rovers.values() if status else []) if s.scanned_at]
        has_new = False
        for rover in ALL_ROVERS:
            st = status.rovers.get(rover) if status else None
            st = st or RoverStatus()
            latest = st.latest_sol if st.latest_sol is not None else self.rover_info.get(rover, {}).get("max_sol")
            sols = "N/A" if st.sols_on_disk is None else (
                f"{st.sols_on_disk:,}" + (f"  ({st.first_sol:,}–{st.last_sol:,})" if st.sols_on_disk else ""))
            if st.sols_on_disk == 0:
                gaps = "None (folder empty)"
            elif st.gap_sols is None:
                gaps = "Not checked" if st.scanned_at else "N/A"
            elif st.gap_sols == 0:
                gaps = "None found"
            else:
                gaps = f"{st.gap_sols:,} sols · up to {st.gap_photos:,}"
            new = "N/A" if st.new_sols is None else (
                f"{st.new_sols:,} sols" if st.new_photos is None else f"{st.new_sols:,} sols · ~{st.new_photos:,}")
            values = {"Sols": sols, "Photos": fmt(st.photos), "Size": fmt_size(st.bytes),
                      "Latest sol": fmt(latest), "To download": new, "Gaps in your sols": gaps}
            for row, text in values.items():
                self.lib_cells[(row, rover)].setText(text)
            if st.new_sols is not None:
                has_new = True
            disk = "not scanned" if st.sols_on_disk is None else f"{st.sols_on_disk:,} sols on disk"
            self.rover_status[rover].setText(disk + (f" · latest {latest:,}" if latest is not None else ""))
        self.lib_note.setVisible(has_new)      # only once "To download" shows numbers
        if not scanned:
            self.scanned_label.setText("Not scanned yet")
            self.lib_hint.setText("Click Quick scan to check this folder." if folder else
                                  "Choose a photo folder, then click Quick scan.")
            self.lib_hint.setVisible(True)
        else:
            outdated = status.outdated
            self.scanned_label.setText(("From before your last download · Quick scan to update" if outdated else
                                        f"Last scanned: {fmt_when(max(scanned))}"))
            self.scanned_label.setToolTip(
                "These numbers were counted before your last download, so they don't include the photos it "
                "added. Click Quick scan to count again." if outdated else "")
            self.scanned_label.setProperty("tone", "warn" if outdated else "")
            repolish(self.scanned_label)
            self.lib_hint.setVisible(False)

    # ---- closing -----------------------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.mode != "idle":
            answer = QMessageBox.question(self, "Stop and quit?",
                                          "A task is running. Stop it and quit? Finished photos are kept.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.pausing = False
            self.cancel.set()
            if self.worker:
                self.worker.join(timeout=30)
        self._save_settings()
        self.tracker.save()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Rover Image Scraper")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))

    win = MainWindow(app)
    win._apply_theme()
    try:   # follow Windows light/dark changes while "Match Windows" is chosen
        app.styleHints().colorSchemeChanged.connect(
            lambda *_: win._apply_theme() if win.settings.theme == "system" else None)
    except AttributeError:
        pass
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

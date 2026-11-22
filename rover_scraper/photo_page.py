"""The Photos page: browse the photo folder by rover, sol and camera, with thumbnails, a preview and details, and a
full-size view with zoom. It only reads the photo folder.

Thumbnails are made on background threads, shrunk as they're read (fast for JPEG), and kept in memory only, so
nothing is written to disk.
"""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
import time
from typing import Callable

from PySide6.QtCore import (QAbstractListModel, QEvent, QModelIndex, QObject, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QImage, QImageReader, QIntValidator, QPainter, QPalette,
                           QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QPushButton, QSizePolicy, QSlider, QStackedWidget, QStyle,
                               QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from photo_library import (PhotoFile, camera_name, cameras_in, earth_date, photo_from_path, photos_in, rovers_in,
                           sol_photo_count, sols_in)

THUMB_MIN, THUMB_MAX = 64, 256
NEWEST_MAX = 2000                  # photos kept in "Newest Downloads"


def count_text(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def fmt_file_size(n: int) -> str:
    for unit, power in (("GB", 3), ("MB", 2), ("KB", 1)):
        if n >= 1024 ** power:
            v = n / 1024 ** power
            return f"{v:,.1f} {unit}" if v < 100 else f"{v:,.0f} {unit}"
    return f"{n:,} B"


def fmt_date(d) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


def show_in_folder(path: str) -> None:
    """Open the system's file manager at the file (selected, where the system can do that)."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


# ---- loading images in the background --------------------------------------------------------------------

def read_image(path: str, size: int | None) -> QImage:
    """The image at path, shrunk while reading so its longer side is at most size (None: full size)."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    if size:
        s = reader.size()
        if s.isValid() and (s.width() > size or s.height() > size):
            reader.setScaledSize(s.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    return img


class ImageLoader(QObject):
    """Reads images on background threads. The most recent requests go first, so the thumbnails on screen load
    before ones scrolled past. ready(key, image) arrives on the GUI thread."""

    ready = Signal(object, QImage)

    def __init__(self, threads: int = 3, max_waiting: int = 600, parent=None):
        super().__init__(parent)
        self._lock = threading.Condition()
        self._waiting: collections.OrderedDict = collections.OrderedDict()   # key -> (path, size)
        self._max_waiting = max_waiting
        self._closed = False
        self._threads = [threading.Thread(target=self._run, daemon=True, name=f"image-loader-{i}")
                         for i in range(threads)]
        for t in self._threads:
            t.start()

    def request(self, key, path: str, size: int | None) -> None:
        with self._lock:
            self._waiting.pop(key, None)
            self._waiting[key] = (path, size)
            while len(self._waiting) > self._max_waiting:
                self._waiting.popitem(last=False)          # the oldest request: long scrolled past
            self._lock.notify()

    def clear(self) -> None:
        with self._lock:
            self._waiting.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._waiting.clear()
            self._lock.notify_all()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._waiting and not self._closed:
                    self._lock.wait()
                if self._closed:
                    return
                key, (path, size) = self._waiting.popitem(last=True)
            try:
                img = read_image(path, size)
            except Exception:  # noqa: BLE001 - an unreadable file shows as blank
                img = QImage()
            with self._lock:
                if self._closed:
                    return
            self.ready.emit(key, img)


class CountWorker(QObject):
    """Counts the photos of each sol on a background thread; counted(rover, {sol: n}) arrives in batches."""

    counted = Signal(str, object)            # object: a Python dict can't pass through a signal declared as dict

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0

    def start(self, folder: str, rover: str, sols: list[int]) -> None:
        self._generation += 1
        gen = self._generation

        def work():
            batch, last = {}, time.monotonic()
            for sol in sols:
                if gen != self._generation:
                    return
                batch[sol] = sol_photo_count(folder, rover, sol)
                if time.monotonic() - last > 0.25:
                    self.counted.emit(rover, batch)
                    batch, last = {}, time.monotonic()
            if batch and gen == self._generation:
                self.counted.emit(rover, batch)

        threading.Thread(target=work, daemon=True, name="photo-counter").start()

    def stop(self) -> None:
        self._generation += 1


# ---- the thumbnail grid ----------------------------------------------------------------------------------

class PhotoGridModel(QAbstractListModel):
    PhotoRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, loader: ImageLoader, parent=None):
        super().__init__(parent)
        self.loader = loader
        self.photos: list[PhotoFile] = []
        self.rows: dict[str, int] = {}
        self.size = 128
        self.show_camera = True
        self.cache: collections.OrderedDict = collections.OrderedDict()    # path -> QPixmap at self.size
        self.blank = QPixmap()
        self._make_blank()
        loader.ready.connect(self._ready)

    def _make_blank(self) -> None:
        self.blank = QPixmap(self.size, self.size)
        self.blank.fill(Qt.GlobalColor.transparent)

    def _cache_limit(self) -> int:
        return max(200, int(160 * 1024 * 1024 / (self.size * self.size * 4)))     # about 160 MB of thumbnails

    def set_photos(self, photos: list[PhotoFile], show_camera: bool) -> None:
        self.beginResetModel()
        self.photos = list(photos)
        self.rows = {p.path: i for i, p in enumerate(self.photos)}
        self.show_camera = show_camera
        self.endResetModel()
        self.loader.clear()

    def append(self, photo: PhotoFile) -> None:
        if photo.path in self.rows:
            return
        row = len(self.photos)
        self.beginInsertRows(QModelIndex(), row, row)
        self.photos.append(photo)
        self.rows[photo.path] = row
        self.endInsertRows()

    def set_size(self, size: int) -> None:
        if size == self.size:
            return
        self.size = size
        self.cache.clear()
        self._make_blank()
        self.loader.clear()
        if self.photos:
            self.dataChanged.emit(self.index(0), self.index(len(self.photos) - 1))

    def caption(self, p: PhotoFile) -> str:
        return f"{p.camera} {p.number}" if self.show_camera else str(p.number)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.photos)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.photos):
            return None
        p = self.photos[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.caption(p)
        if role == Qt.ItemDataRole.DecorationRole:
            pix = self.cache.get(p.path)
            if pix is not None:
                self.cache.move_to_end(p.path)
                return pix
            self.loader.request(("thumb", p.path, self.size), p.path, self.size)
            return self.blank
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{p.rover} · sol {p.sol} · {camera_name(p.camera)} · {p.name}"
        if role == self.PhotoRole:
            return p
        return None

    def _ready(self, key, img: QImage) -> None:
        if not (isinstance(key, tuple) and key[0] == "thumb") or key[2] != self.size:
            return
        path = key[1]
        row = self.rows.get(path)
        if row is None:
            return
        pix = QPixmap.fromImage(img) if not img.isNull() else self.blank
        self.cache[path] = pix
        while len(self.cache) > self._cache_limit():
            self.cache.popitem(last=False)
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


class ThumbDelegate(QStyledItemDelegate):
    """Each thumbnail centered in a square of the thumbnail size, with its caption at the same height in every
    cell, on a rounded background when chosen (accent) or under the pointer (faint accent)."""

    def __init__(self, model: PhotoGridModel, parent=None):
        super().__init__(parent)
        self.model = model

    def paint(self, painter: QPainter, option, index) -> None:
        size = self.model.size
        r = option.rect.adjusted(3, 3, -3, -3)
        pal = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected or hover:
            bg = QColor(pal.color(QPalette.ColorRole.Highlight))
            if not selected:
                bg.setAlpha(60)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(QRectF(r), 6, 6)
        box = QRect(r.x() + (r.width() - size) // 2, r.y() + 5, size, size)
        pix = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pix, QPixmap) and not pix.isNull():
            w, h = pix.width() / pix.devicePixelRatio(), pix.height() / pix.devicePixelRatio()
            painter.drawPixmap(QRectF(box.x() + (size - w) / 2, box.y() + (size - h) / 2, w, h), pix,
                               QRectF(pix.rect()))
        text_rect = QRect(r.x() + 4, box.bottom() + 4, r.width() - 8, option.fontMetrics.height() + 2)
        painter.setPen(pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        text = option.fontMetrics.elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "",
                                             Qt.TextElideMode.ElideMiddle, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, text)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        size = self.model.size
        return QSize(size + 18, size + option.fontMetrics.height() + 20)


# ---- the full-size view ----------------------------------------------------------------------------------

class ImageView(QWidget):
    """One image: fitted to the view at first; the wheel zooms around the pointer, dragging moves the image,
    double-click switches between fitted and actual size."""

    zoom_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("imageView")
        self.pixmap = QPixmap()
        self.zoom: float | None = None          # None: fit to the view
        self.center = QPointF()                 # the image point at the middle of the view
        self._drag: QPointF | None = None
        self.message = ""
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, pixmap: QPixmap, message: str = "") -> None:
        self.pixmap = pixmap
        self.message = message
        self.zoom = None
        self.center = QPointF(pixmap.width() / 2, pixmap.height() / 2)
        self.update()
        self.zoom_changed.emit()

    def fit_scale(self) -> float:
        if self.pixmap.isNull():
            return 1.0
        return min(1.0 * self.width() / self.pixmap.width(), 1.0 * self.height() / self.pixmap.height(),
                   4.0)             # small images are enlarged, up to 4 times

    def scale(self) -> float:
        return self.zoom if self.zoom is not None else self.fit_scale()

    def set_zoom(self, zoom: float | None, around: QPointF | None = None) -> None:
        if self.pixmap.isNull():
            return
        if zoom is None:
            self.zoom = None
            self.center = QPointF(self.pixmap.width() / 2, self.pixmap.height() / 2)
        else:
            zoom = max(0.05, min(16.0, zoom))
            around = around if around is not None else QPointF(self.width() / 2, self.height() / 2)
            mid = QPointF(self.width() / 2, self.height() / 2)
            point = self.center + (around - mid) / self.scale()          # the image point under the pointer
            self.zoom = zoom
            self.center = point - (around - mid) / zoom
            self._clamp()
        self.update()
        self.zoom_changed.emit()

    def _clamp(self) -> None:
        w, h = self.pixmap.width(), self.pixmap.height()
        self.center = QPointF(min(max(self.center.x(), 0), w), min(max(self.center.y(), 0), h))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
        if self.pixmap.isNull():
            if self.message:
                p.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        s = self.scale()
        w, h = self.pixmap.width() * s, self.pixmap.height() * s
        x = self.width() / 2 - self.center.x() * s
        y = self.height() / 2 - self.center.y() * s
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, s < 2)
        p.drawPixmap(QRectF(x, y, w, h), self.pixmap, QRectF(self.pixmap.rect()))

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120
        if steps:
            self.set_zoom(self.scale() * (1.25 ** steps), event.position())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None and not self.pixmap.isNull():
            delta = event.position() - self._drag
            self._drag = event.position()
            if self.zoom is None:
                self.zoom = self.fit_scale()
            self.center -= delta / self.scale()
            self._clamp()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.zoom is None and abs(self.fit_scale() - 1.0) > 0.01:
            self.set_zoom(1.0, event.position())
        else:
            self.set_zoom(None)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.zoom_changed.emit()


# ---- the page --------------------------------------------------------------------------------------------

def _card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(8)
    if title:
        t = QLabel(title.upper())
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return frame, lay


class WrapLabel(QLabel):
    """A word-wrapped label that always gets the height its text needs at its current width (a grid otherwise
    gives it one line and cuts off the rest)."""

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

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

    def changeEvent(self, event) -> None:          # font or style changes (theme switch) change the height
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit()


def _label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lbl = WrapLabel(text) if wrap else QLabel(text)
    if name:
        lbl.setObjectName(name)
    return lbl


class PhotosPage(QWidget):
    """settings: the app's Settings (photo_thumb_size is read and changed); folder(): the photo folder;
    save_settings(): saves them."""

    NEWEST = "newest"

    def __init__(self, settings, folder: Callable[[], str], save_settings: Callable[[], None], parent=None):
        super().__init__(parent)
        self.settings, self.folder, self.save_settings = settings, folder, save_settings
        self.loader = ImageLoader(threads=3)
        self.big_loader = ImageLoader(threads=1, max_waiting=8)       # previews and full-size images
        self.big_loader.ready.connect(self._big_ready)
        self.counter = CountWorker(self)
        self.counter.counted.connect(self._counted)
        self.counts: dict[tuple[str, str, int], int] = {}             # (folder, rover, sol) -> photos
        self.sol_items: dict[int, QTreeWidgetItem] = {}
        self.newest: collections.deque[PhotoFile] = collections.deque(maxlen=NEWEST_MAX)
        self.newest_item: QTreeWidgetItem | None = None
        self.loaded_folder: str | None = None
        self.current: PhotoFile | None = None
        self.view_key: tuple | None = None                            # what the grid shows
        self.full_cache: collections.OrderedDict = collections.OrderedDict()      # path -> QPixmap, a few
        self.extra_details: Callable[[PhotoFile], list[tuple[str, str]]] | None = None   # details from elsewhere
        self._build()
        self.size_timer = QTimer(self, singleShot=True, interval=150)
        self.size_timer.timeout.connect(self._apply_thumb_size)
        self.dirty_sols: set[tuple[str, str, int]] = set()            # sols a download added photos to
        self.recount_timer = QTimer(self, singleShot=True, interval=1500)
        self.recount_timer.timeout.connect(self._recount)

    # ---- layout ------------------------------------------------------------------------------------------

    def _build(self) -> None:
        body = QHBoxLayout(self)
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(16)

        # Browse: rover, Go To Sol, the sols and their cameras
        browse, bl = _card("Browse")
        browse.setFixedWidth(250)
        row = QHBoxLayout()
        row.addWidget(_label("Rover"))
        self.rover_box = QComboBox()
        self.rover_box.currentIndexChanged.connect(lambda _i: self._rover_chosen())
        row.addWidget(self.rover_box, 1)
        bl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(_label("Go To Sol"))
        self.goto = QLineEdit()
        self.goto.setValidator(QIntValidator(0, 100000))
        self.goto.setPlaceholderText("Number")
        self.goto.returnPressed.connect(self._go_to_sol)
        row.addWidget(self.goto, 1)
        bl.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setObjectName("solTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Sol", "Photos"])
        self.tree.headerItem().setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, self.tree.header().ResizeMode.ResizeToContents)
        self.tree.setUniformRowHeights(True)
        self.tree.itemExpanded.connect(self._fill_cameras)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._tree_chosen(cur))
        bl.addWidget(self.tree, 1)
        self.browse_note = _label("", "muted", wrap=True)
        bl.addWidget(self.browse_note)
        body.addWidget(browse)

        # Thumbnails, or one photo at full size
        middle, ml = _card()
        head = QHBoxLayout()
        self.grid_title = _label("", "value")
        head.addWidget(self.grid_title, 1)
        self.size_label = _label("Thumbnail Size", "rowHead")
        head.addWidget(self.size_label)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(THUMB_MIN, THUMB_MAX)
        self.size_slider.setSingleStep(16)
        self.size_slider.setPageStep(32)
        self.size_slider.setFixedWidth(140)
        self.size_slider.setValue(self._thumb_size())
        self.size_slider.valueChanged.connect(lambda _v: self.size_timer.start())
        head.addWidget(self.size_slider)
        ml.addLayout(head)
        self.stack = QStackedWidget()
        self.model = PhotoGridModel(self.loader, self)
        self.model.size = self._thumb_size()
        self.model._make_blank()
        self.grid = QListView()
        self.grid.setObjectName("photoGrid")
        self.grid.setModel(self.model)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(300)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setItemDelegate(ThumbDelegate(self.model, self.grid))
        self.grid.setMouseTracking(True)                  # for the hover background
        self.grid.selectionModel().currentChanged.connect(lambda cur, _prev: self._grid_chosen(cur))
        self.grid.activated.connect(lambda _i: self.open_full())
        self._apply_grid_size()
        self.empty = _label("", "muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        full = QWidget()
        fl = QVBoxLayout(full)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        bar = QHBoxLayout()
        self.back_btn = QPushButton("◀  Back to Thumbnails")
        self.back_btn.setToolTip("Esc")
        self.back_btn.clicked.connect(self.close_full)
        bar.addWidget(self.back_btn)
        bar.addStretch()
        self.prev_btn = QPushButton("Previous")
        self.prev_btn.setToolTip("Left arrow")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn = QPushButton("Next")
        self.next_btn.setToolTip("Right arrow")
        self.next_btn.clicked.connect(lambda: self.step(1))
        self.position = _label("", "muted")
        bar.addWidget(self.prev_btn)
        bar.addWidget(self.position)
        bar.addWidget(self.next_btn)
        bar.addStretch()
        self.fit_btn = QPushButton("Fit")
        self.fit_btn.setToolTip("Fit the photo to the view (double-click the photo to switch)")
        self.fit_btn.clicked.connect(lambda: self.viewer.set_zoom(None))
        self.actual_btn = QPushButton("Actual Size")
        self.actual_btn.clicked.connect(lambda: self.viewer.set_zoom(1.0))
        self.zoom_label = _label("", "muted")
        bar.addWidget(self.zoom_label)
        bar.addWidget(self.fit_btn)
        bar.addWidget(self.actual_btn)
        fl.addLayout(bar)
        self.viewer = ImageView()
        self.viewer.zoom_changed.connect(self._show_zoom)
        fl.addWidget(self.viewer, 1)
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(full)
        self.full_page = full
        self.stack.setCurrentWidget(self.empty)
        ml.addWidget(self.stack, 1)
        body.addWidget(middle, 1)

        # Details of the chosen photo
        details, dl = _card("Details")
        details.setFixedWidth(320)
        self.preview = QLabel()
        self.preview.setObjectName("photoPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(290, 240)
        self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview.setToolTip("Click to view full size")
        self.preview.mousePressEvent = lambda _e: self.open_full()
        dl.addWidget(self.preview)
        self.detail_grid = QGridLayout()
        self.detail_grid.setHorizontalSpacing(12)
        self.detail_grid.setVerticalSpacing(5)
        self.detail_grid.setColumnStretch(1, 1)
        dl.addLayout(self.detail_grid)
        self.detail_rows: list[tuple[QLabel, QLabel]] = []
        self.detail_note = _label("", "muted", wrap=True)
        dl.addWidget(self.detail_note)
        btns = QHBoxLayout()
        self.full_btn = QPushButton("View Full Size")
        self.full_btn.clicked.connect(self.open_full)
        self.folder_btn = QPushButton("Show in Folder")
        self.folder_btn.clicked.connect(lambda: self.current and show_in_folder(self.current.path))
        btns.addWidget(self.full_btn)
        btns.addWidget(self.folder_btn)
        btns.addStretch()
        dl.addLayout(btns)
        dl.addStretch(1)
        body.addWidget(details)
        self._show_details(None)

    def _thumb_size(self) -> int:
        return min(THUMB_MAX, max(THUMB_MIN, int(getattr(self.settings, "photo_thumb_size", 128) or 128)))

    def _apply_grid_size(self) -> None:
        size = self.model.size
        self.grid.setIconSize(QSize(size, size))
        line = self.grid.fontMetrics().height()
        self.grid.setGridSize(QSize(size + 18, size + line + 20))       # the same as ThumbDelegate.sizeHint

    def _apply_thumb_size(self) -> None:
        size = self.size_slider.value() // 16 * 16
        self.settings.photo_thumb_size = size
        self.save_settings()
        self.model.set_size(size)
        self._apply_grid_size()

    # ---- the folder and the tree -------------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the folder (called when the page is shown, and when the folder changes)."""
        folder = self.folder()
        if folder != self.loaded_folder:
            self.loaded_folder = folder
            self.counts = {k: v for k, v in self.counts.items() if k[0] == folder}
            rovers = rovers_in(folder) if folder and os.path.isdir(folder) else []
            keep = self.rover_box.currentText()
            self.rover_box.blockSignals(True)
            self.rover_box.clear()
            self.rover_box.addItems(rovers)
            if keep in rovers:
                self.rover_box.setCurrentText(keep)
            self.rover_box.blockSignals(False)
            self._rover_chosen()
        else:
            self._reload_sols(keep_view=True)

    def _rover(self) -> str:
        return self.rover_box.currentText()

    def _rover_chosen(self) -> None:
        self.current = None
        self.view_key = None
        self._reload_sols(keep_view=False)

    def _reload_sols(self, keep_view: bool) -> None:
        folder, rover = self.loaded_folder or "", self._rover()
        if not folder:
            self._set_empty("Choose a photo folder on the Download page.")
        elif not os.path.isdir(folder):
            self._set_empty("The photo folder doesn't exist yet.")
        elif not rover:
            self._set_empty("No photos in this folder yet. Downloaded photos appear here.")
        sols = sols_in(folder, rover) if folder and rover else []
        if keep_view and sols == sorted(self.sol_items) and self.tree.topLevelItemCount():
            self._count_missing(folder, rover, sols)
            return
        chosen = self.tree.currentItem()
        chosen_key = chosen.data(0, Qt.ItemDataRole.UserRole) if chosen is not None else None
        self.tree.blockSignals(True)
        self.tree.clear()
        self.sol_items = {}
        self.newest_item = None
        if self.newest:
            self._add_newest_item()
        for sol in sols:
            self.sol_items[sol] = self._make_sol_item(folder, rover, sol)
        self.tree.addTopLevelItems(list(self.sol_items.values()))
        self.tree.blockSignals(False)
        self._update_browse_note()
        self._count_missing(folder, rover, sols)
        if keep_view and chosen_key is not None:
            item = self._find_item(chosen_key)
            if item is not None:
                self.tree.setCurrentItem(item)
                return
        if not keep_view and sols:                 # the latest sol, where the newest photos are
            self.tree.setCurrentItem(self.sol_items[sols[-1]])
            self.tree.scrollToItem(self.sol_items[sols[-1]])
        elif not sols and rover:
            self._set_empty("No sols in this rover's folder yet.")

    def _make_sol_item(self, folder: str, rover: str, sol: int) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"Sol {sol}", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, ("sol", sol))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        n = self.counts.get((folder, rover, sol))
        if n is not None:
            item.setText(1, f"{n:,}")
        d = earth_date(rover, sol)
        if d:
            item.setToolTip(0, f"Around {fmt_date(d)} on Earth")
        return item

    def _add_newest_item(self) -> None:
        self.newest_item = QTreeWidgetItem(["Newest Downloads", f"{len(self.newest):,}"])
        self.newest_item.setData(0, Qt.ItemDataRole.UserRole, (self.NEWEST,))
        self.newest_item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.newest_item.setToolTip(0, "Photos saved since the app started, newest last, from every rover")
        f = self.newest_item.font(0)
        f.setBold(True)
        self.newest_item.setFont(0, f)
        self.tree.insertTopLevelItem(0, self.newest_item)

    def _find_item(self, key) -> QTreeWidgetItem | None:
        if key[0] == self.NEWEST:
            return self.newest_item
        item = self.sol_items.get(key[1])
        if item is None or key[0] == "sol":
            return item
        self._fill_cameras(item)
        for i in range(item.childCount()):
            if item.child(i).data(0, Qt.ItemDataRole.UserRole) == key:
                return item.child(i)
        return item

    def _count_missing(self, folder: str, rover: str, sols: list[int]) -> None:
        todo = [s for s in sols if (folder, rover, s) not in self.counts]
        if todo:
            self.counter.start(folder, rover, todo)
        self._update_browse_note()

    def _counted(self, rover: str, batch: dict) -> None:
        folder = self.loaded_folder or ""
        for sol, n in batch.items():
            self.counts[(folder, rover, sol)] = n
            if rover == self._rover() and sol in self.sol_items:
                self.sol_items[sol].setText(1, f"{n:,}")
        self._update_browse_note()

    def _update_browse_note(self) -> None:
        folder, rover = self.loaded_folder or "", self._rover()
        if not rover:
            self.browse_note.setText("")
            return
        sols = list(self.sol_items)
        known = [self.counts[(folder, rover, s)] for s in sols if (folder, rover, s) in self.counts]
        text = count_text(len(sols), "sol")
        if len(known) == len(sols):
            text += f" · {count_text(sum(known), 'photo')}"
        else:
            text += f" · counting photos ({len(known):,} of {len(sols):,} sols)"
        self.browse_note.setText(text)

    def _fill_cameras(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key or key[0] != "sol" or item.childCount():
            return
        folder, rover, sol = self.loaded_folder or "", self._rover(), key[1]
        cams = cameras_in(folder, rover, sol)
        for cam, n in cams:
            child = QTreeWidgetItem([cam, f"{n:,}"])
            child.setData(0, Qt.ItemDataRole.UserRole, ("camera", sol, cam))
            child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            child.setToolTip(0, camera_name(cam))
            item.addChild(child)
        if not cams:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
        total = sum(n for _, n in cams)
        self.counts[(folder, rover, sol)] = total
        item.setText(1, f"{total:,}")

    def _go_to_sol(self) -> None:
        text = self.goto.text().strip()
        if not text or not self.sol_items:
            return
        want = int(text)
        sols = sorted(self.sol_items)
        best = min(sols, key=lambda s: (abs(s - want), s))       # the sol itself, or the nearest one
        item = self.sol_items[best]
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
        self.browse_note.setText(self.browse_note.text() if best == want else
                                 f"No photos for sol {want}; showing sol {best}, the nearest one.")

    # ---- what the grid shows -----------------------------------------------------------------------------

    def _tree_chosen(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        folder, rover = self.loaded_folder or "", self._rover()
        if key[0] == self.NEWEST:
            photos = list(self.newest)
            title = f"Newest Downloads · {count_text(len(photos), 'photo')}"
            self._show_photos(key, photos, title, show_camera=True, empty="No photos downloaded yet.")
            return
        sol = key[1]
        if key[0] == "sol":
            photos = photos_in(folder, rover, sol)
            title = f"{rover} · Sol {sol} · All Cameras · {count_text(len(photos), 'photo')}"
            self._show_photos(key, photos, title, show_camera=True, empty="No photos in this sol's folder.")
        else:
            cam = key[2]
            photos = photos_in(folder, rover, sol, cam)
            title = f"{rover} · Sol {sol} · {camera_name(cam)} · {count_text(len(photos), 'photo')}"
            self._show_photos(key, photos, title, show_camera=False, empty="No photos in this camera's folder.")

    def _show_photos(self, key, photos: list[PhotoFile], title: str, show_camera: bool, empty: str) -> None:
        self.view_key = key
        self.grid_title.setText(title)
        self.model.set_photos(photos, show_camera)
        self.full_cache.clear()
        if photos:
            if self.stack.currentWidget() is not self.full_page:
                self.stack.setCurrentWidget(self.grid)
            self.grid.setCurrentIndex(self.model.index(0))
            self.grid.scrollToTop()
        else:
            self.empty.setText(empty)
            self.stack.setCurrentWidget(self.empty)
            self.current = None
            self._show_details(None)

    def _set_empty(self, text: str) -> None:
        self.tree.clear()
        self.sol_items = {}
        self.newest_item = None
        if self.newest:
            self._add_newest_item()
        self.grid_title.setText("")
        self.model.set_photos([], True)
        self.empty.setText(text)
        self.stack.setCurrentWidget(self.empty)
        self.current = None
        self._show_details(None)
        self.browse_note.setText("")

    def _grid_chosen(self, index) -> None:
        p = self.model.data(index, PhotoGridModel.PhotoRole) if index.isValid() else None
        self.current = p
        self._show_details(p)
        if p is not None and self.stack.currentWidget() is self.full_page:
            self._load_full(p)

    # ---- details and preview -----------------------------------------------------------------------------

    def _set_detail_rows(self, rows: list[tuple[str, str]]) -> None:
        # New labels each time: a reused label with selectable text kept the height of its earlier, longer text
        # (a one-line time sat in three lines of space).
        for head, value in self.detail_rows:
            for w in (head, value):
                self.detail_grid.removeWidget(w)
                w.hide()
                w.deleteLater()
        self.detail_rows = []
        for r, (name, text) in enumerate(rows):
            head, value = _label(name, "rowHead"), _label("", "value", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)   # selectable, links
            value.setOpenExternalLinks(True)
            value.setText(text)
            if name == "File" and self.current:
                value.setToolTip(self.current.path)
            self.detail_grid.addWidget(head, r, 0, Qt.AlignmentFlag.AlignTop)
            self.detail_grid.addWidget(value, r, 1)
            self.detail_rows.append((head, value))

    def details_for(self, p: PhotoFile) -> list[tuple[str, str]]:
        rows = [("Rover", p.rover), ("Sol", f"{p.sol}"), ("Camera", camera_name(p.camera))]
        extra = self.extra_details(p) if self.extra_details else []
        if not any(k == "Taken" for k, _ in extra):
            d = earth_date(p.rover, p.sol)
            if d:
                rows.append(("Earth Date", f"Around {fmt_date(d)}"))
        rows += extra
        size = QImageReader(p.path).size()
        if size.isValid():
            rows.append(("Image Size", f"{size.width():,} × {size.height():,} pixels"))
        try:
            rows.append(("File Size", fmt_file_size(os.path.getsize(p.path))))
        except OSError:
            rows.append(("File Size", "File not found"))
        rows.append(("File", p.name))         # its folder is the rover, sol and camera above (full path: tooltip)
        return rows

    def _show_details(self, p: PhotoFile | None) -> None:
        has = p is not None
        self.full_btn.setEnabled(has)
        self.folder_btn.setEnabled(has)
        if not has:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("")
            self._set_detail_rows([])
            self.detail_note.setText("Choose a photo to see its details.")
            self.detail_note.setVisible(True)
            return
        self.detail_note.setVisible(False)
        self._set_detail_rows(self.details_for(p))
        self.preview.setPixmap(QPixmap())
        self.big_loader.request(("preview", p.path), p.path, max(self.preview.width(), self.preview.height()) * 2)

    def refresh_details(self) -> None:
        """Show the current photo's details again (for example after more details arrived)."""
        if self.current is not None:
            self._set_detail_rows(self.details_for(self.current))

    def _big_ready(self, key, img: QImage) -> None:
        kind, path = key
        if kind == "preview":
            if self.current is not None and self.current.path == path:
                if img.isNull():
                    self.preview.setText("Can't show this file")
                    return
                pix = QPixmap.fromImage(img).scaled(self.preview.size() * self.preview.devicePixelRatioF(),
                                                    Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation)
                pix.setDevicePixelRatio(self.preview.devicePixelRatioF())
                self.preview.setPixmap(pix)
        elif kind == "full":
            pix = QPixmap.fromImage(img) if not img.isNull() else QPixmap()
            self.full_cache[path] = pix
            while len(self.full_cache) > 6:
                self.full_cache.popitem(last=False)
            if self.current is not None and self.current.path == path and self.stack.currentWidget() is self.full_page:
                self.viewer.set_image(pix, "" if not pix.isNull() else "Can't show this file")

    # ---- full size -----------------------------------------------------------------------------------------

    def open_full(self) -> None:
        if self.current is None:
            return
        self.stack.setCurrentWidget(self.full_page)
        self._load_full(self.current)
        self.viewer.setFocus()

    def close_full(self) -> None:
        if self.stack.currentWidget() is self.full_page:
            self.stack.setCurrentWidget(self.grid if self.model.photos else self.empty)
            row = self.model.rows.get(self.current.path) if self.current else None
            if row is not None:
                self.grid.scrollTo(self.model.index(row))
            self.grid.setFocus()

    def _load_full(self, p: PhotoFile) -> None:
        row = self.model.rows.get(p.path, 0)
        total = len(self.model.photos)
        self.position.setText(f"{row + 1:,} of {total:,}")
        self.prev_btn.setEnabled(row > 0)
        self.next_btn.setEnabled(row < total - 1)
        pix = self.full_cache.get(p.path)
        if pix is not None:
            self.viewer.set_image(pix, "" if not pix.isNull() else "Can't show this file")
        else:
            self.viewer.set_image(QPixmap(), "Loading…")
        for near in (row + 1, row - 1):          # the next and previous photo, so stepping is quick
            if 0 <= near < total and self.model.photos[near].path not in self.full_cache:
                self.big_loader.request(("full", self.model.photos[near].path), self.model.photos[near].path, None)
        if p.path not in self.full_cache:       # asked last, so it's read first
            self.big_loader.request(("full", p.path), p.path, None)

    def step(self, delta: int) -> None:
        if self.current is None:
            return
        row = self.model.rows.get(self.current.path)
        if row is None:
            return
        new = min(max(row + delta, 0), len(self.model.photos) - 1)     # stops at the first and last photo
        if new != row:
            self.grid.setCurrentIndex(self.model.index(new))

    def _show_zoom(self) -> None:
        if self.viewer.pixmap.isNull():
            self.zoom_label.setText("")
            return
        self.zoom_label.setText(f"{self.viewer.scale() * 100:,.0f} %")

    def keyPressEvent(self, event) -> None:
        if self.stack.currentWidget() is self.full_page:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close_full()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
                self.step(-1)
                return
            if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
                self.step(1)
                return
            if key == Qt.Key.Key_Home:
                self.step(-len(self.model.photos))
                return
            if key == Qt.Key.Key_End:
                self.step(len(self.model.photos))
                return
        super().keyPressEvent(event)

    # ---- new photos from a download ------------------------------------------------------------------------

    def photo_saved(self, path: str) -> PhotoFile | None:
        """A download saved a photo: add it to Newest Downloads and to the counts."""
        folder = self.loaded_folder if self.loaded_folder is not None else self.folder()
        p = photo_from_path(folder, path) if folder else None
        if p is None:
            return None
        self.newest.append(p)
        # The sol is counted again from the folder shortly (adding 1 here could count a photo twice, if the
        # background count read the folder after the file was saved).
        self.dirty_sols.add((folder, p.rover, p.sol))
        if not self.recount_timer.isActive():
            self.recount_timer.start()
        if folder == self.loaded_folder:
            if self.newest_item is None and self.isVisible():
                self._add_newest_item()
            if self.newest_item is not None:
                self.newest_item.setText(1, f"{len(self.newest):,}")
            if p.rover == self._rover() and p.sol not in self.sol_items and self.tree.topLevelItemCount():
                self._insert_sol(p.sol)
            if self.view_key == (self.NEWEST,):
                self.model.append(p)
                self.grid_title.setText(f"Newest Downloads · {count_text(len(self.model.photos), 'photo')}")
                if self.stack.currentWidget() is self.empty:
                    self.stack.setCurrentWidget(self.grid)
                    self.grid.setCurrentIndex(self.model.index(0))
        return p

    def _insert_sol(self, sol: int) -> None:
        """A download made a new sol folder: add it to the list in its place."""
        item = self._make_sol_item(self.loaded_folder or "", self._rover(), sol)
        later = [s for s in self.sol_items if s > sol]
        self.sol_items[sol] = item
        if later:
            self.tree.insertTopLevelItem(self.tree.indexOfTopLevelItem(self.sol_items[min(later)]), item)
        else:
            self.tree.addTopLevelItem(item)
        self._update_browse_note()

    def _recount(self) -> None:
        """Count the sols that downloads added photos to, and update their rows."""
        dirty, self.dirty_sols = self.dirty_sols, set()
        for key in dirty:
            folder, rover, sol = key
            if folder != self.loaded_folder:
                continue
            cams = cameras_in(folder, rover, sol)
            self.counts[key] = sum(n for _, n in cams)
            item = self.sol_items.get(sol) if rover == self._rover() else None
            if item is None:
                continue
            item.setText(1, f"{self.counts[key]:,}")
            existing = {item.child(i).data(0, Qt.ItemDataRole.UserRole)[2]: item.child(i)
                        for i in range(item.childCount())}
            if item.childCount() or item.isExpanded():
                for cam, n in cams:
                    if cam in existing:
                        existing[cam].setText(1, f"{n:,}")
                    else:                   # a camera folder the download just made
                        item.takeChildren()
                        self._fill_cameras(item)
                        break
        self._update_browse_note()

    def show_photo(self, p: PhotoFile) -> None:
        """Show one photo in Newest Downloads (it's there if a download saved it), else in its camera folder."""
        if p in self.newest and self.newest_item is not None:
            self.tree.setCurrentItem(self.newest_item)
        else:
            if self.rover_box.currentText() != p.rover:
                self.rover_box.setCurrentText(p.rover)
            item = self._find_item(("camera", p.sol, p.camera))
            if item is not None:
                self.tree.setCurrentItem(item)
        row = self.model.rows.get(p.path)
        if row is not None:
            self.grid.setCurrentIndex(self.model.index(row))
            self.grid.scrollTo(self.model.index(row))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self.refresh)

    def shutdown(self) -> None:
        self.counter.stop()
        self.loader.close()
        self.big_loader.close()

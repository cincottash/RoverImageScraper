"""The Photos page: browse the photo folder by rover, sol and camera, with thumbnails, a preview and details, and a
full-size view with zoom. It only reads the photo folder.

Thumbnails are made on background threads, shrunk as they're read (fast for JPEG), and kept in memory only, so
nothing is written to disk.
"""
from __future__ import annotations

import collections
import functools
import os
import subprocess
import sys
import threading
import time
import weakref
from typing import Callable

from PySide6.QtCore import (QAbstractListModel, QEvent, QItemSelectionModel, QModelIndex, QObject, QPointF, QRect,
                            QRectF, QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QImage, QImageReader, QIntValidator, QKeySequence, QPainter,
                           QPalette, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListView, QMenu, QMessageBox, QPushButton, QSizePolicy, QSlider,
                               QStackedWidget, QStyle, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from favorites import FavoritesStore, Flipbook, fav_key
from flipbook import FlipbookBar, FlipbookPlayer
from photo_library import (PhotoFile, camera_name, cameras_in, earth_date, find_photos, photo_from_path, photos_in,
                           rovers_in, sol_photo_count, sols_in)

THUMB_MIN, THUMB_MAX = 64, 256
LIST_CACHE_MAX = 60                # photo lists of sols and cameras kept in memory
STAR_COLOR = "#f5c542"             # the favorite star on thumbnails (on a dark disc, so it shows in every theme)
NEWEST_MAX = 2000                  # photos kept in "Newest Downloads"


def count_text(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


class SortItem(QTreeWidgetItem):
    """A row of the sols' list that sorts by number, not text: column 0 by sol number (camera rows by name), column
    1 by photo count (not counted yet: last). A pinned row (Newest Downloads) stays at the top in either order."""

    def __init__(self, texts, sort_key=None, pinned: bool = False):
        super().__init__(texts)
        self.sort_key = sort_key           # what column 0 sorts by
        self.pinned = pinned

    def _count(self) -> int:
        text = self.text(1).replace(",", "")
        return int(text) if text.isdigit() else -1

    def __lt__(self, other) -> bool:
        tree = self.treeWidget()
        # (column, ascending), set by the page before it sorts (asking the header thousands of times was slow)
        column, ascending = getattr(tree, "sort_state", (0, True))
        mine, theirs = getattr(self, "pinned", False), getattr(other, "pinned", False)
        if mine != theirs:                 # Qt compares the other way round for descending order
            return mine if ascending else theirs
        if column == 1 and isinstance(other, SortItem):
            a, b = self._count(), other._count()
            if a != b:
                return a < b
        a = self.sort_key if self.sort_key is not None else self.text(0)
        b = getattr(other, "sort_key", None)
        b = b if b is not None else other.text(0)
        if type(a) is not type(b):
            return str(a) < str(b)
        return a < b


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

FULL_SIZE = "full-size"           # image text: the photo's own size, "<width>x<height>", before any shrinking


def read_image(path: str, size: int | None) -> QImage:
    """The image at path, shrunk while reading so its longer side is at most size (None: full size). Its own size
    is kept in the image's FULL_SIZE text."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    s = reader.size()
    if size and s.isValid() and (s.width() > size or s.height() > size):
        reader.setScaledSize(s.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    if not img.isNull() and s.isValid():
        img.setText(FULL_SIZE, f"{s.width()}x{s.height()}")
    return img


# Readers for what's on screen (the photo list, thumbnails, previews). The background photo counter waits while
# any of them has work: on a hard disk, two readers at once make both slow.
_SCREEN_READERS: weakref.WeakSet = weakref.WeakSet()


def screen_reads_pending() -> bool:
    return any(r.busy() for r in list(_SCREEN_READERS))


class ImageLoader(QObject):
    """Reads images on background threads. The most recent requests go first, so the thumbnails on screen load
    before ones scrolled past. ready(key, image) arrives on the GUI thread."""

    ready = Signal(object, QImage)

    def __init__(self, threads: int = 3, max_waiting: int = 600, parent=None,
                 hold: Callable[[], bool] | None = None):
        """hold(): while it's true, no new read starts (something more urgent is reading the disk)."""
        super().__init__(parent)
        self._lock = threading.Condition()
        self._waiting: collections.OrderedDict = collections.OrderedDict()   # key -> (path, size)
        self._low: collections.OrderedDict = collections.OrderedDict()       # read ahead, when nothing else waits
        self._max_waiting = max_waiting
        self._hold = hold
        self._active = 0
        self._closed = False
        self._threads = [threading.Thread(target=self._run, daemon=True, name=f"image-loader-{i}")
                         for i in range(threads)]
        for t in self._threads:
            t.start()
        _SCREEN_READERS.add(self)

    def busy(self) -> bool:
        with self._lock:
            return not self._closed and (bool(self._waiting) or bool(self._low) or self._active > 0)

    def waiting(self) -> int:
        with self._lock:
            return len(self._waiting) + len(self._low)

    def request(self, key, path: str, size: int | None, low: bool = False) -> None:
        """low: read ahead (photos likely to be wanted next), only while nothing else is waiting, in the order
        asked. A normal request for the same key takes it over."""
        with self._lock:
            if low:
                if key in self._waiting:
                    return
                self._low.pop(key, None)
                self._low[key] = (path, size)
                while len(self._low) > self._max_waiting:
                    self._low.popitem(last=False)
            else:
                self._low.pop(key, None)
                self._waiting.pop(key, None)
                self._waiting[key] = (path, size)
                while len(self._waiting) > self._max_waiting:
                    self._waiting.popitem(last=False)          # the oldest request: long scrolled past
            self._lock.notify()

    def clear(self) -> None:
        with self._lock:
            self._waiting.clear()

    def clear_low(self) -> None:
        with self._lock:
            self._low.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._waiting.clear()
            self._low.clear()
            self._lock.notify_all()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._closed and ((not self._waiting and not self._low)
                                            or (self._hold is not None and self._hold())):
                    self._lock.wait(0.03 if self._waiting or self._low else None)
                if self._closed:
                    return
                if self._waiting:
                    key, (path, size) = self._waiting.popitem(last=True)
                else:
                    key, (path, size) = self._low.popitem(last=False)
                self._active += 1
            try:
                img = read_image(path, size)
            except Exception:  # noqa: BLE001 - an unreadable file shows as blank
                img = QImage()
            with self._lock:
                self._active -= 1
                if self._closed:
                    return
            self.ready.emit(key, img)


class PhotoLister(QObject):
    """Lists the photos of a sol or a camera folder on a background thread, so a slow disk never freezes the
    window. Only the newest request matters: older ones still waiting are dropped. Read-ahead requests (low) wait
    for it. listed(key, photos) arrives on the GUI thread."""

    listed = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._jobs: collections.OrderedDict = collections.OrderedDict()   # kind -> (key, fn): newest of each kind
        self._low: list = []
        self._active = False                     # a normal request is being read
        self._closed = False
        threading.Thread(target=self._run, daemon=True, name="photo-lister").start()
        _SCREEN_READERS.add(self)

    def request(self, key, folder: str, rover: str, sol: int, camera: str | None, low: bool = False) -> None:
        self.request_call(key, functools.partial(photos_in, folder, rover, sol, camera), low)

    def request_call(self, key, fn: Callable[[], object], low: bool = False) -> None:
        """Any reading of the folder: fn() runs on the background thread; listed(key, its result) follows.
        A request replaces one of the same kind still waiting (key[0]: "view", "expand", …), not other kinds."""
        with self._cond:
            if low:
                self._low.append((key, fn))
                del self._low[:-4]               # only the latest few read-ahead lists
            else:
                kind = key[0] if isinstance(key, tuple) and key else None
                if kind in ("favorites", "flipbook"):   # what the grid shows: one at a time, like "view"
                    kind = "view"
                self._jobs.pop(kind, None)
                self._jobs[kind] = (key, fn)
            self._cond.notify()

    def clear_low(self) -> None:
        with self._cond:
            self._low.clear()

    def busy(self) -> bool:
        """A normal request is waiting or being read (read-ahead doesn't count)."""
        with self._cond:
            return not self._closed and (bool(self._jobs) or self._active)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._jobs.clear()
            self._low.clear()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._jobs and not self._low and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                if self._jobs:
                    _kind, (key, fn) = self._jobs.popitem(last=False)
                    self._active = True
                else:
                    key, fn = self._low.pop(0)
            try:
                photos = fn()
            except Exception:  # noqa: BLE001 - a folder that can't be read shows as empty
                photos = []
            with self._cond:
                self._active = False
                if self._closed:
                    return
            self.listed.emit(key, photos)


class CountWorker(QObject):
    """Counts the photos of each sol on a background thread; counted(folder, rover, {sol: n}) arrives in batches."""

    counted = Signal(str, str, object)       # object: a Python dict can't pass through a signal declared as dict

    def __init__(self, parent=None):
        super().__init__(parent)
        self._generation = 0
        self.running = False

    def start(self, folder: str, rover: str, sols: list[int]) -> None:
        self._generation += 1
        gen = self._generation

        def work():
            self.running = True
            try:
                count(gen)
            finally:
                if gen == self._generation:
                    self.running = False

        def count(gen):
            batch, last = {}, time.monotonic()
            for sol in sols:
                while screen_reads_pending() and gen == self._generation:
                    time.sleep(0.03)                # what's on screen reads the disk first
                if gen != self._generation:
                    return
                batch[sol] = sol_photo_count(folder, rover, sol)
                if time.monotonic() - last > 0.25:
                    self.counted.emit(folder, rover, batch)
                    batch, last = {}, time.monotonic()
            if batch and gen == self._generation:
                self.counted.emit(folder, rover, batch)

        threading.Thread(target=work, daemon=True, name="photo-counter").start()

    def stop(self) -> None:
        self._generation += 1
        self.running = False


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
        self.caption_fn: Callable[[PhotoFile], str] | None = None        # captions of their own (Favorites)
        self.is_favorite: Callable[[PhotoFile], bool] = lambda p: False  # draws a star on the thumbnail
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
        if self.caption_fn is not None:
            return self.caption_fn(p)
        return f"{p.camera} {p.number}" if self.show_camera else str(p.number)

    def photo_changed(self, path: str) -> None:
        """Draw this photo's cell again (its star or caption changed)."""
        row = self.rows.get(path)
        if row is not None:
            self.dataChanged.emit(self.index(row), self.index(row))

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
        pix = QPixmap.fromImage(img) if not img.isNull() else self.blank
        self.cache[path] = pix                   # also photos read ahead, not shown yet
        while len(self.cache) > self._cache_limit():
            self.cache.popitem(last=False)
        row = self.rows.get(path)
        if row is not None:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def read_ahead(self, photos: list[PhotoFile]) -> None:
        """Thumbnails likely to be wanted next, read while nothing else waits."""
        for p in photos:
            if p.path not in self.cache:
                self.loader.request(("thumb", p.path, self.size), p.path, self.size, low=True)


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
        p = index.data(PhotoGridModel.PhotoRole)
        if p is not None and self.model.is_favorite(p):        # a star in the corner, on a dark disc
            d = max(16, option.fontMetrics.height() + 2)
            star = QRectF(box.x() + 2, box.y() + 2, d, d)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(star)
            painter.setPen(QColor(STAR_COLOR))
            painter.drawText(star, Qt.AlignmentFlag.AlignCenter, "★")
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
    save_settings(): saves them. play_state_changed: the flipbook started or stopped, or the chosen photo changed
    (for the window's own Play button)."""

    play_state_changed = Signal()

    NEWEST = "newest"

    FAVORITES, FAV_FOLDER, FLIPBOOKS, FLIPBOOK = "favorites", "favfolder", "flipbooks", "flipbook"
    BROWSE_TABS = ("Sols", "Favorites", "Flipbooks")

    def __init__(self, settings, folder: Callable[[], str], save_settings: Callable[[], None], parent=None,
                 favorites: FavoritesStore | None = None):
        super().__init__(parent)
        self.settings, self.folder, self.save_settings = settings, folder, save_settings
        self.favs = favorites if favorites is not None else FavoritesStore(None)
        # Questions the page asks (replaced in tests, so no dialog ever opens there)
        self.ask_text: Callable[[str, str, str], str | None] = self._ask_text
        self.confirm: Callable[[str, str], bool] = self._confirm
        self.photo_time: Callable[[PhotoFile], str | None] | None = None      # when a photo was taken, if known
        self.fav_item: QTreeWidgetItem | None = None     # "All Favorite Photos", on the Favorites tab
        self.browse_tab = "Sols"
        self.flip_book: Flipbook | None = None          # the saved flipbook shown, if any
        self.flip_frames: list[PhotoFile] = []          # its frames in order (or what Play played last)
        self.missing_frames = 0                         # its photos that aren't in this photo folder
        self.play_set: list[PhotoFile] | None = None    # photos selected when the full-size view opened: Play's
        self.flip_reverse = False                       # play them (or the folder) backwards
        self.clipboard: list[PhotoFile] = []            # Ctrl+C, for pasting into a flipbook
        self.flip_ctx: tuple[str, int, str] | None = None    # (rover, sol, camera): the folder Play plays
        self.flip_source: list[PhotoFile] = []          # that camera folder's photos
        self.flip_pending: tuple | None = None          # the camera folder being read for the bar
        self.play_when_read = False                     # Play was pressed while it was being read
        self.pending_book_adds: dict[str, list] = {}    # flipbook id -> photos to add once its range is read
        # Thumbnails wait while a photo list is read: the list decides what's shown, so it gets the disk first.
        self.loader = ImageLoader(threads=3, hold=lambda: getattr(self, "lister", None) is not None
                                  and self.lister.busy())
        self.big_loader = ImageLoader(threads=1, max_waiting=8)       # previews and full-size images
        self.big_loader.ready.connect(self._big_ready)
        self.loader.ready.connect(self._note_size)
        self.counter = CountWorker(self)
        self.counter.counted.connect(self._counted)
        self.lister = PhotoLister(self)
        self.lister.listed.connect(self._listed)
        self.list_key = None                                          # (folder, rover, tree key) being listed
        self.select_path: str | None = None                           # a photo to choose once its list arrives
        self.goto_note: str | None = None                             # Go To Sol's "nearest sol" note
        self.expand_sol: int | None = None                            # a sol to open once its list arrives
        self.image_sizes: dict[str, str] = {}                         # path -> "1,200 × 900 pixels"
        self.loading_timer = QTimer(self, singleShot=True, interval=250)
        self.loading_timer.timeout.connect(self._show_loading)
        self.list_cache: collections.OrderedDict = collections.OrderedDict()   # (folder, rover, key) -> photos
        self.ahead_timer = QTimer(self, singleShot=True, interval=400)         # neighbours, once settled
        self.ahead_timer.timeout.connect(self._read_ahead_neighbours)
        self.goto_timer = QTimer(self, singleShot=True, interval=250)          # Go To Sol, once typing pauses
        self.goto_timer.timeout.connect(self._read_ahead_goto)
        self.counts: dict[tuple[str, str, int], int] = {}             # (folder, rover, sol) -> photos
        self.sol_items: dict[int, QTreeWidgetItem] = {}
        self.newest: collections.deque[PhotoFile] = collections.deque(maxlen=NEWEST_MAX)
        self.newest_item: QTreeWidgetItem | None = None
        self.loaded_folder: str | None = None
        self.current: PhotoFile | None = None
        self.view_key: tuple | None = None                            # what the grid shows
        self.full_cache: collections.OrderedDict = collections.OrderedDict()      # path -> QPixmap, a few
        self.extra_details: Callable[[PhotoFile], list[tuple[str, str]]] | None = None   # details from elsewhere
        # A button under them when a photo has no details of its own: (text, tooltip, what it does), or None
        self.details_action: Callable[[PhotoFile], tuple[str, str, Callable[[], None]] | None] | None = None
        self._action_fn: Callable[[], None] | None = None
        self.sort_timer = QTimer(self, singleShot=True, interval=1500)   # re-sort by count while counting
        self.sort_timer.timeout.connect(self.apply_sol_sort)
        self._build()
        self._fill_special_items()
        self.show_browse_tab("Sols", choose=False)
        self.tree.sort_state = (self._sol_sort()[0], self._sol_sort()[1] == Qt.SortOrder.AscendingOrder)
        self.tree.header().setSortIndicator(*self._sol_sort())
        self.model.is_favorite = lambda p: self.favs.is_favorite(fav_key(p.rover, p.sol, p.camera, p.number))
        self._build_flipbook()
        # F: favorite; P: play or pause the flipbook. Not while typing (a text box takes its own keys).
        self.shortcuts = {}
        # Delete takes frames out of a saved flipbook (never deletes a photo); Ctrl+C / Ctrl+V copy photos and paste
        # them into a flipbook. A text box keeps these keys for its own text.
        for key, slot in (("F", self.toggle_favorite), ("P", self._play_key), ("Del", self._delete_key),
                          ("Ctrl+C", self._copy_photos), ("Ctrl+V", self._paste_frames)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
            self.shortcuts[key] = sc
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
        self.goto.textEdited.connect(lambda _t: self.goto_timer.start())     # read it ahead while typing
        row.addWidget(self.goto, 1)
        bl.addLayout(row)
        # Tabs: the sols, Favorites (every rover) and saved Flipbooks, each with its own list
        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self.browse_buttons: dict[str, QPushButton] = {}
        for name in self.BROWSE_TABS:
            b = QPushButton(name)
            b.setObjectName("browseTab")
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _c=False, n=name: self.show_browse_tab(n))
            tabs.addWidget(b, 1)                       # the three share the card's width
            self.browse_buttons[name] = b
        bl.addLayout(tabs)
        self.browse_stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setObjectName("solTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Sol", "Photos"])
        self.tree.headerItem().setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        # A fixed width: fitting the column to its contents re-measures every sol each time a count arrives,
        # which froze the window for seconds while thousands of sols were counted.
        self.tree.header().setSectionResizeMode(1, self.tree.header().ResizeMode.Fixed)
        self._size_count_column()
        self.tree.setUniformRowHeights(True)
        # Click Sol or Photos to sort by it; click again to reverse (the choice is saved)
        self.tree.header().setSectionsClickable(True)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().sectionClicked.connect(self._sort_clicked)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._tree_chosen(cur))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(lambda pos: self._show_tree_menu(self.tree, pos))
        self.fav_tree = self._side_list(columns=2)
        self.books_tree = self._side_list(columns=1)
        for t in (self.tree, self.fav_tree, self.books_tree):
            self.browse_stack.addWidget(t)
        bl.addWidget(self.browse_stack, 1)
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
        # Like File Explorer: Ctrl+click adds or removes a photo, Shift+click selects a run, dragging draws a
        # selection rectangle (on empty space or across photos), Ctrl+A selects all.
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setSelectionRectVisible(True)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setItemDelegate(ThumbDelegate(self.model, self.grid))
        self.grid.setMouseTracking(True)                  # for the hover background
        self.grid.selectionModel().currentChanged.connect(lambda cur, _prev: self._grid_chosen(cur))
        self.grid.selectionModel().selectionChanged.connect(lambda *_a: self._update_fav_controls())
        self.grid.activated.connect(lambda _i: self.open_full())
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._show_grid_menu)
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
        self.fav_full_btn = QPushButton("☆  Favorite")
        self.fav_full_btn.setToolTip("Add to Favorites, or remove (F)")
        self.fav_full_btn.clicked.connect(self.toggle_favorite)
        bar.addWidget(self.fav_full_btn)
        fl.addLayout(bar)
        self.viewer = ImageView()
        self.viewer.zoom_changed.connect(self._show_zoom)
        fl.addWidget(self.viewer, 1)
        self.flip_bar = FlipbookBar()
        fl.addWidget(self.flip_bar)
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
        self.note_row = QWidget()
        nl = QHBoxLayout(self.note_row)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.addWidget(_label("Note", "rowHead"))
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Why it's a favorite (optional)")
        self.note_edit.editingFinished.connect(self._save_note)
        nl.addWidget(self.note_edit, 1)
        dl.addWidget(self.note_row)
        self.lookup_btn = QPushButton("")                 # Look Up Again / Try Again / Add API Key
        self.lookup_btn.setVisible(False)
        self.lookup_btn.clicked.connect(self._run_details_action)
        lookup_row = QHBoxLayout()
        lookup_row.addWidget(self.lookup_btn)
        lookup_row.addStretch()
        dl.addLayout(lookup_row)
        self.sel_note = _label("", "muted")              # "5 photos selected"
        self.sel_note.setVisible(False)
        dl.addWidget(self.sel_note)
        fav_row = QHBoxLayout()
        self.fav_btn = QPushButton("☆  Add to Favorites")
        self.fav_btn.setToolTip("F")
        self.fav_btn.clicked.connect(self.toggle_favorite)
        fav_row.addWidget(self.fav_btn)
        fav_row.addStretch()
        dl.addLayout(fav_row)
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
        if self.browse_tab != "Sols":                  # a rover is chosen to browse its sols
            self.show_browse_tab("Sols", choose=False)
        self._forget_listing()
        self.expand_sol = None
        self.goto_note = None
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
        if self._sol_sort() != (0, Qt.SortOrder.AscendingOrder):    # built lowest sol first
            self.apply_sol_sort()
        # On the Favorites or Flipbooks tab, the sols' choice is kept without showing it
        self.tree.blockSignals(self.browse_tab != "Sols")
        self._update_browse_note()
        self._count_missing(folder, rover, sols)
        try:
            if keep_view and chosen_key is not None:
                item = self._find_item(chosen_key)
                if item is not None:
                    self.tree.setCurrentItem(item)
                    return
            if not keep_view and sols:                 # the latest sol, where the newest photos are
                self.tree.setCurrentItem(self.sol_items[sols[-1]])
                self.tree.scrollToItem(self.sol_items[sols[-1]])
        finally:
            self.tree.blockSignals(False)
        if not sols and rover:
            self._set_empty("No sols in this rover's folder yet.")

    # ---- sorting the sols' list ----------------------------------------------------------------------------

    def _sol_sort(self) -> tuple[int, Qt.SortOrder]:
        """(column, order) from the saved choice: "sol" or "photos", "asc" or "desc"."""
        text = str(getattr(self.settings, "photo_sol_sort", "") or "sol:asc")
        column, _, order = text.partition(":")
        return (1 if column == "photos" else 0,
                Qt.SortOrder.DescendingOrder if order == "desc" else Qt.SortOrder.AscendingOrder)

    def _sort_clicked(self, column: int) -> None:
        """A click on Sol or Photos: sort by it (Photos: most first; Sol: lowest first), or reverse it."""
        old_column, old_order = self._sol_sort()
        if column == old_column:
            order = (Qt.SortOrder.DescendingOrder if old_order == Qt.SortOrder.AscendingOrder
                     else Qt.SortOrder.AscendingOrder)
        else:
            order = Qt.SortOrder.DescendingOrder if column == 1 else Qt.SortOrder.AscendingOrder
        self.settings.photo_sol_sort = (f"{'photos' if column == 1 else 'sol'}:"
                                        f"{'desc' if order == Qt.SortOrder.DescendingOrder else 'asc'}")
        self.save_settings()
        # Only sorts: what's chosen stays chosen, and the list shows the top of the new order (jumping to the
        # chosen sol looked as if sorting had chosen it)
        self.apply_sol_sort(keep_in_view=False)
        self.tree.scrollToTop()

    def apply_sol_sort(self, keep_in_view: bool = True) -> None:
        """Sort the sols' list the saved way (keep_in_view: scroll to the chosen row afterwards)."""
        column, order = self._sol_sort()
        self.tree.sort_state = (column, order == Qt.SortOrder.AscendingOrder)
        self.tree.header().setSortIndicator(column, order)
        cur = self.tree.currentItem()
        self.tree.sortItems(column, order)
        if cur is not None and keep_in_view:
            self.tree.scrollToItem(cur)

    def _sort_if_by_count(self) -> None:
        """Counts changed: if the list is sorted by them, sort it again (soon, once for many changes)."""
        if self._sol_sort()[0] == 1 and not self.sort_timer.isActive():
            self.sort_timer.start()

    def _size_count_column(self) -> None:
        """Wide enough for the header and any count (up to 999,999), in the tree's current font."""
        if not hasattr(self, "tree"):                # a change event before the page is built
            return
        self.tree.ensurePolished()                   # the style sheet's font
        fm = self.tree.fontMetrics()
        width = max(fm.horizontalAdvance("999,999"), fm.horizontalAdvance("Photos")) + 24
        self.tree.setColumnWidth(1, width)
        if hasattr(self, "fav_tree"):
            self.fav_tree.setColumnWidth(1, width)

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._size_count_column()
        super().changeEvent(event)

    def _make_sol_item(self, folder: str, rover: str, sol: int) -> QTreeWidgetItem:
        item = SortItem([f"Sol {sol}", ""], sort_key=sol)
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

    @staticmethod
    def _top_item(text: str, key, tip: str) -> QTreeWidgetItem:
        item = SortItem([text, ""], pinned=True)      # stays at the top of a sorted list
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setToolTip(0, tip)
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)
        return item

    def _side_list(self, columns: int) -> QTreeWidget:
        """The Favorites or Flipbooks tab's list: like the sols' list, without a header."""
        t = QTreeWidget()
        t.setObjectName("solTree")
        t.setColumnCount(columns)
        t.setHeaderHidden(True)
        t.setRootIsDecorated(False)
        t.setUniformRowHeights(True)
        t.header().setStretchLastSection(False)
        t.header().setSectionResizeMode(0, t.header().ResizeMode.Stretch)
        if columns > 1:
            t.header().setSectionResizeMode(1, t.header().ResizeMode.Fixed)
        t.currentItemChanged.connect(lambda cur, _prev: self._tree_chosen(cur))
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos, tree=t: self._show_tree_menu(tree, pos))
        return t

    def _fill_special_items(self) -> None:
        """The Favorites tab (All Favorite Photos, then favorite sols and camera folders) and the Flipbooks tab
        (after any change to them)."""
        self.fav_tree.clear()
        self.fav_item = self._top_item("★  All Favorite Photos", (self.FAVORITES,),
                                       "Your favorite photos, from every rover")
        self.fav_item.setText(1, f"{len(self.favs.photos()):,}")
        self.fav_tree.addTopLevelItem(self.fav_item)
        for f in self.favs.folders():               # sols and camera folders: choose one to go there
            item = QTreeWidgetItem([f.note or f.label(), ""])
            item.setData(0, Qt.ItemDataRole.UserRole, (self.FAV_FOLDER, f.rover, f.sol, f.camera))
            item.setToolTip(0, f.label() + (f"\n{f.note}" if f.note else ""))
            self.fav_tree.addTopLevelItem(item)
        self.books_tree.clear()
        for b in self.favs.flipbooks:
            item = QTreeWidgetItem([b.name])
            item.setData(0, Qt.ItemDataRole.UserRole, (self.FLIPBOOK, b.id))
            if b.photos is not None:
                item.setToolTip(0, f"{count_text(len(b.photos), 'chosen photo')} · {b.fps:g} photos/s")
            else:
                item.setToolTip(0, f"{b.rover} · Sol {b.sol:,} · {camera_name(b.camera)} · photos {b.first}–{b.last}"
                                   f" · {b.fps:g} photos/s")
            self.books_tree.addTopLevelItem(item)
        self._update_browse_note()

    def _add_newest_item(self) -> None:
        self.newest_item = self._top_item("Newest Downloads", (self.NEWEST,),
                                          "Photos saved since the app started, newest last, from every rover")
        self.newest_item.setText(1, f"{len(self.newest):,}")
        self.tree.insertTopLevelItem(0, self.newest_item)

    def _tree_of(self, key) -> QTreeWidget:
        if key and key[0] in (self.FAVORITES, self.FAV_FOLDER):
            return self.fav_tree
        if key and key[0] in (self.FLIPBOOKS, self.FLIPBOOK):
            return self.books_tree
        return self.tree

    def show_browse_tab(self, name: str, choose: bool = True) -> None:
        """Sols, Favorites or Flipbooks. choose: show what that tab's list has chosen (Favorites: All Favorite
        Photos when nothing is), as if it had just been clicked."""
        self.browse_tab = name
        for n, b in self.browse_buttons.items():
            b.setChecked(n == name)
        tree = {"Sols": self.tree, "Favorites": self.fav_tree, "Flipbooks": self.books_tree}[name]
        self.browse_stack.setCurrentWidget(tree)
        self._update_browse_note()
        if not choose:
            return
        cur = tree.currentItem()
        key = cur.data(0, Qt.ItemDataRole.UserRole) if cur is not None else None
        if name == "Favorites" and (key is None or key[0] == self.FAV_FOLDER):
            tree.blockSignals(True)                    # a folder would go to the Sols tab again
            tree.setCurrentItem(self.fav_item)
            tree.blockSignals(False)
            cur = self.fav_item
        if cur is not None:
            self._tree_chosen(cur)
        elif name == "Flipbooks":
            self._pause_flipbook(select=False)
            self._show_message((self.FLIPBOOKS,), "Flipbooks",
                               "Saved flipbooks are listed here. To make one, open a photo at full size, choose From "
                               "and To below it, and click Save Flipbook…" if not self.favs.flipbooks else
                               "Choose a flipbook to play it.")

    def _find_item(self, key) -> QTreeWidgetItem | None:
        if key[0] == self.NEWEST:
            return self.newest_item
        if key[0] == self.FAVORITES:
            return self.fav_item
        if key[0] in (self.FAV_FOLDER, self.FLIPBOOK):
            tree = self._tree_of(key)
            return next((tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                         if tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == key), None)
        if key[0] == self.FLIPBOOKS:
            return None
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

    def _counted(self, folder: str, rover: str, batch: dict) -> None:
        if folder != (self.loaded_folder or ""):     # counted before the photo folder changed
            return
        for sol, n in batch.items():
            self.counts[(folder, rover, sol)] = n
            if rover == self._rover() and sol in self.sol_items:
                self.sol_items[sol].setText(1, f"{n:,}")
        self._update_browse_note()
        self._sort_if_by_count()

    def _update_browse_note(self) -> None:
        if not hasattr(self, "browse_note"):
            return
        if self.browse_tab == "Favorites":
            self.browse_note.setText(f"{count_text(len(self.favs.photos()), 'favorite photo')} · "
                                     f"{count_text(len(self.favs.folders()), 'favorite folder')}")
            return
        if self.browse_tab == "Flipbooks":
            self.browse_note.setText(count_text(len(self.favs.flipbooks), "saved flipbook"))
            return
        folder, rover = self.loaded_folder or "", self._rover()
        if self.goto_note and rover:
            self.browse_note.setText(self.goto_note)
            return
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

    def _expanded(self, item: QTreeWidgetItem) -> None:
        """A sol opened with its arrow: its cameras, from the photo list in memory or read in the background (on
        a hard disk, reading them here kept the window waiting)."""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key or key[0] != "sol" or item.childCount():
            return
        folder, rover = self.loaded_folder or "", self._rover()
        list_key = (folder, rover, ("sol", key[1]))
        cached = self.list_cache.get(list_key)
        if cached is not None:
            self._cameras_from_list(list_key, cached)
            return
        self.lister.request_call(("expand", list_key), functools.partial(photos_in, folder, rover, key[1], None))

    def _cameras_from_list(self, list_key, photos: list[PhotoFile]) -> None:
        """A sol's camera rows and photo count, from its photo list."""
        folder, rover, key = list_key
        sol = key[1]
        item = self.sol_items.get(sol)
        if key[0] != "sol" or item is None:
            return
        if not item.childCount():
            self._add_cameras(item, sorted(collections.Counter(p.camera for p in photos).items()))
        self.counts[(folder, rover, sol)] = len(photos)
        item.setText(1, f"{len(photos):,}")
        self._update_browse_note()
        self._sort_if_by_count()

    def _fill_cameras(self, item: QTreeWidgetItem) -> None:
        """The sol's camera rows, read here and now (for going straight to a camera; opening a sol with its arrow
        reads them in the background)."""
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not key or key[0] != "sol" or item.childCount():
            return
        folder, rover, sol = self.loaded_folder or "", self._rover(), key[1]
        cams = cameras_in(folder, rover, sol)
        self._add_cameras(item, cams)
        total = sum(n for _, n in cams)
        self.counts[(folder, rover, sol)] = total
        item.setText(1, f"{total:,}")

    def _add_cameras(self, item: QTreeWidgetItem, cams: list[tuple[str, int]]) -> None:
        """The sol's camera rows: (camera, photos) for each camera folder that has photos."""
        sol = item.data(0, Qt.ItemDataRole.UserRole)[1]
        for cam, n in cams:
            child = SortItem([cam, f"{n:,}"], sort_key=cam)
            child.setData(0, Qt.ItemDataRole.UserRole, ("camera", sol, cam))
            child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            child.setToolTip(0, camera_name(cam))
            item.addChild(child)
        if not cams:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
        elif self._sol_sort() != (0, Qt.SortOrder.AscendingOrder):     # cameras in the list's order too
            item.sortChildren(*self._sol_sort())

    def _go_to_sol(self) -> None:
        text = self.goto.text().strip()
        if not text or not self.sol_items:
            return
        want = int(text)
        sols = sorted(self.sol_items)
        best = min(sols, key=lambda s: (abs(s - want), s))       # the sol itself, or the nearest one
        item = self.sol_items[best]
        if self.browse_tab != "Sols":
            self.show_browse_tab("Sols", choose=False)
        self.expand_sol = None
        if item.childCount():
            item.setExpanded(True)
        else:
            self.expand_sol = best                                 # its cameras come with its photo list
        self.tree.setCurrentItem(item)
        if self.expand_sol == best and self.list_key is None:    # shown already, so no list is coming
            self.expand_sol = None
            item.setExpanded(True)
        self.tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
        # Kept until something else is chosen (counts arriving used to replace it at once).
        self.goto_note = None if best == want else f"No photos for sol {want}; showing sol {best}, the nearest one."
        self._update_browse_note()

    # ---- what the grid shows -----------------------------------------------------------------------------

    def _tree_chosen(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        folder, rover = self.loaded_folder or "", self._rover()
        self._forget_listing()
        if self.goto_note:                             # Go To Sol sets its note again after choosing
            self.goto_note = None
            self._update_browse_note()
        self._pause_flipbook(select=False)
        if key[0] != self.FLIPBOOK and self.flip_book is not None:      # left a saved flipbook
            self.flip_book, self.flip_ctx = None, None
            self.flip_bar.set_book(None)
        self.flip_reverse = False
        if key[0] == self.NEWEST:
            photos = list(self.newest)
            title = f"Newest Downloads · {count_text(len(photos), 'photo')}"
            self._show_photos(key, photos, title, show_camera=True, empty="No photos downloaded yet.")
            return
        if key[0] == self.FAVORITES:
            self._show_favorites()
            return
        if key[0] == self.FLIPBOOKS:
            self._show_message(key, "Flipbooks",
                               "Saved flipbooks are listed under Flipbooks. To make one, open a photo at full size, "
                               "choose From and To below it, and click Save Flipbook…")
            return
        if key[0] == self.FAV_FOLDER:           # after this signal: going there rebuilds the list
            QTimer.singleShot(0, lambda: self._go_to_folder(key[1], key[2], key[3]))
            return
        if key[0] == self.FLIPBOOK:
            self._open_flipbook(key[1])
            return
        # Listed on a background thread: on a hard disk, reading a sol's folders the first time takes a moment.
        # What was shown stays until the list arrives ("Loading photos…" if that takes a while). A list read
        # recently or ahead is shown at once, and read again in the background in case the folder changed.
        list_key = (folder, rover, key)
        self.loader.clear()                            # thumbnails still waiting for the old view
        cam = key[2] if key[0] == "camera" else None
        cached = self.list_cache.get(list_key)
        if cached is not None:
            self.list_cache.move_to_end(list_key)
            self._apply_list(list_key, cached)
            self.lister.request(("check", list_key), folder, rover, key[1], cam)
            return
        self.list_key = list_key
        self.grid_title.setText(f"{self._view_title(rover, key)} · Loading…")
        self.lister.request(("view", list_key), folder, rover, key[1], cam)
        self.loading_timer.start()

    def _forget_listing(self) -> None:
        """A photo list still on its way is no longer wanted, nor what was being read ahead."""
        self.list_key = None
        self.loading_timer.stop()
        self.ahead_timer.stop()
        self.lister.clear_low()
        self.loader.clear_low()

    @staticmethod
    def _view_title(rover: str, key) -> str:
        return f"{rover} · Sol {key[1]} · {camera_name(key[2]) if key[0] == 'camera' else 'All Cameras'}"

    def _show_loading(self) -> None:
        if self.list_key is not None:
            self.model.set_photos([], True)
            self.empty.setText("Loading photos…")
            if self.stack.currentWidget() is not self.full_page:
                self.stack.setCurrentWidget(self.empty)

    def _remember_list(self, list_key, photos: list[PhotoFile]) -> None:
        self.list_cache[list_key] = photos
        self.list_cache.move_to_end(list_key)
        while len(self.list_cache) > LIST_CACHE_MAX:
            self.list_cache.popitem(last=False)

    def _listed(self, job, photos: list[PhotoFile]) -> None:
        if not (isinstance(job, tuple) and len(job) == 2):
            return
        kind, list_key = job
        if list_key[0] != (self.loaded_folder or ""):
            return                                     # another photo folder by now
        if kind == "flipsrc":                          # the flipbook bar's camera folder (any rover)
            self._flip_source_arrived(list_key, photos)
            return
        if kind == "bookadd":                          # a range flipbook's folder, to add photos to it
            self._book_range_arrived(list_key, photos)
            return
        if kind in ("favorites", "flipbook"):          # from every rover
            if list_key == self.list_key:
                if kind == "favorites":
                    self._apply_favorites(*photos)
                else:
                    self._apply_flipbook(list_key, photos)
            return
        if list_key[1] != self._rover():
            return                                     # another rover by now
        self._remember_list(list_key, photos)
        if kind == "ahead":
            self.model.read_ahead(photos[:self._first_screen()])
        elif kind == "expand":                         # a sol opened with its arrow
            self._cameras_from_list(list_key, photos)
        elif kind == "check":                          # shown from memory: did the folder change since?
            if (self.list_key is None and self.view_key == list_key[2]
                    and [p.path for p in photos] != [p.path for p in self.model.photos]):
                if self.current is not None:
                    self.select_path = self.current.path
                self._apply_list(list_key, photos)
        elif list_key == self.list_key:                # not an older choice
            self._apply_list(list_key, photos)

    def _first_screen(self) -> int:
        """About how many thumbnails fit in the grid."""
        cell = self.grid.gridSize()
        vp = self.grid.viewport().size()
        if cell.width() <= 0 or cell.height() <= 0:
            return 30
        return max(12, (max(1, vp.width() // cell.width())) * (vp.height() // cell.height() + 1))

    def _read_ahead(self, sols: list[int]) -> None:
        """Read these sols' photo lists and first thumbnails while nothing else needs the disk."""
        folder, rover = self.loaded_folder or "", self._rover()
        if not folder or not rover:
            return
        self.lister.clear_low()
        self.loader.clear_low()
        for sol in sols:
            list_key = (folder, rover, ("sol", sol))
            cached = self.list_cache.get(list_key)
            if cached is not None:
                self.model.read_ahead(cached[:self._first_screen()])
            else:
                self.lister.request(("ahead", list_key), folder, rover, sol, None, low=True)

    def _read_ahead_goto(self) -> None:
        """Go To Sol: the sol being typed, so it's ready when Enter is pressed."""
        text = self.goto.text().strip()
        if text.isdigit() and self.sol_items:
            want = int(text)
            self._read_ahead([min(self.sol_items, key=lambda s: (abs(s - want), s))])

    def _read_ahead_neighbours(self) -> None:
        """The sols before and after the one shown, for stepping through them."""
        if not self.view_key or self.view_key[0] not in ("sol", "camera") or self.list_key is not None:
            return
        sols = sorted(self.sol_items)
        try:
            i = sols.index(self.view_key[1])
        except ValueError:
            return
        self._read_ahead([sols[j] for j in (i + 1, i - 1) if 0 <= j < len(sols)])

    def _forget_lists(self, folder: str, rover: str, sol: int) -> None:
        """A download changed this sol's folders: its lists in memory are out of date."""
        for k in [k for k in self.list_cache if k[0] == folder and k[1] == rover and k[2][1] == sol]:
            del self.list_cache[k]

    def _apply_list(self, list_key, photos: list[PhotoFile]) -> None:
        """Show a sol's or camera's photos (its list just arrived, or was in memory)."""
        self.list_key = None
        self.loading_timer.stop()
        folder, rover, key = list_key
        sol = key[1]
        item = self.sol_items.get(sol)
        self._cameras_from_list(list_key, photos)     # the sol's cameras and count, from the same list
        if self.expand_sol == sol and item is not None:
            self.expand_sol = None
            item.setExpanded(True)
        title = f"{self._view_title(rover, key)} · {count_text(len(photos), 'photo')}"
        if key[0] == "sol":
            self._show_photos(key, photos, title, show_camera=True, empty="No photos in this sol's folder.")
        else:
            self._show_photos(key, photos, title, show_camera=False, empty="No photos in this camera's folder.")
        self.ahead_timer.start()                       # then the sols before and after, while the disk is free

    def _show_message(self, key, title: str, text: str) -> None:
        """A view with no photos, only a message (Flipbooks, or Favorites without a photo folder)."""
        self._show_photos(key, [], title, show_camera=True, empty=text)

    # ---- favorites -----------------------------------------------------------------------------------------

    def _show_favorites(self) -> None:
        folder, rover = self.loaded_folder or "", self._rover()
        key = (self.FAVORITES,)
        if not folder or not os.path.isdir(folder):
            self._show_message(key, "Favorites", "Choose a photo folder on the Download page to see your favorites.")
            return
        keys = [f.key for f in self.favs.photos()]
        self.list_key = (folder, rover, key)
        self.loader.clear()
        self.grid_title.setText("Favorites · Loading…")
        self.lister.request_call(("favorites", self.list_key), functools.partial(find_photos, folder, keys))
        self.loading_timer.start()

    def _apply_favorites(self, photos: list[PhotoFile], missing: int) -> None:
        self.list_key = None
        self.loading_timer.stop()
        title = f"Favorites · {count_text(len(photos), 'photo')}"
        if missing:
            title += f" · {missing:,} not in this photo folder"
        if not self.favs.photos():
            empty = ("No favorite photos yet. Choose a photo and click Add to Favorites, or press F. Sols and "
                     "camera folders you add (right-click them) are listed under Favorites.")
        else:
            empty = "None of your favorite photos are in this photo folder."
        self._show_photos((self.FAVORITES,), photos, title, show_camera=True, empty=empty,
                          caption_fn=self._favorite_caption)

    def _favorite_caption(self, p: PhotoFile) -> str:
        fav = self.favs.get(fav_key(p.rover, p.sol, p.camera, p.number))
        return (fav.note if fav and fav.note else "") or f"{p.sol} · {p.camera} {p.number}"   # rover: tooltip

    def _go_to_folder(self, rover: str, sol: int, camera: str | None) -> None:
        """A favorite sol or camera folder: show it in its rover's list."""
        if self.rover_box.findText(rover) < 0:
            self._show_message((self.FAVORITES,), "Favorites", f"There's no {rover} folder in this photo folder.")
            return
        if self._rover() != rover:
            self.rover_box.setCurrentText(rover)
        item = self.sol_items.get(sol)
        if item is None:
            self._show_message((self.FAVORITES,), "Favorites", f"Sol {sol:,} isn't in this photo folder.")
            return
        if self.browse_tab != "Sols":
            self.show_browse_tab("Sols", choose=False)
        if camera:
            self._fill_cameras(item)
            item.setExpanded(True)
            item = self._find_item(("camera", sol, camera))
        else:
            item.setExpanded(True)
        if self.tree.currentItem() is item:            # chosen already (before Favorites): show it again
            self._tree_chosen(item)
        else:
            self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)

    def _key_of(self, p: PhotoFile):
        return fav_key(p.rover, p.sol, p.camera, p.number)

    def selected_photos(self) -> list[PhotoFile]:
        """The photos selected in the grid, in the grid's order."""
        rows = sorted({i.row() for i in self.grid.selectionModel().selectedIndexes()})
        return [self.model.photos[r] for r in rows if r < len(self.model.photos)]

    def _fav_targets(self) -> list[PhotoFile]:
        """What F and the star buttons act on: at full size, the photo shown; in the grid, the selected photos
        (or the chosen one)."""
        if self.player.playing:
            p = self.player.shown()
            return [p] if p is not None else []
        if self.stack.currentWidget() is not self.full_page:
            selected = self.selected_photos()
            if len(selected) > 1:
                return selected
        return [self.current] if self.current is not None else []

    def toggle_favorite(self) -> None:
        """F, or the star buttons: add the photos to Favorites, or take them out (when all are in already)."""
        photos = self._fav_targets()
        if not photos:
            return
        keys = [self._key_of(p) for p in photos]
        if all(self.favs.is_favorite(k) for k in keys):
            self.favs.remove_many(keys)
        else:
            self.favs.add_many(keys)
        for p in photos:
            self.model.photo_changed(p.path)
        self._update_fav_controls()
        self._refresh_special()

    def _update_fav_controls(self) -> None:
        p = self.current
        fav = self.favs.get(self._key_of(p)) if p is not None else None
        selected = self.selected_photos() if self.stack.currentWidget() is not self.full_page else []
        n = len(selected)
        self.fav_btn.setEnabled(p is not None)
        self.fav_full_btn.setEnabled(p is not None)
        if n > 1:
            all_fav = all(self.favs.is_favorite(self._key_of(x)) for x in selected)
            self.fav_btn.setText(f"★  Remove {n:,} from Favorites" if all_fav else f"☆  Add {n:,} to Favorites")
            self.fav_btn.setToolTip("The selected photos (F)")
        else:
            self.fav_btn.setText("★  In Favorites" if fav else "☆  Add to Favorites")
            self.fav_btn.setToolTip("Remove from Favorites (F)" if fav else "Add to Favorites (F)")
        self.fav_full_btn.setText("★  Favorite" if fav else "☆  Favorite")
        self.sel_note.setText(f"{n:,} photos selected" if n > 1 else "")
        self.sel_note.setVisible(n > 1)
        self.note_row.setVisible(fav is not None and n <= 1)
        if fav is not None and not self.note_edit.hasFocus():
            self.note_edit.setText(fav.note)

    def _save_note(self) -> None:
        p = self.current
        if p is None:
            return
        self.favs.set_note(self._key_of(p), self.note_edit.text().strip())
        self.model.photo_changed(p.path)

    def _refresh_special(self) -> None:
        """Favorites' and Flipbooks' rows changed: rebuild them, keeping what's chosen (without choosing again)."""
        keys = {}
        for t in (self.fav_tree, self.books_tree):
            cur = t.currentItem()
            keys[t] = cur.data(0, Qt.ItemDataRole.UserRole) if cur is not None else None
            t.blockSignals(True)
        self._fill_special_items()
        for t, key in keys.items():
            if key is not None:
                item = self._find_item(key)
                if item is not None:
                    t.setCurrentItem(item)
            t.blockSignals(False)

    def _tree_menu(self, item: QTreeWidgetItem | None) -> QMenu | None:
        """Right-click: add a sol or camera folder to Favorites; edit or remove a favorite; rename or delete a
        flipbook."""
        key = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not key:
            return None
        menu = QMenu(self)
        rover = self._rover()
        if key[0] in ("sol", "camera"):
            k = fav_key(rover, key[1], key[2] if key[0] == "camera" else None)
            what = f"Sol {key[1]:,}" if key[0] == "sol" else f"the {key[2]} Folder"
            if self.favs.is_favorite(k):
                menu.addAction(f"Remove {what} from Favorites", lambda: self._remove_folder(k))
            else:
                menu.addAction(f"Add {what} to Favorites", lambda: self._add_folder(k))
        elif key[0] == self.FAV_FOLDER:
            k = fav_key(key[1], key[2], key[3])
            menu.addAction("Edit Note…", lambda: self._edit_folder_note(k))
            menu.addAction("Remove from Favorites", lambda: self._remove_folder(k))
        elif key[0] == self.FLIPBOOK:
            menu.addAction("Rename…", lambda: self._rename_flipbook(key[1]))
            menu.addAction("Delete…", lambda: self._delete_flipbook(key[1]))
        else:
            return None
        return menu

    def _grid_menu(self, index) -> QMenu | None:
        """Right-click on a photo: it becomes the chosen photo, and the menu has what the details card offers."""
        if not index.isValid():
            return None
        sm = self.grid.selectionModel()
        if not sm.isSelected(index):                   # like File Explorer: an unselected photo alone
            self.grid.setCurrentIndex(index)
        elif self.grid.currentIndex() != index:        # one of the selection: keep the selection
            sm.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        p = self.current
        if p is None:
            return None
        photos = self.selected_photos() or [p]
        n = len(photos)
        menu = QMenu(self)
        all_fav = all(self.favs.is_favorite(self._key_of(x)) for x in photos)
        if n == 1:
            text = "★  Remove from Favorites" if all_fav else "☆  Add to Favorites"
        else:
            text = f"★  Remove {n:,} Photos from Favorites" if all_fav else f"☆  Add {n:,} Photos to Favorites"
        book = self._book_view()
        if book is not None:                           # a saved flipbook's frames: editing them
            frames = "Frame" if n == 1 else f"{n:,} Frames"
            act = menu.addAction(f"Remove {frames} from Flipbook", self._remove_frames)
            act.setShortcut(QKeySequence("Del"))
            menu.addAction(f"Duplicate {frames}", self._duplicate_frames)
            act = menu.addAction("Copy", self._copy_photos)
            act.setShortcut(QKeySequence("Ctrl+C"))
            act = menu.addAction(f"Paste {count_text(len(self.clipboard), 'Photo')} After" if self.clipboard
                                 else "Paste After", self._paste_frames)
            act.setShortcut(QKeySequence("Ctrl+V"))
            act.setEnabled(bool(self.clipboard))
            menu.addAction("Move Earlier", lambda: self._move_frames(-1))
            menu.addAction("Move Later", lambda: self._move_frames(1))
            menu.addAction("Reverse All Frames", self._reverse)
            menu.addSeparator()
            act = menu.addAction("Play From Here", self._play_key)
            act.setShortcut(QKeySequence("P"))
            menu.addSeparator()
        menu.addAction(text, self.toggle_favorite)
        sub = menu.addMenu("Add to Flipbook" if n == 1 else f"Add {n:,} Photos to Flipbook")
        sub.addAction("New Flipbook…", lambda: self._add_to_new_flipbook(photos))
        others = [b for b in self.favs.flipbooks if book is None or b.id != book.id]
        if others:
            sub.addSeparator()
            for b in others:
                sub.addAction(b.name, lambda b_id=b.id: self._add_to_flipbook(b_id, photos))
        if book is None:
            act = menu.addAction("Copy", self._copy_photos)
            act.setShortcut(QKeySequence("Ctrl+C"))
            act.setToolTip("To paste into a flipbook")
        menu.addSeparator()
        menu.addAction("View Full Size", self.open_full)
        menu.addAction("Show in Folder", lambda: show_in_folder(p.path))
        return menu

    def _notify(self, text: str) -> None:
        """A short message in the grid's title, for a few seconds."""
        before = self.grid_title.text()
        self.grid_title.setText(text)
        QTimer.singleShot(4000, lambda: self.grid_title.text() == text and self.grid_title.setText(before))

    def _unique_in_time_order(self, photos: list[PhotoFile]) -> list[tuple]:
        """Their keys, each once, in the order they were taken (when known for all), else in the grid's order."""
        seen, keys = set(), []
        for p in self._in_time_order(photos):
            k = self._key_of(p)
            if k not in seen:
                seen.add(k)
                keys.append(k)
        return keys

    def _add_to_new_flipbook(self, photos: list[PhotoFile]) -> None:
        keys = self._unique_in_time_order(photos)
        if not keys:
            return
        first = photos[0]
        one_folder = len({(p.rover, p.sol, p.camera) for p in photos}) == 1
        default = (f"{first.rover} · Sol {first.sol:,} · {camera_name(first.camera)}" if one_folder
                   else f"{len(keys):,} chosen photos")
        name = self.ask_text("New Flipbook", "Name:", default)
        if not name or not name.strip():
            return
        b = Flipbook(name=name.strip(), rover=first.rover, sol=first.sol, camera=first.camera, first=0, last=0,
                     same_size=False, photos=[list(k) for k in keys])
        self.favs.save_flipbook(b)
        self._refresh_special()
        self._notify(f"Made the flipbook “{b.name}” with {count_text(len(keys), 'photo')}")

    def _add_to_flipbook(self, book_id: str, photos: list[PhotoFile]) -> None:
        """Add photos to a saved flipbook. One made from a From–To range becomes a list of its photos first (its
        camera folder is read in the background)."""
        b = self.favs.flipbook(book_id)
        if b is None:
            return
        keys = [list(k) for k in self._unique_in_time_order(photos)]
        if b.photos is not None:
            self._finish_book_add(b, keys)
            return
        self.pending_book_adds.setdefault(book_id, []).extend(keys)
        folder = self.loaded_folder or ""
        self.lister.request_call(("bookadd", (folder, b.rover, (self.FLIPBOOK, b.id))),
                                 functools.partial(photos_in, folder, b.rover, b.sol, b.camera))

    def _book_range_arrived(self, list_key, photos: list[PhotoFile]) -> None:
        b = self.favs.flipbook(list_key[2][1])
        keys = self.pending_book_adds.pop(list_key[2][1], [])
        if b is None or not keys:
            return
        if b.photos is None:
            inside = [[b.rover, b.sol, b.camera, p.number] for p in photos if b.first <= p.number <= b.last]
            if not inside:
                self._notify(f"Couldn't read the photos of “{b.name}” in this photo folder; nothing was added")
                return
            b.photos = inside
        self._finish_book_add(b, keys)

    def _finish_book_add(self, b: Flipbook, keys: list[list]) -> None:
        have = {tuple(k) for k in b.photos}
        added = [k for k in keys if tuple(k) not in have]   # at the end, in order (photos it has already: once)
        b.photos = b.photos + added
        self.favs.save_flipbook(b)
        self._refresh_special()
        self._notify(f"Added {count_text(len(added), 'photo')} to the flipbook “{b.name}”")
        if self.view_key == (self.FLIPBOOK, b.id):     # shown now: show its new photos
            self._open_flipbook(b.id)

    def _show_grid_menu(self, pos) -> None:
        menu = self._grid_menu(self.grid.indexAt(pos))
        if menu is not None:
            menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _show_tree_menu(self, tree: QTreeWidget, pos) -> None:
        menu = self._tree_menu(tree.itemAt(pos))
        if menu is not None:
            menu.exec(tree.viewport().mapToGlobal(pos))

    def _add_folder(self, k) -> None:
        self.favs.add(*k)
        self._refresh_special()

    def _remove_folder(self, k) -> None:
        self.favs.remove(k)
        self._refresh_special()

    def _edit_folder_note(self, k) -> None:
        fav = self.favs.get(k)
        if fav is None:
            return
        text = self.ask_text("Favorite Note", f"Note for {fav.label()}:", fav.note)
        if text is not None:
            self.favs.set_note(k, text.strip())
            self._refresh_special()

    def _ask_text(self, title: str, label: str, default: str) -> str | None:
        text, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Normal, default)
        return text if ok else None

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    # ---- the flipbook --------------------------------------------------------------------------------------

    def _build_flipbook(self) -> None:
        self.player = FlipbookPlayer(self._frame_size, self)
        _SCREEN_READERS.add(self.player.loader)
        self.player.frame.connect(self._flip_frame)
        self.player.status.connect(self.flip_bar.status.setText)
        self.player.finished.connect(lambda: self._pause_flipbook(select=True, finished=True))
        bar = self.flip_bar
        bar.play_btn.clicked.connect(self._play_key)
        bar.playing_changed.connect(lambda _playing: self.play_state_changed.emit())
        bar.speed.valueChanged.connect(self._flip_speed)
        bar.loop_box.toggled.connect(self._flip_options)
        bar.same_box.toggled.connect(self._flip_options)
        bar.reverse_btn.clicked.connect(self._reverse)
        bar.edit_btn.clicked.connect(self._edit_frames)
        bar.save_btn.clicked.connect(lambda: self._save_flipbook(as_new=False))
        bar.save_new_btn.clicked.connect(lambda: self._save_flipbook(as_new=True))
        bar.delete_btn.clicked.connect(lambda: self.flip_book and self._delete_flipbook(self.flip_book.id))

    def _frame_size(self) -> int:
        ratio = self.viewer.devicePixelRatioF()
        return max(640, int(max(self.viewer.width(), self.viewer.height()) * ratio))

    def _camera_photos(self, p: PhotoFile) -> list[PhotoFile] | None:
        """All photos of p's camera folder, if they're in memory (None: they have to be read)."""
        folder = self.loaded_folder or ""
        cached = self.list_cache.get((folder, p.rover, ("camera", p.sol, p.camera)))
        if cached is not None:
            return cached
        if (p.rover == self._rover() and self.view_key and self.view_key[0] in ("sol", "camera")
                and self.view_key[1] == p.sol):
            return [x for x in self.model.photos if x.camera == p.camera]
        return None

    def _book_view(self) -> Flipbook | None:
        """The saved flipbook shown in the grid (its frames), if one is."""
        b = self.flip_book
        return b if b is not None and self.view_key == (self.FLIPBOOK, b.id) else None

    def _sync_flip_ctx(self, p: PhotoFile) -> None:
        """Without a selection, Play plays the shown photo's camera folder: it's read (in the background if it isn't
        in memory) when a photo of another folder is shown."""
        if self._book_view() is not None or self.play_set:
            return
        ctx = (p.rover, p.sol, p.camera)
        if ctx == self.flip_ctx:
            return
        self.flip_ctx = ctx
        source = self._camera_photos(p)
        if source is None:
            self.flip_source = []
            self.flip_pending = ctx
            self.flip_bar.status.setText("Reading the camera folder…")
            folder = self.loaded_folder or ""
            self.lister.request_call(("flipsrc", (folder, p.rover, ("camera", p.sol, p.camera))),
                                     functools.partial(photos_in, folder, p.rover, p.sol, p.camera))
            return
        self._set_flip_source(source)

    def _set_flip_source(self, source: list[PhotoFile]) -> None:
        self.flip_pending = None
        self.flip_source = source
        self._show_flip_status()

    def _show_flip_status(self) -> None:
        """What Play will play, when it isn't playing."""
        if self.player.playing:
            return
        b = self._book_view()
        back = " backwards" if self.flip_reverse and b is None else ""
        if b is not None:
            text = f"{count_text(len(self.flip_frames), 'frame')}"
        elif self.play_set:
            text = f"Plays the {count_text(len(self.play_set), 'selected photo')}{back}"
        elif self.flip_pending is not None:
            text = "Reading the camera folder…"
        elif self.flip_ctx:
            text = (f"Plays {camera_name(self.flip_ctx[2])}: {count_text(len(self.flip_source), 'photo')}{back}"
                    if self.flip_source else "")
        else:
            text = ""
        self.flip_bar.status.setText(text)

    def _flip_source_arrived(self, list_key, photos: list[PhotoFile]) -> None:
        _folder, rover, key = list_key
        self._remember_list(list_key, photos)
        if self.flip_pending == (rover, key[1], key[2]) and self.flip_ctx == self.flip_pending:
            self._set_flip_source(photos)
            if self.play_when_read:
                self.play_when_read = False
                self._play()

    def _in_time_order(self, photos: list[PhotoFile]) -> list[PhotoFile]:
        """In the order they were taken when that's known for all of them; otherwise as they are."""
        if self.photo_time is not None and photos:
            times = [self.photo_time(x) for x in photos]
            if all(times):
                order = sorted(range(len(photos)), key=lambda i: (times[i], photos[i].number))
                return [photos[i] for i in order]
        return list(photos)

    def _frames(self) -> list[PhotoFile]:
        """What Play plays: a saved flipbook's frames as they are; otherwise the selected photos (or the shown
        photo's camera folder) in the order they were taken, backwards if Reverse is on."""
        if self._book_view() is not None:
            return list(self.flip_frames)
        frames = self._in_time_order(self.play_set or self.flip_source)
        return frames[::-1] if self.flip_reverse else frames

    def play_or_pause(self) -> None:
        """The window's Play button (bottom right): the same as the flipbook bar's Play."""
        self._play_key()

    def _play_key(self) -> None:
        """P, or Play: play the flipbook from the photo shown (opening the full-size view), or pause it."""
        if self.player.playing:
            self._pause_flipbook(select=True)
            return
        if self.current is None:
            return
        if self.stack.currentWidget() is not self.full_page:
            self.open_full()
        self._play()

    def _play(self) -> None:
        p = self.player.shown() if self.player.frames else None
        p = self.current if p is None or (self.current and self.current.path != p.path) else p
        if p is None:
            return
        self._sync_flip_ctx(p)
        if self.flip_pending is not None:              # its camera folder is still being read: play after
            self.play_when_read = True
            return
        frames = self._frames()
        if not frames:
            self.flip_bar.status.setText("Nothing to play")
            return
        if self._book_view() is None:
            self.flip_frames = frames                  # what Save Flipbook keeps
        # From the photo shown (a camera folder, or Play From Here in a flipbook); a selection from its start
        start = 0 if self.play_set else next((i for i, x in enumerate(frames) if x.path == p.path), 0)
        if self._book_view() is not None and self.current is not None:      # the chosen frame (duplicates)
            row = self.grid.currentIndex().row()
            if 0 <= row < len(frames) and frames[row].path == p.path:
                start = row
        self._apply_flip_options()
        self.player.load(frames, start)
        self.player.play()
        self.flip_bar.set_playing(True)

    def _apply_flip_options(self) -> None:
        bar = self.flip_bar
        self.player.set_fps(bar.speed.value())
        self.player.loop = bar.loop_box.isChecked()
        self.player.same_size = bar.same_box.isChecked()

    def _flip_frame(self, p: PhotoFile, pix: QPixmap) -> None:
        if self.stack.currentWidget() is self.full_page:
            self.viewer.set_image(pix)

    def _pause_flipbook(self, select: bool, finished: bool = False) -> None:
        """Stop playing. select: the frame shown becomes the chosen photo (details, full size), if it was playing
        (or just finished)."""
        if not hasattr(self, "player"):
            return
        was = self.player.playing or finished
        self.play_when_read = False
        self.player.pause()
        self.flip_bar.set_playing(False)
        p = self.player.shown()
        if not was or not select or p is None:
            return
        if self.current is not None and self.current.path == p.path:
            if self.stack.currentWidget() is self.full_page:
                self._load_full(p)               # the full-size image instead of the frame
            return
        row = self.model.rows.get(p.path)
        if row is not None:
            self.grid.setCurrentIndex(self.model.index(row))
        else:
            self.current = p
            self._show_details(p)
            if self.stack.currentWidget() is self.full_page:
                self._load_full(p)

    def _save_book_options(self) -> None:
        """A saved flipbook keeps its speed and options as they're changed."""
        b = self._book_view()
        if b is None:
            return
        bar = self.flip_bar
        b.fps, b.loop, b.same_size = float(bar.speed.value()), bar.loop_box.isChecked(), bar.same_box.isChecked()
        self.favs.save_flipbook(b)
        self._refresh_special()

    def _flip_speed(self, fps: int) -> None:
        self.player.set_fps(fps)
        self._save_book_options()

    def _flip_options(self) -> None:
        self._apply_flip_options()
        self._save_book_options()

    def _restart_if_playing(self) -> None:
        if self.player.playing:                        # play the new order from the frame shown
            self.player.pause()
            self._play()

    def _reverse(self) -> None:
        """Reverse: a saved flipbook's frames are put in the opposite order (and saved); otherwise what Play plays
        is played backwards."""
        if self._book_view() is not None:
            self._set_book_frames(self.flip_frames[::-1])
            self._notify("Reversed the flipbook's frames")
        else:
            self.flip_reverse = not self.flip_reverse
            self._show_flip_status()
        self._restart_if_playing()

    def _edit_frames(self) -> None:
        """Edit Frames: the saved flipbook's frames as thumbnails."""
        if self._book_view() is None:
            return
        self.close_full()
        self._notify("Delete removes frames · Ctrl+C, Ctrl+V copy and paste · right-click for more · P plays")

    def _save_flipbook(self, as_new: bool) -> None:
        """Save Flipbook… (what Play plays: the selected photos or the camera folder, in its order), or Save as
        New… (a copy of the saved flipbook shown)."""
        book = self._book_view()
        if book is not None and as_new:
            frames = list(self.flip_frames)
            default = f"{book.name} (copy)"
        else:
            p = self.current or self.player.shown()
            if p is not None:
                self._sync_flip_ctx(p)
            if self.flip_pending is not None:          # not yet: its folder is being read
                return
            frames = self._frames()
            if not frames:
                return
            one = len({(x.rover, x.sol, x.camera) for x in frames}) == 1
            default = (f"{frames[0].rover} · Sol {frames[0].sol:,} · {camera_name(frames[0].camera)}" if one
                       else f"{count_text(len(frames), 'photo')}")
        name = self.ask_text("Save as New Flipbook" if as_new else "Save Flipbook", "Name:", default)
        if not name or not name.strip():
            return
        bar = self.flip_bar
        first = frames[0]
        b = Flipbook(name=name.strip(), rover=first.rover, sol=first.sol, camera=first.camera, first=0, last=0,
                     fps=float(bar.speed.value()), loop=bar.loop_box.isChecked(),
                     same_size=bar.same_box.isChecked(), photos=[list(self._key_of(x)) for x in frames])
        self.favs.save_flipbook(b)
        self._refresh_special()
        self._notify(f"Saved the flipbook “{b.name}” ({count_text(len(frames), 'frame')}): it's in the Flipbooks "
                     f"tab")

    def _open_flipbook(self, book_id: str) -> None:
        b = self.favs.flipbook(book_id)
        folder = self.loaded_folder or ""
        if b is None:
            return
        self.flip_book = b
        key = (self.FLIPBOOK, b.id)
        if not folder or not os.path.isdir(folder):
            self._show_message(key, b.name, "Choose a photo folder on the Download page to play this flipbook.")
            return
        self.list_key = (folder, b.rover, key)
        self.grid_title.setText(f"Flipbook · {b.name} · Loading…")
        if b.photos is not None:                       # chosen photos, from any folders
            self.lister.request_call(("flipbook", self.list_key),
                                     functools.partial(find_photos, folder, [tuple(k) for k in b.photos]))
        else:
            self.lister.request(("flipbook", self.list_key), folder, b.rover, b.sol, b.camera)
        self.loading_timer.start()

    def _apply_flipbook(self, list_key, source) -> None:
        """A saved flipbook's photos arrived (its camera folder, or its chosen photos with how many are missing):
        its settings go into the bar, and it plays."""
        self.list_key = None
        self.loading_timer.stop()
        b = self.flip_book
        if b is None or list_key[2] != (self.FLIPBOOK, b.id):
            return
        missing = 0
        if isinstance(source, tuple):                  # its photos: (found, how many aren't in this folder)
            frames, missing = source
        else:                                          # an older flipbook made of a From–To range: kept as its
            frames = self._in_time_order([x for x in source if b.first <= x.number <= b.last])   # photos now
            if frames:
                b.photos = [list(self._key_of(x)) for x in frames]
                self.favs.save_flipbook(b)
        self.flip_frames = frames
        self.flip_ctx, self.flip_pending, self.play_set = None, None, None
        bar = self.flip_bar
        for w in (bar.speed, bar.loop_box, bar.same_box):
            w.blockSignals(True)
        bar.speed.setValue(int(round(b.fps)))
        bar._show_speed(bar.speed.value())
        bar.loop_box.setChecked(b.loop)
        bar.same_box.setChecked(b.same_size)
        for w in (bar.speed, bar.loop_box, bar.same_box):
            w.blockSignals(False)
        bar.set_book(b.name)
        self.missing_frames = missing
        self._show_photos(list_key[2], frames, self._book_title(), show_camera=True,
                          empty="None of this flipbook's photos are in this photo folder.")
        self._show_flip_status()
        if frames:
            self.open_full()
            self._play()

    def _book_title(self) -> str:
        b = self.flip_book
        title = f"Flipbook · {b.name} · {count_text(len(self.flip_frames), 'frame')}"
        if self.missing_frames:
            title += f" · {self.missing_frames:,} not in this photo folder"
        return title

    # ---- editing a saved flipbook's frames (its grid shows them in order) ----------------------------------

    def _frame_rows(self) -> list[int]:
        """The selected frames' rows (or the chosen frame's)."""
        rows = sorted({i.row() for i in self.grid.selectionModel().selectedIndexes()})
        if not rows and self.grid.currentIndex().isValid():
            rows = [self.grid.currentIndex().row()]
        return [r for r in rows if 0 <= r < len(self.flip_frames)]

    def _set_book_frames(self, frames: list[PhotoFile], select: list[int] | None = None) -> None:
        """The saved flipbook's new frames: saved, shown in the grid (select: rows to select after)."""
        b = self._book_view()
        if b is None:
            return
        self.player.pause()
        self.flip_bar.set_playing(False)
        b.photos = [list(self._key_of(x)) for x in frames]
        self.favs.save_flipbook(b)
        self.flip_frames = list(frames)
        self.model.set_photos(self.flip_frames, True)
        self.grid_title.setText(self._book_title())
        self._refresh_special()
        self._show_flip_status()
        rows = [r for r in (select or []) if 0 <= r < len(frames)]
        sm = self.grid.selectionModel()
        if rows:
            sm.setCurrentIndex(self.model.index(rows[-1]), QItemSelectionModel.SelectionFlag.ClearAndSelect)
            for r in rows[:-1]:
                sm.select(self.model.index(r), QItemSelectionModel.SelectionFlag.Select)
            self.grid.scrollTo(self.model.index(rows[-1]))
        elif frames:
            self.grid.setCurrentIndex(self.model.index(0))
        else:
            self.current = None
            self._show_details(None)
            self.empty.setText("This flipbook has no frames left. Paste photos into it (Ctrl+V) or delete it.")
            self.stack.setCurrentWidget(self.empty)

    def _remove_frames(self) -> None:
        rows = self._frame_rows()
        if self._book_view() is None or not rows:
            return
        gone = set(rows)
        frames = [x for i, x in enumerate(self.flip_frames) if i not in gone]
        self._set_book_frames(frames, [min(rows[0], len(frames) - 1)] if frames else [])
        self._notify(f"Removed {count_text(len(rows), 'frame')} from the flipbook")

    def _duplicate_frames(self) -> None:
        rows = self._frame_rows()
        if self._book_view() is None or not rows:
            return
        copies = [self.flip_frames[r] for r in rows]
        at = rows[-1] + 1
        frames = self.flip_frames[:at] + copies + self.flip_frames[at:]
        self._set_book_frames(frames, list(range(at, at + len(copies))))

    def _copy_photos(self) -> None:
        """Ctrl+C: the selected photos (or frames) to paste into a flipbook."""
        if self._book_view() is not None:
            self.clipboard = [self.flip_frames[r] for r in self._frame_rows()]
        else:
            self.clipboard = self.selected_photos() or ([self.current] if self.current else [])
        if self.clipboard:
            self._notify(f"Copied {count_text(len(self.clipboard), 'photo')}: paste them into a flipbook with "
                         f"Ctrl+V")

    def _paste_frames(self) -> None:
        """Ctrl+V: the copied photos into the flipbook shown, after the chosen frame."""
        if self._book_view() is None:
            if self.clipboard:
                self._notify("Paste works in a flipbook: open one in the Flipbooks tab, then press Ctrl+V")
            return
        if not self.clipboard:
            return
        rows = self._frame_rows()
        at = rows[-1] + 1 if rows else len(self.flip_frames)
        frames = self.flip_frames[:at] + self.clipboard + self.flip_frames[at:]
        self._set_book_frames(frames, list(range(at, at + len(self.clipboard))))
        self._notify(f"Pasted {count_text(len(self.clipboard), 'photo')}")

    def _move_frames(self, step: int) -> None:
        """Move the selected frames one place earlier (-1) or later (+1)."""
        rows = self._frame_rows()
        if self._book_view() is None or not rows:
            return
        frames = list(self.flip_frames)
        picked = set(rows)
        order = list(range(len(frames)))
        for r in (rows if step < 0 else reversed(rows)):
            i = order.index(r)
            j = i + step
            if 0 <= j < len(order) and order[j] not in picked:
                order[i], order[j] = order[j], order[i]
        new_frames = [frames[i] for i in order]
        self._set_book_frames(new_frames, [order.index(r) for r in rows])

    def _delete_key(self) -> None:
        """Delete: only takes frames out of a saved flipbook (never deletes a photo)."""
        if self._book_view() is not None and self.stack.currentWidget() is not self.full_page:
            self._remove_frames()

    def _rename_flipbook(self, book_id: str) -> None:
        b = self.favs.flipbook(book_id)
        if b is None:
            return
        name = self.ask_text("Rename Flipbook", "Name:", b.name)
        if name and name.strip():
            b.name = name.strip()
            self.favs.save_flipbook(b)
            if self.flip_book is not None and self.flip_book.id == b.id:
                self.flip_bar.set_book(b.name)
                if self._book_view() is not None:
                    self.grid_title.setText(self._book_title())
            self._refresh_special()

    def _delete_flipbook(self, book_id: str) -> None:
        b = self.favs.flipbook(book_id)
        if b is None or not self.confirm("Delete Flipbook", f"Delete the flipbook “{b.name}”? Its photos are "
                                                            f"kept."):
            return
        self.favs.delete_flipbook(book_id)
        if self.flip_book is not None and self.flip_book.id == book_id:
            self.flip_book = None
            self.flip_bar.set_book(None)
        self._refresh_special()

    # ---- showing photos ------------------------------------------------------------------------------------

    def _show_photos(self, key, photos: list[PhotoFile], title: str, show_camera: bool, empty: str,
                     caption_fn: Callable[[PhotoFile], str] | None = None) -> None:
        self.view_key = key
        self.grid_title.setText(title)
        self.model.caption_fn = caption_fn
        self.model.set_photos(photos, show_camera)
        self.full_cache.clear()
        if photos:
            if self.stack.currentWidget() is not self.full_page:
                self.stack.setCurrentWidget(self.grid)
            row = self.model.rows.get(self.select_path, 0) if self.select_path else 0
            self.select_path = None
            self.grid.setCurrentIndex(self.model.index(row))
            if row:
                self.grid.scrollTo(self.model.index(row))
            else:
                self.grid.scrollToTop()
        else:
            self.empty.setText(empty)
            self.stack.setCurrentWidget(self.empty)
            self.current = None
            self._show_details(None)

    def _set_empty(self, text: str) -> None:
        self._forget_listing()
        self.tree.blockSignals(True)
        self.tree.clear()
        self.sol_items = {}
        self.newest_item = None
        if self.newest:
            self._add_newest_item()
        self.tree.blockSignals(False)
        self.view_key = None
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
        # Read with the thumbnail or preview, on a background thread (opening the file here waited for the disk).
        rows.append(("Image Size", self.image_sizes.get(p.path, "…")))
        size = p.size
        if size is None:              # a photo just saved by a download (its folder entry is fresh in memory)
            try:
                size = os.path.getsize(p.path)
            except OSError:
                size = None
        rows.append(("File Size", fmt_file_size(size) if size is not None else "File not found"))
        rows.append(("File", p.name))         # its folder is the rover, sol and camera above (full path: tooltip)
        return rows

    def _show_details(self, p: PhotoFile | None) -> None:
        has = p is not None
        self.full_btn.setEnabled(has)
        self.folder_btn.setEnabled(has)
        self._update_fav_controls()
        self.play_state_changed.emit()
        if not has:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("")
            self._set_detail_rows([])
            self.detail_note.setText("Choose a photo to see its details.")
            self.detail_note.setVisible(True)
            self._update_lookup_button()
            return
        self.detail_note.setVisible(False)
        self._set_detail_rows(self.details_for(p))
        self._update_lookup_button()
        self.preview.setPixmap(QPixmap())
        self.big_loader.request(("preview", p.path), p.path, max(self.preview.width(), self.preview.height()) * 2)

    def refresh_details(self) -> None:
        """Show the current photo's details again (for example after more details arrived)."""
        if self.current is not None:
            self._set_detail_rows(self.details_for(self.current))
        self._update_lookup_button()

    def _update_lookup_button(self) -> None:
        action = self.details_action(self.current) if self.details_action and self.current is not None else None
        self._action_fn = action[2] if action else None
        self.lookup_btn.setVisible(action is not None)
        if action:
            self.lookup_btn.setText(action[0])
            self.lookup_btn.setToolTip(action[1])

    def _run_details_action(self) -> None:
        if self._action_fn is not None:
            self._action_fn()
            self.refresh_details()

    def _note_size(self, key, img: QImage) -> None:
        """Keep a photo's own size, read with its thumbnail or preview; show it if it's the chosen photo."""
        if not isinstance(key, tuple) or len(key) < 2 or not isinstance(key[1], str):
            return
        path = key[1]
        if path in self.image_sizes:
            return
        w, _, h = img.text(FULL_SIZE).partition("x")
        if not (w.isdigit() and h.isdigit()):
            if not img.isNull() or key[0] != "preview":
                return
            text = "Unknown"                       # the preview couldn't read the file either
        else:
            text = f"{int(w):,} × {int(h):,} pixels"
        if len(self.image_sizes) > 20_000:
            self.image_sizes.clear()
        self.image_sizes[path] = text
        if self.current is not None and self.current.path == path:
            self.refresh_details()

    def _big_ready(self, key, img: QImage) -> None:
        self._note_size(key, img)
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
        if self._book_view() is None:                  # several photos selected: Play plays those
            selected = self.selected_photos()
            self.play_set = selected if len(selected) > 1 else None
        self.stack.setCurrentWidget(self.full_page)
        self._load_full(self.current)
        self._show_flip_status()
        self.viewer.setFocus()

    def close_full(self) -> None:
        self._pause_flipbook(select=True)
        if self.stack.currentWidget() is self.full_page:
            self.stack.setCurrentWidget(self.grid if self.model.photos else self.empty)
            row = self.model.rows.get(self.current.path) if self.current else None
            if row is not None:
                self.grid.scrollTo(self.model.index(row))
            self.grid.setFocus()

    def _load_full(self, p: PhotoFile) -> None:
        self._sync_flip_ctx(p)                  # the flipbook bar's From and To: this photo's camera folder
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
        self._pause_flipbook(select=True)       # stepping starts from the frame the flipbook showed
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
        self._forget_lists(folder, p.rover, p.sol)
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
        if self._sol_sort() != (0, Qt.SortOrder.AscendingOrder):    # its place in the list's own order
            self.apply_sol_sort()
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
        self._sort_if_by_count()

    def show_photo(self, p: PhotoFile) -> None:
        """Show one photo in Newest Downloads (it's there if a download saved it), else in its camera folder."""
        if self.browse_tab != "Sols":
            self.show_browse_tab("Sols", choose=False)
        if p in self.newest and self.newest_item is not None:
            self.tree.setCurrentItem(self.newest_item)
        else:
            if self.rover_box.currentText() != p.rover:
                self.rover_box.setCurrentText(p.rover)
            item = self._find_item(("camera", p.sol, p.camera))
            if item is not None:
                self.select_path = p.path
                self.tree.setCurrentItem(item)
                if self.list_key is not None:          # chosen when the camera's photo list arrives
                    return
                self.select_path = None
        row = self.model.rows.get(p.path)
        if row is not None:
            self.grid.setCurrentIndex(self.model.index(row))
            self.grid.scrollTo(self.model.index(row))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._size_count_column()
        QTimer.singleShot(0, self.refresh)

    def shutdown(self) -> None:
        self.counter.stop()
        self.player.close()
        self.lister.close()
        self.loader.close()
        self.big_loader.close()

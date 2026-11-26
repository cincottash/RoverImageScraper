"""Desktop app for Rover Image Scraper.

Run:  python rover_scraper/gui.py

All downloading and scanning runs on a background thread. The window polls shared state on a timer, so it stays
responsive, and Stop works like Ctrl+C in the command line: finished photos are kept, and starting again resumes.
"""
from __future__ import annotations

import collections
import html
import math
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import traceback

from PySide6.QtCore import QEvent, QObject, QProcess, Qt, QTimer, QUrl
from PySide6.QtGui import (QActionGroup, QColor, QDesktopServices, QFont, QIcon, QIntValidator, QKeySequence,
                           QPalette)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
                               QComboBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from api import SUPPORTED_ROVERS, ApiError, Cancelled, MarsVistaClient, load_api_key, save_api_key
from photo_library import camera_name, rovers_in, sols_in
from favorites import FAVORITES_FILE, FavoritesStore, parse_favorites_list
from slow_moments import SlowMoments
from photo_page import PhotosPage
from pds import PdsClient, RequestCounter, RoverSources, is_pds_rover, latest_sol
from download import NASA_STATUS, make_session
from photo_details import (DB_NAME, SIZE_CHECKS_AT_ONCE, DetailsStore, SolLookups, SolResult, describe,
                           describe_sol, folder_key, look_up_sol)
from plan import last_sol_on_disk
from ratelimit import PERIODS, FileLimiter, RequestTracker
from scan import RoverStatus, ScanStore, full_check, quick_scan
from scraper import Report, Scraper
from dataclasses import asdict
from settings import OPACITY_MIN, Settings
from state import (APP_DATA_FILES, NO_DATA_FOLDER, Unavailable, chosen_state_dir, copy_app_data, default_state_dir,
                   keeps_app_data, save_state_dir, session_state_dir, standard_state_dir)



def use_state_dir(path: str) -> None:
    """Point the app at its data folder (settings, scan results, request counts, refused photos)."""
    global STATE_DIR, SETTINGS_PATH, SCANS_PATH, REQUESTS_PATH, UNAVAILABLE_PATH, PAUSE_PATH
    STATE_DIR = path
    SETTINGS_PATH = os.path.join(path, "settings.json")
    SCANS_PATH = os.path.join(path, "scans.json")
    REQUESTS_PATH = os.path.join(path, "requests.json")
    UNAVAILABLE_PATH = os.path.join(path, "unavailable.json")
    PAUSE_PATH = os.path.join(path, "paused.json")      # no longer written; removed if an old version left one


use_state_dir(default_state_dir())

AVG_PHOTO_MB = 0.2          # observed average across Curiosity and Perseverance downloads
LATEST_THUMB = 96           # pixels: the Latest Photo thumbnail in the Progress card
# Where keyring keeps the API key on this system, for the window's wording.
KEY_STORE = ("Windows Credential Manager" if os.name == "nt" else
             "the macOS Keychain" if sys.platform == "darwin" else "your system's keyring")
ALL_ROVERS = SUPPORTED_ROVERS                        # Library status always shows every rover, ticked or not


# ---- formatting ------------------------------------------------------------------------------------------

def fmt(n) -> str:
    return "N/A" if n is None else f"{n:,}"


def count(n: int, word: str) -> str:
    """A number with its unit, singular or plural: "1 sol", "2,514 photos"."""
    return f"{n:,} {word}" + ("" if n == 1 else "s")


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


def _variant(base: dict, bg, card, border, text, muted, accent, accent_hover, input, track,
             accent_text="#FFFFFF", **rest) -> dict:
    """A theme built on Light or Dark: its own surfaces and accent, the base's status colors."""
    return {**base, **dict(bg=bg, card=card, border=border, text=text, muted=muted, accent=accent,
                           accent_hover=accent_hover, accent_text=accent_text, input=input, track=track,
                           log_bg=input), **rest}


# Themes: (group, menu name, setting value, colors). View > Theme has "Match operating system", then a submenu per
# group. Each is built on Light or Dark (status colors) with its own surfaces and accent:
#   _variant(base, bg, card, border, text, muted, accent, accent_hover, input, track, accent_text=...)
# Most of the Dark and Light themes use the published colors of popular open-source editor color schemes (Nord, Dracula,
# Solarized, Gruvbox, Catppuccin and others; see the README); the "Space" themes are this app's own.
L, D = LIGHT, DARK
THEMES = [
    ("Light Themes","Light", "light", LIGHT),
    ("Dark Themes","Dark", "dark", DARK),
    ("Dark Themes","High Contrast", "high_contrast", _variant(D, "#000000", "#000000", "#FFFFFF", "#FFFFFF", "#D0D0D0",
                                                          "#FFD400", "#FFE45C", "#000000", "#333333",
                                                          accent_text="#000000", ok="#7CFF7C", warn="#FFE45C",
                                                          err="#FF8080")),

    # ---- Space (this app's own) ----
    ("Space", "Mars Day", "mars_day", _variant(L, "#FFE7DB", "#FFF4EC", "#F4CFBB", "#3B2418", "#8A6A58",
                                               "#C2410C", "#9A3412", "#FFF8F4", "#FDDBCB")),
    ("Space", "Red Planet", "red_planet", _variant(D, "#1F0E0D", "#2A1614", "#40221E", "#F3E3DC", "#B89A8E",
                                                   "#E2582B", "#F07A4F", "#221010", "#381F1B")),
    ("Space", "Martian Dusk", "martian_dusk", _variant(D, "#211517", "#2B1E22", "#403138", "#ECE4DC", "#A8A1A6",
                                                       "#6FA8DC", "#9CC4EA", "#1E1212", "#36282E",
                                                       accent_text="#0B1622")),
    ("Space", "Deep Space", "deep_space", _variant(D, "#020D20", "#09182E", "#192E4D", "#E1E7F5", "#8D9AB8",
                                                   "#4F7CFF", "#7A9CFF", "#051227", "#132542")),
    ("Space", "Nebula", "nebula", _variant(D, "#141124", "#1D1732", "#332853", "#ECE6F8", "#A699C2",
                                           "#A855F7", "#C084FC", "#17132C", "#292143")),
    ("Space", "Aurora", "aurora", _variant(D, "#0A1816", "#12221F", "#213939", "#DDF2EE", "#8EB3AD",
                                           "#14B8A6", "#2DD4BF", "#0E1D1B", "#1B312D", accent_text="#04201C")),
    ("Space", "Starlight", "starlight", _variant(D, "#100509", "#160F15", "#25212B", "#F1F3F6", "#8C94A3",
                                                 "#38BDF8", "#7DD3FC", "#12080D", "#1F1A23", accent_text="#031722")),
    ("Space", "Solar Flare", "solar_flare", _variant(D, "#121409", "#1D1C0F", "#35311A", "#F6EEDB", "#B8A57E",
                                                     "#F59E0B", "#FBBF24", "#16170C", "#2A2815",
                                                     accent_text="#2A1A00")),
    ("Space", "Eclipse", "eclipse", _variant(D, "#0E0700", "#140F03", "#2F271C", "#F2EEE9", "#A39C94",
                                             "#FF8A3D", "#FFA866", "#120A00", "#231D15", accent_text="#1F0E00")),
    ("Space", "Supernova", "supernova", _variant(D, "#130618", "#1D0D25", "#3B1D4A", "#F7E8FF", "#B9A0C9",
                                                 "#EC4899", "#F472B6", "#0F0411", "#2B1536")),
    ("Space", "Neptune", "neptune", _variant(D, "#06142B", "#0B1E3D", "#1A3561", "#DCE8FF", "#8EA6CF",
                                             "#3B82F6", "#60A5FA", "#051026", "#142B52")),
    ("Space", "Jupiter Storm", "jupiter_storm", _variant(D, "#1B130E", "#261B14", "#433024", "#F4E6D8",
                                                         "#B8A08A", "#D97B45", "#E89A6C", "#160F0B", "#34251B",
                                                         accent_text="#1F0F05")),
    ("Space", "Titan Haze", "titan_haze", _variant(D, "#1C1606", "#27200F", "#44391C", "#F3E9CC", "#B5A77E",
                                                   "#D4A017", "#E8B93A", "#171204", "#362C14",
                                                   accent_text="#1F1700")),
    ("Space", "Comet", "comet", _variant(D, "#051519", "#0C2128", "#1A3B46", "#E0F4F8", "#8AAFB9",
                                         "#22D3EE", "#67E8F9", "#021115", "#13313B", accent_text="#042027")),
    ("Space", "Milky Way", "milky_way", _variant(D, "#0D0A19", "#161424", "#2B2843", "#EEEDFB", "#A3A1C4",
                                                 "#8B8CF8", "#A9AAFB", "#070512", "#221F36",
                                                 accent_text="#0E0D24")),
    ("Space", "Earthrise", "earthrise", _variant(D, "#0A0F0C", "#12191D", "#24303B", "#E3EBF3", "#8F9DAE",
                                                 "#278A5F", "#2F9E6E", "#070B05", "#1C252C")),
    ("Space", "Lunar", "lunar", _variant(L, "#F4EBF9", "#FFF6FF", "#DDD5E5", "#202226", "#686C74",
                                         "#4B5563", "#374151", "#FFFDFF", "#E9E1F0")),
    ("Space", "Ice Moon", "ice_moon", _variant(L, "#D4F3FF", "#E0FBFF", "#B8E1F3", "#14283A", "#5C7489",
                                               "#0284C7", "#0369A1", "#E5FDFF", "#C2E9FA")),
    ("Space", "Europa", "europa", _variant(L, "#DEFFF6", "#EBFFFE", "#C0EDE1", "#12302C", "#557571",
                                           "#0F8C7E", "#0B6E63", "#F0FFFF", "#CDF8EB")),
    ("Space", "Venus", "venus", _variant(L, "#FFFEDA", "#FFFFE9", "#F1E8B5", "#3A3016", "#7E7150",
                                         "#B7791F", "#975A16", "#FFFFF6", "#FAF3C3")),
    ("Space", "Saturn Rings", "saturn_rings", _variant(L, "#E9F1DD", "#F1FCED", "#D8D9BA", "#352B1D", "#7C6E58",
                                                       "#A0703A", "#80592C", "#F1FFF6", "#E1E5CA")),
    ("Space", "Pluto", "pluto", _variant(L, "#F7E0E6", "#FFE9F0", "#E4C8CD", "#2F2524", "#776765",
                                         "#B4566A", "#944457", "#FFEEF6", "#EED3D8")),
    ("Space", "Cosmic Latte", "cosmic_latte", _variant(L, "#FFFBE3", "#FFFFF2", "#F9E5C4", "#33291A", "#7A6C55",
                                                       "#8B5E34", "#6E4A28", "#FFFFFB", "#FFEFD2")),

    # ---- more dark themes (open-source editor color schemes) ----
    ("Dark Themes","Nord", "nord", _variant(D, "#2E343F", "#3B4251", "#4C5669", "#ECEFF4", "#AEB7C8",
                                              "#88C0D0", "#8FBCBB", "#292E38", "#434C5D", accent_text="#2E3440")),
    ("Dark Themes","Dracula", "dracula", _variant(D, "#251F28", "#2C2732", "#484455", "#F8F8F2", "#A4ADD3",
                                                    "#BD93F9", "#FF79C6", "#1F1920", "#484455",
                                                    accent_text="#282A36")),
    ("Dark Themes","Solarized Dark", "solarized_dark", _variant(D, "#002B36", "#073642", "#0E4B59", "#EEE8D5",
                                                                  "#93A1A1", "#268BD2", "#2AA198", "#00222B",
                                                                  "#0B4250")),
    ("Dark Themes","Gruvbox Dark", "gruvbox_dark", _variant(D, "#162121", "#222928", "#4A4B45", "#EBDBB2",
                                                              "#A89984", "#FABD2F", "#FE8019", "#131D1D",
                                                              "#373936", accent_text="#282828")),
    ("Dark Themes","One Dark", "one_dark", _variant(D, "#1F252A", "#262C33", "#3C4450", "#ABB2BF", "#8B919C",
                                                      "#61AFEF", "#8AC4F5", "#1C2226", "#2A3139",
                                                      accent_text="#1E2227")),
    ("Dark Themes","Tokyo Night", "tokyo_night", _variant(D, "#0C1822", "#0F1D2B", "#1C3148", "#C0CAF5",
                                                            "#9AA5CE", "#7AA2F7", "#9ABDF5", "#07151E",
                                                            "#1C3148", accent_text="#16161E")),
    ("Dark Themes","Catppuccin Mocha", "catppuccin_mocha", _variant(D, "#141827", "#1A1E30", "#2E3246", "#CDD6F4",
                                                                      "#A6ADC8", "#CBA6F7", "#B4BEFE", "#0E111D",
                                                                      "#2E3246", accent_text="#1E1E2E")),
    ("Dark Themes","Catppuccin Macchiato", "catppuccin_macchiato",
     _variant(D, "#172135", "#1D283F", "#2F3B54", "#CAD3F5", "#A5ADCB", "#C6A0F6", "#B7BDF8", "#111A2A", "#2F3B54",
              accent_text="#24273A")),
    ("Dark Themes","Catppuccin Frappé", "catppuccin_frappe",
     _variant(D, "#292F41", "#30374B", "#41485F", "#C6D0F5", "#A5ADCE", "#CA9EE6", "#BABBF1", "#232939", "#41485F",
              accent_text="#303446")),
    ("Dark Themes","Rosé Pine", "rose_pine", _variant(D, "#201624", "#261C2E", "#2D223A", "#E0DEF4", "#908CAA",
                                                        "#C4A7E7", "#EBBCBA", "#1B131F", "#2D223A",
                                                        accent_text="#191724")),
    ("Dark Themes","Rosé Pine Moon", "rose_pine_moon", _variant(D, "#24233B", "#2B2944", "#3A3757", "#E0DEF4",
                                                                  "#908CAA", "#C4A7E7", "#EA9A97", "#201F35",
                                                                  "#3A3757", accent_text="#232136")),
    ("Dark Themes","Everforest Dark", "everforest_dark", _variant(D, "#283135", "#2E383D", "#48555B", "#D3C6AA",
                                                                    "#9DA9A0", "#A7C080", "#83C092", "#242D2F",
                                                                    "#3E4B50", accent_text="#2D353B")),
    ("Dark Themes","Kanagawa", "kanagawa", _variant(D, "#181019", "#211A23", "#383141", "#DCD7BA", "#A6A69C",
                                                      "#7E9CD8", "#7FB4CA", "#150D16", "#2C2532",
                                                      accent_text="#16161D")),
    ("Dark Themes","Ayu Dark", "ayu_dark", _variant(D, "#080C15", "#0F141D", "#232934", "#BFBDB6", "#8A8F98",
                                                      "#E6B450", "#FFB454", "#050710", "#1C222C",
                                                      accent_text="#0B0E14")),
    ("Dark Themes","Ayu Mirage", "ayu_mirage", _variant(D, "#1A2533", "#1F2A39", "#2F3C4F", "#CCCAC2", "#9DA2AC",
                                                          "#FFCC66", "#FFD580", "#15202C", "#283446",
                                                          accent_text="#1F2430")),
    ("Dark Themes","Palenight", "palenight", _variant(D, "#282535", "#2E2C3E", "#3F3E58", "#C8CCE6", "#959DCB",
                                                        "#C792EA", "#82AAFF", "#232030", "#39314A",
                                                        accent_text="#1E2130")),
    ("Dark Themes","Night Owl", "night_owl", _variant(D, "#011627", "#0B2942", "#1D3B53", "#D6DEEB", "#8FAABF",
                                                        "#82AAFF", "#C792EA", "#01111D", "#13344F",
                                                        accent_text="#011627")),
    ("Dark Themes","Cobalt2", "cobalt2", _variant(D, "#14232C", "#173548", "#285A79", "#FFFFFF", "#A7C0D2",
                                                    "#FFC600", "#FFD84D", "#112737", "#214E6C",
                                                    accent_text="#193549")),
    ("Dark Themes","Synthwave '84", "synthwave84", _variant(D, "#22172E", "#2A2039", "#4A3169", "#F4EEFF",
                                                              "#B6A8D2", "#FF7EDB", "#F97E72", "#1E1329",
                                                              "#382653", accent_text="#241B2F")),
    ("Dark Themes","Tomorrow Night", "tomorrow_night", _variant(D, "#211D1E", "#2C282B", "#3C393E", "#C5C8C6",
                                                                  "#969896", "#81A2BE", "#B294BB", "#1A1516",
                                                                  "#3C393E", accent_text="#1D1F21")),
    ("Dark Themes","Monokai", "monokai", _variant(D, "#1F1F1B", "#282821", "#4A483C", "#F8F8F2", "#A59F85",
                                                    "#A6E22E", "#E6DB74", "#1B1B16", "#3F3D31",
                                                    accent_text="#272822")),
    ("Dark Themes","Zenburn", "zenburn", _variant(D, "#383838", "#3F3F3F", "#5B5B5B", "#DCDCCC", "#A8A897",
                                                    "#8CD0D3", "#93E0E3", "#313131", "#4F4F4F",
                                                    accent_text="#2B2B2B")),

    # ---- more light themes ----
    ("Light Themes","Nord Light", "nord_light", _variant(L, "#DCEAF9", "#E3F0FD", "#CFDFF2", "#2E3440",
                                                           "#4C566A", "#5E81AC", "#4C6A93", "#F6FFFF", "#CFDFF2")),
    ("Light Themes","Solarized Light", "solarized_light", _variant(L, "#EEE6D6", "#FDF4E4", "#DDD4C2", "#3B4D54",
                                                                     "#657B83", "#268BD2", "#1F6FA8", "#FFF9F0",
                                                                     "#E4DBC9")),
    ("Light Themes","Gruvbox Light", "gruvbox_light", _variant(L, "#F3E5BD", "#FCF1C8", "#D6C4A2", "#3C3836",
                                                                 "#7C6F64", "#AF3A03", "#8F3002", "#FFFAE4",
                                                                 "#ECDBB3")),
    ("Light Themes","One Light", "one_light", _variant(L, "#E9E0DF", "#F9F0ED", "#D3CBCA", "#383A42", "#696C77",
                                                         "#4078F2", "#2F5FD0", "#FEF5F2", "#DFD7D6")),
    ("Light Themes","Catppuccin Latte", "catppuccin_latte", _variant(L, "#E5E7EE", "#EEEFF4", "#CBCED9",
                                                                       "#4C4F69", "#6C6F85", "#8839EF", "#7028D4",
                                                                       "#FEFDFE", "#DBDEE7")),
    ("Light Themes","Rosé Pine Dawn", "rose_pine_dawn", _variant(L, "#FFF7EF", "#FFFDF5", "#F1DDDB", "#575279",
                                                                   "#797593", "#907AA9", "#286983", "#FFFFFD",
                                                                   "#FFECE3")),
    ("Light Themes","Everforest Light", "everforest_light", _variant(L, "#EBEBD0", "#F9F6DF", "#D9D8BA",
                                                                       "#3A454B", "#5F6B63", "#6F8A0B", "#5A7008",
                                                                       "#FBFBEB", "#E2E2C8")),
    ("Light Themes","Tokyo Night Day", "tokyo_night_day", _variant(L, "#D5D5DA", "#E1E1E6", "#C4C7D9", "#1F3B80",
                                                                     "#4F5E96", "#2E7DE9", "#1D63C7", "#F2F2F5",
                                                                     "#CBCCD5")),
    ("Light Themes","Kanagawa Lotus", "kanagawa_lotus", _variant(L, "#E4DDAF", "#F1ECBB", "#D4CEA2", "#3C3C4E",
                                                                   "#625F52", "#4D699B", "#3B5481", "#F9F6D5",
                                                                   "#E6DB9F")),
    ("Light Themes","Ayu Light", "ayu_light", _variant(L, "#DFEFEE", "#EBFAF8", "#CBDCDD", "#4C5157", "#787B80",
                                                         "#C86A14", "#A85710", "#EEFDFB", "#D6E6E5")),
    ("Light Themes","Tomorrow", "tomorrow", _variant(L, "#E5E8E0", "#F5F8F0", "#CCCFC8", "#4D4D4C", "#7A7A78",
                                                       "#4271AE", "#345A8B", "#F5F8F0", "#DCDFD8")),
    ("Light Themes","Paper", "paper", _variant(L, "#F1FDF0", "#FBFFFD", "#DCE6D4", "#2B2A27", "#6E6A62",
                                                 "#3D5A80", "#2C4460", "#FBFFFF", "#E6F0DF")),
    ("Light Themes","Sepia", "sepia", _variant(L, "#F2E0C7", "#FBECD7", "#E0C9A7", "#4A3B28", "#7A6A52",
                                                 "#8A5A2B", "#6E4721", "#FFF5E3", "#EAD5B6")),
]
THEME_COLORS = {key: colors for _, _, key, colors in THEMES}
THEME_GROUPS = ("Space", "Dark Themes", "Light Themes")


def system_is_dark(app: QApplication) -> bool:
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:      # Qt older than 6.5
        return False


def app_icon_path() -> str | None:
    """The window icon: bundled next to the code in the packaged app, in packaging/ when run from source."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(getattr(sys, "_MEIPASS", here), "icon.svg"),
                 os.path.join(here, os.pardir, "packaging", "icon.svg")):
        if os.path.isfile(path):
            return path
    return None


def _is_light(color: str) -> bool:
    c = QColor(color)
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue() > 150


def _check_icon(dark: bool = False) -> str:
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
    return os.path.join(here, "check_dark.svg" if dark else "check.svg").replace("\\", "/")


def apply_theme(app: QApplication, t: dict, only=None) -> None:
    """Style the whole app with theme t, or only one widget and its children (only=widget, for previews)."""
    target = only if only is not None else app
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
    target.setPalette(pal)
    target.setStyleSheet(f"""
        QWidget {{ font-family: "Segoe UI"; font-size: 10pt; }}
        QMainWindow, QWidget#root {{ background: {t['bg']}; }}
        QMenuBar {{ background: {t['card']}; padding: 0; margin: 0; spacing: 0; }}
        QMenuBar::item {{ padding: 4px 8px; margin: 0; border-radius: 0; background: transparent; }}
        QMenuBar::item:selected {{ background: {t['track']}; }}
        QMenu {{ background: {t['card']}; border: 1px solid {t['border']}; padding: 4px; }}
        QMenu::item {{ padding: 6px 28px 6px 26px; border-radius: 4px; color: {t['text']}; }}
        QMenu::item:selected {{ background: {t['accent']}; color: {t['accent_text']}; }}
        QMenu::item:disabled {{ color: {t['muted']}; }}
        QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}
        QMenu QWidget#menuRow, QMenu QWidget#menuRow QLabel {{ background: transparent; color: {t['text']}; }}
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
        QLineEdit {{ background: {t['input']}; border: 1px solid {t['border']}; border-radius: 6px;
                              padding: 5px 8px; }}
        QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
        QLineEdit[missing="true"] {{ border: 1px solid {t['warn_border']}; background: {t['warn_bg']}; }}
        QLineEdit:disabled {{ color: {t['muted']}; }}
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
                                       image: url({_check_icon(_is_light(t['accent_text']))}); }}
        QCheckBox::indicator:disabled {{ background: {t['track']}; border-color: {t['border']}; }}
        QCheckBox::indicator:checked:disabled {{ background: {t['muted']}; border-color: {t['muted']}; }}
        QPushButton#pageTab {{ background: transparent; border: 1px solid transparent; border-radius: 6px;
                              padding: 5px 16px; color: {t['muted']}; font-weight: 600; }}
        QPushButton#pageTab:hover {{ color: {t['text']}; background: {t['track']}; }}
        QPushButton#pageTab:checked {{ background: {t['accent']}; color: {t['accent_text']}; }}
        QPushButton#browseTab {{ background: transparent; border: 1px solid transparent; border-radius: 6px;
                                padding: 4px 4px; color: {t['muted']}; font-weight: 600; min-width: 0; }}
        QPushButton#browseTab:hover {{ color: {t['text']}; background: {t['track']}; }}
        QPushButton#browseTab:checked {{ background: {t['accent']}; color: {t['accent_text']}; }}
        QTreeWidget#solTree, QListView#photoGrid {{ background: {t['input']}; border: 1px solid {t['border']};
                                                    border-radius: 6px; }}
        QTreeWidget#solTree::item {{ padding: 2px 0; }}
        QTreeWidget#solTree::item:hover {{ background: {t['track']}; }}
        QTreeWidget#solTree::item:selected {{ background: {t['accent']}; color: {t['accent_text']}; }}
        QComboBox {{ combobox-popup: 0; }}
        QHeaderView::section {{ background: {t['card']}; color: {t['muted']}; border: none;
                               border-bottom: 1px solid {t['border']}; padding: 4px 6px; }}
        QFrame#card QLabel#photoPreview, QFrame#card QLabel#latestThumb {{ background: {t['input']};
                               border: 1px solid {t['border']}; border-radius: 6px; color: {t['muted']}; }}
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


# ---- first-run setup -------------------------------------------------------------------------------------

def free_space_text(path: str) -> str:
    """Free space on the drive a path is (or would be) on, e.g. "D: has 1.7 TB free"."""
    probe = os.path.abspath(path) if path else ""
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return ""
        probe = parent
    if not probe:
        return ""
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return ""
    drive = os.path.splitdrive(probe)[0]
    return f"{drive} has {fmt_size(free)} free" if drive else f"{fmt_size(free)} free"


def program_location() -> str:
    return sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)


class NumberChoice(QComboBox):
    """A dropdown of whole numbers with a spin-box-like value()/setValue()."""

    def __init__(self, low: int, high: int):
        super().__init__()
        self.low, self.high = low, high
        self.addItems([str(n) for n in range(low, high + 1)])

    def value(self) -> int:
        return int(self.currentText())

    def setValue(self, n: int) -> None:
        self.setCurrentText(str(min(self.high, max(self.low, int(n)))))


class SetupDialog(QDialog):
    """First-run setup (and Settings > Change App Data Folder): where the app keeps its own files and, on first
    run, where photos go. Each can be on a different drive from the program."""

    def __init__(self, parent=None, current: str | None = None, first_run: bool = True):
        super().__init__(parent)
        self.first_run = first_run
        self.old_dir = current or standard_state_dir()
        self.setWindowTitle("First-Time Setup" if first_run else "Change App Data Folder")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(8)
        if first_run:
            lay.addWidget(label("Welcome to Rover Image Scraper", "big"))
            lay.addWidget(label("Choose where the app keeps its files. Each can be on any drive, and they don't "
                                "have to be on the drive the program runs from.", "muted", wrap=True))
        start_dir = standard_state_dir() if first_run or not keeps_app_data() else self.old_dir
        self.data_edit, self.data_free, self.data_browse = self._folder_row(
            lay, "App Data Folder",
            "Settings, scan results, Mars Vista request counts and the list of photos unavailable at NASA. A few "
            "megabytes.", start_dir)
        self.nokeep_box = QCheckBox("Don't Keep App Data")
        self.nokeep_box.setToolTip("Start fresh every time the app opens. Downloaded photos are always kept.")
        self.nokeep_box.setChecked(not first_run and not keeps_app_data())
        self.nokeep_box.toggled.connect(self._refresh)
        lay.addWidget(self.nokeep_box)
        self.nokeep_note = label(
            "Nothing is kept between launches: settings, scan results, Mars Vista request counts and the list of "
            "photos unavailable at NASA live in a temporary folder that's deleted when the app closes. Because "
            "request counts start at zero each time, the app can't keep you under Mars Vista's hourly and daily "
            "limits across restarts, and photos unavailable at NASA are tried again each run. Downloaded photos "
            "are always kept.",
            "muted", wrap=True)
        self.nokeep_note.setProperty("tone", "warn")
        lay.addWidget(self.nokeep_note)
        self.photo_edit = self.photo_free = None
        if first_run:
            self.photo_edit, self.photo_free, _ = self._folder_row(
                lay, "Photo Folder (Optional)",
                "Where photos are saved. All four rovers come to several hundred gigabytes. You can also choose "
                "or change this later in the main window.", "")
        self.has_old = has_old = any(os.path.isfile(os.path.join(self.old_dir, n)) for n in APP_DATA_FILES)
        self.copy_box = QCheckBox("Copy Existing App Data")
        self.copy_box.setChecked(True)
        self.copy_box.setToolTip("Copies settings, scan results, request counts, the list of photos unavailable "
                                 "at NASA, the photo details, and your favorites and flipbooks. The originals are "
                                 "left where they are.")
        self.copy_box.setVisible(has_old)
        lay.addWidget(self.copy_box)
        self.copy_from = label(f"From {self.old_dir}", "muted", wrap=True)     # the path, below its check box
        self.copy_from.setContentsMargins(26, 0, 0, 0)
        self.copy_from.setVisible(has_old)
        lay.addWidget(self.copy_from)
        info = [f"The program itself runs from {program_location()}.",
                f"Your Mars Vista API key is kept in {KEY_STORE}, not in a file."]
        if getattr(sys, "frozen", False) and os.name == "nt":       # the single .exe; an AppImage doesn't unpack
            info.append("Each time it starts, the program unpacks itself into Windows' temporary folder (about "
                        "90 MB); that copy is removed when it closes.")
        lay.addWidget(label(" ".join(info), "muted", wrap=True))
        bb = QDialogButtonBox()
        ok = bb.addButton("Continue" if first_run else "Save and Restart", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        bb.addButton("Quit" if first_run else "Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._refresh()

    def _folder_row(self, lay, title: str, hint: str, value: str):
        lay.addSpacing(6)
        lay.addWidget(label(title.upper(), "cardTitle"))
        row = QHBoxLayout()
        edit = QLineEdit(value)
        edit.textChanged.connect(self._refresh)
        row.addWidget(edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(edit))
        row.addWidget(browse)
        lay.addLayout(row)
        lay.addWidget(label(hint, "muted", wrap=True))
        free = label("", "muted")
        lay.addWidget(free)
        return edit, free, browse

    def _browse(self, edit: QLineEdit) -> None:
        start = edit.text().strip() or os.path.expanduser("~")
        folder = choose_folder(self, "Choose Folder", start)
        if folder:
            edit.setText(os.path.normpath(folder))

    def _refresh(self) -> None:
        if not hasattr(self, "copy_box"):            # still being built
            return
        nokeep = self.keep_nothing()
        for w in (self.data_edit, self.data_browse):
            w.setEnabled(not nokeep)
        self.data_free.setVisible(not nokeep)
        self.nokeep_note.setVisible(nokeep)
        self.data_free.setText(free_space_text(self.data_edit.text().strip()))
        if self.photo_edit is not None:
            self.photo_free.setText(free_space_text(self.photo_edit.text().strip()))
        same = os.path.normcase(self.data_dir()) == os.path.normcase(os.path.abspath(self.old_dir))
        self.copy_box.setVisible(self.has_old and not nokeep)
        self.copy_box.setEnabled(not same)
        self.copy_from.setVisible(self.has_old and not nokeep)
        self.copy_from.setEnabled(not same)

    def keep_nothing(self) -> bool:
        return self.nokeep_box.isChecked()

    def data_dir(self) -> str:
        if self.keep_nothing():
            return session_state_dir()
        return os.path.abspath(self.data_edit.text().strip() or ".")

    def photo_dir(self) -> str:
        return self.photo_edit.text().strip() if self.photo_edit is not None else ""

    def _accept(self) -> None:
        if self.keep_nothing():
            self.accept()
            return
        if not self.data_edit.text().strip():
            QMessageBox.warning(self, "No Folder", "Choose a folder for the app's data.")
            return
        try:
            os.makedirs(self.data_dir(), exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Couldn't Use the Folder", f"{self.data_dir()}\n\n{e}")
            return
        self.accept()

    def apply(self) -> list[str]:
        """Copy existing data if asked, remember the choice, and point the app at it. Returns files copied."""
        copied = []
        if self.keep_nothing():
            save_state_dir(NO_DATA_FOLDER)          # only this choice is remembered, so setup doesn't repeat
            use_state_dir(session_state_dir())
        else:
            if self.has_old and self.copy_box.isEnabled() and self.copy_box.isChecked():
                copied = copy_app_data(self.old_dir, self.data_dir())
            save_state_dir(self.data_dir())
            use_state_dir(self.data_dir())
        if self.photo_dir():
            s = Settings.load(SETTINGS_PATH)
            s.folder = os.path.normpath(self.photo_dir())
            s.save(SETTINGS_PATH)
        return copied


# ---- main window -----------------------------------------------------------------------------------------

def follows_opacity(w) -> bool:
    """Every window of the app: the main window, menus, dropdown lists, dialogs, message boxes and tooltips. Each is a
    window of its own, so the main window's opacity doesn't reach the others by itself."""
    return isinstance(w, QWidget) and w.isWindow()


# Qt's opening animations draw menus, dropdown lists and tooltips solid, ignoring the opacity (a menu opened by a
# click stayed solid), so they're off while the window is see-through.
OPENING_EFFECTS = (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo,
                   Qt.UIEffect.UI_AnimateTooltip, Qt.UIEffect.UI_FadeTooltip)


def choose_folder(parent, title: str, start: str) -> str:
    """A folder picker. The system's own picker is drawn by the operating system and can't be see-through, so while
    View > Window Opacity is below 100 % Qt's picker is used instead, which follows the opacity like every window."""
    f = QApplication.instance().findChild(PopupOpacity, "popupOpacity")
    see_through = f is not None and f.level < 0.995
    options = QFileDialog.Option.ShowDirsOnly | (QFileDialog.Option.DontUseNativeDialog if see_through
                                                 else QFileDialog.Option(0))
    return QFileDialog.getExistingDirectory(parent, title, start, options)


def choose_file(parent, title: str, start: str, filters: str) -> str:
    """A file picker; like choose_folder, Qt's own while the window is see-through."""
    f = QApplication.instance().findChild(PopupOpacity, "popupOpacity")
    see_through = f is not None and f.level < 0.995
    options = QFileDialog.Option.DontUseNativeDialog if see_through else QFileDialog.Option(0)
    return QFileDialog.getOpenFileName(parent, title, start, filters, "", options)[0]


class PopupOpacity(QObject):
    """Gives every window of the app the main window's opacity as it's created and as it opens."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = 1.0
        # The system's own choice of opening animations, to put back when the window is solid again
        self.effects_default = {e: QApplication.isEffectEnabled(e) for e in OPENING_EFFECTS}

    @classmethod
    def for_app(cls, app: QApplication) -> "PopupOpacity":
        """One filter per app, shared by every main window (a second filter would undo the first's level)."""
        existing = app.findChild(cls, "popupOpacity")
        if existing is not None:
            return existing
        f = cls(app)
        f.setObjectName("popupOpacity")
        app.installEventFilter(f)
        return f

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Polish, QEvent.Type.Show) and follows_opacity(obj):
            if abs(obj.windowOpacity() - self.level) > 0.005:
                obj.setWindowOpacity(self.level)
        return False


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
        self.choice_actions: dict[str, list] = {}  # View menu: setting name -> [(value, action)]
        self.pds_requests = RequestCounter()      # NASA archive requests this session (no quota)
        self.limiter: FileLimiter | None = None   # the user's download limit, kept across Pause/Resume
        remove_old_pause_file()
        self._loading = False
        # Camera selection to bring back when an unticked rover is ticked again (and what's saved for it).
        self.saved_excluded: dict[str, set[str]] = {n: set(c) for n, c in self.settings.excluded_cameras.items()}
        self.speed_samples: collections.deque = collections.deque(maxlen=1000)
        # (time, sol index, photos downloaded) for Time Left
        self.sol_samples: collections.deque = collections.deque(maxlen=5000)
        self.eta_at: float | None = None          # when Time Left last expected the rover to be done
        self.run_paused = False                   # waiting for the download limit or Mars Vista's limit
        self.pause_seen = False                   # a pause since the last sample: measure afresh after it
        self.active_scraper: Scraper | None = None     # the running download, for settings changed mid-run
        self.api_error = ""
        self.last_summary = ""
        self.details_totals = None               # Get Details: (sols done, SolResult totals) so far

        self.setWindowTitle("Rover Image Scraper")
        self.setMinimumSize(900, 560)
        # Open big enough to show everything without scrolling, but never larger than the screen.
        avail = app.primaryScreen().availableGeometry() if app.primaryScreen() else None
        self.resize(min(1200, avail.width() - 40) if avail else 1200,
                    min(1000, avail.height() - 60) if avail else 900)
        # Favorites and saved flipbooks, in the app data folder (the Photos page shows and changes them)
        self.favorites = FavoritesStore(os.path.join(STATE_DIR, FAVORITES_FILE))
        self.page_name = "Download"                     # the page shown (read by slow_context on another thread)
        self._build()
        self.popup_opacity = PopupOpacity.for_app(app)
        # Photo details: kept in the app data folder; sols without them are looked up when the Photos page shows
        # one of their photos.
        self.details = DetailsStore(os.path.join(STATE_DIR, DB_NAME))
        self.lookup_key = ""                     # the API key, for look-ups on a background thread
        self.lookups = SolLookups(self.details, self._lookup_sources, lambda: make_session(SIZE_CHECKS_AT_ONCE),
                                  self)
        self.lookups.finished.connect(self._lookup_finished)
        self.photos_page.extra_details = self._photo_extra_details
        self.photos_page.photo_time = self._photo_time
        self.photos_page.details_action = self._photo_details_action
        self.play_btn.clicked.connect(self.photos_page.play_or_pause)
        self.photos_page.play_state_changed.connect(self._update_play_button)
        self.looked_up_again: set = set()               # sols looked up again from the details panel
        self._build_menus()
        self._load_settings_into_widgets()
        self._refresh_library()
        self._validate()
        if not keeps_app_data():
            self._log("App data isn't kept between launches; settings and counts start fresh each time "
                      "(File > Settings > Change App Data Folder to keep them).")

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
        hl.addSpacing(24)
        # The two pages: downloading, and looking through the photos
        self.page_buttons: dict[str, QPushButton] = {}
        for name, tip in (("Download", "Download photos and check the folder (Ctrl+1)"),
                          ("Photos", "Look through the downloaded photos (Ctrl+2)")):
            b = QPushButton(name)
            b.setObjectName("pageTab")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, n=name: self._show_page(n))
            self.page_buttons[name] = b
            hl.addWidget(b)
        hl.addStretch()
        self.pill = label("● Mars Vista: not connected", "pill")
        self.pill.setProperty("tone", "idle")
        hl.addWidget(self.pill)
        outer.addWidget(header)

        self.pages = QStackedWidget()
        download_page = QWidget()
        body = QHBoxLayout(download_page)
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(16)
        self.pages.addWidget(download_page)
        self.download_page = download_page
        self.photos_page = PhotosPage(self.settings, self._folder, self._save_settings, favorites=self.favorites)
        self.pages.addWidget(self.photos_page)
        self.pages.setCurrentWidget(download_page)          # a stacked widget shows its first page otherwise
        self.page_buttons["Download"].setChecked(True)
        outer.addWidget(self.pages, 1)

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
        self.left_col.addStretch(1)        # spare height goes below the cards, not into them

        self.right_col = right
        self.right_cards = [self._build_library_card(), self._build_progress_card(), self._build_usage_card()]
        for c in self.right_cards:
            c.layout().addStretch(1)       # contents stay at the top when a card is given extra height
            right.addWidget(c)
        self.activity_card = self._build_activity_card()
        right.addWidget(self.activity_card, 1)
        # When the activity log is hidden, leftover height goes below the cards, so each card keeps its natural
        # height and they stay together at the top.
        right.addStretch(0)
        self.foldable: dict[str, tuple[QFrame, QWidget, QToolButton]] = {}
        left_cards = [self.left_col.itemAt(i).widget() for i in range(self.left_col.count())]
        for frame in [c for c in left_cards if c is not None] + self.right_cards:
            self._make_foldable(frame)

        bottom = QFrame()
        bottom.setObjectName("bottomBar")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(20, 10, 20, 10)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setToolTip("Estimate what a download would add, without downloading anything")
        self.preview_btn.clicked.connect(self._preview)
        bl.addWidget(self.preview_btn)
        # On the Photos page: what the download or scan is doing, since the Progress card isn't in view
        self.page_status = label("", "muted")
        self.page_status.setVisible(False)
        bl.addSpacing(12)
        bl.addWidget(self.page_status)
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
        # On the Photos page the main button plays the flipbook (Start and Preview belong to the Download page)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setObjectName("primary")
        self.play_btn.setMinimumWidth(130)
        self.play_btn.setVisible(False)
        bl.addWidget(self.play_btn)
        outer.addWidget(bottom)

    def _build_folder_card(self) -> None:
        frame, lay = card("Photo Folder")
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Choose where photos are saved")
        self.folder_edit.textChanged.connect(self._folder_changed)
        row.addWidget(self.folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        lay.addLayout(row)
        self.folder_hint = label("This folder doesn't exist yet. Start, Quick Scan, Full Check or Open Folder "
                                 "will offer to create it.", "muted", wrap=True)
        self.folder_hint.setProperty("tone", "warn")
        self.folder_hint.setVisible(False)
        lay.addWidget(self.folder_hint)
        row2 = QHBoxLayout()
        self.open_btn = link_button("Open Folder")
        self.open_btn.clicked.connect(self._open_folder)
        row2.addWidget(self.open_btn)
        row2.addStretch()
        lay.addLayout(row2)
        self.locked = [self.folder_edit, browse]
        self.left_col.addWidget(frame)

    def _build_key_card(self) -> None:
        frame, lay = card("API Key")
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
        self.key_hint = label("", "muted", wrap=True)
        self.key_hint.setOpenExternalLinks(True)
        self._color_links(DARK)
        lay.addWidget(self.key_hint)
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
        self.check_existing = QCheckBox("Check Existing Sols for Missing Photos")
        self.check_existing.setToolTip("On: start at sol 0 and fill gaps in sols you already have (slower).\n"
                                       "Off: only download sols after the last one in the folder.")
        lay.addWidget(self.check_existing)
        lay.addWidget(label("Slower. Turn off if your existing sols are complete and you only want new ones.",
                            "muted", wrap=True))
        row = QHBoxLayout()
        row.addWidget(label("Sols"))
        self.sol_from, self.sol_to = QLineEdit(), QLineEdit()
        for edit, hint in ((self.sol_from, "From"), (self.sol_to, "To")):
            edit.setPlaceholderText(hint)
            edit.setValidator(QIntValidator(0, 100000))
            edit.setFixedWidth(80)
        row.addWidget(self.sol_from)
        row.addWidget(label("–", "muted"))
        row.addWidget(self.sol_to)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(label("Leave blank for all sols.", "muted"))
        lim = QHBoxLayout()
        self.limit_box = QCheckBox("Limit Downloads")
        self.limit_box.setToolTip("Download at most this many photos in any rolling minute, hour or day. When the "
                                  "limit is reached the download waits, then continues by itself.")
        self.limit_box.toggled.connect(self._validate)
        # A plain number field like the sol boxes (a spin box without arrows collapses under the style sheet).
        self.limit_files = QLineEdit("500")
        self.limit_files.setValidator(QIntValidator(1, 1_000_000))
        self.limit_files.setFixedWidth(95)
        self.limit_files.editingFinished.connect(lambda: self.limit_files.setText(str(self._limit_value())))
        self.limit_period = QComboBox()
        for period in PERIODS:                    # shown in title case; the saved value stays "minute" / "hour" / "day"
            self.limit_period.addItem(f"Per {period.title()}", period)
        # Both stay editable; typing a number or choosing a period turns the limit on (the box turns it off).
        self.limit_files.textEdited.connect(lambda _text: self.limit_box.setChecked(True))
        self.limit_period.activated.connect(lambda _index: self.limit_box.setChecked(True))
        lim.addWidget(self.limit_box)
        lim.addWidget(self.limit_files)
        lim.addWidget(label("Photos"))
        lim.addWidget(self.limit_period)
        lim.addStretch()
        lay.addLayout(lim)
        self.adv_toggle = link_button("Advanced ▸")
        self.adv_toggle.clicked.connect(self._toggle_advanced)
        lay.addWidget(self.adv_toggle)
        self.adv = QWidget()
        al = QHBoxLayout(self.adv)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(label("Parallel Downloads"))
        self.workers = NumberChoice(1, 16)       # a dropdown: the style sheet hides a spin box's arrow buttons
        al.addWidget(self.workers)
        al.addStretch()
        self.adv.setVisible(False)
        lay.addWidget(self.adv)
        # Parallel downloads and the download limit stay editable during a download (see _live_change).
        self.locked += [self.check_existing, self.sol_from, self.sol_to]
        self.workers.currentIndexChanged.connect(lambda _i: self._live_change())
        self.limit_box.toggled.connect(lambda _on: self._live_change())
        self.limit_files.editingFinished.connect(self._live_change)
        self.limit_period.activated.connect(lambda _i: self._live_change())
        self.left_col.addWidget(frame)

    def _build_library_card(self) -> QFrame:
        frame, lay = card()
        head = QHBoxLayout()
        head.addWidget(label("LIBRARY STATUS", "cardTitle"))
        head.addStretch()
        self.scanned_label = label("Not scanned yet", "muted")
        head.addWidget(self.scanned_label)
        lay.addLayout(head)
        self.outdated_note = label("These numbers were counted before your last download, so they don't include "
                                   "the photos it added. Click Quick Scan to update them.", "muted", wrap=True)
        self.outdated_note.setProperty("tone", "warn")
        self.outdated_note.setVisible(False)
        lay.addWidget(self.outdated_note)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(5)
        # Two groups, so what's in the folder isn't confused with what Mars Vista has.
        sections = [("ON DISK", ["Sols", "Photos", "Size"]),
                    ("AVAILABLE ONLINE", ["Latest Sol", "To Download", "Gaps in Existing Sols"])]
        self.lib_rows = [row for _, rows in sections for row in rows]
        self.lib_cells: dict[tuple[str, str], QLabel] = {}
        self.lib_heads: dict[str, QLabel] = {}
        for c, rover in enumerate(ALL_ROVERS, 1):
            self.lib_heads[rover] = label(rover, "colHead")
            grid.addWidget(self.lib_heads[rover], 0, c)
        tip = ("Only sols that have photos are counted. Sol numbers count every Mars day since landing, but on some "
               "days the rover sent back no images (for example during solar conjunction, when the Sun blocks "
               "communication with Earth).")
        # Shown on the "To Download" row, to the right of the last rover column.
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
                if row_name == "To Download":
                    # spans this row and the next, so the wrapped note doesn't stretch one row
                    grid.addWidget(self.lib_note, r, note_col, 2, 1, Qt.AlignmentFlag.AlignTop)
                    for rover in ALL_ROVERS:
                        self.lib_cells[(row_name, rover)].setToolTip(tip)
                r += 1
        grid.setColumnStretch(note_col, 1)
        lay.addLayout(grid)
        self.lib_hint = label("Click Quick Scan to check this folder.", "muted")
        lay.addWidget(self.lib_hint)
        btns = QHBoxLayout()
        btns.addStretch()
        self.quick_btn = QPushButton("Quick Scan")
        self.quick_btn.setToolTip("Count what's on disk and compare with Mars Vista (2 API requests)")
        self.quick_btn.clicked.connect(self._quick_scan)
        self.full_btn = QPushButton("Full Check…")
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
        items = [("Photos Downloaded", 0, 0), ("Already Had", 0, 2),
                 ("Data Downloaded", 1, 0), ("Speed", 1, 2),
                 ("Photo Rate", 2, 0), ("Average Speed", 2, 2),
                 ("Unavailable at NASA", 3, 0), ("Errors", 3, 2),
                 ("Time Left", 4, 0), ("Elapsed", 4, 2)]
        tips = {"Data Downloaded": "Size of the photos saved to the folder in this run.",
                "Speed": "Download speed, averaged over a recent window (View > Units > Speed Averaged Over).",
                "Average Speed": "Average download speed since the run started, including waits.",
                "Photo Rate": "Photos saved per second over the same window as Speed.",
                "Already Had": "Photos that were already in the folder: in sols that were already complete, or "
                               "found while filling in a sol.",
                "Unavailable at NASA": "Photos NASA's image servers won't send (HTTP 403/404), usually older images "
                                       "NASA no longer serves. They can't be downloaded, so they're remembered and "
                                       "skipped on later runs instead of counting as missing. Download > Retry "
                                       "Photos Unavailable at NASA tries them all again.",
                "Time Left": "For the rover being downloaded. Estimated from how fast sols went by over the last "
                             "two minutes, and with a download limit on, from how many photos sols need at the "
                             "limit's pace. It changes when the download moves from sols you already have to new "
                             "ones. While waiting for a limit, it counts down."}
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
        self.problems_btn = link_button("Show Problems")
        self.problems_btn.setVisible(False)
        self.problems_btn.clicked.connect(self._show_problems)
        srow.addWidget(self.problems_btn)
        lay.addLayout(srow)
        # The photo saved most recently; a click shows it on the Photos page
        self.latest_row = QWidget()
        lr = QHBoxLayout(self.latest_row)
        lr.setContentsMargins(0, 4, 0, 0)
        lr.setSpacing(12)
        self.latest_thumb = QLabel()
        self.latest_thumb.setObjectName("latestThumb")
        self.latest_thumb.setFixedSize(LATEST_THUMB, LATEST_THUMB)
        self.latest_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.latest_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.latest_thumb.setToolTip("Show this photo on the Photos page")
        self.latest_thumb.mousePressEvent = lambda _e: self._show_latest_photo()
        lr.addWidget(self.latest_thumb)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(label("Latest Photo", "rowHead"))
        self.latest_text = label("", "value", wrap=True)
        texts.addWidget(self.latest_text)
        self.latest_link = link_button("Show on Photos Page")
        self.latest_link.clicked.connect(self._show_latest_photo)
        texts.addWidget(self.latest_link, 0, Qt.AlignmentFlag.AlignLeft)
        texts.addStretch(1)
        lr.addLayout(texts, 1)
        self.latest_row.setVisible(False)
        lay.addWidget(self.latest_row)
        self.latest_photo = None                  # the newest PhotoFile saved, and the one shown
        self.latest_shown = None
        self.latest_timer = QTimer(self, interval=700)       # at most a few thumbnail loads a second
        self.latest_timer.timeout.connect(self._update_latest_photo)
        self.latest_timer.start()
        self.photos_page.loader.ready.connect(self._latest_ready)
        return frame

    def _build_usage_card(self) -> QFrame:
        frame, lay = card("Mars Vista API Usage (Estimated)")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.hour_bar, self.day_bar = meter(), meter()
        self.hour_lbl, self.day_lbl = label("", "value"), label("", "value")
        for r, (name, bar, lbl) in enumerate((("This Hour", self.hour_bar, self.hour_lbl),
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
        nasa.addWidget(label("NASA Archive", "rowHead"))
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

    def _make_foldable(self, frame: QFrame) -> None:
        """Give a card a Hide/Show link in its title row that folds it down to that row. The card's contents
        move into one body widget, so showing it again keeps each part's own shown/hidden state."""
        lay = frame.layout()
        first = lay.itemAt(0)
        if first.widget() is not None:              # a plain title label: make it a row with room for the link
            title = lay.takeAt(0).widget()
            head = QHBoxLayout()
            head.addWidget(title)
            head.addStretch()
            lay.insertLayout(0, head)
        else:                                       # already a row (Library status)
            head = first.layout()
            title = next(head.itemAt(i).widget() for i in range(head.count())
                         if isinstance(head.itemAt(i).widget(), QLabel))
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(lay.spacing())
        while lay.count() > 1:
            stretch = lay.stretch(1)
            item = lay.takeAt(1)
            if item.widget() is not None:
                body_lay.addWidget(item.widget(), stretch)
            elif item.layout() is not None:
                child = item.layout()
                child.setParent(None)
                body_lay.addLayout(child, stretch)
            else:
                body_lay.addItem(item)
                body_lay.setStretch(body_lay.count() - 1, stretch)
        lay.addWidget(body, 1)
        toggle = link_button("Hide ▾")
        name = title.text()
        toggle.clicked.connect(lambda: self._set_card_open(name, not self.foldable[name][1].isVisibleTo(frame)))
        head.addWidget(toggle)
        self.foldable[name] = (frame, body, toggle)

    def _set_card_open(self, name: str, open_: bool, save: bool = True) -> None:
        frame, body, toggle = self.foldable[name]
        body.setVisible(open_)
        toggle.setText("Hide ▾" if open_ else "Show ▴")
        frame.setSizePolicy(QSizePolicy.Policy.Preferred,
                            QSizePolicy.Policy.Preferred if open_ else QSizePolicy.Policy.Maximum)
        folded = [n for n in self.settings.collapsed_cards if n != name] + ([] if open_ else [name])
        self.settings.collapsed_cards = folded
        self._update_right_stretch()
        if save:
            self._save_settings()

    def _apply_folded_cards(self) -> None:
        for name in self.foldable:
            self._set_card_open(name, name not in self.settings.collapsed_cards, save=False)

    def _update_right_stretch(self) -> None:
        """Spare height on the right goes to the activity log when it's shown, otherwise to the empty space below
        the cards (never into a card, which would only add blank space inside it)."""
        log_open = self.settings.show_log
        self.right_col.setStretchFactor(self.activity_card, 1 if log_open else 0)
        for c in self.right_cards:
            self.right_col.setStretchFactor(c, 0)
        self.right_col.setStretch(self.right_col.count() - 1, 0 if log_open else 1)

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
        self.limit_box.setChecked(s.limit_on)
        self.limit_files.setText(str(max(1, s.limit_files)))
        self.limit_period.setCurrentIndex(self.limit_period.findData(s.limit_period if s.limit_period in PERIODS
                                                                     else "hour"))
        self._loading = False
        self._update_camera_toggles()
        self._set_log_visible(s.show_log)
        self._apply_folded_cards()
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
        s.limit_on = self.limit_box.isChecked()
        s.limit_files, s.limit_period = self._limit_value(), self.limit_period.currentData()

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
        self.lookup_key = self._key()
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
        if hasattr(self, "play_btn"):
            self._update_bottom_bar()

    TASK_NAMES = {"download": "Download", "scan": "Scan", "full": "Full Check", "preview": "Preview",
                  "details": "Details Lookup"}

    def _update_bottom_bar(self) -> None:
        """The bottom bar follows the page: the Download page's Preview, Start, Pause and Stop, or on the Photos
        page the flipbook's Play, with Pause and Stop only while a task runs (named for it, so they can't be
        taken for the flipbook's)."""
        on_photos = self.pages.currentWidget() is self.photos_page
        idle = self.mode == "idle"
        for w in (self.preview_btn, self.start_btn):
            w.setVisible(not on_photos)
        self.start_hint.setVisible(not on_photos and bool(self.start_hint.text()))
        self.play_btn.setVisible(on_photos)
        self.pause_btn.setVisible(not on_photos or self.mode == "download")
        self.stop_btn.setVisible(not on_photos or not idle)
        task = self.TASK_NAMES.get(self.mode, "")
        self.pause_btn.setText("Pause Download" if on_photos else "Pause")
        self.stop_btn.setText(f"Stop {task}" if on_photos and task else "Stop")
        if on_photos:
            self._update_play_button()

    def _update_play_button(self) -> None:
        pp = self.photos_page
        playing = pp.player.playing
        self.play_btn.setText("❚❚  Pause" if playing else "▶  Play")
        self.play_btn.setEnabled(playing or pp.current is not None)
        self.play_btn.setToolTip("Pause the flipbook (P)" if playing else
                                 "Play as a flipbook (P): the photos you selected, the flipbook shown, or this "
                                 "photo's whole camera folder" if pp.current is not None else
                                 "Choose a photo, or select several, to play them as a flipbook")

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
        folder = choose_folder(self, "Choose Photo Folder",self._folder() or os.path.expanduser("~"))
        if folder:
            self.folder_edit.setText(os.path.normpath(folder))

    def _open_folder(self) -> None:
        if self._ensure_folder():          # a missing folder: offers to create it, as Start does
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._folder()))

    def _folder_changed(self) -> None:
        self._validate()
        self._refresh_library()
        if self.pages.currentWidget() is self.photos_page:
            self.photos_page.refresh()

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
            toggle.setText(f"Cameras {arrow}  ({on} of {total})" if total else f"Cameras {arrow}  (Loading…)")

    def _toggle_advanced(self) -> None:
        self.adv.setVisible(not self.adv.isVisible())
        self.adv_toggle.setText("Advanced ▾" if self.adv.isVisible() else "Advanced ▸")

    def _set_verbose(self, on: bool) -> None:
        self.settings.verbose_log = on
        self._save_settings()
        self._log("Detailed activity log " + ("on: every sol and photo is listed." if on else "off."))

    def _detail(self, message: str) -> None:
        """Extra log lines for the detailed activity log (from the download thread; checked live)."""
        if self.settings.verbose_log:
            self._post("log", message)

    def _toggle_log(self) -> None:
        self._set_log_visible(not self.settings.show_log)

    def _set_log_visible(self, visible: bool) -> None:
        # Shown: the activity log takes the spare height. Hidden: the card shrinks to its header (with a "Show"
        # link) at the bottom, and the cards above share the space. The log keeps recording either way.
        self.log_view.setVisible(visible)
        self.activity_card.setSizePolicy(QSizePolicy.Policy.Preferred,
                                         QSizePolicy.Policy.Preferred if visible else QSizePolicy.Policy.Maximum)
        self.log_toggle.setText("Hide ▾" if visible else "Show ▴")
        self.log_toggle.setToolTip("" if visible else "Show the activity log (also under View > Show Activity Log)")
        if hasattr(self, "act_log") and self.act_log.isChecked() != visible:
            self.act_log.setChecked(visible)
        self.settings.show_log = visible
        if hasattr(self, "foldable"):
            self._update_right_stretch()

    # ---- menus -------------------------------------------------------------------------------------------

    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_m = mb.addMenu("&File")
        download_m = mb.addMenu("&Download")
        view_m = mb.addMenu("&View")
        help_m = mb.addMenu("&Help")

        self.act_choose = file_m.addAction("Choose Photo Folder…")
        self.act_choose.setShortcut(QKeySequence("Ctrl+O"))
        self.act_choose.triggered.connect(self._browse)
        open_act = file_m.addAction("Open Photo Folder")
        open_act.triggered.connect(self._open_folder)
        file_m.addSeparator()
        import_act = file_m.addAction("Import Favorites…")
        import_act.setToolTip("Add favorites from a text list: one Rover/sol/CAMERA/number per line, then a note")
        import_act.triggered.connect(self._import_favorites)
        file_m.addSeparator()
        settings_m = file_m.addMenu("&Settings")          # File > Settings (filled in below)
        file_m.addSeparator()
        quit_act = file_m.addAction("Quit")
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(self.close)

        # Download: the same actions as the buttons, enabled whenever their button is
        download_m.setToolTipsVisible(True)
        self.button_actions = []
        for item in ((self.start_btn, "Start"), (self.pause_btn, "Pause"), (self.stop_btn, "Stop"), None,
                     (self.quick_btn, "Quick Scan"), (self.full_btn, "Full Check…"), (self.preview_btn, "Preview")):
            if item is None:
                download_m.addSeparator()
                continue
            button, text = item
            act = download_m.addAction(text)
            act.triggered.connect(button.click)
            self.button_actions.append((act, button))
        download_m.aboutToShow.connect(self._sync_button_actions)
        download_m.addSeparator()
        self.act_details = download_m.addAction("Get Details for Downloaded Photos…")
        self.act_details.setToolTip("Look up when and where each photo in the folder was taken, for the Photos "
                                    "page. Sols already looked up are skipped.")
        self.act_details.triggered.connect(self._get_details)
        download_m.addSeparator()
        self.act_retry = download_m.addAction("Retry Photos Unavailable at NASA (Next Run)")
        self.act_retry.setCheckable(True)
        self.act_retry.setToolTip("Photos NASA's servers won't send (HTTP 403/404) are normally skipped on later "
                                  "runs. Tick this to try them all again on the next run.")

        # The two pages, also in the header
        page_group = QActionGroup(view_m)
        self.page_actions: dict[str, object] = {}
        for name, keys in (("Download", "Ctrl+1"), ("Photos", "Ctrl+2")):
            act = view_m.addAction(f"{name} Page")
            act.setCheckable(True)
            act.setShortcut(QKeySequence(keys))
            act.setChecked(name == "Download")
            act.triggered.connect(lambda _=False, n=name: self._show_page(n))
            page_group.addAction(act)
            self.page_actions[name] = act
        view_m.addSeparator()

        # The groups first (the entries that open a list), each sorted alphabetically, then Match Operating System
        theme_options = [(g, sorted(((name, key) for group, name, key, _ in THEMES if group == g),
                                    key=lambda choice: choice[0].casefold())) for g in THEME_GROUPS]
        theme_options += [("Match Operating System", "system")]
        theme_m = self._choice_menu(view_m, "Theme", theme_options, "theme", after=self._apply_theme)
        # Hovering a theme previews it; clicking keeps it; closing the menu otherwise puts the chosen one back.
        self.theme_previewing: str | None = None
        self.theme_preview_timer = QTimer(self, singleShot=True, interval=90)   # skip themes passed over quickly
        self.theme_preview_timer.timeout.connect(self._show_theme_preview)
        theme_acts = [act for _, act in self.choice_actions["theme"]]
        for value, act in self.choice_actions["theme"]:
            act.hovered.connect(lambda v=value: self._preview_theme(v))
        # A theme shows only while the pointer is on its own entry: on a group's name ("Dark Themes") the saved
        # theme shows again, instead of the last theme the pointer passed over.
        theme_m.hovered.connect(lambda act: self._theme_menu_hovered(act, theme_acts))
        view_m.aboutToHide.connect(self._end_theme_preview)
        units_m = view_m.addMenu("Units")
        self._choice_menu(units_m, "Data", [("Auto", "auto"), ("KB", "KB"), ("MB", "MB"), ("GB", "GB"),
                                            ("TB", "TB")], "data_unit")
        self._choice_menu(units_m, "Speed", [("Auto", "auto"), ("KB/s", "KB/s"), ("MB/s", "MB/s"),
                                             ("Mbit/s", "Mbit/s")], "speed_unit")
        self._choice_menu(units_m, "Speed Averaged Over", [(f"{n} Seconds", n) for n in SPEED_WINDOWS],
                          "speed_window")
        view_m.addSeparator()
        self.act_log = view_m.addAction("Show Activity Log")
        self.act_log.setCheckable(True)
        self.act_log.setChecked(self.settings.show_log)
        self.act_log.toggled.connect(self._set_log_visible)
        self.act_verbose = view_m.addAction("Detailed Activity Log")
        self.act_verbose.setCheckable(True)
        self.act_verbose.setChecked(self.settings.verbose_log)
        self.act_verbose.setToolTip("Also log every sol checked and every photo saved or skipped.")
        self.act_verbose.toggled.connect(self._set_verbose)
        self._build_opacity_menu(view_m.addMenu("Window Opacity"))
        view_m.addSeparator()
        view_m.addAction("Reset View Options").triggered.connect(self._reset_view)

        self.act_move_data = settings_m.addAction("Change App Data Folder…")
        self.act_move_data.triggered.connect(self._change_data_folder)
        settings_m.addSeparator()
        settings_m.addAction("Reset Settings to Defaults…").triggered.connect(self._reset_settings)

        site = help_m.addAction("Mars Vista Website")
        site.triggered.connect(lambda: QDesktopServices.openUrl(QUrl("https://marsvista.dev")))
        help_m.addAction("Open App Data Folder").triggered.connect(self._open_settings_folder)
        help_m.addAction("Report a Problem").triggered.connect(lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/cincottash/RoverImageScraper/issues")))
        help_m.addSeparator()
        about = help_m.addAction("About Rover Image Scraper")
        about.triggered.connect(self._about)

    # ---- pages ---------------------------------------------------------------------------------------------

    def slow_context(self) -> str:
        """What was going on, for slow_moments.log. Called from its watcher thread, so it reads only plain Python
        values, never widgets."""
        pp = self.photos_page
        parts = [f"page: {self.page_name}", f"task: {self.mode}"]
        if self.page_name == "Photos":
            parts += [f"counting photos: {'yes' if pp.counter.running else 'no'}",
                      f"photo list being read: {'yes' if pp.lister.busy() else 'no'}",
                      f"thumbnails waiting: {pp.loader.waiting()}",
                      f"flipbook playing: {'yes' if pp.player.playing else 'no'}"]
        return " · ".join(parts)

    def _show_page(self, name: str) -> None:
        self.page_name = name
        page = self.photos_page if name == "Photos" else self.download_page
        self.pages.setCurrentWidget(page)
        for n, b in self.page_buttons.items():
            b.setChecked(n == name)
        for n, act in self.page_actions.items():
            act.setChecked(n == name)
        self._update_page_status()
        self._update_bottom_bar()
        if name == "Photos":
            self.lookups.failed.clear()          # look-ups that failed are tried again
            self.photos_page.setFocus()

    def _update_page_status(self) -> None:
        """On the Photos page the bottom bar says what a running task is doing."""
        on_photos = self.pages.currentWidget() is self.photos_page
        busy = self.mode != "idle"
        text = self.phase.text() if busy else ""
        if busy and self.mode == "download" and self.report is not None:
            text += f"   ·   {count(self.report.downloaded, 'photo')} downloaded"
        self.page_status.setVisible(on_photos and bool(text))
        if self.page_status.text() != text:
            self.page_status.setText(text)

    def _photo_saved(self, e: dict) -> None:
        dest = self.run_params.get("folder") or self._folder()
        if e.get("photo_id") is not None and e.get("number") is not None:
            # Which photo the file is, so its details need no matching; the sol is looked up again for them.
            self.details.record_file(dest, e["rover"], e["sol"], e["camera"], e["number"], e["photo_id"],
                                     e.get("url"), "download")
            self.details.forget_sol(dest, e["rover"], e["sol"])
        p = self.photos_page.photo_saved(e["path"])
        if p is not None:
            self.latest_photo = p

    # ---- photo details -------------------------------------------------------------------------------------

    def _lookup_sources(self, rover: str):
        """Sources for a look-up on the background thread; None when Mars Vista is needed but there's no key."""
        if not is_pds_rover(rover) and not self.lookup_key:
            return None
        return RoverSources(MarsVistaClient(self.lookup_key, tracker=self.tracker, cancel=self.lookups.cancel),
                            PdsClient(cancel=self.lookups.cancel, counter=self.pds_requests))

    def _photo_extra_details(self, p) -> list[tuple[str, str]]:
        """Details looked up online for a photo on the Photos page, or why there are none (yet)."""
        folder = self.photos_page.loaded_folder or self._folder()
        info, link = self.details.file_photo(folder, p.rover, p.sol, p.camera, p.number)
        if info is not None:
            return describe(info)
        summary = self.details.sol_state(folder, p.rover, p.sol)
        if summary is not None:
            rows = [] if link else describe_sol(summary)
            if rows and (folder_key(folder), p.rover, p.sol) in self.looked_up_again:
                rows.append(("Looked Up Again", "This file still matches no single NASA photo. NASA may no longer "
                                                "list this copy (for example an old thumbnail)."))
            return rows or [("More Details", "None found for this photo.")]
        key = (folder_key(folder), p.rover, p.sol)
        if self.lookups.pending(folder, p.rover, p.sol):
            return [("More Details", "Looking up…")]
        error = self.lookups.failed.get(key)
        if error == "no key" and not self.lookup_key:
            return [("More Details", "Add an API key on the Download page to look up when and where this photo "
                                     "was taken.")]
        if error and error != "no key":
            return [("More Details", f"Couldn't be looked up: {error}. Tried again when the Photos page is "
                                     "opened again.")]
        self.lookups.request(folder, p.rover, p.sol)
        return [("More Details", "Looking up…")]

    def _photo_details_action(self, p):
        """The details panel's button for a photo without details of its own: (text, tooltip, what it does)."""
        folder = self.photos_page.loaded_folder or self._folder()
        if not folder:
            return None
        info, link = self.details.file_photo(folder, p.rover, p.sol, p.camera, p.number)
        if info is not None or self.lookups.pending(folder, p.rover, p.sol):
            return None
        key = (folder_key(folder), p.rover, p.sol)
        error = self.lookups.failed.get(key)
        if error == "no key" and not self.lookup_key:
            return ("Add API Key…", "Looking up Curiosity and Perseverance needs a Mars Vista API key, pasted on the "
                                    "Download page", self._ask_for_key)
        summary = self.details.sol_state(folder, p.rover, p.sol)
        if error:
            return ("Try Again", f"Look up sol {p.sol:,}'s details again (the last try failed: {error})",
                    lambda: self._look_up_again(folder, p.rover, p.sol))
        if summary is not None and not link:
            return ("Look Up Again", f"Look up sol {p.sol:,} again and match its files to NASA's photos once more "
                                     f"(catches photos added since, and look-ups that went wrong). Uses a few Mars "
                                     f"Vista requests for Curiosity and Perseverance",
                    lambda: self._look_up_again(folder, p.rover, p.sol))
        return None

    def _look_up_again(self, folder: str, rover: str, sol: int) -> None:
        self.details.forget_sol(folder, rover, sol)
        self.looked_up_again.add((folder_key(folder), rover, sol))
        self.lookups.request(folder, rover, sol)

    def _ask_for_key(self) -> None:
        self._show_page("Download")
        self.key_edit.setFocus()

    def _photo_time(self, p) -> str | None:
        """When a photo was taken (ISO, UTC), if its details were looked up: flipbooks play in that order."""
        folder = self.photos_page.loaded_folder or self._folder()
        info, _link = self.details.file_photo(folder, p.rover, p.sol, p.camera, p.number)
        return info.taken_utc[:19] if info is not None and info.taken_utc else None

    def _import_favorites(self) -> None:
        """File > Import Favorites…: a text list (like a notes file of favorite photos) into Favorites."""
        path = choose_file(self, "Import Favorites", os.path.expanduser("~"), "All files (*)")
        if path:
            self._import_favorites_from(path)

    def _import_favorites_from(self, path: str, ask: bool = True) -> tuple[int, int, list[str]]:
        """Returns (added, already favorites, lines not understood). The list file is only read."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                favs, problems = parse_favorites_list(f.read())
        except OSError as e:
            QMessageBox.warning(self, "Import Favorites", f"Couldn't read {path}:\n{e}")
            return 0, 0, []
        new = [f for f in favs if not self.favorites.is_favorite(f.key)]
        shown = "\n".join(f"  {p}" for p in problems[:12]) + ("\n  …" if len(problems) > 12 else "")
        text = (f"{count(len(new), 'favorite')} to add"
                + (f" ({len(favs) - len(new):,} already in Favorites)" if len(favs) > len(new) else "") + ".")
        if problems:
            text += f"\n\n{count(len(problems), 'line')} not understood, so left out:\n{shown}"
        if ask:
            if not new:
                QMessageBox.information(self, "Import Favorites", text)
                return 0, len(favs), problems
            if QMessageBox.question(self, "Import Favorites", text + "\n\nAdd them?") != \
                    QMessageBox.StandardButton.Yes:
                return 0, len(favs) - len(new), problems
        for f in new:
            self.favorites.add(f.rover, f.sol, f.camera, f.number, note=f.note)
        self.photos_page._refresh_special()
        self._log(f"Imported {count(len(new), 'favorite')} from {os.path.basename(path)}")
        return len(new), len(favs) - len(new), problems

    def _lookup_finished(self, rover: str, sol: int, error: str) -> None:
        if self.lookups.cancel.is_set():           # the window is closing; the details file may be closed
            return
        cur = self.photos_page.current
        if cur is not None and cur.rover == rover and cur.sol == sol:
            self.photos_page.refresh_details()

    def _get_details(self) -> None:
        """Download > Get Details for Downloaded Photos…: look up every sol in the folder not looked up yet."""
        if not self._ensure_folder():
            return
        folder = self._folder()
        rovers = rovers_in(folder)
        todo = {r: [s for s in sols_in(folder, r) if s not in self.details.sols_done(folder, r)] for r in rovers}
        total = sum(len(v) for v in todo.values())
        if not total:
            QMessageBox.information(self, "Get Details", "Every sol in the photo folder has been looked up "
                                                         "already.")
            return
        needs_key = [r for r in rovers if todo[r] and not is_pds_rover(r)]
        if needs_key and not self._key():
            QMessageBox.information(self, "Get Details", f"Looking up {', '.join(needs_key)} needs a Mars Vista "
                                                         "API key. Paste it under API Key first.")
            return
        per_rover = "\n".join(f"  {r}: {count(len(todo[r]), 'sol')}" for r in rovers if todo[r])
        answer = QMessageBox.question(
            self, "Get Details for Downloaded Photos",
            f"Look up when and where the photos in the folder were taken:\n\n{per_rover}\n\n"
            "Curiosity and Perseverance use about one Mars Vista request per 100 photos, plus a size check of "
            "each photo on NASA's servers (nothing is downloaded). Spirit and Opportunity use a few requests to "
            "NASA's archive per sol.\n\nA whole library can take several hours. Stop keeps what's done, and the "
            "next run continues where it stopped. The photo folder isn't changed.\n\nStart?")
        if answer == QMessageBox.StandardButton.Yes:
            self._run_details(folder, todo)

    def _run_details(self, folder: str, todo: dict[str, list[int]]) -> None:
        self._save_settings()
        self._save_key()
        key = self._key()
        self.report = None
        self.details_totals = None
        self.summary.setVisible(False)
        self.problems_btn.setVisible(False)
        self.run_started = time.time()
        self._log(f"=== Getting photo details started {time.strftime('%H:%M')}")

        def work():
            sources = self._client(key)
            nasa = make_session(SIZE_CHECKS_AT_ONCE)
            totals = SolResult()
            sols_done = 0
            for rover, sols in todo.items():
                if not sols:
                    continue
                self._post("progress", {"event": "rover", "rover": rover, "total": len(sols), "sol_min": sols[0]})
                for i, sol in enumerate(sols, 1):
                    if self.cancel.is_set():
                        break
                    self._post("progress", {"event": "sol", "rover": rover, "sol": sol, "index": i,
                                            "total": len(sols)})
                    r = look_up_sol(folder, rover, sol, sources, self.details, nasa, self.cancel)
                    if self.cancel.is_set():
                        break
                    sols_done += 1
                    for f in ("files", "known", "matched", "unmatched"):
                        setattr(totals, f, getattr(totals, f) + getattr(r, f))
                    self._post("details_totals", (sols_done, totals))
                    self._detail(f"     {rover} sol {sol}: {r.matched:,} matched now, {r.known:,} known before, "
                                 f"{r.unmatched:,} without a match")
            self.tracker.save()
            return sols_done, totals

        self._run_task("details", work)

    def _show_details_summary(self, value, cancelled: bool) -> None:
        sols_done, t = value if value else (self.details_totals or (0, SolResult()))
        state = "Stopped" if cancelled else "Finished"
        matched = t.known + t.matched
        text = (f"<b>{state}.</b> {count(sols_done, 'sol')} looked up: {count(matched, 'photo')} of "
                f"{t.files:,} matched to NASA's records ({t.matched:,} new).")
        if t.unmatched:
            text += (f" {count(t.unmatched, 'photo')} couldn't be matched (older copies and thumbnails NASA no "
                     "longer lists); they show details for their whole sol where there are any.")
        if cancelled:
            text += " The next run continues where this one stopped."
        self.summary.setText(text)
        self.summary.setVisible(True)
        self._log(f"=== Getting photo details {state.lower()} {time.strftime('%H:%M')}: {count(sols_done, 'sol')}, "
                  f"{count(matched, 'photo')} matched, {t.unmatched:,} without a match")
        self.photos_page.refresh_details()

    def _update_latest_photo(self) -> None:
        p = self.latest_photo
        if p is None or p == self.latest_shown:
            return
        self.latest_shown = p
        self.latest_text.setText(f"{p.rover} · Sol {p.sol} · {camera_name(p.camera)} · {p.name}")
        self.latest_row.setVisible(True)
        self.photos_page.loader.request(("latest", p.path), p.path, LATEST_THUMB * 2)

    def _latest_ready(self, key, img) -> None:
        if not (isinstance(key, tuple) and key[0] == "latest"):
            return
        if self.latest_shown is None or self.latest_shown.path != key[1] or img.isNull():
            return
        ratio = self.latest_thumb.devicePixelRatioF()
        pix = QPixmap.fromImage(img).scaled(int(LATEST_THUMB * ratio), int(LATEST_THUMB * ratio),
                                            Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation)
        pix.setDevicePixelRatio(ratio)
        self.latest_thumb.setPixmap(pix)

    def _show_latest_photo(self) -> None:
        if self.latest_shown is None:
            return
        self._show_page("Photos")
        self.photos_page.refresh()
        self.photos_page.show_photo(self.latest_shown)

    def _sync_button_actions(self) -> None:
        """Download menu items follow their buttons (enabled state, and Start/Resume wording)."""
        self.act_details.setEnabled(self.mode == "idle" and bool(self._folder()))
        bar = (self.preview_btn, self.start_btn, self.pause_btn, self.stop_btn)   # hidden on the Photos page only
        for act, button in self.button_actions:
            act.setEnabled(button.isEnabled() and (button.isVisible() or button in bar))
            act.setText(button.text().replace("▶", "").strip())

    def _choice_menu(self, parent, title: str, options: list, attr: str, after=None) -> QMenu:
        """A submenu of mutually exclusive choices stored in self.settings.<attr>. Returns the submenu."""
        menu = parent.addMenu(title)
        group = QActionGroup(menu)
        group.setExclusive(True)
        flat = []
        for text, value in options:          # a list as the value is a submenu of more choices
            if isinstance(value, list):
                sub = menu.addMenu(text)
                flat += [(sub, t, v) for t, v in value]
            else:
                flat.append((menu, text, value))
                menu.addAction(text)          # placeholder, so plain choices keep their place among the submenus
        placeholders = [a for a in menu.actions() if a.menu() is None]
        for where, text, value in flat:
            if where is menu:
                act = placeholders.pop(0)
            else:
                act = where.addAction(text)
            act.setCheckable(True)
            act.setChecked(getattr(self.settings, attr) == value)
            group.addAction(act)
            act.triggered.connect(lambda _=False, v=value: self._set_view_option(attr, v, after))
            self.choice_actions.setdefault(attr, []).append((value, act))
        return menu

    def _build_opacity_menu(self, menu: QMenu) -> None:
        """View > Window Opacity: one slider. The window changes as it moves."""
        row = QWidget()
        row.setObjectName("menuRow")
        h = QHBoxLayout(row)
        h.setContentsMargins(26, 5, 14, 5)
        h.setSpacing(10)
        h.addWidget(QLabel("Opacity"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(OPACITY_MIN, 100)
        self.opacity_slider.setSingleStep(5)
        self.opacity_slider.setPageStep(10)
        self.opacity_slider.setMinimumWidth(170)
        self.opacity_slider.setValue(self.settings.window_opacity)
        h.addWidget(self.opacity_slider, 1)
        self.opacity_value = QLabel(f"{self.settings.window_opacity} %")
        self.opacity_value.setMinimumWidth(self.opacity_value.fontMetrics().horizontalAdvance("100 %") + 4)
        self.opacity_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.opacity_value)
        slider_act = QWidgetAction(menu)
        slider_act.setDefaultWidget(row)
        menu.addAction(slider_act)
        self.opacity_slider.valueChanged.connect(self._opacity_moved)
        self._apply_opacity()

    def _opacity_moved(self, value: int) -> None:
        self.settings.window_opacity = value
        self.opacity_value.setText(f"{value} %")
        self._apply_opacity()
        self._save_settings()

    def _apply_opacity(self) -> None:
        """The opacity from View > Window Opacity, on every window of the app: open or not, so each shows it from the
        moment it appears (windows created later get it from PopupOpacity)."""
        level = self.settings.window_opacity / 100
        self.popup_opacity.level = level
        for w in [self] + [p for p in QApplication.topLevelWidgets() if follows_opacity(p)] + self.findChildren(QMenu):
            if abs(w.windowOpacity() - level) > 0.005:
                w.setWindowOpacity(level)
        solid = level >= 0.995          # opening animations draw solid, so they're off while see-through
        for effect in OPENING_EFFECTS:
            if QApplication.isEffectEnabled(effect) != (solid and self.popup_opacity.effects_default[effect]):
                QApplication.setEffectEnabled(effect, solid and self.popup_opacity.effects_default[effect])

    def _theme_menu_hovered(self, act, theme_acts: list) -> None:
        """The pointer is on something in View > Theme that isn't a theme (a group's name): show the saved theme
        again if another one is being previewed. Nothing is restyled when no preview is active."""
        if act in theme_acts or self.theme_previewing is None or self.theme_previewing == self.settings.theme:
            return
        self._preview_theme(self.settings.theme)

    # View menu choices, and the download options in the left column (File > Settings > Reset Settings to Defaults).
    VIEW_FIELDS = ("data_unit", "speed_unit", "speed_window", "theme", "show_log", "collapsed_cards",
                   "window_opacity", "photo_thumb_size", "photo_sol_sort")
    OPTION_FIELDS = ("rovers", "excluded_cameras", "check_existing", "sol_from", "sol_to", "workers", "limit_on",
                     "limit_files", "limit_period")

    def _reset_view(self) -> None:
        answer = QMessageBox.question(
            self, "Reset View Options?",
            "Put data and speed units, speed averaging, theme, the activity log, folded cards, window opacity, "
            "the thumbnail size and the order of the sols list back to their defaults?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        defaults = Settings()
        for attr in self.VIEW_FIELDS:
            setattr(self.settings, attr, getattr(defaults, attr))
        self._sync_choice_checks()
        self._set_log_visible(self.settings.show_log)
        self._apply_folded_cards()
        self.opacity_slider.setValue(self.settings.window_opacity)     # also updates the value beside it
        self.photos_page.size_slider.setValue(self.settings.photo_thumb_size)
        self.photos_page.apply_sol_sort()
        self._apply_opacity()
        self._apply_theme()
        self._update_counters()
        self._save_settings()
        self._log("View options reset to defaults.")

    def _reset_settings(self) -> None:
        if self.mode != "idle":
            QMessageBox.information(self, "Busy", "Stop the current task before resetting settings.")
            return
        answer = QMessageBox.question(
            self, "Reset Settings to Defaults?",
            "Reset rovers, cameras, \"Check Existing Sols for Missing Photos\", the sol range, parallel downloads, "
            "the download limit and \"Retry Photos Unavailable at NASA\" to their defaults?\n\n"
            "Kept as they are: the photo folder, the API key, the app data folder, scan results and request "
            "counts. View options have their own reset under View.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        defaults = Settings()
        for attr in self.OPTION_FIELDS:
            setattr(self.settings, attr, getattr(defaults, attr))
        self.saved_excluded = {}
        self.act_retry.setChecked(False)
        self._load_settings_into_widgets()
        self._save_settings()
        self._validate()
        self._log("Settings reset to defaults.")

    def _sync_choice_checks(self) -> None:
        """Tick the View menu entries that match the current settings."""
        for attr, actions in self.choice_actions.items():
            for value, act in actions:
                act.setChecked(getattr(self.settings, attr) == value)

    def _set_view_option(self, attr: str, value, after=None) -> None:
        setattr(self.settings, attr, value)
        self._sync_choice_checks()
        self._save_settings()
        self._update_counters()
        if after:
            after()

    def _preview_theme(self, key: str) -> None:
        self.theme_previewing = key
        self.theme_preview_timer.start()

    def _show_theme_preview(self) -> None:
        """Restyle only the window's contents, not the open menus: restyling a menu while the mouse is on it
        makes it redraw and shift."""
        key = self.theme_previewing
        if key is not None:
            colors = THEME_COLORS.get(key) or (DARK if system_is_dark(self.app) else LIGHT)
            apply_theme(self.app, colors, only=self.centralWidget())
            self._color_links(colors)

    def _color_links(self, colors: dict) -> None:
        """Links in labels take the theme's accent color. Qt draws them in its default dark blue otherwise, which
        is hard to read on dark themes."""
        self.key_hint.setText(f'Get a free key at <a href="https://marsvista.dev"><span style="color:{colors["accent"]}">'
                              f'marsvista.dev</span></a>. Saved in {KEY_STORE}.')

    def _end_theme_preview(self) -> None:
        """The View menu closed. After a click the new theme is already saved; otherwise this restores the saved
        one. Run once the click (if any) has been handled, since Qt closes the menu before reporting it."""
        self.theme_preview_timer.stop()
        if self.theme_previewing is not None:
            self.theme_previewing = None
            QTimer.singleShot(0, self._apply_theme)

    def _apply_theme(self, choice: str | None = None) -> None:
        central = self.centralWidget() if hasattr(self, "theme_previewing") else None
        if central is not None and central.styleSheet():       # a preview was showing: back to the app's theme
            central.setStyleSheet("")
            central.setPalette(QPalette())
        choice = choice or self.settings.theme
        if choice in THEME_COLORS:
            colors = THEME_COLORS[choice]
        else:            # "system", or a theme this version doesn't have
            colors = DARK if system_is_dark(self.app) else LIGHT
        apply_theme(self.app, colors)
        self._color_links(colors)

    def _open_settings_folder(self) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(STATE_DIR))

    def _change_data_folder(self) -> None:
        if self.mode != "idle":
            QMessageBox.information(self, "Busy", "Stop the current task before moving the app's data.")
            return
        self._save_settings()
        self.tracker.save()
        dlg = SetupDialog(self, current=STATE_DIR, first_run=False)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if os.path.normcase(dlg.data_dir()) == os.path.normcase(os.path.abspath(STATE_DIR)):
            return
        self.lookups.close()
        self.details.flush()               # the photo details are copied as one file
        dlg.apply()
        self.close()                        # saves settings into the new folder, then a fresh copy starts there
        args = sys.argv[1:] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)] + sys.argv[1:]
        QProcess.startDetached(sys.executable, args)

    def _about(self) -> None:
        QMessageBox.about(self, "About Rover Image Scraper",
                          "<b>Rover Image Scraper</b><br><br>Downloads raw photos from NASA's Mars rovers, "
                          "adding only what's missing. Curiosity and Perseverance come from the Mars Vista API; "
                          "Spirit and Opportunity from NASA's Planetary Data System archive.<br><br>"
                          'Photos: NASA/JPL-Caltech. Data: <a href="https://marsvista.dev">Mars Vista</a>.<br><br>'
                          'Copyright © 2026 cincottash. Free software under the <a href="https://www.gnu.org/licenses/gpl-3.0.html">GNU General '
                          'Public License v3</a>. Source: '
                          '<a href="https://github.com/cincottash/RoverImageScraper">github.com/cincottash/'
                          'RoverImageScraper</a>.')

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

    def _limit_value(self) -> int:
        """The number typed in the download limit box (500 if it's empty or not a number)."""
        text = self.limit_files.text().strip()
        return max(1, int(text)) if text.isdigit() else 500

    def _make_limiter(self) -> FileLimiter | None:
        """The download limit chosen in Options, or None. The same limiter is reused while its numbers stay the
        same, so pausing and resuming (or starting again) doesn't reset what has already been counted."""
        if not self.limit_box.isChecked():
            self.limiter = None
            return None
        n, period = self._limit_value(), self.limit_period.currentData()
        if self.limiter is None:
            self.limiter = FileLimiter(n, PERIODS[period], cancelled_error=Cancelled)
        else:            # change the numbers in place, so photos already counted still count
            self.limiter.max_files, self.limiter.period = n, PERIODS[period]
        self.limiter.label = f"{n:,} photos per {period}"
        return self.limiter

    def _live_change(self) -> None:
        """Parallel downloads or the download limit changed. During a download the new values apply from the next
        camera folder (and the limit at once); otherwise they're simply used by the next run."""
        if self._loading:          # settings being put into the widgets: saving now would store half-loaded values
            return
        self._save_settings()
        scraper = self.active_scraper
        if self.mode != "download" or scraper is None:
            return
        workers = self.workers.value()
        if scraper.workers != workers:
            scraper.workers = workers
            self._log(f"Parallel downloads changed to {workers} (from the next camera folder).")
        old = scraper.limiter
        before = (old.max_files, old.period) if old is not None else None
        limiter = self._make_limiter()            # may update the same object in place
        if limiter is not None:
            limiter.cancel = self.cancel
        scraper.limiter = limiter
        after = (limiter.max_files, limiter.period) if limiter is not None else None
        if after != before:
            self._log(f"Download limit now {limiter.label}." if limiter else "Download limit turned off.")

    def _ensure_folder(self, folder: str | None = None) -> bool:
        """True if the photo folder exists, or the user agreed to create it and it was created."""
        folder = folder or self._folder()
        if not folder:
            QMessageBox.warning(self, "No Photo Folder", "Choose a folder to save photos in first.")
            return False
        if os.path.isdir(folder):
            return True
        if os.path.exists(folder):
            QMessageBox.warning(self, "Not a Folder", f"{folder}\n\nThis path is a file, not a folder.")
            return False
        answer = QMessageBox.question(self, "Create Folder?",
                                      f"{folder}\n\nThis folder doesn't exist. Create it?")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            os.makedirs(folder)
        except OSError as e:
            QMessageBox.warning(self, "Couldn't Create the Folder", f"{folder}\n\n{e}")
            return False
        self._log(f"Created folder {folder}")
        self._validate()
        self._refresh_library()
        return True

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
            self, "Folder Already Has Photos",
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
                box.setWindowTitle("Paused Download")
                box.setText(f"You have a paused download ({cp['rover']} sol {cp['sol']:,}).\n\nYour choices have "
                            f"changed since it was paused. Resume it with its original choices, or start a new "
                            f"download with the current ones? (Starting new discards the paused position; "
                            f"downloaded photos are kept.)")
                resume_btn = box.addButton("Resume Paused", QMessageBox.ButtonRole.AcceptRole)
                new_btn = box.addButton("Start New", QMessageBox.ButtonRole.DestructiveRole)
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
        limiter = self._make_limiter()
        key, bounds = self._key(), self._sol_bounds()
        self.run_params = self._run_params()
        retry_refused = self.act_retry.isChecked()
        if retry_refused:
            self.act_retry.setChecked(False)      # one run only
            self._log("Retrying photos that were unavailable at NASA before "
                      "(Download > Retry Photos Unavailable at NASA).")
        self.report = Report()
        report = self.report
        self.summary.setVisible(False)
        self.problems_btn.setVisible(False)
        self.speed_samples.clear()
        self.sol_samples.clear()
        self.eta_at, self.pause_seen = None, False
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
            if limiter is not None:
                limiter.cancel = self.cancel          # this run's Stop/Pause also ends a limit wait
            scraper = Scraper(client, dest, log=lambda m: self._post("log", m), cancel=self.cancel,
                              unavailable=unavailable, workers=workers, cameras=cameras,
                              progress=lambda e: self._post("progress", e), limiter=limiter,
                              detail=self._detail)
            self.active_scraper = scraper
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
        limiter = self._make_limiter()
        self.run_params = params
        self.report = report_from_dict(cp["report"])
        report = self.report
        self.summary.setVisible(False)
        self.problems_btn.setVisible(False)
        self.speed_samples.clear()
        self.sol_samples.clear()
        self.eta_at, self.pause_seen = None, False
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
            if limiter is not None:
                limiter.cancel = self.cancel          # this run's Stop/Pause also ends a limit wait
            scraper = Scraper(client, dest, log=lambda m: self._post("log", m), cancel=self.cancel,
                              unavailable=unavailable, workers=workers, cameras=cameras,
                              progress=lambda e: self._post("progress", e), limiter=limiter,
                              detail=self._detail)
            self.active_scraper = scraper
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
            self, "Full Check",
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
                continue
            if lines:              # lines logged so far go first, so the log stays in order
                self._log("\n".join(lines))
                lines = []
            if kind == "progress":
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
            elif kind == "details_totals":
                self.details_totals = payload
        if lines:
            self._log("\n".join(lines))
        self._update_live()
        self._update_page_status()

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
            if self.sol_samples and self.sol_samples[-1][1] > e["index"]:      # a new rover started
                self.sol_samples.clear()
                self.eta_at = None
            if self.pause_seen and not self.run_paused:     # moving again: measure from here, not across the wait
                self.sol_samples.clear()
                self.pause_seen = False
            self.sol_samples.append((time.time(), e["index"], self.report.downloaded if self.report else 0))
            if self.mode == "download":
                self.pos["sol"] = e["sol"]
        elif kind == "camera":
            ps["camera"] = e["camera"]
        elif kind == "saved":
            self._photo_saved(e)
        elif kind == "scan_start":
            ps.clear()
            ps.update(scan_rover=e["rover"], full=e["full"])
        elif kind == "scan":
            ps.update(scan_rover=e["rover"], scan_index=e["index"], scan_total=e["total"])

    def _on_done(self, mode: str, value) -> None:
        self.active_scraper = None
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
        elif mode == "details":
            self._show_details_summary(value, cancelled)
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
        elif mode in ("download", "scan", "full", "details"):
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
        QMessageBox.warning(self, "Something Went Wrong", message)

    def _show_summary(self, r: Report, cancelled: bool, paused: bool = False) -> None:
        state = "Paused" if paused else "Stopped" if cancelled else "Finished"
        text = (f"<b>{state}.</b> Downloaded {r.downloaded:,} photos; {r.already_present:,} were already on disk; "
                f"{r.newly_unavailable + r.known_unavailable:,} unavailable at NASA. {count(r.sols_checked, 'sol')} "
                f"checked.")
        if r.left_as_is:
            text += (f" {r.left_as_is:,} Spirit/Opportunity folder(s) from another source were left as is, "
                     f"so nothing was duplicated.")
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
        folder = self._folder()
        if folder:
            where = f"Saving to <b>{html.escape(folder)}</b>"
            free = free_space_text(folder)
            if free:
                where += f" ({free})"
            if not os.path.isdir(folder):
                where += ". This folder doesn't exist yet; Start will offer to create it."
        else:
            where = "No photo folder chosen yet. Choose one before starting."
        lay.addWidget(label(where, "muted", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        for c, head in enumerate(("", "New Sols", "New Photos", "Gap Fills", "Size")):
            grid.addWidget(label(head, "rowHead"), 0, c)
        total_photos = total_requests = 0
        unknown_gaps = any_pds = False
        for r, row in enumerate(rows, 1):
            gaps = row.get("gap_photos")
            if self._preview_check_existing and gaps is None:
                unknown_gaps = True
            total_requests += row["requests"]
            gap_text = "not included" if not self._preview_check_existing else (
                "unknown" if gaps is None else "none" if gaps == 0 else f"up to {count(gaps, 'photo')}")
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
            notes.append("Gaps in existing sols aren't known yet. Run Full Check to include them in the estimate.")
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
        start = bb.addButton("Start Now", QDialogButtonBox.ButtonRole.AcceptRole)
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

        lim = self.limiter
        limit_wait = (self.mode == "download" and not self.cancel.is_set() and lim is not None
                      and lim.waiting_until is not None and lim.waiting_until > time.time())
        waiting = usage.waiting and self.mode != "idle"
        nasa_busy = NASA_STATUS.busy_recently() and self.mode == "download"
        if limit_wait:
            left = lim.waiting_until - time.time()
            resume = time.strftime("%H:%M", time.localtime(lim.waiting_until))
            self.banner_title.setText(f"⏸  Your download limit is reached ({lim.label}). "
                                      f"Resumes in {fmt_duration(left)}, about {resume}.")
            self.banner_text.setText("Set under Options. This isn't an error: the download continues "
                                     "automatically.")
        elif waiting:
            left = usage.waiting_until - time.time()
            resume = time.strftime("%H:%M", time.localtime(usage.waiting_until))
            self.banner_title.setText(f"⏸  Waiting for Mars Vista rate limit ({usage.wait_reason}). "
                                      f"Resumes in {fmt_duration(left)}, about {resume}.")
            self.banner_text.setText("Mars Vista allows 10,000 requests per hour and 100,000 per day. "
                                     "This isn't an error: the run continues automatically.")
        elif nasa_busy:
            self.banner_title.setText("⏸  NASA's image servers are busy. Retrying automatically.")
            self.banner_text.setText("Some downloads are being retried. No action needed.")
        self.banner.setVisible(limit_wait or waiting or nasa_busy)
        self.run_paused = bool(limit_wait or waiting)
        if self.run_paused:
            self.pause_seen = True
        if self.bar.property("paused") != (waiting or limit_wait):
            self.bar.setProperty("paused", waiting or limit_wait)
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
        elif self.mode == "details" and not self.cancel.is_set():
            if ps.get("total"):
                done, totals = self.details_totals or (0, None)
                where = (f"Getting details · {ps.get('rover')} · sol {fmt(ps.get('sol'))} "
                         f"({ps.get('index', 0):,} of {ps['total']:,})")
                if totals is not None:
                    where += f" · {count(totals.known + totals.matched, 'photo')} matched"
                self.phase.setText(where)
                self.bar.setValue(int(1000 * ps.get("index", 0) / max(1, ps["total"])))
            else:
                self.phase.setText("Getting details…")

        self._update_counters()
        self._update_page_status()

    def _update_counters(self) -> None:
        """Counters for the current download, or the last one after it ends (so unit changes still apply)."""
        r = self.report
        if r is None or self.mode in ("scan", "full", "preview"):
            return
        running = self.mode == "download"
        now = time.time() if running else (self.run_ended or time.time())
        s = self.settings
        window = s.speed_window
        self.counter_heads["Speed"].setText(f"Speed (Last {window} s)")
        self.counter_heads["Speed"].setToolTip(f"Download speed averaged over the last {window} seconds. "
                                               "Change it under View > Units > Speed Averaged Over.")
        if running:
            self.speed_samples.append((now, r.bytes_received, r.downloaded))
            while len(self.speed_samples) > 2 and now - self.speed_samples[1][0] >= window:
                self.speed_samples.popleft()
        if running and len(self.speed_samples) >= 2 and now - self.speed_samples[0][0] >= 1:
            t0, b0, d0 = self.speed_samples[0]
            span = now - t0
            byte_rate, photo_rate = (r.bytes_received - b0) / span, (r.downloaded - d0) / span
            self.counters["Speed"].setText(fmt_rate(byte_rate, s.speed_unit))
            self.counters["Photo Rate"].setText(f"{photo_rate:,.1f} photos/s")
        elif not running:
            self.counters["Speed"].setText("–")
            self.counters["Photo Rate"].setText("–")
        elapsed = now - self.run_started if self.run_started else 0
        self.counters["Elapsed"].setText(fmt_duration(elapsed) if elapsed else "–")
        self.counters["Average Speed"].setText(fmt_rate(r.bytes_received / elapsed, s.speed_unit)
                                               if elapsed >= 1 else "–")
        self.counters["Photos Downloaded"].setText(f"{r.downloaded:,}")
        self.counters["Data Downloaded"].setText(fmt_bytes(r.bytes_saved, s.data_unit))
        self.counters["Already Had"].setText(f"{r.already_present:,}")
        self.counters["Unavailable at NASA"].setText(f"{r.newly_unavailable + r.known_unavailable:,}")
        errors = self.counters["Errors"]
        errors.setText(f"{len(r.problems):,}")
        tone = "err" if r.problems else ""
        if errors.property("tone") != tone:
            errors.setProperty("tone", tone)
            repolish(errors)
        ps = self.progress_state
        remaining = (ps.get("total") or 0) - (ps.get("index") or 0)
        while len(self.sol_samples) > 2 and now - self.sol_samples[1][0] >= 120:      # keep the last 2 minutes
            self.sol_samples.popleft()
        left = None
        if running and remaining > 0:
            # While waiting for a limit (and until sols move again), the estimate isn't measured: it counts down
            # from the last one, which already allows for the waits a download limit makes.
            if not (self.run_paused or self.pause_seen):
                rate = self._sol_rate(now)
                if rate:
                    self.eta_at = now + remaining / rate
            if self.eta_at is not None:
                left = max(0.0, self.eta_at - now)
        if left:
            rest = " (this rover)" if ps.get("rover_count", 1) > 1 else ""
            self.counters["Time Left"].setText(f"~{fmt_duration(left)}{rest}")
        else:
            self.counters["Time Left"].setText("–")

    def _sol_rate(self, now: float) -> float | None:
        """Sols per second lately (the last two minutes of samples), or None while there's too little to tell.
        With a download limit on, never faster than the limit allows: the photos a sol needed lately, at the
        limit's pace (the limit's pauses are then part of the estimate)."""
        if len(self.sol_samples) < 2:
            return None
        (t0, i0, d0), (_, i1, d1) = self.sol_samples[0], self.sol_samples[-1]
        span = now - t0
        if span < 20 or i1 <= i0:
            return None
        rate = (i1 - i0) / span
        lim = self.limiter
        if lim is not None and self.limit_box.isChecked() and d1 > d0 and lim.max_files > 0:
            rate = min(rate, (i1 - i0) / (d1 - d0) * lim.max_files / lim.period)
        return rate

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
                gaps = f"{count(st.gap_sols, 'sol')} · up to {count(st.gap_photos or 0, 'photo')}"
            new = "N/A" if st.new_sols is None else (
                count(st.new_sols, "sol") if st.new_photos is None else
                f"{count(st.new_sols, 'sol')} · ~{count(st.new_photos, 'photo')}")
            values = {"Sols": sols, "Photos": fmt(st.photos), "Size": fmt_size(st.bytes),
                      "Latest Sol": fmt(latest), "To Download": new, "Gaps in Existing Sols": gaps}
            for row, text in values.items():
                self.lib_cells[(row, rover)].setText(text)
            if st.new_sols is not None:
                has_new = True
            disk = "not scanned" if st.sols_on_disk is None else f"{count(st.sols_on_disk, 'sol')} on disk"
            self.rover_status[rover].setText(disk + (f" · latest {latest:,}" if latest is not None else ""))
        self.lib_note.setVisible(has_new)      # only once "To Download" shows numbers
        if not scanned:
            self.outdated_note.setVisible(False)
            self.scanned_label.setText("Not scanned yet")
            self.lib_hint.setText("Click Quick Scan to check this folder." if folder else
                                  "Choose a photo folder, then click Quick Scan.")
            self.lib_hint.setVisible(True)
        else:
            outdated = status.outdated
            self.scanned_label.setText(f"Last scanned: {fmt_when(max(scanned))}")
            self.outdated_note.setVisible(outdated)
            self.lib_hint.setVisible(False)

    # ---- closing -----------------------------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.mode != "idle":
            answer = QMessageBox.question(self, "Stop and Quit?",
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
        self.photos_page.shutdown()
        self.lookups.close()
        self.details.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Rover Image Scraper")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    icon = app_icon_path()
    if icon:
        app.setWindowIcon(QIcon(icon))

    if chosen_state_dir() is None:                  # first run: ask where the app's files go
        apply_theme(app, DARK)                       # the default theme, until settings exist
        setup = SetupDialog()
        if setup.exec() != QDialog.DialogCode.Accepted:
            return 0
        setup.apply()
        setup.deleteLater()

    win = MainWindow(app)
    win._apply_theme()
    try:   # follow the operating system's light/dark changes while "Match operating system" is chosen
        app.styleHints().colorSchemeChanged.connect(
            lambda *_: win._apply_theme() if win.settings.theme == "system" else None)
    except AttributeError:
        pass
    win.show()
    # If the window ever stops responding for half a second or more, slow_moments.log says what it was doing.
    win.slow_moments = SlowMoments(os.path.join(STATE_DIR, "slow_moments.log"), win.slow_context, win)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

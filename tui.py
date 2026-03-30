"""Terminal UI for oref.live — braille map + live feed + AOI alerts."""

from __future__ import annotations

import argparse
import atexit
import asyncio
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from collections import deque
import io
import json as _json_stdlib
try:
    import orjson as _orjson
except ImportError:
    _orjson = None
import logging
import os
import sys
import time
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
from rich.markup import escape as markup_escape
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App as TextualApp, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

# ---------------------------------------------------------------------------
# JSON abstraction (orjson when available, stdlib fallback)
# ---------------------------------------------------------------------------

_use_orjson = _orjson is not None


def _json_loads(data):
    return _orjson.loads(data) if _use_orjson else _json_stdlib.loads(data)


def _json_dumps_pretty(obj) -> bytes:
    if _use_orjson:
        return _orjson.dumps(obj, option=_orjson.OPT_INDENT_2)
    return _json_stdlib.dumps(obj, ensure_ascii=False, indent=2).encode()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

try:
    VERSION = _pkg_version("oref-tui")
except PackageNotFoundError:
    VERSION = "dev"
GITHUB_REPO = "rbrook/oref-tui"

AOI_CONFIG = Path.home() / ".config" / "oref-tui" / "aoi.json"
LOG_FILE = Path.home() / ".local" / "share" / "oref-tui" / "oref-tui.log"

logger = logging.getLogger("oref-tui")


class _LogStream:
    """File-like object that redirects writes to a logger."""
    def __init__(self, log: logging.Logger):
        self._log = log
        self._buf = ""

    def write(self, s: str):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log.error(line)

    def flush(self):
        if self._buf.strip():
            self._log.error(self._buf.strip())
            self._buf = ""

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

# Map aspect ratio bounds (height:width in braille pixels, R = rows*2/cols)
MAP_TRUE_RATIO    = 2.6   # geographic ratio of Israel's bounding box
MAP_MIN_RATIO     = 1.8   # widest allowed (below this Israel looks squat)
MAP_MAX_RATIO     = 3.4   # narrowest allowed (above this Israel is a noodle)
MAP_MAX_WIDTH_PCT = 0.50  # map container never exceeds 50% of terminal width

RED    = "#ff0000"
ORANGE = "#ff8800"
PURPLE = "#ff44ff"
YELLOW = "#ffff00"
BLUE   = "#55aaff"
CYAN   = "#00ffff"
GREEN  = "#44aa44"

# Maps oref.live API color codes to color names
COLOR_CODES = {"r": "red", "o": "orange", "p": "purple", "y": "yellow", "b": "blue"}
COLOR_STYLES = {
    "red":    Style(color=RED),
    "orange": Style(color=ORANGE),
    "purple": Style(color=PURPLE),
    "yellow": Style(color=YELLOW),
    "blue":   Style(color=BLUE),
    "dim":    Style(color="grey30"),
    "white":  Style(color="#dddddd"),
    "cyan":   Style(color=CYAN),
    "green":  Style(color=GREEN),
}
ALERT_BG_STYLES = {
    "red":    Style(color="black", bgcolor=RED,    bold=True),
    "orange": Style(color="black", bgcolor=ORANGE, bold=True),
    "purple": Style(color="white", bgcolor=PURPLE, bold=True),
    "yellow": Style(color="black", bgcolor=YELLOW, bold=True),
    "blue":   Style(color="white", bgcolor=BLUE,   bold=True),
}
TYPE_LABELS = {
    "R": ("טילים", "red"), "U": ("כטב\"מ", "orange"), "I": ("חדירה", "purple"),
    "P": ("מקדימה", "yellow"), "C": ("סיום האירוע", "grey"),
    "Rs": ("הסרת התרעה — טילים", "grey"), "Us": ("הסרת התרעה — כטב\"מ", "grey"),
    "Is": ("הסרת התרעה — חדירה", "grey"), "Ps": ("הסרת התרעה — מקדימה", "grey"),
}
ICON_LABELS = {
    "r": ("🚀", "טילים", RED),    "u": ("✈",  "כטב\"מ",   ORANGE),
    "g": ("🥷", "מחבלים", PURPLE), "p": ("⚠",  "מקדימה",  YELLOW),
}

# ---------------------------------------------------------------------------
# Braille canvas (adapted from android/term_map_demo.py)
# ---------------------------------------------------------------------------

BRAILLE_OFFSETS = {
    (0, 0): 0x01, (1, 0): 0x08, (0, 1): 0x02, (1, 1): 0x10,
    (0, 2): 0x04, (1, 2): 0x20, (0, 3): 0x40, (1, 3): 0x80,
}


class BrailleCanvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.pixels: dict[tuple[int, int], str] = {}

    def set(self, x: float, y: float, color: str = "dim"):
        ix, iy = round(x), round(y)
        if 0 <= ix < self.w and 0 <= iy < self.h:
            if color != "dim" or (ix, iy) not in self.pixels:
                self.pixels[(ix, iy)] = color

    def line(self, x0: float, y0: float, x1: float, y1: float, color: str = "dim"):
        """Bresenham's line algorithm with integer ops."""
        ix0, iy0, ix1, iy1 = round(x0), round(y0), round(x1), round(y1)
        dx = abs(ix1 - ix0)
        dy = -abs(iy1 - iy0)
        sx = 1 if ix0 < ix1 else -1
        sy = 1 if iy0 < iy1 else -1
        err = dx + dy
        w, h, pixels = self.w, self.h, self.pixels
        while True:
            if 0 <= ix0 < w and 0 <= iy0 < h:
                key = (ix0, iy0)
                if color != "dim" or key not in pixels:
                    pixels[key] = color
            if ix0 == ix1 and iy0 == iy1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                ix0 += sx
            if e2 <= dx:
                err += dx
                iy0 += sy

    _COLOR_PRIORITY = {"dim": 0, "white": 1, "green": 2, "blue": 3, "yellow": 4, "orange": 5, "purple": 6, "red": 7, "cyan": 8}

    def render(self) -> Text:
        cols = (self.w + 1) // 2
        rows = (self.h + 3) // 4
        text = Text()
        for row in range(rows):
            for col in range(cols):
                pattern = 0
                cell_color = "dim"
                cell_pri = 0
                for (dx, dy), bit in BRAILLE_OFFSETS.items():
                    px, py = col * 2 + dx, row * 4 + dy
                    if (px, py) in self.pixels:
                        pattern |= bit
                        c = self.pixels[(px, py)]
                        p = self._COLOR_PRIORITY.get(c, 1)
                        if p > cell_pri:
                            cell_color = c
                            cell_pri = p
                if pattern == 0:
                    text.append(" ")
                else:
                    text.append(chr(0x2800 + pattern), COLOR_STYLES.get(cell_color, COLOR_STYLES["dim"]))
            if row < rows - 1:
                text.append("\n")
        return text


# ---------------------------------------------------------------------------
# Polygon loading & projection
# ---------------------------------------------------------------------------

def _simplify_ring(ring: list, max_points: int = 20) -> list[tuple[float, float]]:
    """Downsample a polygon ring to at most max_points vertices."""
    pts = [(c[0], c[1]) for c in ring]
    if len(pts) <= max_points:
        return pts
    step = max(1, len(pts) // max_points)
    simplified = pts[::step]
    if simplified[-1] != pts[-1]:
        simplified.append(pts[-1])
    return simplified


class PolyData:
    """Precomputed polygon data with cached geographic bounds."""
    __slots__ = ("polys", "min_lon", "max_lon", "min_lat", "max_lat")

    def __init__(self, polys: list[tuple[str, list[tuple[float, float]]]]):
        self.polys = polys
        all_lons = [c[0] for _, ring in polys for c in ring]
        all_lats = [c[1] for _, ring in polys for c in ring]
        self.min_lon, self.max_lon = min(all_lons), max(all_lons)
        self.min_lat, self.max_lat = min(all_lats), max(all_lats)


def load_polygons(host: str) -> PolyData:
    """Load polygons from backend, simplify, precompute bounds."""
    resp = httpx.get(f"{host}/static/israel.geojson", timeout=10)
    gj = resp.json()
    logger.info("GeoJSON loaded from %s (%d features)", host, len(gj.get("features", [])))

    polys = []
    for feat in gj["features"]:
        name = feat["properties"]["name"]
        geom = feat["geometry"]
        coords = geom["coordinates"]
        if geom["type"] == "Polygon":
            polys.append((name, _simplify_ring(coords[0])))
        elif geom["type"] == "MultiPolygon":
            for poly in coords:
                polys.append((name, _simplify_ring(poly[0])))

    del gj
    return PolyData(polys)


def compute_centroids(pd: PolyData) -> dict[str, tuple[float, float]]:
    """Compute centroid (lat, lon) for each named polygon."""
    centroids: dict[str, tuple[float, float]] = {}
    for name, ring in pd.polys:
        if name in centroids:
            continue
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        centroids[name] = (sum(lats) / len(lats), sum(lons) / len(lons))
    return centroids


def project_polygons(pd: PolyData, canvas_w, canvas_h):
    lon_range = pd.max_lon - pd.min_lon or 1
    lat_range = pd.max_lat - pd.min_lat or 1
    scale_x = (canvas_w - 1) / lon_range
    scale_y = (canvas_h - 1) / lat_range

    projected = []
    for name, ring in pd.polys:
        pts = [(
            (lon - pd.min_lon) * scale_x,
            (pd.max_lat - lat) * scale_y,
        ) for lon, lat in ring]
        projected.append((name, pts))
    return projected


# ---------------------------------------------------------------------------
# AOI persistence
# ---------------------------------------------------------------------------

_DEFAULT_VIEW = {"map": True, "feed": True, "aoi": True}


def load_aoi() -> tuple[list[dict], dict]:
    if AOI_CONFIG.exists():
        try:
            data = _json_loads(AOI_CONFIG.read_bytes())
            if isinstance(data, list):
                return data, dict(_DEFAULT_VIEW)
            return data.get("items", []), {**_DEFAULT_VIEW, **data.get("view", {})}
        except Exception:
            pass
    return [], dict(_DEFAULT_VIEW)


def save_aoi(items: list[dict], view: dict):
    AOI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = AOI_CONFIG.with_suffix(".tmp")
    tmp.write_bytes(_json_dumps_pretty({"items": items, "view": view}))
    tmp.replace(AOI_CONFIG)
    AOI_CONFIG.chmod(0o600)


# ---------------------------------------------------------------------------
# Relative time (Hebrew)
# ---------------------------------------------------------------------------

def relative_time(ts: int) -> tuple[str, bool]:
    """Returns (text, is_recent) where is_recent = under 10 minutes."""
    diff = int(time.time()) - ts
    if diff < 60:
        return "עכשיו", True
    if diff < 3600:
        m = diff // 60
        return f"לפני {m} דקות", m < 10
    if diff < 86400:
        return f"לפני {diff // 3600} שעות", False
    return f"לפני {diff // 86400} ימים", False


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class MapWidget(Static):
    alert_areas: reactive[dict[str, str]] = reactive(dict, always_update=True)
    aoi_areas: reactive[set[str]] = reactive(set, always_update=True)
    highlighted_aoi: reactive[set[str]] = reactive(set, always_update=True)
    _host: str | None = None  # set by app before compose

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._polys = load_polygons(self._host)
        self._projected = []
        self._base_pixels: dict[tuple[int, int], str] = {}
        self._prev_alerts: dict[str, str] = {}
        self._prev_aoi: set[str] = set()
        self._prev_highlighted: set[str] = set()

    def on_resize(self, event):
        w = self.size.width * 2
        h = self.size.height * 4
        self._projected = project_polygons(self._polys, w, h)
        self._base_pixels = {}
        canvas = BrailleCanvas(w, h)
        for name, pts in self._projected:
            for i in range(len(pts)):
                x0, y0 = pts[i]
                x1, y1 = pts[(i + 1) % len(pts)]
                canvas.line(x0, y0, x1, y1, "dim")
        self._base_pixels = dict(canvas.pixels)
        self._render_map()

    def watch_alert_areas(self, alerts: dict[str, str]):
        if alerts == self._prev_alerts:
            return
        self._prev_alerts = dict(alerts)
        self._render_map()

    def watch_aoi_areas(self, aoi: set[str]):
        if aoi == self._prev_aoi:
            return
        self._prev_aoi = set(aoi)
        self._render_map()

    def watch_highlighted_aoi(self, highlighted: set[str]):
        if highlighted == self._prev_highlighted:
            return
        self._prev_highlighted = set(highlighted)
        self._render_map()

    def _render_map(self):
        if not self._projected:
            return
        w = self.size.width * 2
        h = self.size.height * 4
        canvas = BrailleCanvas(w, h)
        # Start from base, overlay active layers on top
        base = self._base_pixels
        overlay: dict[tuple[int, int], str] = {}
        for priority_names, get_color in [
            (self.aoi_areas,       lambda n: "white"),
            (self.alert_areas,     lambda n: self.alert_areas[n]),
            (self.highlighted_aoi, lambda n: "cyan"),
        ]:
            for name, pts in self._projected:
                if name not in priority_names:
                    continue
                color = get_color(name)
                for i in range(len(pts)):
                    x0, y0 = pts[i]
                    x1, y1 = pts[(i + 1) % len(pts)]
                    # Inline Bresenham directly into overlay dict
                    ix0, iy0 = round(x0), round(y0)
                    ix1, iy1 = round(x1), round(y1)
                    dx = abs(ix1 - ix0)
                    dy = -abs(iy1 - iy0)
                    sx = 1 if ix0 < ix1 else -1
                    sy = 1 if iy0 < iy1 else -1
                    err = dx + dy
                    while True:
                        if 0 <= ix0 < w and 0 <= iy0 < h:
                            overlay[(ix0, iy0)] = color
                        if ix0 == ix1 and iy0 == iy1:
                            break
                        e2 = 2 * err
                        if e2 >= dy:
                            err += dy
                            ix0 += sx
                        if e2 <= dx:
                            err += dx
                            iy0 += sy
        # Merge: overlay wins over base
        if overlay:
            merged = {**base, **overlay}
            canvas.pixels = merged
        else:
            canvas.pixels = base
        self.update(canvas.render())


class FeedCard(ListItem):
    """A single feed card in the list."""

    DEFAULT_CSS = """
    FeedCard {
        height: auto;
        border-bottom: solid #333333;
        padding: 0 1;
    }
    FeedCard Label {
        width: 1fr;
    }
    FeedCard .districts-label {
        width: 1fr;
        text-align: right;
    }
    """

    _codemap: dict = {}  # set by app

    def __init__(self, card: dict, **kwargs):
        super().__init__(**kwargs)
        self.card = card

    def compose(self) -> ComposeResult:
        yield Label(self._build_header(), id="card-header")
        dist_hexes = self.card.get("d", [])
        if dist_hexes and self._codemap:
            dists = self._codemap.get("districts", {})
            names = [dists[dh] for dh in dist_hexes if dh in dists]
            if names:
                yield Label(markup_escape(" · ".join(names)), classes="districts-label")

    def _build_header(self) -> Text:
        t = self.card.get("t", "?")
        label, color = TYPE_LABELS.get(t, (t, "grey"))
        n = self.card.get("n", 0)
        ts = self.card.get("s", 0)
        ago, is_recent = relative_time(ts)
        hhmm = time.strftime("%H:%M", time.localtime(ts))
        header = Text()
        style = COLOR_STYLES[color] if color in COLOR_STYLES else Style(color="grey50")
        header.append("■  ", style)
        header.append(f"{label} — {n} ישובים", style)
        header.append(f"  {hhmm} ", Style(color="grey50"))
        if is_recent:
            ago_style = ALERT_BG_STYLES.get(color, Style(color="white", bgcolor="#444444", bold=True))
            header.append(ago, ago_style)
        else:
            header.append(ago, Style(color="grey50"))
        return header

    def refresh_time(self):
        try:
            self.query_one("#card-header", Label).update(self._build_header())
        except Exception:
            pass


class FeedWidget(ListView):

    DEFAULT_CSS = """
    FeedWidget {
        height: 1fr;
        background: #000000;
    }
    FeedWidget > ListItem.--highlight {
        background: #1a1a1a;
    }
    """

    BINDINGS = [
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("enter", "select_card", "Open card", show=False),
    ]

    def action_page_down(self):
        count = len(list(self.children))
        if not count:
            return
        page = max(1, self.size.height)
        idx = min((self.index or 0) + page, count - 1)
        self.index = idx
        self.scroll_to_widget(self.children[idx], top=True)

    def action_page_up(self):
        count = len(list(self.children))
        if not count:
            return
        page = max(1, self.size.height)
        idx = max((self.index or 0) - page, 0)
        self.index = idx
        self.scroll_to_widget(self.children[idx], top=True)

    class CardSelected(Message):
        def __init__(self, card: dict):
            super().__init__()
            self.card = card

    def action_select_card(self):
        if self.index is not None:
            children = list(self.children)
            if 0 <= self.index < len(children):
                item = children[self.index]
                if isinstance(item, FeedCard):
                    self.post_message(self.CardSelected(dict(item.card)))

    def _on_click(self, event):
        """Suppress mouse clicks — keyboard only."""
        event.stop()
        event.prevent_default()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cards: list[dict] = []
        self._codemap: dict = {}

    def on_mount(self):
        self.set_interval(15, self._refresh_times)

    @staticmethod
    def _card_key(c: dict) -> tuple:
        return (c.get("t", ""), c.get("s", 0), c.get("n", 0))

    def update_feed(self, cards: list[dict], codemap: dict):
        if cards == self._cards:
            return
        self._cards = cards
        self._codemap = codemap
        self._render_cards()

    def _refresh_times(self):
        for child in self.children:
            if isinstance(child, FeedCard):
                child.refresh_time()

    def _render_cards(self):
        try:
            cutoff = time.time() - 6 * 3600
            visible = [c for c in self._cards if c.get("s", 0) >= cutoff][:30]
            new_keys = [self._card_key(c) for c in visible]
            old_keys = [self._card_key(ch.card) for ch in self.children if isinstance(ch, FeedCard)]
            # Skip rebuild if card list is unchanged
            if new_keys == old_keys:
                return
            prev_index = self.index
            self.clear()
            if not visible:
                self.append(ListItem(Static(
                    Text("— no recent alerts —", Style(color="grey30")),
                    id="feed-empty")))
                return
            for card in visible:
                self.append(FeedCard(card))
            if prev_index is not None and prev_index < len(visible):
                self.index = prev_index
        except Exception:
            logger.exception("Error rendering feed cards")


THREAT_ORDER = ["p", "r", "u", "g"]  # pre-warning, rockets, uav, infiltration


LRM = '\u200E'  # Left-to-Right Mark — forces LTR paragraph direction for bidi


class ThreatChip(Static):
    """Single threat type indicator — emoji + label, grey when inactive."""

    def __init__(self, key: str, **kwargs):
        icon, label, color = ICON_LABELS[key]
        # LRM prefix forces LTR paragraph direction so emoji stays left of Hebrew
        t = Text(f"{LRM}· {label}", Style(color="grey30"))
        super().__init__(t, **kwargs)
        self._key = key
        self._active = False
        self._icon = icon
        self._label = label
        self._color = color

    def set_active(self, active: bool):
        if active == self._active:
            return
        self._active = active
        if active:
            self.update(Text(f"{LRM}{self._icon} {self._label}", Style(color=self._color)))
        else:
            self.update(Text(f"{LRM}· {self._label}", Style(color="grey30")))


class ThreatBar(Widget):

    DEFAULT_CSS = """
    ThreatBar {
        layout: horizontal;
        height: 1;
    }
    ThreatChip {
        width: auto;
        margin: 0 1 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        for key in THREAT_ORDER:
            yield ThreatChip(key, id=f"threat-{key}")

    def update_threats(self, icon_str: str):
        for key in THREAT_ORDER:
            self.query_one(f"#threat-{key}", ThreatChip).set_active(key in icon_str)



class LiveBadge(Static):
    DEFAULT_CSS = """
    LiveBadge {
        dock: bottom;
        height: 1;
        width: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    def __init__(self, **kwargs):
        super().__init__("", **kwargs)

    def pulse(self):
        self.update(Text("● LIVE", Style(color="green3", bold=True)))

    def set_offline(self):
        self.update(Text("● OFFLINE", Style(color="red1", bold=True)))


class HeaderBar(Widget):

    # Minimum width to show info + threat chips on a single line
    _SINGLE_LINE_MIN = 75

    DEFAULT_CSS = """
    HeaderBar {
        height: auto;
    }
    #header-info {
        width: 1fr;
        height: 1;
    }
    HeaderBar ThreatBar {
        height: 1;
    }
    HeaderBar ThreatChip {
        width: auto;
        margin: 0 0 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="header-info")
        yield ThreatBar(id="threat-bar")

    def on_resize(self, event):
        self._update_layout()

    def _update_layout(self):
        w = self.size.width
        if w >= self._SINGLE_LINE_MIN:
            self.styles.layout = "horizontal"
            self.query_one("#threat-bar").styles.width = "auto"
        else:
            self.styles.layout = "vertical"
            self.query_one("#threat-bar").styles.width = "1fr"

    def on_mount(self):
        self._update_layout()
        self.set_interval(8, self._refresh)
        self._refresh()
        self.run_worker(self._check_version(), exclusive=False)

    async def _check_version(self):
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 200:
                tag = resp.json().get("tag_name", "").lstrip("v")
                if tag and tag != VERSION:
                    self._upgrade_available = True
                    self._refresh()
        except Exception:
            pass

    def _refresh(self):
        now = time.time()
        cpu_now = time.process_time()
        dt = now - self._prev_wall
        dcpu = cpu_now - self._prev_cpu
        self._prev_wall = now
        self._prev_cpu = cpu_now
        if dt > 0:
            self._cpu_history.append(dcpu / dt * 100)
        cpu_str = f"{sum(self._cpu_history) / len(self._cpu_history):.1f}%" if self._cpu_history else "..."
        try:
            with open("/proc/self/statm") as f:
                rss_pages = int(f.read().split()[1])
            rss_mb = rss_pages * 4096 / (1024 * 1024)
        except Exception:
            rss_mb = 0
        text = Text()
        text.append("oref.live", Style(bold=True))
        text.append(f" v{VERSION}", Style(color="grey37"))
        if self._upgrade_available:
            text.append("  (upgrade available)", Style(color="#006400"))
        text.append("  CPU ", Style(color="grey37"))
        text.append(cpu_str, Style(color="grey62"))
        text.append("  RAM ", Style(color="grey37"))
        text.append(f"{rss_mb:.0f}MB", Style(color="grey62"))
        self.query_one("#header-info", Static).update(text)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._prev_wall = time.time()
        self._prev_cpu = time.process_time()
        self._cpu_history: deque[float] = deque(maxlen=3)
        self._upgrade_available: bool = False


_AOI_CHIP_COLORS = {
    "red":    (RED,    "white", True),
    "orange": (ORANGE, "black", True),
    "purple": (PURPLE, "white", True),
    "yellow": (YELLOW, "black", True),
    "blue":   (BLUE,   "white", True),
}


class AoiChip(ListItem):
    """A single AOI item."""

    DEFAULT_CSS = """
    AoiChip {
        width: 1fr;
        height: 1;
    }
    AoiChip Label {
        width: 1fr;
    }
    """

    def __init__(self, item: dict, color: str = "cyan", **kwargs):
        super().__init__(**kwargs)
        self.aoi_item = item
        if color in _AOI_CHIP_COLORS:
            bg, fg, bold = _AOI_CHIP_COLORS[color]
            self._label_style = Style(color=fg, bold=bold)
            self._bg = bg
        else:
            self._label_style = Style(color="cyan")
            self._bg = None

    def on_mount(self):
        if self._bg:
            self.styles.background = self._bg

    def compose(self) -> ComposeResult:
        yield Label(Text(f" {self.aoi_item['name']}", self._label_style))


class AoiPanel(Widget):
    """AOI panel with title and interactive list."""

    BINDINGS = [
        Binding("backspace", "remove_selected", "Remove", show=False),
        Binding("delete", "remove_selected", "Remove", show=False),
    ]

    DEFAULT_CSS = """
    AoiPanel {
        height: auto;
        max-height: 12;
        background: #000000;
        padding: 0 1;
    }
    #aoi-title {
        height: 1;
    }
    #aoi-list {
        height: auto;
        max-height: 8;
        background: #000000;
    }
    #aoi-list > ListItem {
        height: 1;
        width: 1fr;
    }
    #aoi-list > ListItem.--highlight {
        background: #333333;
    }
    """

    class RemoveRequested(Message):
        def __init__(self, name: str):
            super().__init__()
            self.name = name

    class HighlightChanged(Message):
        def __init__(self, area_names: set[str]):
            super().__init__()
            self.area_names = area_names

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._items: list[dict] = []
        self._alert_areas: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static(Text("Areas of Interest", Style(bold=True)), id="aoi-title")
        yield ListView(id="aoi-list")

    def update_chips(self, items: list[dict]):
        self._items = items
        self._rebuild_list()

    def update_alerts(self, alert_areas: dict[str, str]):
        if alert_areas == self._alert_areas:
            return
        self._alert_areas = alert_areas
        self._rebuild_list()

    def _rebuild_list(self):
        lv = self.query_one("#aoi-list", ListView)
        prev_index = lv.index
        lv.clear()
        for item in self._items:
            name = item["name"]
            # Check if any area in this AOI item is under alert
            color = "cyan"
            if item.get("type") == "district":
                for a in item.get("areas", []):
                    if a in self._alert_areas:
                        color = self._alert_areas[a]
                        break
            elif name in self._alert_areas:
                color = self._alert_areas[name]
            lv.append(AoiChip(item, color=color))
        if prev_index is not None and prev_index < len(self._items):
            lv.index = prev_index

    def on_list_view_highlighted(self, event: ListView.Highlighted):
        if event.item and isinstance(event.item, AoiChip):
            item = event.item.aoi_item
            names = set()
            if item.get("type") == "district":
                for a in item.get("areas", []):
                    names.add(a)
            else:
                names.add(item["name"])
            self.post_message(self.HighlightChanged(names))
        else:
            self.post_message(self.HighlightChanged(set()))

    def on_descendant_blur(self, event):
        self.post_message(self.HighlightChanged(set()))

    def action_remove_selected(self):
        lv = self.query_one("#aoi-list", ListView)
        if lv.index is not None and lv.index < len(self._items):
            name = self._items[lv.index]["name"]
            self.post_message(self.RemoveRequested(name))



# ---------------------------------------------------------------------------
# Typed ListItem subclasses (avoid monkey-patching framework objects)
# ---------------------------------------------------------------------------

class _SearchResult(ListItem):
    def __init__(self, search_item: dict, *children, **kwargs):
        super().__init__(*children, **kwargs)
        self.search_item = search_item


class _AoiRemoveItem(ListItem):
    def __init__(self, aoi_name: str, *children, **kwargs):
        super().__init__(*children, **kwargs)
        self.aoi_name = aoi_name


# ---------------------------------------------------------------------------
# AOI search modal
# ---------------------------------------------------------------------------

class AoiSearchScreen(Screen[dict | None]):

    DEFAULT_CSS = """
    AoiSearchScreen {
        background: $background;
    }
    #search-header {
        height: 1;
        background: #2a2a4a;
        padding: 0 1;
        color: white;
    }
    #search-input {
        height: 3;
        margin: 0 1;
        background: $surface;
        color: white;
        border: solid #555555;
    }
    #search-results {
        height: 1fr;
        padding: 0 1;
    }
    #search-results ListItem {
        height: 1;
        padding: 0 1;
    }
    #search-results ListItem.--highlight {
        background: #1a1a1a;
    }
    """

    def __init__(self, host: str):
        super().__init__()
        self._host = host
        self._results: list[dict] = []
        self._debounce_timer: asyncio.TimerHandle | None = None

    def compose(self) -> ComposeResult:
        yield Static(Text("חיפוש אזור להתראה", Style(bold=True, color="white")), id="search-header")
        yield Input(placeholder="type area name...", id="search-input")
        yield ListView(id="search-results")
        yield Footer()

    def on_mount(self):
        self.query_one("#search-input", Input).focus()

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("down", "focus_results", "Results", show=False),
        Binding("up", "focus_results", "Results", show=False),
    ]

    def action_focus_results(self):
        lv = self.query_one("#search-results", ListView)
        if list(lv.children):
            lv.focus()
            if lv.index is None:
                lv.index = 0

    def on_input_changed(self, event: Input.Changed):
        q = event.value.strip()
        if len(q) < 2:
            self.query_one("#search-results", ListView).clear()
            self._results = []
            return
        if self._debounce_timer:
            self._debounce_timer.cancel()
        loop = asyncio.get_event_loop()
        self._debounce_timer = loop.call_later(0.3, lambda: self._do_search(q))

    @work(thread=True)
    def _do_search(self, q: str):
        try:
            resp = httpx.get(f"{self._host}/api/lookup", params={"q": q, "limit": "10"}, timeout=3)
            data = resp.json()
            self._results = data.get("results", []) if isinstance(data, dict) else data
        except Exception:
            self._results = []
        self.app.call_from_thread(self._show_results)

    def _show_results(self):
        lv = self.query_one("#search-results", ListView)
        lv.clear()
        for item in self._results:
            name = item.get("name", "")
            kind = item.get("type", "area")
            tag = "מחוז" if kind == "district" else "ישוב"
            t = Text()
            t.append(name)
            t.append(f"  [{tag}]", Style(color="grey50"))
            lv.append(_SearchResult(item, Label(t)))

    def on_list_view_selected(self, event: ListView.Selected):
        if isinstance(event.item, _SearchResult):
            self.dismiss(event.item.search_item)

    def on_input_submitted(self, event: Input.Submitted):
        lv = self.query_one("#search-results", ListView)
        children = list(lv.children)
        if children:
            idx = lv.index if lv.index is not None else 0
            if 0 <= idx < len(children):
                item = children[idx]
                if isinstance(item, _SearchResult):
                    self.dismiss(item.search_item)

    def action_go_back(self):
        self.dismiss(None)


class FeedDetailScreen(Screen):

    BINDINGS = [Binding("escape", "go_back", "Back")]

    DEFAULT_CSS = """
    FeedDetailScreen {
        background: $background;
    }
    #detail-header {
        height: 1;
        background: #2a2a4a;
        padding: 0 1;
        color: white;
    }
    #detail-main {
        height: 1fr;
    }
    #detail-left {
        width: 1fr;
    }
    #detail-filter {
        height: 3;
        margin: 0 1;
        background: $surface;
        color: white;
        border: solid #555555;
    }
    #detail-districts {
        height: auto;
        padding: 0 1;
        margin: 1 0;
    }
    #detail-scroll {
        height: 1fr;
        padding: 0 2;
    }
    #detail-map {
        width: 1fr;
        height: 1fr;
        border-left: solid #555555;
    }
    """

    def __init__(self, card: dict, host: str, codemap: dict):
        super().__init__()
        self._card = card
        self._host = host
        self._codemap = codemap
        self._all_names: list[str] = []
        self._district_names: list[str] = []
        self._color = "blue"

    def compose(self) -> ComposeResult:
        t = self._card.get("t", "?")
        label, color = TYPE_LABELS.get(t, (t, "grey"))
        self._color = color
        n = self._card.get("n", 0)
        ts = self._card.get("s", 0)
        ago, _ = relative_time(ts)
        hhmm = time.strftime("%H:%M", time.localtime(ts))
        style = COLOR_STYLES[color] if color in COLOR_STYLES else Style(color="grey50")
        yield Static(Text(f"{LRM}■ {label} — {n} ישובים  {ago}  {hhmm}", style), id="detail-header")
        with Horizontal(id="detail-main"):
            with Vertical(id="detail-left"):
                yield Input(placeholder="filter areas...", id="detail-filter")
                yield Static("", id="detail-districts")
                with VerticalScroll(id="detail-scroll"):
                    yield Static(Text("loading...", Style(color="grey50")), id="detail-body")
            yield MapWidget(id="detail-map")
        yield Footer()

    def on_mount(self):
        self.query_one("#detail-filter").focus()
        self._update_map_width()
        self._load_areas()

    def on_resize(self, event):
        self._update_map_width()

    def _update_map_width(self):
        total_w = self.size.width
        # header(1) + footer(1) = 2 rows overhead
        map_h = max(1, self.size.height - 2)
        ratio_max = int(map_h * 2 / MAP_MIN_RATIO)
        ratio_min = max(1, int(map_h * 2 / MAP_TRUE_RATIO))
        map_cols = min(int(total_w * MAP_MAX_WIDTH_PCT), ratio_max)
        map_cols = max(map_cols, ratio_min)
        self.query_one("#detail-map").styles.width = map_cols

    def on_input_changed(self, event: Input.Changed):
        self._filter_areas(event.value.strip())

    @work
    async def _load_areas(self):
        t = self._card.get("t", "")
        s = self._card.get("s", 0)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._host}/api/card-areas", params={"h": self._card.get("h", "")})
            data = resp.json()
            hex_ids = data if isinstance(data, list) else data.get("areas", [])
            area_map = self._codemap.get("areas", {})
            names = sorted(area_map.get(h, h) for h in hex_ids)
            a2d = self._codemap.get("area_to_district", {})
            dists = self._codemap.get("districts", {})
            seen = {}
            for h in hex_ids:
                dh = a2d.get(h)
                if dh and dh not in seen:
                    seen[dh] = dists.get(dh, dh)
            district_names = list(seen.values())
        except Exception:
            names = ["(error loading)"]
            district_names = []
            hex_ids = []
        self._on_areas_loaded(names, district_names, hex_ids)

    def _on_areas_loaded(self, names: list[str], districts: list[str], hex_ids: list[str] = None):
        self._all_names = names
        self._district_names = districts
        # Show districts
        if districts:
            dt = Text()
            for i, d in enumerate(districts):
                dt.append(f" {d} ", Style(color="white", bgcolor="#444444"))
                if i < len(districts) - 1:
                    dt.append(" ")
            self.query_one("#detail-districts").update(dt)
        # Show all areas
        self._filter_areas("")
        # Highlight areas on the detail map
        if hex_ids:
            area_map = self._codemap.get("areas", {})
            # End-of-event ("grey") shows as green on the detail map
            map_color = "green" if self._color == "grey" else self._color
            alert_areas = {area_map.get(h, h): map_color for h in hex_ids}
            self.query_one("#detail-map", MapWidget).alert_areas = alert_areas

    def _filter_areas(self, query: str):
        if query:
            filtered = [n for n in self._all_names if query in n]
        else:
            filtered = self._all_names
        text = Text()
        for i, name in enumerate(filtered):
            text.append(name, Style(color="grey85"))
            if i < len(filtered) - 1:
                text.append("\n")
        if not filtered and self._all_names:
            text.append("(no match)", Style(color="grey50"))
        elif not filtered:
            text.append("(no areas)", Style(color="grey50"))
        self.query_one("#detail-body").update(text)

    def action_go_back(self):
        self.app.pop_screen()


class AoiRemoveScreen(Screen[str | None]):

    BINDINGS = [Binding("escape", "go_back", "Back")]

    DEFAULT_CSS = """
    AoiRemoveScreen {
        background: $background;
    }
    #remove-header {
        height: 1;
        background: #2a2a4a;
        padding: 0 1;
        color: white;
    }
    #remove-list {
        height: 1fr;
        padding: 0 1;
    }
    #remove-list ListItem {
        height: 1;
        padding: 0 1;
    }
    #remove-list ListItem.--highlight {
        background: #1a1a1a;
    }
    """

    def __init__(self, items: list[dict]):
        super().__init__()
        self._items = items

    def compose(self) -> ComposeResult:
        yield Static(Text("הסרת אזור — Enter to remove", Style(bold=True, color="white")), id="remove-header")
        yield ListView(*[_AoiRemoveItem(i["name"], Label(i["name"])) for i in self._items], id="remove-list")
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected):
        if isinstance(event.item, _AoiRemoveItem):
            self.dismiss(event.item.aoi_name)

    def action_go_back(self):
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class PingReceived(Message):
    pass


class ConnectionChanged(Message):
    def __init__(self, connected: bool):
        super().__init__()
        self.connected = connected


class SnapshotReceived(Message):
    def __init__(self, data: dict):
        super().__init__()
        self.data = data


class App(TextualApp):
    TITLE = "oref.live TUI"
    CSS = """
    Screen {
        layout: vertical;
        background: #000000;
    }
    #header-bar {
        height: auto;
        dock: top;
        background: #0d1117;
        padding: 0 1;
    }
    #main {
        height: 1fr;
    }
    #map-container {
        width: 1fr;
        height: 1fr;
    }
    #map-container.has-right {
        border-right: solid #555555;
    }
    #map-panel {
        width: 1fr;
        height: 1fr;
    }
    #right-panel {
        width: 1fr;
    }
    .hidden {
        display: none;
    }

    #aoi-panel {
        height: auto;
        max-height: 12;
        padding: 0 1;
        border-bottom: solid #555555;
    }
    #feed {
        height: 1fr;
        background: #000000;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("escape", "blur_all", "Unfocus", show=False),
        Binding("tab", "focus_panel", "Focus", show=False, priority=True),
        Binding("a", "add_aoi", "Add AOI", priority=True),
        Binding("d", "delete_aoi", "Del AOI", priority=True),
        Binding("m", "toggle_map", "Map", priority=True),
        Binding("f", "toggle_feed", "Feed", priority=True),
        Binding("i", "toggle_aoi", "AOI", priority=True),
    ]

    def __init__(self, host: str | None = None, debug: bool = False):
        super().__init__()
        self._host = host or os.environ.get("OREF_HOST", "https://oref.live")
        self._codemap: dict = {}
        self._connected = False
        self._last_update = 0.0
        self._last_contact = 0.0  # time of last ping or snapshot
        self._fe_stale = False    # no SSE data for >10s
        self._app_stale = False   # backend can't reach poller (snapshot.s)
        self._sys_down = False    # oref API unreachable (snapshot.d)
        self._last_alert_count = 0
        self._aoi_items, self._view_state = load_aoi()
        self._prev_alert_names: set[str] = set()
        self._sse_stop = False
        self._sse_client: httpx.Client | None = None
        if debug:
            self._setup_logging()

    def _setup_logging(self):
        import sys
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch(mode=0o600, exist_ok=True)
        handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.info("TUI started, host=%s", self._host)
        sys.excepthook = lambda t, v, tb: logger.critical("Unhandled exception", exc_info=(t, v, tb))
        # Also redirect stderr to log file so Textual tracebacks are captured
        sys.stderr = _LogStream(logger)

    def compose(self) -> ComposeResult:
        MapWidget._host = self._host
        yield HeaderBar(id="header-bar")
        with Horizontal(id="main"):
            with Vertical(id="map-container"):
                yield MapWidget(id="map-panel")
                yield LiveBadge(id="live-badge")
            with Vertical(id="right-panel"):
                yield AoiPanel(id="aoi-panel")
                yield FeedWidget(id="feed")
        yield Footer()

    def on_resize(self, event):
        self._update_map_width()

    def _update_map_width(self):
        """Recalculate map container width from screen dimensions + ratio bounds."""
        total_w = self.size.width
        map_h = max(1, self.size.height - 3)
        v = self._view_state
        right_visible = v["feed"] or v["aoi"]
        # Max width from ratio: map can't be wider than MIN_RATIO allows
        ratio_max = int(map_h * 2 / MAP_MIN_RATIO)
        ratio_min = max(1, int(map_h * 2 / MAP_TRUE_RATIO))
        if right_visible:
            map_cols = min(int(total_w * MAP_MAX_WIDTH_PCT), ratio_max)
        else:
            # Map-only: full width available, still capped by ratio
            map_cols = min(total_w, ratio_max)
        map_cols = max(map_cols, ratio_min)
        container = self.query_one("#map-container")
        container.styles.width = map_cols
        self.screen.refresh(layout=True)

    def on_mount(self):
        v = self._view_state
        self.query_one("#map-container").display = v["map"]
        self.query_one("#feed").display = v["feed"]
        self.query_one("#aoi-panel").display = v["aoi"]
        self.query_one("#aoi-panel", AoiPanel).update_chips(self._aoi_items)
        self._sync_aoi_to_map()
        self._sync_right_panel()  # also calls _update_map_width
        atexit.register(self._shutdown_sse)
        self._start_sse()
        self.set_interval(2, self._stale_check_tick)

    @work(thread=True, exclusive=True, group="sse", exit_on_error=False)
    def _start_sse(self):
        # Fetch codemap first
        try:
            resp = httpx.get(f"{self._host}/api/codemap", timeout=5)
            self._codemap = resp.json()
            FeedCard._codemap = self._codemap
            logger.info("Codemap loaded: %d areas", len(self._codemap.get("areas", {})))
        except Exception as e:
            logger.error("Codemap fetch failed: %s", e)
            self._codemap = {}

        backoff = 1.0
        while not self._sse_stop:
            try:
                self._sse_client = httpx.Client(timeout=httpx.Timeout(connect=5, read=35, write=5, pool=5))
                with self._sse_client.stream("GET", f"{self._host}/api/stream",
                                              headers={"Accept": "text/event-stream",
                                                       "Cache-Control": "no-cache",
                                                       "User-Agent": f"oref-tui/{VERSION}"}) as resp:
                    self._connected = True
                    self._fe_stale = False
                    self._last_contact = time.time()
                    logger.info("SSE connected")
                    self.call_from_thread(self._update_connection_ui)
                    backoff = 1.0
                    buf = ""
                    for chunk in resp.iter_text():
                        if self._sse_stop:
                            break
                        buf += chunk
                        if len(buf) > 10_000_000:
                            logger.error("SSE buffer exceeded 10MB, disconnecting")
                            break
                        while "\n\n" in buf:
                            event_raw, buf = buf.split("\n\n", 1)
                            self._parse_sse_event(event_raw)
            except Exception as e:
                if not self._sse_stop:
                    logger.error("SSE error: %s", e)
            finally:
                if self._sse_client:
                    try:
                        self._sse_client.close()
                    except Exception:
                        pass
                    self._sse_client = None
            self._connected = False
            self._fe_stale = True
            logger.warning("SSE disconnected, backoff=%.1fs", backoff)
            try:
                self.call_from_thread(self._update_connection_ui)
            except Exception:
                pass
            if self._sse_stop:
                break
            # Sleep using time.sleep (threading.Event.wait can hang)
            end = time.time() + backoff
            while time.time() < end and not self._sse_stop:
                time.sleep(0.2)
            if self._sse_stop:
                break
            logger.debug("Attempting reconnect...")
            backoff = min(backoff * 2, 10)

    def _parse_sse_event(self, raw: str):
        event_type = ""
        data_parts = []
        for line in raw.strip().split("\n"):
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].strip())
        if event_type == "snapshot" and data_parts:
            try:
                raw = data_parts[0] if len(data_parts) == 1 else "".join(data_parts)
                data = _json_loads(raw)
                self.post_message(SnapshotReceived(data))
            except (ValueError, Exception):
                pass
        elif event_type == "ping":
            self._last_contact = time.time()
            self._fe_stale = False
            try:
                self.call_from_thread(self._on_ping)
            except Exception:
                pass
        elif event_type == "reload":
            try:
                self.call_from_thread(self._on_reload)
            except Exception:
                logger.exception("Error handling reload event")

    def on_snapshot_received(self, msg: SnapshotReceived):
        try:
            self._handle_snapshot(msg.data)
        except Exception:
            logger.exception("Error handling snapshot")

    def _handle_snapshot(self, data: dict):
        now = time.time()
        self._last_update = now
        self._last_contact = now
        self._fe_stale = False

        # Track backend stale flags
        was_app_stale = self._app_stale
        was_sys_down = self._sys_down
        self._app_stale = bool(data.get("s"))
        self._sys_down = bool(data.get("d"))
        if self._app_stale and not was_app_stale:
            logger.warning("Backend: data source unreachable")
        if not self._app_stale and was_app_stale:
            logger.info("Backend: data source reconnected")
        if self._sys_down and not was_sys_down:
            logger.warning("Backend: alert source unreachable (oref)")
        if not self._sys_down and was_sys_down:
            logger.info("Backend: alert source recovered")

        self._update_connection_ui()
        if not self._app_stale:
            try:
                self.query_one("#live-badge", LiveBadge).pulse()
            except Exception:
                pass

        # If backend is stale, don't update map/feed (mirrors webapp behavior)
        if self._app_stale:
            return

        logger.debug("Snapshot: %d areas, icons=%s, %d feed cards",
                      len(data.get("a", {})), data.get("i", ""), len(data.get("f", [])))
        area_map = self._codemap.get("areas", {})

        # Resolve hex IDs to area names + colors
        alert_areas: dict[str, str] = {}
        raw_areas = data.get("a", {})
        for hex_id, val in raw_areas.items():
            name = area_map.get(hex_id, hex_id)
            color_code = val[0] if isinstance(val, list) else val
            color = COLOR_CODES.get(color_code, "blue")
            alert_areas[name] = color

        with self.batch_update():
            self.query_one("#map-panel", MapWidget).alert_areas = alert_areas
            self.query_one("#aoi-panel", AoiPanel).update_alerts(alert_areas)
            self.query_one("#feed", FeedWidget).update_feed(data.get("f", []), self._codemap)
            self.query_one("#threat-bar", ThreatBar).update_threats(data.get("i", ""))

        self._last_alert_count = len(alert_areas)

        # AOI bell check
        self._check_aoi_alerts(alert_areas)

    def _on_ping(self):
        """Called directly from SSE thread via call_from_thread."""
        self._update_connection_ui()
        self.query_one("#live-badge", LiveBadge).pulse()

    def _on_reload(self):
        """Called directly from SSE thread via call_from_thread."""
        try:
            header = self.query_one(HeaderBar)
            header.run_worker(header._check_version(), exclusive=False)
        except Exception:
            logger.exception("Error triggering upgrade check from reload")

    def _update_connection_ui(self):
        """Update badge and header based on current connection state."""
        is_offline = self._fe_stale or self._app_stale or self._sys_down
        badge = self.query_one("#live-badge", LiveBadge)
        if is_offline:
            badge.set_offline()
        # badge.pulse() is called separately by ping/snapshot handlers when online

    def _stale_check_tick(self):
        """Every 2s: detect dead stream (no data for 10s)."""
        if self._last_contact > 0:
            gap = time.time() - self._last_contact
            if gap > 10 and not self._fe_stale:
                self._fe_stale = True
                logger.warning("No SSE data for %.0fs, marking stale", gap)
                self._update_connection_ui()
            elif gap <= 10 and self._fe_stale and self._connected:
                self._fe_stale = False
                self._update_connection_ui()

    _cached_aoi_names: set[str] | None = None

    def _aoi_name_set(self) -> set[str]:
        if self._cached_aoi_names is not None:
            return self._cached_aoi_names
        names = set()
        for item in self._aoi_items:
            if item.get("type") == "district":
                for a in item.get("areas", []):
                    names.add(a)
            else:
                names.add(item["name"])
        self._cached_aoi_names = names
        return names

    def _invalidate_aoi_cache(self):
        self._cached_aoi_names = None

    def _sync_aoi_to_map(self):
        self.query_one("#map-panel", MapWidget).aoi_areas = self._aoi_name_set()

    def _check_aoi_alerts(self, alert_areas: dict[str, str]):
        aoi_names = self._aoi_name_set()

        current_hits = {name for name in alert_areas if name in aoi_names}
        new_hits = current_hits - self._prev_alert_names
        self._prev_alert_names = current_hits

        if new_hits:
            logger.info("AOI alert bell: %s", new_hits)
            self.bell()


    _highlight_timer = None

    def on_aoi_panel_highlight_changed(self, msg: AoiPanel.HighlightChanged):
        self.query_one("#map-panel", MapWidget).highlighted_aoi = msg.area_names
        if self._highlight_timer:
            self._highlight_timer.stop()
        if msg.area_names:
            self._highlight_timer = self.set_timer(3, self._clear_highlight)

    def _clear_highlight(self):
        self.query_one("#map-panel", MapWidget).highlighted_aoi = set()

    def on_aoi_panel_remove_requested(self, msg: AoiPanel.RemoveRequested):
        self._aoi_items = [i for i in self._aoi_items if i["name"] != msg.name]
        self._invalidate_aoi_cache()
        save_aoi(self._aoi_items, self._view_state)
        logger.info("AOI removed: %s", msg.name)
        self.query_one("#aoi-panel", AoiPanel).update_chips(self._aoi_items)
        self._sync_aoi_to_map()

    def on_feed_widget_card_selected(self, msg: FeedWidget.CardSelected):
        logger.debug("Opening feed detail for card t=%s s=%s", msg.card.get("t"), msg.card.get("s"))
        self.push_screen(FeedDetailScreen(msg.card, self._host, self._codemap))

    def action_blur_all(self):
        aoi_lv = self.query_one("#aoi-list", ListView)
        feed = self.query_one("#feed", FeedWidget)
        aoi_lv.index = None
        feed.index = None
        self.screen.set_focus(None)

    def action_focus_panel(self):
        aoi_lv = self.query_one("#aoi-list", ListView)
        feed = self.query_one("#feed", FeedWidget)
        aoi_visible = self.query_one("#aoi-panel").display

        if aoi_lv.has_focus and feed.display:
            feed.focus()
            if feed.index is None and feed._cards:
                feed.index = 0
        elif aoi_visible:
            aoi_lv.focus()
            if aoi_lv.index is None and self._aoi_items:
                aoi_lv.index = 0
        elif feed.display:
            feed.focus()
            if feed.index is None and feed._cards:
                feed.index = 0

    def _sync_right_panel(self):
        """Hide the right panel when both feed and AOI are off; recalc map width."""
        v = self._view_state
        show_right = v["feed"] or v["aoi"]
        right = self.query_one("#right-panel")
        container = self.query_one("#map-container")
        if show_right:
            right.remove_class("hidden")
            container.add_class("has-right")
        else:
            right.add_class("hidden")
            container.remove_class("has-right")
        self._update_map_width()

    def action_toggle_map(self):
        container = self.query_one("#map-container")
        container.display = self._view_state["map"] = not container.display
        save_aoi(self._aoi_items, self._view_state)

    def action_toggle_feed(self):
        feed = self.query_one("#feed")
        feed.display = self._view_state["feed"] = not feed.display
        save_aoi(self._aoi_items, self._view_state)
        self._sync_right_panel()

    def action_toggle_aoi(self):
        panel = self.query_one("#aoi-panel")
        panel.display = self._view_state["aoi"] = not panel.display
        save_aoi(self._aoi_items, self._view_state)
        self._sync_right_panel()

    def action_add_aoi(self):
        self.push_screen(AoiSearchScreen(self._host), callback=self._on_aoi_selected)

    def action_delete_aoi(self):
        if not self._aoi_items:
            return
        self.push_screen(AoiRemoveScreen(self._aoi_items), callback=self._on_aoi_removed)

    def _on_aoi_removed(self, name: str | None):
        if name is None:
            return
        self._aoi_items = [i for i in self._aoi_items if i["name"] != name]
        self._invalidate_aoi_cache()
        save_aoi(self._aoi_items, self._view_state)
        logger.info("AOI removed: %s", name)
        self.query_one("#aoi-panel", AoiPanel).update_chips(self._aoi_items)
        self._sync_aoi_to_map()

    def _on_aoi_selected(self, result: dict | None):
        if result is None:
            return
        name = result.get("name", "")
        if any(item["name"] == name for item in self._aoi_items):
            return
        self._aoi_items.append(result)
        self._invalidate_aoi_cache()
        save_aoi(self._aoi_items, self._view_state)
        logger.info("AOI added: %s", name)
        self.query_one("#aoi-panel", AoiPanel).update_chips(self._aoi_items)
        self._sync_aoi_to_map()

    def _shutdown_sse(self):
        self._sse_stop = True
        if self._sse_client:
            try:
                self._sse_client.close()
            except Exception:
                pass

    async def action_quit(self) -> None:
        self._shutdown_sse()
        self.workers.cancel_group(self, "sse")
        # Let Textual restore terminal, then force-exit the process
        # to avoid waiting for the blocked SSE thread
        def _force_exit():
            time.sleep(0.3)  # give Textual time to restore terminal
            os._exit(0)
        threading.Thread(target=_force_exit, daemon=True).start()
        self.exit()

    def on_unmount(self) -> None:
        self._shutdown_sse()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def app():
    default_host = os.environ.get("OREF_HOST", "https://oref.live")

    parser = argparse.ArgumentParser(description="oref.live terminal UI")
    parser.add_argument(
        "--host", "-H",
        default=default_host,
        help="Backend URL (default: $OREF_HOST or https://oref.live)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Write debug log to file",
    )
    parser.add_argument(
        "--no-orjson",
        action="store_true",
        help="Use stdlib json instead of orjson",
    )
    args = parser.parse_args()

    if args.no_orjson:
        global _use_orjson
        _use_orjson = False

    parsed = urlparse(args.host)
    if parsed.scheme not in ("http", "https"):
        print("Error: --host must start with http:// or https://", file=sys.stderr)
        sys.exit(1)
    if parsed.scheme == "http":
        hostname = parsed.hostname or ""
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            print(
                f"Warning: connecting to {args.host!r} over plain HTTP — traffic will not be encrypted.",
                file=sys.stderr,
            )

    App(host=args.host, debug=args.debug).run()


if __name__ == "__main__":
    app()

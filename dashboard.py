"""Separate-process LCARS dashboard for the AI co-host.

Reads data/status.json (written by the bot) and renders an authentic
Star Trek LCARS-style status panel with interactive pill buttons.
"""
import json
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

import config  # for STREAMER_NAME / titles only (read-only)

from PySide6.QtCore import Qt, QTimer, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QScrollArea,
    QSizePolicy,
)

# ---------------------------------------------------------------------------
# Authentic LCARS Palette (24th Century Standard)
# ---------------------------------------------------------------------------
BG          = QColor(0x00, 0x00, 0x00)       # Deep Space Black
PANEL       = QColor(0x05, 0x0B, 0x14)       # Subtle console dark background
ORANGE      = QColor(0xFF, 0x99, 0x00)       # Core LCARS Rust Orange
ORANGE_SOFT = QColor(0xFF, 0xCC, 0x66)       # LCARS Amber Gold
TEAL        = QColor(0x00, 0x99, 0xCC)       # Ice Teal
BLUE        = QColor(0x33, 0x66, 0xCC)       # Federation Blue
RED         = QColor(0xCC, 0x33, 0x33)       # Red Alert / Danger
GREEN       = QColor(0x33, 0xCC, 0x66)       # Sensor Green
PURPLE      = QColor(0xCC, 0x66, 0x99)       # LCARS Lavender / Pink
GREY        = QColor(0x66, 0x77, 0x88)       # Readout Dim Grey
TEXT        = QColor(0xFF, 0xFF, 0xFF)       # Pure White Text

STATUS_PATH = config.STATUS_FILE
CMD_PATH = config.DASHBOARD_CMD_FILE

FONT_TITLE = "Impact"      # Fits LCARS tall, condensed typography standard
FONT_MONO  = "Consolas"


def _make_font(size, bold=True, mono=False):
    f = QFont(FONT_MONO if mono else FONT_TITLE, size)
    f.setBold(bold)
    f.setLetterSpacing(QFont.AbsoluteSpacing, 1)
    return f


# ---------------------------------------------------------------------------
# LCARS Shape Helpers
# ---------------------------------------------------------------------------
def lcars_pill_path(x, y, w, h):
    """Generates a full capsule (stadium / pill) shape path."""
    path = QPainterPath()
    r = h / 2.0
    path.moveTo(x + r, y)
    path.lineTo(x + w - r, y)
    path.quadTo(x + w, y, x + w, y + r)
    path.quadTo(x + w, y + h, x + w - r, y + h)
    path.lineTo(x + r, y + h)
    path.quadTo(x, y + h, x, y + r)
    path.quadTo(x, y, x + r, y)
    path.closeSubpath()
    return path


class LcarsPillButton(QWidget):
    """Interactive LCARS Pill/Capsule button that updates background colors, 
    text color, and hover highlights depending on state."""

    clicked = Signal()

    def __init__(self, text="", active=False, parent=None):
        super().__init__(parent)
        self._text = text
        self._active = active
        self._hovered = False
        self.setFixedHeight(30)
        self.setMinimumWidth(140)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, text, active):
        self._text = text
        self._active = active
        self.update()

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Choose base color based on state
        if self._active:
            bg_color = ORANGE_SOFT if self._hovered else GREEN
            fg_color = QColor(0x00, 0x00, 0x00)  # Dark text on bright background
        else:
            bg_color = QColor(0xEE, 0x44, 0x44) if self._hovered else RED
            fg_color = TEXT                       # Bright text on red background

        # Draw full pill shape
        p.setPen(Qt.NoPen)
        p.setBrush(bg_color)
        p.drawPath(lcars_pill_path(0, 0, w, h))

        # Optional subtle border highlight on hover
        if self._hovered:
            p.setPen(QPen(TEXT, 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(lcars_pill_path(1, 1, w - 2, h - 2))

        # Render Pill Label Text
        p.setPen(fg_color)
        p.setFont(_make_font(11, bold=True, mono=True))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._text)


# ---------------------------------------------------------------------------
# Core LCARS Visual Components
# ---------------------------------------------------------------------------
class LcarsHeaderRail(QWidget):
    """Sweeping top-left frame rail with LCARS curves, title, and cap block."""

    def __init__(self, title="AI CO-HOST · COMMAND INTERFACE", parent=None):
        super().__init__(parent)
        self._title = title
        self.setFixedHeight(50)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, BG)

        # Draw left sweeping curve elbow (Top-Left LCARS Frame Anchor)
        p.setBrush(ORANGE)
        p.setPen(Qt.NoPen)
        
        path = QPainterPath()
        path.moveTo(0, h)
        path.lineTo(0, 20)
        path.quadTo(0, 0, 20, 0)
        path.lineTo(180, 0)
        path.lineTo(180, 16)
        path.lineTo(35, 16)
        path.quadTo(16, 16, 16, 35)
        path.lineTo(16, h)
        path.closeSubpath()
        p.drawPath(path)

        # Top Horizontal Bar Block
        p.fillRect(186, 0, w - 300, 16, ORANGE_SOFT)

        # Right End Pill Cap
        p.drawPath(lcars_pill_path(w - 100, 0, 100, 16))

        # Header Text inside LCARS frame alignment
        p.setFont(_make_font(18, bold=True))
        p.setPen(ORANGE)
        p.drawText(QRectF(30, 18, w - 100, 30), Qt.AlignLeft | Qt.AlignVCenter, self._title)


class LcarsElbowPanel(QFrame):
    """Authentic LCARS Panel frame featuring a left-side elbow column and top header."""

    def __init__(self, title, accent_color=ORANGE, parent=None):
        super().__init__(parent)
        self._title = title
        self._color = accent_color
        self.setStyleSheet(f"background:{PANEL.name()}; border:0;")
        self.setMinimumWidth(180)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Background fill
        p.fillRect(0, 0, w, h, PANEL)

        # Draw LCARS Left Column & Elbow
        p.setBrush(self._color)
        p.setPen(Qt.NoPen)

        # Elbow Top Bar
        p.fillRect(16, 0, w - 16, 8, self._color)
        
        # Left Vertical Rail Block
        p.fillRect(0, 0, 12, h, self._color)

        # Decorative cutout line under header
        p.setPen(QPen(BG, 2))
        p.drawLine(12, 28, w, 28)

        # Header Title
        p.setPen(self._color)
        p.setFont(_make_font(13, bold=True, mono=True))
        p.drawText(QRectF(20, 8, w - 30, 20), Qt.AlignLeft | Qt.AlignVCenter, self._title)


class Pill(QWidget):
    """Classic LCARS Stadium Pill Label with solid cap and background."""

    def __init__(self, text="", color=ORANGE, size=11, bold=True, width=0, mono=True, parent=None):
        super().__init__(parent)
        self._text = text
        self._color = color
        self._size = size
        self._bold = bold
        self._mono = mono
        self.setFixedHeight(24)
        if width:
            self.setFixedWidth(width)
        else:
            self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_text(self, text):
        self._text = text
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Draw full pill capsule background
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawPath(lcars_pill_path(0, 0, w, h))

        # Text inside pill
        p.setPen(QColor(0x00, 0x00, 0x00))
        p.setFont(_make_font(self._size, self._bold, mono=self._mono))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._text)


class ChannelToggle(QWidget):
    """Clickable platform toggle (LCARS light + ONLINE/OFFLINE capsule).
    Clicking emits clicked(); the dashboard decides what command to send."""

    clicked = Signal()

    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self._on = None
        self._hovered = False
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

    def set_state(self, on):
        self._on = bool(on)
        self.update()

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.setFont(_make_font(11, bold=True, mono=True))
        p.setPen(TEXT)
        p.drawText(QRectF(0, 0, w - 90, h), Qt.AlignVCenter | Qt.AlignLeft, self._label)

        if self._hovered:
            p.setPen(QPen(TEXT, 2))
        else:
            p.setPen(Qt.NoPen)
        p.setBrush(Qt.NoBrush)

        if self._on is None:
            state, color = "OFFLINE", GREY
        elif self._on:
            state, color = "ONLINE", GREEN
        else:
            state, color = "OFFLINE", RED

        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(lcars_pill_path(w - 75, 4, 75, h - 8))

        p.setPen(QColor(0x00, 0x00, 0x00))
        p.setFont(_make_font(10, bold=True, mono=True))
        p.drawText(QRectF(w - 75, 4, 75, h - 8), Qt.AlignCenter, state)


class StatusLight(QWidget):
    """Platform status indicator with an LCARS capsule light state."""

    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self._on = None
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, on):
        self._on = bool(on)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.setFont(_make_font(11, bold=True, mono=True))
        p.setPen(TEXT)
        p.drawText(QRectF(0, 0, w - 85, h), Qt.AlignVCenter | Qt.AlignLeft, self._label)

        if self._on is None:
            state, color = "OFFLINE", GREY
        elif self._on:
            state, color = "ONLINE", GREEN
        else:
            state, color = "OFFLINE", RED

        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(lcars_pill_path(w - 75, 4, 75, h - 8))

        p.setPen(QColor(0x00, 0x00, 0x00))
        p.setFont(_make_font(10, bold=True, mono=True))
        p.drawText(QRectF(w - 75, 4, 75, h - 8), Qt.AlignCenter, state)


class Meter(QWidget):
    """LCARS horizontal segment quota bar."""

    def __init__(self, label="", parent=None):
        super().__init__(parent)
        self._label = label
        self._frac = 0.0
        self._sub = ""
        self._color = ORANGE
        self.setMinimumHeight(42)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, used, limit, remaining):
        frac = 0.0 if not limit else max(0.0, min(1.0, used / float(limit)))
        self._frac = frac
        pct = int(frac * 100)
        if pct >= 90:
            self._color = RED
        elif pct >= 70:
            self._color = ORANGE_SOFT
        else:
            self._color = TEAL
        if remaining is not None:
            self._sub = f"{remaining} REM"
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()

        p.setFont(_make_font(10, bold=True, mono=True))
        p.setPen(TEXT)
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignLeft, self._label)
        p.setPen(GREY)
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignRight, self._sub)

        # Bar track
        by = 18
        bh = 14
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0x15, 0x20, 0x30))
        p.drawPath(lcars_pill_path(0, by, w, bh))

        # Value Segment Fill
        if self._frac > 0:
            fw = max(bh, int(w * self._frac))
            p.setBrush(self._color)
            p.drawPath(lcars_pill_path(0, by, fw, bh))


class RotationRing(QWidget):
    """LCARS segmented channel sequence status bar. Each segment is a clickable
    pill: clicking one asks the bot to switch to that comm channel."""

    clicked = Signal(int)  # 0-based index into the rotation (pill label = i+1)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._current = ""
        self._usage = []
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_rotation(self, items, current):
        self._items = list(items)
        self._current = current
        self.update()

    def set_usage(self, usage):
        self._usage = list(usage or [])
        self.update()

    def _hit_index(self, pos):
        n = len(self._items)
        if not n:
            return -1
        x, y = pos.x(), pos.y()
        w = self.width()
        cols_row1 = 3
        cols_row2 = 3
        seg_w1 = (w - ((cols_row1 - 1) * 4)) / cols_row1
        seg_w2 = (w - ((cols_row2 - 1) * 4)) / cols_row2
        row_height = 36
        for i in range(n):
            if i < 3:
                row, col, seg_w = 0, i, seg_w1
            else:
                row, col, seg_w = 1, i - 3, seg_w2
            px = col * (seg_w + 4)
            py = row * row_height
            if px <= x < px + seg_w and py <= y < py + row_height:
                return i
        return -1

    def mouseMoveEvent(self, e):
        i = self._hit_index(e.position().toPoint())
        if i != self._hover:
            self._hover = i
            self.update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        self._hover = -1
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        i = self._hit_index(e.position().toPoint())
        if i >= 0:
            self.clicked.emit(i)
        super().mouseReleaseEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._items)

        if not n:
            p.setPen(GREY)
            p.setFont(_make_font(10, bold=True, mono=True))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignVCenter | Qt.AlignLeft, "STANDBY — ROTATION INACTIVE")
            return

        cols_row1 = 3
        cols_row2 = 3
        seg_w1 = (w - ((cols_row1 - 1) * 4)) / cols_row1
        seg_w2 = (w - ((cols_row2 - 1) * 4)) / cols_row2
        
        pill_h = 18
        row_height = 36

        for i, item in enumerate(self._items):
            if i < 3:
                row = 0
                col = i
                seg_w = seg_w1
            else:
                row = 1
                col = i - 3
                seg_w = seg_w2

            x = col * (seg_w + 4)
            y = row * row_height

            is_cur = (
                (item == self._current)
                or (bool(self._current) and item == f"{self._current} (local)")
                or (self._current == "" and i == 0)
            )

            remaining = None
            if i < len(self._usage):
                remaining = self._usage[i].get("remaining")

            depleted = remaining == 0
            color = ORANGE if (is_cur and not depleted) else (
                QColor(0x3A, 0x24, 0x1A) if depleted else QColor(0x15, 0x25, 0x35)
            )

            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawPath(lcars_pill_path(x, y + 2, seg_w, pill_h))

            p.setPen(QColor(0x00, 0x00, 0x00) if is_cur else GREY)
            p.setFont(_make_font(10, bold=True, mono=True))
            p.drawText(QRectF(x, y + 2, seg_w, pill_h), Qt.AlignCenter, str(i + 1))

            if i == self._hover:
                p.setPen(QPen(TEXT, 2))
                p.setBrush(Qt.NoBrush)
                p.drawPath(lcars_pill_path(x + 1, y + 3, seg_w - 2, pill_h - 2))

            if remaining is not None:
                p.setFont(_make_font(9, bold=True, mono=True))
                p.setPen(RED if remaining == 0 else (GREEN if remaining <= 3 else GREY))
                p.drawText(QRectF(x, y + pill_h + 2, seg_w, 14), Qt.AlignCenter, str(remaining))
            elif i < len(self._usage) and self._usage[i].get("limit"):
                p.setFont(_make_font(9, bold=True, mono=True))
                p.setPen(GREY)
                p.drawText(QRectF(x, y + pill_h + 2, seg_w, 14), Qt.AlignCenter, "—")


class LatencyReadout(QWidget):
    """LCARS pipeline response latency diagnostics display."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = None
        self._brain = None
        self._tts = None
        self._play = None
        self._avg = None
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_latency(self, total, brain, tts, play, avg):
        self._total = total
        self._brain = brain
        self._tts = tts
        self._play = play
        self._avg = avg
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()

        total_sec = (self._total / 1000.0) if self._total else 0.0
        color = GREEN if total_sec < 2.0 else (ORANGE_SOFT if total_sec < 5.0 else RED)

        p.setFont(_make_font(10, bold=True, mono=True))
        p.setPen(TEXT)
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignLeft, "RESPONSE DELAY")
        
        p.setPen(color)
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignRight, f"{total_sec:.2f}s" if self._total else "—")

        def _fmt(v):
            return "—" if v is None else f"{v / 1000.0:.2f}"

        sub = f"B:{_fmt(self._brain)}s  T:{_fmt(self._tts)}s  P:{_fmt(self._play)}s"
        p.setFont(_make_font(9, bold=True, mono=True))
        p.setPen(GREY)
        p.drawText(QRectF(0, 18, w, 14), Qt.AlignLeft, sub)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0x15, 0x20, 0x30))
        p.drawPath(lcars_pill_path(0, 34, w, 8))
        if self._total:
            p.setBrush(color)
            p.drawPath(lcars_pill_path(0, 34, max(12, int(w * min(1.0, total_sec / 6.0))), 8))


# ---------------------------------------------------------------------------
# Main LCARS Interface Dashboard
# ---------------------------------------------------------------------------
class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self._last_activity = None
        self._last = {}
        self.setWindowTitle(f"{config.BOT_NAME} — LCARS COMMAND INTERFACE")
        self.setWindowFlags(Qt.Window)
        self.resize(1200, 780)
        self.setStyleSheet(f"background:{BG.name()}; color:{TEXT.name()};")

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(500)
        self._poll()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(10)

        # Top LCARS Frame Curve Header
        self.header = LcarsHeaderRail(
            f"{config.BOT_NAME.upper()} · SYSTEM DIAGNOSTICS · Beta v1.0")
        root.addWidget(self.header)

        # Content Frame Layout
        content = QHBoxLayout()
        content.setSpacing(10)
        root.addLayout(content, 1)

        # Left Grid Panel (Brain / Platforms / Sensors / Power)
        left_grid = QVBoxLayout()
        left_grid.setSpacing(10)
        content.addLayout(left_grid, 1)

        top_panels = QHBoxLayout()
        top_panels.setSpacing(10)
        left_grid.addLayout(top_panels)

        # BRAIN PANEL
        self.brain = LcarsElbowPanel("PRIMARY BRAIN CORE", ORANGE)
        bl = QVBoxLayout(self.brain)
        bl.setContentsMargins(20, 36, 12, 12)
        bl.setSpacing(6)

        self.model_label = QLabel("—")
        self.model_label.setFont(_make_font(18, bold=True, mono=True))
        self.model_label.setStyleSheet("color:#FFCC66; background:transparent;")
        bl.addWidget(self.model_label)

        self.badge = Pill("OFFLINE", ORANGE, 10)
        bl.addWidget(self.badge)
        self.ring = RotationRing()
        self.ring.clicked.connect(self._switch_channel)
        self.ring.setToolTip("Click a channel to switch the active comm frequency")
        bl.addWidget(self.ring)
        self.latency = LatencyReadout()
        bl.addWidget(self.latency)
        top_panels.addWidget(self.brain, 1)

        # POWER / BUDGET PANEL
        self.power = LcarsElbowPanel("POWER & ALLOCATION", TEAL)
        powl = QVBoxLayout(self.power)
        powl.setContentsMargins(20, 36, 12, 12)
        powl.setSpacing(6)

        self.token_meter = Meter("GEMINI TOKENS")
        powl.addWidget(self.token_meter)

        self.reset_label = QLabel("RESET: —")
        self.reset_label.setFont(_make_font(10, bold=True, mono=True))
        self.reset_label.setStyleSheet("color:#0099CC; background:transparent;")
        powl.addWidget(self.reset_label)
        top_panels.addWidget(self.power, 1)

        # PROPULSION / PLATFORMS PANEL
        self.plat = LcarsElbowPanel("COMMS & PLATFORMS", BLUE)
        pll = QVBoxLayout(self.plat)
        pll.setContentsMargins(20, 36, 12, 12)
        pll.setSpacing(4)

        self.l_twitch = ChannelToggle("TWITCH CHAT")
        self.l_twitch.setToolTip("Click to turn Twitch chat on/off")
        self.l_twitch.clicked.connect(self._toggle_twitch_chat)
        pll.addWidget(self.l_twitch)
        self.l_twitch_live = ChannelToggle("TWITCH LIVE")
        self.l_twitch_live.setToolTip("Click to turn Twitch LIVE tracking on/off")
        self.l_twitch_live.clicked.connect(self._toggle_twitch_live)
        pll.addWidget(self.l_twitch_live)

        # Optional integrations (Kick / Discord / OBS). Status comes from the
        # status feed; clicking a connected pill toggles that integration.
        self.l_kick = LcarsPillButton("KICK · OFFLINE", active=False)
        self.l_kick.setToolTip("Kick chat integration")
        self.l_kick.clicked.connect(self._toggle_kick_chat)
        pll.addWidget(self.l_kick)
        self.l_discord = LcarsPillButton("DISCORD · OFFLINE", active=False)
        self.l_discord.setToolTip("Discord bot integration")
        self.l_discord.clicked.connect(self._toggle_discord_chat)
        pll.addWidget(self.l_discord)
        self.l_obs = LcarsPillButton("OBS · OFFLINE", active=False)
        self.l_obs.setToolTip("OBS WebSocket overlay")
        self.l_obs.clicked.connect(self._toggle_obs)
        pll.addWidget(self.l_obs)
        left_grid.addWidget(self.plat)

        # Right Grid Panel (Transmission / Activity Log)
        right_grid = QVBoxLayout()
        right_grid.setSpacing(10)
        content.addLayout(right_grid, 1)

        # SENSORS PANEL
        self.sensor = LcarsElbowPanel("AUDIO SENSORS", GREEN)
        sl = QVBoxLayout(self.sensor)
        sl.setContentsMargins(20, 36, 12, 12)
        sl.setSpacing(8)

        # EARS Interactive LCARS Pill Button
        self.ears_pill = LcarsPillButton("EARS: OFF", active=False)
        self.ears_pill.setToolTip("Click to toggle passive listening (EARS ON/OFF)")
        self.ears_pill.clicked.connect(self._toggle_ears)
        sl.addWidget(self.ears_pill)

        self.vc_label = QLabel("TTS: —")
        self.vc_label.setFont(_make_font(11, bold=True, mono=True))
        self.vc_label.setStyleSheet("color:#33CC66; background:transparent;")
        sl.addWidget(self.vc_label)

        # VOICE Interactive LCARS Pill Button
        self.voice_pill = LcarsPillButton("VOICE: OFF", active=False)
        self.voice_pill.setToolTip("Click to toggle vocals (VOICE ON/OFF, silent mode)")
        self.voice_pill.clicked.connect(self._toggle_voice)
        sl.addWidget(self.voice_pill)

        self.wake_label = QLabel("WAKE WORDS: —")
        self.wake_label.setFont(_make_font(10, bold=True, mono=True))
        self.wake_label.setStyleSheet("color:#667788; background:transparent;")
        self.wake_label.setWordWrap(True)
        sl.addWidget(self.wake_label)
        right_grid.addWidget(self.sensor)

        # LATEST TRANSMISSION PANEL
        self.trans = LcarsElbowPanel("SUBSPACE TRANSMISSION", PURPLE)
        tl = QVBoxLayout(self.trans)
        tl.setContentsMargins(20, 36, 12, 12)
        self.trans_label = QLabel("— NO SIGNAL —")
        self.trans_label.setWordWrap(True)
        self.trans_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.trans_label.setFont(_make_font(12, bold=True))
        self.trans_label.setStyleSheet("color:#CC6699; background:transparent;")
        tl.addWidget(self.trans_label)
        right_grid.addWidget(self.trans)

        # SYSTEM SETUP — identity (BOT_NAME / STREAMER_NAME) writer for .env
        self.btn_setup = LcarsPillButton("SETUP · BOT IDENTITY", active=False)
        self.btn_setup.setToolTip("Edit BOT_NAME / STREAMER_NAME and write them to .env, then restart start_bot.bat")
        self.btn_setup.clicked.connect(self._launch_setup_gui)
        right_grid.addWidget(self.btn_setup)

        # ACTIVITY LOG PANEL
        self.act = LcarsElbowPanel("SYSTEM LOG", ORANGE_SOFT)
        al = QVBoxLayout(self.act)
        al.setContentsMargins(20, 36, 12, 12)

        self.log_scroll = QScrollArea()
        self.log_scroll.setWidgetResizable(True)
        self.log_scroll.setStyleSheet("background:transparent; border:0;")
        
        self.log_box = QVBoxLayout()
        self.log_box.setContentsMargins(0, 0, 0, 0)
        self.log_box.setSpacing(2)

        loghost = QWidget()
        loghost.setStyleSheet("background:transparent;")
        loghost.setLayout(self.log_box)
        self.log_scroll.setWidget(loghost)
        al.addWidget(self.log_scroll)
        right_grid.addWidget(self.act, 1)

        # Bottom Ticker Footer
        self.ticker = QLabel("STARDATE — NO SIGNAL")
        self.ticker.setStyleSheet("color:#FF9900; background:transparent;")
        self.ticker.setFont(_make_font(11, bold=True, mono=True))
        root.addWidget(self.ticker)

    # -- Polling & File Reader -------------------------------------------
    def _read_status(self):
        try:
            with open(STATUS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    # -- Partly interactive: send a command to the bot --------------------
    def _send_command(self, cmd):
        try:
            tmp = CMD_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"cmd": cmd, "ts": int(time.time())}, f)
            os.replace(tmp, CMD_PATH)
        except OSError as e:
            print(f"Dashboard: could not send '{cmd}': {e}")

    def _toggle_ears(self):
        d = self._last or {}
        active = ((d.get("ears_state") or "off").lower() in ("running", "starting"))
        self._send_command("!ears off" if active else "!ears on")

    def _toggle_voice(self):
        d = self._last or {}
        self._send_command("!voice on" if d.get("voice_mute") else "!voice off")

    def _toggle_twitch_chat(self):
        m = (self._last or {}).get("chat_mutes") or {}
        self._send_command("!mute twitch-chat off" if m.get("twitch") else "!mute twitch-chat on")

    def _toggle_twitch_live(self):
        tracking = (self._last or {}).get("twitch_live_tracking", True)
        self._send_command("!mute twitch-live on" if tracking else "!mute twitch-live off")

    def _toggle_kick_chat(self):
        # Kick/Discord/OBS are enabled via .env + restarting start_bot.bat.
        # The pill reflects live status from the status feed.
        print("[yellow]Kick/Discord/OBS toggles require editing .env and restarting start_bot.bat.")

    def _toggle_discord_chat(self):
        print("[yellow]Kick/Discord/OBS toggles require editing .env and restarting start_bot.bat.")

    def _toggle_obs(self):
        print("[yellow]Kick/Discord/OBS toggles require editing .env and restarting start_bot.bat.")

    def _integrate_not_available(self):
        """Kept for backward compatibility; now a no-op."""
        pass

    def _switch_channel(self, index):
        self._send_command(f"!freq {index + 1}")

    def _launch_setup_gui(self):
        """Launch the identity setup GUI (setup_gui.py) as a separate process."""
        here = os.path.dirname(os.path.abspath(__file__))
        gui = os.path.join(here, "setup_gui.py")
        # Prefer the venv python next to dashboard.bat if present, else sys.executable.
        venv_py = os.path.join(here, ".venv", "Scripts", "python.exe")
        py = venv_py if os.path.exists(venv_py) else sys.executable
        try:
            subprocess.Popen([py, gui], cwd=here, close_fds=True)
        except Exception as e:
            print(f"Dashboard: could not launch setup GUI: {e}")

    def _poll(self):
        data = self._read_status()
        if not data:
            self._show_offline()
            return
        self._render(data)

    def _show_offline(self):
        self.ticker.setText("STARDATE — NO SIGNAL · BOT OFFLINE · RUN start_bot.bat")
        self.model_label.setText("OFFLINE")
        self.badge.set_text("DISCONNECTED")
        self.trans_label.setText("— Subspace channel offline —")
        self.latency.set_latency(None, None, None, None, None)
        self.l_twitch.set_state(False)
        self.l_twitch_live.set_state(False)
        self.l_kick.set_state("KICK · OFFLINE", active=False)
        self.l_discord.set_state("DISCORD · OFFLINE", active=False)
        self.l_obs.set_state("OBS · OFFLINE", active=False)
        self.ears_pill.set_state("EARS: OFFLINE", active=False)
        self.voice_pill.set_state("VOICE: OFFLINE", active=False)

    def _reset_countdown(self, d):
        epoch = d.get("reset_epoch")
        if not epoch:
            t = d.get("reset_time")
            return f"RESET: {t or '—'}"
        remaining = max(0, int(epoch) - int(time.time()))
        h, rem = divmod(remaining, 3600)
        m, s = divmod(rem, 60)
        return f"RESET {h:02d}:{m:02d}:{s:02d}"

    def _render(self, d):
        # Brain
        model = d.get("model") or ""
        badge = d.get("model_badge") or ""
        is_local = bool(d.get("is_local"))
        local = "LOCAL" if is_local else "GEMINI"
        self.model_label.setText(model or "—")
        self.badge.set_text(f"{local} · {badge}" if badge else local)
        self.badge._color = TEAL if is_local else ORANGE
        self.badge.update()
        self.ring.set_rotation(d.get("rotation") or [], model)
        self.ring.set_usage(d.get("channel_rpd") or [])

        # Pipeline Latency
        lat = d.get("latency") or {}
        self.latency.set_latency(
            lat.get("total_ms"), lat.get("brain_ms"),
            lat.get("tts_ms"), lat.get("play_ms"), lat.get("avg_ms"),
        )

        # Power/Budget
        self.token_meter.set_value(
            d.get("tokens_used") or 0, d.get("token_limit") or 0, d.get("tokens_remaining"))
        self.reset_label.setText(self._reset_countdown(d))

        # Platforms
        mutes = d.get("chat_mutes") or {}
        self.l_twitch.set_state(False if mutes.get("twitch") else bool(d.get("twitch_connected")))
        self.l_twitch_live.set_state(
            False if not d.get("twitch_live_tracking") else d.get("twitch_live"))
        # Optional integrations — pill shows live status from the status feed.
        kick_on = bool(d.get("kick_enabled", False))
        self.l_kick.set_state("KICK · ONLINE" if kick_on else "KICK · OFFLINE", active=kick_on)
        discord_on = bool(d.get("discord_connected", False))
        self.l_discord.set_state("DISCORD · ONLINE" if discord_on else "DISCORD · OFFLINE", active=discord_on)
        obs_on = bool(d.get("obs_enabled", False))
        self.l_obs.set_state("OBS · ONLINE" if obs_on else "OBS · OFFLINE", active=obs_on)

        # Sensors
        self._last = d
        ears = (d.get("ears_state") or "off").lower()
        ears_active = ears in ("running", "starting")
        self.ears_pill.set_state(f"EARS: {'ON' if ears_active else 'OFF'}", active=ears_active)

        self.vc_label.setText(f"TTS: {(d.get('tts_engine') or '—').upper()}")

        muted = bool(d.get("voice_mute"))
        voice_active = not muted
        self.voice_pill.set_state(f"VOICE: {'ACTIVE' if voice_active else 'MUTED'}", active=voice_active)

        self.wake_label.setText("WAKE WORDS: " + (", ".join(d.get("wake_words") or []) or "—"))

        # Latest Transmission
        trans = d.get("latest_transmission") or ""
        self.trans_label.setText(trans or "— awaiting transmission —")

        # Activity Log
        activity = d.get("activity") or []
        if activity and activity != self._last_activity:
            self._last_activity = activity
            while self.log_box.count():
                item = self.log_box.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
            for entry in reversed(activity[-30:]):
                lbl = QLabel(f"{entry.get('ts','')} {entry.get('text','')}")
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color:#FFCC66; background:transparent;")
                lbl.setFont(_make_font(10, bold=True, mono=True))
                self.log_box.addWidget(lbl)
            self.log_box.addStretch(1)

        # Ticker
        up = d.get("uptime_seconds") or 0
        h, rem = divmod(int(up), 3600)
        m, s = divmod(rem, 60)
        ooc = d.get("ooc") or {}
        state = "MODE: OOC" if ooc.get("active") else "MODE: CHARACTER"
        self.ticker.setText(
            f"SYSTEM ACTIVE · UPTIME {h:02d}:{m:02d}:{s:02d} · "
            f"CORE ACTIVE: {model or '—'} · {state}"
        )


def main():
    app = QApplication(sys.argv)
    w = Dashboard()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
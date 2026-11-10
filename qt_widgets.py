# -*- coding: utf-8 -*-
"""WIP TRACKER (Qt) - the shared building blocks every page uses.

  Ctx            the one object every page talks to (database, user, hooks)
  NavRail        the narrow page menu on the left (icon + short name)
  Card / ModuleCard   a card; a module card can be opened in its own window
  Seg / Toggle   segmented choice and on/off buttons
  FlowLayout     a layout that wraps its children onto new lines
  PartChips      the part picker (one part, or any number + ALL PARTS)
  FilterBar      the filter strip: chips that open a drawer over the page
  DataTable      a fast table with frozen left columns, row colours, menus
"""

import datetime
import math

from PySide6.QtCore import (Qt, Signal, QAbstractTableModel, QModelIndex, QRect, QRectF,
                            QSize, QPoint, QPointF, QTimer, QEvent, QEventLoop)
from PySide6.QtGui import (QColor, QPainter, QPen, QFont, QBrush, QPolygonF, QFontMetrics,
                           QPainterPath)
from PySide6.QtWidgets import (QToolTip, QSplitter, QWidget, QFrame, QLabel, QPushButton,
                               QHBoxLayout, QVBoxLayout, QLayout, QSizePolicy, QComboBox, QSpinBox,
                               QLineEdit, QDialog, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QTableView, QHeaderView, QAbstractItemView, QMenu, QApplication,
                               QMessageBox, QStyledItemDelegate, QScrollArea, QGridLayout,
                               QFileDialog, QCalendarWidget, QAbstractScrollArea, QBoxLayout)

from wip_core import (ALL, MONTH_NAMES, iso_year_week, week_offset, norm_text,
                      parse_date)
import qt_theme

# Heat maps: the original green - amber - red, a pile-up far above the rest
# in purple (the same rules as the Tk program's heat_color / heat_scale).
OUTLIER_COLOR = "#5D06A9"


def heat_color(t):
    """v69 "level 9" scale: hue 138 (green) -> 4 (red) at 93% saturation,
    34.4% lightness - flat, deep enough for white text on every step."""
    t = max(0.0, min(1.0, t))
    h, sat, li = 138.0 - 134.0 * t, 0.93, 0.344
    c = (1 - abs(2 * li - 1)) * sat
    hp = h / 60.0
    x = c * (1 - abs(hp % 2 - 1))
    m = li - c / 2
    if hp < 1:
        r, g, b = c, x, 0.0
    elif hp < 2:
        r, g, b = x, c, 0.0
    else:
        r, g, b = 0.0, c, x
    return "#%02X%02X%02X" % tuple(int(round((v + m) * 255)) for v in (r, g, b))


def heat_scale(values, outlier_factor=4.0):
    nonzero = sorted(v for v in values if v)
    if not nonzero:
        return 0, 0, None
    mid = len(nonzero) // 2
    median = nonzero[mid] if len(nonzero) % 2 else (nonzero[mid - 1] + nonzero[mid]) / 2.0
    threshold = median * outlier_factor
    normal = [v for v in nonzero if v <= threshold]
    if not normal or len(normal) == len(nonzero):
        return nonzero[0], nonzero[-1], None
    return min(normal), max(normal), threshold


FLAG_KINDS = ("teco", "mrb", "mrbent", "scrap", "removed", "repair", "note", "fixed")
FLAG_KIND_TITLE = {"teco": "TECO", "mrb": "MRB", "mrbent": "MRB ENTERED",
                   "scrap": "SCRAP", "removed": "REMOVED", "repair": "REPAIR",
                   "note": "NOTE", "fixed": "FIXED"}


# =====================================================================
#  CONTEXT
# =====================================================================
class Ctx:
    """What every page shares.  Filled in by qt_app."""

    def __init__(self):
        self.db = None
        self.prefs = {}
        self.wf_an = None
        self.hooks = {}
        self.user = None
        self.session_id = None
        self.TH = qt_theme.theme("night")
        self.selected_serial = ""
        self.show_all_notes = False
        self.tables = []
        self.window = None
        self.status = lambda msg: None
        self.loading = None           # LoadingOverlay over the pages (qt_app)
        self.sheet = ""               # the theme's style sheet now (qt_app.apply_theme)
        self._busy_depth = 0

    def username(self):
        return (self.user or {}).get("username") or ""

    def is_admin(self):
        from wip_core import ADMIN_ROLES
        return bool(self.user) and self.user.get("role") in ADMIN_ROLES

    def home_engineer(self):
        """v72: the engineer every filter starts on for this account (its
        own name in the PART list), or None = ALL."""
        from wip_core import engineer_for_user
        if self.db is None or not self.user:
            return None
        key = (self.username(), self.db.data_version())
        hit = self.__dict__.get("_home_eng")
        if hit is None or hit[0] != key:
            try:
                eng = engineer_for_user(self.user, self.db.engineers())
            except Exception:
                eng = None
            hit = (key, eng)
            self._home_eng = hit
        return hit[1]

    def flag_text(self, kind):
        return self.TH["flag_text"].get(kind, self.TH["text"])

    def set_selected_serial(self, serial_no):
        key = norm_text(serial_no or "")
        self.selected_serial = key
        for t in list(self.tables):
            try:
                t.refresh_selection()
            except RuntimeError:
                self.tables.remove(t)

    def toggle_selected_serial(self, serial_no):
        key = norm_text(serial_no or "")
        self.set_selected_serial("" if key == self.selected_serial else key)

    def busy(self, on, message=""):
        """v71: counted - a page's own busy() inside the window's "Opening
        ..." no longer switches the indicator off half way - and shown as
        a loading card over the page, not only as the mouse pointer."""
        if on:
            self._busy_depth += 1
            if self._busy_depth == 1:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                self._busy_msg = message
            if message:
                self.status(message)
            if self.loading is not None:
                self.loading.show_msg(message or "Loading...")
            # draw it now (but do not take clicks while busy)
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        else:
            self._busy_depth = max(0, self._busy_depth - 1)
            if self._busy_depth:
                return
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            if self.loading is not None:
                self.loading.hide()
            # a page that wrote no status of its own leaves "Ready."
            lbl = getattr(self.status, "__self__", None)
            if getattr(self, "_busy_msg", "") and lbl is not None and lbl.text() == self._busy_msg:
                self.status("Ready.")
            self._busy_msg = ""


ctx = Ctx()


class LoadingOverlay(QWidget):
    """v71: "Loading..." over the page area while a page opens or
    recalculates - the program is working, not stuck."""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.msg = ""
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        host.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, ev):
        if obj is self.host and ev.type() == QEvent.Resize:
            self.setGeometry(self.host.rect())
        return False

    def show_msg(self, msg):
        self.msg = msg
        self.setGeometry(self.host.rect())
        self.raise_()
        self.show()
        self.repaint()

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        night = th["name"] != "day"
        p.fillRect(self.rect(), QColor(0, 0, 0, 70) if night else QColor(255, 255, 255, 110))
        f = QFont(qt_theme.FONT)
        f.setPointSizeF(11)
        f.setBold(True)
        fm = QFontMetrics(f)
        text = self.msg or "Loading..."
        w = max(260, fm.horizontalAdvance(text) + 90)
        h = 64
        r = QRectF((self.width() - w) / 2, (self.height() - h) / 2 - 40, w, h)
        p.setPen(QPen(QColor(th["border"]), 1))
        p.setBrush(QColor(th["card"]))
        p.drawRoundedRect(r, 14, 14)
        # a ring: the working sign (it cannot spin while the work runs)
        c = QPointF(r.left() + 34, r.center().y())
        p.setPen(QPen(QColor(th["dim"]), 4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 12, 12)
        p.setPen(QPen(QColor(th["accent"]), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(c.x() - 12, c.y() - 12, 24, 24), 90 * 16, -110 * 16)
        p.setFont(f)
        p.setPen(QColor(th["text"]))
        p.drawText(QRectF(r.left() + 58, r.top(), r.width() - 70, h),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()


# =====================================================================
#  SMALL HELPERS
# =====================================================================
def hbox(*widgets, spacing=8, margins=(0, 0, 0, 0), stretch_last=False):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        elif isinstance(x, int):
            lay.addSpacing(x)
        else:
            lay.addWidget(x)
    if stretch_last:
        lay.addStretch(1)
    return w


def label(text="", muted=False, section=False, obj=None, wrap=False):
    lb = QLabel(text)
    if muted:
        lb.setProperty("muted", True)
    if section:
        lb.setProperty("section", True)
    if obj:
        lb.setObjectName(obj)
    if wrap:
        lb.setWordWrap(True)
    return lb


def button(text, primary=False, danger=False, tip=None, slot=None, icon_only=False):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    if primary:
        b.setProperty("primary", True)
    if danger:
        b.setProperty("danger", True)
    if icon_only:
        b.setProperty("icon", True)
    if tip:
        b.setToolTip(tip)
    if slot:
        b.clicked.connect(lambda _=False: slot())
    return b


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


class Toggle(QPushButton):
    """An on/off button with a tick box drawn at its left."""

    PAD = "      "

    def __init__(self, text, checked=False, slot=None):
        super().__init__(self.PAD + text)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setProperty("toggle", True)
        self.setCursor(Qt.PointingHandCursor)
        if slot:
            self.toggled.connect(lambda _v: slot())

    def sizeHint(self):
        # lit, the label turns bold: leave room for that so it is not cut
        s = super().sizeHint()
        f = QFont(self.font()); f.setBold(True)
        extra = QFontMetrics(f).horizontalAdvance(self.text()) - \
            QFontMetrics(self.font()).horizontalAdvance(self.text())
        return QSize(s.width() + max(0, extra) + 14, s.height())

    def minimumSizeHint(self):
        return self.sizeHint()

    def get(self):
        return self.isChecked()

    def set(self, v):
        self.setChecked(bool(v))

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRect(10, (self.height() - 13) // 2, 13, 13)
        th = ctx.TH
        if self.isChecked():
            p.setBrush(QColor(th["accent"]))
            p.setPen(QPen(QColor(th["accent"]), 1.5))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawLine(r.left() + 3, r.center().y(), r.left() + 6, r.bottom() - 3)
            p.drawLine(r.left() + 6, r.bottom() - 3, r.right() - 2, r.top() + 3)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(th["muted"]), 1.5))
            p.drawRoundedRect(r, 3, 3)
        p.end()


def _toggle_text_fix(t):
    return t


class Seg(QFrame):
    """A segmented choice: exactly one of several options lit."""
    changed = Signal(str)

    def __init__(self, options, value=None, slot=None):
        super().__init__()
        self.setProperty("segbox", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.buttons = {}
        for lab, val in options:
            b = QPushButton(lab)
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, v=val: self.set(v, emit=True))
            lay.addWidget(b)
            self.buttons[val] = b
        self._value = None
        self.set(value if value is not None else options[0][1])
        if slot:
            self.changed.connect(lambda _v: slot())

    def get(self):
        return self._value

    def label(self):
        b = self.buttons.get(self._value)
        return b.text() if b else ""

    def set(self, v, emit=False):
        self._value = v
        for val, b in self.buttons.items():
            b.setChecked(val == v)
        if emit:
            self.changed.emit(v)


class Card(QFrame):
    """A rounded card with an optional head (title, muted note, actions)."""

    def __init__(self, title="", sub="", parent=None, padding=12):
        super().__init__(parent)
        self.setProperty("card", True)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(padding, padding - 2, padding, padding)
        self.outer.setSpacing(8)
        self.head = QHBoxLayout()
        self.head.setSpacing(8)
        self.title_lbl = label(title, obj="CardTitle")
        self.sub_lbl = label(sub, muted=True)
        # a long note may be cut short, it must never widen the window
        self.sub_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.sub_lbl.setMinimumWidth(0)
        self.sub_lbl.setToolTip(sub)
        self.head.addWidget(self.title_lbl)
        self.head.addWidget(self.sub_lbl, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        self.head.addLayout(self.actions)
        if title or sub:
            self.outer.addLayout(self.head)
        self.body = QVBoxLayout()
        self.body.setSpacing(8)
        self.outer.addLayout(self.body)

    def add(self, w, stretch=0):
        self.body.addWidget(w, stretch)
        return w

    def add_action(self, w):
        self.actions.addWidget(w)
        return w

    def set_sub(self, text):
        self.sub_lbl.setText(text)
        self.sub_lbl.setToolTip(text)


class ModuleCard(Card):
    """A page module: the card's content can be opened in a window of its
    own (OPEN IN WINDOW) and docked back; where it was, a dashed strip
    says so.  Qt can simply move the widget between parents, so nothing is
    rebuilt and the selection and scroll position survive the trip."""

    def __init__(self, title, sub="", on_export=None):
        super().__init__(title, sub)
        self.content = QWidget()
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(0, 0, 0, 0)
        self.content_lay.setSpacing(6)
        self.body.addWidget(self.content, 1)
        self.extra_actions = QHBoxLayout()
        self.extra_actions.setSpacing(8)
        self.actions.addLayout(self.extra_actions)
        if on_export:
            self.add_action(IconButton("download", "Export to Excel", on_export))
        self.win_btn = self.add_action(IconButton("window", "Open in window", self.detach))
        self.fold_btn = self.add_action(IconButton("up", "Fold / unfold", self.toggle_fold))
        self.folded = False
        self.window = None
        self.stub = None
        self.title_text = title

    def add_extra(self, w):
        """A control in the card head, left of the icons (a toggle, a unit)."""
        self.extra_actions.addWidget(w)
        return w

    def toggle_fold(self):
        self.folded = not self.folded
        self.content.setVisible(not self.folded)
        self.fold_btn.set_icon("down" if self.folded else "up")
        self.updateGeometry()

    def detach(self):
        if self.window is not None:
            self.window.raise_()
            return
        w = QWidget(None, Qt.Window)
        w.setWindowTitle("WIP TRACKER · %s" % self.title_text)
        if ctx.sheet:
            w.setStyleSheet(ctx.sheet)      # the theme on now (v73)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        bar = hbox(label(self.title_text, obj="CardTitle"), None,
                   button("Dock back", slot=self.dock))
        lay.addWidget(bar)
        self.body.removeWidget(self.content)
        lay.addWidget(self.content, 1)
        w.resize(max(900, self.width()), max(520, self.height()))
        w.closeEvent = lambda ev: (ev.accept(), self.dock())
        self.window = w
        # where it was: a dashed strip with the way back
        self.stub = QFrame()
        self.stub.setProperty("stub", True)
        sl = QHBoxLayout(self.stub)
        sl.setContentsMargins(14, 8, 10, 8)
        sl.addWidget(label("<b>%s</b> is open in its own window" % self.title_text,
                           muted=True))
        sl.addStretch(1)
        sl.addWidget(IconButton("back", "Dock back", self.dock, text="Dock back"))
        self.body.addWidget(self.stub)
        self.win_btn.setEnabled(False)
        w.show()

    def dock(self):
        if self.window is None:
            return
        w, self.window = self.window, None
        if self.stub is not None:
            self.body.removeWidget(self.stub)
            self.stub.hide()
            self.stub.deleteLater()
            self.stub = None
        self.content.setParent(self)
        self.body.addWidget(self.content, 1)
        self.content.show()
        self.win_btn.setEnabled(True)
        w.closeEvent = lambda ev: ev.accept()
        w.close()
        w.deleteLater()

    @property
    def detached(self):
        return self.window is not None


def relayout_up(w):
    """A widget changed its height: make every layout above it (card,
    splitter, module row, the page) take the new size at once - a module
    shown again must not keep the height it had while it was closed."""
    p = w.parentWidget()
    while p is not None:
        if p.layout() is not None:
            p.layout().invalidate()
        p.updateGeometry()
        if p.isWindow() or isinstance(p, QScrollArea):
            break
        p = p.parentWidget()


class Splitter(QSplitter):
    """A QSplitter that tells its parent when its contents change size: a
    module opened again (or a table that grew) makes the row tall again
    instead of staying squeezed at the old height."""

    def event(self, ev):
        r = super().event(ev)
        if ev.type() == QEvent.LayoutRequest:
            self.updateGeometry()
        return r

    def childEvent(self, ev):
        super().childEvent(ev)
        self.updateGeometry()


class ModuleGrid(QWidget):
    """Where a page's modules sit.  v72: every open module in ONE row, side
    by side - a module opened next to another comes up beside it, not
    under it.  v73: with a handle between them - drag it and one module
    narrows while its neighbour widens; every open module fits the window
    (each keeps at least MIN_W, beyond that the page scrolls sideways).
    relayout() after a module is opened or closed."""
    MIN_W = 320

    def __init__(self, spacing=14):
        super().__init__()
        self.cards = []            # [(card, wide, weight)]
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(spacing)
        self.sp = spacing
        self.view_w = 0
        self.split = Splitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(spacing)
        self.lay.addWidget(self.split)
        self.lay.addStretch(1)
        self._open = ()
        self.holders = {}

    def add(self, card, wide=False, weight=1):
        # each module sits at the top of its own column at its own height -
        # a short module beside a tall one is not stretched to match
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        hl.addWidget(card)
        hl.addStretch(1)
        self.split.addWidget(holder)
        self.split.setStretchFactor(self.split.indexOf(holder), weight)
        self.cards.append((card, wide, weight))
        self.holders[id(card)] = holder
        card.hide()
        holder.hide()
        return card

    def shown(self):
        return [(c, k) for c, _w, k in self.cards if not c.isHidden()]

    def fit_widths(self, view_w):
        """Each module's least width: its own alone, MIN_W beside others."""
        self.view_w = view_w
        shown = self.shown()
        for c, _k in shown:
            h = self.holders[id(c)]
            base = c.minimumWidth()
            want = max(base, self.MIN_W) if len(shown) > 1 else base
            if h.minimumWidth() != want:
                h.setMinimumWidth(want)

    def fit_height(self):
        """The row is as tall as its tallest open module really is - Qt's
        own guess counted a wrapping tool row twice and made the page
        scroll for nothing."""
        shown = self.shown()
        if not shown:
            return
        h = max(_natural_h(c) for c, _k in shown)
        if self.split.minimumHeight() != h or self.split.maximumHeight() != h:
            self.split.setFixedHeight(h)

    def relayout(self):
        for c, _w, _k in self.cards:
            self.holders[id(c)].setVisible(not c.isHidden())
        shown = self.shown()
        self.fit_widths(self.view_w)
        now = tuple(id(c) for c, _k in shown)
        if now != self._open and shown:
            # a module opened or closed: share the width by weight again
            self._open = now
            total = max(1, self.split.width() or self.view_w or 1200)
            weights = {id(c): k for c, k in shown}
            tw = float(sum(weights.values())) or 1.0
            self.split.setSizes([int(total * weights[id(c)] / tw) if id(c) in weights else 0
                                 for c, _w, _k in self.cards])
        self.split.setVisible(bool(shown))
        relayout_up(self.split)


# =====================================================================
#  FLOW LAYOUT (wrapping chips)
# =====================================================================
class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(2 + m.left() + m.right(), 2 + m.top() + m.bottom())

    def _do(self, rect, test):
        m = self.contentsMargins()
        outer = rect
        rect = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + sp
            if nx - sp > rect.right() and line_h > 0:
                x = rect.x()
                y = y + line_h + sp
                nx = x + hint.width() + sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - outer.y() + m.bottom()


# =====================================================================
#  NAV RAIL
# =====================================================================
def draw_icon(p, name, cx, cy, col, w=1.7):
    pen = QPen(QColor(col), w)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    L = lambda pts: [p.drawLine(int(pts[i]), int(pts[i + 1]), int(pts[i + 2]),
                                int(pts[i + 3])) for i in range(0, len(pts) - 2, 2)]
    if name == "rw":
        L([cx - 9, cy + 9, cx + 9, cy + 9]); L([cx - 9, cy + 9, cx - 9, cy - 9])
        for i, h in enumerate((6, 12, 8)):
            L([cx - 4 + i * 5, cy + 9, cx - 4 + i * 5, cy + 9 - h])
    elif name == "cause":
        p.drawEllipse(QPoint(int(cx), int(cy)), 8, 8)
        p.drawEllipse(QPoint(int(cx), int(cy)), 3, 3)
    elif name == "wf":
        L([cx - 9, cy - 6, cx - 3, cy - 6, cx - 3, cy, cx + 3, cy, cx + 3, cy + 6, cx + 9, cy + 6])
    elif name == "lt":             # a stopwatch
        p.drawEllipse(QPoint(int(cx), int(cy + 1)), 7, 7)
        L([cx, cy - 3, cx, cy + 1, cx + 3, cy + 3]); L([cx - 2, cy - 9, cx + 2, cy - 9])
    elif name == "mrb":
        L([cx - 6, cy + 9, cx - 6, cy - 9])
        p.drawPolygon(QPolygonF([QPoint(cx - 6, cy - 9), QPoint(cx + 8, cy - 9),
                                 QPoint(cx + 4, cy - 4), QPoint(cx + 8, cy + 1),
                                 QPoint(cx - 6, cy + 1)]))
    elif name == "bn":
        p.drawPolygon(QPolygonF([QPoint(cx - 9, cy - 8), QPoint(cx + 9, cy - 8),
                                 QPoint(cx + 2, cy), QPoint(cx + 2, cy + 8),
                                 QPoint(cx - 2, cy + 6), QPoint(cx - 2, cy)]))
    elif name == "b1000":
        p.drawRect(int(cx - 9), int(cy - 8), 18, 16)
        L([cx - 1, cy - 8, cx - 1, cy + 2]); L([cx - 9, cy + 2, cx + 4, cy + 2])
    elif name == "fc":
        L([cx - 9, cy + 6, cx - 3, cy, cx + 1, cy + 3, cx + 9, cy - 6])
        L([cx + 4, cy - 6, cx + 9, cy - 6, cx + 9, cy - 1])
    elif name == "acc":
        for dx in (-5, 4):
            for dy in (-5, 4):
                p.drawRect(int(cx + dx - 3), int(cy + dy - 3), 6, 6)
    elif name == "user":
        p.drawEllipse(QPoint(int(cx), int(cy - 3)), 3, 3)
        p.drawArc(int(cx - 6), int(cy + 2), 12, 10, 0, 180 * 16)
    elif name == "sun":
        p.drawEllipse(QPoint(int(cx), int(cy)), 3, 3)
        for k in range(8):
            a = k * math.pi / 4
            L([cx + 5.5 * math.cos(a), cy + 5.5 * math.sin(a),
               cx + 7.5 * math.cos(a), cy + 7.5 * math.sin(a)])
    elif name == "moon":
        p.drawArc(int(cx - 6), int(cy - 6), 12, 12, 60 * 16, 250 * 16)
        p.drawArc(int(cx - 2), int(cy - 8), 10, 10, 200 * 16, 130 * 16)
    elif name == "clock":
        p.drawEllipse(QPoint(int(cx), int(cy)), 6, 6)
        L([cx, cy - 3, cx, cy, cx + 2.5, cy + 1.5])
    elif name == "download":
        L([cx, cy - 6, cx, cy + 3]); L([cx - 3.5, cy - 0.5, cx, cy + 3, cx + 3.5, cy - 0.5])
        L([cx - 6, cy + 6, cx + 6, cy + 6])
    elif name == "window":
        for dx in (-4, 4):
            for dy in (-4, 4):
                p.drawRect(int(cx + dx - 3), int(cy + dy - 3), 6, 6)
    elif name == "up":
        L([cx - 4, cy + 2, cx, cy - 2, cx + 4, cy + 2])
    elif name == "down":
        L([cx - 4, cy - 2, cx, cy + 2, cx + 4, cy - 2])
    elif name == "upload":
        L([cx, cy + 3, cx, cy - 6]); L([cx - 4, cy - 2, cx, cy - 6, cx + 4, cy - 2])
        L([cx - 6, cy + 6, cx + 6, cy + 6])
    elif name == "heart":
        p.drawArc(int(cx - 6), int(cy - 6), 6, 6, 30 * 16, 180 * 16)
        p.drawArc(int(cx), int(cy - 6), 6, 6, -30 * 16, 180 * 16)
        L([cx - 5.6, cy - 2, cx, cy + 5, cx + 5.6, cy - 2])
    elif name == "wand":
        L([cx - 6, cy + 6, cx + 3, cy - 3]); L([cx + 4, cy - 7, cx + 4, cy - 5])
        L([cx + 6, cy - 4, cx + 7, cy - 4])
    elif name == "lines":
        L([cx - 6, cy - 4, cx + 6, cy - 4]); L([cx - 6, cy, cx + 3, cy])
        L([cx - 6, cy + 4, cx + 1, cy + 4])
    elif name == "calendar":
        p.drawRoundedRect(QRectF(cx - 6, cy - 5, 12, 11), 2, 2)
        L([cx - 6, cy - 1, cx + 6, cy - 1]); L([cx - 3, cy - 7, cx - 3, cy - 4])
        L([cx + 3, cy - 7, cx + 3, cy - 4])
    elif name == "merge":
        L([cx - 4, cy - 6, cx - 4, cy + 1, cx + 5, cy + 1]); L([cx + 2, cy - 2, cx + 5, cy + 1, cx + 2, cy + 4])
    elif name == "db":
        p.drawEllipse(QRectF(cx - 6, cy - 7, 12, 4))
        L([cx - 6, cy - 5, cx - 6, cy + 5]); L([cx + 6, cy - 5, cx + 6, cy + 5])
        p.drawArc(int(cx - 6), int(cy + 3), 12, 4, 180 * 16, 180 * 16)
    elif name == "users":
        p.drawEllipse(QPoint(int(cx - 2), int(cy - 3)), 3, 3)
        p.drawArc(int(cx - 7), int(cy + 1), 10, 10, 0, 180 * 16)
        p.drawArc(int(cx + 1), int(cy - 6), 6, 6, -90 * 16, 180 * 16)
    elif name == "monitor":
        p.drawRoundedRect(QRectF(cx - 7, cy - 6, 14, 9), 2, 2)
        L([cx - 3, cy + 6, cx + 3, cy + 6]); L([cx, cy + 3, cx, cy + 6])
    elif name == "power":
        p.drawArc(int(cx - 6), int(cy - 5), 12, 12, 120 * 16, -240 * 16)
        L([cx, cy - 7, cx, cy - 1])
    elif name == "close":
        L([cx - 4, cy - 4, cx + 4, cy + 4]); L([cx - 4, cy + 4, cx + 4, cy - 4])
    elif name == "search":
        p.drawEllipse(QPoint(int(cx - 1), int(cy - 1)), 4, 4)
        L([cx + 2, cy + 2, cx + 5, cy + 5])
    elif name == "back":
        L([cx - 5, cy - 1, cx - 2, cy - 4]); L([cx - 5, cy - 1, cx - 2, cy + 2])
        p.drawArc(int(cx - 5), int(cy - 1), 10, 8, 90 * 16, -180 * 16)
    elif name == "settings":       # gear
        p.drawEllipse(QPoint(int(cx), int(cy)), 3, 3)
        teeth = 8
        poly = []
        for k in range(teeth * 2):
            a = k * math.pi / teeth
            r = 9.5 if k % 2 == 0 else 6.5
            poly.append(QPoint(int(cx + r * math.cos(a)), int(cy + r * math.sin(a))))
        p.drawPolygon(QPolygonF(poly))


class IconButton(QPushButton):
    """A small square button with a painted icon (download, window,
    up/down, sun/moon, user...).  Optional text after the icon."""

    def __init__(self, icon, tip="", slot=None, text="", size=34):
        super().__init__(("      " + text) if text else "")
        self.icon_name = icon
        self.setProperty("icon", True)
        self.setCursor(Qt.PointingHandCursor)
        if tip:
            self.setToolTip(tip)
        if not text:
            self.setFixedSize(size, size)
        if slot:
            self.clicked.connect(lambda _=False: slot())

    def set_icon(self, name):
        self.icon_name = name
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = ctx.TH["muted"] if not self.isEnabled() else ctx.TH["text"]
        cx = self.width() / 2 if not self.text() else 20
        draw_icon(p, self.icon_name, cx, self.height() / 2, col, 1.4)
        p.end()


class NavRail(QWidget):
    """The page menu: one entry per page, the open one lit, SETTINGS at the
    bottom.  Clicking an entry emits `picked(index)`."""
    picked = Signal(int)
    W = 80

    def __init__(self, items, bottom):
        super().__init__()
        self.setFixedWidth(self.W)
        self.items = list(items)          # [(key, label, page_index)]
        self.bottom = list(bottom)
        self.current = -1
        self.setMouseTracking(True)
        self.hover = None
        self.setCursor(Qt.PointingHandCursor)

    def _cells(self):
        out = []
        y = 10
        for it in self.items:
            out.append((QRect(6, y, self.W - 12, 58), it))
            y += 62
        y = self.height() - 10 - 58
        for it in reversed(self.bottom):
            out.append((QRect(6, y, self.W - 12, 58), it))
            y -= 62
        return out

    def set_current(self, idx):
        self.current = idx
        self.update()

    def mousePressEvent(self, ev):
        for r, (key, lab, idx) in self._cells():
            if r.contains(ev.position().toPoint()):
                self.picked.emit(idx)
                return

    def mouseMoveEvent(self, ev):
        h = None
        for r, it in self._cells():
            if r.contains(ev.position().toPoint()):
                h = it[2]
        if h != self.hover:
            self.hover = h
            self.update()

    def leaveEvent(self, ev):
        self.hover = None
        self.update()

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(th["rail_bg"]))
        f = QFont(qt_theme.FONT, 8)
        for r, (key, lab, idx) in self._cells():
            on = idx == self.current
            fg = th["rail_active_fg"] if on else th["rail_fg"]
            if on or idx == self.hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(th["rail_active_bg"]))
                p.drawRoundedRect(r, 8, 8)
            if on:
                p.setBrush(QColor(th["rail_active_fg"]))
                p.drawRoundedRect(QRect(r.left(), r.top() + 8, 3, r.height() - 16), 1, 1)
            draw_icon(p, key, r.center().x(), r.top() + 20, fg)
            f.setBold(on)
            p.setFont(f)
            p.setPen(QColor(fg))
            p.drawText(QRect(r.left(), r.top() + 34, r.width(), 20),
                       Qt.AlignHCenter | Qt.AlignVCenter, lab)
        p.end()


# =====================================================================
#  PART CHIPS
# =====================================================================
class PartChips(QWidget):
    """Single-select (one part) or multi-select (+ ALL PARTS).  Same API as
    the Tk version: get_selected / rebuild / set_single / clear_selection /
    select_many / buttons."""
    changed = Signal()

    def __init__(self, multi=False, include_all=None):
        super().__init__()
        self.multi = multi
        self.include_all = multi if include_all is None else include_all
        self.flow = FlowLayout(self, 6)
        self.selected = set()
        self.single = ""
        self.buttons = {}

    def _is_on(self, pno):
        if self.multi:
            return (not self.selected) if pno == ALL else pno in self.selected
        return pno == self.single

    def paint(self):
        for pno, b in self.buttons.items():
            b.setProperty("on", self._is_on(pno))
            repolish(b)

    def _click(self, pno):
        if self.multi:
            if pno == ALL:
                self.selected.clear()
            elif pno in self.selected:
                self.selected.discard(pno)
            else:
                self.selected.add(pno)
        else:
            self.single = pno
        self.paint()
        self.changed.emit()

    def rebuild(self, pnos, labels=None):
        pnos = list(pnos)
        labels = labels or {}
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.buttons.clear()
        for pno in (([ALL] + pnos) if self.include_all else pnos):
            b = QPushButton("ALL PARTS" if pno == ALL else labels.get(pno, pno))
            b.setProperty("chip", True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, p=pno: self._click(p))
            self.flow.addWidget(b)
            self.buttons[pno] = b
        if self.multi:
            self.selected = {p for p in self.selected if p in self.buttons}
        elif self.single not in self.buttons:
            self.single = (ALL if self.include_all and ALL in self.buttons
                           else (pnos[0] if pnos else ""))
        self.paint()
        self.updateGeometry()

    def get_selected(self):
        if self.multi:
            return sorted(self.selected) if self.selected else ALL
        return self.single

    def set_single(self, pno):
        self.single = pno
        self.paint()

    def clear_selection(self):
        self.selected.clear()
        self.paint()

    def select_many(self, pnos):
        self.selected = {p for p in pnos if p in self.buttons}
        self.paint()

    def label_of(self, pno):
        b = self.buttons.get(pno)
        return b.text() if b is not None else pno


# =====================================================================
#  DIALOGS: serial list, multi select
# =====================================================================
class DateField(QLineEdit):
    """A YYYY-MM-DD box that opens a calendar when clicked (v67 DateField:
    Today / Clear / Close).  Typing still works."""

    picked = Signal()

    def __init__(self):
        super().__init__()
        self.setPlaceholderText("YYYY-MM-DD")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to pick a date")

    def mousePressEvent(self, ev):
        super().mousePressEvent(ev)
        self.open_calendar()

    def open_calendar(self):
        pop = QFrame(self, Qt.Popup)
        pop.setProperty("popup", True)
        v = QVBoxLayout(pop)
        v.setContentsMargins(8, 8, 8, 8)
        cal = QCalendarWidget()
        cal.setGridVisible(True)
        cal.setFirstDayOfWeek(Qt.Monday)
        cal.setVerticalHeaderFormat(QCalendarWidget.ISOWeekNumbers)
        d = parse_date(self.text())
        if d:
            cal.setSelectedDate(d)
        v.addWidget(cal)

        def take(day):
            self.setText(day.isoformat() if day else "")
            pop.close()
            self.picked.emit()
        cal.clicked.connect(lambda qd: take(qd.toPython()))
        v.addWidget(hbox(button("Today", primary=True, slot=lambda: take(datetime.date.today())),
                         button("Clear", slot=lambda: take(None)), None,
                         button("Close", slot=pop.close)))
        pop.adjustSize()
        pop.move(self.mapToGlobal(self.rect().bottomLeft()))
        pop.show()
        self._pop = pop


class SerialListDialog(QDialog):
    def __init__(self, parent, current, on_apply):
        super().__init__(parent)
        self.setWindowTitle("SERIAL NUMBER LIST")
        self.resize(420, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Paste serial numbers - one per line, or separated "
                            "by commas or spaces.", muted=True, wrap=True))
        lay.addWidget(hbox(button("Import from Excel...", slot=self._import_excel),
                           button("Clear box", slot=lambda: self.text.setPlainText("")), None))
        self.text = QPlainTextEdit("\n".join(sorted(current)))
        lay.addWidget(self.text, 1)
        lay.addWidget(hbox(None, button("Cancel", slot=self.reject),
                           button("Apply", primary=True, slot=self._ok)))
        self.on_apply = on_apply

    def _import_excel(self):
        from wip_core import read_serial_list_file
        path, _f = QFileDialog.getOpenFileName(self, "Select a serial number list", "",
                                               "Excel (*.xlsx *.xlsm *.xls);;All files (*)")
        if not path:
            return
        try:
            values = read_serial_list_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Could not read file", str(e))
            return
        cur = self.text.toPlainText().rstrip()
        self.text.setPlainText((cur + "\n" if cur else "") + "\n".join(values))

    def _ok(self):
        raw = self.text.toPlainText().replace(",", " ").replace(";", " ").split()
        self.on_apply({norm_text(s) for s in raw if norm_text(s)})
        self.accept()


class MultiSelect(QDialog):
    """Pick any number from a list of (code, description), with a search."""

    def __init__(self, parent, title, items, selected, on_apply):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 560)
        lay = QVBoxLayout(self)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Find")
        lay.addWidget(self.find)
        self.lst = QListWidget()
        sel = set(selected)
        for code, desc in items:
            it = QListWidgetItem("%s   %s" % (code, desc))
            it.setData(Qt.UserRole, code)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if code in sel else Qt.Unchecked)
            self.lst.addItem(it)
        lay.addWidget(self.lst, 1)
        self.find.textChanged.connect(self._filter)
        lay.addWidget(hbox(button("Select all shown", slot=lambda: self._all(True)),
                           button("Clear", slot=lambda: self._all(False)), None,
                           button("Cancel", slot=self.reject),
                           button("Apply", primary=True, slot=self._ok)))
        self.on_apply = on_apply

    def _filter(self, txt):
        t = txt.lower()
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _all(self, on):
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if not it.isHidden():
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _ok(self):
        self.on_apply([self.lst.item(i).data(Qt.UserRole) for i in range(self.lst.count())
                       if self.lst.item(i).checkState() == Qt.Checked])
        self.accept()


# =====================================================================
#  FILTER BAR
# =====================================================================
class Chip(QPushButton):
    """A filter chip: "Engineer  All  v" - the value bold, lit while the
    filter restricts the result or its drawer is open."""

    def __init__(self, name):
        super().__init__("")
        self.setProperty("chip", True)
        self.setCursor(Qt.PointingHandCursor)
        self.name, self.value, self.active, self.open = name, "All", False, False

    def set_state(self, name, value, active, open_):
        self.name, self.value, self.active, self.open = name, str(value), active, open_
        self.setProperty("on", bool(active or open_))
        repolish(self)
        self.updateGeometry()
        self.update()

    def _fonts(self):
        f1 = QFont(self.font())
        f2 = QFont(self.font())
        f2.setBold(True)
        return f1, f2

    def sizeHint(self):
        f1, f2 = self._fonts()
        w = (QFontMetrics(f1).horizontalAdvance(self.name + "  ") +
             QFontMetrics(f2).horizontalAdvance(self.value) + 18 + 28)
        return QSize(w, 30)          # v73: slimmer (was 36)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = ctx.TH
        f1, f2 = self._fonts()
        x = 14
        p.setFont(f1)
        p.setPen(QColor(th["text"] if not self.active else th["text"]))
        a = QFontMetrics(f1).horizontalAdvance(self.name + "  ")
        p.drawText(QRect(x, 0, a, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.name)
        x += a
        p.setFont(f2)
        p.setPen(QColor(th["accent"] if self.active else th["text"]))
        b = QFontMetrics(f2).horizontalAdvance(self.value)
        p.drawText(QRect(x, 0, b + 2, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.value)
        draw_icon(p, "up" if self.open else "down", x + b + 14, self.height() / 2,
                  th["muted"], 1.3)
        p.end()


class Drawer(QFrame):
    """The panel a chip opens: a pop-up under the strip, over the page.
    It closes on a click outside it, with Esc, or from its chip."""

    def __init__(self, owner):
        super().__init__(None, Qt.Popup)
        self.setProperty("popup", True)
        self.owner = owner
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(12, 10, 12, 12)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        QTimer.singleShot(0, self.owner._drawer_closed)


class FilterBar(QWidget):
    """The filter strip every page shares.  It reads the same as the Tk
    one: WHEN (all / FW week / FW range / month / dates / this week / last
    week / L4W), ENGINEER, GROUP, PART, TECO/MRB flags, SERIAL list,
    WAITING > N days, MACHINES and the WIP switch.  Chips say what each
    filter is doing and light up while it restricts the result.

    `changed` fires whenever any of it changes; `eng_grp_changed` when the
    engineer or group does (the page then rebuilds its part list).
    """
    changed = Signal()
    eng_grp_changed = Signal()
    part_changed = Signal()

    def __init__(self, *, week_start_fn=None, part_multi=False, with_parts=True,
                 with_flags=True, with_serial=True, with_wait_days=False,
                 wait_days_default=10, wait_days_drop=False, with_machines=False,
                 machine_scope_fn=None, with_time=True, wip_label=None,
                 wip_default=False, flag_kinds=FLAG_KINDS, with_from_op=False):
        super().__init__()
        self.wip_default = wip_default
        self.week_start_fn = week_start_fn
        self.part_multi = part_multi
        self.with_time = with_time
        self.wait_days_drop = wait_days_drop
        self.machine_scope_fn = machine_scope_fn
        self._quiet = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        # ---- WHEN: the Time chip's drawer ----
        self.when = QWidget()
        wv = QVBoxLayout(self.when)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(10)
        wl = QHBoxLayout()
        wl.setSpacing(6)
        wv.addLayout(wl)
        self.mode = Seg([("All time", "all"), ("FW week", "week"), ("FW range", "weeks"),
                         ("Month", "month"), ("Dates", "dates")], "all")
        self.mode.changed.connect(self._mode_picked)
        wl.addWidget(self.mode)
        self.year = QComboBox()
        self.year.setMinimumWidth(70)
        self.week = QSpinBox(); self.week.setRange(1, 53)
        self.wstart = QSpinBox(); self.wstart.setRange(1, 53)
        self.wend = QSpinBox(); self.wend.setRange(1, 53); self.wend.setValue(53)
        self.month = QComboBox(); self.month.addItems(MONTH_NAMES)
        self.month.setCurrentIndex(datetime.date.today().month - 1)
        self.dstart = DateField(); self.dstart.setFixedWidth(110)
        self.dend = DateField(); self.dend.setFixedWidth(110)
        self.dstart.setPlaceholderText("YYYY-MM-DD")
        self.dend.setPlaceholderText("YYYY-MM-DD")
        self._fields = {
            "week": [label("Year", muted=True), self.year, label("Week", muted=True), self.week],
            "weeks": [label("Year", muted=True), self.year, label("From", muted=True), self.wstart,
                      label("To", muted=True), self.wend],
            "month": [label("Year", muted=True), self.year, label("Month", muted=True), self.month],
            "dates": [label("From", muted=True), self.dstart, label("To", muted=True), self.dend],
        }
        self.field_box = QWidget()
        self.field_lay = QHBoxLayout(self.field_box)
        self.field_lay.setContentsMargins(0, 0, 0, 0)
        self.field_lay.setSpacing(6)
        wl.addStretch(1)
        wv.addWidget(self.field_box)
        wv.addWidget(hbox(button("This wk", slot=lambda: self.quick_week(0)),
                          button("Last wk", slot=lambda: self.quick_week(-1)),
                          button("L4W", slot=lambda: self.quick_weeks(4)), None))
        for w in (self.year, self.month):
            w.currentIndexChanged.connect(lambda _i: self._emit())
        for w in (self.week, self.wstart, self.wend):
            w.valueChanged.connect(lambda _v: self._emit())
        for w in (self.dstart, self.dend):
            w.editingFinished.connect(self._emit)
            w.picked.connect(self._emit)

        # ---- ONE ROW OF CHIPS (the design) ----
        row = QWidget()
        self.chip_flow = FlowLayout(row, 8)
        lay.addWidget(row)
        self.drawer = None
        self.open_key = None
        self.panels = {}
        self.chips = {}
        # engineer / group: the combos hold the value, a list in a drawer picks it
        self.eng = QComboBox()
        self.grp = QComboBox()
        self.eng.addItem(ALL); self.grp.addItem(ALL)
        self.eng.currentIndexChanged.connect(lambda _i: (self.paint_chips(), self._eng_grp()))
        self.grp.currentIndexChanged.connect(lambda _i: (self.paint_chips(), self._eng_grp()))
        self.lists = {}
        for key, title, combo in (("eng", "Engineer", self.eng), ("grp", "Group", self.grp)):
            lw = QListWidget()
            lw.setMinimumHeight(60)
            lw.itemClicked.connect(lambda it, c=combo, l=lw: (c.setCurrentIndex(l.row(it)),
                                                              self.close_drawer()))
            self.lists[key] = (lw, combo)
            self._panel(key, title, lw)

        # PART
        self.parts = None
        self._parts_dirty = False
        self._parts_saved = set()
        if with_parts:
            # 2.1.1: one part -> a click applies it and closes the panel;
            # several -> clicks only mark them, APPLY puts them into effect
            # (one refresh, not one per click), a click outside drops them
            self.parts = PartChips(multi=part_multi)
            self.parts.changed.connect(self._part_clicked)
            find = QLineEdit()
            find.setPlaceholderText("Find part")
            find.textChanged.connect(self._find_part)
            self.part_count = label("", muted=True)
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(10)
            bl.addWidget(find)
            self.part_scroll = QScrollArea()
            self.part_scroll.setWidgetResizable(True)
            self.part_scroll.setFrameShape(QFrame.NoFrame)
            self.part_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.part_scroll.setWidget(self.parts)
            self.part_scroll.viewport().setAutoFillBackground(False)
            self.part_scroll.viewport().setStyleSheet("background: transparent;")
            self.parts.setAttribute(Qt.WA_TranslucentBackground)
            self.parts.setAutoFillBackground(False)
            bl.addWidget(self.part_scroll)
            if part_multi:
                self.part_apply = button("Apply", primary=True, slot=self._apply_parts)
                bl.addWidget(hbox(button("Clear", slot=self._clear_parts), None,
                                  self.part_apply))
            self._panel("part", "Part", box, extra=self.part_count)
        if with_time:
            self._panel("time", "Time", self.when)
        # FLAGS
        self.exclude_flags, self.only_flags = {}, {}
        if with_flags:
            box = QWidget()
            g = QGridLayout(box)
            g.setContentsMargins(0, 0, 0, 0)
            g.setHorizontalSpacing(6)
            g.setVerticalSpacing(6)
            for r, (title, store) in enumerate((("EXCLUDE", self.exclude_flags),
                                                ("ONLY", self.only_flags))):
                g.addWidget(label(title, section=True), r, 0)
                for c, k in enumerate(flag_kinds, start=1):
                    t = Toggle(FLAG_KIND_TITLE[k], slot=self._flags)
                    _toggle_text_fix(t)
                    store[k] = t
                    g.addWidget(t, r, c)
            self._panel("flags", "TECO / MRB / notes", box)
        # SERIAL
        self.serials = set()
        if with_serial:
            box = QWidget()
            bl = QHBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            self.serial_status = label("not set", muted=True)
            bl.addWidget(button("Serial list...", slot=self._serial_dialog))
            bl.addWidget(self.serial_status)
            bl.addWidget(button("Clear", slot=lambda: self.set_serials(set())))
            bl.addStretch(1)
            self._panel("serial", "By serial", box)
        # WAIT
        self.wait_on = Toggle("On", slot=self._wait)
        _toggle_text_fix(self.wait_on)
        self.wait_n = QSpinBox(); self.wait_n.setRange(1, 999)
        self.wait_n.setValue(wait_days_default)
        self.wait_n.valueChanged.connect(lambda _v: self._wait())
        self.with_wait = with_wait_days
        if with_wait_days:
            box = hbox(self.wait_on, label("leave out serials waiting more than" if wait_days_drop
                                           else "show only serials still waiting more than"),
                       self.wait_n, label("days at their current operation"), None)
            self._panel("wait", "Leave out parts waiting longer than" if wait_days_drop
                        else "Waiting longer than", box)
        # MACHINES
        self.with_mach = with_machines
        self.mach_list = []
        self.mach_on = Toggle("On", slot=self._mach)
        _toggle_text_fix(self.mach_on)
        self.mach_limit = QSpinBox(); self.mach_limit.setRange(0, 9999)
        self.mach_limit.setSpecialValueText("all")
        self.mach_limit.valueChanged.connect(lambda _v: self._mach())
        if with_machines:
            self.mach_lbl = label("none picked", muted=True)
            self.mach_typed = QLineEdit(); self.mach_typed.setFixedWidth(120)
            self.mach_typed.returnPressed.connect(self._mach_add_typed)
            box = hbox(self.mach_on, label("Work centre"),
                       button("Choose...", slot=self._mach_pick), self.mach_lbl,
                       label("or type"), self.mach_typed,
                       button("Add", slot=self._mach_add_typed),
                       label("show"), self.mach_limit,
                       label("most recent parts"), None)
            self._panel("mach", "Machines", box)
        # WIP
        self.wip = None
        if wip_label:
            self.wip = Toggle(wip_label, checked=wip_default, slot=self._emit)
            _toggle_text_fix(self.wip)
            self.chip_flow.addWidget(self.wip)
        # 2.1.1: FROM OP - only the parts at the chosen operation or past it
        self.with_from_op = with_from_op
        self._fromop_want = None
        self.fromop_on = Toggle("From op", slot=self._emit)
        _toggle_text_fix(self.fromop_on)
        self.fromop = QComboBox()
        self.fromop.setMinimumWidth(160)
        self.fromop.setMaxVisibleItems(25)
        self.fromop.currentIndexChanged.connect(self._fromop_picked)
        if with_from_op:
            self.fromop_on.setToolTip("Show only the parts at this operation or further on")
            self.chip_flow.addWidget(hbox(self.fromop_on, self.fromop, spacing=6))
        self.reset_btn = button("Reset all", slot=self.reset_all)
        self.reset_btn.setProperty("flat", True)
        self.chip_flow.addWidget(self.reset_btn)
        self.sync_mode_ui(emit=False)
        self.paint_chips()

    # ---- drawer ----
    def _panel(self, key, title, widget, extra=None):
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)
        t = label(title, obj="CardTitle")
        hl.addWidget(hbox(t, None, extra) if extra is not None else t)
        hl.addWidget(widget)
        self.panels[key] = holder
        chip = Chip(title)
        chip.clicked.connect(lambda _=False, k=key: self.show_panel(k))
        self.chips[key] = chip
        self.chip_flow.addWidget(chip)

    def show_panel(self, key):
        if self.open_key == key and self.drawer is not None and self.drawer.isVisible():
            self.drawer.hide()
            return
        self._drop_staged_parts()
        if key == "part" and self.parts is not None:
            self._parts_saved = set(self.parts.selected)
            self._parts_dirty = False
        if self.drawer is None:
            self.drawer = Drawer(self)
        for k, p in self.panels.items():
            p.setParent(None)
        self.open_key = key
        if key in self.lists:
            lw, combo = self.lists[key]
            lw.clear()
            for i in range(combo.count()):
                lw.addItem("All" if combo.itemText(i) == ALL else combo.itemText(i))
            lw.setCurrentRow(combo.currentIndex())
        self.drawer.lay.addWidget(self.panels[key])
        self.panels[key].show()
        self._fit_drawer()
        self.drawer.show()
        QTimer.singleShot(0, self._fit_drawer)    # again, once it is styled and laid out
        self.paint_chips()

    # 2.1.1: the panel takes the size of what is in it (it used to open
    # 900 px wide whatever it held)
    PAD_W = 26                     # the drawer's own margins and frame

    def _panel_width(self, key):
        win_w = self.window().width() - 40
        if key in self.lists:
            lw, combo = self.lists[key]
            fm = QFontMetrics(lw.font())
            widest = max([fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())]
                         + [fm.horizontalAdvance("Engineer")])
            w = widest + 70
            lo, hi = 220, 460
        elif key == "part" and self.parts is not None:
            sp = self.parts.flow.spacing()
            chips = [b.sizeHint().width() for b in self.parts.buttons.values()]
            total = sum(chips) + sp * max(0, len(chips) - 1)
            # few parts: one row as wide as they are; many: rows up to 860
            w = min(total, 860) + self.PAD_W + 30
            lo, hi = 360, 920
        else:
            self.panels[key].adjustSize()
            w = self.panels[key].sizeHint().width() + self.PAD_W
            lo, hi = 280, win_w
        return max(lo, min(hi, w, win_w))

    def _fit_drawer(self):
        if self.drawer is None or self.open_key is None:
            return
        key = self.open_key
        chip = self.chips[key]
        win = self.window()
        w = self._panel_width(key)
        if key == "part" and self.parts is not None:
            inner = w - self.PAD_W - 16     # leaves room for a scroll bar
            need = self.parts.flow.heightForWidth(inner) + 2
            self.part_scroll.setFixedHeight(max(34, min(need, int(win.height() * 0.5))))
        elif key in self.lists:
            lw = self.lists[key][0]
            row = max(18, lw.sizeHintForRow(0))
            lw.setFixedHeight(min(int(win.height() * 0.5), row * max(1, lw.count()) + 8))
        top_left = chip.mapToGlobal(QPoint(0, chip.height() + 6))
        right = win.mapToGlobal(QPoint(win.width() - 20, 0)).x()
        if top_left.x() + w > right:
            top_left.setX(max(win.mapToGlobal(QPoint(20, 0)).x(), right - w))
        self.drawer.setMinimumSize(0, 0)
        self.drawer.setMaximumSize(16777215, 16777215)
        self.drawer.setFixedWidth(w)
        self.drawer.layout().activate()
        self.drawer.setFixedHeight(self.drawer.sizeHint().height())   # shrinks too
        self.drawer.move(top_left)

    def close_drawer(self):
        if self.drawer is not None and self.drawer.isVisible():
            self.drawer.hide()

    def _drawer_closed(self):
        self._drop_staged_parts()
        self.open_key = None
        self.paint_chips()

    # ---- 2.1.1: PART - one click, or several and APPLY ----
    def _part_clicked(self):
        if self.part_multi:
            self._parts_dirty = True
            self.paint_chips()
            return
        self._part()
        self.close_drawer()

    def _apply_parts(self):
        self._parts_dirty = False
        self._part()
        self.close_drawer()

    def _drop_staged_parts(self):
        """Picks never applied are forgotten when the panel closes."""
        if self._parts_dirty and self.parts is not None:
            self._parts_dirty = False
            self.parts.selected = {p for p in self._parts_saved if p in self.parts.buttons}
            self.parts.paint()
            self.paint_chips()

    # ---- WHEN ----
    def _mode_picked(self, m):
        if m == "dates" and self._last_mode != "dates":
            today = datetime.date.today().isoformat()
            self.dstart.setText(today)
            self.dend.setText(today)
        self.sync_mode_ui()

    def sync_mode_ui(self, emit=True):
        m = self.mode.get()
        self._last_mode = m
        while self.field_lay.count():
            it = self.field_lay.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        if m == "dates":
            today = datetime.date.today().isoformat()
            for w in (self.dstart, self.dend):
                if not w.text().strip():
                    w.setText(today)
        for w in self._fields.get(m, []):
            self.field_lay.addWidget(w)
            w.show()
        self.field_lay.addStretch(1)
        if self.chips:
            self.paint_chips()
        if self.drawer is not None and self.drawer.isVisible():
            self._fit_drawer()
        if emit:
            self._emit()

    def quick_week(self, offset):
        ws = self.week_start_fn() if self.week_start_fn else 1
        y, w = iso_year_week(datetime.date.today() + datetime.timedelta(days=7 * offset),
                             week_offset(ws))
        self._quiet += 1
        self.mode.set("week")
        self._set_year(y)
        self.week.setValue(w)
        self._quiet -= 1
        self.sync_mode_ui()

    def quick_weeks(self, n):
        ws = self.week_start_fn() if self.week_start_fn else 1
        y, w = iso_year_week(datetime.date.today(), week_offset(ws))
        self._quiet += 1
        self.mode.set("weeks")
        self._set_year(y)
        self.wstart.setValue(max(1, w - n + 1))
        self.wend.setValue(w)
        self._quiet -= 1
        self.sync_mode_ui()

    def _set_year(self, y):
        i = self.year.findText(str(y))
        if i < 0:
            self.year.addItem(str(y))
            i = self.year.findText(str(y))
        self.year.setCurrentIndex(i)

    def refresh_years(self, years):
        cur = self.year.currentText()
        self._quiet += 1
        self.year.clear()
        self.year.addItems([""] + [str(y) for y in years])
        i = self.year.findText(cur)
        if i <= 0:
            i = self.year.findText(str(years[-1])) if years else 0
        self.year.setCurrentIndex(max(0, i))
        self._quiet -= 1

    def filters(self):
        """The time part of a page's filter dict - the same keys the Tk
        pages built by hand."""
        m = self.mode.get()
        yt = self.year.currentText()
        f = {"time_mode": m, "year": int(yt) if yt else None}
        if m == "week":
            f["week"] = self.week.value()
        elif m == "weeks":
            f["week_start"] = self.wstart.value()
            f["week_end"] = self.wend.value()
        elif m == "month":
            f["month"] = self.month.currentIndex() + 1
        elif m == "dates":
            f["date_start"] = self.dstart.text().strip() or None
            f["date_end"] = self.dend.text().strip() or None
        return f

    def time_label(self, f):
        yr = f.get("year") or "all years"
        return {"week": "FW week %s / %s" % (f.get("week"), yr),
                "weeks": "FW weeks %s - %s / %s" % (f.get("week_start"), f.get("week_end"), yr),
                "month": "%s %s" % (self.month.currentText(), f.get("year") or "?"),
                "dates": "%s ... %s" % (f.get("date_start") or "start", f.get("date_end") or "end"),
                }.get(f.get("time_mode"), "all time")

    def analysis_filters(self, page, apply_machines=True, need_cause_details=False):
        """The whole filter dict the analysis core expects, for `page`
        (its own merge switch and week start), with the MACHINES filter
        folded into the serial list the way the Tk pages did it."""
        from wip_core import allowed_part_keys, time_mode_to_date_range
        f = dict(self.filters())
        f.update({"engineer": self.engineer(), "part_group": self.group(),
                  "part_no": self.parts.get_selected() if self.parts else ALL,
                  "merge": ctx.db.merge_on(page), "wk_day": ctx.db.week_start(page),
                  "reject_ops": [], "cause_ops": [],
                  "wip_only": bool(self.wip and self.wip.isChecked()),
                  "serials": sorted(self.get_serials()),
                  "exclude_flag_kinds": [k for k, t in self.exclude_flags.items() if t.isChecked()],
                  "only_flag_kinds": [k for k, t in self.only_flags.items() if t.isChecked()],
                  "need_cause_details": need_cause_details})
        f["time_label"] = self.time_label(f)
        if apply_machines:
            d1, d2 = time_mode_to_date_range(f, ctx.db.week_start(page))
            scope = allowed_part_keys(ctx.db, f["engineer"], f["part_group"], f["part_no"],
                                      f["merge"])
            from_m = self.machine_serials(d1, d2, part_keys=scope)
            if from_m is not None:
                chosen = self.get_serials()
                f["serials"] = sorted((chosen & from_m) if chosen else from_m)
        return f

    def machine_scope_for(self, page):
        from wip_core import allowed_part_keys, time_mode_to_date_range
        f = self.analysis_filters(page, apply_machines=False)
        d1, d2 = time_mode_to_date_range(f, ctx.db.week_start(page))
        return (allowed_part_keys(ctx.db, f["engineer"], f["part_group"], f["part_no"],
                                  f["merge"]), d1, d2)

    def cascade(self, page, single=False):
        """Engineer -> group -> part list, for `page`'s merge switch."""
        parts = ctx.db.all_parts(ctx.db.merge_on(page))
        eng = self.engineer()
        groups = sorted({p["part_group"] for p in parts if p["part_group"] and
                         (eng == ALL or (p["engineer"] or "").upper() == eng.upper())})
        grp = self.set_choices(self.grp, groups)
        pnos = sorted({p["part_no"] for p in parts
                       if (eng == ALL or (p["engineer"] or "").upper() == eng.upper())
                       and (grp == ALL or p["part_group"] == grp)})
        if self.parts is not None:
            self.parts.rebuild(pnos)
        return parts

    # ---- engineer / group ----
    def set_choices(self, combo, values):
        cur = combo.currentText()
        if combo is self.eng and values and not self.__dict__.get("_eng_seeded"):
            # v72: the first time the list is filled, start on the
            # account's own engineer (when it has parts), else ALL
            self._eng_seeded = True
            home = ctx.home_engineer()
            if home and cur in ("", ALL):
                cur = home
        self._quiet += 1
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([ALL] + list(values))
        i = combo.findText(cur)
        combo.setCurrentIndex(i if i >= 0 else 0)
        combo.blockSignals(False)
        self._quiet -= 1
        self.paint_chips()           # v73: the chip says what was picked
        return combo.currentText()

    def engineer(self):
        return self.eng.currentText() or ALL

    def group(self):
        return self.grp.currentText() or ALL

    def _eng_grp(self):
        if self._quiet:
            return
        self.eng_grp_changed.emit()

    # ---- chips ----
    def _part(self):
        self.paint_chips()
        self.part_changed.emit()
        self._emit()

    def _flags(self):
        self.paint_chips()
        self._emit()

    def _serial_dialog(self):
        SerialListDialog(self.window(), self.serials, self.set_serials).exec()

    def set_serials(self, s):
        self.serials = set(s)
        if hasattr(self, "serial_status"):
            self.serial_status.setText("not set" if not s else "%d serial(s)" % len(s))
        self.paint_chips()
        self._emit()

    def get_serials(self):
        return set(self.serials)

    def wait_days(self):
        if not self.with_wait or not self.wait_on.isChecked():
            return None
        return self.wait_n.value() or None

    def _wait(self):
        self.paint_chips()
        self._emit()

    def machine_codes(self):
        if not self.with_mach or not self.mach_on.isChecked():
            return []
        return list(self.mach_list)

    def machine_limit(self):
        return self.mach_limit.value() or None

    def machine_serials(self, d1, d2, part_keys=None):
        codes = self.machine_codes()
        if not codes:
            return None
        return set(ctx.db.serials_at_work_centers(codes, d1, d2, self.machine_limit(),
                                                  part_keys=part_keys))

    def _mach(self):
        self.paint_chips()
        self._emit()

    def _mach_pick(self):
        pk, d1, d2 = (None, None, None)
        if self.machine_scope_fn:
            try:
                pk, d1, d2 = self.machine_scope_fn()
            except Exception:
                pass
        rows = ctx.db.work_centers(part_keys=pk, d1=d1, d2=d2)
        if not rows:
            rows = ctx.db.work_centers()
        if not rows:
            QMessageBox.information(self, "Machines", "No work centres in the stored data yet.")
            return
        items = [(r["code"], "%s   [%s]" % (r["name"] or "-", r["comp"])) for r in rows]

        def applied(sel):
            self.mach_list = list(sel)
            if self.mach_list and not self.mach_on.isChecked():
                self.mach_on.setChecked(True)
            else:
                self._mach()
        if self.drawer is not None:
            self.drawer.hide()
        MultiSelect(self.window(), "Select work centres", items, self.mach_list, applied).exec()

    def _mach_add_typed(self):
        codes = [c.strip() for c in self.mach_typed.text().replace(";", ",").split(",")
                 if c.strip()]
        if not codes:
            return
        have = set(self.mach_list)
        self.mach_list += [c for c in codes if c not in have]
        self.mach_typed.setText("")
        if not self.mach_on.isChecked():
            self.mach_on.setChecked(True)
        else:
            self._mach()

    # ---- 2.1.1: each account's filters come back next time ----
    # Saved per page in the account's user_pref.  Engineer / group / part
    # are NOT kept: they always start on the base rule (own engineer).
    _live = []

    def remember(self, page):
        """Called by the page once its own defaults are set: from now on
        the bar is stored under `page` and the saved state is put back."""
        self.page_key = page
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self.save_state)
        self._save_user = None
        FilterBar._live.append(self)
        self.load_saved(emit=False)

    def get_state(self):
        s = {"mode": self.mode.get(), "year": self.year.currentText(),
             "week": self.week.value(), "wstart": self.wstart.value(),
             "wend": self.wend.value(), "month": self.month.currentIndex(),
             "dstart": self.dstart.text(), "dend": self.dend.text(),
             "ex": [k for k, t in self.exclude_flags.items() if t.isChecked()],
             "on": [k for k, t in self.only_flags.items() if t.isChecked()],
             "serials": sorted(self.serials),
             "wait_on": self.wait_on.isChecked(), "wait_n": self.wait_n.value(),
             "mach_on": self.mach_on.isChecked(), "mach": list(self.mach_list),
             "mach_limit": self.mach_limit.value()}
        if self.wip is not None:
            s["wip"] = self.wip.isChecked()
        if self.with_from_op:
            s["fromop_on"] = self.fromop_on.isChecked()
            s["fromop"] = self._fromop_want
        return s

    def set_state(self, s):
        self._quiet += 1
        try:
            if self.with_time and s.get("mode") in self.mode.buttons:
                self.mode.set(s["mode"])
                if s.get("year"):
                    self._set_year(s["year"])
                self.week.setValue(int(s.get("week", self.week.value())))
                self.wstart.setValue(int(s.get("wstart", self.wstart.value())))
                self.wend.setValue(int(s.get("wend", self.wend.value())))
                self.month.setCurrentIndex(int(s.get("month", self.month.currentIndex())))
                self.dstart.setText(s.get("dstart") or "")
                self.dend.setText(s.get("dend") or "")
            if "ex" in s:
                for k, t in self.exclude_flags.items():
                    t.setChecked(k in s["ex"])
                for k, t in self.only_flags.items():
                    t.setChecked(k in s.get("on", []))
            if hasattr(self, "serial_status") and "serials" in s:
                self.serials = set(s["serials"])
                n = len(self.serials)
                self.serial_status.setText("not set" if not n else "%d serial(s)" % n)
            if self.with_wait and "wait_on" in s:
                self.wait_n.setValue(int(s.get("wait_n", self.wait_n.value())))
                self.wait_on.setChecked(bool(s["wait_on"]))
            if self.with_mach and "mach_on" in s:
                self.mach_list = list(s.get("mach", []))
                self.mach_limit.setValue(int(s.get("mach_limit", 0)))
                self.mach_on.setChecked(bool(s["mach_on"]))
            if self.wip is not None and "wip" in s:
                self.wip.setChecked(bool(s["wip"]))
            if self.with_from_op and "fromop_on" in s:
                self._fromop_want = s.get("fromop")
                i = self.fromop.findData(self._fromop_want) if self._fromop_want else -1
                self.fromop.blockSignals(True)
                self.fromop.setCurrentIndex(i)
                self.fromop.blockSignals(False)
                self.fromop_on.setChecked(bool(s["fromop_on"]))
        finally:
            self._quiet -= 1
        self.sync_mode_ui(emit=False)
        self.paint_chips()

    def load_saved(self, emit=True):
        """Puts back what this account last left on this page (if any)."""
        if not getattr(self, "page_key", None):
            return
        user = ctx.username()
        self._save_user = user
        try:
            s = (ctx.db.user_pref(user).get("filters") or {}).get(self.page_key) if user else None
        except Exception:
            s = None
        if not isinstance(s, dict):
            return
        try:
            self.set_state(s)
        except Exception:
            return
        if emit:
            self._emit()

    def _schedule_save(self):
        if getattr(self, "page_key", None) and ctx.username():
            self._save_user = ctx.username()
            self._save_timer.start()

    def save_state(self):
        user = getattr(self, "_save_user", None)
        if not getattr(self, "page_key", None) or not user or ctx.db is None:
            return
        self._save_timer.stop()
        try:
            all_f = dict(ctx.db.user_pref(user).get("filters") or {})
            all_f[self.page_key] = self.get_state()
            ctx.db.set_user_pref(user, "filters", all_f)
        except Exception:
            pass

    @classmethod
    def flush_all(cls):
        """Writes any change still waiting on its timer (sign out / close)."""
        for b in list(cls._live):
            try:
                if b._save_timer.isActive():
                    b.save_state()
            except RuntimeError:          # its widget is gone
                cls._live.remove(b)

    @classmethod
    def reload_all(cls):
        """A new account signed in: each page takes that account's filters."""
        for b in list(cls._live):
            try:
                b.load_saved()
            except RuntimeError:
                cls._live.remove(b)

    # ---- 2.1.1: FROM OP ----
    def set_route_ops(self, route):
        """The page's route for the picked part: the FROM OP list."""
        from wip_core import route_step
        want = self._fromop_want
        self._quiet += 1
        self.fromop.blockSignals(True)
        self.fromop.clear()
        for col in route or []:
            txt = str(col.get("label") or col.get("op"))
            if col.get("desc"):
                txt += "   " + str(col["desc"])
            self.fromop.addItem(txt, route_step(col))
        i = self.fromop.findData(want) if want else -1
        self.fromop.setCurrentIndex(i)
        self.fromop.blockSignals(False)
        self._quiet -= 1

    def _fromop_picked(self, _i):
        st = self.fromop.currentData()
        if st is None:
            return
        self._fromop_want = st
        if not self.fromop_on.isChecked():
            self.fromop_on.setChecked(True)      # picking an op switches it on
        else:
            self._emit()

    def from_op_step(self):
        """The step key picked in FROM OP, or None while it is off."""
        if not self.with_from_op or not self.fromop_on.isChecked():
            return None
        return self._fromop_want

    def reset_all(self):
        self._quiet += 1
        home = self.eng.findText(ctx.home_engineer() or "\x00")
        self.eng.setCurrentIndex(home if home >= 0 else 0)    # v72: back to the base
        self.grp.setCurrentIndex(0)
        if self.wip is not None:
            self.wip.setChecked(self.wip_default)
        self.serials = set()
        if hasattr(self, "serial_status"):
            self.serial_status.setText("not set")
        for t in list(self.exclude_flags.values()) + list(self.only_flags.values()):
            t.setChecked(False)
        self.mode.set("all")
        self.wait_on.setChecked(False)
        self.mach_on.setChecked(False)
        self.mach_list = []
        self.mach_limit.setValue(0)
        self.fromop_on.setChecked(False)
        if self.parts is not None and self.part_multi:
            self.parts.clear_selection()
        self._quiet -= 1
        self.sync_mode_ui(emit=False)
        self.paint_chips()
        self.eng_grp_changed.emit()
        self._emit()

    def _part_state(self):
        if self.parts is None:
            return "All", False
        sel = self.parts.get_selected()
        if self.part_multi:
            if sel == ALL or not sel:
                return "All", False
            if len(sel) == 1:
                return self.parts.label_of(sel[0]), True
            return "%d chosen" % len(sel), True
        if not sel:
            return "none", False
        return self.parts.label_of(sel), True

    def _flag_state(self):
        ex = [FLAG_KIND_TITLE[k] for k, t in self.exclude_flags.items() if t.isChecked()]
        on = [FLAG_KIND_TITLE[k] for k, t in self.only_flags.items() if t.isChecked()]
        bits = []
        if on:
            bits.append("only " + "+".join(on))
        if ex == ["FIXED"] and not on:
            return "Fixed hidden", True
        if ex:
            bits.append("no " + "+".join(ex))
        return (", ".join(bits) if bits else "Off"), bool(bits)

    def _find_part(self, txt):
        t = txt.strip().upper()
        for pno, b in self.parts.buttons.items():
            b.setVisible(not t or t in b.text().upper())

    def _clear_parts(self):
        if self.part_multi:
            self.parts.clear_selection()
            self._parts_dirty = True
            self.paint_chips()

    def paint_chips(self):
        states = {}
        for key, name, combo in (("eng", "Engineer", self.eng), ("grp", "Group", self.grp)):
            if key in self.chips:
                v = combo.currentText() or ALL
                states[key] = (name, "All" if v == ALL else v, v != ALL)
        if "time" in self.chips:
            f = self.filters()
            m = f["time_mode"]
            txt = {"all": "All time", "week": "FW %s" % f.get("week"),
                   "weeks": "FW %s-%s" % (f.get("week_start"), f.get("week_end")),
                   "month": self.month.currentText()[:3] + " %s" % (f.get("year") or ""),
                   "dates": "%s..%s" % (f.get("date_start") or "", f.get("date_end") or "")}.get(m, m)
            states["time"] = ("Time", txt, m != "all")
        if self.parts is not None and hasattr(self, "part_count"):
            sel = self.parts.get_selected()
            txt = ("%d chosen" % len(sel)) if self.part_multi and sel != ALL else \
                ("all parts" if self.part_multi else "")
            if self._parts_dirty:
                txt += "  ·  press Apply"
            self.part_count.setText(txt)
        if "part" in self.chips:
            states["part"] = ("Part",) + self._part_state()
        if "flags" in self.chips:
            states["flags"] = ("Flags",) + self._flag_state()
        if "serial" in self.chips:
            n = len(self.serials)
            states["serial"] = ("Serial", "%d listed" % n if n else "Any", bool(n))
        if "wait" in self.chips:
            n = self.wait_days()
            states["wait"] = ("Wait", ("drop > %d days" % n if self.wait_days_drop else "> %d days" % n)
                              if n else "Off", bool(n))
        if "mach" in self.chips:
            codes = self.machine_codes()
            lim = self.machine_limit()
            txt = (codes[0] if len(codes) == 1 else "%d WC" % len(codes)) if codes else "All"
            if codes and lim:
                txt += " / last %d" % lim
            states["mach"] = ("Machines", txt, bool(codes))
            picked = self.mach_list
            if hasattr(self, "mach_lbl"):
                self.mach_lbl.setText("none picked" if not picked else
                                      ", ".join(picked[:3]) + ("  +%d" % (len(picked) - 3)
                                                               if len(picked) > 3 else ""))
        for k, (name, txt, act) in states.items():
            self.chips[k].set_state(name, txt, bool(act), self.open_key == k)

    def _emit(self):
        if self._quiet:
            return
        if self.chips:
            self.paint_chips()
        self._schedule_save()
        self.changed.emit()


# =====================================================================
#  DATA TABLE
# =====================================================================
ROW_KEYS = ("a", "b", "count", "hour", "sub")


def note_ink(col):
    """A note colour made readable as text on the current theme."""
    c = QColor(col)
    if ctx.TH["name"] == "day":
        return c.darker(130).name() if c.lightness() > 150 else c.name()
    return c.lighter(150).name() if c.lightness() < 110 else c.name()


_BRUSHES = {}
_ALIGN = {"w": int(Qt.AlignLeft | Qt.AlignVCenter), "e": int(Qt.AlignRight | Qt.AlignVCenter),
          "center": int(Qt.AlignHCenter | Qt.AlignVCenter)}


def _brush(col):
    b = _BRUSHES.get(col)
    if b is None:
        if len(_BRUSHES) > 4000:
            _BRUSHES.clear()
        b = _BRUSHES[col] = QBrush(QColor(col))
    return b


class TableModel(QAbstractTableModel):
    def __init__(self, owner):
        super().__init__()
        self.o = owner

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.o.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.o.columns)

    def data(self, idx, role=Qt.DisplayRole):
        # v69: called for every cell and role on each repaint - brushes,
        # fonts and alignments come from small caches, not built anew
        if not idx.isValid():
            return None
        r, c = idx.row(), idx.column()
        o = self.o
        if role == Qt.DisplayRole:
            row = o.rows[r]
            v = row[c] if c < len(row) else ""
            v = "" if v is None else str(v)
            if c == 0 and o.dot_col0 and v:
                return "●  " + v
            return v
        if role == Qt.BackgroundRole:
            col = o.cell_bg.get((r, c))
            return _brush(col or o.bg_for(r))
        if role == Qt.ForegroundRole:
            col = o.cell_fg.get((r, c))
            if col:
                return _brush(col)
            rb = o.row_bg[r] if r < len(o.row_bg) else None
            fc = o.serial_col if o.serial_col >= 0 else 0
            if o.stripes and rb in qt_theme.FLAG_COLORS:
                # flag rows (new design): a coloured stripe and the serial's
                # text in the flag colour, not a painted row
                return _brush(ctx.flag_text(rb) if c == fc else ctx.TH["text"])
            if o.stripes and isinstance(rb, str) and rb.startswith("#"):
                # a note: the same way (v70) - it used to paint the row
                return _brush(note_ink(rb) if c == fc else ctx.TH["text"])
            if (rb in qt_theme.FLAG_COLORS) or (isinstance(rb, str) and rb.startswith("#")):
                return _brush("#FFFFFF")
            return _brush(ctx.TH["text"])
        if role == Qt.FontRole:
            fc = o._font_cache
            key = (o.zoom, o.fit_width)
            if fc.get("key") != key:
                f = QFont(o.font())
                fb = QFont(f)
                fb.setBold(True)
                fc.clear()
                fc.update(key=key, n=f, b=fb)
            return fc["b"] if (r < len(o.row_bold) and o.row_bold[r]) else fc["n"]
        if role == Qt.TextAlignmentRole:
            a = o.align[c] if c < len(o.align) else "w"
            return _ALIGN.get(a, _ALIGN["w"])
        if role == Qt.ToolTipRole:
            return o.cell_tip.get((r, c))
        return None

    def headerData(self, s, orient, role=Qt.DisplayRole):
        if orient == Qt.Horizontal and role == Qt.DisplayRole and s < len(self.o.columns):
            return head_text(self.o.columns[s][0])
        return None

    def reset(self):
        self.beginResetModel()
        self.endResetModel()


KEEP_UPPER = {"RW", "FW", "FTY", "WC", "TECO", "MRB", "SAP", "COMP", "ENG", "WIP", "ID",
              "QTY", "STD", "CMM", "NDT", "X", "W", "F"}


def head_text(t):
    """Column headings read like the design: "REJECT OP DESCRIPTION" is
    shown as "Reject op description", codes (RW, FW, TECO...) stay upper.
    Headings that already carry lower case (dates, days) are left alone."""
    t = str(t)
    if t and t != t.upper():
        return t           # dates and the like, line breaks kept (FC4)
    t = t.replace("\n", " ")
    if not t or not any(ch.isalpha() for ch in t):
        return t
    out = []
    for i, w in enumerate(t.split(" ")):
        core = w.strip("()%/.,:")
        if core in KEEP_UPPER or any(ch.isdigit() for ch in w):
            out.append(w)
        elif i == 0 or (out and out[-1] == ""):
            out.append(w[:1] + w[1:].lower())
        else:
            out.append(w.lower())
    return " ".join(out).replace(" op ", " op ")


class VerticalHeader(QHeaderView):
    """Column headings written upright (day / operation columns)."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.vertical = False
        self.level = 0             # the first `level` columns read level
        self.spans = {}            # first column -> how many it heads
        self.covered = set()       # columns headed by the one before

    def sizeHint(self):
        s = super().sizeHint()
        if self.vertical:
            f = QFont(self.font()); f.setBold(True)
            fm = QFontMetrics(f)
            m = self.model()
            longest = max([fm.horizontalAdvance(str(m.headerData(i, Qt.Horizontal) or ""))
                           for i in range(self.level, m.columnCount())] or [40]) if m else 40
            s.setHeight(min(170, longest + 22))
        return s

    def paintSection(self, p, rect, idx):
        if not self.vertical:
            return super().paintSection(p, rect, idx)
        if idx < self.level:
            p.save()
            p.fillRect(rect, QColor(ctx.TH["card"]))
            p.setPen(QColor(ctx.TH["border"]))
            p.drawLine(rect.bottomLeft(), rect.bottomRight())
            p.setPen(QColor(ctx.TH["muted"]))
            f = QFont(self.font()); f.setBold(True)
            p.setFont(f)
            p.drawText(rect.adjusted(8, 0, -4, -6), Qt.AlignLeft | Qt.AlignBottom,
                       str(self.model().headerData(idx, Qt.Horizontal) or ""))
            p.restore()
            return
        p.save()
        p.fillRect(rect, QColor(ctx.TH["card"]))
        p.setPen(QColor(ctx.TH["border"]))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())
        p.setPen(QColor(ctx.TH["muted"]))
        f = QFont(self.font()); f.setBold(True)
        p.setFont(f)
        # a heading over several columns: each column paints its own slice
        # of the text, so a repaint of one of them never cuts it in half
        start = idx
        if idx in self.covered:
            start = max(c for c in self.spans if c < idx and c + self.spans[c] > idx)
        txt = str(self.model().headerData(start, Qt.Horizontal) or "")
        n = self.spans.get(start, 1)
        cx = rect.center().x()
        if n > 1:
            left = rect.left() - sum(self.sectionSize(k) for k in range(start, idx))
            cx = left + sum(self.sectionSize(start + k) for k in range(n)) // 2
            p.setClipRect(rect)
        p.translate(cx + 5, rect.bottom() - 6)
        p.rotate(-90)
        p.drawText(0, 0, txt)
        p.restore()


class StripeDelegate(QStyledItemDelegate):
    """Paints the coloured stripe at the left edge of a flag row."""

    def __init__(self, table, parent):
        super().__init__(parent)
        self.t = table

    def _pair_frame(self, p, opt, idx):
        """Two columns of one day (W | F) share one framed box: each cell
        draws its half of the box.  True when this cell is in a pair."""
        t = self.t
        c = idx.column()
        if c in t.pair_cols:
            ext = QRectF(opt.rect).adjusted(0, 0, self.parent().columnWidth(c + 1), 0)
        elif (c - 1) in t.pair_cols:
            ext = QRectF(opt.rect).adjusted(-self.parent().columnWidth(c - 1), 0, 0, 0)
        else:
            return False
        th = ctx.TH
        night = th["name"] != "day"
        p.save()
        p.fillRect(opt.rect, QColor(t.bg_for(idx.row())))
        p.setClipRect(opt.rect)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#3A4252" if night else "#C9D2E0"), 1))
        p.setBrush(QColor("#252A33" if night else "#EEF2F8"))
        p.drawRoundedRect(ext.adjusted(2.5, 2.5, -2.5, -2.5), 9, 9)
        p.restore()
        return True

    def paint(self, p, opt, idx):
        t = self.t
        rc = (idx.row(), idx.column())
        if rc in t.cell_loadbar:
            # Forecast: the load as a bar rising from the bottom, a dashed
            # line where 100% is (the bar's scale runs to 140%)
            f, txt = t.cell_loadbar[rc]
            night = ctx.TH["name"] != "day"
            p.save()
            p.fillRect(opt.rect, QColor(t.bg_for(idx.row())))
            p.setRenderHint(QPainter.Antialiasing)
            box = QRectF(opt.rect).adjusted(2, 3, -2, -3)
            path = QPainterPath()
            path.addRoundedRect(box, 6, 6)
            p.setClipPath(path)
            p.fillRect(box, QColor("#262626" if night else "#EEF0F3"))
            h = box.height() * min(1.0, max(0.0, f) / 1.4)
            p.fillRect(QRectF(box.left(), box.bottom() - h, box.width(), h),
                       QColor(t.cell_bg.get(rc, ctx.TH["accent"])))
            y = box.bottom() - box.height() / 1.4
            pen = QPen(QColor(255, 255, 255, 115) if night else QColor(0, 0, 0, 90), 1, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
            f2 = QFont(t.font())
            f2.setBold(True)
            p.setFont(f2)
            # white on the bar (with a soft shadow); where the bar does not
            # reach the number, plain text colour on the track
            on_bar = h >= box.height() / 2 + 7
            if on_bar or night:
                p.setPen(QColor(0, 0, 0, 130))
                p.drawText(box.translated(0, 1).toRect(), Qt.AlignCenter, txt)
                p.setPen(QColor("#FFFFFF"))
            else:
                p.setPen(QColor(ctx.TH["text"]))
            p.drawText(box.toRect(), Qt.AlignCenter, txt)
            p.restore()
            return
        if t.tiles:
            pair = self._pair_frame(p, opt, idx)
            tr = t.tile_radius
            inner = (QRectF(opt.rect).adjusted(3, 5, -3, -5) if pair
                     else QRectF(opt.rect).adjusted(2, 2, -2, -2))
        if t.tiles and rc in t.cell_bg and rc not in t.plain_cells:
            # a heat cell as a rounded tile on the row, the number centred
            p.save()
            if not pair:
                p.fillRect(opt.rect, QColor(t.bg_for(idx.row())))
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.cell_bg[rc]))
            p.drawRoundedRect(inner, tr, tr)
            f = QFont(t.font())
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(t.cell_fg.get(rc, ctx.TH["text"])))
            p.drawText(opt.rect, Qt.AlignCenter, str(idx.data() or ""))
            p.restore()
            return
        if rc in t.cell_bar:
            # a value with a thin progress bar under it (flow efficiency)
            frac, col = t.cell_bar[rc]
            p.save()
            p.fillRect(opt.rect, QColor(t.bg_for(idx.row())))
            p.setRenderHint(QPainter.Antialiasing)
            r = QRectF(opt.rect)
            track = QRectF(r.left() + 8, r.bottom() - 7, r.width() - 16, 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ctx.TH["dim"]))
            p.drawRoundedRect(track, 2, 2)
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * frac, 4), 2, 2)
            p.setFont(t.font())
            p.setPen(QColor(ctx.TH["text"]))
            p.drawText(r.adjusted(0, 0, 0, -6).toRect(), Qt.AlignCenter, str(idx.data() or ""))
            p.restore()
            return
        if t.tiles and rc in t.plain_cells:
            p.save()
            if not pair:
                p.fillRect(opt.rect, QColor(t.bg_for(idx.row())))
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.cell_bg.get(rc, ctx.TH["dim"])))
            p.drawRoundedRect(inner, tr, tr)
            p.restore()
            return
        super().paint(p, opt, idx)
        # a line over the first row of each box (one box per serial etc.)
        if idx.row() in t.group_starts and idx.row() > 0:
            gw = t.group_line
            if gw > 1:
                # v70: a firm line between boxes (serial groups in RW)
                p.fillRect(QRect(opt.rect.left(), opt.rect.top(), opt.rect.width(), gw),
                           QColor(ctx.TH["muted"]))
            else:
                p.setPen(QPen(QColor(ctx.TH["grid_strong"] if ctx.TH["name"] == "day"
                                     else ctx.TH["border"]), 1))
                p.drawLine(opt.rect.topLeft(), opt.rect.topRight())
        # the stripe sits at the serial (v70) - at column 0 it landed on the
        # group's merged cell when grouped by anything but the serial
        if t.stripes and idx.column() == (t.serial_col if t.serial_col >= 0 else 0):
            r = idx.row()
            rb = t.row_bg[r] if r < len(t.row_bg) else None
            if t.is_picked_row(r):
                p.fillRect(QRect(opt.rect.left(), opt.rect.top(), 3, opt.rect.height()),
                           QColor(ctx.TH["sel_edge"]))
            elif rb in qt_theme.FLAG_COLORS:
                p.fillRect(QRect(opt.rect.left(), opt.rect.top(), 3, opt.rect.height()),
                           QColor(qt_theme.FLAG_COLORS[rb]))
            elif isinstance(rb, str) and rb.startswith("#"):
                p.fillRect(QRect(opt.rect.left(), opt.rect.top(), 3, opt.rect.height()),
                           QColor(rb))


class DataTable(QWidget):
    """A table like the Tk GridTable, with the same set_data() call:

        set_data(columns[(title, width)], rows, row_bg, row_bold, groups,
                 merge_cols, frozen=0, align=None, cell_bg=None, cell_fg=None,
                 cell_span=None, vertical_head=False, ...)

    row_bg keys: 'a'/'b' (zebra), 'count', 'hour', 'sub', a flag kind
    (teco/mrb/...) or a literal '#rrggbb'.  The first `frozen` columns stay
    put while scrolling sideways.  Clicking a row calls on_row_click(ri, row);
    right-click calls on_row_menu(ri, row, global_pos); a right-click on a
    heading calls on_head_menu(col, global_pos).  serial_col makes the row
    of the serial selected anywhere in the app light up here too.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.columns, self.rows = [], []
        self.row_bg, self.row_bold, self.align = [], [], []
        self.cell_bg, self.cell_fg = {}, {}
        self.frozen = 0
        self.stripes = False        # flag rows as a stripe, not a painted row
        self.group_line = 1         # px of the line over each box's first row
        self.tiles = False          # heat cells as rounded tiles (design)
        self.cell_bar = {}          # (r, c) -> (fraction, colour): a thin bar
        self._font_cache = {}
        self.pair_cols = set()      # first column of each W | F pair (one frame)
        self.row_base = 30          # row height at 100% zoom
        self.cell_loadbar = {}      # (r, c) -> (load, text): a bar, dashed 100% line
        self.tile_radius = 4
        self.dot_col0 = False       # a coloured dot before the first column (flag type)
        self.plain_cells = set()
        self.cell_tip = {}
        self.fit_max = None         # rows shown before the table scrolls itself
        self.groups, self.merge_cols, self.group_starts = [], [], set()
        # a second way to light rows: by the value in pick_col (an
        # operation, say); on_pick lets a page light it in its other tables
        self.pick_col = -1
        self.picked = ""
        self.on_pick = None
        self.serial_col = -1
        self.on_row_click = None
        self.on_row_menu = None
        self.on_head_menu = None
        self.on_cell_click = None   # (row, col) - v69: a click on a merged box cell
        self.on_head_click = None   # (col) - v69: a left click on a heading
        self._spans_now = set()
        self.fit_min = 5            # 2.2.0: rows shown at least, on a page (then the page scrolls)
        self.fit_width = False      # v69: every column shares the visible width, no side scroll
        self.grid = False           # v69: cell grid lines
        self.zoom = 1.0
        self.model = TableModel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QTableView(self)
        self.head = VerticalHeader(self.view)
        self.view.setHorizontalHeader(self.head)
        self.view.setModel(self.model)
        self._setup(self.view)
        lay.addWidget(self.view)
        # frozen columns: a second view laid over the left edge
        self.fview = QTableView(self.view)
        self.fhead = VerticalHeader(self.fview)
        self.fview.setHorizontalHeader(self.fhead)
        self.fview.setModel(self.model)
        self._setup(self.fview)
        self.fview.setFocusPolicy(Qt.NoFocus)
        self.fview.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.fview.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.fview.setStyleSheet("QTableView { border-right: 1px solid %s; }" % ctx.TH["border"])
        self.fview.verticalScrollBar().valueChanged.connect(self.view.verticalScrollBar().setValue)
        self.view.verticalScrollBar().valueChanged.connect(self.fview.verticalScrollBar().setValue)
        self.fview.hide()
        for v in (self.view, self.fview):
            v.clicked.connect(self._clicked)
            v.setContextMenuPolicy(Qt.CustomContextMenu)
            v.customContextMenuRequested.connect(lambda pos, vv=v: self._menu(vv, pos))
            v.horizontalHeader().sectionClicked.connect(
                lambda c: self.on_head_click and self.on_head_click(c))
            v.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
            v.horizontalHeader().customContextMenuRequested.connect(
                lambda pos, vv=v: self._head_menu(vv, pos))
        self.view.horizontalHeader().sectionResized.connect(self._sync_widths)
        self.view.horizontalHeader().setStretchLastSection(True)
        for v_ in (self.view, self.fview):
            v_.viewport().installEventFilter(self)
        for v in (self.view, self.fview):
            v.setItemDelegate(StripeDelegate(self, v))
        ctx.tables.append(self)

    def _setup(self, v):
        v.verticalHeader().hide()
        v.setSelectionBehavior(QAbstractItemView.SelectRows)
        v.setSelectionMode(QAbstractItemView.NoSelection)
        v.setEditTriggers(QAbstractItemView.NoEditTriggers)
        v.setShowGrid(False)
        v.setWordWrap(False)
        v.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        v.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        v.horizontalHeader().setHighlightSections(False)
        v.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        v.verticalHeader().setDefaultSectionSize(30)

    def font(self):
        f = QFont(qt_theme.FONT)
        f.setPointSizeF((9 if self.fit_width else 10) * self.zoom)
        return f

    def row_h(self):
        return int(self.row_base * self.zoom)

    def fit_rows(self, n_max):
        """Card height follows the rows (up to n_max, then it scrolls
        inside) - the way the design shows short tables short."""
        self.fit_max = n_max
        self._fit()

    def _fit(self):
        if not self.fit_max:
            return
        hh = self.view.horizontalHeader().sizeHint().height()
        if self.fit_width:           # 2.2.0: its heading is the tall, wrapping one
            hh = max(hh, int(62 * self.zoom))
        sb = 14 if sum(w for _t, w in self.columns) * self.zoom > self.width() else 4
        # v72: on a page, as many rows as the window has room for (at least
        # five); in a window of its own, the old n_max
        room = view_room(self)
        if room is None:
            n = max(1, min(len(self.rows), self.fit_max))
        else:
            fit_n = int((room - hh - sb - 2) // max(1, self.row_h()))
            n = max(1, min(len(self.rows), max(self.fit_min, fit_n)))
        h = hh + n * self.row_h() + sb + 2
        if h != self.height() or self.minimumHeight() != h:
            self.setFixedHeight(h)
            relayout_up(self)

    def is_picked_row(self, r):
        if self.serial_col >= 0 and ctx.selected_serial and r < len(self.rows):
            row = self.rows[r]
            return (self.serial_col < len(row)
                    and norm_text(str(row[self.serial_col])) == ctx.selected_serial)
        return False

    def bg_for(self, r):
        th = ctx.TH
        key = self.row_bg[r] if r < len(self.row_bg) else None
        if self.stripes and self.is_picked_row(r):
            # v75: the picked serial stands out like on the Waterfall - a
            # clear blue (over flags and notes too) and an edge at its serial
            base = th["row_a"] if r % 2 == 0 else th["row_b"]
            return mix(base, th["sel_edge"], 0.30 if th["name"] == "night" else 0.24)
        if isinstance(key, str) and key.startswith("#"):
            if not self.stripes:
                return key
            # v70: a note shows as a stripe, the serial in its colour and a
            # very soft tint of the row - it used to paint the whole row
            base = th["row_a"] if r % 2 == 0 else th["row_b"]
            return mix(base, key, 0.13)
        if key in qt_theme.FLAG_COLORS:
            if not self.stripes:
                return qt_theme.FLAG_COLORS[key]
            # v73: TECO / SCRAP / ... tint the whole row softly, like a note
            base = th["row_a"] if r % 2 == 0 else th["row_b"]
            return mix(base, qt_theme.FLAG_COLORS[key], 0.13)
        if self.serial_col >= 0 and ctx.selected_serial and r < len(self.rows):
            row = self.rows[r]
            if self.serial_col < len(row) and norm_text(str(row[self.serial_col])) == ctx.selected_serial:
                return th["sel_row"]
        if self.pick_col >= 0 and self.picked and r < len(self.rows):
            row = self.rows[r]
            if self.pick_col < len(row) and str(row[self.pick_col]) == self.picked:
                return th["sel_row"]
        return {"count": th["count"], "hour": th["hour"], "sub": th["subtotal"],
                "a": th["row_a"], "b": th["row_b"]}.get(key, th["row_a"] if r % 2 == 0 else th["row_b"])

    def set_data(self, columns, rows, row_bg=None, row_bold=None, groups=None,
                 merge_cols=None, frozen=0, plain_cells=None, cell_span=None,
                 head_span=None, align=None, plain_key="card", cell_bg=None,
                 cell_fg=None, row_sep=False, vertical_head=False):
        self._last_kw = dict(groups=groups, merge_cols=merge_cols, frozen=frozen,
                             plain_cells=plain_cells, cell_span=cell_span, head_span=head_span,
                             align=align, plain_key=plain_key, cell_bg=cell_bg, cell_fg=cell_fg,
                             row_sep=row_sep, vertical_head=vertical_head)
        self.columns = list(columns)
        self.rows = [list(r) for r in rows]
        self.cell_tip = {}
        self.row_bg = list(row_bg or [None] * len(rows))
        self.row_bold = list(row_bold or [False] * len(rows))
        self.align = list(align or [])
        self._theme = dict(ctx.TH)
        self.cell_bg = dict(cell_bg or {})
        self.cell_fg = dict(cell_fg or {})
        # empty cells of a heat map read as "nothing here": a dim shade
        dim = ctx.TH.get(plain_key if plain_key in ctx.TH else "dim", ctx.TH["dim"])
        for rc in (plain_cells or ()):
            self.cell_bg.setdefault(rc, dim)
        self.plain_cells, self.plain_key = set(plain_cells or ()), plain_key
        self.frozen = min(frozen or 0, len(columns))
        self.head.vertical = self.fhead.vertical = bool(vertical_head)
        spans = dict(head_span or {})
        covered = {c + k for c, n in spans.items() for k in range(1, n)}
        for h in (self.head, self.fhead):
            h.spans, h.covered = spans, covered
            h.level = self.frozen if vertical_head else 0
        # a grid of day / week tiles keeps the last day as wide as the others
        self.view.horizontalHeader().setStretchLastSection(not self.tiles)
        self.model.reset()
        for v in (self.view, self.fview):
            v.clearSpans()
            v.setFont(self.font())
            v.verticalHeader().setDefaultSectionSize(self.row_h())
            hh = v.horizontalHeader()
            for c, (_t, w) in enumerate(self.columns):
                v.setColumnWidth(c, int(w * self.zoom * 1.12))
            v.setShowGrid(bool(self.grid))
            if self.grid:
                v.setStyleSheet("QTableView { gridline-color: %s; }" % ctx.TH["border"])
            hh.updateGeometry()
            v.updateGeometries()
        self._spans_now = set((cell_span or {}).keys())
        for (r, c), n in (cell_span or {}).items():
            for v in (self.view, self.fview):
                v.setSpan(r, c, 1, n)
        # boxes: a line over each, and the merge columns written once per box
        self.groups = list(groups or [])
        self.merge_cols = list(merge_cols or [])
        self.group_starts = {a for a, _b in self.groups}
        for a, b in self.groups:
            if b > a:
                for c in self.merge_cols:
                    for v in (self.view, self.fview):
                        v.setSpan(a, c, b - a + 1, 1)
        self._apply_fit_width()
        self._layout_frozen()
        self._fit()

    def _apply_fit_width(self):
        """Share the visible width between the columns in proportion to
        their natural widths; headings wrap onto two lines (v69 RW D1)."""
        if not self.fit_width or not self.columns:
            if getattr(self, "_was_fit", False):
                # back to full widths: side scroll, one-line headings
                self._was_fit = False
                hh = self.view.horizontalHeader()
                hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                hh.setMinimumHeight(0)
                hh.setMaximumHeight(16777215)
                self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            return
        self._was_fit = True
        avail = self.view.viewport().width() or self.width()
        if avail < 200:
            return
        fm = QFontMetrics(self.font())
        bf = QFont(self.font()); bf.setBold(True)
        hfm = QFontMetrics(bf)
        sample = self.rows[:300]
        nat = []
        for c, (t, _w) in enumerate(self.columns):
            words = head_text(t).split() or [""]
            hw = max(hfm.horizontalAdvance(w) for w in words) + 16
            cw = 0
            for r, row in enumerate(sample):
                if c < len(row) and row[c] not in (None, "") and (r, c) not in self._spans_now:
                    cw = max(cw, fm.horizontalAdvance(str(row[c])))
            nat.append(max(hw, cw + 26, 24))
        avail -= 2 + self.view.verticalScrollBar().sizeHint().width()
        widths = list(nat)
        if sum(nat) > avail:
            # short columns (codes, dates, hours) keep their width; the long
            # text columns give up what is needed, in proportion
            longc = [c for c, w in enumerate(nat) if w > 150]
            fixed = sum(w for c, w in enumerate(nat) if c not in longc)
            room = max(len(longc) * 50, avail - fixed)
            ltot = float(sum(nat[c] for c in longc)) or 1.0
            for c in longc:
                widths[c] = max(50, int(nat[c] / ltot * room))
        else:
            extra = avail - sum(nat)
            tot = float(sum(nat))
            widths = [int(w + extra * w / tot) for w in nat]
        if sum(widths) > avail:
            k = avail / float(sum(widths))
            widths = [max(22, int(w * k)) for w in widths]
        for c, w in enumerate(widths):
            self.view.setColumnWidth(c, w)
        hh = self.view.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap)
        hh.setFixedHeight(int(62 * self.zoom))
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def clear(self):
        self.set_data([], [])

    def refresh_selection(self):
        self.view.viewport().update()
        self.fview.viewport().update()

    def set_picked(self, v):
        self.picked = v or ""
        self.refresh_selection()

    def refresh_theme(self):
        self.fview.setStyleSheet("QTableView { border-right: 1px solid %s; }" % ctx.TH["border"])
        self._remap_theme()
        self.refresh_selection()

    def _remap_theme(self):
        """v71: colours a page wrote into the table for the OLD theme (text,
        flag text, dividers...) follow the switch to day / night - a table
        that was not rebuilt kept night's white text on day's white rows."""
        old = getattr(self, "_theme", None)
        if not old or old.get("name") == ctx.TH["name"]:
            self._theme = dict(ctx.TH)
            return
        m = {}
        for k, v in old.items():
            nv = ctx.TH.get(k)
            if isinstance(v, str) and v.startswith("#") and isinstance(nv, str) and nv != v:
                m.setdefault(v.upper(), nv)
        night = ctx.TH["name"] != "day"
        a, b = ((qt_theme.FLAG_TEXT_DAY, qt_theme.FLAG_TEXT_NIGHT) if night
                else (qt_theme.FLAG_TEXT_NIGHT, qt_theme.FLAG_TEXT_DAY))
        for k, v in a.items():
            if k in b:
                m.setdefault(v.upper(), b[k])
        day_gap, night_gap = "#C9CFD8", "#3A3F48"       # week / gap dividers
        if night:
            m.setdefault(day_gap, night_gap)
        else:
            m.setdefault(night_gap, day_gap)
        if m:
            fix = lambda c: m.get(str(c).upper(), c) if isinstance(c, str) else c
            self.cell_fg = {rc: fix(c) for rc, c in self.cell_fg.items()}
            self.cell_bg = {rc: fix(c) for rc, c in self.cell_bg.items()}
            self.model.reset()
        self._theme = dict(ctx.TH)

    def set_zoom(self, z):
        self.zoom = max(0.5, min(2.5, z))
        kw = dict(getattr(self, "_last_kw", {}))
        tips = self.cell_tip          # zooming must not lose the hover texts
        self.set_data(self.columns, self.rows, self.row_bg, self.row_bold, **kw)
        self.cell_tip = tips

    def eventFilter(self, obj, ev):
        """v70: Ctrl + mouse wheel zooms on every table - through the page's
        Zoom control when it has one (so all its tables follow), or this
        table alone (MRB, Settings)."""
        if ev.type() == QEvent.Wheel and ev.modifiers() & Qt.ControlModifier:
            up = ev.angleDelta().y() > 0
            w = self.parentWidget()
            while w is not None:
                zc = getattr(w, "zoom", None)
                if isinstance(zc, ZoomControl):
                    zc.step(10 if up else -10)
                    return True
                w = w.parentWidget()
            self.set_zoom(round(self.zoom * 10 + (1 if up else -1)) / 10.0)
            return True
        return super().eventFilter(obj, ev)

    def _layout_frozen(self):
        if self.frozen <= 0 or not self.columns:
            self.fview.hide()
            return
        for c in range(len(self.columns)):
            self.fview.setColumnHidden(c, c >= self.frozen)
        w = sum(self.view.columnWidth(c) for c in range(self.frozen))
        self.fview.setGeometry(self.view.frameWidth(), self.view.frameWidth(), w,
                               self.view.viewport().height() + self.view.horizontalHeader().height())
        self.fview.horizontalHeader().setFixedHeight(self.view.horizontalHeader().height())
        self.fview.show()
        self.fview.raise_()

    def _sync_widths(self, idx, _old, new):
        if idx < self.frozen:
            self.fview.setColumnWidth(idx, new)
            self._layout_frozen()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.fit_width:
            QTimer.singleShot(0, self._apply_fit_width)
        QTimer.singleShot(0, self._layout_frozen)

    def _clicked(self, idx):
        r = idx.row()
        if 0 <= r < len(self.rows):
            if self.serial_col >= 0 and self.serial_col < len(self.rows[r]):
                ctx.toggle_selected_serial(self.rows[r][self.serial_col])
            if self.pick_col >= 0 and self.pick_col < len(self.rows[r]):
                v = str(self.rows[r][self.pick_col])
                v = "" if v == self.picked else v
                if self.on_pick:
                    self.on_pick(v)
                else:
                    self.set_picked(v)
            if self.on_cell_click:
                self.on_cell_click(r, idx.column())
            if self.on_row_click:
                self.on_row_click(r, self.rows[r])

    def _menu(self, v, pos):
        idx = v.indexAt(pos)
        r = idx.row()
        if not (0 <= r < len(self.rows)):
            return
        gpos = v.viewport().mapToGlobal(pos)
        if self.on_row_menu:
            self.on_row_menu(r, self.rows[r], gpos)
        elif self.serial_col >= 0 and "serial_menu" in ctx.hooks:
            ctx.hooks["serial_menu"](self.rows[r][self.serial_col], gpos)

    def _head_menu(self, v, pos):
        if self.on_head_menu:
            c = v.horizontalHeader().logicalIndexAt(pos)
            self.on_head_menu(c, v.horizontalHeader().mapToGlobal(pos))


def autofit_columns(columns, rows, min_w=42, max_w=260, pad=22, sample=1200):
    """Each column as wide as its heading and values need."""
    fm = QFontMetrics(QFont(qt_theme.FONT, 9))
    fb = QFontMetrics(QFont(qt_theme.FONT, 9, QFont.Bold))
    if rows and len(rows) > sample:
        step = len(rows) / float(sample)
        measured = [rows[int(i * step)] for i in range(sample)]
    else:
        measured = rows
    out = []
    for ci, (title, _w) in enumerate(columns):
        best = max((fb.horizontalAdvance(line) for line in str(title).split("\n")), default=0)
        for row in measured:
            if ci < len(row) and row[ci] not in (None, ""):
                best = max(best, fm.horizontalAdvance(str(row[ci])))
        out.append((title, max(min_w, min(max_w, best + pad))))
    return out


def _natural_h(wd):
    """The height a widget needs at the width it has (a row of chips that
    wraps needs two lines) - not what a stretched parent handed it."""
    fixed = wd.minimumHeight() == wd.maximumHeight() and wd.maximumHeight() < 16777215
    if fixed:
        return wd.maximumHeight()
    if wd.width() > 0:
        if wd.hasHeightForWidth():
            return max(wd.heightForWidth(wd.width()), wd.minimumSizeHint().height())
        lay = wd.layout()
        if lay is not None and lay.hasHeightForWidth():
            m = wd.contentsMargins()
            return lay.heightForWidth(wd.width()) + m.top() + m.bottom()
    return max(wd.sizeHint().height(), wd.minimumHeight())


def _item_h(it):
    """Height a layout item really takes: its natural height, or less when
    it is shown smaller - never the extra a stretched card handed it."""
    wd = it.widget()
    if wd is not None:
        if wd.isHidden():
            return None
        return _natural_h(wd)
    if it.layout() is not None:
        lay = it.layout()
        if lay.hasHeightForWidth() and lay.geometry().width() > 0:
            return lay.heightForWidth(lay.geometry().width())
        return lay.sizeHint().height()
    return 0 if it.spacerItem() is not None else it.sizeHint().height()


def _find_path(lay, target):
    """[(layout, index), ...] from `lay` down to the item holding target."""
    for i in range(lay.count()):
        it = lay.itemAt(i)
        if it.widget() is target:
            return [(lay, i)]
        sub = it.layout()
        if sub is not None:
            p = _find_path(sub, target)
            if p:
                return [(lay, i)] + p
    return None


def _vertical(lay):
    return isinstance(lay, QBoxLayout) and lay.direction() in (QBoxLayout.TopToBottom,
                                                                QBoxLayout.BottomToTop)


def view_room(w):
    """v72: the height `w` may take so that its page fits the window with no
    page scrolling: the visible page height less what sits above it and
    what has to stay under it (the rest of its card, anything below the
    modules).  None when `w` is not on a scrolling page (a module opened in
    a window of its own).

    v73: worked out from the LAYOUTS (what comes before and after w at
    each level), not from where w happens to sit right now.  Measuring
    positions fed back on itself - a taller table moved things, the
    moved things made the room smaller, the table shrank, and so on: the
    Waterfall flickered without end."""
    sp = None
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, ScrollPage):
            sp = p
            break
        p = p.parentWidget()
    if sp is None or not w.isVisible():
        return None
    above = below = 0
    child, p = w, w.parentWidget()
    while p is not None and p is not sp.viewport():
        lay = p.layout()
        path = _find_path(lay, child) if lay is not None else None
        if path:
            for sub, idx in path:
                m = sub.contentsMargins()
                above += m.top()
                below += m.bottom()
                if not _vertical(sub):
                    continue
                for j in range(sub.count()):
                    if j == idx:
                        continue
                    h = _item_h(sub.itemAt(j))
                    if h is None:
                        continue
                    if j < idx:
                        above += h + (sub.spacing() if h else 0)
                    else:
                        below += h + (sub.spacing() if h else 0)
        if p is sp.inner:
            break
        child, p = p, p.parentWidget()
    return sp.viewport().height() - above - below - 4     # a hair to spare


class ScrollPage(QScrollArea):
    """A page that scrolls as a whole - but v72: only when it has to.  One
    module fits the window and only its table scrolls; a table that
    reaches its end never drags the page along."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.inner = QWidget()
        self.inner.setObjectName("Page")
        self.setWidget(self.inner)
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(20, 16, 20, 16)
        self.lay.setSpacing(12)
        self._refit_due = False
        self.inner.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.inner and ev.type() == QEvent.LayoutRequest:
            self.schedule_refit()
        return False

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.schedule_refit()

    def schedule_refit(self):
        if not self._refit_due:
            self._refit_due = True
            QTimer.singleShot(0, self.refit)

    def refit(self):
        """Tables and the waterfall take the height the window has."""
        self._refit_due = False
        try:
            vw = self.viewport().width() - self.lay.contentsMargins().left() \
                - self.lay.contentsMargins().right()
            for g in self.inner.findChildren(ModuleGrid):
                g.fit_widths(vw)
            for t in self.inner.findChildren(DataTable):
                if t.fit_max and t.isVisible():
                    t._fit()
            for a in self.inner.findChildren(QAbstractScrollArea):
                f = getattr(a, "refit_view", None)
                if f is not None and a.isVisible():
                    f()
            for g in self.inner.findChildren(ModuleGrid):
                g.fit_height()
        except RuntimeError:
            pass            # a page being torn down

    def wheelEvent(self, ev):
        # v70: Ctrl + wheel on the page (not on a table) uses the page's Zoom
        if ev.modifiers() & Qt.ControlModifier:
            zc = getattr(self.parentWidget(), "zoom", None)
            if isinstance(zc, ZoomControl):
                zc.step(10 if ev.angleDelta().y() > 0 else -10)
            ev.accept()
            return
        # v72: the wheel over a table that scrolls stays with that table -
        # at its end it used to carry on and pull the whole page
        d = ev.angleDelta()
        vertical = abs(d.y()) >= abs(d.x())
        w = self.viewport().childAt(ev.position().toPoint())
        while w is not None and w is not self:
            if isinstance(w, QAbstractScrollArea):
                sb = w.verticalScrollBar() if vertical else w.horizontalScrollBar()
                if sb is not None and sb.maximum() > 0:
                    ev.accept()
                    return
            w = w.parentWidget()
        super().wheelEvent(ev)


def mix(a, b, t):
    try:
        ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    except (TypeError, ValueError, IndexError):
        return a
    t = max(0.0, min(1.0, t))
    return "#%02X%02X%02X" % tuple(int(round(x + (y - x) * t)) for x, y in zip(ca, cb))


class BarList(QWidget):
    """Horizontal bars, one per line: name, bar, value (the design's
    "Overall cause op share" and "By rework work centre").  v69 (CA1):
    an item may carry its own colour; with `bold_code` the first word of
    the name (the op number) and the % after " · " are bold."""
    ROW = 32

    def __init__(self, color=None, bold_code=False, bold_pct=None):
        super().__init__()
        self.items = []
        self.color = color
        self.bold_code = bold_code
        self.bold_pct = bold_code if bold_pct is None else bold_pct
        self.zoom = 1.0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def row_h(self):
        return int(self.ROW * self.zoom)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            w = self.parentWidget()
            while w is not None:
                zc = getattr(w, "zoom", None)
                if isinstance(zc, ZoomControl):
                    zc.step(10 if ev.angleDelta().y() > 0 else -10)
                    ev.accept()
                    return
                w = w.parentWidget()
        super().wheelEvent(ev)

    def set_zoom(self, z):
        """v70: follows the page's Zoom (and Ctrl + wheel) like the tables."""
        self.zoom = max(0.5, min(2.5, z))
        f = QFont(qt_theme.FONT)
        f.setPointSizeF(10 * self.zoom)
        self.setFont(f)
        self.set_items(self.items)

    def set_items(self, items):
        """items: [(name, fraction 0..1, value text, tooltip[, colour])]"""
        self.items = list(items)
        self.setFixedHeight(max(1, len(self.items)) * self.row_h() + 6)
        self.update()

    def clear(self):
        self.set_items([])

    def event(self, ev):
        if ev.type() == QEvent.ToolTip:
            i = int(ev.pos().y() // self.row_h())
            if 0 <= i < len(self.items) and len(self.items[i]) > 3 and self.items[i][3]:
                QToolTip.showText(ev.globalPos(), self.items[i][3], self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        name_w = int(min(260, w * 0.36))
        fm = QFontMetrics(self.font())
        bf = QFont(self.font())
        bf.setBold(True)
        bfm = QFontMetrics(bf)
        val_w = max([bfm.horizontalAdvance(str(it[2])) for it in self.items] or [40]) + 16
        bar_x, bar_w = name_w + 8, max(20, w - name_w - 8 - val_w - 8)
        col = QColor(self.color or th["accent"])
        for i, it in enumerate(self.items):
            name, frac, val = str(it[0]), it[1], str(it[2])
            y = i * self.row_h()
            p.setPen(QColor(th["text"]))
            if self.bold_code and " " in name:
                code, rest = name.split(" ", 1)
                p.setFont(bf)
                p.drawText(QRect(0, y, name_w, self.row_h()), Qt.AlignVCenter | Qt.AlignLeft, code)
                cw = bfm.horizontalAdvance(code + " ")
                p.setFont(self.font())
                p.drawText(QRect(cw, y, name_w - cw, self.row_h()), Qt.AlignVCenter | Qt.AlignLeft,
                           fm.elidedText(rest, Qt.ElideRight, name_w - cw))
            else:
                p.drawText(QRect(0, y, name_w, self.row_h()), Qt.AlignVCenter | Qt.AlignLeft,
                           fm.elidedText(name, Qt.ElideRight, name_w))
            bh = 12
            by = y + (self.row_h() - bh) / 2.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(th["dim"] if th["name"] == "day" else th["surf2"]))
            p.drawRoundedRect(QRectF(bar_x, by, bar_w, bh), 6, 6)
            p.setBrush(QColor(it[4]) if len(it) > 4 and it[4] else col)
            p.drawRoundedRect(QRectF(bar_x, by, max(6.0, bar_w * max(0.0, min(1.0, frac))), bh),
                              6, 6)
            p.setPen(QColor(th["text"]))
            if self.bold_pct and " · " in val:
                head, tail = val.rsplit(" · ", 1)
                p.setFont(bf)
                p.drawText(QRect(w - val_w, y, val_w, self.row_h()), Qt.AlignVCenter | Qt.AlignRight,
                           tail)
                tw = bfm.horizontalAdvance(tail)
                p.setFont(self.font())
                p.drawText(QRect(w - val_w, y, val_w - tw, self.row_h()),
                           Qt.AlignVCenter | Qt.AlignRight, head + " · ")
            else:
                p.drawText(QRect(w - val_w, y, val_w, self.row_h()), Qt.AlignVCenter | Qt.AlignRight,
                           val)
        p.end()


def page_layout(page):
    """Every page scrolls as a whole (the design): returns the layout the
    page's head, filters and modules go into."""
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    sp = ScrollPage()
    sp.lay.setContentsMargins(26, 18, 26, 18)
    sp.lay.setSpacing(12)
    outer.addWidget(sp)
    page.scroll = sp
    return sp.lay


def page_head(lay, title, sub, *actions):
    head = QHBoxLayout()
    tl = QVBoxLayout()
    tl.setSpacing(2)
    tl.addWidget(label(title, obj="PageTitle"))
    # v73: no description under the page name - the room goes to the tables
    head.addLayout(tl)
    head.addStretch(1)
    for a in actions:
        head.addWidget(a)
    lay.addLayout(head)
    return head


def export_button(slot):
    b = IconButton("download", "", slot, text="  Export to Excel")
    b.setProperty("primary", True)
    b.setFixedHeight(40)
    return b


def serial_menu(serial_no, gpos):
    """Right-click on a serial anywhere: what can be done with it."""
    m = QMenu(ctx.window)
    a = m.addAction("Serial %s" % serial_no)
    a.setEnabled(False)
    m.addSeparator()
    m.addAction("Open details / my note...", lambda: SerialDetailDialog(ctx.window, serial_no).exec())
    if "mrbscrap_load_serial" in ctx.hooks:
        m.addAction("Edit in MRB / SCRAP / TECO...",
                    lambda: ctx.hooks["mrbscrap_load_serial"](serial_no))
    m.addAction("Copy serial", lambda: QApplication.clipboard().setText(str(serial_no)))
    m.exec(gpos)


ctx.hooks["serial_menu"] = serial_menu


class SerialDetailDialog(QDialog):
    """The serial's MRB / SCRAP / TECO record, the user's own note (colour,
    hide from others) and what other users wrote - same as v67."""

    def __init__(self, parent, serial_no):
        super().__init__(parent)
        from wip_core import USER_NOTE_COLORS
        self.setWindowTitle("Serial %s" % serial_no)
        self.setMinimumWidth(480)
        self.serial_no = serial_no
        self.sk = norm_text(serial_no)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Serial %s" % serial_no, obj="PageTitle"))
        lay.addWidget(label("MRB / SCRAP / TECO", section=True))
        flag = ctx.db.flag_get(self.sk)
        if flag:
            k = flag["kind"]
            t = label(FLAG_KIND_TITLE.get(k, k))
            t.setStyleSheet("font-weight: 700; color: %s;" % ctx.flag_text(k))
            lay.addWidget(t)
            lay.addWidget(label(flag["reason"] or "(no reason written)", wrap=True))
            extra = ["Date: %s" % (flag["entry_date"] or "-")]
            if k == "teco":
                extra.append("MRB probability: %s" % (flag["mrb_prob"] or "-"))
                extra.append("SCRAP probability: %s" % (flag["scrap_prob"] or "-"))
            lay.addWidget(label("    ".join(extra), muted=True))
        else:
            lay.addWidget(label("No MRB/SCRAP/TECO/REPAIR/NOTE entry yet for this serial.",
                                muted=True))
        me = ctx.username()
        self.row = (ctx.db.user_note_row(self.sk, me) if me else
                    {"note": "", "color": None, "private": 0})
        lay.addWidget(label("My note", section=True))
        self.note = QPlainTextEdit(self.row["note"] or "")
        self.note.setFixedHeight(70)
        self.note.setEnabled(bool(me))
        lay.addWidget(self.note)
        sw = QHBoxLayout()
        sw.setSpacing(4)
        sw.addWidget(label("Colour", muted=True))
        self.swatches = []
        for _lbl, hexv in USER_NOTE_COLORS:
            b = QPushButton("")
            b.setFixedSize(22, 22)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, h=hexv: self.pick_color(h))
            sw.addWidget(b)
            self.swatches.append((b, hexv))
        self.private = Toggle("Hide from others", checked=bool(self.row.get("private")))
        sw.addSpacing(10)
        sw.addWidget(self.private)
        sw.addStretch(1)
        swh = QWidget()
        swh.setLayout(sw)
        lay.addWidget(swh)
        self.pick_color(self.row.get("color") or USER_NOTE_COLORS[0][1])
        if me:
            see_all = ctx.is_admin() and ctx.show_all_notes
            others = [n for n in ctx.db.notes_for_serial(self.sk, me, all_users=see_all)
                      if n["username"] != me]
            if others:
                lay.addWidget(label("Other users' notes (admin view - includes hidden)"
                                    if see_all else "Other users' notes", section=True))
                for n in others:
                    o = label("%s%s:  %s" % (n["full_name"] or n["username"],
                                             "  [hidden]" if n.get("private") else "",
                                             n["note"]), wrap=True)
                    if n.get("color"):
                        o.setStyleSheet("color: %s;" % n["color"])
                    lay.addWidget(o)
        bt = []
        if "mrbscrap_load_serial" in ctx.hooks:
            bt.append(button("Edit entry" if flag else "Add entry", primary=True, slot=self._go))
        bt += [button("Save my note", primary=not bt, slot=self._save), None,
               button("Close", slot=self.reject)]
        lay.addWidget(hbox(*bt))

    def pick_color(self, hexv):
        self.color = hexv
        self.note.setStyleSheet("color: %s;" % hexv)
        for b, hv in self.swatches:
            on = hv == hexv
            b.setStyleSheet("QPushButton { background: %s; border: %dpx solid %s; border-radius: 5px; }"
                            % (hv, 3 if on else 1, ctx.TH["text"] if on else ctx.TH["border"]))

    def _store(self):
        me = ctx.username()
        if me:
            ctx.db.set_user_note(self.sk, me, self.note.toPlainText(), self.color,
                                 self.private.isChecked())

    def _save(self):
        self._store()
        self.accept()

    def _go(self):
        self._store()
        self.accept()
        ctx.hooks["mrbscrap_load_serial"](self.serial_no)


class ZoomControl(QWidget):
    """- / 100% / + for the tables of one page."""

    def __init__(self, get_tables, start=100):
        super().__init__()
        self.get_tables = get_tables
        self.z = start
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(label("Zoom", muted=True))
        lay.addWidget(button("−", icon_only=True, slot=lambda: self.step(-10)))
        self.lbl = label("%d%%" % start)
        lay.addWidget(self.lbl)
        lay.addWidget(button("+", icon_only=True, slot=lambda: self.step(10)))

    def step(self, d):
        self.z = max(50, min(250, self.z + d))
        self.lbl.setText("%d%%" % self.z)
        self.apply_zoom()

    def apply_zoom(self):
        for t in self.get_tables():
            if t is not None and abs(t.zoom - self.z / 100.0) > 1e-6:
                t.set_zoom(self.z / 100.0)


class HeatLegend(QWidget):
    """few [green..amber..red] many   [purple] far above the rest"""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(26)
        self.setMinimumWidth(420)

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fm = QFontMetrics(self.font())
        p.setPen(QColor(th["muted"]))
        x = 0
        p.drawText(QRect(x, 0, 40, 26), Qt.AlignVCenter | Qt.AlignLeft, "few")
        x += fm.horizontalAdvance("few") + 10
        for i in range(160):
            p.fillRect(QRect(x + i, 10, 1, 6), QColor(heat_color(i / 159.0)))
        x += 170
        p.drawText(QRect(x, 0, 60, 26), Qt.AlignVCenter | Qt.AlignLeft, "many")
        x += fm.horizontalAdvance("many") + 22
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(OUTLIER_COLOR))
        p.drawRoundedRect(QRectF(x, 7, 12, 12), 2, 2)
        p.setPen(QColor(th["muted"]))
        p.drawText(QRect(x + 18, 0, 200, 26), Qt.AlignVCenter | Qt.AlignLeft, "far above the rest")
        p.end()


class KpiInline(QFrame):
    """The design's small total: TITLE and a big number side by side, a
    coloured line along the top (IN WIP 48, WAITING 19...)."""

    def __init__(self, title, color, compact=False):
        super().__init__()
        self.setProperty("card", True)
        self.color = color
        self.compact = compact
        lay = QHBoxLayout(self)
        # v73: compact = one slim line, it sits in the page's title row
        lay.setContentsMargins(*((10, 2, 12, 2) if compact else (16, 10, 18, 10)))
        lay.setSpacing(8 if compact else 12)
        lay.addWidget(label(title, section=True))
        self.val = QLabel("-")
        lay.addWidget(self.val)
        self.paint()

    def set(self, v):
        self.val.setText(str(v))

    def paint(self):
        col = ctx.TH.get(self.color, self.color) if self.color else ctx.TH["text"]
        self.setStyleSheet("KpiInline { border-top: 3px solid %s; }" % (col if self.color
                                                                          else ctx.TH["border"]))
        self.val.setStyleSheet("font-size: %dpt; font-weight: 700; color: %s;"
                               % (13 if self.compact else 19, col))


class StatBox(QFrame):
    """A summary box (v69 FC4 / B1K1): a coloured line on top, TITLE, a big
    value and a small line under it.  color_fn gives the line colour."""

    def __init__(self, title, color_fn):
        super().__init__()
        self.setProperty("card", True)
        self.color_fn = color_fn
        self.setMinimumWidth(170)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        lay.addWidget(label(title, section=True))
        self.val = QLabel("-")
        self.sub = label("", muted=True)
        lay.addWidget(self.val)
        lay.addWidget(self.sub)
        self.paint()

    def set(self, v, sub=""):
        self.val.setText(v)
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))

    def paint(self):
        self.setStyleSheet("StatBox { border-top: 3px solid %s; }" % self.color_fn())
        self.val.setStyleSheet("font-size: 16pt; font-weight: 600; color: %s;" % ctx.TH["text"])


class MetricBox(QFrame):
    """A number in a box (PART COUNT, TOTAL RW HOUR...)."""

    def __init__(self, title, color_key=None):
        super().__init__()
        self.setProperty("card", True)
        self.color_key = color_key
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        lay.addWidget(label(title, section=True))
        self.val = QLabel("0")
        lay.addWidget(self.val)
        self.paint()

    def set(self, v):
        self.val.setText(str(v))

    def paint(self):
        col = ctx.TH.get(self.color_key) if self.color_key else ctx.TH["text"]
        self.val.setStyleSheet("font-size: 18pt; font-weight: 700; color: %s;" % col)

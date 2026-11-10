# -*- coding: utf-8 -*-
"""LEAD TIME ANALYSIS - the Qt page (2.2.0).

What used to be the LEAD TIME / QUEUE / FLOW EFFICIENCY module of the
WATERFALL page, on a page of its own.  The figures are worked out exactly
as before (wip_core.lead_time_analysis: completion to completion in clock
time; OPERATION TIME = machine time or - where there is none - labour,
production bookings only, never a rework's hours).

Four modules, each switched on and off in the MODULES row:

    STEPS             one part, one row per operation of its route
    OPERATION GROUPS  named stretches of a route (TURN = 130 -> 150), over
                      one or several parts; the group's own average and
                      each part's beside it.  The sets are saved for
                      everybody (wip_core.lt_groups_*).
    ROUTE PROFILE     a column per operation, operation time under queue - where
                      the time goes along the route.  Click a column (or a
                      row of STEPS) to look at that one step.
    SERIALS           a column per serial, longest first, against the
                      average: the whole route END TO END (the first
                      operation's completion to the last one's X - the
                      same figure as LEAD at the top), or the step picked.

The date range picks the data one of three ways: the operation records
inside it, the serials seen inside it (all their records), or the serials
SOLD inside it (all their records).  Shown in days; hours on request.
"""

import math
import re
import traceback

from PySide6.QtCore import Qt, QTimer, QRect, QRectF, QPointF, QSize, QEvent
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QMessageBox, QFileDialog,
                               QComboBox, QLineEdit, QDialog, QScrollArea, QFrame,
                               QToolTip, QSizePolicy, QListWidget, QListWidgetItem,
                               QAbstractItemView, QStyledItemDelegate, QStyle, QPushButton,
                               QLabel, QInputDialog)

from wip_core import (ALL, export_leadtime_to_excel, format_wf_hours, lead_time_analysis,
                      lead_group_analysis, lt_groups_load, lt_groups_save, lt_groups_delete,
                      lt_groups_normalize, lt_part_ops, lt_runs, lt_run_bounds,
                      lead_group_values, lead_entries, lead_keep_mask, LT_GROUP_COLORS,
                      norm_text, time_mode_to_date_range, save_prefs)
from qt_widgets import (ctx, ModuleCard, Card, Splitter, DataTable, FilterBar, Toggle, Seg, FlowLayout,
                        label, button, hbox, autofit_columns, KpiInline, ModuleGrid, page_layout,
                        page_head, export_button, ZoomControl, serial_menu)
from qt_theme import FONT

# lead blue, cutting green, queue yellow - one set per theme
LT_SOFT_NIGHT = {"lead": "#6FA8FF", "cut": "#4CC67A", "queue": "#E8B53E"}
LT_SOFT_DAY = {"lead": "#1F63D6", "cut": "#1E8E46", "queue": "#A87400"}
MIN_ROWS = 14        # a table shows at least this many rows; beyond the window the page scrolls
DATE_MODES = [("By op date", "ops"), ("By serial", "serial"), ("Sold in range", "sold")]
MODE_TIP = ("By op date: only the operation records inside the date range.\n"
            "By serial: the serials with an entry inside the range, with all their "
            "operations.\n"
            "Sold in range: only the serials SOLD inside the range (shipment report), "
            "with all their operations.")


def soft():
    return LT_SOFT_NIGHT if ctx.TH["name"] != "day" else LT_SOFT_DAY


def lt_chip(col, k, night):
    """A soft chip: the colour as a light tint (stronger for larger k) on
    the card, the number in the full colour."""
    c = QColor(col)
    base = QColor(ctx.TH["card"])
    a = (0.14 + 0.30 * max(0.0, min(1.0, k))) if night else (0.10 + 0.22 * max(0.0, min(1.0, k)))
    mix = QColor(int(base.red() + (c.red() - base.red()) * a),
                 int(base.green() + (c.green() - base.green()) * a),
                 int(base.blue() + (c.blue() - base.blue()) * a))
    return mix.name(), col


def eff_text(eff, overlap=False):
    if eff is None:
        return ""
    return ("%.0f%% *" if overlap else "%.0f%%") % (eff * 100.0)


# =====================================================================
#  A COLUMN CHART: operation time under queue, an average line
# =====================================================================
class ColumnChart(QWidget):
    """One column per item, operation time (green) with queue (yellow) on top, a
    dashed line at the average.  Painted by hand in the theme's colours;
    it has no scrolling of its own, so the wheel over it moves the page.

        set_data([{"key", "label", "cut", "queue", "tip"}], unit, avg)

    Hovering names the column; a click calls on_pick(key), a right-click
    on_menu(key, global position).  `picked` keeps one column lit."""
    BASE_H = 250

    def __init__(self, labels=True):
        super().__init__()
        self.items, self.unit, self.avg = [], "days", None
        self.labels = labels            # the label under every column
        self.zoom = 1.0
        self.picked = None
        self.hover = -1
        self.empty_text = ""
        self.on_pick = None
        self.on_menu = None
        self._geo = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(self.BASE_H)

    def set_zoom(self, z):
        self.zoom = max(0.5, min(2.5, z))
        self.setFixedHeight(int(self.BASE_H * self.zoom))
        self.update()

    def set_data(self, items, unit, avg=None, empty_text=""):
        self.items, self.unit, self.avg = list(items), unit, avg
        self.empty_text = empty_text
        self.hover = -1
        self.update()

    def clear(self, text=""):
        self.set_data([], self.unit, None, text)

    def font_(self, size, bold=False):
        f = QFont(FONT)
        f.setPointSizeF(max(6.0, size * self.zoom))
        f.setBold(bold)
        return f

    @staticmethod
    def nice_top(v):
        """An axis end a little over v: 1, 2, 2.5, 5 x a power of ten."""
        if v <= 0:
            return 1.0
        e = 10.0 ** math.floor(math.log10(v))
        for k in (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
            if e * k >= v:
                return e * k
        return e * 10.0

    def geometry_(self):
        """(plot rect, column pitch, column width, axis top in shown units)."""
        k = 24.0 if self.unit == "days" else 1.0
        top = max([(it["cut"] + it["queue"]) / k for it in self.items] + [(self.avg or 0) / k])
        top = self.nice_top(top * 1.05)
        fm = QFontMetrics(self.font_(8))
        left = fm.horizontalAdvance("%.2f" % top) + 12
        bottom = 8
        if self.labels and self.items:
            bottom = max(fm.horizontalAdvance(str(it["label"])) for it in self.items) + 12
            bottom = min(bottom, int(70 * self.zoom))
        plot = QRectF(left, 14 * self.zoom, max(10, self.width() - left - 8),
                      max(10, self.height() - 14 * self.zoom - bottom))
        n = max(1, len(self.items))
        pitch = plot.width() / float(n)
        colw = max(1.0, min(pitch * 0.72, 34 * self.zoom))
        return plot, pitch, colw, top, k

    def index_at(self, x):
        if not self.items or self._geo is None:
            return -1
        plot, pitch = self._geo[0], self._geo[1]
        if x < plot.left() or x > plot.right():
            return -1
        return max(0, min(len(self.items) - 1, int((x - plot.left()) // pitch)))

    def mouseMoveEvent(self, ev):
        i = self.index_at(ev.position().x())
        if i != self.hover:
            self.hover = i
            self.setCursor(Qt.PointingHandCursor if (i >= 0 and self.on_pick) else Qt.ArrowCursor)
            self.update()
        if i >= 0 and self.items[i].get("tip"):
            QToolTip.showText(QCursor.pos(), self.items[i]["tip"], self)
        else:
            QToolTip.hideText()

    def event(self, ev):
        # The hover text is this widget's own business (mouseMoveEvent
        # shows it, column by column).  Left alone, Qt's tooltip request
        # went on to the card behind the chart, and the card's own tooltip
        # kept replacing the column's - the two took turns.
        if ev.type() == QEvent.ToolTip:
            i = self.index_at(ev.pos().x())
            if i >= 0 and self.items[i].get("tip"):
                QToolTip.showText(ev.globalPos(), self.items[i]["tip"], self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)

    def leaveEvent(self, ev):
        self.hover = -1
        QToolTip.hideText()
        self.update()

    def mousePressEvent(self, ev):
        i = self.index_at(ev.position().x())
        if i < 0:
            return
        if ev.button() == Qt.RightButton:
            if self.on_menu:
                self.on_menu(self.items[i]["key"], ev.globalPosition().toPoint())
        elif self.on_pick:
            self.on_pick(self.items[i]["key"])

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
        ev.ignore()                      # the page scrolls

    def paintEvent(self, ev):
        th = ctx.TH
        s_ = soft()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.items:
            p.setPen(QColor(th["muted"]))
            p.setFont(self.font_(10))
            p.drawText(self.rect(), Qt.AlignCenter, self.empty_text or "nothing to show")
            p.end()
            self._geo = None
            return
        self._geo = geo = self.geometry_()
        plot, pitch, colw, top, k = geo
        fm_f = self.font_(8)
        p.setFont(fm_f)
        # grid and the axis figures
        for g in range(5):
            y = plot.bottom() - plot.height() * g / 4.0
            p.setPen(QPen(QColor(th["border"]), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(th["muted"]))
            p.drawText(QRectF(0, y - 9, plot.left() - 6, 18), Qt.AlignRight | Qt.AlignVCenter,
                       "%.2f" % (top * g / 4.0))
        cut_c, queue_c = QColor(s_["cut"]), QColor(s_["queue"])
        every = 1
        if self.labels:
            lh = QFontMetrics(fm_f).height()
            every = max(1, int(lh / max(1.0, pitch)) + (1 if pitch < lh else 0))
        for i, it in enumerate(self.items):
            x = plot.left() + i * pitch + (pitch - colw) / 2.0
            ch = plot.height() * (it["cut"] / k) / top
            qh = plot.height() * (it["queue"] / k) / top
            lit = (i == self.hover) or (self.picked is not None and it["key"] == self.picked)
            dim = self.picked is not None and it["key"] != self.picked and i != self.hover
            p.setPen(Qt.NoPen)
            p.setOpacity(0.35 if dim else 1.0)
            p.setBrush(cut_c)
            p.drawRect(QRectF(x, plot.bottom() - ch, colw, ch))
            p.setBrush(queue_c)
            p.drawRect(QRectF(x, plot.bottom() - ch - qh, colw, qh))
            p.setOpacity(1.0)
            if lit:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(th["text"]), 1.2))
                p.drawRect(QRectF(x - 1, plot.bottom() - ch - qh - 1, colw + 2, ch + qh + 1))
            if self.labels and i % every == 0:
                p.save()
                p.translate(x + colw / 2.0, plot.bottom() + 6)
                p.rotate(-90)
                p.setPen(QColor(th["text"] if lit else th["muted"]))
                p.drawText(QRectF(-200, -9, 200, 18), Qt.AlignRight | Qt.AlignVCenter,
                           str(it["label"]))
                p.restore()
        if self.avg is not None:
            y = plot.bottom() - plot.height() * (self.avg / k) / top
            pen = QPen(QColor(s_["lead"]), 1.4, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setFont(self.font_(8, True))
            txt = "avg %.2f" % (self.avg / k)
            tw = QFontMetrics(p.font()).horizontalAdvance(txt) + 8
            p.fillRect(QRectF(plot.right() - tw, y - 16, tw, 14), QColor(th["card"]))
            p.drawText(QRectF(plot.right() - tw, y - 17, tw - 2, 16),
                       Qt.AlignRight | Qt.AlignVCenter, txt)
        p.end()


def legend_dots():
    """cutting / queue / average - what the chart colours mean."""
    s_ = soft()
    lb = label("", muted=True)
    lb.setTextFormat(Qt.RichText)
    lb.setText('<span style="color:%s;">■</span> operation time &nbsp; '
               '<span style="color:%s;">■</span> queue &nbsp; '
               '<span style="color:%s;">- -</span> average'
               % (s_["cut"], s_["queue"], s_["lead"]))
    return lb


# =====================================================================
#  THE EDITOR OF ONE SET OF OPERATION GROUPS
# =====================================================================
def tint(col, a):
    """`col` laid over the card colour at strength a - a soft fill that
    stays readable in both themes."""
    c, base = QColor(col), QColor(ctx.TH["card"])
    return QColor(int(base.red() + (c.red() - base.red()) * a),
                  int(base.green() + (c.green() - base.green()) * a),
                  int(base.blue() + (c.blue() - base.blue()) * a))


class OpDelegate(QStyledItemDelegate):
    """One operation of the part: a bar in its group's colour, the number,
    the description, and the group's name as a chip on the right."""

    def sizeHint(self, opt, idx):
        return QSize(100, 30)

    def paint(self, p, opt, idx):
        th = ctx.TH
        d = idx.data(Qt.UserRole) or {}
        r = opt.rect.adjusted(2, 1, -2, -1)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        sel = bool(opt.state & QStyle.State_Selected)
        col = d.get("color")
        p.setPen(Qt.NoPen)
        if col:
            p.setBrush(tint(col, 0.30 if sel else 0.16))
        else:
            p.setBrush(QColor(th["sel_row"]) if sel else QColor(th["card"]))
        p.drawRoundedRect(QRectF(r), 5, 5)
        if sel:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(th["accent"]), 1.4))
            p.drawRoundedRect(QRectF(r).adjusted(0.7, 0.7, -0.7, -0.7), 5, 5)
            p.setPen(Qt.NoPen)
        p.setBrush(QColor(col) if col else QColor(th["border"]))
        p.drawRoundedRect(QRectF(r.left() + 3, r.top() + 5, 4, r.height() - 10), 2, 2)
        f = QFont(FONT, 10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(th["text"]))
        p.drawText(QRect(r.left() + 14, r.top(), 60, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   str(d.get("op", "")))
        chip_w = 0
        if d.get("group"):
            fm = QFontMetrics(f)
            chip_w = min(150, fm.horizontalAdvance(d["group"]) + 18)
            cr = QRectF(r.right() - chip_w - 6, r.top() + 5, chip_w, r.height() - 10)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(col))
            p.drawRoundedRect(cr, 8, 8)
            p.setPen(QColor("#FFFFFF"))
            f2 = QFont(FONT, 8)
            f2.setBold(True)
            p.setFont(f2)
            p.drawText(cr, Qt.AlignCenter,
                       QFontMetrics(f2).elidedText(d["group"], Qt.ElideRight, chip_w - 10))
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(th["text"] if col else th["muted"]))
        room = r.width() - 80 - chip_w - 16
        p.drawText(QRect(r.left() + 78, r.top(), max(10, room), r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(f).elidedText(d.get("desc", ""), Qt.ElideRight, max(10, room)))
        p.restore()


class RouteRibbon(QWidget):
    """The whole route as one strip: a cell per operation in its group's
    colour - the grouping at a glance, updated as it is edited."""

    def __init__(self):
        super().__init__()
        self.cells = []            # [(op, colour or None, group name)]
        self.setFixedHeight(26)
        self.setMouseTracking(True)

    def set_cells(self, cells):
        self.cells = list(cells)
        self.update()

    def mouseMoveEvent(self, ev):
        n = len(self.cells)
        if n:
            i = max(0, min(n - 1, int(ev.position().x() * n / max(1, self.width()))))
            op, _c, g = self.cells[i]
            QToolTip.showText(QCursor.pos(), "op %s%s" % (op, ("  ·  " + g) if g else ""), self)

    def event(self, ev):
        if ev.type() == QEvent.ToolTip:          # shown by mouseMoveEvent
            return True
        return super().event(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.cells)
        if not n:
            p.end()
            return
        w = self.width() / float(n)
        gap = 2.0 if w > 6 else 0.0
        for i, (_op, col, _g) in enumerate(self.cells):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(col) if col else QColor(ctx.TH["border"]))
            p.drawRoundedRect(QRectF(i * w, 4, max(1.0, w - gap), self.height() - 8), 3, 3)
        p.end()


class GroupCard(QFrame):
    """One group in the editor: colour, name, the current part's first and
    last operation, and what it measures.  Operations (or a part) dropped
    on it - or a click on it - put the selection into the group."""

    def __init__(self, ed, g):
        super().__init__()
        self.ed, self.g = ed, g
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.hot = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(6)
        self.sw = QPushButton()
        self.sw.setFixedSize(22, 22)
        self.sw.setCursor(Qt.PointingHandCursor)
        self.sw.setToolTip("Next colour")
        self.sw.clicked.connect(lambda: ed.next_color(g))
        self.name = QLineEdit(g["name"])
        self.name.setPlaceholderText("Group name (TURN, MILL ...)")
        self.name.textEdited.connect(self._renamed)
        lay.addWidget(hbox(self.sw, self.name,
                           button("▲", icon_only=True, tip="Move up",
                                  slot=lambda: ed.move_group(g, -1)),
                           button("▼", icon_only=True, tip="Move down",
                                  slot=lambda: ed.move_group(g, 1)),
                           button("×", icon_only=True, tip="Delete this group",
                                  slot=lambda: ed.remove_group(g)), spacing=6))
        self.first, self.last = QComboBox(), QComboBox()
        for cb in (self.first, self.last):
            cb.setMinimumWidth(80)
            cb.activated.connect(lambda _i: self._range_picked())
        lay.addWidget(hbox(label("First op", muted=True), self.first,
                           label("Last op", muted=True), self.last, spacing=6))
        self.cap = label("", wrap=True)
        self.cap.setTextFormat(Qt.RichText)
        lay.addWidget(self.cap)
        self.others = label("", muted=True, wrap=True)
        lay.addWidget(self.others)
        self.paint()

    def _renamed(self, t):
        self.g["name"] = t
        self.ed.names_changed()

    def paint(self):
        th, col = ctx.TH, self.g["color"]
        self.setStyleSheet(
            "GroupCard { background: %s; border: 1px solid %s; border-left: 5px solid %s; "
            "border-radius: 8px; }" % (tint(col, 0.22 if self.hot else 0.08).name(),
                                       col if self.hot else th["border"], col))
        self.sw.setStyleSheet("QPushButton { background: %s; border: none; border-radius: 11px; }"
                              % col)

    def fill(self):
        """The current part's operations into the two lists, and the
        captions."""
        ed, g = self.ed, self.g
        ops = ed.ops(ed.cur)
        mine = set(g["ops"].get(ed.cur, [])) if ed.cur else set()
        idx = [i for i, o in enumerate(ops) if o["op"] in mine]
        for cb, want in ((self.first, idx[0] if idx else -1),
                         (self.last, idx[-1] if idx else -1)):
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("—", None)
            for o in ops:
                cb.addItem(("%s  ·  %s" % (o["op"], o["desc"])) if o["desc"] else o["op"],
                           o["op"])
            cb.setCurrentIndex(want + 1)
            cb.setEnabled(bool(ops))
            cb.blockSignals(False)
        self.cap.setText(ed.caption(g))
        rest = []
        for p in ed.parts:
            pk = p["part_key"]
            if pk == ed.cur or not g["ops"].get(pk):
                continue
            o2 = [o["op"] for o in ed.ops(pk) if o["op"] in set(g["ops"][pk])]
            if o2:
                rest.append("%s: %s → %s" % (ed.part_name(pk), o2[0], o2[-1]))
        self.others.setText("   ·   ".join(rest))
        self.others.setVisible(bool(rest))

    def _range_picked(self):
        a, b = self.first.currentIndex() - 1, self.last.currentIndex() - 1
        if a < 0 and b < 0:
            self.ed.set_range(self.g, None, None)
            return
        if a < 0:
            a = b
        if b < 0:
            b = a
        if b < a:
            a, b = b, a
        self.ed.set_range(self.g, a, b)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.ed.assign(self.g)

    def _ours(self, ev):
        return ev.source() in (self.ed.olist, self.ed.plist)

    def dragEnterEvent(self, ev):
        if self._ours(ev):
            self.hot = True
            self.paint()
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if self._ours(ev):
            ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        self.hot = False
        self.paint()

    def dropEvent(self, ev):
        self.hot = False
        self.paint()
        if ev.source() is self.ed.olist:
            self.ed.assign(self.g)
        elif ev.source() is self.ed.plist:
            # a part dropped on a group: its operations come up and the
            # first / last operation is picked right here
            it = self.ed.plist.currentItem()
            if it is not None:
                self.ed.set_current(it.data(Qt.UserRole))
                self.first.setFocus()
                self.first.showPopup()
        ev.acceptProposedAction()


class GroupEditor(QDialog):
    """NEW / EDIT of one analysis, the way CUSTOM ANALYSIS is built:

        1  PARTS        add the parts, give them a sub group (STG1 BLISK)
        2  OPERATIONS   the picked part's whole route, one line each
        3  GROUPS       named, coloured; select operations and click (or
                        drop them on) a group - or pick its first and
                        last operation on the group itself

    Each group is measured from the END of the operation before its first
    one to the END (X) of its last one, so the groups follow each other
    with no gap.  The figures beside each group are live (all time)."""

    def __init__(self, parent, name, definition, parts):
        super().__init__(parent)
        self.setWindowTitle("Operation groups")
        if ctx.sheet:
            self.setStyleSheet(ctx.sheet)
        self.resize(1280, 760)
        self.all_parts = list(parts)               # [(part key, shown name)]
        self.names = dict(parts)
        self.old_name = name
        self.result_name, self.result_def = None, None
        d = lt_groups_normalize(ctx.db, definition or {})
        self.parts = d["parts"]                    # [{"part_key", "sub"}]
        self.groups = d["groups"]                  # [{"name", "color", "ops": {pk: [op]}}]
        self.cur = self.parts[0]["part_key"] if self.parts else None
        self._ops, self._ents = {}, {}
        self.cards = []

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self.name_ed = QLineEdit(name or "")
        self.name_ed.setPlaceholderText("e.g. STG1 BLISK")
        self.name_ed.setMinimumWidth(280)
        tl = QVBoxLayout()
        tl.setSpacing(2)
        tl.addWidget(label("Edit analysis" if name else "New analysis", obj="PageTitle"))
        tl.addWidget(label("Add the parts, select their operations, put them into groups",
                           obj="PageSub"))
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addLayout(tl)
        top.addStretch(1)
        top.addWidget(label("Name", muted=True))
        top.addWidget(self.name_ed)
        top.addWidget(button("Cancel", slot=self.reject))
        top.addWidget(button("Save", primary=True, slot=self.save))
        lay.addLayout(top)

        sp = Splitter(Qt.Horizontal)
        sp.setChildrenCollapsible(False)
        sp.setHandleWidth(10)
        lay.addWidget(sp, 1)

        # ---- 1  parts ----
        c1 = Card()
        c1.add(hbox(self.badge(1), label("Parts", obj="CardTitle"), None, spacing=8))
        self.plist = QListWidget()
        self.plist.setDragEnabled(True)
        self.plist.setDragDropMode(QAbstractItemView.DragOnly)
        self.plist.setMaximumHeight(190)
        self.plist.currentItemChanged.connect(
            lambda it, _o: self.set_current(it.data(Qt.UserRole)) if it is not None else None)
        c1.add(self.plist)
        c1.add(hbox(button("Sub group…", tip="A name the parts are shown together under "
                                             "(STG1 BLISK ...)", slot=self.set_sub),
                    button("Remove", danger=True, slot=self.remove_part), None, spacing=6))
        c1.add(label("Add a part", muted=True))
        self.find = QLineEdit()
        self.find.setPlaceholderText("Find a part to add")
        self.find.textChanged.connect(self.fill_avail)
        self.find.returnPressed.connect(self.add_part)
        c1.add(self.find)
        self.avail = QListWidget()
        self.avail.itemDoubleClicked.connect(lambda _i: self.add_part())
        c1.add(self.avail, 1)
        c1.add(label("Double-click adds the part; its operations open at once.", muted=True,
                     wrap=True))
        c1.setMinimumWidth(300)
        sp.addWidget(c1)

        # ---- 2  operations ----
        c2 = Card()
        self.ops_title = label("Operations", obj="CardTitle")
        c2.add(hbox(self.badge(2), self.ops_title, None,
                    button("Select all", slot=lambda: self.olist.selectAll()),
                    button("Out of its group", tip="Take the selected operations out of "
                                                   "their group", slot=lambda: self.assign(None)),
                    spacing=8))
        self.ribbon = RouteRibbon()
        c2.add(self.ribbon)
        c2.add(label("Click an operation, Shift + click for a stretch, Ctrl + click to add. "
                     "Then click a group on the right - or drag the selection onto it.",
                     muted=True, wrap=True))
        self.olist = QListWidget()
        self.olist.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.olist.setDragEnabled(True)
        self.olist.setDragDropMode(QAbstractItemView.DragOnly)
        self.olist.setItemDelegate(OpDelegate(self.olist))
        self.olist.setSpacing(1)
        self.olist.setMouseTracking(True)
        c2.add(self.olist, 1)
        c2.setMinimumWidth(400)
        sp.addWidget(c2)

        # ---- 3  groups ----
        c3 = Card()
        c3.add(hbox(self.badge(3), label("Groups", obj="CardTitle"), None,
                    button("+ Add group", primary=True, slot=lambda: self.add_group()),
                    spacing=8))
        self.gscroll = QScrollArea()
        self.gscroll.setWidgetResizable(True)
        self.gscroll.setFrameShape(QFrame.NoFrame)
        self.ghost = QWidget()
        self.glay = QVBoxLayout(self.ghost)
        self.glay.setContentsMargins(0, 0, 4, 0)
        self.glay.setSpacing(8)
        self.glay.setAlignment(Qt.AlignTop)
        self.gscroll.setWidget(self.ghost)
        self.gscroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                   "{ background: transparent; }")
        c3.add(self.gscroll, 1)
        self.prob = label("", wrap=True)
        self.prob.setTextFormat(Qt.RichText)
        c3.add(self.prob)
        c3.setMinimumWidth(400)
        sp.addWidget(c3)
        sp.setStretchFactor(0, 2)
        sp.setStretchFactor(1, 4)
        sp.setStretchFactor(2, 4)

        th = ctx.TH
        for lw in (self.plist, self.avail, self.olist):
            lw.setFrameShape(QFrame.NoFrame)
            lw.setStyleSheet(
                "QListWidget { background: transparent; outline: none; } "
                + ("" if lw is self.olist else
                   "QListWidget::item { padding: 5px 8px; border-radius: 5px; color: %s; } "
                   "QListWidget::item:selected { background: %s; color: %s; } "
                   "QListWidget::item:hover { background: %s; }"
                   % (th["text"], th["accent"], "#FFFFFF", th["sel_row"])))
        if not self.groups:
            self.add_group(quiet=True)
        self.fill_parts()
        self.fill_avail()
        self.rebuild_cards()
        self.fill_ops()

    # ---- small things ----
    @staticmethod
    def badge(n):
        b = QLabel(str(n))
        b.setFixedSize(22, 22)
        b.setAlignment(Qt.AlignCenter)
        b.setStyleSheet("background: %s; color: #FFFFFF; border-radius: 11px; font-weight: 700;"
                        % ctx.TH["accent"])
        return b

    def part_name(self, pk):
        return self.names.get(pk, pk)

    def ops(self, pk):
        if not pk:
            return []
        if pk not in self._ops:
            self._ops[pk] = lt_part_ops(ctx.db, pk)
        return self._ops[pk]

    def entries(self, pk):
        if pk not in self._ents:
            try:
                self._ents[pk] = lead_entries(ctx.db, pk, None, None)
            except Exception:
                self._ents[pk] = {}
        return self._ents[pk]

    def group_of(self, pk, op):
        for g in self.groups:
            if op in g["ops"].get(pk, ()):
                return g
        return None

    # ---- 1  parts ----
    def fill_parts(self):
        self.plist.blockSignals(True)
        self.plist.clear()
        for p in self.parts:
            pk = p["part_key"]
            n = sum(len(g["ops"].get(pk, ())) for g in self.groups)
            txt = self.part_name(pk)
            if p["sub"]:
                txt += "    ·  %s" % p["sub"]
            it = QListWidgetItem("%s    ·  %d op%s" % (txt, n, "" if n == 1 else "s"))
            it.setToolTip("%s - %d of its operations are in a group.\nDrag it onto a group "
                          "to pick that group's first and last operation."
                          % (self.part_name(pk), n))
            it.setData(Qt.UserRole, pk)
            self.plist.addItem(it)
            if pk == self.cur:
                self.plist.setCurrentItem(it)
        self.plist.blockSignals(False)

    def fill_avail(self):
        have = {p["part_key"] for p in self.parts}
        q = self.find.text().strip().upper()
        self.avail.clear()
        for pk, disp in self.all_parts:
            if pk in have or (q and q not in disp.upper() and q not in str(pk).upper()):
                continue
            it = QListWidgetItem(disp)
            it.setData(Qt.UserRole, pk)
            self.avail.addItem(it)
        if self.avail.count():
            self.avail.setCurrentRow(0)

    def add_part(self):
        it = self.avail.currentItem()
        if it is None:
            return
        pk = it.data(Qt.UserRole)
        self.parts.append({"part_key": pk, "sub": ""})
        self.cur = pk
        self.find.clear()
        self.fill_avail()
        self.refresh_all()

    def remove_part(self):
        if not self.cur:
            return
        pk = self.cur
        self.parts = [p for p in self.parts if p["part_key"] != pk]
        for g in self.groups:
            g["ops"].pop(pk, None)
        self.cur = self.parts[0]["part_key"] if self.parts else None
        self.fill_avail()
        self.refresh_all()

    def set_sub(self):
        if not self.cur:
            return
        p = next(p for p in self.parts if p["part_key"] == self.cur)
        subs = sorted({q["sub"] for q in self.parts if q["sub"]})
        v, ok = QInputDialog.getItem(
            self, "Sub group", "Sub group of %s  (empty = none).\nParts with the same sub "
            "group are shown together under each group." % self.part_name(self.cur),
            [p["sub"]] + [s for s in subs if s != p["sub"]], 0, True)
        if ok:
            p["sub"] = v.strip()
            self.fill_parts()

    def set_current(self, pk):
        if pk and pk != self.cur:
            self.cur = pk
            self.refresh_all()

    # ---- 2  operations ----
    def fill_ops(self):
        keep = {it.data(Qt.UserRole)["op"] for it in self.olist.selectedItems()}
        top = self.olist.verticalScrollBar().value()
        self.olist.clear()
        ops = self.ops(self.cur)
        cells = []
        for i, o in enumerate(ops):
            g = self.group_of(self.cur, o["op"])
            d = {"op": o["op"], "desc": o["desc"], "color": g["color"] if g else None,
                 "group": (g["name"] or "?") if g else ""}
            it = QListWidgetItem()
            it.setData(Qt.UserRole, d)
            seqs = ("  ·  sequences: %s" % ", ".join(o["steps"])) if len(o["steps"]) > 1 else ""
            it.setToolTip("op %s  %s%s\nmeasured from the end of op %s"
                          % (o["op"], o["desc"], seqs, ops[i - 1]["op"] if i else o["op"]))
            self.olist.addItem(it)
            if o["op"] in keep:
                it.setSelected(True)
            cells.append((o["op"], d["color"], d["group"]))
        self.olist.verticalScrollBar().setValue(top)
        self.ribbon.set_cells(cells)
        if self.cur:
            self.ops_title.setText("Operations · %s" % self.part_name(self.cur))
        else:
            self.ops_title.setText("Operations · add a part first")

    def selected_ops(self):
        return [it.data(Qt.UserRole)["op"] for it in self.olist.selectedItems()]

    def _put(self, g, chosen):
        """`chosen` operations of the current part into group g (None =
        out of every group), kept in route order."""
        pk = self.cur
        chosen = set(chosen)
        for other in self.groups:
            left = [o for o in other["ops"].get(pk, []) if o not in chosen]
            if left:
                other["ops"][pk] = left
            else:
                other["ops"].pop(pk, None)
        if g is not None and chosen:
            have = set(g["ops"].get(pk, [])) | chosen
            g["ops"][pk] = [o["op"] for o in self.ops(pk) if o["op"] in have]

    def assign(self, g):
        sel = self.selected_ops()
        if not self.cur or not sel:
            if g is not None:
                self.prob.setText("Select operations in the middle list first.")
            return
        self._put(g, sel)
        self.olist.clearSelection()
        self.refresh_all()

    def set_range(self, g, a, b):
        """The group is exactly the operations a..b of the current part."""
        if not self.cur:
            return
        ops = self.ops(self.cur)
        self._put(None, g["ops"].get(self.cur, []))
        if a is not None:
            self._put(g, [o["op"] for o in ops[a:b + 1]])
        self.refresh_all()

    # ---- 3  groups ----
    def add_group(self, quiet=False):
        used = {g["color"] for g in self.groups}
        col = next((c for c in LT_GROUP_COLORS if c not in used),
                   LT_GROUP_COLORS[len(self.groups) % len(LT_GROUP_COLORS)])
        self.groups.append({"name": "", "color": col, "ops": {}})
        if not quiet:
            self.rebuild_cards()
            self.cards[-1].name.setFocus()

    def remove_group(self, g):
        if any(g["ops"].values()) and QMessageBox.question(
                self, "Operation groups", "Delete the group '%s'?" % (g["name"] or "?")) \
                != QMessageBox.Yes:
            return
        self.groups = [x for x in self.groups if x is not g]
        self.refresh_all(cards=True)

    def move_group(self, g, step):
        i = next(i for i, x in enumerate(self.groups) if x is g)
        j = i + step
        if 0 <= j < len(self.groups):
            self.groups[i], self.groups[j] = self.groups[j], self.groups[i]
            self.rebuild_cards()

    def next_color(self, g):
        i = LT_GROUP_COLORS.index(g["color"]) if g["color"] in LT_GROUP_COLORS else -1
        g["color"] = LT_GROUP_COLORS[(i + 1) % len(LT_GROUP_COLORS)]
        self.refresh_all()

    def names_changed(self):
        self.fill_ops()

    def rebuild_cards(self):
        while self.glay.count():
            w = self.glay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.cards = []
        for g in self.groups:
            c = GroupCard(self, g)
            self.cards.append(c)
            self.glay.addWidget(c)
            c.fill()

    def caption(self, g):
        """What the group measures on the current part, with its live
        figure (all time, no filters)."""
        th = ctx.TH
        if not self.cur:
            return "<span style='color:%s;'>add a part first</span>" % th["muted"]
        chosen = g["ops"].get(self.cur, [])
        if not chosen:
            return ("<span style='color:%s;'>no operations of this part yet - drop them "
                    "here</span>" % th["muted"])
        ops = self.ops(self.cur)
        runs, _m = lt_runs(ops, chosen)
        ref = ops[lt_run_bounds(*runs[0])[0]]["op"]
        txt = "end of op <b>%s</b> → end of op <b>%s</b> · %d op%s" % (
            ref, ops[runs[-1][1]]["op"], len(chosen), "" if len(chosen) == 1 else "s")
        if len(runs) > 1:
            txt += " · %d separate stretches, added up" % len(runs)
        vals, prob = lead_group_values(ops, self.entries(self.cur), chosen)
        kept = [v for v, k in zip(vals, lead_keep_mask([v[1] for v in vals])) if k]
        if kept:
            avg = sum(v[1] for v in kept) / len(kept)
            txt += ("<br><span style='color:%s;'><b>%.2f d</b></span> average lead · %d serials"
                    % (g["color"], avg / 24.0, len(kept)))
        elif prob:
            txt += "<br><span style='color:%s;'>%s</span>" % (th["danger"], prob)
        else:
            txt += "<br><span style='color:%s;'>no closed serials yet</span>" % th["muted"]
        return txt

    def refresh_all(self, cards=False):
        self.prob.setText("")
        self.fill_parts()
        self.fill_ops()
        if cards or len(self.cards) != len(self.groups):
            self.rebuild_cards()
        else:
            for c in self.cards:
                c.paint()
                c.fill()

    # ---- save ----
    def save(self):
        name = self.name_ed.text().strip()
        bad = []
        if not name:
            bad.append("Give the analysis a name.")
        groups = [g for g in self.groups if any(g["ops"].values())]
        if not groups:
            bad.append("Put at least one operation into a group.")
        seen = set()
        for g in groups:
            g["name"] = g["name"].strip()
            if not g["name"]:
                bad.append("A group with operations has no name.")
            elif g["name"].upper() in seen:
                bad.append("Two groups are called '%s'." % g["name"])
            seen.add(g["name"].upper())
        if bad:
            self.prob.setText("<span style='color:%s;'>%s</span>"
                              % (ctx.TH["danger"], "<br>".join(bad)))
            return
        if name != self.old_name and name in lt_groups_load(ctx.db):
            if QMessageBox.question(self, "Operation groups",
                                    "'%s' already exists. Replace it?" % name) \
                    != QMessageBox.Yes:
                return
        used = {pk for g in groups for pk in g["ops"]}
        self.result_name = name
        self.result_def = {
            "parts": [dict(p) for p in self.parts if p["part_key"] in used],
            "groups": [{"name": g["name"], "color": g["color"],
                        "ops": {pk: list(v) for pk, v in g["ops"].items() if v}}
                       for g in groups]}
        self.accept()


# =====================================================================
#  THE PAGE
# =====================================================================
class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.last = {"data": None, "groups": None, "part": "", "label": "all time",
                     "set": ""}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        outer = page_layout(self)
        head = page_head(outer, "Lead Time Analysis", "Lead time, queue and flow efficiency")

        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("waterfall"),
                             part_multi=False, with_machines=True,
                             machine_scope_fn=self.machine_scope)
        self.bar.remember("leadtime")
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(self.cascade)
        outer.addWidget(self.bar)

        # ---- view / options: one row ----
        opts = QWidget()
        of = FlowLayout(opts, 8)
        self.unit = Seg([("Days", "days"), ("Hours", "hours")], "days")
        self.unit.changed.connect(lambda _v: self.schedule())
        of.addWidget(hbox(label("Show", muted=True), self.unit, spacing=8))
        try:
            mode = ctx.prefs.get("lt_date_mode", "ops")
        except Exception:
            mode = "ops"
        self.mode = Seg(DATE_MODES, mode if mode in ("ops", "serial", "sold") else "ops")
        self.mode.setToolTip(MODE_TIP)
        self.mode.changed.connect(self._mode_changed)
        of.addWidget(hbox(label("Date filter", muted=True), self.mode, spacing=8))
        self.t_noclean = Toggle("Remove clean ops", checked=True, slot=self.schedule)
        self.t_dedupe = Toggle("One entry per op", checked=True, slot=self.schedule)
        of.addWidget(self.t_noclean)
        of.addWidget(self.t_dedupe)
        outer.addWidget(opts)

        # ---- the whole route, in the title row ----
        self.k_lead = KpiInline("LEAD", None, compact=True)
        self.k_cut = KpiInline("OP. TIME", None, compact=True)
        self.k_queue = KpiInline("QUEUE", None, compact=True)
        self.k_eff = KpiInline("FLOW EFF.", None, compact=True)
        self.info_lbl = label("", muted=True)
        head.addSpacing(24)
        for k in (self.k_lead, self.k_cut, self.k_queue, self.k_eff):
            head.addWidget(k)
        head.addSpacing(8)
        head.addWidget(self.info_lbl)
        head.addStretch(1)
        head.addWidget(export_button(self.do_export))

        # ---- MODULES: each one on / off, and the zoom ----
        trow = QHBoxLayout()
        trow.setSpacing(8)
        trow.addWidget(label("MODULES", section=True))
        self.t_steps = Toggle("Steps", checked=True, slot=self.rebuild_panels)
        self.t_groups = Toggle("Operation groups", checked=True, slot=self.rebuild_panels)
        self.t_prof = Toggle("Route profile", checked=True, slot=self.rebuild_panels)
        self.t_ser = Toggle("Serials", checked=True, slot=self.rebuild_panels)
        for t in (self.t_steps, self.t_groups, self.t_prof, self.t_ser):
            trow.addWidget(t)
        trow.addStretch(1)
        self.zoom = ZoomControl(lambda: [self.table, self.gtable, self.prof, self.serc])
        trow.addWidget(self.zoom)
        outer.addLayout(trow)

        # ---- the tables: steps | operation groups ----
        self.grid = ModuleGrid()
        outer.addWidget(self.grid)
        self.steps = ModuleCard("Steps", "", on_export=self.do_export)
        self.note = label("")
        self.note.setTextFormat(Qt.RichText)
        self.note.setWordWrap(True)
        self.note.hide()
        self.steps.content_lay.addWidget(self.note)
        self.table = DataTable()
        self.table.tiles = True
        self.table.fit_width = True
        self.table.fit_min = MIN_ROWS
        self.table.fit_rows(24)
        self.table.on_row_click = lambda ri, _row: self.pick_step(ri)
        self.steps.content_lay.addWidget(self.table, 1)
        self.steps.setMinimumWidth(420)

        self.gcard = ModuleCard("Operation groups",
                                "named stretches of the route, over one or several parts",
                                on_export=self.do_export)
        self.set_cb = QComboBox()
        self.set_cb.setMinimumWidth(180)
        self.set_cb.currentIndexChanged.connect(self._set_picked)
        srow = QWidget()
        sf = FlowLayout(srow, 8)
        sf.addWidget(hbox(label("Analysis", muted=True), self.set_cb, spacing=8))
        sf.addWidget(button("New", slot=lambda: self.edit_set(None)))
        self.b_edit = button("Edit", slot=lambda: self.edit_set(self.set_name()))
        self.b_del = button("Delete", danger=True, slot=self.delete_set)
        sf.addWidget(self.b_edit)
        sf.addWidget(self.b_del)
        self.t_detail = Toggle("Show parts", slot=lambda: self.show_groups(
            self.last["groups"], self.unit.get(), self.last["set"]))
        self.t_detail.setToolTip("The parts under each group / sub group, with their "
                                 "first and last operation.")
        sf.addWidget(self.t_detail)
        self.gcard.content_lay.addWidget(srow)
        self.gtable = DataTable()
        self.gtable.tiles = True
        self.gtable.fit_width = True
        self.gtable.fit_min = MIN_ROWS
        self.gtable.fit_rows(24)
        self.gtable.on_row_click = lambda ri, _row: self.pick_group(ri)
        self.gcard.content_lay.addWidget(self.gtable, 1)
        self.gnote = label("", muted=True, wrap=True)
        self.gcard.content_lay.addWidget(self.gnote)
        self.gcard.setMinimumWidth(420)
        self.grid.add(self.steps, wide=True, weight=3)
        self.grid.add(self.gcard, wide=True, weight=3)

        # ---- the charts under them: route profile | serials ----
        self.grid2 = ModuleGrid()
        outer.addWidget(self.grid2)
        self.pcard = ModuleCard("Route profile", "per operation")
        self.leg1 = legend_dots()
        self.pcard.add_extra(self.leg1)
        self.prof = ColumnChart(labels=True)
        self.prof.on_pick = self.pick_step_key
        self.pcard.content_lay.addWidget(self.prof)
        self.pcard.setMinimumWidth(420)

        self.scard = ModuleCard("Serials", "")
        self.b_whole = button("Whole route", tip="Back from the picked step to the whole route",
                              slot=lambda: self.pick_step(None))
        self.b_whole.hide()
        self.scard.add_extra(self.b_whole)
        self.serc = ColumnChart(labels=False)
        self.serc.on_menu = lambda key, gpos: serial_menu(
            (self.last["data"] or {}).get("serial_names", {}).get(key, key), gpos)
        self.scard.content_lay.addWidget(self.serc)
        self.scard.setMinimumWidth(420)
        self.grid2.add(self.pcard, wide=True, weight=3)
        self.grid2.add(self.scard, wide=True, weight=3)

        self.flags = ((self.t_steps, self.steps, self.grid), (self.t_groups, self.gcard, self.grid),
                      (self.t_prof, self.pcard, self.grid2), (self.t_ser, self.scard, self.grid2))
        self.sel_step = None                 # index into data["steps"], None = whole route
        self.sel_group = None                # (group, sub, part) of the row on the chart
        self.grow_keys = []                  # the groups table, row by row
        for _t, c, _g in self.flags:
            c.show()
        self.grid.relayout()
        self.grid2.relayout()
        self.paint_kpis()

    # ---------------------------------------------------------------
    def paint_kpis(self):
        s = soft()
        for k, key in ((self.k_lead, "lead"), (self.k_cut, "cut"), (self.k_queue, "queue"),
                       (self.k_eff, "cut")):
            k.color = s[key]
            k.paint()

    def rebuild_panels(self):
        for t, c, _g in self.flags:
            if not t.isChecked() and c.detached:
                c.dock()
            c.setVisible(t.isChecked())
        self.grid.relayout()
        self.grid2.relayout()
        self.schedule()

    # ---- one step picked (a row of STEPS, a column of the profile) ----
    def pick_step_key(self, key):
        self.pick_step(key)

    def pick_step(self, idx):
        if idx is not None and idx == self.sel_step:
            idx = None                       # a second click lets go
        self.sel_step = idx
        self.sel_group = None
        self.draw_charts()

    def pick_group(self, ri):
        """A row of OPERATION GROUPS: its serials go on the chart."""
        key = self.grow_keys[ri] if 0 <= ri < len(self.grow_keys) else None
        self.sel_group = None if key == self.sel_group else key
        if self.sel_group is not None:
            self.sel_step = None
        self.draw_charts()

    def group_serials(self):
        """(title, [(serial key, lead, op time)], names) of the picked row,
        or None when it is gone."""
        if self.sel_group is None:
            return None
        gname, sub, pk = self.sel_group
        for g in self.last["groups"] or []:
            if g["name"] != gname:
                continue
            if pk is not None:
                for p in g["parts"]:
                    if p["part_key"] == pk:
                        return ("%s · %s" % (gname, p.get("part") or pk), p["serials"],
                                g["names"])
            elif sub is not None:
                for sb in g["subs"]:
                    if sb["name"] == sub:
                        return ("%s · %s" % (gname, sub),
                                [v for p in sb["parts"] for v in p["serials"]], g["names"])
            else:
                return (gname, [(sk, h, c) for sk, h, c, _pk in g["serials"]], g["names"])
        return None

    def _mode_changed(self, v):
        try:
            ctx.prefs["lt_date_mode"] = v
            save_prefs(ctx.prefs)
        except Exception:
            pass
        self.schedule()

    def time_range(self):
        f = self.bar.filters()
        if f["time_mode"] == "all":
            return None, None
        return time_mode_to_date_range(f, ctx.db.week_start("waterfall"))

    def time_text(self):
        f = self.bar.filters()
        if f["time_mode"] == "all":
            return "all time"
        if f["time_mode"] == "dates":
            d1, d2 = self.time_range()
            return "%s .. %s" % (d1 or "start", d2 or "end")
        return self.bar.time_label(f)

    def machine_scope(self):
        pk = self.bar.parts.get_selected()
        d1, d2 = self.time_range()
        return (ctx.db.wf_resolve_material_keys(pk) if pk else None, d1, d2)

    def all_parts(self):
        return ctx.wf_an.parts(ctx.db.merge_on("waterfall"))

    def matching_parts(self):
        merge = ctx.db.merge_on("waterfall")
        idx = {(p["part_key"] or norm_text(p["part_no"])): p for p in ctx.db.all_parts(merge)}
        eng, grp = self.bar.engineer(), self.bar.group()
        out = []
        for pk, disp in ctx.wf_an.parts(merge):
            info = idx.get(pk)
            e = (info["engineer"] if info else "") or ""
            g = (info["part_group"] if info else "") or ""
            if eng != ALL and e.upper() != eng.upper():
                continue
            if grp != ALL and g != grp:
                continue
            out.append((pk, disp))
        return out

    def cascade(self):
        parts = ctx.db.all_parts(ctx.db.merge_on("waterfall"))
        eng = self.bar.set_choices(self.bar.eng, sorted({p["engineer"] for p in parts
                                                         if p["engineer"]}))
        self.bar.set_choices(self.bar.grp, sorted(
            {p["part_group"] for p in parts if p["part_group"]
             and (eng == ALL or (p["engineer"] or "").upper() == eng.upper())}))
        shown = self.matching_parts()
        self.bar.parts.rebuild([pk for pk, _d in shown], labels=dict(shown))
        self.bar.paint_chips()
        self.schedule()

    # ---- the saved sets ----
    def set_name(self):
        return self.set_cb.currentData()

    def fill_sets(self, want=None):
        want = want if want is not None else (self.set_name() or self._pref_set())
        names = sorted(lt_groups_load(ctx.db), key=lambda s: s.upper())
        self.set_cb.blockSignals(True)
        self.set_cb.clear()
        for n in names:
            self.set_cb.addItem(n, n)
        i = self.set_cb.findData(want) if want else -1
        self.set_cb.setCurrentIndex(i if i >= 0 else (0 if names else -1))
        self.set_cb.blockSignals(False)
        self.b_edit.setEnabled(bool(names))
        self.b_del.setEnabled(bool(names))

    def _pref_set(self):
        try:
            return ctx.prefs.get("lt_group_set")
        except Exception:
            return None

    def _set_picked(self, _i):
        try:
            ctx.prefs["lt_group_set"] = self.set_name()
            save_prefs(ctx.prefs)
        except Exception:
            pass
        self.schedule()

    def edit_set(self, name):
        definition = lt_groups_load(ctx.db).get(name) if name else None
        dlg = GroupEditor(self, name, definition, self.all_parts())
        if dlg.exec() != QDialog.Accepted or not dlg.result_name:
            return
        try:
            if name and name != dlg.result_name:
                lt_groups_delete(ctx.db, name)
            lt_groups_save(ctx.db, dlg.result_name, dlg.result_def)
        except Exception as e:
            QMessageBox.critical(self, "Operation groups", "Could not save: %s" % e)
            return
        self.fill_sets(dlg.result_name)
        self._set_picked(0)

    def delete_set(self):
        name = self.set_name()
        if not name:
            return
        if QMessageBox.question(self, "Operation groups",
                                "Delete '%s' for everybody?" % name) != QMessageBox.Yes:
            return
        try:
            lt_groups_delete(ctx.db, name)
        except Exception as e:
            QMessageBox.critical(self, "Operation groups", "Could not delete: %s" % e)
            return
        self.fill_sets()
        self.schedule()

    # ---- life cycle ----
    def on_show(self):
        if not self.built:
            self.built = True
            self.bar.refresh_years(ctx.db.wf_years())
            self.cascade()
        self.fill_sets()
        self.timer.stop()
        self.refresh(force=True)

    def reload(self):
        if self.built:
            self.bar.refresh_years(ctx.db.wf_years())
            self.fill_sets()
            self.cascade()

    def schedule(self):
        if self.built:
            self.timer.start(220)

    def refresh(self, force=False):
        if not self.built:
            return
        if not force and not self.isVisible() and not any(c.detached
                                                         for _t, c, _g in self.flags):
            return
        self.timer.stop()
        ctx.busy(True, "Working out lead times...")
        try:
            self._refresh()
        except Exception as e:
            traceback.print_exc()
            ctx.status("Lead time error: %s" % e)
        finally:
            ctx.busy(False)

    def excluded_keys(self):
        ex = {k for k, t in self.bar.exclude_flags.items() if t.isChecked()}
        return ctx.db.flag_keys(ex) if ex else set()

    def wanted_serials(self, pk, d1, d2):
        """The SERIAL list, the ONLY flags and the MACHINES filter as one
        set of serial keys - None when none of them restricts."""
        wanted = self.bar.get_serials() or None
        only = {k for k, t in self.bar.only_flags.items() if t.isChecked()}
        if only:
            keys = ctx.db.flag_keys(only)
            wanted = (wanted & keys) if wanted else set(keys)
            if not wanted:
                return set(), True
        if pk:
            from_m = self.bar.machine_serials(d1, d2,
                                              part_keys=ctx.db.wf_resolve_material_keys(pk))
            if from_m is not None:
                wanted = (wanted & from_m) if wanted else set(from_m)
                if not wanted:
                    return set(), True
        return wanted, False

    def _refresh(self):
        unit = self.unit.get()
        d1, d2 = self.time_range()
        mode = self.mode.get()
        lbl = self.time_text()
        pk = self.bar.parts.get_selected()
        excl = self.excluded_keys()
        self.last.update(label=lbl, data=None, groups=None,
                         part=self.bar.parts.label_of(pk) if pk else "")
        mode_txt = dict((v, t) for t, v in DATE_MODES)[mode]
        self.info_lbl.setText("%s   |   %s" % (lbl, mode_txt.lower()))

        data = None
        if pk:
            wanted, empty = self.wanted_serials(pk, d1, d2)
            if not empty:
                data = lead_time_analysis(ctx.db, pk, d1, d2, serial_keys=wanted,
                                          exclude_clean=self.t_noclean.isChecked(),
                                          dedupe_ops=self.t_dedupe.isChecked(),
                                          exclude_serial_keys=excl or None, date_mode=mode)
        if data is not self.last.get("drawn"):
            self.sel_step = None             # other figures: the picked step goes
        self.last["data"] = data
        self.last["drawn"] = data
        self.show_steps(data, unit, pk)
        self.draw_charts()

        # the groups: their own parts; the machines filter belongs to the
        # one part picked above, so only the serial list and flags apply
        wanted, empty = self.wanted_serials(None, d1, d2)
        name = self.set_name()
        definition = lt_groups_load(ctx.db).get(name) if name else None
        groups = None
        if definition and not empty:
            groups = lead_group_analysis(ctx.db, definition, d1, d2, serial_keys=wanted,
                                         exclude_serial_keys=excl or None, date_mode=mode)
        elif definition:
            groups = lead_group_analysis(ctx.db, definition, d1, d2, serial_keys={"\0"},
                                         date_mode=mode)
        self.last.update(groups=groups, set=name or "")
        self.show_groups(groups, unit, name)
        ctx.status("%s   |   %s   |   %s"
                   % (self.last["part"] or "no part", lbl, mode_txt))

    # ---- STEPS ----
    def show_steps(self, data, unit, pk):
        u = "d" if unit == "days" else "h"
        if not data:
            self.table.clear()
            self.note.hide()
            for k in (self.k_lead, self.k_cut, self.k_queue, self.k_eff):
                k.set("-")
            self.steps.title_lbl.setText("Steps")
            self.steps.set_sub("pick a part" if not pk else "no traveler route / no data")
            return
        night = ctx.TH["name"] != "day"
        s_ = soft()
        cols = [("OP", 70), ("DESCRIPTION", 200), ("LEAD TIME (%s)" % unit, 110),
                ("OP. TIME (%s)" % unit, 110), ("QUEUE (%s)" % unit, 110),
                ("FLOW EFFICIENCY", 120), ("SERIALS", 70)]
        steps = data["steps"]
        avgs = [s["avg_hours"] for s in steps if s["avg_hours"] is not None]
        lo, hi = (min(avgs), max(avgs)) if avgs else (0, 0)
        qs = [s["queue_hours"] for s in steps if s["queue_hours"] is not None]
        qlo, qhi = (min(qs), max(qs)) if qs else (0, 0)
        rows, cbg, cfg, bars = [], {}, {}, {}
        for ri, s in enumerate(steps):
            rows.append([str(s["to_op"]), s["to_desc"],
                         format_wf_hours(s["avg_hours"], unit),
                         format_wf_hours(s["cut_hours"], unit),
                         format_wf_hours(s["queue_hours"], unit),
                         eff_text(s["efficiency"], s.get("overlap")),
                         str(s["n"]) if s["n"] else ""])
            # the longer the step (or its queue), the stronger the tint
            if s["avg_hours"] is not None:
                k = 0.5 if hi <= lo else (s["avg_hours"] - lo) / float(hi - lo)
                cbg[(ri, 2)], cfg[(ri, 2)] = lt_chip(s_["lead"], k, night)
            if s["cut_hours"] is not None:
                cbg[(ri, 3)], cfg[(ri, 3)] = lt_chip(s_["cut"], 0.15, night)
            if s["queue_hours"] is not None:
                k = 0.5 if qhi <= qlo else (s["queue_hours"] - qlo) / float(qhi - qlo)
                cbg[(ri, 4)], cfg[(ri, 4)] = lt_chip(s_["queue"], k, night)
            if s["efficiency"] is not None:
                bars[(ri, 5)] = (max(0.0, min(1.0, s["efficiency"])), s_["cut"])
        self.table.cell_bar = bars
        self.table.set_data(autofit_columns(cols, rows), rows,
                            ["a" if i % 2 == 0 else "b" for i in range(len(rows))],
                            align=["w", "w"] + ["center"] * 5, cell_bg=cbg, cell_fg=cfg)
        self.table.cell_tip = {(ri, 1): rows[ri][1] for ri in range(len(rows))}
        # THE WHOLE ROUTE, END TO END: the first operation's completion to
        # the last operation's X, over the serials that finished.  Only
        # when none has finished yet: the steps added up.
        eff = data.get("flow_efficiency")
        whole = data.get("route_lead")
        if whole is not None:
            work = data.get("route_cut") or 0.0
            self.k_lead.set("%s %s" % (format_wf_hours(whole, unit), u))
            self.k_cut.set("%s %s" % (format_wf_hours(work, unit), u))
            self.k_queue.set("%s %s" % (format_wf_hours(max(0.0, whole - work), unit), u))
            self.k_eff.set(("%.1f%%" % (work / whole * 100.0)) if whole else "-")
            tip = ("Whole route: op %s completed → op %s closed (X)\n"
                   "average of %d finished serial(s)"
                   % (data.get("route_first"), data.get("route_last"), data.get("route_n")))
        elif eff is not None:
            self.k_lead.set("%s %s" % (format_wf_hours(data["total_lead"], unit), u))
            self.k_cut.set("%s %s" % (format_wf_hours(data["total_cut"], unit), u))
            self.k_queue.set("%s %s" % (format_wf_hours(data["total_queue"], unit), u))
            self.k_eff.set("%.1f%%" % (eff * 100.0))
            tip = "No serial has finished the route in this selection:\nthe steps added up"
        else:
            tip = ""
        for k in (self.k_lead, self.k_cut, self.k_queue, self.k_eff):
            k.setToolTip(tip)
        if whole is None and eff is None:
            for k in (self.k_lead, self.k_cut, self.k_queue, self.k_eff):
                k.set("-")
        if data.get("n_overlap"):
            self.note.setText('<span style="color:%s;">* %d step(s) overlap - counted as all '
                              'operation time</span>' % (ctx.TH["muted"], data["n_overlap"]))
            self.note.show()
        else:
            self.note.hide()
        self.steps.title_lbl.setText("Steps · %s" % self.last["part"])
        self.steps.set_sub("%d serials · each step from the operation above it"
                           % data["n_serials"])

    # ---- THE CHARTS ----
    def draw_charts(self):
        data, unit = self.last["data"], self.unit.get()
        u = "d" if unit == "days" else "h"
        for lb in (self.leg1,):
            s_ = soft()
            lb.setText('<span style="color:%s;">■</span> operation time &nbsp; '
                       '<span style="color:%s;">■</span> queue &nbsp; '
                       '<span style="color:%s;">- -</span> average'
                       % (s_["cut"], s_["queue"], s_["lead"]))
        f = lambda v: format_wf_hours(v, unit)
        picked = self.group_serials()
        if picked is None:
            self.sel_group = None
        if not data and picked is None:
            self.prof.clear("pick a part")
            self.serc.clear("pick a part - or click a row of Operation groups")
            self.scard.title_lbl.setText("Serials")
            self.scard.set_sub("")
            self.b_whole.hide()
            return
        steps = data["steps"] if data else []
        if self.sel_step is not None and not (0 <= self.sel_step < len(steps)):
            self.sel_step = None
        if not data:
            self.prof.clear("pick a part")

        # the route, operation by operation
        items = []
        for i, s in enumerate(steps):
            if s["avg_hours"] is None:
                continue
            items.append({"key": i, "label": s["to_op"], "cut": s["cut_hours"] or 0.0,
                          "queue": s["queue_hours"] or 0.0,
                          "tip": "%s  %s\nlead %s %s  =  operation time %s + queue %s\n%d serials"
                                 "\nclick: this step's serials"
                                 % (s["to_op"], s["to_desc"] or "", f(s["avg_hours"]), u,
                                    f(s["cut_hours"]), f(s["queue_hours"]), s["n"])})
        if data:
            avg = (sum(it["cut"] + it["queue"] for it in items) / len(items)) if items else None
            self.prof.picked = self.sel_step
            self.prof.set_data(items, unit, avg, "no closed steps in this selection")

        # the serials: the whole route, the step picked, or a group's row
        names = data.get("serial_names", {}) if data else {}
        if picked is not None:
            title, vals, names = picked
            rows = [(sk, h, c, 1) for sk, h, c in vals]
            title = "Serials · %s" % title
            extra = "operation group"
            tip = lambda r: "%s\nlead %s %s  =  operation time %s + queue %s" % (
                names.get(r[0], r[0]), f(r[1]), u, f(r[2]), f(max(0.0, r[1] - r[2])))
            whole_avg = None
            self.b_whole.setVisible(bool(data))
        elif self.sel_step is None:
            # end to end: first operation completed -> last operation's X
            rs = data.get("route_serial") or {}
            rows = [(sk, v[0], v[1], v[2]) for sk, v in rs.items()]
            far = sum(1 for r in rows if not r[3])
            title = "Serials · whole route"
            extra = "op %s completed → op %s closed (X) · finished serials%s" % (
                data.get("route_first"), data.get("route_last"),
                (" · %d far-off, not in the average" % far) if far else "")
            tip = lambda r: "%s\nop %s completed → op %s closed:  %s %s\n=  operation time %s + queue %s%s" % (
                names.get(r[0], r[0]), data.get("route_first"), data.get("route_last"),
                f(r[1]), u, f(r[2]), f(max(0.0, r[1] - r[2])),
                "" if r[3] else "\nfar-off: not in the average")
            whole_avg = data.get("route_lead")
            self.b_whole.hide()
        else:
            s = steps[self.sel_step]
            rows = [(sk, h, c, 1) for sk, h, c in s.get("serials", [])]
            title = "Serials · op %s" % s["to_op"]
            extra = s["to_desc"] or ""
            tip = lambda r: "%s\nlead %s %s  =  operation time %s + queue %s" % (
                names.get(r[0], r[0]), f(r[1]), u, f(r[2]), f(max(0.0, r[1] - r[2])))
            whole_avg = None
            self.b_whole.show()
        rows.sort(key=lambda r: (-r[1], r[0]))
        items = [{"key": r[0], "label": names.get(r[0], r[0]), "cut": r[2],
                  "queue": max(0.0, r[1] - r[2]), "tip": tip(r)} for r in rows]
        avg = (sum(r[1] for r in rows) / len(rows)) if rows else None
        if whole_avg is not None:
            avg = whole_avg                  # the same figure as LEAD at the top
        self.serc.set_data(items, unit, avg,
                           "no serial has finished the route in this selection"
                           if (picked is None and self.sel_step is None)
                           else "no serials with a closed step")
        self.scard.title_lbl.setText(title)
        if rows:
            lo, hi = rows[-1][1], rows[0][1]
            ends = ("op %s → op %s · " % (data.get("route_first"), data.get("route_last"))
                    if (picked is None and self.sel_step is None and data) else "")
            self.scard.set_sub("%d serials · %s%s .. %s %s" % (len(rows), ends, f(lo), f(hi), u))
            # on the heading only - a tooltip on the whole card showed up
            # over the chart and fought with the columns' own
            self.scard.title_lbl.setToolTip("Longest first · %s" % extra)
        else:
            self.scard.set_sub(extra)

    # ---- OPERATION GROUPS ----
    def show_groups(self, groups, unit, name):
        self.grow_keys = []
        if not groups:
            self.gtable.clear()
            self.gnote.setText("No analysis yet - press New: add the parts, select their "
                               "operations and put them into groups of your own naming "
                               "(TURN, MILL ...)."
                               if not name else "This analysis has no groups.")
            self.gnote.show()
            return
        night = ctx.TH["name"] != "day"
        s_ = soft()
        names = dict(self.all_parts())
        detail = self.t_detail.isChecked()
        # the OPS column only with the parts open - the room goes to the names
        cols = [("GROUP", 150), ("SUB GROUP / PART" if detail else "SUB GROUP", 190),
                ("OPS", 90), ("LEAD TIME (%s)" % unit, 100), ("OP. TIME (%s)" % unit, 100),
                ("QUEUE (%s)" % unit, 100), ("FLOW EFF.", 100), ("SERIALS", 70)]
        avgs = [g["avg_hours"] for g in groups if g["avg_hours"] is not None]
        lo, hi = (min(avgs), max(avgs)) if avgs else (0, 0)
        rows, bg, bold, cbg, cfg, bars, tips = [], [], [], {}, {}, {}, {}

        def line(vals, d, kind, k, key, color=None):
            ri = len(rows)
            rows.append(vals)
            self.grow_keys.append(key)
            bg.append("sub" if kind == "group" else ("a" if ri % 2 == 0 else "b"))
            bold.append(kind != "part")
            if color:
                cfg[(ri, 0)] = color
            if d["avg_hours"] is not None:
                cbg[(ri, 3)], cfg[(ri, 3)] = lt_chip(s_["lead"], k, night)
                cbg[(ri, 4)], cfg[(ri, 4)] = lt_chip(s_["cut"], 0.15, night)
                cbg[(ri, 5)], cfg[(ri, 5)] = lt_chip(s_["queue"], k, night)
            if d["efficiency"] is not None:
                bars[(ri, 6)] = (max(0.0, min(1.0, d["efficiency"])), s_["cut"])
            return ri

        def figures(d):
            return [format_wf_hours(d["avg_hours"], unit), format_wf_hours(d["cut_hours"], unit),
                    format_wf_hours(d["queue_hours"], unit),
                    eff_text(d["efficiency"], d.get("overlap")),
                    str(d["n"]) if d["n"] else ""]

        def part_line(g, p):
            ops = ("%s → %s" % (p["first"], p["last"])) if p["first"] else ""
            ri = line(["", "      " + p["part"], ops] + figures(p), p, "part", 0.15,
                      (g["name"], None, p["part_key"]))
            tip = p["problem"] or ("measured from the end of op %s to the end of op %s"
                                   "  ·  %d operation(s)%s"
                                   % (p["ref"], p["last"], p["n_ops"],
                                      ("  ·  %d separate stretches" % p["runs"])
                                      if p["runs"] > 1 else ""))
            if p["problem"]:
                rows[ri][-1] = "!"
            tips[(ri, 1)] = tips[(ri, 2)] = tips[(ri, 7)] = tip
        hidden = 0
        for g in groups:
            k = 0.5
            if g["avg_hours"] is not None and hi > lo:
                k = (g["avg_hours"] - lo) / float(hi - lo)
            n_parts = len({p["part_key"] for p in g["parts"]})
            for p in g["parts"]:
                p["part"] = names.get(p["part_key"], p["part_key"])
            ri = line(["● " + g["name"], "%d part%s" % (n_parts, "" if n_parts == 1 else "s"), ""]
                      + figures(g), g, "group", k, (g["name"], None, None), g["color"])
            bad = [p for p in g["parts"] if p["problem"]]
            if bad:
                tips[(ri, 1)] = "\n".join("%s: %s" % (p["part"], p["problem"]) for p in bad)
                rows[ri][1] += "  !"
            in_sub = set()
            for sb in g["subs"]:
                line(["", "   %s · %d part%s" % (sb["name"], len(sb["parts"]),
                                                   "" if len(sb["parts"]) == 1 else "s"), ""]
                     + figures(sb), sb, "sub", 0.3, (g["name"], sb["name"], None))
                tips[(len(rows) - 1, 1)] = "%s: %s" % (sb["name"], ", ".join(
                    p["part"] for p in sb["parts"]))
                for p in sb["parts"]:
                    in_sub.add(p["part_key"])
                    if detail:
                        part_line(g, p)
                    else:
                        hidden += 1
            for p in g["parts"]:
                if p["part_key"] in in_sub:
                    continue
                if detail:
                    part_line(g, p)
                else:
                    hidden += 1
        if not detail:                       # drop OPS (column 2)
            cols = cols[:2] + cols[3:]
            rows = [r[:2] + r[3:] for r in rows]
            shift = lambda d: {(r, c - 1 if c > 2 else c): v for (r, c), v in d.items()
                               if c != 2}
            cbg, cfg, bars, tips = shift(cbg), shift(cfg), shift(bars), shift(tips)
        self.gtable.cell_bar = bars
        self.gtable.set_data(autofit_columns(cols, rows, max_w=320), rows, bg, bold,
                             align=["w", "w"] + ["center"] * (len(cols) - 2), cell_bg=cbg,
                             cell_fg=cfg)
        self.gtable.cell_tip = tips
        if hidden and not detail:
            self.gnote.setText("%d part row%s hidden - switch on Show parts for the detail.  "
                               "Click a row to see its serials on the chart."
                               % (hidden, "" if hidden == 1 else "s"))
            self.gnote.show()
        else:
            self.gnote.hide()

    # ---------------------------------------------------------------
    def do_export(self):
        data, groups = self.last["data"], self.last["groups"]
        if not data and not groups:
            QMessageBox.warning(self, "Nothing to export", "Pick a part or an analysis first.")
            return
        base = self.last["part"] or self.last["set"] or "groups"
        path, _x = QFileDialog.getSaveFileName(
            self, "Export", "LeadTime_%s.xlsx" % re.sub(r"[^A-Za-z0-9]+", "_", str(base))[:32],
            "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_leadtime_to_excel(path, self.last["part"] or "-", self.last["label"], data,
                                     unit=self.unit.get(), groups=groups,
                                     group_set=self.last["set"])
        except Exception as e:
            QMessageBox.critical(self, "Export failed", "%s\n\n%s" % (e, traceback.format_exc()))
            return
        ctx.status("Exported: " + path)

    def refresh_theme(self):
        # the chips are tints of the card colour: they are drawn again
        self.paint_kpis()
        if self.built:
            self.show_steps(self.last["data"], self.unit.get(), self.bar.parts.get_selected())
            self.draw_charts()
            self.show_groups(self.last["groups"], self.unit.get(), self.last["set"])

    def annotations_changed(self):
        if self.built:
            self.schedule()

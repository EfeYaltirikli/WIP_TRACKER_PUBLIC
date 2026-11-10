# -*- coding: utf-8 -*-
"""CUSTOM ANALYSIS - the Qt page (new design).

Same as the Tk page (page_acc.py): a kanban board over a hand-picked set
of steps.  Left, the board: one column per group, one part-coloured box per
(part, step) with the big count, a day badge per serial (dark = waiting,
orange = being worked) and the group name with "N waiting / being worked"
under it.  Right, two cards: the DAILY REPORT (serials that finished their
last chosen step, per day, stacked by part) and the WEEKLY TARGET bar.
Where a serial stands is worked out by wip_core.acc_board().
"""

import datetime

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QSize, QEvent, QPoint
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QPainterPath
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QSpinBox,
                               QMessageBox, QLineEdit, QListWidget, QListWidgetItem, QScrollArea,
                               QColorDialog, QInputDialog, QSizePolicy, QFrame, QLabel,
                               QAbstractItemView, QPushButton)

from wip_core import (WEEKDAY_NAMES, ACC_COLORS, ACC_FLAG_KINDS, acc_board, acc_check,
                      acc_delete, acc_load, acc_save, iso_year_week, norm_text, op_key,
                      route_step, save_prefs, short_part, week_bounds, week_offset)
from qt_widgets import (ctx, Splitter, Card, ModuleCard, Toggle, FlowLayout, label, button, hbox,
                        page_layout, page_head, mix, analyses_export_dialog,
                        analyses_import_dialog)
from qt_theme import FONT

RW_RED = "#E0443A"
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_ROWS_DEFAULT = 12


def ink_on(color):
    try:
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    except (TypeError, ValueError, IndexError):
        return "#000000"
    return "#16202B" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#FFFFFF"


def fmt_days(d):
    return ("%.0f" % d) if d >= 10 else ("%.1f" % d)


def mark_rework(db, definition, boxes):
    """SHOW REWORK: "rw_ops" on every serial on the board (see page_acc)."""
    steps = definition.get("steps") or []
    ranges = {}
    for p in definition.get("parts") or []:
        pk = p.get("part_key")
        route = db.wf_traveler_for(pk) if pk else []
        pos = {route_step(c): i for i, c in enumerate(route)}
        last = [pos[s["step"]] for s in steps if s.get("part_key") == pk and s.get("step") in pos]
        if p.get("start") not in pos or not last:
            continue
        ranges[pk] = [op_key(c["op"]) for c in route[pos[p["start"]] + 1:max(last) + 1]]
    entries = {}
    for (pk, _step), sers in boxes.items():
        for e in sers:
            e["rw_ops"] = []
            entries.setdefault(e["serial_key"], []).append((pk, e))
    found = {}
    keys = sorted(entries)
    for i in range(0, len(keys), 800):
        chunk = keys[i:i + 800]
        for r in db.rows("SELECT serial_no, reject_op FROM rw WHERE UPPER(REPLACE(serial_no,' ','')) "
                         "IN (%s)" % ",".join("?" * len(chunk)), tuple(chunk)):
            found.setdefault(norm_text(r["serial_no"]).replace(" ", ""), set()).add(
                op_key(r["reject_op"]))
    for sk, lst in entries.items():
        ops_ = found.get(sk)
        if not ops_:
            continue
        for pk, e in lst:
            seen = []
            for o in ranges.get(pk) or []:
                if o in ops_ and o not in seen:
                    seen.append(o)
            e["rw_ops"] = seen


def font(size, bold=False, z=1.0):
    f = QFont(FONT)
    f.setPointSizeF(max(5.0, size * z))
    f.setBold(bold)
    return f


# =====================================================================
#  THE BOARD
# =====================================================================
class Board(QWidget):
    """The kanban, painted.  Clicking a serial selects it; clicking a box
    shows all its serials (or folds it back); right-click = serial menu."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.data = None
        self.z = 1.0
        self.hits = []          # (QRectF, "ser"/"box", value)
        self.setMouseTracking(True)
        ctx.tables.append(self)

    def refresh_selection(self):
        self.update()

    def refresh_theme(self):
        self.update()

    def set_data(self, data):
        self.data = data
        self.relayout()

    def relayout(self):
        w, h = self.measure()
        self.setFixedSize(int(w), int(h))
        self.update()

    # ---- geometry shared by measure() and paint ----
    def model(self):
        d = self.data["def"]
        parts = [p for p in d.get("parts") or [] if p.get("part_key")]
        color = {p["part_key"]: p.get("color") or ACC_COLORS[i % len(ACC_COLORS)]
                 for i, p in enumerate(parts)}
        order = {p["part_key"]: i for i, p in enumerate(parts)}
        name = {p["part_key"]: short_part(p["part_key"]) or p["part_key"] for p in parts}
        groups = list(d.get("groups") or [])
        for s in d.get("steps") or []:
            g = s.get("group")
            if g and g not in groups:
                groups.append(g)
        steps_of = {g: [] for g in groups}
        for s in d.get("steps") or []:
            if s.get("group") in steps_of and s.get("part_key") in order:
                steps_of[s["group"]].append((s["part_key"], s["step"]))
        boxes = self.data["board"]["boxes"]
        cols = []
        for g in groups:
            items = sorted(steps_of[g], key=lambda k: (order[k[0]], k[1]))
            cols.append([(k, boxes.get(k, [])) for k in items])
        try:
            max_rows = max(1, int(d.get("max_rows") or MAX_ROWS_DEFAULT))
        except (TypeError, ValueError):
            max_rows = MAX_ROWS_DEFAULT
        return parts, color, name, groups, cols, max_rows

    def S(self, v):
        return v * self.z

    def shown_rows(self, k, sers, max_rows):
        if len(sers) <= max_rows or ("%s|%s" % k) in self.page.open_boxes:
            return len(sers)
        return max_rows + 1

    def heights(self, cols, max_rows):
        S = self.S
        out = []
        for items in cols:
            h = 0
            for k, sers in items:
                h += S(30) + max(1, self.shown_rows(k, sers, max_rows)) * S(30) + S(12) + S(10)
            out.append(h)
        return out

    def measure(self):
        if not self.data:
            return 400, 200
        S = self.S
        parts, color, name, groups, cols, max_rows = self.model()
        hs = self.heights(cols, max_rows)
        show_rw = bool(self.data["def"].get("show_rework"))
        w = S(10) + max(1, len(groups)) * (S(270) + S(18))
        h = S(60) + max(hs or [0]) + S(110 if show_rw else 92)
        return max(w, 300), max(h, 200)

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(th["card"]))
        self.hits = []
        if not self.data:
            p.setPen(QColor(th["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, self.page.empty_text)
            p.end()
            return
        S = self.S
        z = self.z
        parts, color, name, groups, cols, max_rows = self.model()
        board = self.data["board"]
        show_rw = bool(self.data["def"].get("show_rework"))
        hs = self.heights(cols, max_rows)
        COL_W, GAP = S(270), S(18)
        top_y = S(50)
        base_y = top_y + max(hs or [0])
        x0 = S(4)
        for ci, (g, items) in enumerate(zip(groups, cols)):
            cx = x0 + ci * (COL_W + GAP)
            yy = base_y - hs[ci]          # bottom aligned, a stack on the station
            total = n_rw = 0
            for k, sers in items:
                pk, step = k
                n = len(sers)
                total += n
                n_rw += sum(1 for e in sers if e.get("rw_ops"))
                rows_n = self.shown_rows(k, sers, max_rows)
                h = S(30) + max(1, rows_n) * S(30) + S(12)
                fill = color[pk] if n else th["card"]
                r = QRectF(cx, yy, COL_W, h)
                p.setPen(QPen(QColor(mix(color[pk], th["text"], 0.15) if n else th["border"]),
                              max(1.0, S(1.2))))
                p.setBrush(QColor(fill if n else th["surf2"]))
                p.drawRoundedRect(r, S(8), S(8))
                self.hits.append((r, "box", "%s|%s" % k))
                ink = ink_on(fill) if n else th["text"]
                # the count, big on the left
                p.setPen(QColor(ink))
                p.setFont(font(26 if n < 100 else 19, True, z))
                p.drawText(QRectF(cx, yy, S(56), h), Qt.AlignCenter, str(n))
                p.setFont(font(10, True, z))
                p.drawText(QRectF(cx + S(60), yy + S(6), COL_W - S(64), S(20)),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           "%s · %s" % (name[pk], board["labels"].get(k, step)))
                ry = yy + S(30)
                cut = rows_n < n
                for e in (sers[:max_rows] if cut else sers):
                    bx = cx + S(60)
                    badge = QRectF(bx, ry + S(4), S(40), S(22))
                    p.setPen(Qt.NoPen)
                    picked = ctx.selected_serial and norm_text(e["serial"]) == ctx.selected_serial
                    p.setBrush(QColor(th["wf_running"] if e["running"] else "#16202B"))
                    p.drawRoundedRect(badge, S(11), S(11))
                    p.setPen(QColor("#FFFFFF"))
                    p.setFont(font(9, True, z))
                    p.drawText(badge, Qt.AlignCenter, fmt_days(e["days"]))
                    p.setPen(QColor(ink))
                    p.setFont(font(10.5, True, z))
                    txt = e["serial"] + ("   > working" if e["running"] else "")
                    tr = QRectF(bx + S(48), ry, COL_W - S(112), S(30))
                    p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, txt)
                    tw = QFontMetrics(font(10.5, True, z)).horizontalAdvance(txt)
                    if picked:
                        p.setPen(QPen(QColor(th["sel_edge"]), max(1.0, S(2))))
                        p.setBrush(Qt.NoBrush)
                        p.drawRoundedRect(QRectF(bx - S(3), ry + S(1), tw + S(56), S(28)),
                                          S(6), S(6))
                    if e.get("rw_ops"):
                        dx = bx + S(48) + tw + S(10)
                        p.setPen(QPen(QColor("#FFFFFF"), 1))
                        p.setBrush(QColor(RW_RED))
                        p.drawEllipse(QPointF(dx, ry + S(15)), S(5), S(5))
                        room = cx + COL_W - S(8) - (dx + S(9))
                        fm = QFontMetrics(font(9, True, z))
                        ops_ = e["rw_ops"]
                        t_ = ""
                        for k_ in range(len(ops_), -1, -1):
                            rest = len(ops_) - k_
                            cand = ("RW " + ", ".join(ops_[:k_]) + ((" +%d" % rest) if rest else "")
                                    if k_ else "+%d" % rest)
                            if fm.horizontalAdvance(cand) <= room:
                                t_ = cand
                                break
                        if t_:
                            p.setPen(QColor(RW_RED if ink != "#FFFFFF" else "#FFB3AD"))
                            p.setFont(font(9, True, z))
                            p.drawText(QRectF(dx + S(9), ry, room, S(30)),
                                       Qt.AlignLeft | Qt.AlignVCenter, t_)
                        self.page.rw_of[e["serial"]] = ops_
                    self.hits.append((QRectF(bx, ry, COL_W - S(64), S(30)), "ser", e["serial"]))
                    ry += S(30)
                if cut:
                    p.setPen(QColor(ink))
                    p.setFont(font(9, True, z))
                    p.drawText(QRectF(cx + S(60), ry, COL_W - S(64), S(30)),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               "+ %d more   (click to show all)" % (n - max_rows))
                elif n > max_rows:
                    p.setPen(QColor(ink))
                    p.setFont(font(8, False, z))
                    p.drawText(QRectF(cx, yy + S(6), COL_W - S(10), S(18)),
                               Qt.AlignRight | Qt.AlignVCenter, "click to fold")
                yy += h + S(10)
            # the station under its column
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(th["grid_strong"]))
            p.drawRoundedRect(QRectF(cx, base_y + S(4), COL_W, S(3)), 1, 1)
            p.setPen(QColor(th["text"]))
            p.setFont(font(14, True, z))
            p.drawText(QRectF(cx, base_y + S(12), COL_W, S(26)), Qt.AlignCenter, g)
            p.setPen(QColor(th["muted"]))
            p.setFont(font(10, False, z))
            p.drawText(QRectF(cx, base_y + S(38), COL_W, S(20)), Qt.AlignCenter,
                       "%d waiting / being worked" % total)
            if show_rw:
                p.setPen(QColor(RW_RED))
                p.setFont(font(10, True, z))
                p.drawText(QRectF(cx, base_y + S(58), COL_W, S(20)), Qt.AlignCenter,
                           "%d with rework (RW)" % n_rw)
        # the key
        ky = base_y + S(84 if show_rw else 66)
        p.setFont(font(9.5, False, z))
        p.setPen(QColor(th["muted"]))
        txt = "Badge = days since the last entry on the chosen steps."
        p.drawText(QRectF(x0, ky, S(900), S(20)), Qt.AlignLeft | Qt.AlignVCenter, txt)
        kx = x0 + QFontMetrics(font(9.5, False, z)).horizontalAdvance(txt) + S(14)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(th["wf_running"]))
        p.drawEllipse(QPointF(kx + S(6), ky + S(10)), S(6), S(6))
        p.setPen(QColor(th["muted"]))
        t2 = "being worked (entry, no X yet)"
        p.drawText(QRectF(kx + S(16), ky, S(300), S(20)), Qt.AlignLeft | Qt.AlignVCenter, t2)
        kx += S(16) + QFontMetrics(font(9.5, False, z)).horizontalAdvance(t2) + S(14)
        p.setPen(QPen(QColor(th["muted"]), 1))
        p.setBrush(QColor("#16202B"))
        p.drawEllipse(QPointF(kx + S(6), ky + S(10)), S(6), S(6))
        p.drawText(QRectF(kx + S(16), ky, S(200), S(20)), Qt.AlignLeft | Qt.AlignVCenter, "waiting")
        p.end()

    def mousePressEvent(self, ev):
        pt = ev.position()
        for r, kind, val in reversed(self.hits):
            if r.contains(pt):
                if kind == "ser":
                    if ev.button() == Qt.RightButton:
                        ctx.set_selected_serial(val)
                        m = ctx.hooks.get("serial_menu")
                        if m:
                            m(val, ev.globalPosition().toPoint())
                        return
                    ctx.toggle_selected_serial(val)
                    rw = self.page.rw_of.get(val)
                    ctx.status(("Serial %s selected.%s" % (val, ("   Rework (RW) at op: " +
                                                                ", ".join(rw)) if rw else ""))
                               if ctx.selected_serial else "Selection cleared.")
                    return
                if kind == "box" and ev.button() == Qt.LeftButton:
                    self.page.open_boxes.symmetric_difference_update({val})
                    self.relayout()
                    return

    def mouseMoveEvent(self, ev):
        pt = ev.position()
        hand = any(r.contains(pt) for r, _k, _v in self.hits)
        self.setCursor(Qt.PointingHandCursor if hand else Qt.ArrowCursor)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            self.page.zoom_step(1 if ev.angleDelta().y() > 0 else -1)
            return
        super().wheelEvent(ev)


class DailyChart(QWidget):
    """Per day of the week, serials that closed their last chosen step,
    stacked by part, the day's total in a dark circle over the bar."""

    def __init__(self):
        super().__init__()
        self.data = None
        self.setFixedHeight(150)          # v71: compact by default

    def set_data(self, data):
        self.data = data
        self.update()

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.data or self.data.get("d1") is None:
            p.end()
            return
        d = self.data["def"]
        parts = [q for q in d.get("parts") or [] if q.get("part_key")]
        color = {q["part_key"]: q.get("color") or ACC_COLORS[i % len(ACC_COLORS)]
                 for i, q in enumerate(parts)}
        fin = self.data["board"]["finished"]
        days = [self.data["d1"] + datetime.timedelta(days=i) for i in range(7)]
        per_day = [[(q["part_key"], fin.get((q["part_key"], dd), 0)) for q in parts] for dd in days]
        totals = [sum(n for _pk, n in row) for row in per_day]
        peak = max(totals + [1])
        W, H = self.width(), self.height()
        top, bot = 22, H - 34
        slot = W / 7.0
        # v69 (ACC1): wide bars, rounded on top, the day's total over them
        bw = max(10.0, slot * 0.86)
        for i, (dd, row) in enumerate(zip(days, per_day)):
            cx = slot * (i + 0.5)
            yy = bot
            segs = [(pk, n) for pk, n in row if n]
            if segs:
                full = (bot - top) * totals[i] / float(peak)
                path = QPainterPath()
                path.addRoundedRect(QRectF(cx - bw / 2, bot - full, bw, full + 6), 6, 6)
                p.save()
                p.setClipPath(path)
                p.setClipRect(QRectF(cx - bw / 2, top - 30, bw, bot - top + 30), Qt.IntersectClip)
                for pk, n in segs:
                    h = (bot - top) * n / float(peak)
                    r = QRectF(cx - bw / 2, yy - h, bw, h)
                    p.fillRect(r, QColor(color[pk]))
                    if h >= 18 and len(segs) > 1:
                        p.setPen(QColor(ink_on(color[pk])))
                        p.setFont(font(9.5, True))
                        p.drawText(r, Qt.AlignCenter, str(n))
                    yy -= h
                p.restore()
                p.setPen(QColor(th["text"]))
                p.setFont(font(11, True))
                p.drawText(QRectF(cx - slot / 2, yy - 24, slot, 20), Qt.AlignCenter,
                           str(totals[i]))
            p.setPen(QColor(th["text"]))
            p.setFont(font(10.5, True))
            p.drawText(QRectF(cx - slot / 2, bot + 2, slot, 16), Qt.AlignCenter,
                       DAY_SHORT[dd.weekday()])
            p.setPen(QColor(th["muted"]))
            p.setFont(font(9.5))
            p.drawText(QRectF(cx - slot / 2, bot + 17, slot, 15), Qt.AlignCenter,
                       dd.strftime("%d.%m"))
        p.end()


class TargetBars(QWidget):
    """Per STG group: finished this week (stacked by part) against the target."""

    def __init__(self):
        super().__init__()
        self.data = None
        self.setMinimumHeight(60)

    def set_data(self, data):
        self.data = data
        d = (data or {}).get("def") or {}
        stgs = {(q.get("stg") or "").strip() for q in d.get("parts") or []
                if (q.get("stg") or "").strip()}
        self.setFixedHeight(max(1, len(stgs)) * 44 + 4)
        self.update()

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.data:
            p.end()
            return
        d = self.data["def"]
        parts = [q for q in d.get("parts") or [] if q.get("part_key")]
        color = {q["part_key"]: q.get("color") or ACC_COLORS[i % len(ACC_COLORS)]
                 for i, q in enumerate(parts)}
        fin = self.data["board"]["finished"]
        targets = d.get("targets") or {}
        stgs = []
        for q in parts:
            s = (q.get("stg") or "").strip()
            if s and s not in stgs:
                stgs.append(s)
        if not stgs:
            p.setPen(QColor(th["muted"]))
            p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter,
                       "Give parts an STG group and a weekly target in Edit to see this.")
            p.end()
            return
        done_by = {}
        for (pk, _day), n in fin.items():
            done_by[pk] = done_by.get(pk, 0) + n
        W = self.width()
        y = 0
        for s in stgs:
            members = [q["part_key"] for q in parts if (q.get("stg") or "").strip() == s]
            done = [(pk, done_by.get(pk, 0)) for pk in members]
            n_done = sum(n for _pk, n in done)
            try:
                target = int(targets.get(s) or 0)
            except (TypeError, ValueError):
                target = 0
            left = max(0, target - n_done)
            scale = max(target, n_done, 1)
            # v69 (ACC1): name and "done / target · n to go" over a slim bar
            p.setPen(QColor(th["text"]))
            p.setFont(font(10.5, True))
            p.drawText(QRectF(0, y, W, 22), Qt.AlignLeft | Qt.AlignVCenter, s)
            p.setPen(QColor(th["muted"]))
            p.setFont(font(10))
            p.drawText(QRectF(0, y, W, 22), Qt.AlignRight | Qt.AlignVCenter,
                       "%d / %s · %s" % (n_done, target or "-",
                                         ("%d to go" % left) if left else "reached"))
            bh, by = 14, y + 24
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, by, W, bh), 8, 8)
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(0, by, W, bh), QColor(th["surf2"]))
            bx = 0.0
            for pk, n in done:
                if not n:
                    continue
                w = W * n / float(scale)
                p.fillRect(QRectF(bx, by, w, bh), QColor(color[pk]))
                if w > 18 and len(done) > 1:
                    p.setPen(QColor(ink_on(color[pk])))
                    p.setFont(font(9.5, True))
                    p.drawText(QRectF(bx, by, w, bh), Qt.AlignCenter, str(n))
                bx += w
            p.restore()
            y += 44
        p.end()


# =====================================================================
#  THE PAGE
# =====================================================================
class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.data = None
        self.open_boxes = set()
        self.rw_of = {}
        self.empty_text = "Pick an analysis."
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        outer = page_layout(self)
        # v69: the board and the editor (ED1) share the page - one at a time
        self.board_view = QWidget()
        bv = QVBoxLayout(self.board_view)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(12)
        outer.addWidget(self.board_view, 1)
        self.editor = EditorView(self)
        self.editor.hide()
        outer.addWidget(self.editor, 1)

        page_head(bv, "Custom Analysis", "Hand-picked steps as a board, with weekly targets",
                  button("+ New", slot=lambda: self.open_setup(None)),
                  button("Edit", slot=lambda: self.open_setup(self.name_cb.currentText() or None)),
                  button("Delete", danger=True, slot=self.delete_analysis),
                  # 2.3.0: the saved analyses as an Excel file, and back
                  button("Export analyses", slot=lambda: analyses_export_dialog(self),
                         tip="Every saved analysis (custom and lead time) into one Excel "
                             "file - a copy that outlives the database."),
                  button("Import analyses", slot=self.import_analyses,
                         tip="Builds the analyses again from such an Excel file."))

        # one row that wraps on a narrow window (never a sideways page)
        row_w = QWidget()
        row = FlowLayout(row_w, 10)
        self.name_cb = QComboBox()
        self.name_cb.setMinimumWidth(220)
        self.name_cb.activated.connect(lambda _i: self.on_pick())
        row.addWidget(hbox(label("Analysis", muted=True), self.name_cb, spacing=8))
        # the week: "FW 40  2026 · 25.09 – 01.10" between < and >; a click
        # on it opens the year / week boxes
        self.year = QSpinBox()
        self.year.setRange(2000, 2100)
        self.week = QSpinBox()
        self.week.setRange(1, 53)
        self.week.setPrefix("FW ")
        for w in (self.year, self.week):
            w.valueChanged.connect(lambda _v: self.schedule())
            w.valueChanged.connect(lambda _v: self.paint_week_btn())
        self.week_pop = QFrame(None, Qt.Popup)
        self.week_pop.setProperty("popup", True)
        wl = QHBoxLayout(self.week_pop)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.addWidget(hbox(label("Year", muted=True), self.year, label("Week", muted=True),
                          self.week, spacing=6))
        self.week_btn = button("", slot=self.open_week_pop)
        self.week_btn.setMinimumWidth(230)
        row.addWidget(hbox(label("Factory week", muted=True),
                           button("‹", icon_only=True, slot=lambda: self.shift_week(-1)),
                           self.week_btn,
                           button("›", icon_only=True, slot=lambda: self.shift_week(1)),
                           button("This week", slot=self.this_week), spacing=6))
        # off by default: the board is about what is on the floor
        self.t_all = Toggle("Show sold / TECO / MRB / SCRAP", slot=self.schedule)
        row.addWidget(self.t_all)
        self.zoom_lbl = label("100%")
        row.addWidget(hbox(label("Zoom", muted=True),
                           button("−", icon_only=True, slot=lambda: self.zoom_step(-1)),
                           self.zoom_lbl,
                           button("+", icon_only=True, slot=lambda: self.zoom_step(1)),
                           button("Fit", slot=self.fit), spacing=6))
        bv.addWidget(row_w)
        self.week_lbl = label("", muted=True)
        self.info_lbl = label("", muted=True, wrap=True)
        bv.addWidget(self.info_lbl)

        # the board | the side panel, a handle between; the side panel can
        # be hidden to a thin strip and the board takes the room (ACC1)
        self.board_card = ModuleCard("Waiting parts kanban", "")
        self.board_card.fold_btn.hide()
        self.board_card.title_lbl.setTextFormat(Qt.RichText)
        self.legend = QLabel("")
        self.legend.hide()
        # v72: the fit note ("Fit uses the room ...") is no longer shown
        self.fit_lbl = label("", muted=True)
        self.fit_lbl.hide()
        self.board = Board(self)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.board)
        self.scroll.setWidgetResizable(False)
        self.scroll.setMinimumHeight(420)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                  "{ background: transparent; }")
        self.scroll.viewport().installEventFilter(self)
        self.board_card.content_lay.addWidget(self.scroll, 1)
        self.board_card.setMinimumWidth(360)
        self.fit_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.info_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.fit_on = False

        self.side = QWidget()
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(12)
        self.daily_card = ModuleCard("Daily report", "")
        # v71: in the card's own head, so the side cards line up with the
        # board's top edge
        self.daily_card.add_extra(button("Hide  ›", slot=lambda: self.set_side(False)))
        self.daily_note = label("serials that finished their LAST chosen step (its X) that day",
                                muted=True, wrap=True)
        self.daily_card.content_lay.addWidget(self.daily_note)
        self.daily = DailyChart()
        self.daily_card.content_lay.addWidget(self.daily)
        sl.addWidget(self.daily_card)
        self.target_card = ModuleCard("Weekly target", "")
        self.targets = TargetBars()
        self.target_card.content_lay.addWidget(self.targets)
        sl.addWidget(self.target_card)
        sl.addStretch(1)
        for c in (self.daily_card, self.target_card):
            c.win_btn.hide()
        self.side.setMinimumWidth(250)

        self.split = Splitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(10)
        self.split.addWidget(self.board_card)
        self.split.addWidget(self.side)
        self.split.setStretchFactor(0, 4)
        self.split.setStretchFactor(1, 1)
        self._side_sized = False
        self.strip = QPushButton("‹")
        self.strip.setToolTip("Show side panel")
        self.strip.setCursor(Qt.PointingHandCursor)
        self.strip.setFixedWidth(22)
        self.strip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.strip.clicked.connect(lambda: self.set_side(True))
        self.strip.hide()
        body = QHBoxLayout()
        body.setSpacing(6)
        body.addWidget(self.split, 1)
        body.addWidget(self.strip)
        bv.addLayout(body, 1)
        if ctx.prefs.get("acc_side_hidden"):
            self.set_side(False, save=False)

    def set_side(self, on, save=True):
        """Side panel shown, or hidden to the thin strip (the board grows)."""
        self.side.setVisible(on)
        self.strip.setVisible(not on)
        if save:
            ctx.prefs["acc_side_hidden"] = not on
            try:
                save_prefs(ctx.prefs)
            except Exception:
                pass

    def eventFilter(self, obj, ev):
        # Fit follows the room the board has: a handle moved or the side
        # panel hidden refits it
        if obj is self.scroll.viewport() and ev.type() == QEvent.Resize and self.fit_on:
            QTimer.singleShot(0, self.fit)
        return super().eventFilter(obj, ev)

    def open_week_pop(self):
        self.week_pop.setStyleSheet("QFrame[popup=\"true\"] { background: %s; border: 1px solid "
                                    "%s; border-radius: 10px; }" % (ctx.TH["card"], ctx.TH["accent"]))
        self.week_pop.adjustSize()
        self.week_pop.move(self.week_btn.mapToGlobal(self.week_btn.rect().bottomLeft())
                           + QPoint(0, 6))
        self.week_pop.show()

    def paint_week_btn(self):
        d1, d2 = self.current_week()
        txt = "FW %d   %d" % (self.week.value(), self.year.value())
        if d1:
            txt += " · %s – %s" % (d1.strftime("%d.%m"), d2.strftime("%d.%m"))
        self.week_btn.setText(txt)

    # ---------------------------------------------------------------
    def week_start(self):
        return ctx.db.week_start("acc")

    def this_week(self):
        self.week_pop.hide()
        y, w = iso_year_week(datetime.date.today(), week_offset(self.week_start()))
        self.year.blockSignals(True)
        self.year.setValue(y)
        self.year.blockSignals(False)
        self.week.setValue(w)
        self.schedule()

    def shift_week(self, n):
        d1, _d2 = self.current_week()
        if d1 is None:
            return self.this_week()
        y, w = iso_year_week(d1 + datetime.timedelta(days=7 * n + 3), week_offset(self.week_start()))
        self.year.blockSignals(True)
        self.year.setValue(y)
        self.year.blockSignals(False)
        self.week.setValue(w)

    def current_week(self):
        try:
            return week_bounds(self.year.value(), self.week.value(), self.week_start())
        except (TypeError, ValueError):
            return None, None

    def zoom_step(self, d):
        self.fit_on = False
        self.fit_lbl.setText("")
        self.board.z = max(0.3, min(1.8, round(self.board.z * 10 + d) / 10.0))
        self.zoom_lbl.setText("%d%%" % round(self.board.z * 100))
        self.board.relayout()

    FIT_MIN, FIT_MAX = 0.7, 1.8

    def fit(self):
        """v69: the board fills the width it has now, never below 70% (more
        groups then scroll sideways) nor above 180%; it keeps fitting while
        the handle moves or the side panel opens / closes."""
        if not self.data:
            return
        self.fit_on = True
        self.board.z = 1.0
        w, _h = self.board.measure()
        avail = self.scroll.viewport().width() - 4
        z = avail / float(w) if w else 1.0
        z = max(self.FIT_MIN, min(self.FIT_MAX, int(z * 20) / 20.0))
        self.board.z = z
        self.zoom_lbl.setText("%d%%" % round(z * 100))
        self.board.relayout()

    # ---- analyses ----
    def refresh_sources(self):
        names = sorted(acc_load(ctx.db), key=lambda s: s.upper())
        cur = self.name_cb.currentText() or ctx.prefs.get("acc_last", "")
        self.name_cb.blockSignals(True)
        self.name_cb.clear()
        self.name_cb.addItems(names)
        i = self.name_cb.findText(cur)
        self.name_cb.setCurrentIndex(i if i >= 0 else (0 if names else -1))
        self.name_cb.blockSignals(False)

    def on_pick(self):
        ctx.prefs["acc_last"] = self.name_cb.currentText()
        try:
            save_prefs(ctx.prefs)
        except Exception:
            pass
        self.open_boxes.clear()
        self.schedule()

    def import_analyses(self):
        if analyses_import_dialog(self):
            self.refresh_sources()
            self.schedule()

    def delete_analysis(self):
        n = self.name_cb.currentText()
        if not n:
            return
        if ctx.db.read_only:
            QMessageBox.warning(self, "Delete", "The database is open read-only.")
            return
        if QMessageBox.question(self, "Delete", "Delete the analysis '%s' for everybody?" % n) \
                != QMessageBox.Yes:
            return
        acc_delete(ctx.db, n)
        self.refresh_sources()
        self.schedule()

    # ---- refresh ----
    def on_show(self):
        if not self.built:
            self.built = True
            self.refresh_sources()
            self.this_week()
        self.timer.stop()
        self.refresh()
        if not self._side_sized:
            # v71: the board gets the room, the side cards start narrow
            self._side_sized = True
            QTimer.singleShot(0, self._size_side)

    def _size_side(self):
        w = self.split.width()
        if w > 600:
            self.split.setSizes([w - 290, 290])

    def reload(self):
        if self.built:
            self.refresh_sources()
            self.schedule()

    def schedule(self):
        if self.built:
            self.timer.start(200)

    def refresh(self):
        if not self.built:
            return
        self.timer.stop()
        name = self.name_cb.currentText()
        definition = acc_load(ctx.db).get(name) if name else None
        if not definition:
            self.data = None
            self.empty_text = ("No analysis yet - press New to pick parts, steps and groups."
                               if self.name_cb.count() == 0 else "Pick an analysis.")
            self.info_lbl.setText(self.empty_text)
            for w in (self.board, self.daily, self.targets):
                w.set_data(None)
            return
        d1, d2 = self.current_week()
        bad = set()
        if not self.t_all.isChecked():
            bad = set(ctx.db.sold_serial_keys()) | ctx.db.flag_keys(ACC_FLAG_KINDS)
        ctx.busy(True, "Building custom analysis...")
        try:
            board = acc_board(ctx.db, definition, exclude_serial_keys=bad, d1=d1, d2=d2,
                              exclude_finished_keys=set())
            if definition.get("show_rework"):
                mark_rework(ctx.db, definition, board["boxes"])
        except Exception as e:
            ctx.status("Custom analysis error: %s" % e)
            return
        finally:
            ctx.busy(False)
        wk = self.week.value()
        self.data = {"name": name, "def": definition, "board": board, "d1": d1, "d2": d2,
                     "week": (self.year.value(), wk)}
        self.rw_of.clear()
        n_on = sum(len(v) for v in board["boxes"].values())
        n_run = sum(1 for v in board["boxes"].values() for e in v if e["running"])
        txt = ("%d serial(s) on the board, %d of them being worked.   %s   Ctrl + wheel to zoom, "
               "right-click a serial for its menu." % (
                   n_on, n_run, ("%s - %s (week starts %s)." % (
                       d1.strftime("%d.%m.%Y"), d2.strftime("%d.%m.%Y"),
                       WEEKDAY_NAMES[self.week_start()])) if d1 else ""))
        if board["problems"]:
            txt = "CHECK THE SETUP: " + "  ".join(board["problems"][:4]) + "     " + txt
        self.info_lbl.setText(txt)
        parts = [p for p in definition.get("parts") or [] if p.get("part_key")]
        self.legend.setText("   ".join(
            "<span style='color:%s'>■</span> <b>%s</b> <span style='color:%s'>%s</span>"
            % (p.get("color") or ACC_COLORS[i % len(ACC_COLORS)],
               short_part(p["part_key"]) or p["part_key"], ctx.TH["muted"], p["part_key"])
            for i, p in enumerate(parts)))
        self.board_card.title_lbl.setText(
            "<b>%s · waiting parts kanban</b>&nbsp;&nbsp;&nbsp;%s" % (name, self.legend.text()))
        self.daily_card.title_lbl.setText("FW%s · Daily report" % wk)
        self.target_card.title_lbl.setText("FW%s · Weekly target" % wk)
        self.board.set_data(self.data)
        self.daily.set_data(self.data)
        self.targets.set_data(self.data)
        self.paint_week_btn()
        if self.fit_on:
            QTimer.singleShot(0, self.fit)

    def refresh_theme(self):
        if self.data:
            self.refresh()

    def annotations_changed(self):
        self.schedule()

    # ---- setup (ED1: the editor takes the page, "Back to board") ----
    def open_setup(self, edit_name):
        if ctx.db.read_only:
            QMessageBox.warning(self, "Custom analysis",
                                "The database is open read-only - an analysis cannot be saved.")
            return
        self.editor.load(edit_name)
        self.board_view.hide()
        self.editor.show()

    def close_setup(self, saved_name=None):
        self.editor.hide()
        self.board_view.show()
        if saved_name:
            ctx.prefs["acc_last"] = saved_name
            try:
                save_prefs(ctx.prefs)
            except Exception:
                pass
            self.refresh_sources()
            i = self.name_cb.findText(saved_name)
            if i >= 0:
                self.name_cb.setCurrentIndex(i)
            self.schedule()


# =====================================================================
#  THE EDITOR (v69 ED1): one page, three columns - Parts | Steps of the
#  picked part | Groups, targets, options.  Same data and the same rules
#  as the old setup window (page_acc.py); only the layout changed.
# =====================================================================
START_COLOR = "#E3B341"
GROUP_COLORS = ["#4FA3D1", "#9B7BE0", "#E8913A", "#4CB782", "#D45D9E", "#C9A400",
                "#5B8DEF", "#E05A5A"]


def step_badge(n):
    b = QLabel(str(n))
    b.setFixedSize(24, 24)
    b.setAlignment(Qt.AlignCenter)
    b.setStyleSheet("background: %s; color: #FFFFFF; border-radius: 12px; font-weight: 700;"
                    % ctx.TH["accent"])
    return b


class ClickFrame(QFrame):
    """A row that answers a click (part rows, step rows)."""

    def __init__(self, on_click=None):
        super().__init__()
        self.on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.on_click:
            self.on_click()
        super().mousePressEvent(ev)


class GroupList(QListWidget):
    """The groups, left to right on the board - drag ⠿ to reorder."""

    def __init__(self, on_moved):
        super().__init__()
        self.on_moved = on_moved
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSpacing(3)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QListWidget { background: transparent; } "
                           "QListWidget::item { border: none; }")

    def dropEvent(self, ev):
        super().dropEvent(ev)
        QTimer.singleShot(0, self.on_moved)


class GroupPreview(QWidget):
    """A small board: one column per group, a box per part in it."""

    def __init__(self):
        super().__init__()
        self.cols = []
        self.setMinimumHeight(90)

    def set_cols(self, cols):
        self.cols = cols
        n = max([len(c[1]) for c in cols] or [1])
        self.setFixedHeight(24 + n * 24 + 26)
        self.update()

    def paintEvent(self, ev):
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.cols:
            p.setPen(QColor(th["muted"]))
            p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, "No groups yet.")
            p.end()
            return
        n = len(self.cols)
        gap = 8.0
        w = (self.width() - gap * (n - 1)) / n
        rows = max([len(c[1]) for c in self.cols] or [1])
        base = 4 + rows * 24
        for i, (g, colors) in enumerate(self.cols):
            x = i * (w + gap)
            y = base - len(colors) * 24
            for c in colors:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(c))
                p.drawRoundedRect(QRectF(x, y + 2, w, 20), 5, 5)
                y += 24
            p.setBrush(QColor(th["grid_strong"]))
            p.drawRect(QRectF(x, base + 3, w, 2))
            p.setPen(QColor(th["text"]))
            p.setFont(font(9, True))
            p.drawText(QRectF(x, base + 6, w, 20), Qt.AlignCenter, g)
        p.end()


class EditorView(QWidget):
    START = "START"

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.edit_name = None
        self.saved_name = None
        self.work = {"parts": [], "steps": [], "groups": [], "targets": {},
                     "show_rework": False, "max_rows": MAX_ROWS_DEFAULT}
        self.routes = {}
        self.all_parts = []
        self.cur = -1
        self.brush = None
        self.extra_groups = []
        self.tvars = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(button("‹  Back to board", slot=self.cancel), 0, Qt.AlignTop)
        tl = QVBoxLayout()
        tl.setSpacing(2)
        self.title = label("Edit analysis", obj="PageTitle")
        tl.addWidget(self.title)
        tl.addWidget(label("Pick the parts, give their steps a group, order the groups",
                           obj="PageSub"))
        head.addLayout(tl)
        head.addStretch(1)
        self.name_ed = QLineEdit()
        self.name_ed.setMinimumWidth(260)
        head.addWidget(label("Name", muted=True))
        head.addWidget(self.name_ed)
        head.addWidget(button("Cancel", slot=self.cancel))
        head.addWidget(button("Save", primary=True, slot=self.save))
        lay.addLayout(head)

        self.split = Splitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(10)
        lay.addWidget(self.split, 1)

        # ---- 1  parts ----
        c1 = Card()
        self.parts_head = label("", wrap=False)
        c1.add(hbox(step_badge(1), label("Parts", obj="CardTitle"), self.parts_head, None,
                    spacing=8))
        self.parts_box = QWidget()
        self.parts_lay = QVBoxLayout(self.parts_box)
        self.parts_lay.setContentsMargins(0, 0, 0, 0)
        self.parts_lay.setSpacing(6)
        c1.add(self.parts_box)
        c1.add(hbox(button("Colour", slot=self.pick_color),
                    button("STG group", slot=self.set_stg),
                    button("Remove", danger=True, slot=self.remove_part), None, spacing=6))
        c1.add(label("Add a part", muted=True))
        self.find = QLineEdit()
        self.find.setPlaceholderText("Find a part to add")
        self.find.textChanged.connect(self.fill_avail)
        c1.add(self.find)
        self.avail = QListWidget()
        self.avail.setFrameShape(QFrame.NoFrame)
        self.avail.setSpacing(2)
        self.avail.setStyleSheet("QListWidget { background: transparent; } "
                                 "QListWidget::item { border: none; }")
        self.avail.itemDoubleClicked.connect(lambda _i: self.add_part())
        c1.add(self.avail, 1)
        c1.setMinimumWidth(300)
        self.split.addWidget(c1)

        # ---- 2  steps ----
        c2 = Card()
        self.steps_title = label("Steps", obj="CardTitle")
        self.steps_sub = label("", muted=True)
        self.steps_sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        c2.add(hbox(step_badge(2), self.steps_title, self.steps_sub, spacing=8))
        self.brush_box = QWidget()
        self.brush_lay = FlowLayout(self.brush_box, 6)
        c2.add(self.brush_box)
        self.t_only = Toggle("Only grouped steps", slot=self.fill_steps)
        c2.add(hbox(self.t_only, None))
        self.steps_scroll = QScrollArea()
        self.steps_scroll.setWidgetResizable(True)
        self.steps_scroll.setFrameShape(QFrame.NoFrame)
        self.steps_host = QWidget()
        self.steps_lay = QVBoxLayout(self.steps_host)
        self.steps_lay.setContentsMargins(0, 0, 0, 0)
        self.steps_lay.setSpacing(2)
        self.steps_lay.setAlignment(Qt.AlignTop)
        self.steps_scroll.setWidget(self.steps_host)
        self.steps_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                        "{ background: transparent; }")
        c2.add(self.steps_scroll, 1)
        c2.setMinimumWidth(420)
        self.split.addWidget(c2)

        # ---- 3  groups, targets, options ----
        c3 = Card()
        c3.add(hbox(step_badge(3), label("Groups, targets, options", obj="CardTitle"), None,
                    spacing=8))
        c3.add(label("Groups, left to right on the board – drag ⠿ to reorder", muted=True))
        self.glist = GroupList(self.groups_moved)
        self.glist.setMinimumHeight(120)
        c3.add(self.glist, 1)
        c3.add(button("+ Add group", slot=self.add_group))
        c3.add(label("Weekly targets (pieces / week)", obj="CardTitle"))
        self.tbox = QGridLayout()
        tw = QWidget()
        tw.setLayout(self.tbox)
        c3.add(tw)
        c3.add(label("Options", obj="CardTitle"))
        self.t_rw = Toggle("Show rework")
        c3.add(hbox(self.t_rw, None))
        c3.add(label("Red dot + op next to a serial rejected (RW) after START up to "
                     "the last chosen step.", muted=True, wrap=True))
        self.max_rows = QSpinBox()
        self.max_rows.setRange(1, 500)
        c3.add(hbox(label("Serials per box"), None, self.max_rows, spacing=8))
        c3.add(label("Preview", obj="CardTitle"))
        self.preview = GroupPreview()
        c3.add(self.preview)
        self.prob = label("", wrap=True)
        c3.add(self.prob)
        c3.setMinimumWidth(320)
        self.split.addWidget(c3)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 5)
        self.split.setStretchFactor(2, 3)

    # ---------------------------------------------------------------
    def load(self, edit_name):
        self.edit_name = edit_name
        self.saved_name = None
        src = acc_load(ctx.db).get(edit_name) if edit_name else None
        src = src or {}
        self.work = {"parts": [dict(p) for p in src.get("parts", [])],
                     "steps": [dict(s) for s in src.get("steps", [])],
                     "groups": list(src.get("groups", [])),
                     "targets": dict(src.get("targets", {})),
                     "show_rework": bool(src.get("show_rework")),
                     "max_rows": int(src.get("max_rows") or MAX_ROWS_DEFAULT)}
        self.routes = {}
        self.all_parts = ctx.db.wf_parts(ctx.db.merge_on("acc"))
        self.extra_groups = []
        self.brush = None
        self.title.setText("Edit analysis" if edit_name else "New analysis")
        self.name_ed.setText(edit_name or "")
        self.t_rw.setChecked(self.work["show_rework"])
        self.max_rows.setValue(self.work["max_rows"])
        self.prob.setText("")
        self.find.clear()
        self.cur = 0 if self.work["parts"] else -1
        self.fill_avail()
        self.fill_chosen(0)
        self.fill_groups()
        self.fill_targets()

    def cancel(self):
        self.page.close_setup(None)

    def route_of(self, pk):
        if pk not in self.routes:
            self.routes[pk] = ctx.db.wf_traveler_for(pk)
        return self.routes[pk]

    def group_color(self, g):
        if g == self.START:
            return START_COLOR
        names = self.group_names()
        i = names.index(g) if g in names else 0
        return GROUP_COLORS[i % len(GROUP_COLORS)]

    # ---- parts ----
    def fill_avail(self):
        q = self.find.text().strip().upper()
        taken = {p["part_key"] for p in self.work["parts"]}
        self.avail.clear()
        shown = 0
        for pk, disp in self.all_parts:
            if pk in taken or (q and q not in (disp or "").upper() and q not in pk.upper()):
                continue
            it = QListWidgetItem()
            it.setData(Qt.UserRole, pk)
            row = QFrame()
            row.setProperty("card", True)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 4, 6, 4)
            rl.addWidget(label("<b>%s</b>  <span style='color:%s'>%s</span>"
                               % (short_part(pk) or pk, ctx.TH["muted"], disp)))
            rl.addStretch(1)
            add = button("+ Add", slot=lambda _pk=pk: self.add_part(_pk))
            add.setStyleSheet("border: none; background: transparent; color: %s;"
                              % ctx.TH["accent"])
            rl.addWidget(add)
            it.setSizeHint(QSize(10, 38))
            self.avail.addItem(it)
            self.avail.setItemWidget(it, row)
            shown += 1
            if shown >= 300:
                break

    def fill_chosen(self, select=None):
        if select is not None:
            self.cur = select
        n = len(self.work["parts"])
        self.cur = max(0, min(self.cur, n - 1)) if n else -1
        while self.parts_lay.count():
            it = self.parts_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        th = ctx.TH
        for i, p in enumerate(self.work["parts"]):
            pk = p["part_key"]
            n_steps = sum(1 for s in self.work["steps"] if s["part_key"] == pk)
            start = p.get("start")
            start_lbl = ""
            if start:
                c = next((c for c in self.route_of(pk) if route_step(c) == start), None)
                start_lbl = " · start %s" % ((c.get("label") or c["op"]) if c else start)
            row = ClickFrame(lambda _i=i: self.pick_part(_i))
            on = i == self.cur
            row.setStyleSheet("ClickFrame { background: %s; border: 1px solid %s; border-radius: 10px; }"
                              % (th["sel_row"] if on else th["surf2"],
                                 th["accent"] if on else th["border"]))
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 8, 10, 8)
            sw = QLabel()
            sw.setFixedSize(14, 14)
            sw.setStyleSheet("background: %s; border-radius: 3px;" % p["color"])
            rl.addWidget(sw)
            tx = QVBoxLayout()
            tx.setSpacing(0)
            tx.addWidget(label("<b>%s</b>  %s" % (short_part(pk) or pk, pk)))
            tx.addWidget(label("%d step%s grouped%s" % (n_steps, "" if n_steps == 1 else "s",
                                                         start_lbl), muted=True))
            rl.addLayout(tx, 1)
            if p.get("stg"):
                st = QLabel(p["stg"])
                st.setStyleSheet("background: %s; border-radius: 9px; padding: 2px 8px; "
                                 "font-size: 9pt;" % th["card"])
                rl.addWidget(st)
            row.setToolTip(pk)
            self.parts_lay.addWidget(row)
        self.parts_head.setText("<span style='color:%s'>%d on the board</span>"
                                % (th["muted"], n))
        self.fill_steps()
        self.fill_preview()

    def pick_part(self, i):
        self.cur = i
        self.fill_chosen()

    def sel_part(self):
        r = self.cur
        return self.work["parts"][r] if 0 <= r < len(self.work["parts"]) else None

    def add_part(self, pk=None):
        if pk is None:
            it = self.avail.currentItem()
            if it is None:
                return
            pk = it.data(Qt.UserRole)
        used = {p.get("color") for p in self.work["parts"]}
        col = next((c for c in ACC_COLORS if c not in used), ACC_COLORS[0])
        self.work["parts"].append({"part_key": pk, "color": col, "stg": "", "start": None})
        self.fill_avail()
        self.fill_chosen(len(self.work["parts"]) - 1)

    def remove_part(self):
        p = self.sel_part()
        if not p:
            return
        self.work["parts"].remove(p)
        self.work["steps"] = [s for s in self.work["steps"] if s["part_key"] != p["part_key"]]
        self.fill_avail()
        self.fill_chosen()
        self.fill_groups()

    def pick_color(self):
        p = self.sel_part()
        if not p:
            return
        c = QColorDialog.getColor(QColor(p["color"]), self, "Part colour")
        if c.isValid():
            p["color"] = c.name().upper()
            self.fill_chosen()

    def set_stg(self):
        p = self.sel_part()
        if not p:
            return
        known = sorted({q.get("stg") for q in self.work["parts"] if q.get("stg")})
        v, ok = QInputDialog.getText(self, "STG", "Weekly target group for %s (e.g. STG 1).\n"
                                     "Known: %s\nLeave empty for none."
                                     % (short_part(p["part_key"]), ", ".join(known) or "-"),
                                     text=p.get("stg") or "")
        if not ok:
            return
        p["stg"] = v.strip()
        self.fill_chosen()
        self.fill_targets()

    # ---- steps ----
    def group_names(self):
        out = list(self.work["groups"])
        for s in self.work["steps"]:
            if s.get("group") and s["group"] not in out:
                out.append(s["group"])
        for g in self.extra_groups:
            if g not in out:
                out.append(g)
        return out

    def set_role(self, pk, step, value):
        value = (value or "").strip()
        p = next(q for q in self.work["parts"] if q["part_key"] == pk)
        self.work["steps"] = [s for s in self.work["steps"]
                              if not (s["part_key"] == pk and s["step"] == step)]
        if p.get("start") == step:
            p["start"] = None
        if value.upper() == self.START:
            p["start"] = step
        elif value:
            self.work["steps"].append({"part_key": pk, "step": step, "group": value})
            if value not in self.work["groups"]:
                self.work["groups"].append(value)
        self.fill_groups()
        pos = self.steps_scroll.verticalScrollBar().value()
        self.fill_chosen()
        QTimer.singleShot(0, lambda: self.steps_scroll.verticalScrollBar().setValue(pos))

    def paint_brushes(self):
        """Quick assign: pick START or a group, then click steps to give it
        (a click on a step that has it already takes it off)."""
        while self.brush_lay.count():
            it = self.brush_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.brush_lay.addWidget(label("Quick assign:", muted=True))
        for g in [self.START] + self.group_names():
            col = self.group_color(g)
            b = QPushButton(g)
            b.setCheckable(True)
            b.setChecked(self.brush == g)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet("QPushButton { border: 1.5px solid %s; border-radius: 13px; "
                            "padding: 3px 12px; font-weight: 700; background: transparent; } "
                            "QPushButton:checked { background: %s; color: #FFFFFF; }"
                            % (col, col))
            b.setMinimumWidth(QFontMetrics(font(10, True)).horizontalAdvance(g) + 34)
            b.clicked.connect(lambda _c=False, _g=g: self.set_brush(_g))
            self.brush_lay.addWidget(b)
        self.brush_lay.addWidget(label("pick one, then click steps", muted=True))

    def set_brush(self, g):
        self.brush = None if self.brush == g else g
        self.paint_brushes()

    def brush_step(self, pk, step, current):
        if not self.brush:
            return
        self.set_role(pk, step, "" if current == self.brush else self.brush)

    def fill_steps(self):
        while self.steps_lay.count():
            it = self.steps_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.paint_brushes()
        p = self.sel_part()
        if not p:
            self.steps_title.setText("Steps")
            self.steps_sub.setText("pick a part on the left")
            return
        pk = p["part_key"]
        th = ctx.TH
        self.steps_title.setText("Steps of %s" % (short_part(pk) or pk))
        self.steps_sub.setText("give a GROUP to each step you want on the board · START lets "
                               "the part onto the board")
        role = {s["step"]: s["group"] for s in self.work["steps"] if s["part_key"] == pk}
        if p.get("start"):
            role[p["start"]] = self.START
        names = ["", self.START] + self.group_names()
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 2, 8, 2)
        for t, w in (("Op", 60), ("Description", 0), ("Group", 190)):
            l_ = label(t, muted=True)
            l_.setStyleSheet("font-weight: 600;")
            if w:
                l_.setFixedWidth(w)
            hl.addWidget(l_, 0 if w else 1)
        self.steps_lay.addWidget(head)
        only = self.t_only.isChecked()
        for c in self.route_of(pk):
            k = route_step(c)
            r = role.get(k, "")
            if only and not r:
                continue
            col = self.group_color(r) if r else None
            row = ClickFrame(lambda _k=k, _r=r: self.brush_step(pk, _k, _r))
            row.setStyleSheet("ClickFrame { background: %s; border-left: 3px solid %s; "
                              "border-radius: 4px; }"
                              % (mix(th["card"], col, 0.10) if col else "transparent",
                                 col or "transparent"))
            rl = QHBoxLayout(row)
            rl.setContentsMargins(11, 3, 8, 3)
            op = label(str(c.get("label") or c["op"]))
            op.setFixedWidth(60)
            op.setStyleSheet("font-weight: 700;%s" % ((" color: %s;" % col) if col else ""))
            rl.addWidget(op)
            d = label((c.get("desc") or "")[:60])
            d.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            rl.addWidget(d, 1)
            cb = QComboBox()
            cb.setEditable(True)
            cb.addItems([n or "–" for n in names])
            cb.setCurrentText(r or "–")
            cb.setFixedWidth(190)
            if col:
                cb.setStyleSheet("QComboBox { border: 1.5px solid %s; font-weight: 700; }" % col)

            def commit(_x=None, _k=k, _cb=cb, _old=r):
                v = (_cb.currentText() or "").strip()
                v = "" if v == "–" else v
                if v != _old:
                    self.set_role(pk, _k, v)
            cb.activated.connect(commit)
            cb.lineEdit().editingFinished.connect(commit)
            rl.addWidget(cb)
            self.steps_lay.addWidget(row)

    # ---- groups ----
    def fill_groups(self, select=None):
        used = {s["group"] for s in self.work["steps"] if s.get("group")}
        names = self.group_names()
        self.work["groups"] = [g for g in names if g in used]
        # a group added with "+ Add group" stays (empty) until it gets steps
        self.extra_groups = [g for g in self.extra_groups if g not in used]
        shown = self.work["groups"] + self.extra_groups
        th = ctx.TH
        self.glist.clear()
        for g in shown:
            n = sum(1 for s in self.work["steps"] if s.get("group") == g)
            it = QListWidgetItem()
            it.setData(Qt.UserRole, g)
            row = QFrame()
            row.setStyleSheet("QFrame#grow { background: %s; border: 1px solid %s; "
                              "border-radius: 8px; }" % (th["surf2"], th["border"]))
            row.setObjectName("grow")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 4, 6, 4)
            h = label("⠿", muted=True)
            h.setCursor(Qt.OpenHandCursor)
            rl.addWidget(h)
            nm = label(g)
            nm.setStyleSheet("font-weight: 700; color: %s;" % self.group_color(g))
            rl.addWidget(nm, 1)
            rl.addWidget(label("%d step%s" % (n, "" if n == 1 else "s"), muted=True))
            ren = button("✎", icon_only=True, slot=lambda _g=g: self.rename_group(_g))
            rm = button("✕", icon_only=True, slot=lambda _g=g: self.remove_group(_g))
            rm.setStyleSheet("color: %s;" % th["danger"])
            rl.addWidget(ren)
            rl.addWidget(rm)
            it.setSizeHint(QSize(10, 42))
            self.glist.addItem(it)
            self.glist.setItemWidget(it, row)
        if select is not None:
            self.glist.setCurrentRow(select)
        self.fill_preview()

    def groups_moved(self):
        order = [self.glist.item(i).data(Qt.UserRole) for i in range(self.glist.count())]
        self.work["groups"] = [g for g in order if g in self.work["groups"]]
        self.extra_groups = [g for g in order if g in self.extra_groups]
        self.fill_groups()
        self.fill_steps()

    def move_group(self, dn):
        i = self.glist.currentRow()
        j = i + dn
        g = self.work["groups"]
        if i < 0 or not 0 <= j < len(g):
            return
        g[i], g[j] = g[j], g[i]
        self.fill_groups(select=j)

    def add_group(self):
        v, ok = QInputDialog.getText(self, "Group", "Name of the new group:")
        v = (v or "").strip()
        if not ok or not v or v.upper() == self.START or v in self.group_names():
            return
        self.extra_groups.append(v)
        self.fill_groups()
        self.brush = v
        self.fill_steps()

    def remove_group(self, g):
        self.work["steps"] = [s for s in self.work["steps"] if s.get("group") != g]
        self.extra_groups = [x for x in self.extra_groups if x != g]
        if self.brush == g:
            self.brush = None
        self.fill_groups()
        self.fill_chosen()

    def rename_group(self, old=None):
        if old is None:
            i = self.glist.currentRow()
            if i < 0:
                return
            old = self.glist.item(i).data(Qt.UserRole)
        new, ok = QInputDialog.getText(self, "Group", "New name for %s:" % old, text=old)
        if not ok or not new.strip() or new.strip() == old:
            return
        new = new.strip()
        for s in self.work["steps"]:
            if s.get("group") == old:
                s["group"] = new
        self.work["groups"] = [new if g == old else g for g in self.work["groups"]]
        self.extra_groups = [new if g == old else g for g in self.extra_groups]
        if self.brush == old:
            self.brush = new
        self.fill_groups()
        self.fill_steps()

    def fill_preview(self):
        cols = []
        for g in self.work["groups"]:
            colors = [p["color"] for p in self.work["parts"]
                      for s in self.work["steps"]
                      if s.get("group") == g and s["part_key"] == p["part_key"]]
            cols.append((g, colors))
        self.preview.set_cols(cols)

    def fill_targets(self):
        while self.tbox.count():
            it = self.tbox.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        stgs = []
        for p in self.work["parts"]:
            s = (p.get("stg") or "").strip()
            if s and s not in stgs:
                stgs.append(s)
        old = {s: e.text() for s, e in self.tvars.items()}
        self.tvars = {}
        if not stgs:
            self.tbox.addWidget(label("Give parts an STG group (STG group button) to set a weekly "
                                      "target.", muted=True, wrap=True), 0, 0, 1, 2)
        for i, s in enumerate(stgs):
            members = [short_part(p["part_key"]) or p["part_key"] for p in self.work["parts"]
                       if (p.get("stg") or "").strip() == s]
            self.tbox.addWidget(label("<b>%s</b>  <span style='color:%s'>%s</span>"
                                      % (s, ctx.TH["muted"], ", ".join(members))), i, 0)
            e = QLineEdit(old.get(s, str(self.work["targets"].get(s, "") or "")))
            e.setFixedWidth(90)
            e.setAlignment(Qt.AlignRight)
            self.tbox.addWidget(e, i, 1)
            self.tvars[s] = e

    # ---- save ----
    def save(self):
        nm = self.name_ed.text().strip()
        if not nm:
            QMessageBox.warning(self, "Custom analysis", "Give the analysis a name.")
            return
        if not self.work["parts"]:
            QMessageBox.warning(self, "Custom analysis", "Add at least one part.")
            return
        targets = {}
        for s, e in self.tvars.items():
            try:
                targets[s] = max(0, int(float(e.text())))
            except (TypeError, ValueError):
                pass
        self.work["targets"] = targets
        self.work["show_rework"] = self.t_rw.isChecked()
        self.work["max_rows"] = self.max_rows.value()
        # empty groups are not saved (the old window never kept them)
        self.extra_groups = []
        self.fill_groups()
        problems = acc_check(self.work, {p["part_key"]: self.route_of(p["part_key"])
                                         for p in self.work["parts"]})
        if problems:
            self.prob.setText("  ".join(problems))
            self.prob.setStyleSheet("color: %s;" % ctx.TH["danger"])
            if QMessageBox.question(self, "Custom analysis", "Some parts are not set up completely:"
                                    "\n\n%s\n\nSave anyway?" % "\n".join(problems[:12])) \
                    != QMessageBox.Yes:
                return
        if nm != self.edit_name and nm in acc_load(ctx.db):
            if QMessageBox.question(self, "Custom analysis", "'%s' already exists. Replace it?"
                                    % nm) != QMessageBox.Yes:
                return
        acc_save(ctx.db, nm, self.work)
        if self.edit_name and nm != self.edit_name:
            acc_delete(ctx.db, self.edit_name)
        self.saved_name = nm
        self.page.close_setup(nm)

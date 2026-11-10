# -*- coding: utf-8 -*-
"""B1000 LIVE - the Qt page (new design).

Same as the Tk page (page_b1000.py), and drawn by the same rules so the
plan everybody has laid out looks and reads exactly as before: every
machine with its library symbol, name and work-centre code, zones with
what waits in them, the serial on each machine and its progress bar.

LIVE   the plan coloured by what is on the floor (wip_core.live_positions).
EDIT   (admin only) library, add zone / label, move, resize, rotate,
       colour, duplicate, remove, undo / redo, snap to grid, carry
       contents and lock for zones, save as a new version, back up to a
       file and restore from one.
"""

import copy
import datetime
import os

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (QColor, QPainter, QPen, QFont, QFontMetrics, QKeySequence, QPixmap,
                           QLinearGradient, QBrush)
# v69: the soft drawing (rounded, gently filled machines, names cut to fit)
SOFT = True
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea,
                               QMessageBox, QInputDialog, QFileDialog, QColorDialog, QLineEdit,
                               QPushButton, QSizePolicy, QFrame)

from wip_core import (ALL, allowed_part_keys, capacity_of_items, live_positions, norm_text,
                      split_step, step_key, WORDS, short_part, time_mode_to_date_range,
                      work_centers_by_group, save_prefs)
from qt_widgets import (ctx, Splitter, Card, DataTable, FilterBar, Toggle, Seg, label, button, hbox,
                        IconButton, page_head, autofit_columns)
from qt_theme import FONT
import wip_layout_assets as LA

_MIN_DT = datetime.datetime.min
HANDLE = 7          # corner grab square, screen pixels
GRID = 8            # snap step, plan units
DRAG_START = 4
UNDO_DEPTH = 40
HERE_COLS = [("SERIAL", 100), ("PART", 64), ("OP", 60), ("STATE", 84), ("PROGRESS", 112),
             ("LEFT", 62), ("OFF AT", 92)]
GAUGE_CELLS = 10


def rot_pt(px, py, rot):
    rot = int(rot or 0) % 360
    if rot == 90:
        return 100.0 - py, px
    if rot == 180:
        return 100.0 - px, 100.0 - py
    if rot == 270:
        return py, 100.0 - px
    return px, py


class PlanView(QWidget):
    """The floor plan, painted.  Sized to the plan x zoom inside a scroll
    area; the page holds the state and the rules."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self._pan = None
        self._cache = None          # (key, QPixmap): the live plan, drawn once
        ctx.tables.append(self)

    def invalidate(self):
        self._cache = None
        self.update()

    def refresh_selection(self):
        self.update()

    def refresh_theme(self):
        self.invalidate()

    def resize_to_plan(self):
        W, H = self.page.plan_extent()
        z = self.page.zoom()
        self.setFixedSize(int(W * z + 20), int(H * z + 20))
        self.update()

    # ---- painting (the same rules as page_b1000.draw) ----
    def paintEvent(self, ev):
        """v69: in Live the plan does not move, so it is drawn ONCE into a
        picture and every scroll / pan / repaint just shows the part of it
        that is on screen - moving around a 60-machine plan was redrawing
        every machine, every time.  In Edit (things move) only the part of
        the plan that is on screen is drawn."""
        pg = self.page
        er = ev.rect()
        p = QPainter(self)
        live = pg.state["mode"] == "live"
        dpr = self.devicePixelRatioF()
        big = self.width() * self.height() * dpr * dpr > 45e6
        drag = pg.state.get("drag")
        if not live and not big and drag and drag.get("live"):
            # v73: dragging in Edit - everything that is NOT moving is drawn
            # once into a picture for this drag; each step only the moving
            # pieces are drawn over it (it was the whole plan, every step)
            moving = (set(drag["orig"]) if drag["kind"] == "move"
                      else {drag["i"]} | set(drag.get("inner") or ()))
            key = ("edit", id(drag), pg.zoom(), ctx.TH["name"], self.width(), self.height(), dpr)
            if self._cache is None or self._cache[0] != key:
                pix = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
                pix.setDevicePixelRatio(dpr)
                pp = QPainter(pix)
                self._paint_plan(pp, QRectF(self.rect()), skip=moving)
                pp.end()
                self._cache = (key, pix)
            p.drawPixmap(QRectF(er), self._cache[1], QRectF(er.x() * dpr, er.y() * dpr,
                                                     er.width() * dpr, er.height() * dpr))
            p.setClipRect(er)
            self._paint_plan(p, QRectF(er), only=moving)
            self._paint_selection(p)
            p.end()
            return
        if not big:
            # Live: the plan does not move.  Edit (v73): the picture is kept
            # until the plan changes (rev moves on every change)
            key = (id(pg.state["items"]), id(pg.state["live"]) if live else pg.state.get("rev", 0),
                   pg.state["mode"], pg.t_snap.isChecked(), pg.zoom(), ctx.TH["name"],
                   self.width(), self.height(), dpr)
            if self._cache is None or self._cache[0] != key:
                pix = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
                pix.setDevicePixelRatio(dpr)
                pp = QPainter(pix)
                self._paint_plan(pp, QRectF(self.rect()))
                pp.end()
                self._cache = (key, pix)
            p.drawPixmap(QRectF(er), self._cache[1], QRectF(er.x() * dpr, er.y() * dpr,
                                                     er.width() * dpr, er.height() * dpr))
        else:
            p.setClipRect(er)
            self._paint_plan(p, QRectF(er))
        self._paint_selection(p)
        p.end()

    def _paint_selection(self, p):
        pg = self.page
        st = pg.state
        th = ctx.TH
        z = pg.zoom()
        p.setRenderHint(QPainter.Antialiasing)
        for i in st["sel"]:
            if not 0 <= i < len(st["items"]):
                continue
            it = st["items"][i]
            x0, y0 = it["x"] * z, it["y"] * z
            x1, y1 = (it["x"] + it["w"]) * z, (it["y"] + it["h"]) * z
            p.setPen(QPen(QColor(th["sel_edge"]), 2))
            p.setBrush(Qt.NoBrush)
            if SOFT:
                p.drawRoundedRect(QRectF(x0 - 2, y0 - 2, x1 - x0 + 4, y1 - y0 + 4),
                                  max(4.0, 10 * z), max(4.0, 10 * z))
            else:
                p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(th["sel_edge"]))
            for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                p.drawRect(QRectF(hx - HANDLE / 2, hy - HANDLE / 2, HANDLE, HANDLE))

    def _paint_plan(self, p, area, skip=None, only=None):
        """Everything but the selection, for the part of the plan in `area`.
        v73: `skip` leaves those items out (the still picture under a drag),
        `only` draws just those, on top, with no background."""
        pg = self.page
        st = pg.state
        th = ctx.TH
        p.setRenderHint(QPainter.Antialiasing)
        z = pg.zoom()
        W, H = pg.plan_extent()
        if only is not None:
            self._paint_items(p, area, z, th, pg, st, skip=None, only=only)
            return
        p.fillRect(area, QColor(th["card"]))
        p.setPen(QPen(QColor(th["border"]), 1))
        p.setBrush(QColor(th["bg"]))
        p.drawRect(QRectF(0, 0, W * z, H * z))
        if st["mode"] == "edit" and pg.t_snap.isChecked() and z >= 0.35:
            p.setPen(QPen(QColor(th["border"]), 1))
            step = GRID * 4 * z
            y = step
            while y < H * z:
                x = step
                while x < W * z:
                    p.drawPoint(QPointF(x, y))
                    x += step
                y += step
        self._paint_items(p, area, z, th, pg, st, skip=skip, only=None)

    def _paint_items(self, p, area, z, th, pg, st, skip=None, only=None):
        counts = {i: len(st["live"].get(pg.bucket_key(it), [])) for i, it in enumerate(st["items"])}

        def fs(base):
            n = int(round(base * z))
            return n if n >= 4 else 0

        def text(cx, cy, s, size, color, bold=False, anchor="c", maxw=None):
            f = QFont(FONT)
            f.setPointSizeF(size)
            f.setBold(bold)
            p.setFont(f)
            p.setPen(QColor(color))
            fm = QFontMetrics(f)
            if SOFT and maxw and fm.horizontalAdvance(s) > maxw:
                s = fm.elidedText(s, Qt.ElideRight, int(max(10, maxw)))
            w, h = fm.horizontalAdvance(s) + 4, fm.height()
            if anchor == "ne":
                r = QRectF(cx - w, cy, w, h)
            elif anchor == "nw":
                r = QRectF(cx, cy, w, h)
            else:
                r = QRectF(cx - w / 2, cy - h / 2, w, h)
            p.drawText(r, Qt.AlignCenter, s)

        for i, it in enumerate(st["items"]):
            if (skip is not None and i in skip) or (only is not None and i not in only):
                continue
            # off screen: nothing to draw (a margin for the labels and handles)
            if not area.intersects(QRectF(it["x"] * z - 14, it["y"] * z - 14,
                                          it["w"] * z + 28, it["h"] * z + 28)):
                continue
            n = counts.get(i, 0)
            col = pg.colour_for(it)
            if SOFT and th.get("name") == "day":
                # drawing only: the stored colour is never changed
                col = QColor(col).darker(165).name()
            x0, y0 = it["x"] * z, it["y"] * z
            x1, y1 = (it["x"] + it["w"]) * z, (it["y"] + it["h"]) * z
            if SOFT:
                r = QRectF(x0 + 1, y0 + 1, x1 - x0 - 2, y1 - y0 - 2)
                rad = max(3.0, 9 * z)
                gcol = QColor(col)
                gr = QLinearGradient(r.topLeft(), r.bottomLeft())
                c1 = QColor(gcol); c1.setAlpha(46 if n else 26)
                c2 = QColor(gcol); c2.setAlpha(14 if n else 6)
                gr.setColorAt(0.0, c1); gr.setColorAt(1.0, c2)
                p.setBrush(QBrush(gr))
                edge = QColor(gcol); edge.setAlpha(230)
                pen = QPen(edge, max(1.2, 1.8 * min(z, 1.4)))
                pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                p.drawRoundedRect(r, rad, rad)
            else:
                p.setPen(QPen(QColor(col), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            self.draw_symbol(p, it, x0, y0, x1, y1, col)
            name = it.get("label") or ""
            code = (it.get("wc_code") or "") if it["kind"] == "machine" else ""
            if not name:
                name, code = code, ""
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            f_name, f_code = fs(9), fs(8)
            if name and code and f_name:
                if (y1 - y0) >= 30 * z and f_code:
                    text(cx, y0 + 9 * z, name, f_name, th["text"], True, maxw=x1 - x0 - 6)
                    text(cx, y0 + 20 * z, code, f_code, th["muted"], maxw=x1 - x0 - 6)
                else:
                    text(cx, y0 + 9 * z, "%s %s" % (name, code), f_name, th["text"], True, maxw=x1 - x0 - 6)
            elif name and f_name:
                text(cx, y0 + 9 * z, name, f_name, th["text"], True, maxw=x1 - x0 - 6)
            if it["kind"] == "machine" and not it.get("wc_code") and fs(7):
                text(cx, y1 - 9 * z, WORDS["no_wc"], fs(7), th["danger"], True)
            if st["mode"] == "live" and n:
                bucket = st["live"].get(pg.bucket_key(it), [])
                if it["kind"] == "machine":
                    cutting = [q for q in bucket if q["running"]]
                    lead = (cutting or sorted(bucket, key=lambda q: (q["last_entry"] or _MIN_DT,
                                                                     q["date"] or ""),
                                              reverse=True))[0] if bucket else None
                    held = len(bucket) - 1 if bucket else 0
                    last_only = False
                    if lead is None:
                        # v75: nothing on it now - the last part put on it
                        lead = st.get("last_on", {}).get(pg.bucket_key(it))
                        last_only = lead is not None
                    f_ser, f_part = fs(11), fs(9)
                    if lead is not None and f_ser:
                        text(cx, cy - 6 * z, lead["serial"], f_ser,
                             th["muted"] if last_only else th["text"], True)
                        if f_part:
                            text(cx, cy + 6 * z, short_part(lead["material"]), f_part,
                                 th["muted"], True)
                        if fs(7) and last_only:
                            text(cx, y1 - 15 * z, "last loaded", fs(7), th["muted"], True)
                        elif not lead["running"] and fs(7):
                            text(cx, y1 - 15 * z, WORDS["done"], fs(7), th["muted"], True)
                    if held and fs(7):
                        text(x1 - 4, y0 + 4, "+%d" % held, fs(7), th["muted"], True, "ne")
                elif fs(15):
                    tally = {}
                    for q in bucket:
                        k = str(q["next_op"] or "?")
                        tally[k] = tally.get(k, 0) + 1
                    order = sorted(tally.items(), key=lambda kv: -kv[1])
                    f_d = fs(8)
                    if len(order) > 1 and f_d and (y1 - y0) > 54 * z:
                        text(cx, y0 + 26 * z, str(n), fs(13), th["text"], True)
                        rows_n = max(1, int((y1 - (y0 + 40 * z)) / (11 * z)))
                        shown = order[:rows_n]
                        yy = y0 + 42 * z
                        for opno, cnt in shown:
                            text(cx, yy, "%s   x%d" % (opno, cnt), f_d, th["muted"], True)
                            yy += 11 * z
                        left = len(order) - len(shown)
                        if left and yy < y1 - 6 * z:
                            text(cx, yy, "+%d more op(s)" % left, f_d, th["muted"])
                    else:
                        text(cx, cy, str(n), fs(15), th["text"], True)
                        if order and f_d and (y1 - y0) > 40 * z:
                            text(cx, cy + 16 * z, "waiting for %s" % order[0][0], f_d,
                                 th["muted"], True)
                here_now = [q for q in bucket if q["running"] and q["pct"] is not None]
                if here_now:
                    ld = max(here_now, key=lambda q: q["pct"])
                    pad = max(2.0, 4 * z)
                    bh = max(4.0, 7 * z)
                    bw = max(0.0, (x1 - x0) - 2 * pad)
                    by = y1 - bh - pad / 2
                    if SOFT:
                        pad = max(4.0, 7 * z)
                        bw = max(0.0, (x1 - x0) - 2 * pad)
                        by = y1 - bh - pad / 1.4
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(th["dim"]))
                        p.drawRoundedRect(QRectF(x0 + pad, by, bw, bh), bh / 2, bh / 2)
                        p.setBrush(QColor(th["wf_awaiting"]))
                        p.drawRoundedRect(QRectF(x0 + pad, by, max(bh, bw * ld["pct"]), bh), bh / 2, bh / 2)
                    else:
                        p.fillRect(QRectF(x0 + pad, by, bw, bh), QColor(th["dim"]))
                        p.fillRect(QRectF(x0 + pad, by, bw * ld["pct"], bh), QColor(th["wf_awaiting"]))
            if it.get("locked") and fs(7):
                text(x0 + 4, y0 + 4, "LOCKED", fs(7), th["muted"], True, "nw")

    def draw_symbol(self, p, it, x0, y0, x1, y1, col):
        geom = LA.drawing_geometry(LA.ASSET.get(it.get("asset"), {}).get("drawing", ""))
        if not geom:
            return
        w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
        sx, sy = w / 100.0, h / 100.0
        rot = int(it.get("rot") or 0) % 360
        if rot:
            turned = []
            for g in geom:
                if g[0] == "rect":
                    _t, rx, ry, rw, rh, _r, d = g
                    cs = [rot_pt(a, b, rot) for a, b in ((rx, ry), (rx + rw, ry), (rx, ry + rh),
                                                         (rx + rw, ry + rh))]
                    ax = [c[0] for c in cs]
                    ay = [c[1] for c in cs]
                    turned.append(("rect", min(ax), min(ay), max(ax) - min(ax), max(ay) - min(ay),
                                   0, d))
                elif g[0] == "circle":
                    _t, ccx, ccy, cr, d = g
                    nx, ny = rot_pt(ccx, ccy, rot)
                    turned.append(("circle", nx, ny, cr, d))
                else:
                    turned.append((g[0], [rot_pt(a, b, rot) for a, b in g[1]], g[2]))
            geom = turned
        for g in geom:
            pc = QColor(col)
            if SOFT:
                pc.setAlpha(170)
            pen = QPen(pc, 1.2 if SOFT else 1)
            if SOFT:
                pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
            if g[-1]:
                pen.setDashPattern([3, 2])
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            if g[0] == "rect":
                _t, rx, ry, rw, rh, _r, _d = g
                if SOFT:
                    rr = min(3.0, 0.18 * min(rw * sx, rh * sy))
                    p.drawRoundedRect(QRectF(x0 + rx * sx, y0 + ry * sy, rw * sx, rh * sy), rr, rr)
                else:
                    p.drawRect(QRectF(x0 + rx * sx, y0 + ry * sy, rw * sx, rh * sy))
            elif g[0] == "circle":
                _t, ccx, ccy, cr, _d = g
                p.drawEllipse(QRectF(x0 + (ccx - cr) * sx, y0 + (ccy - cr) * sy, 2 * cr * sx,
                                     2 * cr * sy))
            else:
                pts = [QPointF(x0 + px * sx, y0 + py * sy) for px, py in g[1]]
                if len(pts) >= 2:
                    p.drawPolyline(pts)

    # ---- mouse ----
    def to_plan(self, pos):
        z = self.page.zoom()
        return pos.x() / z, pos.y() / z

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() in (Qt.MiddleButton, Qt.RightButton):
            self._pan = (e.globalPosition().toPoint(), self.page.scroll.horizontalScrollBar().value(),
                         self.page.scroll.verticalScrollBar().value())
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            px, py = self.to_plan(e.position())
            self.page.on_press(px, py, e.position(), bool(e.modifiers() & Qt.ShiftModifier))

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            start, hx, vy = self._pan
            d = e.globalPosition().toPoint() - start
            self.page.scroll.horizontalScrollBar().setValue(hx - d.x())
            self.page.scroll.verticalScrollBar().setValue(vy - d.y())
            return
        if e.buttons() & Qt.LeftButton:
            px, py = self.to_plan(e.position())
            self.page.on_motion(px, py, e.position())

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            self.setCursor(Qt.ArrowCursor)
            return
        self.page.on_release()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.page.on_double(*self.to_plan(e.position()))

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.page.set_zoom_pct(self.page.zoom_pct + (10 if e.angleDelta().y() > 0 else -10))
            return
        super().wheelEvent(e)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.page.remove_sel()
        elif k == Qt.Key_R and not e.modifiers():
            self.page.rotate_sel()
        elif e.matches(QKeySequence.Undo):
            self.page.undo()
        elif e.matches(QKeySequence.Redo):
            self.page.redo()
        else:
            super().keyPressEvent(e)


class LibTile(QFrame):
    """One machine type in the library: its drawing over its name; a click
    puts one on the plan (B1K3)."""

    def __init__(self, page, asset):
        super().__init__()
        self.page = page
        self.asset = asset
        self.setProperty("card", True)
        self.setFixedHeight(92)
        self.setMinimumWidth(100)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("%s  (%s) - click to place" % (asset[1], asset[5]))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.page.add_item(self.asset[0])

    def paintEvent(self, ev):
        super().paintEvent(ev)
        th = ctx.TH
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        aw, ah = float(self.asset[3] or 100), float(self.asset[4] or 100)
        box = 46.0
        s = box / max(aw, ah)
        iw, ih = aw * s, ah * s
        x0, y0 = (w - iw) / 2, 10 + (box - ih) / 2
        col = "#5B8DEF" if th["name"] != "day" else "#1F63D6"
        p.setPen(QPen(QColor(col), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x0, y0, iw, ih), 4, 4)
        self.page.view.draw_symbol(p, {"asset": self.asset[0], "rot": 0}, x0, y0, x0 + iw,
                                   y0 + ih, col)
        p.setPen(QColor(th["text"]))
        f = QFont(FONT)
        f.setPointSizeF(9)
        p.setFont(f)
        fm = QFontMetrics(f)
        p.drawText(QRectF(4, h - 26, w - 8, 20), Qt.AlignCenter,
                   fm.elidedText(self.asset[1], Qt.ElideRight, w - 10))
        p.end()


def state_text(q):
    if q["std_hour"] is None:
        return "%s d" % q["days"] if q["days"] is not None else ""
    if q["running"]:
        return WORDS["processing"]
    if q.get("queued"):
        return WORDS["queued"]
    if q["stalled"]:
        return "%s %sd" % (WORDS["idle"], q["days"] or 0)
    return "%s d" % (q["days"] or 0)


class LiveRow(QFrame):
    """A serial in the machine panel: serial, part · op, state; the one on
    the machine (big) also has its progress bar and when it comes off.
    Left click selects the serial everywhere, right click = serial menu."""

    def __init__(self, q, big=False):
        super().__init__()
        self.q = q
        th = ctx.TH
        self.setObjectName("liverow")
        self.setStyleSheet("QFrame#liverow { background: %s; border-radius: 8px; }" % th["surf2"])
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8 if big else 5, 12, 8 if big else 5)
        lay.setSpacing(4)
        top = QHBoxLayout()
        sl = label(q["serial"])
        sl.setStyleSheet("font-weight: 700;%s" % (" font-size: 11pt;" if big else ""))
        self.setMinimumWidth(0)
        top.addWidget(sl)
        top.addStretch(1)
        top.addWidget(label("%s · op %s" % (short_part(q["material"]), q["last_op"]), muted=True))
        if not big:
            st = label(state_text(q))
            st.setStyleSheet("font-weight: 700;")
            top.addWidget(st)
        lay.addLayout(top)
        if big:
            bar = QFrame()
            bar.setFixedHeight(6)
            pct = max(0.0, min(1.0, q["pct"] or 0.0))
            bar.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %s, "
                              "stop:%.3f %s, stop:%.3f %s, stop:1 %s); border-radius: 3px;"
                              % (th["wf_awaiting"], pct, th["wf_awaiting"], min(1.0, pct + 0.001),
                                 th["dim"], th["dim"]))
            lay.addWidget(bar)
            bits = []
            if q["std_hour"] is not None:
                bits.append("%.1f h of %.1f h standard" % (q["booked"] or 0, q["std_hour"]))
            if q["remaining"]:
                bits.append("%.1f h left" % q["remaining"])
            if q["eta"]:
                bits.append("off at %s" % q["eta"].strftime("%d.%m %H:%M"))
            info = label(" · ".join(bits) or state_text(q), muted=True, wrap=True)
            info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(info)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            ctx.set_selected_serial(self.q["serial"])
            m = ctx.hooks.get("serial_menu")
            if m:
                m(self.q["serial"], ev.globalPosition().toPoint())
            return
        if ev.button() == Qt.LeftButton:
            ctx.toggle_selected_serial(self.q["serial"])


class MachineRow(QFrame):
    """A machine in the panel's list: name, work centre, what it is cutting
    and how far along; a click picks it on the plan."""

    def __init__(self, page, index, it, q, picked):
        super().__init__()
        self.page, self.index = page, index
        th = ctx.TH
        self.setObjectName("machrow")
        self._picked = None
        self.set_picked(picked)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(10)
        nm = label(it.get("label") or "-")
        nm.setStyleSheet("font-weight: 700;")
        nm.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        nm.setToolTip(it.get("label") or "")
        lay.addWidget(nm, 3)
        lay.addWidget(label(it.get("wc_code") or "", muted=True), 1)
        if q:
            lay.addWidget(label(q["serial"]), 2)
        else:
            # v75: not cutting - the last part put on it, greyed
            last = page.state.get("last_on", {}).get(page.bucket_key(it))
            sl = label(last["serial"] if last else "–", muted=True)
            if last:
                sl.setToolTip("last loaded · op %s" % last["last_op"])
            lay.addWidget(sl, 2)
        bar = QFrame()
        bar.setFixedSize(60, 6)
        pct = max(0.0, min(1.0, (q["pct"] or 0.0) if q else 0.0))
        bar.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %s, "
                          "stop:%.3f %s, stop:%.3f %s, stop:1 %s); border-radius: 3px;"
                          % (th["wf_awaiting"], pct, th["wf_awaiting"], min(1.0, pct + 0.001),
                             th["dim"], th["dim"]))
        lay.addWidget(bar)

    def set_picked(self, picked):
        if picked == self._picked:
            return
        self._picked = picked
        self.setStyleSheet("QFrame#machrow { background: %s; border-radius: 8px; }"
                           % (ctx.TH["sel_row"] if picked else "transparent"))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.page.pick_item(self.index)


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.state = {"items": [], "sel": set(), "mode": "live", "shown": False,
                      "canvas": list(LA.BASE_CANVAS), "drag": None, "layout_id": None,
                      "dirty": False, "live": {}}
        self.zoom_pct = 100
        self.undo_stack, self.redo_stack = [], []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.refresh_live() if self.state["mode"] == "live"
                                   else None)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 18, 26, 14)
        outer.setSpacing(12)
        self.mode_seg = Seg([("Live", "live"), ("Edit plan", "edit")], "live")
        self.mode_seg.changed.connect(self.set_mode)
        page_head(outer, "B1000 Live", "Where each part is on the floor right now",
                  self.mode_seg, button("Fit", slot=self.fit))
        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("b1000"), part_multi=True,
                             with_serial=False, wip_label="Only WIP")
        self.bar.wip.setChecked(True)
        self.bar.remember("b1000")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(self.cascade)
        outer.addWidget(self.bar)

        # v73: no totals boxes and no capacity line at the top - the plan gets the room

        # ---- edit toolbar (B1K3): one card ----
        self.edit_bar = Card()
        eb = QHBoxLayout()
        eb.setSpacing(8)
        eb.addWidget(IconButton("back", "", self.undo, text="  Undo"))
        eb.addWidget(button("Redo", slot=self.redo))
        eb.addWidget(button("+ Zone", slot=lambda: self.add_plain("zone")))
        eb.addWidget(button("+ Label", slot=lambda: self.add_plain("prop")))
        self.t_snap = Toggle("Snap to grid", checked=True, slot=lambda: self.view.update())
        eb.addWidget(self.t_snap)
        self.t_lib = Toggle("Library", checked=True, slot=self.paint_library)
        eb.addWidget(self.t_lib)
        self.edit_status = label("", muted=True)
        eb.addWidget(self.edit_status)
        eb.addStretch(1)
        eb.addWidget(button("Restore from file", slot=self.import_plan))
        eb.addWidget(button("Backup to file", slot=self.export_plan))
        eb.addWidget(button("Reload", slot=self.load_plan))
        eb.addWidget(button("Save plan", primary=True, slot=self.save_plan))
        ebw = QWidget()
        ebw.setLayout(eb)
        self.edit_bar.add(ebw)
        outer.addWidget(self.edit_bar)
        self.edit_bar.hide()
        self.info_lbl = label("", muted=True, wrap=True)
        self.info_lbl.hide()
        outer.addWidget(self.info_lbl)
        # capacity warnings only (the numbers are in the CAPACITY box)
        self.cap_lbl = label("", muted=True, wrap=True)
        outer.addWidget(self.cap_lbl)

        # ---- library | plan | side panel, handles between ----
        self.body = Splitter(Qt.Horizontal)
        self.body.setChildrenCollapsible(False)
        self.body.setHandleWidth(10)
        body_row = QHBoxLayout()
        body_row.setSpacing(6)
        body_row.addWidget(self.body, 1)
        outer.addLayout(body_row, 1)
        # library: a grid of tiles, a drawing of each machine type
        self.lib = Card("Library", "click to place")
        self.lib_find = QLineEdit()
        self.lib_find.setPlaceholderText("Find a machine type")
        self.lib_find.textChanged.connect(self.fill_library)
        self.lib.add(self.lib_find)
        self.lib_host = QWidget()
        self.lib_grid = QGridLayout(self.lib_host)
        self.lib_grid.setContentsMargins(0, 0, 0, 0)
        self.lib_grid.setSpacing(8)
        self.lib_grid.setAlignment(Qt.AlignTop)
        ls = QScrollArea()
        ls.setWidgetResizable(True)
        ls.setFrameShape(QFrame.NoFrame)
        ls.setWidget(self.lib_host)
        ls.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.lib.add(ls, 1)
        self.lib.setMinimumWidth(250)
        self.body.addWidget(self.lib)
        self.lib.hide()
        self.fill_library()
        # plan
        self.plan_card = Card("Floor plan", "")
        self.legend = label("", muted=True)
        self.plan_card.add_action(self.legend)
        self.zoom_lbl = label("100%")
        self.plan_card.add_action(label("Zoom", muted=True))
        self.plan_card.add_action(button("−", icon_only=True,
                                         slot=lambda: self.set_zoom_pct(self.zoom_pct - 10)))
        self.plan_card.add_action(self.zoom_lbl)
        self.plan_card.add_action(button("+", icon_only=True,
                                         slot=lambda: self.set_zoom_pct(self.zoom_pct + 10)))
        self.view = PlanView(self)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.view)
        self.scroll.setWidgetResizable(False)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                  "{ background: transparent; }")
        self.plan_card.add(self.scroll, 1)
        self.plan_card.setMinimumWidth(420)
        self.body.addWidget(self.plan_card)

        # side: the live machine panel (Live) or the Selected editor (Edit)
        self.side = QWidget()
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        self.livep = Card("Machines", "")
        self.livep.add_action(button("Hide  ›", slot=lambda: self.set_live_panel(False)))
        self.live_host = QWidget()
        self.live_lay = QVBoxLayout(self.live_host)
        self.live_lay.setContentsMargins(0, 0, 0, 0)
        self.live_lay.setSpacing(6)
        self.live_lay.setAlignment(Qt.AlignTop)
        lsc = QScrollArea()
        lsc.setWidgetResizable(True)
        lsc.setFrameShape(QFrame.NoFrame)
        lsc.setWidget(self.live_host)
        lsc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lsc.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.livep.add(lsc, 1)
        sl.addWidget(self.livep, 1)

        self.detail = Card("Selected", "")
        d = self.detail
        self.f_name = QLineEdit()
        self.f_wc = QLineEdit()
        self.f_people = QLineEdit()
        self.f_groups = QLineEdit()
        self.f_ops = QLineEdit()
        self.f_pgroups = QLineEdit()
        for e in (self.f_name, self.f_wc, self.f_people, self.f_groups, self.f_ops, self.f_pgroups):
            e.editingFinished.connect(self.apply_details)
        self.rows = {}
        for key, cap, w in (("name", "Name", self.f_name), ("wc", "Work centre", self.f_wc),
                            ("people", "People", self.f_people),
                            ("groups", "Machine group", self.f_groups),
                            ("ops", "Waits for (ops)", self.f_ops),
                            ("pgroups", "Part group", self.f_pgroups)):
            box = QWidget()
            bl = QHBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(8)
            cl = label(cap, muted=True)
            cl.setFixedWidth(118)
            bl.addWidget(cl)
            bl.addWidget(w, 1)
            self.rows[key] = box
            d.add(box)
        self.grp_hint = label("", muted=True, wrap=True)
        self.pgrp_hint = label("", muted=True, wrap=True)
        d.add(self.grp_hint)
        d.add(self.pgrp_hint)
        self.edit_btns = QWidget()
        g = QGridLayout(self.edit_btns)
        g.setContentsMargins(0, 0, 0, 0)
        g.addWidget(button("Colour...", slot=self.pick_colour), 0, 0)
        g.addWidget(button("Rotate 90°", slot=self.rotate_sel), 0, 1)
        g.addWidget(button("Duplicate", slot=self.duplicate_sel), 1, 0)
        g.addWidget(button("Remove", danger=True, slot=self.remove_sel), 1, 1)
        d.add(self.edit_btns)
        self.t_carry = Toggle("Carry contents", checked=True, slot=self.apply_options)
        self.t_lock = Toggle("Lock", slot=self.apply_options)
        self.opt_row = hbox(self.t_carry, self.t_lock, None)
        d.add(self.opt_row)
        self.size_lbl = label("", muted=True)
        d.add(self.size_lbl)
        d.add(label("What is here now", obj="CardTitle"))
        self.here = DataTable()
        self.here.serial_col = 0
        self.here.on_row_menu = lambda ri, row, gp: ctx.hooks["serial_menu"](row[0], gp) \
            if row and row[0] else None
        d.add(self.here, 1)
        sl.addWidget(self.detail, 1)
        self.detail.hide()
        self.side.setMinimumWidth(320)
        self.body.addWidget(self.side)
        self.body.setStretchFactor(0, 0)
        self.body.setStretchFactor(1, 3)
        self.body.setStretchFactor(2, 1)
        self.side_strip = QPushButton("‹")
        self.side_strip.setToolTip("Show the machine panel")
        self.side_strip.setCursor(Qt.PointingHandCursor)
        self.side_strip.setFixedWidth(22)
        self.side_strip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.side_strip.clicked.connect(lambda: self.set_live_panel(True))
        self.side_strip.hide()
        body_row.addWidget(self.side_strip)
        self.live_hidden = bool(ctx.prefs.get("b1000_panel_hidden"))
        self.paint_side()

    # =================================================================
    #  v69 side panel / library / totals
    # =================================================================
    def set_live_panel(self, on):
        self.live_hidden = not on
        ctx.prefs["b1000_panel_hidden"] = self.live_hidden
        try:
            save_prefs(ctx.prefs)
        except Exception:
            pass
        self.paint_side()

    def paint_side(self):
        """Live: the machine panel (unless hidden to the strip).  Edit: the
        Selected editor, always."""
        edit = self.state["mode"] == "edit"
        self.livep.setVisible(not edit)
        self.detail.setVisible(edit)
        show = edit or not self.live_hidden
        self.side.setVisible(show)
        self.side_strip.setVisible(not show)

    def fill_library(self):
        while self.lib_grid.count():
            it = self.lib_grid.takeAt(0)
            w_ = it.widget()
            if w_:
                w_.setParent(None)
                w_.deleteLater()
        q = self.lib_find.text().strip().upper()
        n = 0
        for a in LA.CATALOG:
            if q and q not in a[1].upper() and q not in a[5].upper():
                continue
            t = LibTile(self, a)
            self.lib_grid.addWidget(t, n // 2, n % 2)
            n += 1

    def paint_live_panel(self):
        """B1K1: the picked machine (what is on it, what waits here) and
        every machine on the plan with what it is cutting.  v69: only the
        top part is rebuilt when the selection changes; the machine list is
        rebuilt only when the plan or the live data changes."""
        if getattr(self, "_lp_top", None) is None:
            self._lp_top = QWidget()
            self._lp_top_lay = QVBoxLayout(self._lp_top)
            self._lp_top_lay.setContentsMargins(0, 0, 0, 0)
            self._lp_top_lay.setSpacing(6)
            self._lp_mach = QWidget()
            self._lp_mach_lay = QVBoxLayout(self._lp_mach)
            self._lp_mach_lay.setContentsMargins(0, 0, 0, 0)
            self._lp_mach_lay.setSpacing(6)
            self.live_lay.addWidget(self._lp_top)
            self.live_lay.addWidget(self._lp_mach)
            self._mach_key, self._mach_rows = None, {}
        top_lay = self._lp_top_lay
        while top_lay.count():
            it = top_lay.takeAt(0)
            w_ = it.widget()
            if w_:
                w_.setParent(None)
                w_.deleteLater()
        items = self.state["items"]
        live = self.state["live"]
        sel = sorted(self.state["sel"])
        comp = ctx.db.comp_map()
        if sel:
            it = items[sel[0]]
            wc = (it.get("wc_code") or "").strip()
            self.livep.title_lbl.setText("%s%s" % (it.get("label") or it["kind"].title(),
                                                    (" · %s" % wc) if wc else ""))
            info = []
            if it["kind"] == "machine":
                grp = ", ".join(it.get("groups") or []) or (comp.get(wc, ("", ""))[1] if wc else "")
                info = [("Work centre", wc or "-"), ("Machine group", grp or "-")]
                ops = sorted({str(q["last_op"]) for q in live.get(self.bucket_key(it), [])
                              if q["running"]})
                info.append(("Ops here", ", ".join(ops) or "-"))
            elif it["kind"] == "zone":
                info = [("Machine groups", ", ".join(it.get("groups") or []) or "-"),
                        ("Waits for (ops)", ", ".join(it.get("ops") or []) or "-"),
                        ("Part groups", ", ".join(it.get("part_groups") or []) or "all")]
            gw = QWidget()
            gl = QGridLayout(gw)
            gl.setContentsMargins(0, 0, 0, 4)
            gl.setVerticalSpacing(4)
            for r, (k, v) in enumerate(info):
                gl.addWidget(label(k, muted=True), r, 0)
                vl = label(v)
                vl.setStyleSheet("font-weight: 700;")
                gl.addWidget(vl, r, 1)
            top_lay.addWidget(gw)
            found = []
            for i in sel:
                found.extend(live.get(self.bucket_key(items[i]), []))
            run = [q for q in found if q["running"] and not q["stalled"]]
            rest = [q for q in found if q not in run]
            if it["kind"] == "machine":
                # v75: the parts waiting for THIS machine's group sit in a
                # waiting area, not on the machine - list them here too
                want = {norm_text(g) for g in (it.get("groups") or [])} or \
                    ({norm_text(comp.get(wc, ("", ""))[1])} if wc else set())
                want.discard("")
                have = {q["serial_key"] for q in found}
                rest += [q for q in self.state.get("pos", [])
                         if not q["running"] and q["serial_key"] not in have
                         and norm_text(q.get("next_group") or "") in want]
            rest.sort(key=lambda q: -(q["days"] or 0))
            if it["kind"] == "machine" or run:
                top_lay.addWidget(self._sec("On the machine"))
                if not run:
                    top_lay.addWidget(label("nothing on it now", muted=True))
                    last = self.state.get("last_on", {}).get(self.bucket_key(it))
                    if last is not None:
                        top_lay.addWidget(label("Last loaded", muted=True))
                        top_lay.addWidget(LiveRow(last))
                for q in run:
                    top_lay.addWidget(LiveRow(q, big=True))
            top_lay.addWidget(self._sec("Waiting for this machine" if it["kind"] == "machine"
                                              else "In this area"))
            if not rest:
                top_lay.addWidget(label("none", muted=True))
            for q in rest[:60]:
                top_lay.addWidget(LiveRow(q))
            if len(rest) > 60:
                top_lay.addWidget(label("+ %d more" % (len(rest) - 60), muted=True))
        else:
            self.livep.title_lbl.setText("Machines")
            top_lay.addWidget(label("Click a machine on the plan, or one below.", muted=True,
                                          wrap=True))
        key = (id(items), id(live), ctx.TH["name"])
        if key != self._mach_key:
            ml = self._lp_mach_lay
            while ml.count():
                it_ = ml.takeAt(0)
                w_ = it_.widget()
                if w_:
                    w_.setParent(None)
                    w_.deleteLater()
            ml.addWidget(self._sec("Machines"))
            self._mach_rows = {}
            for i, it in enumerate(items):
                if it["kind"] != "machine":
                    continue
                qs = [q for q in live.get(self.bucket_key(it), []) if q["running"]]
                row = MachineRow(self, i, it, qs[0] if qs else None, i in sel)
                self._mach_rows[i] = row
                ml.addWidget(row)
            self._mach_key = key
        else:
            for i, row in self._mach_rows.items():
                row.set_picked(i in sel)

    def _sec(self, text):
        l_ = label(text, obj="CardTitle")
        l_.setContentsMargins(0, 8, 0, 0)
        return l_

    def pick_item(self, i):
        self.state["sel"] = {i}
        self.draw()
        self.sync_details()

    # =================================================================
    #  geometry / rules (as page_b1000)
    # =================================================================
    def zoom(self):
        return max(0.1, self.zoom_pct / 100.0)

    @staticmethod
    def item_rect(it):
        return it["x"], it["y"], it["x"] + it["w"], it["y"] + it["h"]

    @staticmethod
    def centre_in(outer, inner):
        cx = inner["x"] + inner["w"] / 2.0
        cy = inner["y"] + inner["h"] / 2.0
        return (outer["x"] <= cx <= outer["x"] + outer["w"]
                and outer["y"] <= cy <= outer["y"] + outer["h"])

    def zone_contents(self, i):
        it = self.state["items"][i]
        if it["kind"] != "zone" or not it.get("carry", True):
            return set()
        return {j for j, o in enumerate(self.state["items"]) if j != i and self.centre_in(it, o)}

    def move_group(self, sel):
        out = set()
        for i in sel:
            if self.state["items"][i].get("locked"):
                continue
            out.add(i)
            out |= {j for j in self.zone_contents(i) if not self.state["items"][j].get("locked")}
        return out

    def locked_item(self, i):
        if self.state["items"][i].get("locked"):
            return True
        for zi, it in enumerate(self.state["items"]):
            if it["kind"] == "zone" and it.get("locked") and it.get("carry", True) \
                    and i in self.zone_contents(zi):
                return True
        return False

    def hit(self, px, py):
        under = []
        for i in range(len(self.state["items"]) - 1, -1, -1):
            x0, y0, x1, y1 = self.item_rect(self.state["items"][i])
            if x0 <= px <= x1 and y0 <= py <= y1:
                under.append((abs((x1 - x0) * (y1 - y0)), i))
        if not under:
            return None
        return min(under, key=lambda t: (t[0], -t[1]))[1]

    def handle_at(self, px, py):
        tol = HANDLE / self.zoom()
        for i in self.state["sel"]:
            x0, y0, x1, y1 = self.item_rect(self.state["items"][i])
            for name, hx, hy in (("nw", x0, y0), ("ne", x1, y0), ("sw", x0, y1), ("se", x1, y1)):
                if abs(px - hx) <= tol and abs(py - hy) <= tol:
                    return i, name
        return None

    @staticmethod
    def colour_for(it):
        return it.get("color") or LA.KIND_COLOR.get(it["kind"], "#8FA3B7")

    def plan_extent(self):
        W, H = self.state["canvas"]
        for it in self.state["items"]:
            W = max(W, it["x"] + it["w"] + 40)
            H = max(H, it["y"] + it["h"] + 40)
        return int(W), int(H)

    def bucket_key(self, it):
        if it["kind"] == "machine":
            return "wc:" + (it.get("wc_code") or "")
        if it["kind"] == "zone":
            if it.get("groups"):
                return "grp:" + ",".join(sorted(norm_text(g) for g in it["groups"]))
            if it.get("ops"):
                return "op:" + ",".join(sorted(step_key(*split_step(o)) for o in it["ops"]))
        return ""

    def snap(self, v):
        return round(v / GRID) * GRID if self.t_snap.isChecked() else v

    def draw(self):
        if not (self.view._cache and self.view._cache[0][0] == "edit"
                and self.state.get("drag")):
            self.view._cache = None          # a drag keeps its still picture
        self.state["rev"] = self.state.get("rev", 0) + 1
        self.view.resize_to_plan()
        self.zoom_lbl.setText("%d%%" % self.zoom_pct)
        if self.state["mode"] == "edit":
            n = len(self.undo_stack) if self.state.get("dirty") else 0
            self.edit_status.setText(("%d unsaved change%s · not live until Save plan"
                                      % (n, "" if n == 1 else "s")) if n
                                     else "saved · this is the live plan")

    # =================================================================
    #  live data
    # =================================================================
    def current_filters(self):
        f = dict(self.bar.filters())
        f.update({"engineer": self.bar.engineer(), "part_group": self.bar.group(),
                  "part_no": self.bar.parts.get_selected()})
        return f

    def refresh_live(self):
        if not self.state.get("shown"):
            self.draw()
            return
        f = self.current_filters()
        d1, d2 = time_mode_to_date_range(f, ctx.db.week_start("b1000"))
        scope = allowed_part_keys(ctx.db, f["engineer"], f["part_group"], f["part_no"],
                                  ctx.db.merge_on("b1000"))
        ctx.busy(True, "Reading the floor...")
        try:
            pos = live_positions(ctx.db, part_keys=scope, d1=d1, d2=d2,
                                 exclude_sold=self.bar.wip.isChecked())
        finally:
            ctx.busy(False)
        zone_ops, zone_groups, zone_only_pg = {}, {}, []
        for it in self.state["items"]:
            if it["kind"] != "zone":
                continue
            pg = {norm_text(g) for g in (it.get("part_groups") or []) if str(g).strip()}
            if it.get("groups"):
                for g in it["groups"]:
                    zone_groups.setdefault(norm_text(g), []).append((pg, self.bucket_key(it)))
            elif it.get("ops"):
                for o in it["ops"]:
                    zone_ops.setdefault(step_key(*split_step(o)), []).append(
                        (pg, self.bucket_key(it)))
            elif pg:
                zone_only_pg.append((pg, self.bucket_key(it)))
        for table in (zone_groups, zone_ops):
            for k in table:
                table[k].sort(key=lambda zz: 0 if zz[0] else 1)

        def claim(table, key, part_group):
            for pg, bk in table.get(key, ()):
                if not pg or norm_text(part_group or "") in pg:
                    return bk
            return None

        buckets = {}
        part_group_of = ctx.db.part_group_map()
        for q in pos:
            if q["running"]:
                key = "wc:" + q["last_wc"]
            else:
                grp = part_group_of.get(norm_text(q["material"]), "")
                nxt_op, nxt_seq = split_step(q["next_op"])
                key = (claim(zone_groups, q["next_group"], grp)
                       or claim(zone_ops, step_key(nxt_op, nxt_seq), grp)
                       or (claim(zone_ops, step_key(nxt_op), grp) if nxt_seq else None)
                       or claim({"": zone_only_pg}, "", grp)
                       or ("wc:" + q["last_wc"] if q["last_wc"] else ""))
            if not key:
                continue
            buckets.setdefault(key, []).append(q)
        self.state["live"] = buckets
        # v75: what was put on each machine last - shown on a machine that
        # is not cutting anything now, and in its panel
        last_on = {}
        for q in pos:
            if not q.get("last_wc"):
                continue
            k = "wc:" + q["last_wc"]
            stamp = (q.get("last_entry") or _MIN_DT, q.get("date") or "")
            if k not in last_on or stamp > last_on[k][0]:
                last_on[k] = (stamp, q)
        self.state["last_on"] = {k: v[1] for k, v in last_on.items()}
        self.state["pos"] = pos
        # v73: no totals line (on the floor / being cut / waiting) - the
        # plan itself shows where the parts are
        self.info_lbl.setText("")
        self.plan_card.set_sub("click a machine")
        self.paint_capacity()
        self.draw()
        self.show_selected_contents()

    def paint_capacity(self):
        """Only the warnings, and only while the plan is being edited
        (machines that carry no capacity) - v73: the capacity line itself
        is not shown."""
        if self.state.get("mode") != "edit":
            self.cap_lbl.setVisible(False)
            return
        try:
            _unit, _n_by, gaps = capacity_of_items(self.state["items"], ctx.db.comp_map())
        except Exception:
            self.cap_lbl.setText("")
            return
        warn = []
        if gaps["no_wc"]:
            warn.append("%d machine(s) carry NO capacity (no work centre code and no group typed): "
                        "%s" % (len(gaps["no_wc"]), ", ".join(gaps["no_wc"][:6])))
        if gaps["unknown_wc"]:
            warn.append("work centre(s) not in COMP, counted as their own group: %s"
                        % ", ".join(sorted(gaps["unknown_wc"])[:6]))
        self.cap_lbl.setText("   ".join(warn))
        self.cap_lbl.setVisible(bool(warn))

    @staticmethod
    def gauge(q):
        if q["std_hour"] is None:
            return ""
        n = int((q["pct"] or 0) * GAUGE_CELLS)
        n = min(n, GAUGE_CELLS - 1) if q["running"] else GAUGE_CELLS
        n = max(0, n)
        return "%s%s  %.1f/%.1f h" % ("█" * n, "░" * (GAUGE_CELLS - n), q["booked"], q["std_hour"])

    def show_selected_contents(self):
        found = []
        for i in sorted(self.state["sel"]):
            found.extend(self.state["live"].get(self.bucket_key(self.state["items"][i]), []))
        found.sort(key=lambda q: ((0, q["remaining"] or 0.0) if q["running"] and not q["stalled"]
                                  else (1, -(q["days"] or 0))))
        rows = []
        for q in found:
            if q["std_hour"] is None:
                st = "%s d" % q["days"] if q["days"] is not None else ""
            elif q["running"]:
                st = WORDS["processing"]
            elif q.get("queued"):
                st = WORDS["queued"]
            elif q["stalled"]:
                st = "%s %sd" % (WORDS["idle"], q["days"] or 0)
            else:
                st = "%s %sd" % (WORDS["waiting"], q["days"] or 0)
            rows.append([q["serial"], short_part(q["material"]), str(q["last_op"]), st,
                         self.gauge(q), "%.1f h" % q["remaining"] if q["remaining"] else "",
                         q["eta"].strftime("%d.%m %H:%M") if q["eta"] else ""])
        if rows:
            self.here.set_data(autofit_columns(HERE_COLS, rows), rows,
                               ["a" if i % 2 == 0 else "b" for i in range(len(rows))], frozen=1)
        else:
            self.here.clear()
        if self.state["mode"] == "live":
            self.paint_live_panel()

    # =================================================================
    #  editing
    # =================================================================
    def may_edit(self, complain=False, fresh=False):
        # fresh: the role as the file has it now (before a write to the
        # shared plan) - it may have been changed on another computer
        if fresh and ctx.window is not None and hasattr(ctx.window, "check_account"):
            try:
                ctx.window.check_account()
            except Exception:
                pass
        if not ctx.is_admin():
            if complain:
                QMessageBox.warning(self, "Edit plan", "Only an admin can change the shared floor "
                                                       "plan.\n\nYou can look at it in Live.")
            return False
        return True

    def snapshot(self):
        return (copy.deepcopy(self.state["items"]), set(self.state["sel"]),
                list(self.state["canvas"]))

    def restore(self, shot):
        items, sel, canvas = shot
        self.state["items"] = copy.deepcopy(items)
        self.state["sel"] = {i for i in sel if i < len(self.state["items"])}
        self.state["canvas"] = list(canvas)
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    def remember(self):
        self.undo_stack.append(self.snapshot())
        del self.undo_stack[:-UNDO_DEPTH]
        self.redo_stack.clear()

    def undo(self):
        if not self.may_edit(complain=True) or not self.undo_stack:
            return
        self.redo_stack.append(self.snapshot())
        self.restore(self.undo_stack.pop())
        self.info_lbl.setText("undo  -  %d step(s) left" % len(self.undo_stack))

    def redo(self):
        if not self.may_edit(complain=True) or not self.redo_stack:
            return
        self.undo_stack.append(self.snapshot())
        self.restore(self.redo_stack.pop())

    def free_spot(self, x, y, w, h):
        W, H = self.state["canvas"]
        for step in range(200):
            cx, cy = x + step * GRID * 2, y + step * GRID * 2
            if cx + w > W or cy + h > H:
                cx, cy = x + (step % 12) * GRID * 2, y + (step // 12) * GRID * 2
            clash = any(not (cx + w <= it["x"] or cx >= it["x"] + it["w"] or
                             cy + h <= it["y"] or cy >= it["y"] + it["h"])
                        for it in self.state["items"])
            if not clash:
                return cx, cy
        return x, y

    def add_item(self, asset_id, x=40, y=40):
        if not self.may_edit(complain=True):
            return
        self.remember()
        a = LA.ASSET[asset_id]
        x, y = self.free_spot(self.snap(x), self.snap(y), a["w"], a["h"])
        self.state["items"].append({"kind": a["kind"], "asset": asset_id, "label": a["name"],
                                    "wc_code": "", "ops": [], "groups": [], "part_groups": [],
                                    "carry": True, "locked": False, "people": 0,
                                    "x": self.snap(x), "y": self.snap(y), "w": a["w"],
                                    "h": a["h"], "rot": 0, "color": ""})
        self.state["sel"] = {len(self.state["items"]) - 1}
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    def add_plain(self, kind):
        self.add_item("ASSET-35" if kind == "zone" else "ASSET-27")
        if not self.may_edit():
            return
        self.state["items"][-1]["kind"] = kind
        self.state["items"][-1]["label"] = "NEW ZONE" if kind == "zone" else "AREA"
        self.draw()
        self.sync_details()

    def remove_sel(self):
        if not self.may_edit(complain=True) or not self.state["sel"] or self.state["mode"] != "edit":
            return
        self.remember()
        for i in sorted(self.state["sel"], reverse=True):
            del self.state["items"][i]
        self.state["sel"] = set()
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    def rotate_sel(self):
        if not self.may_edit(complain=True) or not self.state["sel"] or self.state["mode"] != "edit":
            return
        self.remember()
        for i in self.state["sel"]:
            it = self.state["items"][i]
            it["rot"] = (int(it.get("rot") or 0) + 90) % 360
            cx, cy = it["x"] + it["w"] / 2.0, it["y"] + it["h"] / 2.0
            it["w"], it["h"] = it["h"], it["w"]
            it["x"], it["y"] = self.snap(cx - it["w"] / 2.0), self.snap(cy - it["h"] / 2.0)
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    def duplicate_sel(self):
        if not self.may_edit(complain=True):
            return
        group = sorted(self.move_group(self.state["sel"]))
        if not group:
            return
        its = self.state["items"]
        x0 = min(its[i]["x"] for i in group)
        y0 = min(its[i]["y"] for i in group)
        w = max(its[i]["x"] + its[i]["w"] for i in group) - x0
        h = max(its[i]["y"] + its[i]["h"] for i in group) - y0
        nx, ny = self.free_spot(x0 + 20, y0 + 20, w, h)
        dx, dy = nx - x0, ny - y0
        self.remember()
        made = []
        for i in group:
            it = dict(its[i])
            it["x"], it["y"] = self.snap(it["x"] + dx), self.snap(it["y"] + dy)
            its.append(it)
            made.append(len(its) - 1)
        self.state["sel"] = set(made)
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    def pick_colour(self):
        if not self.may_edit(complain=True) or not self.state["sel"]:
            return
        cur = self.state["items"][min(self.state["sel"])].get("color") or "#7AABFF"
        c = QColorDialog.getColor(QColor(cur), self, "Colour")
        if not c.isValid():
            return
        self.remember()
        for i in self.state["sel"]:
            self.state["items"][i]["color"] = c.name()
        self.state["dirty"] = True
        self.draw()

    def fit(self):
        vp = self.scroll.viewport()
        W, H = self.plan_extent()
        pct = int(max(10, min(200, 100 * min((vp.width() - 20) / float(max(1, W)),
                                             (vp.height() - 20) / float(max(1, H))))))
        self.set_zoom_pct(pct)

    def set_zoom_pct(self, pct):
        self.zoom_pct = max(10, min(400, int(pct)))
        self.draw()

    # ---- selection panel ----
    def machine_group_hint(self, it):
        typed = [g for g in it.get("groups", []) if str(g).strip()]
        if typed:
            return "counts towards %s (typed here, COMP is not consulted)" % ", ".join(typed)
        wc = (it.get("wc_code") or "").strip()
        if not wc:
            return ("NO WORK CENTRE CODE and no group typed - this machine carries NO capacity in "
                    "Forecast.  Give it a code, or type its group here.")
        comp = ctx.db.comp_map()
        if wc in comp and (comp[wc][1] or "").strip():
            return "counts towards %s (from COMP, via %s)" % (comp[wc][1], wc)
        return ("%s is not in COMP, so this machine counts as its own group \"%s\".  Type the real "
                "group here if that is wrong." % (wc, wc))

    def group_hint(self, chosen):
        by_group = work_centers_by_group(ctx.db)
        if not chosen:
            return "groups in COMP:  " + (", ".join(sorted(by_group)) or "(COMP not loaded)")
        return "   |   ".join("%s: %s" % (g, ", ".join(sorted(by_group.get(norm_text(g), [])))
                                         or "NOT IN COMP") for g in chosen)

    def part_group_hint(self, chosen):
        have = sorted({(q["part_group"] or "").strip() for q in ctx.db.all_parts()
                       if (q["part_group"] or "").strip()})
        if not chosen:
            return "all parts.  Groups in PART DATA:  " + (", ".join(have) or "(not loaded)")
        known = {norm_text(g) for g in have}
        return "only:  " + ",  ".join(g if norm_text(g) in known else "%s: NOT IN PART DATA" % g
                                      for g in chosen)

    def sync_details(self):
        edit = self.state["mode"] == "edit"
        if not self.state["sel"]:
            self.detail.title_lbl.setText("Nothing selected")
            for w in self.rows.values():
                w.hide()
            for w in (self.grp_hint, self.pgrp_hint, self.opt_row, self.size_lbl):
                w.hide()
            self.edit_btns.setVisible(False)
            self.paint_capacity()
            self.show_selected_contents()
            return
        it = self.state["items"][min(self.state["sel"])]
        n = len(self.state["sel"])
        title = "Selected · %s%s" % (it.get("label") or it["kind"].title(),
                                     "  (%d)" % n if n > 1 else "")
        kind = it["kind"]
        for e, v in ((self.f_name, it.get("label", "")), (self.f_wc, it.get("wc_code", "")),
                     (self.f_people, str(int(it.get("people") or 0) or "")),
                     (self.f_groups, ", ".join(str(g) for g in it.get("groups", []))),
                     (self.f_ops, ", ".join(str(o) for o in it.get("ops", []))),
                     (self.f_pgroups, ", ".join(str(g) for g in it.get("part_groups", [])))):
            e.blockSignals(True)
            e.setText(v)
            e.blockSignals(False)
            e.setReadOnly(not (edit and self.may_edit()))
        self.rows["name"].show()
        self.rows["wc"].setVisible(kind == "machine")
        self.rows["people"].setVisible(kind == "machine")
        self.rows["groups"].setVisible(kind in ("machine", "zone"))
        self.rows["ops"].setVisible(kind == "zone")
        self.rows["pgroups"].setVisible(kind == "zone")
        self.opt_row.setVisible(kind == "zone")
        self.grp_hint.setVisible(kind in ("machine", "zone"))
        self.pgrp_hint.setVisible(kind == "zone")
        if kind == "machine":
            self.grp_hint.setText(self.machine_group_hint(it))
        if kind == "zone":
            for t, v in ((self.t_carry, it.get("carry", True)), (self.t_lock, it.get("locked", False))):
                t.blockSignals(True)
                t.setChecked(bool(v))
                t.blockSignals(False)
                t.setEnabled(edit and self.may_edit())
            self.grp_hint.setText(self.group_hint(it.get("groups", [])))
            self.pgrp_hint.setText(self.part_group_hint(it.get("part_groups", [])))
            held = self.zone_contents(min(self.state["sel"]))
            if held:
                title += "  · holds %d" % len(held)
        self.detail.title_lbl.setText(title)
        self.size_lbl.setText("%d x %d  at  %d, %d" % (it["w"], it["h"], it["x"], it["y"]))
        self.size_lbl.show()
        self.edit_btns.setVisible(edit)
        self.paint_capacity()
        self.show_selected_contents()

    def apply_options(self):
        if not self.may_edit() or self.state["mode"] != "edit" or not self.state["sel"]:
            self.sync_details()
            return
        self.remember()
        for i in self.state["sel"]:
            it = self.state["items"][i]
            if it["kind"] == "zone":
                it["carry"] = self.t_carry.isChecked()
                it["locked"] = self.t_lock.isChecked()
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    def apply_details(self):
        if not self.state["sel"] or self.state["mode"] != "edit" or not self.may_edit():
            return
        pre = copy.deepcopy(self.state["items"])
        for i in self.state["sel"]:
            it = self.state["items"][i]
            it["label"] = self.f_name.text().strip()
            split = lambda s: [x.strip() for x in s.replace(";", ",").split(",") if x.strip()]
            if it["kind"] == "machine":
                it["wc_code"] = self.f_wc.text().strip()
                try:
                    it["people"] = max(0, int(self.f_people.text().strip() or 0))
                except ValueError:
                    it["people"] = 0
                it["groups"] = split(self.f_groups.text())
            if it["kind"] == "zone":
                it["ops"] = split(self.f_ops.text())
                it["groups"] = split(self.f_groups.text())
                it["part_groups"] = split(self.f_pgroups.text())
        if pre != self.state["items"]:
            self.undo_stack.append((pre, set(self.state["sel"]), list(self.state["canvas"])))
            del self.undo_stack[:-UNDO_DEPTH]
            self.redo_stack.clear()
            self.state["dirty"] = True
        self.draw()
        self.sync_details()

    # ---- mouse (page side) ----
    def on_press(self, px, py, spos, shift):
        st = self.state
        if st["mode"] == "edit":
            h = self.handle_at(px, py)
            if h and self.locked_item(h[0]):
                h = None
            if h and self.may_edit():
                zi = h[0]
                self.remember()
                st["drag"] = {"kind": "resize", "i": zi, "corner": h[1], "sx": spos.x(),
                              "sy": spos.y(), "live": False,
                              "box": self.item_rect(st["items"][zi]),
                              "inner": {j: (st["items"][j]["x"], st["items"][j]["y"],
                                            st["items"][j]["w"], st["items"][j]["h"])
                                        for j in self.zone_contents(zi)}}
                return
        i = self.hit(px, py)
        if i is None:
            st["sel"] = set()
            st["drag"] = None
        else:
            if shift:
                st["sel"] ^= {i}
            elif i not in st["sel"]:
                st["sel"] = {i}
            if st["mode"] == "edit" and not self.locked_item(i) and self.may_edit():
                self.remember()
                st["drag"] = {"kind": "move", "px": px, "py": py, "sx": spos.x(), "sy": spos.y(),
                              "live": False,
                              "orig": {j: (st["items"][j]["x"], st["items"][j]["y"])
                                       for j in self.move_group(st["sel"])}}
        self.view.update()
        self.sync_details()

    def on_motion(self, px, py, spos):
        d = self.state["drag"]
        if not d or self.state["mode"] != "edit":
            return
        if not d["live"]:
            if abs(spos.x() - d["sx"]) + abs(spos.y() - d["sy"]) < DRAG_START:
                return
            d["live"] = True
        its = self.state["items"]
        if d["kind"] == "move":
            dx, dy = px - d["px"], py - d["py"]
            for j, (ox, oy) in d["orig"].items():
                its[j]["x"] = self.snap(ox + dx)
                its[j]["y"] = self.snap(oy + dy)
        else:
            it = its[d["i"]]
            bx0, by0, bx1, by1 = d["box"]
            x0, y0, x1, y1 = self.item_rect(it)
            if "w" in d["corner"]:
                x0 = min(self.snap(px), x1 - GRID)
            else:
                x1 = max(self.snap(px), x0 + GRID)
            if "n" in d["corner"]:
                y0 = min(self.snap(py), y1 - GRID)
            else:
                y1 = max(self.snap(py), y0 + GRID)
            it.update(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
            if d["inner"]:
                fx = (x1 - x0) / float(max(1e-6, bx1 - bx0))
                fy = (y1 - y0) / float(max(1e-6, by1 - by0))
                for j, (ox, oy, ow, oh) in d["inner"].items():
                    inner = its[j]
                    inner["x"] = x0 + (ox - bx0) * fx
                    inner["y"] = y0 + (oy - by0) * fy
                    inner["w"] = max(GRID, ow * fx)
                    inner["h"] = max(GRID, oh * fy)
        self.state["dirty"] = True
        self.draw()

    def on_release(self):
        d = self.state["drag"]
        if d:
            if not d.get("live") and self.undo_stack:
                self.undo_stack.pop()          # a plain click changed nothing
            self.state["drag"] = None
            self.sync_details()

    def on_double(self, px, py):
        if self.state["mode"] != "edit" or not self.may_edit():
            return
        i = self.hit(px, py)
        if i is None:
            return
        it = self.state["items"][i]
        if it["kind"] == "machine":
            v, ok = QInputDialog.getText(self, "Work centre", "Work centre code for %s:" % it["label"],
                                         text=it.get("wc_code", ""))
            if ok:
                self.remember()
                it["wc_code"] = v.strip()
        else:
            v, ok = QInputDialog.getText(self, "Operations", "Operations waiting here (comma "
                                         "separated):", text=", ".join(it.get("ops", [])))
            if ok:
                self.remember()
                it["ops"] = [o.strip() for o in v.replace(";", ",").split(",") if o.strip()]
        self.state["dirty"] = True
        self.draw()
        self.sync_details()

    # =================================================================
    #  save / load
    # =================================================================
    def save_plan(self):
        if not self.may_edit(complain=True, fresh=True):
            return
        missing = [it for it in self.state["items"] if it["kind"] == "machine" and not it.get("wc_code")]
        if missing and QMessageBox.question(self, "Save plan", "%d machine(s) have no work centre "
                                            "code and will show no data.\n\nSave anyway?"
                                            % len(missing)) != QMessageBox.Yes:
            return
        note, ok = QInputDialog.getText(self, "Save plan", "A note for this version:")
        if not ok:
            return
        try:
            ctx.db.layout_save("B1000", self.state["canvas"], self.state["items"],
                               saved_by=ctx.username(), note=note or "")
        except Exception as e:
            QMessageBox.critical(self, "Save plan", "Could not save:\n\n%s" % e)
            return
        self.state["dirty"] = False
        lay = ctx.db.layout_active("B1000")
        QMessageBox.information(self, "Save plan", "Saved as version %d." % (lay["version"] if lay else 0))

    def export_plan(self):
        lay = ctx.db.layout_active("B1000")
        if not lay:
            QMessageBox.information(self, "Backup to file", "There is no saved plan to back up yet.")
            return
        path, _x = QFileDialog.getSaveFileName(
            self, "Back up the B1000 plan", "B1000_PLAN_v%s_%s.json"
            % (lay.get("version") or 0, datetime.date.today().isoformat()),
            "WIP TRACKER plan (*.json);;All files (*)")
        if not path:
            return
        try:
            info = ctx.db.layout_export(path)
        except Exception as e:
            QMessageBox.critical(self, "Backup to file", "Could not write:\n\n%s" % e)
            return
        QMessageBox.information(self, "Backup to file", "Version %s saved, %d item(s).\n\n%s\n\nKeep "
                                "this somewhere that is not the shared folder."
                                % (info["version"], info["items"], info["path"]))

    def import_plan(self):
        if not self.may_edit(complain=True, fresh=True):
            return
        path, _x = QFileDialog.getOpenFileName(self, "Restore a B1000 plan", "",
                                               "WIP TRACKER plan (*.json);;All files (*)")
        if not path:
            return
        try:
            info = ctx.db.layout_import(path, saved_by=ctx.username())
        except Exception as e:
            QMessageBox.critical(self, "Restore from file", "Could not read that file:\n\n%s" % e)
            return
        if not info.get("layout_id"):
            QMessageBox.critical(self, "Restore from file", "The database is read-only from this "
                                 "machine, so nothing was written.")
            return
        self.load_plan()
        lay = ctx.db.layout_active("B1000")
        QMessageBox.information(self, "Restore from file", "Restored %d item(s) from %s as version "
                                "%s.\n\nEvery earlier version is still on file."
                                % (info["items"], os.path.basename(path),
                                   lay["version"] if lay else "?"))

    def load_plan(self):
        lay = ctx.db.layout_active("B1000")
        if not lay:
            self.state["items"] = []
            self.state["canvas"] = list(LA.BASE_CANVAS)
            self.info_lbl.setText("No plan saved yet - switch to Edit plan and start from the "
                                  "library.")
            self.draw()
            return
        self.state["layout_id"] = lay["id"]
        self.state["canvas"] = [lay["canvas_w"], lay["canvas_h"]]
        self.state["items"] = [{
            "kind": r["kind"], "asset": r["asset"], "label": r["label"] or "",
            "wc_code": r["wc_code"] or "",
            "ops": [o for o in (r["ops"] or "").split(",") if o],
            "groups": [g for g in (r.get("groups") or "").split(",") if g],
            "part_groups": [g for g in (r.get("part_groups") or "").split(",") if g],
            "carry": bool(r["carry"]) if "carry" in r.keys() else True,
            "locked": bool(r["locked"]) if "locked" in r.keys() else False,
            "people": (int(r["people"] or 0) if "people" in r.keys() else 0),
            "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"],
            "rot": r["rot"] or 0, "color": r["color"] or ""}
            for r in ctx.db.layout_items(lay["id"])]
        self.state["sel"] = set()
        self.state["dirty"] = False
        self.draw()

    def reload_plan_if_safe(self):
        if self.state.get("dirty"):
            self.info_lbl.setText("Somebody else saved a new plan - your unsaved changes are kept.  "
                                  "Press Reload to take theirs.")
            return False
        self.load_plan()
        return True

    # =================================================================
    #  mode / page
    # =================================================================
    def paint_library(self):
        self.lib.setVisible(self.state["mode"] == "edit" and self.t_lib.isChecked())

    def set_mode(self, m):
        if m == "edit" and not self.may_edit(complain=True):
            self.mode_seg.set("live")
            m = "live"
        self.state["mode"] = m
        self.mode_seg.set(m)
        edit = m == "edit"
        self.edit_bar.setVisible(edit)
        self.paint_library()
        self.paint_side()
        self.plan_card.title_lbl.setText("Floor plan · editing" if edit else "Floor plan")
        if edit:
            self.plan_card.set_sub("drag to move · corner to resize · R rotate · Del remove · "
                                   "Ctrl+Z / Ctrl+Y")
        if edit:
            self.draw()
        else:
            self.refresh_live()
        self.sync_details()

    def cascade(self):
        parts = ctx.db.all_parts(ctx.db.merge_on("b1000"))
        eng = self.bar.set_choices(self.bar.eng, sorted({q["engineer"] for q in parts if q["engineer"]}))
        grp = self.bar.set_choices(self.bar.grp, sorted(
            {q["part_group"] for q in parts if q["part_group"]
             and (eng == ALL or (q["engineer"] or "").upper() == eng.upper())}))
        self.bar.parts.rebuild(sorted({q["part_no"] for q in parts
                                       if (eng == ALL or (q["engineer"] or "").upper() == eng.upper())
                                       and (grp == ALL or q["part_group"] == grp)}))
        self.bar.paint_chips()
        self.schedule()

    def schedule(self):
        if self.built:
            self.timer.start(220)

    def on_show(self):
        if not self.built:
            self.built = True
            self.bar.refresh_years(ctx.db.rw_years() or ctx.db.wf_years())
            self.load_plan()
            self.cascade()
        self.state["shown"] = True
        self.timer.stop()
        if self.state["mode"] == "live":
            self.refresh_live()
        else:
            self.draw()
        if not self.state["items"] or self.zoom_pct == 100:
            QTimer.singleShot(0, self.fit)

    def reload(self):
        if self.built:
            self.reload_plan_if_safe()
            self.bar.refresh_years(ctx.db.rw_years() or ctx.db.wf_years())
            self.cascade()

    def user_changed(self):
        if self.state["mode"] == "edit" and not ctx.is_admin():
            self.set_mode("live")

    def unsaved(self):
        return bool(self.state.get("dirty"))

    def refresh_theme(self):
        self.view.update()
        self.paint_capacity()
        if self.state["mode"] == "live":
            self.paint_live_panel()

    def annotations_changed(self):
        # v71: a note or a TECO / MRB mark somewhere does not move a part
        # on the floor - re-reading every position for it froze the plan
        # every time anybody typed a note
        pass

# -*- coding: utf-8 -*-
"""WATERFALL - the Qt page (new design).

Same as the Tk page (page_waterfall.py): one part's serials against its
traveler route, frozen info columns (SERIAL, CURRENT OP, STATUS, WAITING,
REMAINING) on the left and one narrow column per operation, numbers
written upright.  Rows come furthest along first.  Durations read 0.00.
(2.2.0: lead time / queue / flow efficiency moved to its own page,
LEAD TIME ANALYSIS - qt_page_leadtime.)  A red rework box opens that serial's rework
records for the operation.
"""

import datetime
import re
import traceback

from PySide6.QtCore import Qt, QTimer, QRect, QRectF, QPoint, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetrics, QBrush, QLinearGradient
from PySide6.QtWidgets import (QWidget, QMessageBox, QFileDialog,
                               QAbstractScrollArea, QSlider, QSpinBox, QFrame, QToolTip)

from wip_core import (ALL, UNKNOWN, WF_STATUS_COLOR_KEY, WORDS,
                      export_waterfall_to_excel, fmt_hour, format_wf_hours,
                      norm_text, route_step, time_mode_to_date_range, wf_format_remaining,
                      wf_predicted_view, wf_current_op)
from qt_widgets import (ctx, ModuleCard, FilterBar, Toggle, Seg, FlowLayout,
                        label, button, hbox, KpiInline, ModuleGrid, page_layout,
                        page_head, export_button, view_room, mix)
from qt_theme import FLAG_COLORS, FONT

WF_FLAG_COLORS = {k: v for k, v in FLAG_COLORS.items() if k != "fixed"}
LEGEND = [("wf_green", "done"), ("wf_running", "on machine"), ("wf_wait", "waiting"),
          ("row_b", "not reached yet"), ("accent", "route complete"),
          ("wf_awaiting", "awaiting shipment"), ("danger", "rework booked")]


def _mix(a, b, t):
    try:
        ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    except (TypeError, ValueError, IndexError):
        return a
    return "#%02X%02X%02X" % tuple(int(round(x + (y - x) * t)) for x, y in zip(ca, cb))


def status_text(row):
    """The full status (tooltip): "In Progress" covers queueing and being
    cut - named apart here."""
    run = row.get("running")
    if run is None:
        return row["status"]
    return "%s %s/%sh" % (WORDS["processing"], fmt_hour(run["booked"]), fmt_hour(run["std"]))


STATUS_SHORT = {"Sold": "SOLD", "Awaiting Shipment": "SHIP", "Not Started": "NEW"}


def status_short(row):
    """v69: the STATUS column in a few letters (the full text is the
    tooltip) - RUN 4.2/5.7h, WAIT, SHIP, SOLD, NEW."""
    run = row.get("running")
    if run is not None:
        return "RUN"           # v71: the hours are in the cell and the tooltip
    st = row.get("status") or ""
    if st in STATUS_SHORT:
        return STATUS_SHORT[st]
    if st == "In Progress":
        return "WAIT" if (row.get("waiting_hours") or 0) > 0 else "IN WORK"
    return st


class WaterfallMatrix(QAbstractScrollArea):
    """Painted by hand: corner, upright operation header, frozen info
    columns and the cells.  Only the rows on screen are drawn, so a part
    with a thousand serials scrolls as lightly as one with ten."""

    INFO_COLS = ["SERIAL", "CURRENT OP", "STATUS", "WAITING", "REMAINING"]

    def __init__(self):
        super().__init__()
        self.zoom, self.unit, self.show_lead, self.show_desc = 1.0, "days", False, False
        self.result, self.route, self.rows = None, [], []
        self.flag_map, self.note_colors = {}, {}
        self.on_rework_click = None
        self.on_ctrl_wheel = None
        self._vals = []
        self._widths = []
        self._fit_cache = {}
        self.setFrameShape(QFrame.NoFrame)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        ctx.tables.append(self)

    # ---- geometry ----
    def z(self):
        return max(0.25, self.zoom)

    def row_h(self):
        return int(30 * self.z())

    def col_w(self):
        # v71: square cells - as wide as a row is tall
        return max(self.row_h(), 18)

    def font(self, size, bold=False):
        f = QFont(FONT)
        f.setPointSizeF(max(6.0, size * self.z()))
        f.setBold(bold)
        return f

    def head_h(self):
        z = self.z()
        op_f = QFontMetrics(self.font(self.HEAD_PT, True))
        longest = max((op_f.horizontalAdvance(str(c.get("label") or c["op"])) for c in self.route),
                      default=0)
        # v71: the info headings are written upright too
        info_f = QFontMetrics(self.font(self.HEAD_PT, True))
        longest = max([longest] + [info_f.horizontalAdvance(self.info_title(t))
                                   for t in self.INFO_COLS])
        self._op_h = max(int(28 * z), longest + int(14 * z))
        self._desc_h = int(64 * z) if self.show_desc else 0
        self._avg_h = int(18 * z) if self.show_lead else 0
        return self._op_h + self._desc_h + self._avg_h

    def froz_w(self):
        return sum(self._widths)

    @staticmethod
    def info_title(t):
        return t[:1] + t[1:].lower()

    HEAD_PT = 7.5        # v73: the upright headings, smaller (was 9)

    def rows_shown(self):
        """Rows the window has room for (v72), so the page itself does not
        scroll; zoomed out, the smaller rows fill the same room.  In a
        window of its own: 20 at 100%, more when zoomed out."""
        room = view_room(self)
        if room is None:
            return max(3, min(len(self.rows), max(20, int(20 / self.z()))))
        fit_n = int((room - self.head_h() - 18) // max(1, self.row_h()))
        return max(3, min(len(self.rows), max(5, fit_n)))

    def refit_view(self):
        h = self.head_h() + self.rows_shown() * self.row_h() + 18
        if h != self.height():
            self.setFixedHeight(h)
            self._update_bars()

    def set_result(self, result, zoom, unit, show_lead, show_desc, flag_map=None,
                   note_colors=None, keep_scroll=False):
        top = self.verticalScrollBar().value() if keep_scroll else 0
        self.result, self.zoom, self.unit = result, zoom, unit
        self.show_lead, self.show_desc = show_lead, show_desc
        self.flag_map = flag_map or {}
        self.note_colors = note_colors or {}
        self.route = result["route"] if result else []
        self.rows = result["rows"] if result else []
        self._fit_cache = {}
        self._vals = [[r["serial"], wf_current_op(r, self.route) or UNKNOWN,
                       status_short(r),
                       format_wf_hours(r["waiting_hours"], unit)
                       if r["waiting_hours"] is not None else "",
                       wf_format_remaining(r["remaining"], unit)] for r in self.rows]
        fm = QFontMetrics(self.font(9.5, True))
        z = self.z()
        sample = self._vals
        if len(sample) > 800:
            step = len(sample) / 800.0
            sample = [sample[int(i * step)] for i in range(800)]
        self._widths = []
        for ci, title in enumerate(self.INFO_COLS):
            best = 0             # the heading is upright, only the values count
            for v in sample:
                if v[ci] not in (None, ""):
                    best = max(best, fm.horizontalAdvance(str(v[ci])))
            self._widths.append(max(int(26 * z), min(int(210 * z), best + int(14 * z))))
        # as tall as its rows, up to the card's room - then it scrolls inside
        self.setFixedHeight(self.head_h() + self.rows_shown() * self.row_h() + 18)
        self._update_bars()
        self.verticalScrollBar().setValue(top)
        if not keep_scroll:
            self.horizontalScrollBar().setValue(0)
        self.viewport().update()

    def _update_bars(self):
        vw, vh = self.viewport().width(), self.viewport().height()
        hh = self.head_h()
        sw = self.col_w() * len(self.route)
        th = max(len(self.rows), 1) * self.row_h()
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        hb.setRange(0, max(0, sw - (vw - self.froz_w())))
        hb.setPageStep(max(1, vw - self.froz_w()))
        hb.setSingleStep(self.col_w())
        vb.setRange(0, max(0, th - (vh - hh)))
        vb.setPageStep(max(1, vh - hh))
        vb.setSingleStep(self.row_h())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_bars()

    def refresh_selection(self):
        self.viewport().update()

    def refresh_theme(self):
        self.viewport().update()

    # ---- painting ----
    def fit(self, p, rect, text, size, color, bold=False):
        """Text centred in `rect`, the font shrunk until it fits."""
        text = "" if text is None else str(text)
        if not text:
            return
        key = (text, rect.width(), size, bold, self.zoom)
        f = self._fit_cache.get(key)
        if f is None:
            s = size
            f = self.font(s, bold)
            while QFontMetrics(f).horizontalAdvance(text) > rect.width() - 6 and s > 5:
                s -= 0.5
                f = self.font(s, bold)
            self._fit_cache[key] = f
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(rect, Qt.AlignCenter, text)

    def fit_left(self, p, rect, text, size, color, bold=False):
        text = "" if text is None else str(text)
        if not text:
            return
        f = self.font(size, bold)
        p.setFont(f)
        p.setPen(QColor(color))
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(f).elidedText(text, Qt.ElideRight, rect.width()))

    def paintEvent(self, _e):
        th = ctx.TH
        p = QPainter(self.viewport())
        vw, vh = self.viewport().width(), self.viewport().height()
        p.fillRect(0, 0, vw, vh, QColor(th["card"]))
        hh = self.head_h()
        fw = self.froz_w() if self.route else 0
        if not self.result or not self.route:
            p.setPen(QColor(th["muted"]))
            p.setFont(self.font(10))
            p.drawText(QRect(0, 0, vw, vh), Qt.AlignCenter,
                       "Pick a part." if not self.result else "This part has no traveler route.")
            p.end()
            return
        sx, sy = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        rh, cw = self.row_h(), self.col_w()
        n = len(self.rows)
        first = max(0, sy // rh)
        last = min(n, (sy + vh - hh) // rh + 2)
        c_first = max(0, sx // cw)
        c_last = min(len(self.route), (sx + vw - fw) // cw + 2)
        grid = QPen(QColor(th["grid"]))

        # ---- body ----
        p.save()
        p.setClipRect(QRect(fw, hh, vw - fw, vh - hh))
        p.translate(fw - sx, hh - sy)
        for ri in range(first, last):
            self._paint_row(p, ri, rh, cw, c_first, c_last, grid)
        p.restore()

        # ---- frozen info columns ----
        p.save()
        p.setClipRect(QRect(0, hh, fw, vh - hh))
        p.translate(0, hh - sy)
        for ri in range(first, last):
            row = self.rows[ri]
            sk = row.get("serial_key")
            flag = WF_FLAG_COLORS.get(self.flag_map.get(sk)) or self.note_colors.get(sk)
            picked = bool(ctx.selected_serial) and sk == ctx.selected_serial
            # the design: plain text, a flag shows as a stripe and a coloured serial
            kind = self.flag_map.get(sk)
            # v73: a flag or note tints the serial's info cells softly
            bg = th["sel_row"] if picked else (mix(th["card"], flag, 0.13) if flag else th["card"])
            x = 0
            y0 = ri * rh
            p.fillRect(QRect(0, y0, fw, rh), QColor(bg))
            for ci, (w, val) in enumerate(zip(self._widths, self._vals[ri])):
                r = QRect(x + 6, y0, w - 8, rh)
                fg = th["text"] if ci == 0 else th["muted"] if ci in (1, 3, 4) else th["text"]
                if ci == 0 and kind in WF_FLAG_COLORS:
                    fg = ctx.flag_text(kind)
                if ci == 2:
                    v = str(val)
                    fg = (th["wf_running"] if v.startswith("RUN") else th["wf_wait"] if v == "WAIT"
                          else th["wf_awaiting"] if v == "SHIP" else th["accent"] if v == "SOLD"
                          else th["text"])
                self.fit_left(p, r, val, 9.5, fg, bold=(ci == 0))
                x += w
            if flag:
                p.fillRect(QRect(0, y0 + 2, 3, rh - 4), QColor(flag))
            if picked:
                p.fillRect(QRect(0, y0 + 2, 3, rh - 4), QColor(th["sel_edge"]))
        p.restore()

        # ---- header ----
        p.save()
        p.setClipRect(QRect(fw, 0, vw - fw, hh))
        p.translate(fw - sx, 0)
        op_h, desc_h, avg_h = self._op_h, self._desc_h, self._avg_h
        content_h = hh - avg_h
        op_f, desc_f = self.font(self.HEAD_PT, True), self.font(7)
        for ci in range(c_first, c_last):
            col = self.route[ci]
            x0 = ci * cw
            p.fillRect(QRect(x0, 0, cw, hh), QColor(th["card"]))
            p.save()
            p.setPen(QColor(th["text"]))
            p.setFont(op_f)
            p.translate(x0 + cw / 2 + QFontMetrics(op_f).ascent() / 2 - 1, op_h - int(7 * self.z()))
            p.rotate(-90)
            p.drawText(0, 0, str(col.get("label") or col["op"]))
            p.restore()
            if self.show_desc and cw >= 22:
                p.save()
                p.setPen(QColor(th["muted"]))
                p.setFont(desc_f)
                p.translate(x0 + cw / 2 + 4, content_h - 4)
                p.rotate(-90)
                p.drawText(QRect(0, -12, desc_h - 8, 16), Qt.AlignLeft | Qt.AlignVCenter,
                           QFontMetrics(desc_f).elidedText(str(col["desc"]), Qt.ElideRight,
                                                           desc_h - 8))
                p.restore()
        if self.show_lead:
            for end_pos, info in self.result["averages"].items():
                if info["avg_hours"] is None:
                    continue
                d = info["def"]
                gx0, gx1 = (d["start_pos"] + 1) * cw, (end_pos + 1) * cw
                r = QRect(gx0 + 2, content_h + 2, gx1 - gx0 - 4, avg_h - 4)
                p.setRenderHint(QPainter.Antialiasing)
                night = th["name"] == "night"
                p.setPen(QPen(QColor(122, 168, 245, 190) if night else QColor(31, 99, 214, 165), 1.2))
                p.setBrush(QColor(30, 44, 70, 235) if night else QColor(232, 240, 253, 242))
                p.drawRoundedRect(QRectF(r), 8, 8)
                self.fit(p, r, format_wf_hours(info["avg_hours"], self.unit), 7,
                         "#CFE0FF" if night else "#1F4E9C")
        p.restore()

        # ---- corner ----
        x = 0
        p.fillRect(QRect(0, 0, fw, hh), QColor(th["card"]))
        f = self.font(self.HEAD_PT, True)
        p.setFont(f)
        p.setPen(QColor(th["text"]))
        for title, w in zip(self.INFO_COLS, self._widths):
            # upright, like the operation numbers (v71)
            p.save()
            p.translate(x + w / 2 + QFontMetrics(f).ascent() / 2 - 1, hh - 6)
            p.rotate(-90)
            p.drawText(0, 0, self.info_title(title))
            p.restore()
            x += w
        p.setPen(QColor(th["border"]))
        p.drawLine(0, hh - 1, vw, hh - 1)
        p.end()

    def _paint_row(self, p, ri, rh, cw, c_first, c_last, grid):
        th = ctx.TH
        row = self.rows[ri]
        y0 = ri * rh
        sk = row.get("serial_key")
        picked = bool(ctx.selected_serial) and sk == ctx.selected_serial
        opmap = row["opmap"]
        last_done = row["last_done_pos"]
        all_done = row["all_done"]
        awaiting = row.get("status") == "Awaiting Shipment"
        pred_from = row.get("predicted_from_pos")
        running = row.get("running_pos")
        last_rec = opmap.get(route_step(self.route[last_done])) if last_done >= 0 else None
        stuck = bool(last_rec and last_rec["rework"] > 0)
        here = row.get("here_pos")
        if here is None:
            here = last_done
        here_unstarted = (here is not None and here not in (row.get("done_positions") or ())
                          and running is None)
        for ci in range(c_first, c_last):
            col = self.route[ci]
            x0 = ci * cw
            cell = QRect(x0, y0, cw, rh)
            if all_done:
                key = "awaiting_shipment" if awaiting else "done_all"
            elif ci == running:
                key = "running"
            elif ci < here:
                key = "passed"
            elif ci == here:
                key = ("current" if stuck and here_unstarted
                       else "current_wait" if here_unstarted else "passed")
            elif ci == here + 1 and not here_unstarted and running is None:
                key = "current" if stuck else "current_wait"
            else:
                key = "future"
            color = th[WF_STATUS_COLOR_KEY[key]]
            if picked:
                color = _mix(color, th["sel_edge"], 0.45)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            rr = max(3.0, 6 * self.z())
            p.drawRoundedRect(QRectF(cell).adjusted(2, 3, -2, -3), rr, rr)
            if key == "running":
                run = row.get("running") or {}
                pct = max(0.0, min(1.0, run.get("pct_est") or 0.0))
                self.fit(p, cell, "%s/%s" % (fmt_hour(run.get("booked") or 0.0),
                                             fmt_hour(run.get("std") or 0.0)), 7, "#FFFFFF")
                bh = max(2, int(3 * self.z()))
                p.fillRect(QRectF(x0 + 1, y0 + rh - bh - 1, (cw - 2) * pct, bh),
                           QColor(th["wf_warn"]))
            elif key == "current_wait" and cw > 26 * self.z():
                self.fit(p, cell, WORDS["waiting"], 9, "#3A2E08")
            if pred_from is not None and pred_from < ci <= last_done:
                # no timestamp reaches here yet - the lead-time model puts it here
                p.fillRect(cell, QBrush(QColor(th["accent"]), Qt.BDiagPattern))
                p.setPen(QPen(QColor(th["accent"]), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(cell.adjusted(1, 1, -2, -2))
            rec = opmap.get(route_step(col))
            if rec and rec["rework"] > 0:
                b = self._rework_box(x0, y0, cw, rh)
                g = QLinearGradient(QPointF(b.topLeft()), QPointF(b.bottomLeft()))
                g.setColorAt(0.0, QColor("#C8473C"))
                g.setColorAt(1.0, QColor("#9C2626"))
                p.setPen(QPen(QColor(120, 20, 20, 140), 1))
                p.setBrush(QBrush(g))
                p.drawRoundedRect(QRectF(b), 5, 5)
                if b.width() >= 12:
                    # v71: whole hours here (a click shows the entries)
                    self.fit(p, b, str(max(1, int(rec["rework"] + 0.5))), 6.5, "#FFEDEA")
        if self.show_lead:
            night = th["name"] == "night"
            edge = QColor(122, 168, 245, 190) if night else QColor(31, 99, 214, 165)
            fill = QColor(91, 149, 245, 18) if night else QColor(31, 99, 214, 15)
            chip_bg = QColor(30, 44, 70, 235) if night else QColor(232, 240, 253, 242)
            chip_fg = "#CFE0FF" if night else "#1F4E9C"
            for end_pos, c in row["cell_lead"].items():
                info = self.result["averages"].get(end_pos)
                if not info or c["state"] != "done":
                    continue
                d = info["def"]
                x0, x1 = (d["start_pos"] + 1) * cw, (end_pos + 1) * cw
                fr = QRectF(x0 + 1.5, y0 + 1.5, x1 - x0 - 3, rh - 3)
                p.setPen(QPen(edge, 1.5))
                p.setBrush(fill)
                p.drawRoundedRect(fr, 8, 8)
                txt = format_wf_hours(c["hours"], self.unit)
                f = self.font(7.5)
                tw = QFontMetrics(f).horizontalAdvance(txt) + 10
                # v70: a one-step group is one cell wide - the chip shrinks
                # to the cell (and the text with it) instead of not showing
                tw = min(tw, fr.width() - 4)
                if tw >= 12:
                    cr = QRectF(fr.center().x() - tw / 2, fr.center().y() - 8, tw, 16)
                    p.setPen(Qt.NoPen)
                    p.setBrush(chip_bg)
                    p.drawRoundedRect(cr, 8, 8)
                    self.fit(p, cr.adjusted(1, 0, -1, 0).toRect(), txt, 7.5, chip_fg)
        if picked:
            p.setPen(QPen(QColor(th["sel_edge"]), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(c_first * cw + 1, y0 + 1, (c_last - c_first) * cw - 2, rh - 2), 5, 5)

    def _rework_box(self, x0, y0, cw, rh):
        z = self.z()
        bw = max(12, min(cw - 8, int(26 * z)))
        bh = max(9, min(rh - 6, int(15 * z)))
        return QRect(int(x0 + (cw - bw) / 2), int(y0 + (rh - bh) / 2), bw, bh)

    # ---- mouse ----
    def _locate(self, pos):
        """(row index, column index or -1 for the info columns), or None."""
        if not self.result or not self.route:
            return None
        hh, fw = self.head_h(), self.froz_w()
        if pos.y() < hh:
            return None
        ri = (pos.y() - hh + self.verticalScrollBar().value()) // self.row_h()
        if not 0 <= ri < len(self.rows):
            return None
        if pos.x() < fw:
            return ri, -1, None
        bx = pos.x() - fw + self.horizontalScrollBar().value()
        ci = bx // self.col_w()
        if not 0 <= ci < len(self.route):
            return ri, -2, None
        by = pos.y() - hh + self.verticalScrollBar().value()
        return ri, ci, QPoint(bx, by)

    def _rework_hit(self, loc):
        if not loc or loc[1] < 0:
            return None
        ri, ci, pt = loc
        row, col = self.rows[ri], self.route[ci]
        rec = row["opmap"].get(route_step(col))
        if rec and rec["rework"] > 0:
            if self._rework_box(ci * self.col_w(), ri * self.row_h(), self.col_w(),
                                self.row_h()).adjusted(-2, -2, 2, 2).contains(pt):
                return row["serial"], col.get("label") or col["op"]
        return None

    def mousePressEvent(self, e):
        loc = self._locate(e.position().toPoint())
        if not loc:
            return
        row = self.rows[loc[0]]
        if e.button() == Qt.RightButton and loc[1] == -1:
            ctx.set_selected_serial(row["serial"])
            menu = ctx.hooks.get("serial_menu")
            if menu:
                menu(row["serial"], e.globalPosition().toPoint())
            return
        if e.button() != Qt.LeftButton:
            return
        hit = self._rework_hit(loc)
        if hit and self.on_rework_click:
            self.on_rework_click(*hit)
            return
        ctx.toggle_selected_serial(row["serial"])

    def mouseMoveEvent(self, e):
        loc = self._locate(e.position().toPoint())
        hit = self._rework_hit(loc)
        self.viewport().setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)
        tip = ""
        if loc and loc[1] == -1:
            x = e.position().toPoint().x()
            w0 = sum(self._widths[:2])
            if w0 <= x < w0 + self._widths[2]:
                tip = status_text(self.rows[loc[0]])
        if tip:
            QToolTip.showText(e.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier and self.on_ctrl_wheel:
            self.on_ctrl_wheel(1 if e.angleDelta().y() > 0 else -1)
            return
        super().wheelEvent(e)


def wf_from_op_view(view, step):
    """2.1.1 FROM OP: only the serials at route step `step` or further on
    (a part done with the whole route counts as furthest).  Where a part
    is: its predicted position when shown, else where it is now, else the
    last step it finished - the same as the matrix and the sort.  A step
    the part's route does not have: the first step with an operation
    number at least as high.  Returns (view, info text or "")."""
    if not step:
        return view, ""
    route = view["route"]
    pos = next((i for i, c in enumerate(route) if route_step(c) == step), None)
    if pos is None:
        try:
            want = int(str(step).split("/")[0])
            pos = next((i for i, c in enumerate(route) if int(c.get("op") or 0) >= want),
                       len(route))
        except (TypeError, ValueError):
            return view, ""
    n = len(route)

    def where(r):
        if r.get("all_done"):
            return n
        for k in ("predicted_pos", "here_pos"):
            if r.get(k) is not None:
                return r[k]
        return r.get("last_done_pos", -1)
    rows = [r for r in view["rows"] if where(r) >= pos]
    lbl = (route[pos].get("label") or route[pos].get("op")) if pos < n else step
    return dict(view, rows=rows), "from op %s: %d" % (lbl, len(rows))


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.last = {"view": None, "part": None, "part_disp": None, "label": "All dates"}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        outer = page_layout(self)
        head = page_head(outer, "Waterfall", "Each serial along its route")

        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("waterfall"),
                             part_multi=False, wip_label="By WIP part", wip_default=True,
                             with_wait_days=True, with_from_op=True,
                             with_machines=True, machine_scope_fn=self.machine_scope)
        self.bar.remember("waterfall")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(self.cascade)
        outer.addWidget(self.bar)

        # ---- view / options: one row (the design) ----
        opts = QWidget()
        of = FlowLayout(opts, 8)
        self.unit = Seg([("Hours", "hours"), ("Days", "days")], "days")
        self.unit.changed.connect(lambda _v: self.schedule())
        of.addWidget(hbox(label("Show", muted=True), self.unit, spacing=8))
        self.t_lead = Toggle("Lead time", slot=self.schedule)
        self.t_desc = Toggle("Op text", slot=self.schedule)
        self.t_rework = Toggle("+ rework", slot=self._rework_changed)
        self.t_predict = Toggle("Predicted pos.", slot=self.schedule)
        self.t_noclean = Toggle("Remove clean ops", checked=True, slot=self._clean_changed)
        self.t_dedupe = Toggle("One entry per op", checked=True, slot=self._dedupe_changed)
        for t in (self.t_lead, self.t_desc, self.t_rework, self.t_predict, self.t_noclean,
                  self.t_dedupe):
            of.addWidget(t)
        self.t_furthest = Toggle("Furthest along, top", slot=self.schedule)
        self.furthest_n = QSpinBox()
        self.furthest_n.setRange(1, 999)
        self.furthest_n.setValue(20)
        self.furthest_n.valueChanged.connect(self._furthest_changed)
        of.addWidget(hbox(self.t_furthest, self.furthest_n, spacing=6))
        # zoom of the matrix: - / + like every other page (25..100%)
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(25, 100)
        self.zoom.setValue(100)
        self.zoom.hide()
        self.zoom_lbl = label("100%")
        self.zoom.valueChanged.connect(self._zoomed)
        # v73: the zoom sits in the route card's head (one row less up here)
        self.zoom_box = hbox(label("Zoom", muted=True),
                             button("−", icon_only=True,
                                    slot=lambda: self.zoom.setValue(self.zoom.value() - 5)),
                             self.zoom_lbl,
                             button("+", icon_only=True,
                                    slot=lambda: self.zoom.setValue(self.zoom.value() + 5)),
                             spacing=6)
        outer.addWidget(opts)

        # ---- the totals: slim, in the title row (v73) - the route gets the room ----
        self.k_wip = KpiInline("IN WIP", "accent", compact=True)
        self.k_wait = KpiInline("WAITING", "wf_wait", compact=True)
        self.k_work = KpiInline("IN WORK", "wf_green", compact=True)
        self.k_lead = KpiInline("AVG LEAD", None, compact=True)
        self.info_lbl = label("All dates", muted=True)
        head.addSpacing(24)
        for k in (self.k_wip, self.k_wait, self.k_work, self.k_lead):
            head.addWidget(k)
        head.addSpacing(8)
        head.addWidget(self.info_lbl)
        head.addStretch(1)
        head.addWidget(export_button(self.do_export))
        # kept for the tests and the old caption: IN WIP and its note
        self.wip_count = self.k_wip.val
        self.wip_note = label("")

        # ---- the route ----
        self.grid = ModuleGrid()
        outer.addWidget(self.grid)
        self.mcard = ModuleCard("Route", "", on_export=self.do_export)
        self.mcard.add_extra(self.zoom_box)
        self.matrix = WaterfallMatrix()
        self.matrix.on_rework_click = self.open_rework
        self.matrix.on_ctrl_wheel = lambda d: self.zoom.setValue(self.zoom.value() + d * 5)
        self.mcard.content_lay.addWidget(self.matrix)
        self.legend_sw = []
        self.paint_legend()
        # ModuleGrid.add() hides what it is given: shown AFTER it is added
        # (2.2.0 first build showed it before - the route never appeared)
        self.grid.add(self.mcard, wide=True)
        self.mcard.show()
        self.grid.relayout()

    # ---------------------------------------------------------------
    def paint_legend(self):
        for sw, key in self.legend_sw:
            sw.setStyleSheet("background: %s; border-radius: 3px;" % ctx.TH[key])
        for k in (getattr(self, "k_wip", None), getattr(self, "k_wait", None),
                  getattr(self, "k_work", None), getattr(self, "k_lead", None)):
            if k is not None:
                k.paint()

    def _zoomed(self, v):
        v = int(round(v / 5.0) * 5)
        self.zoom_lbl.setText("%d%%" % v)
        self.schedule()

    def _rework_changed(self):
        ctx.wf_an.set_add_rework(self.t_rework.isChecked())
        self.schedule()

    def _clean_changed(self):
        ctx.wf_an.set_exclude_clean(self.t_noclean.isChecked())
        self.schedule()

    def _dedupe_changed(self):
        ctx.wf_an.set_dedupe_ops(self.t_dedupe.isChecked())
        self.schedule()

    def _furthest_changed(self, _v):
        if not self.t_furthest.isChecked():
            self.t_furthest.setChecked(True)      # schedules by itself
        else:
            self.schedule()

    def time_range(self):
        return time_mode_to_date_range(self.bar.filters(), ctx.db.week_start("waterfall"))

    def machine_scope(self):
        pk = self.bar.parts.get_selected()
        d1, d2 = self.time_range()
        return (ctx.db.wf_resolve_material_keys(pk) if pk else None, d1, d2)

    def open_rework(self, serial, op):
        from qt_page_analysis import show_rework_detail
        show_rework_detail(serial, op, self.last.get("part_disp") or "")

    def note_colors(self):
        me = ctx.username()
        if not me:
            return {}
        return ctx.db.user_note_colors(me, ctx.is_admin() and ctx.show_all_notes)

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

    def on_show(self):
        if not self.built:
            self.built = True
            ctx.wf_an.set_exclude_clean(self.t_noclean.isChecked())
            ctx.wf_an.set_dedupe_ops(self.t_dedupe.isChecked())
            self.bar.refresh_years(ctx.db.wf_years())
            self.bar._quiet += 1
            self.bar.quick_week(0)
            self.bar._quiet -= 1
            self.cascade()
        self.timer.stop()
        self.refresh(force=True)

    def reload(self):
        if self.built:
            ctx.wf_an._cache.clear()
            self.bar.refresh_years(ctx.db.wf_years())
            self.cascade()

    def schedule(self):
        if self.built:
            self.timer.start(220)

    def refresh(self, force=False):
        if not self.built:
            return
        if not force and not self.isVisible():
            return
        self.timer.stop()
        ctx.busy(True, "Building waterfall...")
        try:
            self._refresh()
        except Exception as e:
            traceback.print_exc()
            ctx.status("Waterfall error: %s" % e)
        finally:
            ctx.busy(False)

    def apply_flag_filters(self):
        ctx.wf_an.set_exclude_serial_keys(ctx.db.flag_keys(self.excluded_kinds()))
        only = {k for k, t in self.bar.only_flags.items() if t.isChecked()}
        ctx.wf_an.set_only_serial_keys(ctx.db.flag_keys(only) if only else None)

    def excluded_kinds(self):
        return {k for k, t in self.bar.exclude_flags.items() if t.isChecked()}

    def apply_time(self):
        f = self.bar.filters()
        if f["time_mode"] == "all":
            ctx.wf_an.set_date_range(None, None)
            lbl = "All dates"
        else:
            d1, d2 = self.time_range()
            ctx.wf_an.set_date_range(d1, d2)
            lbl = self.bar.time_label(f) if f["time_mode"] != "dates" else \
                "%s .. %s" % (d1 or "start", d2 or "end")
        self.last["label"] = lbl
        return lbl

    # ---------------------------------------------------------------
    def _refresh(self):
        self.apply_flag_filters()
        lbl = self.apply_time()
        z = self.zoom.value() / 100.0
        unit = self.unit.get()
        pk = self.bar.parts.get_selected()
        if not pk:
            self.matrix.set_result(None, z, unit, self.t_lead.isChecked(), self.t_desc.isChecked())
            self.wip_count.setText("-")
            self.wip_note.setText("no part selected")
            self.last["view"] = None
            self.info_lbl.setText(lbl)
            return
        # AVG LEAD (title row and the matrix's lead row) is always all-time;
        # the dated figures are in the lead time / queue / efficiency module
        all_time = ctx.wf_an.analyze_all_time(pk)
        # 2.1.1: the dated analysis only when it is shown - WIP ignores dates
        view = None if self.bar.wip.isChecked() else ctx.wf_an.analyze(pk)
        if self.bar.wip.isChecked():
            # WIP is the floor's state now - the date range does not apply
            sold = ctx.db.sold_serial_keys()
            view = dict(all_time, rows=[r for r in all_time["rows"] if r["serial_key"] not in sold])
        info = []
        if self.t_predict.isChecked():
            view = wf_predicted_view(view, ctx.db, pk, exclude_clean=self.t_noclean.isChecked(),
                                     dedupe_ops=self.t_dedupe.isChecked(),
                                     cutoff=datetime.datetime.now(), flag_map=ctx.db.flag_map())
            moved = sum(1 for r in view["rows"] if r.get("predicted_from_pos") is not None)
            info.append("%d of %d moved forward  (lead time)" % (moved, len(view["rows"])))
        self.bar.set_route_ops(view["route"])
        view, from_txt = wf_from_op_view(view, self.bar.from_op_step())
        if from_txt:
            info.append(from_txt)
        wanted = self.bar.get_serials()
        d1, d2 = self.time_range()
        from_m = self.bar.machine_serials(d1, d2, part_keys=ctx.db.wf_resolve_material_keys(pk))
        if from_m is not None:
            wanted = (wanted & from_m) if wanted else from_m
        if wanted:
            view = dict(view, rows=[r for r in view["rows"] if r["serial_key"] in wanted])
        elif from_m is not None:
            view = dict(view, rows=[])
        wait_n = self.bar.wait_days()
        if wait_n:
            limit = wait_n * 24.0
            view = dict(view, rows=[r for r in view["rows"] if (r["waiting_hours"] or 0.0) > limit])
        if self.t_furthest.isChecked():
            n = self.furthest_n.value()

            def along(r):
                p = r.get("predicted_pos")
                if p is None:
                    p = r.get("here_pos")
                if p is None:
                    p = r.get("last_done_pos", -1)
                return ((-1 if p is None else p),
                        r["last_move_dt"].timestamp() if r.get("last_move_dt") else 0.0)
            # parts still at an operation first: a finished one is not "nearly out"
            going = sorted([r for r in view["rows"] if not r.get("all_done")], key=along,
                           reverse=True)
            done = sorted([r for r in view["rows"] if r.get("all_done")], key=along, reverse=True)
            view = dict(view, rows=(going + done)[:n])
            info.append("furthest %d along the route" % len(view["rows"]))
        self.info_lbl.setText("   |   ".join([lbl] + info))

        keep = self.last.get("part") == pk
        view = dict(view, averages=all_time["averages"])
        self.matrix.set_result(view, z, unit, self.t_lead.isChecked(), self.t_desc.isChecked(),
                               flag_map=ctx.db.flag_map(), note_colors=self.note_colors(),
                               keep_scroll=keep)
        self.last.update(view=view, part=pk, part_disp=self.bar.parts.label_of(pk))
        sold_now = ctx.db.sold_serial_keys()
        in_wip = sum(1 for r in view["rows"] if r["serial_key"] not in sold_now)
        self.wip_count.setText(str(in_wip))
        self.wip_note.setText("of %d shown%s" % (len(view["rows"]), "" if self.bar.wip.isChecked()
                                                 else "  (WIP filter off)"))
        live = [r for r in view["rows"] if not r.get("all_done")]
        self.k_work.set(sum(1 for r in live if r.get("running") is not None))
        self.k_wait.set(sum(1 for r in live if r.get("running") is None))
        avg = all_time.get("total_avg_hours")
        self.k_lead.set(("%s %s" % (format_wf_hours(avg, unit), "d" if unit == "days" else "h"))
                        if avg is not None else "-")
        self.mcard.title_lbl.setText("Route · %s" % self.last["part_disp"])
        self.mcard.set_sub("%d serials%s" % (len(view["rows"]), "" if self.bar.wip.isChecked()
                                             else " · WIP filter off"))
        ctx.status("%s   |   %s   |   %d serials, %d operations"
                   % (self.last["part_disp"], lbl, len(view["rows"]), len(view["route"])))

    # ---------------------------------------------------------------
    def do_export(self):
        pk = self.bar.parts.get_selected()
        if not pk:
            QMessageBox.warning(self, "Nothing to export", "Pick a part first.")
            return
        # exactly what the matrix shows (WIP / predicted / serial filters)
        result = self.last["view"] if self.last["part"] == pk else ctx.wf_an.analyze(pk)
        if not result or not result["route"]:
            QMessageBox.warning(self, "Nothing to export", "This part has no traveler route.")
            return
        disp = self.last["part_disp"] or pk
        path, _x = QFileDialog.getSaveFileName(
            self, "Export", "Waterfall_%s.xlsx" % re.sub(r"[^A-Za-z0-9]+", "_", str(disp))[:32],
            "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_waterfall_to_excel(result, path, disp, self.unit.get(), self.last["label"])
        except Exception as e:
            QMessageBox.critical(self, "Export failed", "%s\n\n%s" % (e, traceback.format_exc()))
            return
        ctx.status("Exported: " + path)

    def refresh_theme(self):
        self.paint_legend()
        self.matrix.viewport().update()

    def annotations_changed(self):
        if self.built:
            self.schedule()

    def notes_visibility_changed(self):
        self.annotations_changed()

# -*- coding: utf-8 -*-
"""RW ANALYSIS - the Qt page (new design).

Same as the Tk page (page_analysis.py), with CROSS CHART taken off at the
user's request:
  * modules PER RAW DATA / PER SERIAL / ALL PARTS REPORT, opened side by
    side; each can be opened in its own window
  * PER RAW DATA: one box per serial with a TOTAL line; right-click a
    column heading to group by it or filter on it
  * metrics: PART COUNT, TOTAL RW HOUR, FTY; rows removed at import
  * the filters are remembered between sessions
"""

import re
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QComboBox, QMessageBox, QFileDialog, QMenu,
                               QDialog, QLabel)

from wip_core import (ALL, APP_TITLE, UNKNOWN, enrich, export_to_excel,
                      fmt_hour, iso_year_week, lookups, norm_text, parse_date,
                      run_analysis_cached, save_prefs, serial_history, totals_by_part,
                      week_offset, allowed_part_keys, time_mode_to_date_range)
from qt_widgets import (ctx, ModuleCard, DataTable, FilterBar, Toggle, Seg, label, button, hbox,
                        MultiSelect, ZoomControl, MetricBox, autofit_columns, ModuleGrid,
                        page_layout, page_head, export_button, FlowLayout, note_ink)

BASE_COLUMNS = [
    ("SERIAL NO", 110), ("REJECT OP", 85), ("REJECT OP DESCRIPTION", 205),
    ("CAUSE OP", 85), ("CAUSE OP DESCRIPTION", 195), ("RW HOUR", 80),
    ("WORK CENTRE", 100), ("WC DESCRIPTION", 180),
    ("RW EMPLOYEE NO", 110), ("RW EMPLOYEE NAME", 175),
    ("RW DATE", 100), ("FW WEEK", 80),
    ("CAUSE OP WORK CTR", 120), ("CAUSE OP MACHINE", 190),
    ("CAUSE OP EMPLOYEE NO", 130), ("CAUSE OP EMPLOYEE NAME", 185),
]
N_TAIL = 4
PART_COLUMN = ("PART NO", 120)
GROUP_FIELDS = {
    "PART NO": "part_no", "SERIAL NO": "serial_key", "REJECT OP": "reject_op",
    "REJECT OP DESCRIPTION": "reject_op_desc", "CAUSE OP": "cause_op",
    "CAUSE OP DESCRIPTION": "cause_op_desc", "WORK CENTRE": "wc_code",
    "WC DESCRIPTION": "wc_name", "FW WEEK": "iso_week", "CAUSE OP WORK CTR": "cause_wc",
    "CAUSE OP MACHINE": "cause_machine", "CAUSE OP EMPLOYEE NO": "cause_emp_no",
    "CAUSE OP EMPLOYEE NAME": "cause_emp_name", "RW EMPLOYEE NO": "emp_no",
    "RW EMPLOYEE NAME": "emp_name", "RW DATE": "rw_date",
}
GROUP_DEFAULT = "SERIAL NO"
# v69 (RW D1): the "Group by" bar - heading, and the columns merged into one
# box cell for that grouping (the group's value and its description)
GROUP_BAR = [("Serial no", "SERIAL NO"), ("Reject op", "REJECT OP"), ("Cause op", "CAUSE OP"),
             ("Cause op machine", "CAUSE OP MACHINE"), ("Cause op employee", "CAUSE OP EMPLOYEE NAME"),
             ("Work centre", "WORK CENTRE"), ("FW week", "FW WEEK")]
GROUP_MERGE = {"SERIAL NO": ("PART NO", "SERIAL NO"), "REJECT OP": ("REJECT OP", "REJECT OP DESCRIPTION"),
               "CAUSE OP": ("CAUSE OP", "CAUSE OP DESCRIPTION"),
               "CAUSE OP MACHINE": ("CAUSE OP WORK CTR", "CAUSE OP MACHINE"),
               "CAUSE OP EMPLOYEE NAME": ("CAUSE OP EMPLOYEE NO", "CAUSE OP EMPLOYEE NAME"),
               "WORK CENTRE": ("WORK CENTRE", "WC DESCRIPTION"), "FW WEEK": ("FW WEEK",)}
ARROW_COL = (" ", 24)
ALLPARTS_COLS = [("ENGINEER", 110), ("GROUP", 80), ("PART NO", 120),
                 ("REJECTED PARTS", 100), ("RW HOUR", 90), ("ENTRIES", 70)]


def part_tag(part_no):
    if isinstance(part_no, (list, set, tuple)):
        parts = sorted(part_no)
        if not parts:
            return ALL
        return parts[0] if len(parts) == 1 else "%d parts" % len(parts)
    return part_no


def raw_layout(show_part_first):
    if show_part_first:
        return [PART_COLUMN] + BASE_COLUMNS, 2, [0, 1]
    return BASE_COLUMNS, 1, [0]


def raw_row(rec, show_part_first):
    base = [rec["serial_no"], rec["reject_op"], rec["reject_op_desc"], rec["cause_op"],
            rec["cause_op_desc"], fmt_hour(rec["rw_hour"]), rec["wc_code"], rec["wc_name"],
            rec.get("emp_no") or UNKNOWN, rec.get("emp_name") or UNKNOWN,
            rec["rw_date"], rec["iso_week"], rec.get("cause_wc", UNKNOWN),
            rec.get("cause_machine", UNKNOWN), rec.get("cause_emp_no", UNKNOWN),
            rec.get("cause_emp_name", UNKNOWN)]
    return ([rec["part_no"]] + base) if show_part_first else base


def hour_col_index(columns):
    for i, c in enumerate(columns):
        if str(c[0]).split("\n")[0].strip() == "RW HOUR":
            return i
    return max(0, len(columns) - 5 - N_TAIL)


def blank_row(n, hour_index, text, hours):
    row = [""] * n
    row[hour_index - 1] = text
    row[hour_index] = hours
    return row


class TableDialog(QDialog):
    def __init__(self, parent, title, note, cols, rows, bgs=None, bolds=None, groups=None,
                 frozen=0, size=(1180, 440)):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        lay = QVBoxLayout(self)
        lay.addWidget(label(note, muted=True, wrap=True))
        self.table = DataTable()
        self.table.set_data(autofit_columns(cols, rows), rows, bgs, bolds, groups or [],
                            frozen=frozen)
        lay.addWidget(self.table, 1)


def show_rework_detail(serial, op, part_disp=""):
    """The rework entries of one serial at one operation (opened from a
    waterfall cell)."""
    rows = ctx.db.rework_rows_for(serial, op)
    if not rows:
        QMessageBox.information(ctx.window, "No rework record",
                                "No rework entry is stored for serial %s at operation %s."
                                % (serial, op))
        return
    index, comp, opdesc = lookups(ctx.db, ctx.db.merge_on("analysis"))
    recs = enrich(rows, index, comp, opdesc, ctx.db.cause_op_index({norm_text(serial)}))
    cols, frozen, _m = raw_layout(True)
    data = [raw_row(r, True) for r in recs]
    data.append(blank_row(len(cols), hour_col_index(cols), "TOTAL  (%d entries)" % len(recs),
                          fmt_hour(sum(r["rw_hour"] for r in recs))))
    TableDialog(ctx.window, "%s - rework %s / op %s" % (APP_TITLE, serial, op),
                "%s   serial %s   operation %s   -   %d rework entry(ies), %s hour(s) in total"
                % (part_disp or recs[0]["part_no"], serial, op, len(recs),
                   fmt_hour(sum(r["rw_hour"] for r in recs))),
                cols, data, ["a"] * len(recs) + ["sub"], [False] * len(recs) + [True],
                [(0, len(recs) - 1)] if recs else [], frozen).exec()


ctx.hooks["show_rework_detail"] = show_rework_detail


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.current = {"result": None, "serial": None}
        self.group_by = GROUP_DEFAULT
        self.col_filters = {}
        self.sel_reject_ops, self.sel_cause_ops = [], []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        outer = page_layout(self)
        page_head(outer, "RW Analysis", "Rework records and per-serial history",
                  export_button(self.do_export))

        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("analysis"),
                             part_multi=True, with_machines=True,
                             machine_scope_fn=self.machine_scope, wip_label="By WIP part")
        self.bar.remember("analysis")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(self.cascade)
        outer.addWidget(self.bar)

        # the totals are read in the Per raw data card's head (the design)
        self.m_parts = MetricBox("PART COUNT")
        self.m_hours = MetricBox("TOTAL RW HOUR")
        self.m_fty = MetricBox("FTY")
        self.removed_btn = button("", slot=self.show_removed)
        self.removed_btn.setProperty("flat", True)
        self.removed_btn.hide()

        # wraps on a narrow window instead of pushing the page sideways
        trow_w = QWidget()
        trow = FlowLayout(trow_w, 8)
        # v69 (RW D1): the totals as boxes in front of the MODULES switches
        for m in (self.m_parts, self.m_hours, self.m_fty):
            m.setMinimumWidth(130)
            trow.addWidget(m)
        trow.addWidget(label("MODULES", section=True))
        self.t_raw = Toggle("Per raw data", checked=True, slot=self.rebuild_panels)
        self.t_serial = Toggle("Per serial", slot=self.rebuild_panels)
        self.t_all = Toggle("All parts report", slot=self.rebuild_panels)
        for t in (self.t_raw, self.t_serial, self.t_all):
            trow.addWidget(t)
        trow.addWidget(self.removed_btn)
        self.zoom = ZoomControl(lambda: [self.raw_table, self.serial_table, self.all_table])
        trow.addWidget(self.zoom)
        outer.addWidget(trow_w)

        self.grid = ModuleGrid()
        outer.addWidget(self.grid)
        # PER RAW DATA
        self.raw = ModuleCard("Per raw data", "one box per serial number", on_export=self.do_export)
        self.group_lbl = label("grouped by SERIAL NO", obj="CardTitle")
        self.group_lbl.hide()
        self.colf_lbl = label("", wrap=True)
        self.open_all, self.closed, self.opened = True, set(), set()
        self.exp_btn = button("▴  Collapse all", slot=self.toggle_all)
        self.exp_btn.setProperty("primary", False)
        self.group_seg = Seg([(t, k) for t, k in GROUP_BAR], GROUP_DEFAULT)
        self.group_seg.changed.connect(lambda v: self.set_group(v))
        self.t_fit = Toggle("Fit to width", checked=bool(ctx.prefs.get("rw_fit_width")),
                            slot=self.fit_changed)
        self.t_fit.setToolTip("On: every column on screen, long texts shortened.  "
                              "Off: full texts, scroll sideways.")
        tb = QWidget()
        tbl = FlowLayout(tb, 8)
        for w in (self.exp_btn, hbox(label("Group by", muted=True), self.group_seg, spacing=8),
                  self.t_fit, label("click a heading to filter", muted=True)):
            tbl.addWidget(w)
        self.raw.content_lay.addWidget(tb)
        self.raw.content_lay.addWidget(self.colf_lbl)
        self.colf_lbl.hide()
        self.raw_table = DataTable()
        self.raw_table.on_head_menu = self.head_menu
        self.raw_table.on_head_click = self.head_click
        self.raw_table.on_cell_click = self.cell_click
        self.raw_table.fit_rows(18)
        self.raw_table.stripes = True
        self.raw_table.fit_width = bool(ctx.prefs.get("rw_fit_width"))
        self.raw_table.grid = True
        self.raw.content_lay.addWidget(self.raw_table, 1)
        self.grand_lbl = QLabel("")
        self.grand_lbl.setObjectName("GrandTotal")
        self.raw.content_lay.addWidget(self.grand_lbl)
        self.box_rows = {}
        # PER SERIAL
        self.ser = ModuleCard("Per serial", "full history")
        self.serial_cb = QComboBox()
        self.serial_cb.setEditable(True)
        self.serial_cb.setMinimumWidth(200)
        self.serial_cb.lineEdit().setPlaceholderText("pick or type a serial")
        self.serial_cb.lineEdit().editingFinished.connect(self.schedule)
        self.serial_cb.activated.connect(lambda _i: self.schedule())
        self.ser.content_lay.addWidget(hbox(label("Serial", muted=True), self.serial_cb, None))
        self.serial_table = DataTable()
        self.serial_table.serial_col = 0
        self.serial_table.fit_rows(12)
        self.ser.content_lay.addWidget(self.serial_table, 1)
        # ALL PARTS
        self.allp = ModuleCard("All parts report",
                               "every part in this time frame, filters above not applied")
        self.all_table = DataTable()
        self.all_table.fit_rows(16)
        self.allp.content_lay.addWidget(self.all_table, 1)
        self.grid.add(self.raw, wide=True)
        self.grid.add(self.ser)
        self.grid.add(self.allp)
        for t, c in ((self.t_raw, self.raw), (self.t_serial, self.ser), (self.t_all, self.allp)):
            c.setVisible(t.isChecked())
        self.grid.relayout()

    # ---------------------------------------------------------------
    def machine_scope(self):
        f = self.current_filters(apply_machines=False)
        d1, d2 = time_mode_to_date_range(f, ctx.db.week_start("analysis"))
        return (allowed_part_keys(ctx.db, f.get("engineer"), f.get("part_group"),
                                  f.get("part_no"), f.get("merge")), d1, d2)

    def cascade(self):
        parts = ctx.db.all_parts(ctx.db.merge_on("analysis"))
        eng = self.bar.engineer()
        groups = sorted({p["part_group"] for p in parts if p["part_group"] and
                         (eng == ALL or (p["engineer"] or "").upper() == eng.upper())})
        grp = self.bar.set_choices(self.bar.grp, groups)
        pnos = sorted({p["part_no"] for p in parts
                       if (eng == ALL or (p["engineer"] or "").upper() == eng.upper())
                       and (grp == ALL or p["part_group"] == grp)})
        self.bar.parts.rebuild(pnos)
        self.schedule()

    def refresh_filter_sources(self):
        self.bar.set_choices(self.bar.eng, ctx.db.engineers())
        self.bar.refresh_years(ctx.db.rw_years())
        cur = self.serial_cb.currentText()
        self.serial_cb.blockSignals(True)
        self.serial_cb.clear()
        self.serial_cb.addItems(ctx.db.serials()[:5000])
        self.serial_cb.setEditText(cur)
        self.serial_cb.blockSignals(False)
        self.cascade()

    def cascade_quiet(self):
        parts = ctx.db.all_parts(ctx.db.merge_on("analysis"))
        eng = self.bar.engineer()
        groups = sorted({p["part_group"] for p in parts if p["part_group"] and
                         (eng == ALL or (p["engineer"] or "").upper() == eng.upper())})
        self.bar.set_choices(self.bar.grp, groups)
        grp = self.bar.group()
        self.bar.parts.rebuild(sorted({p["part_no"] for p in parts
                                       if (eng == ALL or (p["engineer"] or "").upper() == eng.upper())
                                       and (grp == ALL or p["part_group"] == grp)}))

    def current_filters(self, apply_machines=True):
        b = self.bar
        f = dict(b.filters())
        f.update({"engineer": b.engineer(), "part_group": b.group(),
                  "part_no": b.parts.get_selected(),
                  "merge": ctx.db.merge_on("analysis"),
                  "wk_day": ctx.db.week_start("analysis"),
                  "reject_ops": list(self.sel_reject_ops), "cause_ops": list(self.sel_cause_ops),
                  "wip_only": bool(b.wip and b.wip.isChecked()),
                  "serials": sorted(b.get_serials()),
                  "exclude_flag_kinds": [k for k, t in b.exclude_flags.items() if t.isChecked()],
                  "only_flag_kinds": [k for k, t in b.only_flags.items() if t.isChecked()],
                  "need_cause_details": bool(self.t_raw.isChecked() or self.raw.detached)})
        m = f["time_mode"]
        yr = f.get("year") or "all years"
        f["time_label"] = {"week": "FW week %s / %s" % (f.get("week"), yr),
                           "weeks": "FW weeks %s - %s / %s" % (f.get("week_start"), f.get("week_end"), yr),
                           "month": "%s %s" % (b.month.currentText(), f.get("year") or "?"),
                           "dates": "%s ... %s" % (f.get("date_start") or "start", f.get("date_end") or "end"),
                           }.get(m, "all time")
        if apply_machines:
            d1, d2 = time_mode_to_date_range(f, ctx.db.week_start("analysis"))
            scope = allowed_part_keys(ctx.db, f.get("engineer"), f.get("part_group"),
                                      f.get("part_no"), f.get("merge"))
            from_m = b.machine_serials(d1, d2, part_keys=scope)
            if from_m is not None:
                chosen = b.get_serials()
                f["serials"] = sorted((chosen & from_m) if chosen else from_m)
        return f

    # ---------------------------------------------------------------
    def rebuild_panels(self):
        for t, c in ((self.t_raw, self.raw), (self.t_serial, self.ser), (self.t_all, self.allp)):
            c.setVisible(t.isChecked() or c.detached)
        self.grid.relayout()
        self.refresh()

    def on_show(self):
        if not self.built:
            self.built = True
            self.bar.set_choices(self.bar.eng, ctx.db.engineers())
            self.bar.refresh_years(ctx.db.rw_years())
            self.cascade_quiet()
            # v73: every start opens on this week and the account's own
            # engineer - the last session's filters are not brought back
            self.bar.quick_week(0)
            self.timer.stop()
            for t, c in ((self.t_raw, self.raw), (self.t_serial, self.ser), (self.t_all, self.allp)):
                c.setVisible(t.isChecked())
            self.serial_cb.addItems(ctx.db.serials()[:5000])
            self.serial_cb.setEditText("")
        self.refresh(force=True)

    def reload(self):
        if self.built:
            self.refresh_filter_sources()

    def annotations_changed(self):
        if self.built and self.isVisible():
            self.refresh()

    def notes_visibility_changed(self):
        self.annotations_changed()

    def schedule(self):
        if self.built:
            self.timer.start(220)

    # ---------------------------------------------------------------
    def regroup(self, res):
        boxes = self.apply_col_filters(res.get("groups") or [])
        if self.group_by == GROUP_DEFAULT:
            return boxes
        field = GROUP_FIELDS.get(self.group_by)
        if not field:
            return boxes
        out = {}
        for _k, recs in boxes:
            for rec in recs:
                v = rec.get(field)
                out.setdefault("" if v is None else str(v), []).append(rec)
        return sorted(out.items(), key=lambda kv: (-sum(r["rw_hour"] for r in kv[1]), kv[0]))

    def apply_col_filters(self, boxes):
        if not self.col_filters:
            return boxes
        sel = [(GROUP_FIELDS[a], v) for a, v in self.col_filters.items() if a in GROUP_FIELDS]
        out = []
        for key, recs in boxes:
            keep = [r for r in recs
                    if all(("" if r.get(f) is None else str(r.get(f))) in ok for f, ok in sel)]
            if keep:
                out.append((key, keep))
        return out

    def column_values(self, heading):
        field = GROUP_FIELDS.get(heading)
        res = self.current.get("result")
        if not field or not res:
            return {}
        count = {}
        for _k, recs in (res.get("groups") or []):
            for rec in recs:
                v = rec.get(field)
                v = "" if v is None else str(v)
                count[v] = count.get(v, 0) + 1
        return count

    def head_menu(self, ci, gpos):
        cols = self.raw_table.columns
        if ci < 0 or ci >= len(cols):
            return
        heading = str(cols[ci][0]).split("\n")[0].strip()
        m = QMenu(self)
        a = m.addAction(heading); a.setEnabled(False)
        m.addSeparator()
        if heading in GROUP_FIELDS:
            m.addAction("Group by  %s" % heading, lambda: self.set_group(heading))
            sel = self.col_filters.get(heading)
            m.addAction(("Filter  %s ...  (%d selected)" % (heading, len(sel))) if sel
                        else "Filter  %s ..." % heading, lambda: self.open_col_filter(heading))
            if sel:
                m.addAction("Clear filter on %s" % heading, lambda: self.clear_col_filter(heading))
        else:
            b = m.addAction("This column cannot group or filter"); b.setEnabled(False)
        if self.group_by != GROUP_DEFAULT or self.col_filters:
            m.addSeparator()
        if self.group_by != GROUP_DEFAULT:
            m.addAction("Grouping: back to %s (default)" % GROUP_DEFAULT,
                        lambda: self.set_group(GROUP_DEFAULT))
        if self.col_filters:
            m.addAction("Clear ALL column filters (%d)" % len(self.col_filters),
                        self.clear_all_col_filters)
        m.exec(gpos)

    def set_group(self, col):
        if self.group_by == col:
            return
        self.group_by = col
        self.group_lbl.setText("grouped by %s" % col)
        if hasattr(self, "group_seg") and self.group_seg.get() != col and col in dict((k, 1) for _t, k in GROUP_BAR):
            self.group_seg.blockSignals(True)
            self.group_seg.set(col)
            self.group_seg.blockSignals(False)
        self.open_all, self.closed, self.opened = True, set(), set()
        self.refresh(force=True)

    def fit_changed(self):
        """Fit to width on/off (v69): off shows every text in full and the
        table scrolls sideways with its first columns held still."""
        on = self.t_fit.isChecked()
        ctx.prefs["rw_fit_width"] = on
        save_prefs(ctx.prefs)
        self.raw_table.fit_width = on
        self.refresh(force=True)

    def toggle_all(self):
        """Expand all / collapse all (v69).  With everything closed, a click
        on one box opens just that one."""
        if self.open_all and not self.closed:
            self.open_all, self.closed, self.opened = False, set(), set()
        else:
            self.open_all, self.closed, self.opened = True, set(), set()
        self.refresh(force=True)

    def is_open(self, key):
        return (key not in self.closed) if self.open_all else (key in self.opened)

    def cell_click(self, r, c):
        key = self.box_rows.get(r)
        if key is None:
            return
        # v71: only the arrow at the far left opens / closes a box - a click
        # on the part or serial (to read or select it) must not fold it
        if c != 0:
            return
        if self.open_all:
            self.closed.symmetric_difference_update({key})
        else:
            self.opened = set() if key in self.opened else {key}
        self.refresh(force=True)

    def head_click(self, ci):
        cols = self.raw_table.columns
        if 0 <= ci < len(cols):
            heading = str(cols[ci][0]).split("\n")[0].strip()
            if heading in GROUP_FIELDS:
                self.open_col_filter(heading)

    def open_col_filter(self, heading):
        count = self.column_values(heading)
        if not count:
            QMessageBox.information(self, "Filter", "Nothing to filter on in %s." % heading)
            return
        items = [(v, "%d row(s)" % n) for v, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))]

        def apply(chosen):
            if chosen and len(chosen) < len(items):
                self.col_filters[heading] = set(chosen)
            else:
                self.col_filters.pop(heading, None)
            self.paint_col_filters()
            self.refresh(force=True)
        MultiSelect(self, "Filter - %s" % heading, items,
                    sorted(self.col_filters.get(heading) or ()), apply).exec()

    def clear_col_filter(self, heading):
        self.col_filters.pop(heading, None)
        self.paint_col_filters()
        self.refresh(force=True)

    def clear_all_col_filters(self):
        self.col_filters.clear()
        self.paint_col_filters()
        self.refresh(force=True)

    def paint_col_filters(self):
        if not self.col_filters:
            self.colf_lbl.hide()
            return
        parts = ["%s: %d" % (a, len(v)) for a, v in sorted(self.col_filters.items())]
        self.colf_lbl.setText("COLUMN FILTERS (this table only) - %s   [right-click a heading to clear]"
                              % ",  ".join(parts))
        self.colf_lbl.setStyleSheet("color: %s; font-weight: 600;" % ctx.TH["danger"])
        self.colf_lbl.show()

    def apply_page_week(self, res):
        ws = ctx.db.week_start("analysis")
        if ws == ctx.db.week_start():
            return
        off = week_offset(ws)
        for _k, recs in (res.get("groups") or []):
            for rec in recs:
                d = parse_date(rec.get("rw_date"))
                if d is not None:
                    rec["iso_year"], rec["iso_week"] = iso_year_week(d, off)

    # ---------------------------------------------------------------
    def refresh(self, force=False):
        if not self.built:
            return
        if not force and not self.isVisible() and not any(c.detached for c in (self.raw, self.ser, self.allp)):
            return
        self.timer.stop()
        ctx.busy(True, "Analysing rework data...")
        try:
            self._refresh()
        except Exception as e:
            traceback.print_exc()
            ctx.status("Analysis error: %s" % e)
        finally:
            ctx.busy(False)

    def _refresh(self):
        if ctx.db.counts()["rw"] == 0:
            for t in (self.raw_table, self.serial_table, self.all_table):
                t.clear()
            for m in (self.m_parts, self.m_hours, self.m_fty):
                m.set("0")
            ctx.status("No RW data loaded yet - go to DATA & SETTINGS.")
            return
        f = self.current_filters()
        res = run_analysis_cached(ctx.db, f)
        self.current["result"] = res
        self.apply_page_week(res)
        self.m_parts.set(res["part_count"])
        self.m_hours.set(fmt_hour(res["rw_hour"]))
        self.m_fty.set(("%.1f%%" % res["fty"]) if res["fty"] is not None else "-")
        self.raw.set_sub("%s parts · %s h · FTY %s" % (res["part_count"], fmt_hour(res["rw_hour"]),
                                                      self.m_fty.val.text()))
        if self.t_all.isChecked() or self.allp.detached:
            self.refresh_all_parts(f)
        show_part_first = not isinstance(f["part_no"], str) or f["part_no"] == ALL
        columns, frozen, merge = raw_layout(show_part_first)
        if self.t_raw.isChecked() or self.raw.detached:
            self.fill_raw(res, columns, show_part_first)
        if self.t_serial.isChecked() or self.ser.detached:
            sn = self.serial_cb.currentText().strip()
            if sn:
                recs = serial_history(ctx.db, sn)
                self.current["serial"] = (sn, recs)
                # v69 (RW D1): the columns that tell one serial's story
                scols = [("REJECT OP", 70), ("REJECT OP DESCRIPTION", 160), ("CAUSE OP", 70),
                         ("CAUSE OP DESCRIPTION", 160), ("RW HOUR", 70), ("CAUSE OP MACHINE", 130),
                         ("CAUSE OP EMPLOYEE", 130), ("RW EMPLOYEE", 120), ("RW DATE", 90)]
                rows = [[r["reject_op"], r["reject_op_desc"], r["cause_op"], r["cause_op_desc"],
                         fmt_hour(r["rw_hour"]), r.get("cause_machine", UNKNOWN),
                         r.get("cause_emp_name", UNKNOWN), r.get("emp_name") or UNKNOWN, r["rw_date"]]
                        for r in recs]
                bgs, bolds = ["a"] * len(rows), [False] * len(rows)
                spans, fgs = {}, {}
                if recs:
                    rows.append(["TOTAL  ·  %d entries" % len(recs), "", "", "",
                                 "%s h" % fmt_hour(sum(r["rw_hour"] for r in recs)), "", "", "", ""])
                    bgs.append("sub")
                    bolds.append(True)
                    spans[(len(rows) - 1, 0)] = 4
                    fgs[(len(rows) - 1, 4)] = ctx.TH["accent"]
                self.serial_table.serial_col = -1
                self.serial_table.fit_width = True
                self.serial_table.grid = True
                al = ["w"] * 9
                al[4] = "e"
                self.serial_table.set_data(autofit_columns(scols, rows), rows, bgs, bolds,
                                           cell_span=spans, cell_fg=fgs, align=al)
                fk = ctx.db.flag_map().get(norm_text(sn))
                self.ser.set_sub(("full history · %s" % fk.upper()) if fk else "full history")
            else:
                self.current["serial"] = None
                self.serial_table.set_data([("SERIAL NO", 340)], [["Pick or type a serial number above"]])
        self.zoom.apply_zoom()
        who = part_tag(f["part_no"]) if f["part_no"] != ALL else (
            f["part_group"] if f["part_group"] != ALL else f["engineer"])
        msg = "%s  |  %s  |  PART COUNT %d  |  RW HOUR %s" % (who, f["time_label"], res["part_count"],
                                                             fmt_hour(res["rw_hour"]))
        if f.get("wip_only"):
            msg += "  |  BY WIP PART"
        if res["unknown_work_centers"]:
            msg += "  |  work centres missing from COMP: %s" % ", ".join(res["unknown_work_centers"][:6])
        n_removed = ctx.db.discarded_count()
        if n_removed:
            self.removed_btn.setText("Removed rows: %d" % n_removed)
            self.removed_btn.show()
            msg += "  |  %d row(s) removed at import" % n_removed
        else:
            self.removed_btn.hide()
        ctx.status(msg)

    def fill_raw(self, res, columns, show_part_first):
        """PER RAW DATA (v69, design RW D1): every column shares the width;
        one box per group with the group's columns merged into one cell, a
        TOTAL row under every open box, closed boxes show their latest entry
        with the box's total hours; GRAND TOTAL under the table."""
        cols = [ARROW_COL] + list(columns)
        names = [str(c[0]) for c in cols]
        ncol = len(cols)
        hidx = names.index("RW HOUR")
        merge_names = GROUP_MERGE.get(self.group_by, (self.group_by,))
        merge = [names.index(n) for n in merge_names if n in names]
        self._merge_cols = merge
        sidx = names.index("SERIAL NO")
        self.raw_table.serial_col = sidx
        flags = ctx.db.flag_map()
        notes = (ctx.db.user_note_colors(ctx.username(), ctx.is_admin() and ctx.show_all_notes)
                 if ctx.username() else {})
        rows, bgs, bolds, groups, spans, fgs, tips = [], [], [], [], {}, {}, {}
        self.box_rows = {}
        n_ser, n_ent, hours = set(), 0, 0.0
        for gi, (key, recs) in enumerate(self.regroup(res)):
            key = str(key)
            recs = sorted(recs, key=lambda x: (str(x.get("rw_date") or ""), str(x.get("reject_op") or "")))
            tot_h = sum(x["rw_hour"] for x in recs)
            n_ent += len(recs)
            hours += tot_h
            for x in recs:
                n_ser.add(x.get("serial_key") or norm_text(x["serial_no"]))
            open_ = self.is_open(key)
            start = len(rows)
            shown = recs if open_ else recs[-1:]
            for x in shown:
                rk = x.get("serial_key") or norm_text(x["serial_no"])
                row = [""] + raw_row(x, show_part_first)
                if not open_:
                    row[hidx] = "%s h" % fmt_hour(tot_h)
                rows.append(row)
                k2 = flags.get(rk)
                bgs.append(k2 or notes.get(rk) or ("a" if gi % 2 == 0 else "b"))
                bolds.append(False)
                if k2:
                    fgs[(len(rows) - 1, sidx)] = ctx.flag_text(k2)
                elif notes.get(rk):
                    fgs[(len(rows) - 1, sidx)] = note_ink(notes[rk])
                self.box_rows[len(rows) - 1] = key
                if not open_ and len(recs) > 1:
                    tips[(len(rows) - 1, names.index("RW DATE"))] = "latest of %d entries" % len(recs)
                    tips[(len(rows) - 1, hidx)] = "total of %d entries (latest %s h)" % (
                        len(recs), fmt_hour(x["rw_hour"]))
            rows[start][0] = ("▾" if open_ else "▸")
            groups.append((start, len(rows) - 1))
            for ci in merge:
                fgs.setdefault((start, ci), ctx.TH["text"])
            if open_:
                tr = [""] * ncol
                first = 1
                if self.group_by != GROUP_DEFAULT and sidx + 1 < hidx:
                    # v70: grouped by anything but the serial - the box counts
                    # its distinct serials, under the SERIAL NO column
                    n_s = len({x.get("serial_key") or norm_text(x["serial_no"]) for x in recs})
                    tr[sidx] = "%d serial%s" % (n_s, "" if n_s == 1 else "s")
                    fgs[(len(rows), sidx)] = ctx.TH["accent"]
                    first = sidx + 1
                    tr[first] = "TOTAL  %s" % key
                else:
                    tr[first] = "TOTAL  %s  ·  %d %s" % (key, len(recs),
                                                       "entry" if len(recs) == 1 else "entries")
                tr[hidx] = "%s h" % fmt_hour(tot_h)
                rows.append(tr)
                bgs.append("sub")
                bolds.append(True)
                spans[(len(rows) - 1, first)] = hidx - first
                fgs[(len(rows) - 1, hidx)] = ctx.TH["accent"]
                self.box_rows[len(rows) - 1] = None
        self.box_rows = {r: k for r, k in self.box_rows.items() if k is not None}
        # v70: serial boxes get a firmer line between them
        self.raw_table.group_line = 2 if self.group_by == GROUP_DEFAULT else 1
        self.raw.set_sub("%d boxes · %d rows · grouped by %s" % (len(groups), n_ent, self.group_by))
        self.exp_btn.setText("▴  Collapse all" if (self.open_all and not self.closed) else "▾  Expand all")
        align = ["center"] + ["w"] * (ncol - 1)
        align[hidx] = "e"
        self.raw_table.set_data(autofit_columns(cols, rows), rows, bgs, bolds, groups,
                                merge_cols=[0] + merge,
                                frozen=0 if self.raw_table.fit_width else sidx + 1, cell_span=spans,
                                cell_fg=fgs, align=align)
        self.raw_table.cell_tip = tips
        flt = (" · filtered on %s" % ", ".join("%s (%d)" % (a, len(v)) for a, v in sorted(self.col_filters.items()))
               if self.col_filters else " · every row that passes the filters")
        self.grand_lbl.setText("<b>GRAND TOTAL</b><span style='color:%s'>%s</span>"
                               "&nbsp;&nbsp;&nbsp;&nbsp; Serials <b>%d</b> &nbsp;&nbsp; Entries <b>%d</b>"
                               " &nbsp;&nbsp; RW hour <b>%s</b>"
                               % (ctx.TH["muted"], flt, len(n_ser), n_ent, fmt_hour(hours)))

    def refresh_all_parts(self, f):
        try:
            rows = totals_by_part(ctx.db, f)
        except Exception:
            self.all_table.clear()
            return
        data = [[r["engineer"] or UNKNOWN, r["part_group"] or UNKNOWN, r["part_no"],
                 str(r["parts"]), fmt_hour(r["hour"]), str(r["entries"])] for r in rows]
        if data:
            data.append(["", "", "TOTAL  (%d parts)" % len(rows), str(sum(r["parts"] for r in rows)),
                         fmt_hour(sum(r["hour"] for r in rows)), str(sum(r["entries"] for r in rows))])
        bgs = ["a" if i % 2 == 0 else "b" for i in range(max(0, len(data) - 1))]
        bolds = [False] * len(bgs)
        if data:
            bgs.append("sub")
            bolds.append(True)
        self.all_table.set_data(autofit_columns(ALLPARTS_COLS, data), data, bgs, bolds, frozen=3,
                                align=["w", "w", "w", "e", "e", "e"])

    def show_removed(self):
        rows = ctx.db.discarded_rows()
        if not rows:
            QMessageBox.information(self, "Nothing removed", "No row was removed.")
            return
        cols = [("MATERIAL", 130), ("SERIAL NO", 110), ("OPERATION", 100), ("CAUSE OP", 100),
                ("RW HOUR", 90), ("DATE", 105), ("WORK CENTRE", 110), ("REASON", 320)]
        data = [[r["material"], r["serial_no"], r["reject_op"], r["cause_op"], fmt_hour(r["rw_hour"]),
                 r["rw_date"] or UNKNOWN, r["wc_code"], r["reason"]] for r in rows]
        TableDialog(self, "Rows removed at import",
                    "Left out of the analysis: a single rework entry cannot be this long.",
                    cols, data, ["sub"] * len(data), frozen=2, size=(1000, 460)).exec()

    def do_export(self):
        res = self.current["result"]
        if not res:
            QMessageBox.warning(self, "Nothing to export", "Run an analysis first.")
            return
        f = res["filters"]
        tag = part_tag(f["part_no"]) if f["part_no"] != ALL else (
            f["part_group"] if f["part_group"] != ALL else f["engineer"])
        p, _x = QFileDialog.getSaveFileName(
            self, "Export", "RW_Analysis_%s.xlsx" % re.sub(r"[^A-Za-z0-9]+", "_", str(tag))[:32],
            "Excel (*.xlsx)")
        if not p:
            return
        sr = self.current.get("serial")
        try:
            if not f.get("need_cause_details"):
                ctx.busy(True, "Preparing export...")
                try:
                    res = run_analysis_cached(ctx.db, dict(f, need_cause_details=True))
                finally:
                    ctx.busy(False)
            export_to_excel(res, p, serial_recs=(sr[1] if sr else None),
                            serial_no=(sr[0] if sr else None))
        except Exception as e:
            QMessageBox.critical(self, "Export failed", "%s\n\n%s" % (e, traceback.format_exc()))
            return
        ctx.status("Exported: " + p)

    def refresh_theme(self):
        for m in (self.m_parts, self.m_hours, self.m_fty):
            m.paint()
        self.paint_col_filters()
        # v71: the table's text colours belong to the theme - rebuild it
        if self.built:
            self.refresh(force=True)

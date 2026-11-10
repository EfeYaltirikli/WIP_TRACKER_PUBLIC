# -*- coding: utf-8 -*-
"""CAUSE OP ANALYSIS - the Qt page (new design).

Same as the Tk page (page_cause.py): OVERALL CAUSE OP SHARE, PER PART /
PER REJECT OP and BY REWORK WORKCENTER - each one opened from MODULES and
each can go into its own window.  (v69: the cause op trend module was
taken out at the user's request; only the overall share opens by default.)  Every table shows the
numbers themselves (parts, hours) next to the percentages.
"""

import datetime
import re
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QHBoxLayout, QMessageBox, QFileDialog

from wip_core import (ALL, UNKNOWN, all_parts_totals, export_cause_to_excel,
                      fmt_hour, run_analysis_cached, unit_key)
from qt_widgets import (ctx, Splitter, ModuleCard, DataTable, FilterBar, Toggle, label, ZoomControl,
                        MetricBox, heat_color, BarList, ModuleGrid, page_layout, page_head,
                        export_button)
from qt_page_analysis import part_tag

OVERALL_COLS = [("CAUSE OP", 90), ("CAUSE OP DESCRIPTION", 220), ("REJECTED PARTS", 100),
                ("% OF PARTS", 100), ("RW HOUR", 90), ("% OF RW HOUR", 110)]
CAUSE_COLS = [("PART NO", 120), ("REJECT OP", 80), ("REJECT OP DESCRIPTION", 200),
              ("CAUSE OP", 80), ("CAUSE OP DESCRIPTION", 200), ("RW HOUR", 80),
              ("% OF THIS REJECT OP", 130)]
WORKCENTER_COLS = [("WORK CENTER GROUP", 160), ("REJECTED PARTS", 100), ("% OF PARTS", 100),
                   ("RW HOUR", 90), ("% OF RW HOUR", 110)]


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self._agg = {"key": None}
        self.last = {"overall": [], "detail": [], "workcenter": [], "filters": None}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        outer = page_layout(self)
        page_head(outer, "Cause Op Analysis", "Where rework comes from",
                  export_button(self.do_export))

        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("cause"), part_multi=True,
                             with_machines=True,
                             machine_scope_fn=lambda: self.bar.machine_scope_for("cause"))
        self.bar.remember("cause")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(lambda: (self.bar.cascade("cause"), self.schedule()))
        outer.addWidget(self.bar)

        # v69 (CA1): the four totals as boxes, the time frame beside them
        self.m_parts = MetricBox("REJECTED PARTS (SELECTION)")
        self.m_hours = MetricBox("RW HOUR (SELECTION)")
        self.m_all_parts = MetricBox("REJECTED PARTS (ALL)")
        self.m_all_hours = MetricBox("RW HOUR (ALL)")
        self.when_lbl = label("", muted=True)
        mrow = QHBoxLayout()
        mrow.setSpacing(10)
        for m in (self.m_parts, self.m_hours, self.m_all_parts, self.m_all_hours):
            m.setMinimumWidth(190)
            mrow.addWidget(m)
        mrow.addWidget(self.when_lbl)
        mrow.addStretch(1)
        outer.addLayout(mrow)

        trow = QHBoxLayout()
        trow.setSpacing(8)
        trow.addWidget(label("MODULES", section=True))
        self.t_overall = Toggle("Overall cause op share", checked=True, slot=self.rebuild_panels)
        # 2.1.3: every module open on arrival - switch off what is not wanted
        self.t_detail = Toggle("Per part / per reject op", checked=True, slot=self.rebuild_panels)
        self.t_wc = Toggle("By rework work centre", checked=True, slot=self.rebuild_panels)
        for t in (self.t_overall, self.t_detail, self.t_wc):
            trow.addWidget(t)
        trow.addStretch(1)
        self.zoom = ZoomControl(lambda: [self.detail_t, self.overall_t, self.wc_t])
        trow.addWidget(self.zoom)
        outer.addLayout(trow)

        self.grid = ModuleGrid()
        outer.addWidget(self.grid)
        self.overall = ModuleCard("Overall cause op share", "", on_export=self.do_export)
        self.detail = ModuleCard("Per part / per reject op", "RW hours", on_export=self.do_export)
        self.wc = ModuleCard("By rework work centre", "", on_export=self.do_export)
        self.overall_t = BarList(bold_code=True)
        self.wc_t = BarList(bold_pct=True)
        self.detail_t = DataTable()
        self.detail_t.tiles = True
        self.detail_t.fit_rows(14)
        self.detail_t.view.horizontalHeader().setStretchLastSection(False)
        for card, t in ((self.overall, self.overall_t), (self.detail, self.detail_t),
                        (self.wc, self.wc_t)):
            card.content_lay.addWidget(t)
            card.content_lay.addStretch(1)
        # v69 (CA1): overall | per part side by side with a handle between,
        # the work centre list under them; a closed module gives its room
        # to the others
        self.rows2 = []
        for cards, ks in (((self.overall, self.detail), (2, 3)), ((self.wc,), (1,))):
            sp = Splitter(Qt.Horizontal)
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(10)
            for i, (c, k) in enumerate(zip(cards, ks)):
                sp.addWidget(c)
                sp.setStretchFactor(i, k)
                c.setMinimumWidth(320)
            self.grid.add(sp, wide=True)
            sp.show()
            self.rows2.append((sp, cards))
        for t, c in ((self.t_overall, self.overall), (self.t_detail, self.detail),
                     (self.t_wc, self.wc)):
            c.setVisible(t.isChecked())
        self.show_rows()
        self.grid.relayout()
        self.flags = ((self.t_overall, self.overall), (self.t_detail, self.detail),
                      (self.t_wc, self.wc))

    # ---------------------------------------------------------------
    def aggregates(self, records):
        if self._agg["key"] == id(records):
            return self._agg
        overall, by_part, by_wc = {}, {}, {}
        for r in records:
            hour = r["rw_hour"]
            unit = unit_key(r)
            ck = (r["cause_op"], r["cause_op_desc"])
            e = overall.setdefault(ck, {"hour": 0.0, "units": set()})
            e["hour"] += hour
            e["units"].add(unit)
            bucket = by_part.setdefault(r["part_no"], {})
            rk = (r["reject_op"], r["reject_op_desc"])
            rej = bucket.setdefault(rk, {"causes": {}, "hour": 0.0, "units": set()})
            rej["hour"] += hour
            rej["units"].add(unit)
            c = rej["causes"].setdefault(ck, {"hour": 0.0, "units": set()})
            c["hour"] += hour
            c["units"].add(unit)
            cls = r["comp"] or UNKNOWN
            w = by_wc.setdefault(cls, {"hour": 0.0, "units": set(), "codes": set()})
            w["hour"] += hour
            w["units"].add(unit)
            if r["wc_code"]:
                w["codes"].add(r["wc_code"])
        self._agg = {"key": id(records), "overall": overall, "breakdown": by_part,
                     "workcenter": by_wc}
        return self._agg

    def show_rows(self):
        for sp, cards in self.rows2:
            sp.setVisible(not all(c.isHidden() for c in cards))

    def rebuild_panels(self):
        for t, c in self.flags:
            c.setVisible(t.isChecked() or c.detached)
        self.show_rows()
        self.grid.relayout()
        self.refresh()

    def on_show(self):
        if not self.built:
            self.built = True
            self.bar.set_choices(self.bar.eng, ctx.db.engineers())
            self.bar.refresh_years(ctx.db.rw_years())
            self.bar.cascade("cause")
            self.bar.quick_week(0)
            self.timer.stop()
        self.refresh(force=True)

    def reload(self):
        if self.built:
            self.bar.set_choices(self.bar.eng, ctx.db.engineers())
            self.bar.refresh_years(ctx.db.rw_years())
            self.bar.cascade("cause")

    def schedule(self):
        if self.built:
            self.timer.start(220)

    def refresh(self, force=False):
        if not self.built:
            return
        if not force and not self.isVisible() and not any(c.detached for _t, c in self.flags):
            return
        self.timer.stop()
        ctx.busy(True, "Analysing cause operations...")
        try:
            self._refresh()
        except Exception as e:
            traceback.print_exc()
            ctx.status("Cause op error: %s" % e)
        finally:
            ctx.busy(False)

    def _refresh(self):
        if ctx.db.counts()["rw"] == 0:
            for t in (self.overall_t, self.detail_t, self.wc_t):
                t.clear()
            return
        f = self.bar.analysis_filters("cause")
        res = run_analysis_cached(ctx.db, f)
        self.when_lbl.setText(f["time_label"])
        self.m_parts.set(res["part_count"])
        self.m_hours.set(fmt_hour(res["rw_hour"]))
        a_parts, a_hours = all_parts_totals(ctx.db, f)
        self.m_all_parts.set(a_parts)
        self.m_all_hours.set(fmt_hour(a_hours))
        agg = self.aggregates(res["records"])
        totals = agg["overall"]
        gh = sum(e["hour"] for e in totals.values())
        gp = res["part_count"]
        orows = []
        for ck, e in sorted(totals.items(), key=lambda kv: -kv[1]["hour"]):
            orows.append([ck[0], ck[1], str(len(e["units"])),
                          "%.1f%%" % ((len(e["units"]) / gp * 100.0) if gp else 0.0),
                          fmt_hour(e["hour"]), "%.1f%%" % ((e["hour"] / gh * 100.0) if gh else 0.0)])
        self.when_lbl.setText(f["time_label"])
        self.overall.set_sub("%s h" % fmt_hour(gh))
        if self.t_overall.isChecked() or self.overall.detached:
            top = sorted(totals.items(), key=lambda kv: -kv[1]["hour"])
            items = []
            top_h = top[0][1]["hour"] if top else 1.0
            for ck, e in top[:12]:
                frac = (e["hour"] / gh) if gh else 0.0
                # bars against the biggest op; "Other" fills its row (CA1)
                items.append(("%s %s" % (ck[0], ck[1]), e["hour"] / (top_h * 1.05) if top_h else 0.0,
                    "%s h · %.0f%%" % (fmt_hour(e["hour"]), frac * 100),
                    "%s parts (%.1f%% of parts)" % (len(e["units"]),
                                                    (len(e["units"]) / gp * 100.0) if gp else 0.0),
                    heat_color(e["hour"] / top_h if top_h else 0.0)))
            rest = top[12:]
            if rest:
                rh = sum(e["hour"] for _ck, e in rest)
                frac = (rh / gh) if gh else 0.0
                items.append(("Other %d ops" % len(rest), 1.0,
                              "%s h · %.0f%%" % (fmt_hour(rh), frac * 100), "",
                              ctx.TH["muted"]))
            self.overall_t.set_items(items)
        wrows = []
        for desc, e in sorted(agg["workcenter"].items(), key=lambda kv: -kv[1]["hour"]):
            wrows.append([desc, str(len(e["units"])),
                          "%.1f%%" % ((len(e["units"]) / gp * 100.0) if gp else 0.0),
                          fmt_hour(e["hour"]), "%.1f%%" % ((e["hour"] / gh * 100.0) if gh else 0.0)])
        self.last["workcenter"] = wrows
        if self.t_wc.isChecked() or self.wc.detached:
            items = []
            wsorted = sorted(agg["workcenter"].items(), key=lambda kv: -kv[1]["hour"])
            wmax = wsorted[0][1]["hour"] if wsorted else 1.0
            for desc, e in wsorted:
                frac = (e["hour"] / gh) if gh else 0.0
                items.append((desc, e["hour"] / wmax if wmax else 0.0, "%s parts · %s h · %.0f%%"
                              % (len(e["units"]), fmt_hour(e["hour"]), frac * 100),
                              ", ".join(sorted(e["codes"]))[:300],
                              heat_color(e["hour"] / wmax if wmax else 0.0)))
            self.wc_t.set_items(items)
        # detail rows (the Excel sheet keeps every cause behind each tile)
        by_part = agg["breakdown"]
        rows = []
        for part_no in sorted(by_part):
            for rk in sorted(by_part[part_no], key=lambda k: -by_part[part_no][k]["hour"]):
                rej = by_part[part_no][rk]
                causes = sorted(rej["causes"].items(), key=lambda kv: -kv[1]["hour"])
                for ck, c in causes:
                    pct = (c["hour"] / rej["hour"] * 100.0) if rej["hour"] else 0.0
                    rows.append([part_no, rk[0], rk[1], ck[0], ck[1], fmt_hour(c["hour"]),
                                 "%.1f%%" % pct])
                if len(causes) > 1:
                    rows.append([part_no, rk[0], rk[1], "", "TOTAL", fmt_hour(rej["hour"]), "100.0%"])
        self.last["detail"] = rows
        if self.t_detail.isChecked() or self.detail.detached:
            # part x reject op, RW hours in each tile; hover for the causes
            ops = {}
            for part_no, rejs in by_part.items():
                for rk, rej in rejs.items():
                    ops[rk[0]] = ops.get(rk[0], 0.0) + rej["hour"]
            op_list = sorted(ops, key=lambda o: -ops[o])[:24]
            op_list.sort(key=lambda o: (len(str(o)), str(o)))
            cols = [("PART", 120)] + [(str(o), 52) for o in op_list]
            drows, dbg, dfg, tips, plain = [], {}, {}, {}, set()
            hmax = max([rej["hour"] for rejs in by_part.values() for rej in rejs.values()] or [1.0])
            for ri, part_no in enumerate(sorted(by_part)):
                line = [part_no]
                cells = {rk[0]: (rk, rej) for rk, rej in by_part[part_no].items()}
                for ci, o in enumerate(op_list, start=1):
                    if o in cells:
                        rk, rej = cells[o]
                        line.append(fmt_hour(rej["hour"]))
                        dbg[(ri, ci)] = heat_color(rej["hour"] / hmax if hmax else 0)
                        dfg[(ri, ci)] = "#FFFFFF"
                        causes = sorted(rej["causes"].items(), key=lambda kv: -kv[1]["hour"])
                        tips[(ri, ci)] = "%s  ·  reject op %s %s\n" % (part_no, rk[0], rk[1]) + "\n".join(
                            "cause %s %s: %s h (%.0f%%)" % (ck[0], ck[1], fmt_hour(c["hour"]),
                                                          (c["hour"] / rej["hour"] * 100.0)
                                                          if rej["hour"] else 0.0)
                            for ck, c in causes[:8])
                    else:
                        line.append("")
                        plain.add((ri, ci))
                drows.append(line)
            # v69 (CA1): a TOTAL row - each reject op over all parts
            drows.append(["TOTAL"] + [fmt_hour(sum(by_part[pn][rk]["hour"] for pn in by_part
                                                   for rk in by_part[pn] if rk[0] == o))
                                      for o in op_list])
            bgs = ["a"] * (len(drows) - 1) + ["sub"]
            self.detail_t.set_data(cols, drows, bgs, [False] * (len(drows) - 1) + [True],
                                   frozen=1, cell_bg=dbg,
                                   cell_fg=dfg, plain_cells=plain,
                                   align=["w"] + ["center"] * len(op_list), vertical_head=False)
            self.detail_t.cell_tip = tips
            self.detail.set_sub("RW hours · reject ops across, hover a tile for its causes"
                                + ("  (24 biggest ops)" if len(ops) > 24 else ""))
        self.zoom.apply_zoom()
        self.last["overall"] = orows
        self.last["filters"] = f

    def do_export(self):
        f = self.last["filters"]
        if not f:
            QMessageBox.warning(self, "Nothing to export", "Open the page first.")
            return
        tag = part_tag(f["part_no"]) if f["part_no"] != ALL else (
            f["part_group"] if f["part_group"] != ALL else f["engineer"])
        path, _x = QFileDialog.getSaveFileName(
            self, "Export", "Cause_Op_%s.xlsx" % re.sub(r"[^A-Za-z0-9]+", "_", str(tag))[:32],
            "Excel (*.xlsx)")
        if not path:
            return
        a_parts, a_hours = all_parts_totals(ctx.db, f)
        header = [("Engineer", f["engineer"]), ("Part group", f["part_group"]),
                  ("Part number", part_tag(f["part_no"])), ("Time frame", f["time_label"]),
                  ("Rejected parts (selection)", self.m_parts.val.text()),
                  ("Total RW hour (selection)", self.m_hours.val.text()),
                  ("Rejected parts (all parts)", a_parts),
                  ("Total RW hour (all parts)", fmt_hour(a_hours)),
                  ("Created", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))]
        try:
            export_cause_to_excel(path, header, [c[0] for c in OVERALL_COLS], self.last["overall"],
                                  [c[0] for c in CAUSE_COLS], self.last["detail"],
                                  [c[0] for c in WORKCENTER_COLS], self.last["workcenter"])
        except Exception as e:
            QMessageBox.critical(self, "Export failed", "%s\n\n%s" % (e, traceback.format_exc()))
            return
        ctx.status("Exported: " + path)

    def refresh_theme(self):
        for m in (self.m_parts, self.m_hours, self.m_all_parts, self.m_all_hours):
            m.paint()
        if self.built:
            self.refresh(force=True)

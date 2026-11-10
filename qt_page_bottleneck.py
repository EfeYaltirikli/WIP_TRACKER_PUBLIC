# -*- coding: utf-8 -*-
"""PART BOTTLENECK ANALYSIS - the Qt page (new design).

Same as the Tk page (page_bottleneck.py): one part, two modules side by
side - LONGEST WAIT, PER SERIAL and PARTS AT EACH OPERATION, DAY BY DAY -
each opened from MODULES and each can go into its own window.  The day
table keeps the original green-amber-red scale with purple for the days
that piled up far past the rest, upright day headings and a dark gap at
each week break.  Clicking an operation lights it in both tables.
"""

import re
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QHBoxLayout, QMessageBox, QFileDialog

from wip_core import (ALL, bottleneck_flow, bottleneck_matrix, bottleneck_wait_ranking,
                      export_bottleneck_to_excel, format_wf_hours, iso_year_week, norm_text,
                      sold_count_by_date_for_part, time_mode_to_date_range, week_offset)
from qt_widgets import (ctx, Splitter, ModuleCard, DataTable, FilterBar, Toggle, Seg, label,
                        ZoomControl, OUTLIER_COLOR, heat_color, heat_scale, FlowLayout, hbox,
                        HeatLegend, ModuleGrid, page_layout, page_head, export_button)

EMPTY_FLOW = {"completed": {}, "completed_no_x": {}, "queue": {}, "left": {}, "skipped": {},
              "arrived": {}, "passed": {}, "waiting_end": {}}


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.last = {"data": None, "part": "", "label": ""}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)

        outer = page_layout(self)
        page_head(outer, "Part Bottleneck", "Where parts wait, and how many sit at each operation",
                  export_button(self.do_export))

        # On by default here: the question this page answers ("what is
        # stuck right now") is about the floor, not parts that shipped.
        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("bottleneck"),
                             part_multi=False, wip_label="By WIP part")
        self.bar.wip.setChecked(True)
        self.bar.remember("bottleneck")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(self.cascade)
        outer.addWidget(self.bar)

        opts = QWidget()
        of = FlowLayout(opts, 8)
        self.wait_unit = Seg([("Hours", "hours"), ("Days", "days")], "days")
        self.wait_unit.changed.connect(lambda _v: self.schedule())
        of.addWidget(hbox(label("Wait as", muted=True), self.wait_unit, spacing=8))
        self.t_show_wait = Toggle("Waiting", checked=True, slot=lambda: self._toggle_show(self.t_show_wait))
        self.t_show_done = Toggle("Finished", checked=True, slot=lambda: self._toggle_show(self.t_show_done))
        # A step moved past without its X is finished all the same, on the
        # day of its last entry; off gives exactly the shop's Excel numbers.
        self.t_nox = Toggle("Count finished without X", checked=True, slot=self.schedule)
        of.addWidget(hbox(label("Show", muted=True), self.t_show_wait, self.t_show_done,
                          self.t_nox, spacing=8))
        self.t_noclean = Toggle("Remove clean ops", checked=True, slot=self.schedule)
        self.t_dedupe = Toggle("One entry per op", checked=True, slot=self.schedule)
        of.addWidget(self.t_noclean)
        of.addWidget(self.t_dedupe)
        outer.addWidget(opts)

        orow = QHBoxLayout()
        orow.setSpacing(8)
        orow.addWidget(label("MODULES", section=True))
        self.t_wait = Toggle("Longest wait, per serial", checked=True, slot=self.rebuild_panels)
        self.t_day = Toggle("Parts at each operation, day by day", checked=True, slot=self.rebuild_panels)
        orow.addWidget(self.t_wait)
        orow.addWidget(self.t_day)
        orow.addStretch(1)
        self.zoom = ZoomControl(lambda: [self.wait_t, self.day_t])
        orow.addWidget(self.zoom)
        outer.addLayout(orow)
        self.filter_lbl = label("", muted=True)
        self.filter_lbl.setWordWrap(True)
        outer.addWidget(self.filter_lbl)

        self.grid = ModuleGrid()
        outer.addWidget(self.grid)
        self.wait = ModuleCard("Longest wait, per serial", "", on_export=self.do_export)
        self.wait_t = DataTable()
        self.wait_t.tiles = True
        self.wait_t.tile_radius = 7
        self.wait_t.fit_width = True
        # beside the day table it fills the card height (scrolls inside)
        self.wait.content_lay.addWidget(self.wait_t, 1)
        # v71: no card-wide hover text - it popped up over every empty cell;
        # the numbers carry their own (the serials they count)
        self.day = ModuleCard("Parts at each operation, day by day",
                              "waiting = here at the end of the day · finished = closed by its X "
                              "that day", on_export=self.do_export)
        self.day_t = DataTable()
        self.day_t.tiles = True
        self.day_t.tile_radius = 7
        self.day_t.fit_rows(22)
        self.day.content_lay.addWidget(self.day_t)
        self.day.content_lay.addWidget(HeatLegend())
        # v69 (BN1): the two modules side by side with a handle between
        # them to share the width; one closed, the other takes it all
        self.split = Splitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(10)
        self.split.addWidget(self.wait)
        self.split.addWidget(self.day)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.wait.setMinimumWidth(320)
        self.day.setMinimumWidth(360)
        self.grid.add(self.split, wide=True)
        self.split.show()
        for t, c in ((self.t_wait, self.wait), (self.t_day, self.day)):
            c.setVisible(t.isChecked())
        self.grid.relayout()
        self.flags = ((self.t_wait, self.wait), (self.t_day, self.day))
        for t in (self.wait_t, self.day_t):
            t.pick_col = 0
            t.on_pick = self.pick_op

    # ---------------------------------------------------------------
    def _toggle_show(self, changed):
        # never both off - there would be nothing left to draw
        if not self.t_show_wait.isChecked() and not self.t_show_done.isChecked():
            changed.setChecked(True)
            return
        self.schedule()

    def pick_op(self, op):
        """One operation lit in BOTH tables; clicking it again puts it out."""
        for t in (self.wait_t, self.day_t):
            t.set_picked(op)

    def rebuild_panels(self):
        for t, c in self.flags:
            c.setVisible(t.isChecked() or c.detached)
        self.grid.relayout()
        self.schedule()

    def cascade(self):
        """Engineer -> group -> parts with a traveler route (without one there
        are no operations to lay the days out against)."""
        merge = ctx.db.merge_on("bottleneck")
        parts = ctx.db.all_parts(merge)
        eng = self.bar.set_choices(self.bar.eng, sorted({p["engineer"] for p in parts
                                                         if p["engineer"]}))
        grp = self.bar.set_choices(self.bar.grp, sorted(
            {p["part_group"] for p in parts if p["part_group"]
             and (eng == ALL or (p["engineer"] or "").upper() == eng.upper())}))
        idx = {(p["part_key"] or norm_text(p["part_no"])): p for p in parts}
        shown = []
        for pk, disp in ctx.db.wf_parts(merge):
            info = idx.get(pk)
            e = (info["engineer"] if info else "") or ""
            g = (info["part_group"] if info else "") or ""
            if eng != ALL and e.upper() != eng.upper():
                continue
            if grp != ALL and g != grp:
                continue
            shown.append((pk, disp))
        self.bar.parts.rebuild([pk for pk, _d in shown], labels=dict(shown))
        self.bar.paint_chips()
        self.schedule()

    def on_show(self):
        if not self.built:
            self.built = True
            self.bar.refresh_years(ctx.db.rw_years() or ctx.db.wf_years())
            self.bar._quiet += 1
            self.bar.quick_week(0)
            self.bar._quiet -= 1
            self.cascade()
        self.timer.stop()
        self.refresh(force=True)

    def reload(self):
        if self.built:
            self.bar.refresh_years(ctx.db.rw_years() or ctx.db.wf_years())
            self.cascade()

    def schedule(self):
        if self.built:
            self.timer.start(220)

    def refresh(self, force=False):
        if not self.built:
            return
        if not force and not self.isVisible() and not any(c.detached for _t, c in self.flags):
            return
        self.timer.stop()
        ctx.busy(True, "Building bottleneck view...")
        try:
            self._refresh()
        except Exception as e:
            traceback.print_exc()
            ctx.status("Bottleneck error: %s" % e)
        finally:
            ctx.busy(False)

    # ---------------------------------------------------------------
    def _refresh(self):
        pk = self.bar.parts.get_selected()
        if not pk:
            self.day_t.clear()
            self.wait_t.clear()
            self.filter_lbl.setText("No part with a traveler route.")
            return
        f = self.bar.filters()
        f["time_label"] = self.bar.time_label(f)
        ws = ctx.db.week_start("bottleneck")
        d1, d2 = time_mode_to_date_range(f, ws)
        wanted = self.bar.get_serials()
        no_clean = self.t_noclean.isChecked()
        dedupe = self.t_dedupe.isChecked()
        bad = ctx.db.flag_keys({k for k, t in self.bar.exclude_flags.items() if t.isChecked()})
        # WIP on: only what is still on the floor; off: the whole history
        if self.bar.wip.isChecked():
            bad |= ctx.db.sold_serial_keys()
        only = {k for k, t in self.bar.only_flags.items() if t.isChecked()}
        if only:
            only_set = ctx.db.flag_keys(only)
            wanted = (wanted & only_set) if wanted else only_set
        # the matrix backs the day table AND the export, so it always runs
        data = bottleneck_matrix(ctx.db, pk, d1, d2, serial_keys=wanted or None,
                                 exclude_clean=no_clean, dedupe_ops=dedupe,
                                 exclude_serial_keys=bad)
        if not data:
            self.day_t.clear()
            self.wait_t.clear()
            self.filter_lbl.setText("This part has no traveler route.")
            return
        self.last = {"data": data, "part": self.bar.parts.label_of(pk), "label": f["time_label"]}
        days = data["days"]
        text = f["time_label"]
        if data["truncated"]:
            text += "   (showing the last %d days, %d earlier day(s) left out)" % (
                len(days), data["truncated"])
        self.filter_lbl.setText(text)

        if self.t_day.isChecked() or self.day.detached:
            self.fill_day(pk, d1, d2, data, wanted, no_clean, dedupe, bad, ws, text)
        else:
            self.day_t.clear()
        if self.t_wait.isChecked() or self.wait.detached:
            self.fill_wait(pk, d1, d2, wanted, no_clean, dedupe, bad)
        else:
            self.wait_t.clear()
        self.zoom.apply_zoom()
        ctx.status("%s   |   %s   |   %d days, %d operations"
                   % (self.last["part"], f["time_label"], len(days), len(data["route"])))

    def fill_day(self, pk, d1, d2, data, wanted, no_clean, dedupe, bad, ws, text):
        days = data["days"]
        th = ctx.TH
        cols = [("OPERATION", 90), ("DESCRIPTION", 190), ("PASSED", 66), ("WAITING NOW", 104)]
        show_wait, show_done = self.t_show_wait.isChecked(), self.t_show_done.isChecked()
        both = show_wait and show_done
        flow = bottleneck_flow(ctx.db, pk, d1, d2, serial_keys=wanted or None,
                               exclude_clean=no_clean, dedupe_ops=dedupe,
                               exclude_serial_keys=bad,
                               count_without_x=self.t_nox.isChecked(),
                               with_serials=True) or EMPTY_FLOW
        done = flow.get("completed") or {}
        nox = flow.get("completed_no_x") or {}
        n_route = len(data["route"])
        # WAITING is the running backlog; when both columns show they share
        # its colour so the pair reads as one unit.  Days that piled up far
        # past the rest get purple and drop out of the scale.
        wlo, whi, wout = heat_scale([flow["queue"].get((ri, d), 0)
                                     for ri in range(n_route) for d in days])
        # v69 (BN1): FINISHED gets its own colour from its own scale, the
        # pair of one day sits in one framed box instead
        flo, fhi, fout = heat_scale([done.get((ri, d), 0)
                                     for ri in range(n_route) for d in days])
        pair_cols = set()
        if not both:
            source = flow["queue"] if show_wait else done
            lo, hi, out_t = heat_scale(list(source.values()))
        day_week = [iso_year_week(d, week_offset(ws)) for d in days]
        week_break = [i < len(days) - 1 and day_week[i] != day_week[i + 1]
                      for i in range(len(days))]
        GAP_W = 6
        head_span = {}
        for i, d in enumerate(days):
            if both:
                head_span[len(cols)] = 2
                pair_cols.add(len(cols))
                cols.append((d.strftime("%d.%m %a") + "  W | F", 38))
                cols.append(("", 38))
            else:
                cols.append((d.strftime("%d.%m %a"), 36))
            if week_break[i]:
                cols.append(("W%s" % day_week[i + 1][1], GAP_W))

        rows, bgs, plain, cbg, cfg, span = [], [], set(), {}, {}, {}
        # v70: hovering a number lists the serials it counts
        tips = {}
        q_who = flow.get("queue_who") or {}
        f_who = flow.get("completed_who") or {}

        def tip(ri, k, d, what, who):
            if not who:
                return
            col = data["route"][ri]
            names = sorted(who)
            shown = names[:60]
            lines = ["   ".join(shown[i:i + 4]) for i in range(0, len(shown), 4)]
            more = ("\n+ %d more" % (len(names) - len(shown))) if len(names) > len(shown) else ""
            tips[(ri, k)] = "%s  ·  op %s %s\n%s (%d):\n%s%s" % (
                d.strftime("%d.%m.%Y %a"), col.get("label") or col["op"], col["desc"], what,
                len(names), "\n".join(lines), more)

        def gap(line, ri, i):
            # a thin dark line between weeks (no gap between days: the tiles
            # have their own)
            if week_break[i]:
                line.append("")
                cbg[(ri, len(line) - 1)] = "#3A3F48" if th["name"] != "day" else "#C9CFD8"

        for ri, col in enumerate(data["route"]):
            line = [str(col.get("label") or col["op"]), col["desc"],
                    str(flow["passed"].get(ri, 0)), str(flow["waiting_end"].get(ri, 0))]
            for i, d in enumerate(days):
                if both:
                    w, c = flow["queue"].get((ri, d), 0), done.get((ri, d), 0)
                    a, b = len(line), len(line) + 1
                    tip(ri, a, d, "Waiting at the end of the day", q_who.get((ri, d)))
                    tip(ri, b, d, "Finished that day", f_who.get((ri, d)))
                    line += [str(w) if w else "", str(c) if c else ""]
                    for k, v, lo_, hi_, out_ in ((a, w, wlo, whi, wout), (b, c, flo, fhi, fout)):
                        if not v:
                            plain.add((ri, k))
                            continue
                        if out_ is not None and v > out_:
                            cbg[(ri, k)] = OUTLIER_COLOR
                        else:
                            cbg[(ri, k)] = heat_color(0.5 if hi_ <= lo_ else (v - lo_) / float(hi_ - lo_))
                        cfg[(ri, k)] = "#FFFFFF"
                else:
                    n = source.get((ri, d), 0)
                    k = len(line)
                    if show_wait:
                        tip(ri, k, d, "Waiting at the end of the day", q_who.get((ri, d)))
                    else:
                        tip(ri, k, d, "Finished that day", f_who.get((ri, d)))
                    if n:
                        line.append(str(n))
                        if out_t is not None and n > out_t:
                            cbg[(ri, k)] = OUTLIER_COLOR
                        else:
                            cbg[(ri, k)] = heat_color(0.5 if hi <= lo else (n - lo) / float(hi - lo))
                        cfg[(ri, k)] = "#FFFFFF"
                    else:
                        line.append("")
                        plain.add((ri, k))
                gap(line, ri, i)
            rows.append(line)
            bgs.append("a" if ri % 2 == 0 else "b")

        if days:
            sold = sold_count_by_date_for_part(ctx.db, pk, days[0], days[-1])
            line = ["SOLD THAT DAY", "", "", ""]
            ri = len(rows)
            for i, d in enumerate(days):
                n = sold.get(d, 0)
                if both:
                    # one number for the day, drawn across the W/F pair
                    span[(ri, len(line))] = 2
                    line += [str(n) if n else "", ""]
                else:
                    line.append(str(n) if n else "")
                gap(line, ri, i)
            rows.append(line)
            bgs.append("sub")

        n_nox = sum(nox.get((ri, d), 0) for ri in range(n_route) for d in days)
        if n_nox:
            self.filter_lbl.setText(
                ("%s   -   %d of the FINISHED count moved past their step without its X "
                 "(counted on the day of the last entry)." if self.t_nox.isChecked() else
                 "%s   -   %d steps moved past without their X are NOT counted in FINISHED.")
                % (text, n_nox))
        align = ["w", "w"] + ["center"] * (len(cols) - 2)
        self.day_t.pair_cols = pair_cols
        self.day_t.set_data(cols, rows, bgs, [False] * len(rows), frozen=4, plain_cells=plain,
                            align=align, plain_key="dim", cell_bg=cbg, cell_fg=cfg,
                            row_sep=True, cell_span=span, head_span=head_span,
                            vertical_head=True)
        self.day_t.cell_tip = tips

    def fill_wait(self, pk, d1, d2, wanted, no_clean, dedupe, bad):
        rank = bottleneck_wait_ranking(ctx.db, pk, d1, d2, serial_keys=wanted or None,
                                       exclude_clean=no_clean, dedupe_ops=dedupe,
                                       exclude_serial_keys=bad)
        if not rank:
            self.wait_t.clear()
            return
        unit = self.wait_unit.get()
        cols = [("OPERATION", 90), ("DESCRIPTION", 220), ("SERIALS WORST HERE", 130),
                ("AVG WAIT (%s)" % unit, 110)]
        total = sum(w["n_serials"] for w in rank)
        rows, cbg, cfg = [], {}, {}
        for ri, w in enumerate(rank):
            rows.append([str(w["op"]), w["desc"], str(w["n_serials"]),
                         format_wf_hours(w["avg_hours"], unit)])
            t = (w["n_serials"] / float(total)) if total else 0.0
            cbg[(ri, 2)] = heat_color(min(1.0, t * 3))
            cfg[(ri, 2)] = "#FFFFFF"
        self.wait_t.set_data(cols, rows, ["a" if i % 2 == 0 else "b" for i in range(len(rows))],
                             frozen=2, align=["w", "w", "center", "center"], cell_bg=cbg,
                             cell_fg=cfg)

    # ---------------------------------------------------------------
    def do_export(self):
        if not self.last["data"]:
            QMessageBox.warning(self, "Nothing to export", "Open the page first.")
            return
        path, _x = QFileDialog.getSaveFileName(
            self, "Export", "Bottleneck_%s.xlsx"
            % re.sub(r"[^A-Za-z0-9]+", "_", str(self.last["part"]))[:32], "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_bottleneck_to_excel(path, self.last["part"], self.last["label"],
                                       self.last["data"])
        except Exception as e:
            QMessageBox.critical(self, "Export failed", "%s\n\n%s" % (e, traceback.format_exc()))
            return
        ctx.status("Exported: " + path)

    def refresh_theme(self):
        if self.built:
            self.refresh(force=True)

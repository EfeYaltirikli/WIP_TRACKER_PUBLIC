# -*- coding: utf-8 -*-
"""FORECAST - the Qt page (new design).

Same as the Tk page (page_forecast.py): how busy each machine group is
about to be, built from the routes still open on the floor, the machines
on the B1000 plan (or typed in) and the shift timetable.  Every cell shows
the HOURS and the load % together ("120 h · 85%"); the colour always
shows the real load.  See wip_core.forecast_load for the arithmetic.
"""

import datetime
import json

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QMessageBox,
                               QFileDialog, QSpinBox, QDialog, QLineEdit, QCheckBox, QFrame, QLabel)

from wip_core import (ALL, WEEKDAY_NAMES, _hhmm, _mmhh, allowed_part_keys, capacity_from,
                      iso_year_week, week_offset, capacity_units_by_group, day_shift_hours,
                      forecast_load, manual_capacity, norm_text, read_rules,
                      save_shift_schedule, set_manual_capacity)
from qt_widgets import (ctx, Splitter, Card, ModuleCard, DataTable, FilterBar, Toggle, Seg,
                        FlowLayout, MultiSelect, label, button, hbox, ZoomControl, IconButton,
                        ModuleGrid, page_layout, page_head, export_button, heat_color)


def shift_editor_state(takvim):
    """The three shift definitions (the distinct shifts in clock order) and,
    per weekday, which of them are ticked - see page_forecast.py."""
    takvim = takvim or {}
    tanimlar = []
    for gun in sorted(takvim, key=lambda g: int(g)):
        for a, b in (takvim.get(gun) or ()):
            cift = (_mmhh(_hhmm(a)), _mmhh(_hhmm(b)))
            if cift not in tanimlar:
                tanimlar.append(cift)
    tanimlar.sort(key=lambda t: _hhmm(t[0]))
    varsayilan = [("07:00", "15:00"), ("15:00", "23:00"), ("23:00", "07:00")]
    for i, cift in enumerate(tanimlar[:3]):
        varsayilan[i] = cift
    tikli = {}
    for g in range(7):
        gunun = [(_mmhh(_hhmm(a)), _mmhh(_hhmm(b))) for a, b in (takvim.get(str(g)) or [])]
        tikli[g] = [varsayilan[i] in gunun for i in range(3)]
    return varsayilan, tikli


class ShiftDialog(QDialog):
    def __init__(self, parent, on_saved):
        super().__init__(parent)
        self.setWindowTitle("SHIFT TIMETABLE")
        self.on_saved = on_saved
        lay = QVBoxLayout(self)
        lay.addWidget(label("WHICH HOURS THE SHOP RUNS, DAY BY DAY", section=True))
        t = label("Tick a shift to run it on that day.  A day with no shift ticked is a day the "
                  "shop does not work - queue time and forecast capacity both stop on it.  A "
                  "shift that ends before it starts (23:00-07:00) runs through midnight and "
                  "belongs to the day it starts on.", muted=True)
        t.setWordWrap(True)
        lay.addWidget(t)
        varsayilan, tikli = shift_editor_state(read_rules().get("shift_schedule") or {})
        g = QGridLayout()
        g.setHorizontalSpacing(14)
        self.times = []
        for i, (a, b) in enumerate(varsayilan):
            ea, eb = QLineEdit(a), QLineEdit(b)
            for e in (ea, eb):
                e.setFixedWidth(60)
            self.times.append((ea, eb))
            g.addWidget(hbox(label("SHIFT %d" % (i + 1), section=True), spacing=4), 0, i + 1)
            g.addWidget(hbox(ea, label("-"), eb, spacing=4), 1, i + 1)
        self.ticks = {}
        for d, name in enumerate(WEEKDAY_NAMES):
            g.addWidget(label(name), d + 2, 0)
            for i in range(3):
                c = QCheckBox()
                c.setChecked(tikli[d][i])
                self.ticks[(d, i)] = c
                g.addWidget(c, d + 2, i + 1, alignment=Qt.AlignCenter)
        lay.addLayout(g)
        self.warn = label("")
        self.warn.setStyleSheet("color: %s;" % ctx.TH["danger"])
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)
        lay.addWidget(hbox(button("Save", primary=True, slot=self.save),
                           button("Cancel", slot=self.reject), None))

    def save(self):
        takvim = {}
        for d in range(7):
            gunun = [[self.times[i][0].text().strip(), self.times[i][1].text().strip()]
                     for i in range(3) if self.ticks[(d, i)].isChecked()]
            if gunun:
                takvim[str(d)] = gunun
        if not takvim:
            self.warn.setText("Tick at least one shift: a shop that never runs has no capacity "
                              "and nothing can be forecast.")
            return
        if not ctx.is_admin():
            self.warn.setText("Only an admin can change the shift timetable.")
            return
        try:
            save_shift_schedule(ctx.db, takvim)
        except Exception as e:
            self.warn.setText("Could not save: %s" % e)
            return
        self.accept()
        self.on_saved()


def week_breaks(days, week_start):
    """Indexes i where day i+1 starts a new factory week."""
    off = week_offset(week_start)
    wk = [iso_year_week(d, off) for d in days]
    return [i for i in range(len(days) - 1) if wk[i] != wk[i + 1]]


def insert_gaps(first, breaks, cols, rows, maps, gap_w=6):
    """Puts a narrow divider column after day column first+i for each i in
    breaks; every (row, col) keyed dict in maps is shifted to match."""
    cut = sorted(first + i for i in breaks)
    if not cut:
        return cols, rows, list(maps) + [set()]

    def new_c(c):
        return c + sum(1 for k in cut if k < c)
    ncols = []
    for c, col in enumerate(cols):
        ncols.append(col)
        if c in cut:
            ncols.append(("", gap_w))
    nrows = []
    for r in rows:
        line = []
        for c, v in enumerate(r):
            line.append(v)
            if c in cut:
                line.append("")
        nrows.append(line)
    nmaps = []
    for m in maps:
        nm = {(r, new_c(c)): v for (r, c), v in m.items()}
        nmaps.append(nm)
    gap_cells = {(r, new_c(k) + 1) for r in range(len(rows)) for k in cut}
    return ncols, nrows, nmaps + [gap_cells]


def load_level(f):
    """The three load colours, from the level 9 scale: green to 70%,
    amber to 100%, red over."""
    if f <= 0.7:
        return heat_color(0.0)
    if f <= 1.0:
        return heat_color(88.0 / 134.0)
    return heat_color(1.0)


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.state = {"last": None, "key": None, "groups": set(), "view_parts": None,
                      "chips": None}
        self.manual_rows = {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        self.mtimer = QTimer(self)
        self.mtimer.setSingleShot(True)
        self.mtimer.timeout.connect(self.apply_manual)

        outer = page_layout(self)
        page_head(outer, "Forecast", "Expected load per machine group, day by day",
                  export_button(self.do_export))

        # a forecast is "this many days ahead", not a date range
        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("forecast"), part_multi=True,
                             with_time=False, with_wait_days=True, wait_days_drop=True,
                             wait_days_default=30, wip_label="By WIP parts")
        self.bar.wip.setChecked(True)
        self.bar.remember("forecast")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.schedule)
        self.bar.eng_grp_changed.connect(self.schedule)
        outer.addWidget(self.bar)

        # v69 (FC4): every forecast option sits in one "Forecast settings"
        # menu; what is chosen shows as tags beside it
        self.days = QSpinBox()
        self.days.setRange(1, 365)
        self.days.setValue(30)
        self.days.setFixedWidth(90)
        self.days.valueChanged.connect(lambda _v: self.schedule())
        self.mode = Seg([("Machine by machine", "machine"), ("Standard times", "standard")],
                        "machine")
        self.mode.changed.connect(lambda _v: self.refresh())
        self.rows_sel = Seg([("Machine group", "machine"), ("Part group", "part_group"),
                             ("Part", "part")], "machine")
        self.rows_sel.changed.connect(lambda _v: self.draw())
        # Hours AND the load % together by default
        self.t_pct = Toggle("Show %", checked=True, slot=self.draw)
        # filtered parts still queue behind the rest of the shop
        self.t_others = Toggle("When parts are filtered, the rest still use the machines",
                               checked=True, slot=self.refresh)
        self.source = Seg([("B1000 Live", "plan"), ("Typed in", "manual")], "plan")
        self.source.changed.connect(lambda _v: self.on_source())
        self.show_sel = Seg([("Capacity I will use", "busy"), ("With backlog", "with_queue"),
                             ("Hours unused", "idle"), ("Parts waiting", "queue_n")],
                            "with_queue")
        self.show_sel.changed.connect(lambda _v: self.draw())

        self.menu = QFrame(None, Qt.Popup)
        self.menu.setProperty("popup", True)
        ml = QGridLayout(self.menu)
        ml.setContentsMargins(14, 12, 14, 14)
        ml.setHorizontalSpacing(10)
        ml.setVerticalSpacing(8)
        ml.addWidget(label("How far ahead", muted=True), 0, 0)
        ml.addWidget(hbox(self.days, label("days", muted=True), None, spacing=8), 0, 1)
        for r, (name, w) in enumerate((("How", self.mode), ("Rows", self.rows_sel),
                                       ("Capacity from", self.source), ("Cell", self.show_sel)),
                                      start=1):
            ml.addWidget(label(name, muted=True), r, 0)
            ml.addWidget(hbox(w, None), r, 1)
        ml.addWidget(hbox(self.t_pct, None), 5, 0, 1, 2)
        ml.addWidget(hbox(self.t_others, None), 6, 0, 1, 2)
        self.menu_btn = button("⚙  Forecast settings  ▾", slot=self.open_menu)
        self.chips_box = QWidget()
        self.chips_lay = FlowLayout(self.chips_box, 6)
        self.zoom = ZoomControl(lambda: [self.table])
        srow = QHBoxLayout()
        srow.setSpacing(8)
        srow.addWidget(self.menu_btn)
        srow.addWidget(self.chips_box, 1)
        srow.addWidget(self.zoom)
        outer.addLayout(srow)

        # v73: no OVER CAPACITY / WORST / MACHINES / BACKLOG boxes - the table has the room

        self.cap_lbl = label("", muted=True)
        self.cap_lbl.setWordWrap(True)
        outer.addWidget(self.cap_lbl)

        self.grid = ModuleGrid()
        outer.addWidget(self.grid)
        self.load = ModuleCard("Load", "bar = load · dashed line = 100 % capacity")
        self.load.fold_btn.hide()
        self.group_btn = button("Machine groups: ALL", slot=self.pick_groups)
        self.load.add_extra(self.group_btn)
        self.load.add_extra(IconButton("user", "", self.toggle_editor, text="  Machines / people"))
        self.table = DataTable()
        self.table.tiles = True
        self.table.fit_rows(24)
        self.load.content_lay.addWidget(self.table)
        self.note_lbl = label("", muted=True)
        self.note_lbl.setWordWrap(True)
        self.note_lbl.hide()
        _set = self.note_lbl.setText
        self.note_lbl.setText = lambda t: (_set(t), self.note_lbl.setVisible(bool(t)))
        self.load.content_lay.addWidget(self.note_lbl)

        # Machines / people opens on the right, a handle to share the width
        self.edit = Card("Machines / people", "")
        self.edit.add_action(IconButton("close", "Close", lambda: self.toggle_editor(False)))
        self.edit_inner = QWidget()
        self.edit_grid = QGridLayout(self.edit_inner)
        self.edit_grid.setContentsMargins(0, 0, 0, 0)
        self.edit_grid.setAlignment(Qt.AlignTop)
        self.edit.add(self.edit_inner)
        self.shift_box = QWidget()
        self.shift_grid = QGridLayout(self.shift_box)
        self.shift_grid.setContentsMargins(0, 10, 0, 0)
        self.shift_grid.setHorizontalSpacing(10)
        self.shift_grid.setVerticalSpacing(3)
        self.edit.add(self.shift_box)
        self.edit.add(hbox(button("Save → typed in", primary=True, slot=self.save_editor),
                           button("Reset from B1000", slot=self.reset_editor), None))
        self.split = Splitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(10)
        self.split.addWidget(self.load)
        self.split.addWidget(self.edit)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        self.edit.setMinimumWidth(300)
        self.grid.add(self.split, wide=True)
        self.split.show()
        self.load.show()
        self.edit.setVisible(False)
        self.grid.relayout()

        # the colour key: small, at the very bottom
        leg = QWidget()
        ll = QHBoxLayout(leg)
        ll.setContentsMargins(2, 0, 0, 0)
        ll.setSpacing(14)
        self.legend_sw = []
        for f, text in ((0.5, "≤ 70 % load"), (0.9, "≤ 100 %"), (2.0, "over capacity")):
            sw = QFrame()
            sw.setFixedSize(11, 9)
            self.legend_sw.append((sw, f))
            ll.addWidget(hbox(sw, label(text, muted=True), spacing=6))
        ll.addWidget(label("weekend / no shift days are shaded", muted=True))
        ll.addStretch(1)
        leg.setStyleSheet("font-size: 8.5pt;")
        outer.addWidget(leg)
        self.paint_legend()

    def paint_legend(self):
        for sw, f in self.legend_sw:
            sw.setStyleSheet("background: %s; border-radius: 3px;" % load_level(f))

    def open_menu(self):
        self.menu.setStyleSheet("QFrame[popup=\"true\"] { background: %s; border: 1px solid %s; "
                                "border-radius: 12px; }" % (ctx.TH["card"], ctx.TH["accent"]))
        self.menu.adjustSize()
        self.menu.move(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()) + QPoint(0, 6))
        self.menu.show()

    def paint_chips(self):
        """The chosen settings as tags beside the menu."""
        while self.chips_lay.count():
            it = self.chips_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        tags = ["%d days" % self.days.value(), self.mode.label(),
                "Rows: " + self.rows_sel.label().lower(),
                "Capacity: " + self.source.label(), "Cell: " + self.show_sel.label().lower()]
        if self.t_pct.isChecked():
            tags.append("Show %")
        if self.t_others.isChecked() and self.chosen_parts() is not None:
            tags.append("Rest still use the machines")
        for t in tags:
            c = QLabel(t)
            c.setStyleSheet("background: %s; color: %s; border-radius: 12px; padding: 4px 10px; "
                            "font-size: 9pt;" % (ctx.TH["sel_row"], ctx.TH["text"]))
            self.chips_lay.addWidget(c)

    # ---------------------------------------------------------------
    def on_show(self):
        if not self.built:
            self.built = True
            self.refresh_sources()
        self.timer.stop()
        self.refresh()

    def reload(self):
        if self.built:
            self.refresh_sources()
            self.schedule()

    def refresh_sources(self):
        """Only parts WITH A TRAVELER ROUTE can be forecast."""
        merge = ctx.db.merge_on("forecast")
        parts = ctx.db.all_parts(merge)
        self.bar.set_choices(self.bar.eng, ctx.db.engineers())
        self.bar.set_choices(self.bar.grp, sorted({p["part_group"] for p in parts
                                                   if p["part_group"]}))
        routed = ctx.db.wf_parts(merge)
        if self.state.get("chips") != routed:
            self.state["chips"] = routed
            self.bar.parts.rebuild([pk for pk, _d in routed], labels=dict(routed))
            self.bar.paint_chips()

    def schedule(self):
        if self.built:
            self.timer.start(220)

    # ---- typed-in capacity ----
    def on_source(self):
        if self.source.get() == "manual" and self.edit.isHidden():
            self.toggle_editor(True)
        self.refresh()

    def toggle_editor(self, on=None):
        on = self.edit.isHidden() if on is None else on
        if on:
            self.build_editor()
            self.build_shifts()
        self.edit.setVisible(on)

    def build_shifts(self):
        """Which hours the shop runs, day by day - read here, changed in
        the shift timetable window (admin)."""
        while self.shift_grid.count():
            it = self.shift_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        varsayilan, tikli = shift_editor_state(read_rules().get("shift_schedule") or {})
        t = label("Which hours the shop runs, day by day")
        t.setStyleSheet("font-weight: 600;")
        self.shift_grid.addWidget(t, 0, 0, 1, 4)
        for i, (a, b) in enumerate(varsayilan):
            self.shift_grid.addWidget(label("%s–%s" % (a[:2], b[:2]), muted=True), 1, i + 1)
        for d, name in enumerate(WEEKDAY_NAMES):
            self.shift_grid.addWidget(label(name[:3]), d + 2, 0)
            for i in range(3):
                self.shift_grid.addWidget(label("✓" if tikli[d][i] else "–",
                                                muted=not tikli[d][i]), d + 2, i + 1)
        self.shift_grid.addWidget(hbox(button("Shift timetable...", slot=self.open_shift_editor),
                                       None), 9, 0, 1, 4)

    def reset_editor(self):
        """Put the numbers back to what the B1000 floor plan says."""
        birim, makine, _kisi = capacity_from(ctx.db, "plan")
        for g, (m, k) in self.manual_rows.items():
            m.setValue(int(makine.get(g, 0)))
            k.setValue(int(birim.get(g, 0)))

    def save_editor(self):
        try:
            set_manual_capacity(ctx.db, self.read_editor())
        except Exception as e:
            QMessageBox.warning(self, "Machines / people", "Could not save: %s" % e)
            return
        self.source.set("manual")
        self.toggle_editor(False)
        self.refresh()

    def build_editor(self):
        """One row per machine group, seeded from the floor plan."""
        while self.edit_grid.count():
            it = self.edit_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.manual_rows.clear()
        birim, makine, _kisi = capacity_from(ctx.db, "plan")
        elle = manual_capacity(ctx.db)
        hepsi = sorted(set(birim) | set(elle))
        if not hepsi:
            t = label("No machine groups yet - draw the machines on B1000 first.", muted=True)
            t.setWordWrap(True)
            self.edit_grid.addWidget(t, 0, 0, 1, 3)
            return
        self.edit_grid.setColumnStretch(0, 1)
        for c, h in enumerate(("Machine group", "Machines", "People")):
            hl = label(h)
            hl.setStyleSheet("font-weight: 600;")
            self.edit_grid.addWidget(hl, 0, c)
        for r, g in enumerate(hepsi, start=1):
            m0, k0 = elle.get(g, (makine.get(g, 0), int(birim.get(g, 0))))
            self.edit_grid.addWidget(label(g[:18]), r, 0)
            boxes = []
            for c, v in enumerate((m0, k0), start=1):
                sb = QSpinBox()
                sb.setRange(0, 999)
                sb.setValue(int(v))
                self.edit_grid.addWidget(sb, r, c)
                boxes.append(sb)
            self.manual_rows[g] = tuple(boxes)

    def read_editor(self):
        return {g: (m.value(), k.value()) for g, (m, k) in self.manual_rows.items()}

    def apply_manual(self):
        try:
            set_manual_capacity(ctx.db, self.read_editor())
        except Exception:
            pass
        self.refresh()

    def open_shift_editor(self):
        ShiftDialog(self, self._shifts_saved).exec()

    def _shifts_saved(self):
        if not self.edit.isHidden():
            self.build_shifts()
        self.refresh()

    # ---- captions and pickers ----
    def capacity_text(self):
        takvim = read_rules().get("shift_schedule") or {}
        units, _n = capacity_units_by_group(ctx.db)
        if not units:
            return ("No machines on the B1000 floor plan carry a work centre code, so no "
                    "capacity can be worked out.  Draw the machines and give each one its WC code.")
        where = "typed in" if self.source.get() == "manual" else "from the B1000 floor plan"
        lines, prev, start = [], None, None
        for g in range(7):
            gunun = tuple(tuple(v) for v in (takvim.get(str(g)) or ()))
            if gunun != prev:
                if prev is not None:
                    lines.append((start, g - 1, prev))
                prev, start = gunun, g
        lines.append((start, 6, prev))

        def say(a, b, shifts):
            name = (WEEKDAY_NAMES[a][:3] if a == b
                    else "%s-%s" % (WEEKDAY_NAMES[a][:3], WEEKDAY_NAMES[b][:3]))
            if not shifts:
                return "%s  CLOSED" % name
            return "%s  %s  = %g h" % (name, " ".join("%s-%s" % (x, y) for x, y in shifts),
                                       day_shift_hours(a))
        return "SHIFT TIMETABLE:   %s      Machines and people: %s." % (
            "   |   ".join(say(*x) for x in lines), where)

    def paint_group_btn(self):
        n = len(self.state["groups"])
        self.group_btn.setText("Machine groups: ALL" if not n else "Machine groups: %d" % n)

    def pick_groups(self):
        data = self.state.get("last")
        hepsi = (data or {}).get("all_groups") or []
        if not hepsi:
            QMessageBox.information(self, "MACHINE GROUPS", "Run a forecast first.")
            return
        kap = (data or {}).get("capacity") or {}

        def apply(sel):
            self.state["groups"] = set(sel)
            self.paint_group_btn()
            self.draw()
        MultiSelect(self, "MACHINE GROUPS",
                    [(g, ("%.0f h/day" % kap[g]) if kap.get(g) else "") for g in hepsi],
                    sorted(self.state["groups"]), apply).exec()

    # ---- what the filters mean ----
    def chosen_parts(self):
        merge = ctx.db.merge_on("forecast")
        izin = allowed_part_keys(ctx.db, self.bar.engineer(), self.bar.group(), ALL, merge)
        sel = self.bar.parts.get_selected()
        if izin is None and sel == ALL:
            return None
        left = {pk for pk, _d in ctx.db.wf_parts(merge)}
        if sel != ALL:
            left &= set(sel)
        if izin is not None:
            left &= set(izin)
        return left

    def excluded_serials(self):
        kinds = {k for k, t in self.bar.exclude_flags.items() if t.isChecked()}
        return (ctx.db.flag_keys(kinds) or None) if kinds else None

    def only_serials(self):
        wanted = self.bar.get_serials()
        only = {k for k, t in self.bar.only_flags.items() if t.isChecked()}
        if only:
            s = ctx.db.flag_keys(only)
            wanted = (wanted & s) if wanted else s
        return wanted or None

    def scope(self):
        """(what the simulation runs on, whose numbers are shown)."""
        sel = self.chosen_parts()
        if sel is None or not self.t_others.isChecked():
            return sel, None
        return None, set(sel)

    def calc_key(self):
        """Everything that changes the numbers (not SHOW / % / groups)."""
        try:
            ver, ann = ctx.db.data_version(), ctx.db.annotation_version()
        except Exception:
            ver = ann = None
        sim, _shown = self.scope()
        try:
            lay = ctx.db.layout_active("B1000") or {}
            plan = (lay.get("id"), lay.get("version"))
        except Exception:
            plan = None
        manual = self.source.get() == "manual"
        return (self.days.value(), self.mode.get(), self.source.get(), self.bar.wip.isChecked(),
                tuple(sorted(sim)) if sim is not None else None,
                tuple(sorted(self.only_serials() or ())),
                tuple(sorted(k for k, t in self.bar.exclude_flags.items() if t.isChecked())),
                ver, ann, json.dumps(read_rules().get("shift_schedule") or {}, sort_keys=True),
                self.bar.wait_days(), plan, ctx.db.merge_on("forecast"),
                tuple(sorted(manual_capacity(ctx.db).items())) if manual else None)

    def refresh(self):
        if not self.built:
            return
        self.timer.stop()
        ctx.busy(True, "Forecasting...")
        try:
            self.cap_lbl.setText(self.capacity_text())
            key = self.calc_key()
            sim, shown = self.scope()
            self.state["view_parts"] = shown
            if self.state.get("key") != key or self.state.get("last") is None:
                n = key[0]
                today = datetime.date.today()
                self.state["last"] = forecast_load(
                    ctx.db, today, today + datetime.timedelta(days=n - 1), part_keys=sim,
                    exclude_sold=self.bar.wip.isChecked(), analyzer=ctx.wf_an,
                    mode=self.mode.get(), source=self.source.get(),
                    serial_keys=self.only_serials(), exclude_serial_keys=self.excluded_serials(),
                    max_wait_days=self.bar.wait_days(), merge=ctx.db.merge_on("forecast"))
                self.state["key"] = key
            self.draw()
        finally:
            ctx.busy(False)

    # ---- draw ----
    @staticmethod
    def load_text(f):
        return "#FFFFFF"

    def load_color(self, f):
        """v69: flat level-9 colour of the load (green / amber / red)."""
        return load_level(f)

    def fw_head(self, d):
        wk = iso_year_week(d, week_offset(ctx.db.week_start("forecast")))[1]
        # two lines, date over week (FC4)
        return "%s\nFW%d" % (d.strftime("%d.%m %a"), wk)

    def cell_text(self, v, frac, ne):
        """Hours and the load % together - the numbers, not only the %."""
        if not v:
            return ""
        if ne == "queue_n":
            return "%d" % v
        if self.t_pct.isChecked() and frac is not None:
            return "%.0f h · %d%%" % (v, round(frac * 100))
        return "%.0f h" % v

    def draw(self):
        data = self.state.get("last")
        if not data:
            return
        days = data["days"]
        if self.state["groups"]:
            want = {norm_text(g) for g in self.state["groups"]}
            groups = [g for g in data["groups"] if g in want]
        else:
            groups = list(data["groups"])
        self.paint_group_btn()
        self.paint_chips()
        if not groups:
            self.table.clear()
            self.note_lbl.setText("Nothing to forecast: no machine groups with both a capacity "
                                  "and work on the way.")
            return
        ne = self.show_sel.get()
        machine_mode = data["mode"] == "machine"
        week_h = sum(day_shift_hours(g) for g in range(7))
        shown = self.state.get("view_parts")

        def share(c, key):
            return sum(v for pk, v in (c.get(key) or {}).items() if pk in shown)

        def value(c):
            if ne == "queue_n":
                return share(c, "queue_n_by") if shown else c.get("queue_n", 0)
            if ne == "idle":
                return c.get("idle", 0.0)
            if ne == "busy":
                return share(c, "busy_by") if shown else c.get("busy", c.get("done", 0.0))
            if shown:
                return share(c, "busy_by") + share(c, "queue_by")
            return c.get("with_queue", c.get("waiting", 0.0))

        def load(c):
            return (c.get("with_queue", c.get("waiting", 0.0)) if machine_mode
                    else c.get("waiting", 0.0))

        if self.rows_sel.get() != "machine":
            self.table.row_base = 30
            self.table.cell_loadbar = {}
            self.draw_by_part(data, days, groups, ne, machine_mode)
            return
        # v69 (FC4): each day a bar of the load, a dashed line at 100%
        self.table.row_base = 42
        bars = {}
        pct_only = self.t_pct.isChecked() and ne in ("busy", "with_queue")
        DAY_W = 72
        cols = [("MACHINE GROUP", 140), ("MACHINES", 80), ("H/DAY", 70), ("H/WEEK", 74)] + [(self.fw_head(d), DAY_W) for d in days]
        rows, cbg, cfg, bolds = [], {}, {}, []
        for ri, g in enumerate(groups):
            kap = data["capacity"].get(g, 0.0)
            n_m = data["machines"].get(g, 0)
            n_p = data.get("people", {}).get(g, 0)
            unit = data.get("units", {}).get(g, 0.0)
            line = [g, ("%d" % n_m) if n_p == n_m else "%d / %d" % (n_m, n_p),
                    ("%.0f" % kap) if kap else "-", ("%.0f" % (unit * week_h)) if kap else "-"]
            for ci, d in enumerate(days, start=4):
                c = data["plan"].get((g, d))
                if not c:
                    line.append("")
                    continue
                v = value(c)
                if not c["capacity"]:
                    line.append(("%d" % v) if ne == "queue_n" else (("%.0f h" % v) if v else ""))
                    cbg[(ri, ci)] = ctx.TH["head_cell"]
                    continue
                frac = load(c) / c["capacity"]
                line.append(self.cell_text(v, frac, ne))
                if v:
                    cbg[(ri, ci)] = self.load_color(frac)
                    cfg[(ri, ci)] = self.load_text(frac)
                    bars[(ri, ci)] = (frac, ("%d%%" % round(frac * 100)) if pct_only
                                      else line[-1].split(" · ")[0])
            rows.append(line)
            bolds.append(False)
        tot_kap = sum(data["capacity"].get(g, 0.0) for g in groups)
        tot_unit = sum(data.get("units", {}).get(g, 0.0) for g in groups)
        total = ["ALL GROUPS", str(sum(data["machines"].get(g, 0) for g in groups)),
                 "%.0f" % tot_kap, "%.0f" % (tot_unit * week_h)]
        ri = len(rows)
        all_groups = len(groups) == len(data["groups"])
        for ci, d in enumerate(days, start=4):
            cells = [h for h in (data["plan"].get((g, d)) for g in groups) if h]
            v = sum(value(h) for h in cells)
            if ne == "queue_n" and all_groups:
                # one part waiting at two groups is still one part
                v = self.waiting_that_day(data, d)
            kap = sum(h["capacity"] for h in cells)
            frac = (sum(load(h) for h in cells) / kap) if kap else None
            total.append(self.cell_text(v, frac, ne))
            if frac is not None and v:
                cbg[(ri, ci)] = self.load_color(frac)
                cfg[(ri, ci)] = self.load_text(frac)
                bars[(ri, ci)] = (frac, ("%d%%" % round(frac * 100)) if pct_only
                                  else total[-1].split(" · ")[0])
        rows.append(total)
        bolds.append(True)
        # v70: a narrow dark column between factory weeks, as on Bottleneck
        brk = week_breaks(days, ctx.db.week_start("forecast"))
        cols, rows, (cbg, cfg, bars, gaps) = insert_gaps(4, brk, cols, rows, [cbg, cfg, bars])
        for rc in gaps:
            cbg[rc] = "#3A3F48" if ctx.TH["name"] != "day" else "#C9CFD8"
        self.table.cell_loadbar = bars
        self.table.view.horizontalHeader().setMinimumHeight(int(48 * self.table.zoom))
        self.table.set_data(cols, rows, ["a" if i % 2 == 0 else "b" for i in range(len(rows))],
                            bolds, frozen=4, align=["w"] + ["center"] * (len(cols) - 1),
                            cell_bg=cbg, cell_fg=cfg, row_sep=True)
        # the bar shows the %; the hours are on hover
        self.table.cell_tip = {(r, c): "%s  (%s)" % (rows[r][c], cols[c][0]) for r, c in bars}
        self.zoom.apply_zoom()

        # v73: no long explanation under the table - it squeezed the table
        self.note_lbl.setText("")
        ctx.status("Forecast   |   %d day(s) ahead   |   %d machine group(s)"
                   % (len(days), len(groups)))

    def waiting_that_day(self, data, d):
        shown = self.state.get("view_parts")
        if shown:
            return sum(n for (pk, day), n in (data.get("queue_n_part_day") or {}).items()
                       if day == d and pk in shown)
        return (data.get("queue_n_day") or {}).get(d, 0)

    def part_names(self):
        merge = ctx.db.merge_on("forecast")
        names = {pk: disp for pk, disp in ctx.db.wf_parts(merge)}
        groups = {}
        for p in ctx.db.all_parts(merge):
            groups[p["part_key"] or norm_text(p["part_no"])] = p["part_group"] or "(no part group)"
        return names, groups

    def draw_by_part(self, data, days, groups, ne, machine_mode):
        """The same days read by PART (or part group): capacity belongs to
        machines, so there is no capacity column and no percentage here."""
        if ne == "idle":
            self.table.clear()
            self.note_lbl.setText("MACHINE HOURS UNUSED is a machine's idle time - it belongs to "
                                  "a machine, not to a part, so it cannot be broken down by part.  "
                                  "Pick another SHOW option, or put ROWS back on machine group.")
            return
        names, group_of = self.part_names()
        by_part = self.rows_sel.get() == "part"

        def tag(pk):
            return names.get(pk, pk) if by_part else group_of.get(pk, "(no part group)")

        def shares(c):
            if ne == "queue_n":
                return c.get("queue_n_by") or {}
            if ne == "busy":
                return c.get("busy_by") or {}
            pay = dict(c.get("busy_by") or {})
            for pk, h in (c.get("queue_by") or {}).items():
                pay[pk] = pay.get(pk, 0.0) + h
            return pay

        shown = self.state.get("view_parts")
        all_groups = len(groups) == len(data["groups"])
        totals, tags = {}, set()
        if ne == "queue_n" and all_groups:
            for (pk, d), n in (data.get("queue_n_part_day") or {}).items():
                if shown and pk not in shown:
                    continue
                e = tag(pk)
                tags.add(e)
                totals[(e, d)] = totals.get((e, d), 0) + n
        else:
            for g in groups:
                for d in days:
                    c = data["plan"].get((g, d))
                    if not c:
                        continue
                    for pk, v in shares(c).items():
                        if shown and pk not in shown:
                            continue
                        e = tag(pk)
                        tags.add(e)
                        totals[(e, d)] = totals.get((e, d), 0) + v
        if not tags:
            self.table.clear()
            self.note_lbl.setText("Nothing waiting in this window for the machine groups selected.")
            return
        count = ne == "queue_n"
        order = sorted(tags, key=lambda e: (-max([totals.get((e, d), 0) for d in days] or [0])
                                            if count else
                                            -sum(totals.get((e, d), 0) for d in days), e))
        title = "PART" if by_part else "PART GROUP"
        # days of a count are not added up: the summary is the WORST DAY
        cols = [(title, 210), ("WORST DAY" if count else "TOTAL", 90)] + \
            [(self.fw_head(d), 72) for d in days]
        self.table.view.horizontalHeader().setMinimumHeight(int(48 * self.table.zoom))

        def fmt(v):
            return "" if not v else (("%d" % v) if count else "%.0f h" % v)

        def summary(vals):
            vals = list(vals)
            return (max(vals) if count else sum(vals)) if vals else 0
        rows, bolds = [], []
        for e in order:
            rows.append([e, fmt(summary(totals.get((e, d), 0) for d in days))] +
                        [fmt(totals.get((e, d), 0)) for d in days])
            bolds.append(False)
        daily = [sum(totals.get((e, d), 0) for e in order) for d in days]
        rows.append(["ALL %sS" % title, fmt(summary(daily))] + [fmt(v) for v in daily])
        bolds.append(True)
        brk = week_breaks(days, ctx.db.week_start("forecast"))
        cols, rows, (gaps,) = insert_gaps(2, brk, cols, rows, [])
        cbg = {rc: ("#3A3F48" if ctx.TH["name"] != "day" else "#C9CFD8") for rc in gaps}
        self.table.cell_loadbar = {}
        self.table.set_data(cols, rows, ["a" if i % 2 == 0 else "b" for i in range(len(rows))],
                            bolds, frozen=2, align=["w"] + ["center"] * (len(cols) - 1),
                            row_sep=True, cell_bg=cbg)
        self.zoom.apply_zoom()
        self.note_lbl.setText("")

    # ---------------------------------------------------------------
    def do_export(self):
        data = self.state.get("last")
        if not data or not data["groups"]:
            QMessageBox.warning(self, "Nothing to export", "Run a forecast first.")
            return
        path, _x = QFileDialog.getSaveFileName(
            self, "Export", "Forecast_%s.xlsx" % datetime.date.today().isoformat(),
            "Excel (*.xlsx)")
        if not path:
            return
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "FORECAST"
            for key, title in (("busy", "CAPACITY USED"), ("with_queue", "WITH BACKLOG"),
                               ("idle", "MACHINE HOURS UNUSED"), ("queue_n", "PARTS WAITING")):
                ws.append([title])
                ws.append(["MACHINE GROUP", "MACHINES", "CAPACITY h/day"] +
                          [d.isoformat() for d in data["days"]])
                for g in data["groups"]:
                    ws.append([g, data["machines"].get(g, 0), round(data["capacity"].get(g, 0.0), 1)]
                              + [round((data["plan"].get((g, d)) or {}).get(key, 0), 1)
                                 for d in data["days"]])
                ws.append([])
            wb.save(path)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        ctx.status("Exported: " + path)

    def refresh_theme(self):
        self.paint_legend()
        if self.built:
            self.draw()

    def annotations_changed(self):
        if self.built:
            self.schedule()

# -*- coding: utf-8 -*-
"""LEAD TIME ANALYSIS - the Qt page (2.2.0).

What used to be the LEAD TIME / QUEUE / FLOW EFFICIENCY module of the
WATERFALL page, on a page of its own.  The figures are worked out exactly
as before (wip_core.lead_time_analysis: completion to completion in clock
time, cutting = machine time or - where there is none - labour).

Two modules side by side:

    STEPS             one part, one row per operation of its route
    OPERATION GROUPS  named stretches of a route (TURN = 130 -> 150), over
                      one or several parts; the group's own average and
                      each part's beside it.  The sets are saved for
                      everybody (wip_core.lt_groups_*).

The date range picks the data one of three ways: the operation records
inside it, the serials seen inside it (all their records), or the serials
SOLD inside it (all their records).  Shown in days; hours on request.
"""

import re
import traceback

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QMessageBox, QFileDialog,
                               QComboBox, QLineEdit, QDialog, QScrollArea, QFrame)

from wip_core import (ALL, export_leadtime_to_excel, format_wf_hours, lead_time_analysis,
                      lead_group_analysis, lt_groups_load, lt_groups_save, lt_groups_delete,
                      norm_text, route_step, time_mode_to_date_range, save_prefs)
from qt_widgets import (ctx, ModuleCard, DataTable, FilterBar, Toggle, Seg, FlowLayout,
                        label, button, hbox, autofit_columns, KpiInline, ModuleGrid, page_layout,
                        page_head, export_button)

# lead blue, cutting green, queue yellow - one set per theme
LT_SOFT_NIGHT = {"lead": "#6FA8FF", "cut": "#4CC67A", "queue": "#E8B53E"}
LT_SOFT_DAY = {"lead": "#1F63D6", "cut": "#1E8E46", "queue": "#A87400"}
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
#  THE EDITOR OF ONE SET OF OPERATION GROUPS
# =====================================================================
class GroupEditor(QDialog):
    """One line per stretch: GROUP name, PART, FROM op, TO op.  Lines with
    the same group name are one group (several parts, or several stretches
    of one part)."""

    def __init__(self, parent, name, definition, parts):
        super().__init__(parent)
        self.setWindowTitle("Operation groups")
        if ctx.sheet:
            self.setStyleSheet(ctx.sheet)
        self.resize(900, 520)
        self.parts = list(parts)                  # [(part key, shown name)]
        self.old_name = name
        self.result_name, self.result_def = None, None
        self.lines = []
        self.routes = {}

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self.name_ed = QLineEdit(name or "")
        self.name_ed.setPlaceholderText("e.g. STG1 BLISK")
        self.name_ed.setMinimumWidth(280)
        lay.addWidget(hbox(label("Analysis name", muted=True), self.name_ed, None))
        lay.addWidget(label("One line per stretch of a route.  Lines with the same group name "
                            "are shown together - the group's lead time is the average over "
                            "every serial of its parts.  The stretch runs from the completion "
                            "of FROM to the completion (X) of TO.", muted=True, wrap=True))
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 4, 0)
        head.setSpacing(8)
        for txt, st in (("GROUP", 2), ("PART", 3), ("FROM OP", 3), ("TO OP", 3)):
            head.addWidget(label(txt, section=True), st)
        head.addSpacing(40)
        lay.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.host = QWidget()
        self.host_lay = QVBoxLayout(self.host)
        self.host_lay.setContentsMargins(4, 0, 4, 0)
        self.host_lay.setSpacing(6)
        self.host_lay.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.host)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget "
                                  "{ background: transparent; }")
        lay.addWidget(self.scroll, 1)
        self.prob = label("", wrap=True)
        lay.addWidget(self.prob)
        lay.addWidget(hbox(button("+ Add line", slot=lambda: self.add_line()), None,
                           button("Cancel", slot=self.reject),
                           button("Save", primary=True, slot=self.save)))
        for g in (definition or {}).get("groups") or []:
            for rg in g.get("ranges") or []:
                self.add_line(g.get("name") or "", rg.get("part_key"), rg.get("from"),
                              rg.get("to"))
        if not self.lines:
            self.add_line()

    def route(self, pk):
        if pk not in self.routes:
            self.routes[pk] = ctx.db.wf_traveler_for(pk) if pk else []
        return self.routes[pk]

    def group_names(self):
        out = []
        for ln in self.lines:
            t = ln["grp"].currentText().strip()
            if t and t not in out:
                out.append(t)
        return out

    def add_line(self, grp=None, pk=None, a=None, b=None):
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        g = QComboBox()
        g.setEditable(True)
        g.addItems(self.group_names())
        if grp is None and self.lines:            # a new line continues the last group
            grp = self.lines[-1]["grp"].currentText()
            pk = pk or None
        g.setEditText(grp or "")
        p = QComboBox()
        p.setEditable(True)                       # type to find a part
        p.setInsertPolicy(QComboBox.NoInsert)
        for key, disp in self.parts:
            p.addItem(disp, key)
        fa, fb = QComboBox(), QComboBox()
        for cb, st in ((g, 2), (p, 3), (fa, 3), (fb, 3)):
            cb.setMinimumWidth(90)
            rl.addWidget(cb, st)
        ln = {"row": row, "grp": g, "part": p, "from": fa, "to": fb}
        rm = button("×", icon_only=True, tip="Remove this line",
                    slot=lambda: self.remove_line(ln))
        rl.addWidget(rm)
        self.lines.append(ln)
        self.host_lay.addWidget(row)
        i = p.findData(pk) if pk else -1
        if pk and i < 0:                          # a part that lost its route
            p.addItem("%s  (no route)" % pk, pk)
            i = p.count() - 1
        p.setCurrentIndex(i if i >= 0 else (0 if p.count() else -1))
        self.fill_ops(ln, a, b)
        p.currentIndexChanged.connect(lambda _i: self.fill_ops(ln))
        return ln

    def remove_line(self, ln):
        if ln in self.lines:
            self.lines.remove(ln)
            ln["row"].hide()
            ln["row"].deleteLater()

    def fill_ops(self, ln, a=None, b=None):
        pk = ln["part"].currentData()
        route = self.route(pk)
        for cb, want, last in ((ln["from"], a, False), (ln["to"], b, True)):
            cb.blockSignals(True)
            cb.clear()
            for c in route:
                txt = str(c.get("label") or c["op"])
                if c.get("desc"):
                    txt += "  ·  %s" % c["desc"]
                cb.addItem(txt, route_step(c))
            i = cb.findData(want) if want else -1
            if i < 0 and cb.count():
                i = cb.count() - 1 if last else 0
            cb.setCurrentIndex(i)
            cb.blockSignals(False)

    def save(self):
        name = self.name_ed.text().strip()
        if not name:
            self.prob.setText("Give the analysis a name.")
            return
        groups, order, bad = {}, [], []
        for n, ln in enumerate(self.lines, start=1):
            g = ln["grp"].currentText().strip()
            pk = ln["part"].currentData()
            a, b = ln["from"].currentData(), ln["to"].currentData()
            if not g:
                bad.append("line %d has no group name" % n)
                continue
            if not pk or a is None or b is None:
                bad.append("line %d (%s): pick a part and its operations" % (n, g))
                continue
            if ln["from"].currentIndex() >= ln["to"].currentIndex():
                bad.append("line %d (%s): TO must come after FROM on the route" % (n, g))
                continue
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append({"part_key": pk, "from": a, "to": b})
        if bad:
            self.prob.setText("<span style='color:%s;'>%s</span>"
                              % (ctx.TH["danger"], "<br>".join(bad)))
            return
        if not order:
            self.prob.setText("Add at least one line.")
            return
        if name != self.old_name and name in lt_groups_load(ctx.db):
            if QMessageBox.question(self, "Operation groups",
                                    "'%s' already exists. Replace it?" % name) \
                    != QMessageBox.Yes:
                return
        self.result_name = name
        self.result_def = {"groups": [{"name": g, "ranges": groups[g]} for g in order]}
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
        self.k_cut = KpiInline("CUTTING", None, compact=True)
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

        # ---- the two modules ----
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
        self.table.fit_rows(24)
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
        self.gcard.content_lay.addWidget(srow)
        self.gtable = DataTable()
        self.gtable.tiles = True
        self.gtable.fit_width = True
        self.gtable.fit_rows(24)
        self.gcard.content_lay.addWidget(self.gtable, 1)
        self.gnote = label("", muted=True, wrap=True)
        self.gcard.content_lay.addWidget(self.gnote)
        self.gcard.setMinimumWidth(420)

        self.grid.add(self.steps, wide=True, weight=3)
        self.grid.add(self.gcard, wide=True, weight=3)
        self.steps.show()
        self.gcard.show()
        self.grid.relayout()
        self.paint_kpis()

    # ---------------------------------------------------------------
    def paint_kpis(self):
        s = soft()
        for k, key in ((self.k_lead, "lead"), (self.k_cut, "cut"), (self.k_queue, "queue"),
                       (self.k_eff, "cut")):
            k.color = s[key]
            k.paint()

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
        if not force and not self.isVisible() and not (self.steps.detached
                                                       or self.gcard.detached):
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
        self.last["data"] = data
        self.show_steps(data, unit, pk)

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
                ("CUTTING (%s)" % unit, 100), ("QUEUE (%s)" % unit, 110),
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
        eff = data.get("flow_efficiency")
        if eff is not None:
            self.k_lead.set("%s %s" % (format_wf_hours(data["total_lead"], unit), u))
            self.k_cut.set("%s %s" % (format_wf_hours(data["total_cut"], unit), u))
            self.k_queue.set("%s %s" % (format_wf_hours(data["total_queue"], unit), u))
            self.k_eff.set("%.1f%%" % (eff * 100.0))
        else:
            for k in (self.k_lead, self.k_cut, self.k_queue, self.k_eff):
                k.set("-")
        if data.get("n_overlap"):
            self.note.setText('<span style="color:%s;">* %d step(s) overlap - counted as all '
                              'cutting</span>' % (ctx.TH["muted"], data["n_overlap"]))
            self.note.show()
        else:
            self.note.hide()
        self.steps.title_lbl.setText("Steps · %s" % self.last["part"])
        self.steps.set_sub("%d serials · each step from the operation above it"
                           % data["n_serials"])

    # ---- OPERATION GROUPS ----
    def show_groups(self, groups, unit, name):
        if not groups:
            self.gtable.clear()
            self.gnote.setText("No analysis yet - press New, name your groups (TURN, MILL ...) "
                               "and give each part its stretch of the route (130 → 150)."
                               if not name else "This analysis has no groups.")
            self.gnote.show()
            return
        self.gnote.hide()
        night = ctx.TH["name"] != "day"
        s_ = soft()
        names = dict(self.all_parts())
        cols = [("GROUP", 110), ("PART", 150), ("FROM → TO", 120),
                ("LEAD TIME (%s)" % unit, 110), ("CUTTING (%s)" % unit, 100),
                ("QUEUE (%s)" % unit, 110), ("FLOW EFFICIENCY", 120), ("SERIALS", 70)]
        avgs = [g["avg_hours"] for g in groups if g["avg_hours"] is not None]
        lo, hi = (min(avgs), max(avgs)) if avgs else (0, 0)
        rows, bg, bold, cbg, cfg, bars, tips = [], [], [], {}, {}, {}, {}

        def line(vals, d, is_group, k):
            ri = len(rows)
            rows.append(vals)
            bg.append("sub" if is_group else ("a" if ri % 2 == 0 else "b"))
            bold.append(is_group)
            if d["avg_hours"] is not None:
                cbg[(ri, 3)], cfg[(ri, 3)] = lt_chip(s_["lead"], k, night)
                cbg[(ri, 4)], cfg[(ri, 4)] = lt_chip(s_["cut"], 0.15, night)
                cbg[(ri, 5)], cfg[(ri, 5)] = lt_chip(s_["queue"], k, night)
            if d["efficiency"] is not None:
                bars[(ri, 6)] = (max(0.0, min(1.0, d["efficiency"])), s_["cut"])

        def figures(d):
            return [format_wf_hours(d["avg_hours"], unit), format_wf_hours(d["cut_hours"], unit),
                    format_wf_hours(d["queue_hours"], unit),
                    eff_text(d["efficiency"], d.get("overlap")),
                    str(d["n"]) if d["n"] else ""]
        for g in groups:
            k = 0.5
            if g["avg_hours"] is not None and hi > lo:
                k = (g["avg_hours"] - lo) / float(hi - lo)
            n_parts = len({p["part_key"] for p in g["parts"]})
            line([g["name"], "%d part%s" % (n_parts, "" if n_parts == 1 else "s"), ""]
                 + figures(g), g, True, k)
            for p in g["parts"]:
                p["part"] = names.get(p["part_key"], p["part_key"])
                vals = ["", p["part"], "%s → %s" % (p["from_label"], p["to_label"])] + figures(p)
                if p["problem"]:
                    vals[-1] = "!"
                    tips[(len(rows), 7)] = p["problem"]
                    tips[(len(rows), 1)] = p["problem"]
                line(vals, p, False, 0.15)
        self.gtable.cell_bar = bars
        self.gtable.set_data(autofit_columns(cols, rows), rows, bg, bold,
                             align=["w", "w", "center"] + ["center"] * 5, cell_bg=cbg,
                             cell_fg=cfg)
        self.gtable.cell_tip = tips

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
            self.show_groups(self.last["groups"], self.unit.get(), self.last["set"])

    def annotations_changed(self):
        if self.built:
            self.schedule()

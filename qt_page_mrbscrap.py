# -*- coding: utf-8 -*-
"""MRB / SCRAP / TECO - the Qt page (new design).

Same behaviour as the Tk page (page_mrbscrap.py):
  * TOTALS: one row of boxes; MRB = entry not made yet, with ENTERED and
    FIXED under it; SCRAP shows REMOVED (taken off the books); BY PART
    GROUP opens from the button at the end of the row.
  * The list: TYPE / PART / SERIAL / REASON / DATE / LAST OP DATE, one block
    per type, FIXED left out by default (EXCLUDE FIXED lit).
  * On the right: the form for the picked serial (part from the data, or
    typed by hand for an old serial), its OUTCOME, and its rework history
    (folded away until asked for).
  * UNDO / REDO of every SAVE, REMOVE FLAG and RESOLVE since the page opened.
"""

import datetime
import os

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QLabel,
                               QComboBox, QLineEdit, QPlainTextEdit, QSplitter, QMessageBox,
                               QFileDialog, QPushButton, QScrollArea)

from wip_core import (ALL, USER_NOTE_COLORS, allowed_part_keys, parts_for_serials,
                      materials_for_serials, match_part, export_flags_to_excel,
                      fmt_hour, lookups, norm_text, parse_date, serial_history,
                      time_mode_to_date_range)
import qt_theme
from qt_widgets import (ModuleCard, ctx, Card, DataTable, FilterBar, Seg, Toggle, label, button,
                        hbox, autofit_columns, page_head, IconButton, DateField)

KINDS = ("teco", "mrb", "scrap", "repair", "note")
ALL_KINDS = ("teco", "mrb", "mrbent", "scrap", "repair", "note", "fixed")
BOX_KINDS = KINDS + ("fixed",)
KIND_TITLE = {"teco": "TECO", "mrb": "MRB", "mrbent": "MRB ENTERED",
              "scrap": "SCRAP", "repair": "REPAIR", "note": "NOTE", "fixed": "FIXED"}
LIST_ORDER = {k: i for i, k in enumerate(ALL_KINDS)}
FIXED_UNDER = ("teco", "mrb", "scrap", "repair")
SUB_TITLE = {"scrap": "REMOVED"}
GROUP_COLS = [("teco", "TECO"), ("mrb", "MRB"), ("mrbent", "MRB ENT."),
              ("scrap", "SCRAP"), ("repair", "REPAIR"), ("fixed", "FIXED"),
              ("removed", "REMOVED")]
RESOLVE_OPTIONS = {
    "teco": [("Fixed", "fixed"), ("Became MRB", "mrb"), ("Became SCRAP", "scrap")],
    "mrb": [("Entry done", "entered"), ("Accept", "accept"), ("Reject", "reject"),
            ("Repair", "repair")],
    "mrbent": [("Accept", "accept"), ("Reject", "reject"), ("Repair", "repair"),
               ("Entry undone", "undo")],
    "scrap": [("Removed from system", "removed")],
    "repair": [("Completed", "completed")],
}
RESOLVE_TARGET = {
    "teco": {"fixed": "fixed", "mrb": "mrb", "scrap": "scrap"},
    "mrb": {"entered": "mrbent", "accept": "fixed", "reject": "scrap", "repair": "repair"},
    "mrbent": {"accept": "fixed", "reject": "scrap", "repair": "repair", "undo": "mrb"},
    "scrap": {"removed": "fixed"},
    "repair": {"completed": "fixed"},
}
LIST_COLS = [("TYPE", 110), ("PART", 120), ("SERIAL NO", 110), ("REASON", 320),
             ("DATE", 90), ("LAST OP", 70), ("LAST OP DATE", 100), ("IDLE DAYS", 80)]
SERIAL_COL = 2
DETAIL_COLS = [("REJECT OP", 80), ("REJECT OP DESCRIPTION", 190), ("RW HOUR", 70),
               ("RW MACHINE", 90), ("MACHINE DESCRIPTION", 190), ("RW DATE", 95)]


class KpiBox(QFrame):
    """One TOTALS box: title + number on the left, its sub-lines on the right."""

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.setProperty("card", True)
        g = QGridLayout(self)
        g.setContentsMargins(12, 8, 12, 8)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(0)
        self.title = label(KIND_TITLE.get(kind, "ALL OPEN"), section=True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Show only this type in the list (click again for all)")
        self.num = QLabel("0")
        self.ent = QLabel("")
        self.fix = QLabel("")
        for w in (self.ent, self.fix):
            w.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        g.addWidget(self.title, 0, 0)
        g.addWidget(self.num, 1, 0, 2, 1)
        g.addWidget(self.ent, 1, 1)
        g.addWidget(self.fix, 2, 1)
        g.setColumnStretch(1, 1)
        self.paint()

    def mousePressEvent(self, ev):
        if callable(getattr(self, "on_click", None)):
            self.on_click(self.kind)

    def paint(self):
        th = ctx.TH
        if self.kind == "all":
            self.setStyleSheet("KpiBox { border-top: 3px solid %s; }" % th["muted"])
            self.num.setStyleSheet("font-size: 20pt; font-weight: 700; color: %s;" % th["text"])
            return
        col = qt_theme.FLAG_COLORS.get(self.kind)
        self.setStyleSheet("KpiBox { border-top: 3px solid %s; }" % col)
        self.num.setStyleSheet("font-size: 20pt; font-weight: 700; color: %s;"
                               % ctx.flag_text(self.kind))
        self.ent.setStyleSheet("font-size: 10pt; font-weight: 700; color: %s;"
                               % ctx.flag_text("mrbent"))
        self.fix.setStyleSheet("font-size: 10pt; font-weight: 700; color: %s;"
                               % (ctx.flag_text("removed") if self.kind == "scrap"
                                  else ctx.flag_text("fixed")))


class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.state = {"editing": None, "part_data": None}
        self.history = {"undo": [], "redo": []}
        self.built = False
        ctx.hooks["mrbscrap_load_serial"] = self.load_serial_from_elsewhere
        ctx.hooks["mrbscrap_refresh"] = self.refresh_all

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 18, 26, 14)
        outer.setSpacing(12)
        page_head(outer, "MRB / Scrap / TECO", "Flagged serials, their outcome and counts by part group")

        # ---- filters ----
        self.bar = FilterBar(week_start_fn=lambda: ctx.db.week_start("mrbscrap"),
                             part_multi=True)
        self.bar.exclude_flags["fixed"].setChecked(True)
        self.bar.exclude_flags["removed"].setChecked(True)
        self.bar.paint_chips()
        self.bar.remember("mrbscrap")          # 2.1.1: last filters per account
        self.bar.changed.connect(self.refresh_all)
        self.bar.eng_grp_changed.connect(self.cascade)
        outer.addWidget(self.bar)

        # ---- TOTALS row ----
        krow = QHBoxLayout()
        krow.setSpacing(8)
        self.boxes = {}
        for k in ("all",) + BOX_KINDS:
            b = KpiBox(k)
            b.on_click = self.box_clicked
            self.boxes[k] = b
            krow.addWidget(b, 1)
        self.grp_btn = button("By part group  ▾", slot=self.toggle_groups)
        self.grp_btn.setMinimumHeight(60)
        krow.addWidget(self.grp_btn)
        outer.addLayout(krow)
        self.grp_pop = QFrame(None, Qt.Popup)
        self.grp_pop.setProperty("popup", True)
        gl = QVBoxLayout(self.grp_pop)
        gl.setContentsMargins(14, 10, 14, 12)
        gl.addWidget(label("BY PART GROUP", section=True))
        self.grp_grid_host = QWidget()
        self.grp_grid = QGridLayout(self.grp_grid_host)
        self.grp_grid.setContentsMargins(0, 4, 0, 0)
        self.grp_grid.setHorizontalSpacing(14)
        self.grp_grid.setVerticalSpacing(3)
        gl.addWidget(self.grp_grid_host)
        self.grp_rows = []

        # ---- list | right panel ----
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)
        self.list_card = ModuleCard("Flagged serials")
        self.find = QLineEdit()
        self.find.setPlaceholderText("Search serial or part")
        self.find.setFixedWidth(240)
        self.find.textChanged.connect(lambda _t: self.refresh_list_only())
        self.list_card.add_extra(self.find)
        self.list_card.add_extra(IconButton("download", "Export to Excel", self.export_flags, text="  Export"))
        self.table = DataTable()
        self.table.serial_col = SERIAL_COL
        self.table.stripes = True
        self.table.dot_col0 = True
        self.table.on_row_click = lambda ri, row: row[SERIAL_COL] and self.load_serial(row[SERIAL_COL])
        self.list_card.content_lay.addWidget(self.table, 1)
        split.addWidget(self.list_card)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        right.setObjectName("Page")
        rscroll = QScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.viewport().setObjectName("Page")
        rscroll.setWidget(right)
        rscroll.setMinimumWidth(430)
        split.addWidget(rscroll)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 2)
        split.setSizes([1000, 460])
        rl.addWidget(self._build_form())
        rl.addWidget(self._build_outcome())
        rl.addWidget(self._build_history())
        rl.addStretch(1)
        self.part_timer = QTimer(self)
        self.part_timer.setSingleShot(True)
        self.part_timer.timeout.connect(lambda: self.sync_part_field())
        self.paint_history()
        self.sync_outcome_ui()

    # =================================================================
    #  FORM
    # =================================================================
    def _build_form(self):
        card = Card("Edit serial")
        self.type_badge = label("", section=True)
        card.add_action(self.type_badge)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(4)
        self.serial_cb = QComboBox()
        self.serial_cb.setEditable(True)
        self.serial_cb.lineEdit().setPlaceholderText("serial number")
        self.serial_cb.lineEdit().textEdited.connect(lambda _t: self.part_timer.start(350))
        self.serial_cb.activated.connect(lambda _i: self.part_timer.start(10))
        self._serials_loaded = False
        orig_popup = self.serial_cb.showPopup

        def popup():
            if not self._serials_loaded:
                cur = self.serial_cb.currentText()
                self.serial_cb.addItems(ctx.db.all_serial_numbers()[:5000])
                self.serial_cb.setEditText(cur)
                self._serials_loaded = True
            orig_popup()
        self.serial_cb.showPopup = popup
        # v73: a click opens the same calendar as the date filters
        self.date_ed = DateField()
        self.date_ed.setText(datetime.date.today().isoformat())
        g.addWidget(label("Serial", muted=True), 0, 0)
        g.addWidget(label("Date", muted=True), 0, 1)
        g.addWidget(self.serial_cb, 1, 0)
        g.addWidget(self.date_ed, 1, 1)
        self.part_cb = QComboBox()
        self.part_cb.setEditable(True)
        self._parts_loaded = False
        orig_ppop = self.part_cb.showPopup

        def ppop():
            if not self._parts_loaded:
                cur = self.part_cb.currentText()
                self.part_cb.addItems(sorted({p["part_no"] for p in ctx.db.all_parts(False)
                                              if p.get("part_no")}))
                self.part_cb.setEditText(cur)
                self._parts_loaded = True
            orig_ppop()
        self.part_cb.showPopup = ppop
        self.part_note = label("", muted=True)
        g.addWidget(label("Part", muted=True), 2, 0)
        g.addWidget(self.part_cb, 3, 0, 1, 2)
        g.addWidget(self.part_note, 4, 0, 1, 2)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        wrap = QWidget()
        wrap.setLayout(g)
        card.add(wrap)
        card.add(label("Type", muted=True))
        self.type_seg = Seg([(KIND_TITLE[k], k) for k in KINDS], "teco",
                            slot=self.sync_outcome_ui)
        card.add(self.type_seg)
        self.type_value = "teco"
        card.add(label("Reason", muted=True))
        self.reason = QPlainTextEdit()
        self.reason.setFixedHeight(70)
        card.add(self.reason)
        card.add(label("My note (only you)", muted=True))
        self.note_ed = QLineEdit()
        card.add(self.note_ed)
        sw = QHBoxLayout()
        sw.setSpacing(4)
        self.note_color = USER_NOTE_COLORS[0][1]
        self.swatches = []
        for _lbl, hexv in USER_NOTE_COLORS:
            b = QPushButton("")
            b.setFixedSize(22, 22)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, h=hexv: self.pick_note_color(h))
            sw.addWidget(b)
            self.swatches.append((b, hexv))
        self.note_private = Toggle("Hide from others")
        sw.addSpacing(10)
        sw.addWidget(self.note_private)
        sw.addStretch(1)
        swh = QWidget()
        swh.setLayout(sw)
        card.add(swh)
        self.pick_note_color(self.note_color)
        self.undo_btn = button("Undo", slot=self.do_undo)
        self.redo_btn = button("Redo", slot=self.do_redo)
        card.add(hbox(button("Save", primary=True, slot=self.do_save),
                      button("Remove flag", danger=True, slot=self.do_delete),
                      button("Clear", slot=self.clear_form), None,
                      self.undo_btn, self.redo_btn, spacing=6))
        return card

    def pick_note_color(self, hexv):
        self.note_color = hexv
        for b, hv in self.swatches:
            on = hv == hexv
            b.setStyleSheet("QPushButton { background: %s; border: %dpx solid %s; border-radius: 5px; }"
                            % (hv, 3 if on else 1, ctx.TH["text"] if on else ctx.TH["border"]))

    def kind(self):
        """The type of the serial being edited (MRB ENTERED is not a
        button - it is an MRB outcome - so it is kept here)."""
        return self.type_value if self.type_value == "mrbent" else self.type_seg.get()

    def _build_outcome(self):
        card = Card("Outcome")
        self.outcome_head = label("", muted=True, wrap=True)
        card.add(self.outcome_head)
        self.outcome_boxes = {}
        for k, opts in RESOLVE_OPTIONS.items():
            seg = Seg(opts, opts[0][1])
            self.outcome_boxes[k] = seg
            card.add(seg)
        card.add(hbox(button("Mark resolved", primary=True, slot=self.do_resolve), None))
        return card

    def _build_history(self):
        self.hist_card = Card()
        self.hist_btn = button("Rework history  ▾", slot=self.toggle_history)
        self.hist_card.add(self.hist_btn)
        self.hist_head = label("No serial selected.", muted=True, wrap=True)
        self.hist_table = DataTable()
        self.hist_table.setMinimumHeight(180)
        self.hist_card.add(self.hist_head)
        self.hist_card.add(self.hist_table)
        self.hist_head.hide()
        self.hist_table.hide()
        self.hist_open = False
        self.hist_serial = ""
        return self.hist_card

    def toggle_history(self):
        self.hist_open = not self.hist_open
        self.hist_head.setVisible(self.hist_open)
        self.hist_table.setVisible(self.hist_open)
        self.hist_btn.setText("Rework history  ▴" if self.hist_open else "Rework history  ▾")
        if self.hist_open:
            self.show_history(self.hist_serial)

    def show_history(self, serial_no):
        self.hist_serial = serial_no or ""
        if not self.hist_open:
            return
        if not norm_text(serial_no or ""):
            self.hist_head.setText("No serial selected.")
            self.hist_table.clear()
            return
        recs = [r for r in serial_history(ctx.db, serial_no) if r["rw_hour"]]
        rows = [[r["reject_op"], r["reject_op_desc"], fmt_hour(r["rw_hour"]), r["wc_code"],
                 r["wc_name"], r["rw_date"]] for r in recs]
        if rows:
            tot = sum(r["rw_hour"] or 0.0 for r in recs)
            self.hist_head.setText("%s  -  %d rework entry(s), %s in total."
                                   % (serial_no, len(recs), fmt_hour(tot)))
            self.hist_table.set_data(DETAIL_COLS, rows, None, None,
                                     align=["w", "w", "e", "w", "w", "center"])
        else:
            self.hist_head.setText("%s  -  no rework on file." % serial_no)
            self.hist_table.clear()

    def sync_outcome_ui(self):
        if self.type_seg.get() != "mrb" or self.type_value != "mrbent":
            self.type_value = self.type_seg.get()
        t = self.kind()
        for k, seg in self.outcome_boxes.items():
            seg.setVisible(k == t)
        ed = self.state["editing"]
        if t not in RESOLVE_OPTIONS:
            self.outcome_head.setText("%s has no outcome." % KIND_TITLE.get(t, t))
        elif ed:
            self.outcome_head.setText("%s  -  %s" % (ed, KIND_TITLE.get(t, t)))
        else:
            self.outcome_head.setText("Pick a %s serial from the list." % KIND_TITLE.get(t, t))
        self.type_badge.setText("●  " + KIND_TITLE.get(t, t))
        self.type_badge.setStyleSheet("color: %s;" % ctx.flag_text(t))

    # =================================================================
    #  PART: from the data, or typed by hand
    # =================================================================
    def data_part_of(self, serial_no):
        sk = norm_text(serial_no or "")
        if not sk:
            return None
        idx, _c, _o = lookups(ctx.db, ctx.db.merge_on("mrbscrap"))
        part = parts_for_serials(ctx.db, idx, [sk]).get(sk)
        return (part or {}).get("part_no")

    def sync_part_field(self, manual=None):
        serial = self.serial_cb.currentText()
        found = self.data_part_of(serial)
        before = self.state["part_data"]
        self.state["part_data"] = found
        if before and not found and self.part_cb.currentText() == before:
            self.part_cb.setEditText("")
        if found:
            self.part_cb.setEditText(found)
            self.part_cb.setEnabled(False)
            self.part_note.setText("From data")
        else:
            self.part_cb.setEnabled(True)
            if manual is not None:
                self.part_cb.setEditText(manual)
            self.part_note.setText("Not in data - type it" if serial.strip() else "")

    # =================================================================
    #  LOAD / CLEAR / SAVE / DELETE / RESOLVE
    # =================================================================
    def clear_form(self):
        self.state["editing"] = None
        self.state["part_data"] = None
        self.serial_cb.setEditText("")
        self.part_cb.setEditText("")
        self.part_cb.setEnabled(True)
        self.part_note.setText("")
        self.reason.setPlainText("")
        self.date_ed.setText(datetime.date.today().isoformat())
        self.mrb_prob = ""
        self.scrap_prob = ""
        self.type_value = "teco"
        self.type_seg.set("teco")
        self.note_ed.setText("")
        self.pick_note_color(USER_NOTE_COLORS[0][1])
        self.note_private.setChecked(False)
        self.sync_outcome_ui()

    def load_serial(self, serial_no):
        flag = ctx.db.flag_get(norm_text(serial_no))
        if flag:
            self.state["editing"] = flag["serial_key"]
            k = flag["kind"]
            self.type_value = k
            self.type_seg.set("mrb" if k == "mrbent" else (k if k in KINDS else "teco"))
            self.serial_cb.setEditText(flag["serial_no"])
            self.reason.setPlainText(flag["reason"] or "")
            self.date_ed.setText(flag["entry_date"] or "")
            self.mrb_prob = flag["mrb_prob"] or ""
            self.scrap_prob = flag["scrap_prob"] or ""
        else:
            self.clear_form()
            self.serial_cb.setEditText(serial_no)
        self.part_timer.stop()
        self.part_cb.setEditText("")
        self.sync_part_field(manual=(flag or {}).get("part_no") or "")
        self.sync_outcome_ui()
        me = ctx.username()
        nrow = (ctx.db.user_note_row(norm_text(serial_no), me) if me
                else {"note": "", "color": USER_NOTE_COLORS[0][1], "private": 0})
        self.note_ed.setText(nrow["note"] or "")
        self.pick_note_color(nrow["color"] or USER_NOTE_COLORS[0][1])
        self.note_private.setChecked(bool(nrow.get("private")))
        self.show_history(serial_no)

    def load_serial_from_elsewhere(self, serial_no):
        ctx.hooks["show_page"](ctx.hooks["page_index"]("mrb"))
        self.load_serial(serial_no)

    def recorded(self, label_, sk, action):
        me = ctx.username()
        before = ctx.db.flag_snapshot(sk, me)
        action()
        after = ctx.db.flag_snapshot(sk, me)
        if before["flag"] != after["flag"] or before["note"] != after["note"]:
            self.history["undo"].append({"label": label_, "before": before, "after": after})
            self.history["redo"].clear()
        self.paint_history()

    def paint_history(self):
        for stack, btn, name in ((self.history["undo"], self.undo_btn, "Undo"),
                                 (self.history["redo"], self.redo_btn, "Redo")):
            btn.setText("%s (%d)" % (name, len(stack)) if stack else name)
            btn.setEnabled(bool(stack))

    def step_back(self, src, dst, put, expect, verb):
        if not src:
            return
        e = src[-1]
        now = ctx.db.flag_snapshot(e[expect]["serial_key"], e[expect]["username"])
        if now["flag"] != e[expect]["flag"] or now["note"] != e[expect]["note"]:
            if QMessageBox.question(self, verb, "Serial %s was changed after this step (by "
                                    "another user or on another page).\n\n%s anyway?"
                                    % (e[expect]["serial_key"], verb.capitalize())) != QMessageBox.Yes:
                return
        src.pop()
        ctx.db.flag_restore(e[put])
        dst.append(e)
        ctx.status("%s: %s" % (verb.upper(), e["label"]))
        self.clear_form()
        self.refresh_all()
        self.paint_history()

    def do_undo(self):
        self.step_back(self.history["undo"], self.history["redo"], "before", "after", "undo")

    def do_redo(self):
        self.step_back(self.history["redo"], self.history["undo"], "after", "before", "redo")

    def do_save(self):
        serial_no = self.serial_cb.currentText().strip()
        if not serial_no:
            QMessageBox.warning(self, "Serial number", "Enter a serial number first.")
            return
        sk = norm_text(serial_no)
        typed = "" if self.data_part_of(serial_no) else self.part_cb.currentText().strip()
        kind = self.kind()
        reason = self.reason.toPlainText().strip()
        date = self.date_ed.text().strip()

        def write():
            ctx.db.flag_set(sk, serial_no, kind, reason, date, getattr(self, "mrb_prob", ""),
                            getattr(self, "scrap_prob", ""), part_no=typed)
            me = ctx.username()
            if me:
                ctx.db.set_user_note(sk, me, self.note_ed.text(), self.note_color,
                                     self.note_private.isChecked())
        self.recorded("save %s" % serial_no, sk, write)
        self.clear_form()
        self.refresh_all()

    def do_delete(self):
        target = self.state["editing"] or norm_text(self.serial_cb.currentText())
        if not target:
            return
        self.recorded("remove flag %s" % target, target, lambda: ctx.db.flag_delete(target))
        self.clear_form()
        self.refresh_all()

    def do_resolve(self):
        target = self.state["editing"] or norm_text(self.serial_cb.currentText())
        kind = self.kind()
        seg = self.outcome_boxes.get(kind)
        choice = seg.get() if seg else None
        flag = ctx.db.flag_get(target) if target else None
        target_kind = RESOLVE_TARGET.get(kind, {}).get(choice)
        if not flag or flag["kind"] != kind or not target_kind:
            QMessageBox.warning(self, "Resolve", "Pick an existing %s serial from the list and "
                                "choose an outcome first." % KIND_TITLE.get(kind, kind))
            return
        self.recorded("resolve %s" % target, target,
                      lambda: ctx.db.flag_resolve(target, target_kind, by=ctx.username()))
        self.clear_form()
        self.refresh_all()

    def export_flags(self):
        rows = ctx.db.flag_all()
        if not rows:
            QMessageBox.information(self, "Export", "There is nothing to export yet.")
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Export MRB / SCRAP / TECO records",
            "MRB_SCRAP_TECO_%s.xlsx" % datetime.date.today().isoformat(), "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_flags_to_excel(path, rows)
        except Exception as e:
            QMessageBox.critical(self, "Export", "Could not write the file:\n\n%s" % e)
            return
        ctx.status("%d record(s) exported to %s" % (len(rows), os.path.basename(path)))

    # =================================================================
    #  FILTER SOURCES / REFRESH
    # =================================================================
    def cascade(self):
        parts = ctx.db.all_parts(ctx.db.merge_on("mrbscrap"))
        engineers = sorted({p["engineer"] for p in parts if p["engineer"]})
        eng = self.bar.set_choices(self.bar.eng, engineers)
        groups = sorted({p["part_group"] for p in parts if p["part_group"] and
                         (eng == ALL or (p["engineer"] or "").upper() == eng.upper())})
        grp = self.bar.set_choices(self.bar.grp, groups)
        pnos = sorted({p["part_no"] for p in parts
                       if (eng == ALL or (p["engineer"] or "").upper() == eng.upper())
                       and (grp == ALL or p["part_group"] == grp)})
        self.bar.parts.rebuild(pnos)
        self.refresh_all()

    def on_show(self):
        if not self.built:
            self.built = True
            self.bar.refresh_years(ctx.db.rw_years())
            self.cascade()
        else:
            self.refresh_all()

    def reload(self):
        if self.built:
            self.bar.refresh_years(ctx.db.rw_years())

    def annotations_changed(self):
        if self.built and self.isVisible():
            self.refresh_all()

    def resolve_parts(self, flags, idx):
        sks = [f["serial_key"] for f in flags]
        part_of = parts_for_serials(ctx.db, idx, sks)
        mat_of = None
        out = {}
        for f in flags:
            sk = f["serial_key"]
            part = part_of.get(sk)
            if part:
                out[sk] = (part, part.get("part_no") or "-")
                continue
            typed = (f.get("part_no") or "").strip()
            if typed:
                out[sk] = (match_part(norm_text(typed), idx), typed)
                continue
            if mat_of is None:
                mat_of = materials_for_serials(ctx.db, sks)
            out[sk] = (None, mat_of.get(sk) or "-")
        return out

    def refresh_all(self):
        if not self.built:
            return
        merged = ctx.db.merge_on("mrbscrap")
        idx, _c, _o = lookups(ctx.db, merged)
        f = self.bar.filters()
        d1, d2 = time_mode_to_date_range(f, ctx.db.week_start("mrbscrap"))
        allowed = allowed_part_keys(ctx.db, self.bar.engineer(), self.bar.group(),
                                    self.bar.parts.get_selected(), merged)
        every = ctx.db.flag_rows()
        parts = self.resolve_parts(every, idx)

        def in_scope(fl):
            if allowed is not None:
                part = parts[fl["serial_key"]][0]
                if not part or part["part_key"] not in allowed:
                    return False
            if d1 or d2:
                d = parse_date(fl["entry_date"])
                if d1 and (not d or d < d1):
                    return False
                if d2 and (not d or d > d2):
                    return False
            return True

        scoped = [fl for fl in every if in_scope(fl)]
        excl = {k for k, t in self.bar.exclude_flags.items() if t.isChecked()}
        only = {k for k, t in self.bar.only_flags.items() if t.isChecked()}

        def was_of(fl):
            was = fl.get("resolved_from") or ""
            return "mrb" if was == "mrbent" else was

        def shown_kind(fl):
            k = fl["kind"]
            if k == "fixed" and was_of(fl) == "scrap":
                k = "removed"            # v73: its own filter button
            if only:
                # a type box also lists what was closed from that type, so a
                # REMOVED / FIXED count can be traced back to its serials
                # v72: MRB also lists the MRBs already entered
                return (k in only or (k == "mrbent" and "mrb" in only)
                        or (k in ("fixed", "removed") and was_of(fl) in only))
            return k not in excl

        say = dict.fromkeys(ALL_KINDS, 0)
        fixed_from = dict.fromkeys(FIXED_UNDER, 0)
        for fl in scoped:
            if fl["kind"] == "fixed" and fl.get("resolved_from") == "scrap":
                fixed_from["scrap"] += 1
                continue
            say[fl["kind"]] = say.get(fl["kind"], 0) + 1
            was = fl.get("resolved_from")
            if was == "mrbent":
                was = "mrb"
            if fl["kind"] == "fixed" and was in fixed_from:
                fixed_from[was] += 1
        say["all"] = sum(v for kk, v in say.items() if kk not in ("fixed", "all"))
        for k, b in self.boxes.items():
            b.num.setText(str(say.get(k, 0)))
            mut = ctx.TH["text"]
            b.ent.setText("<span style='color:%s; font-weight:400'>Entered</span> %d"
                          % (mut, say["mrbent"]) if k == "mrb" else "")
            b.fix.setText("<span style='color:%s; font-weight:400'>%s</span> %d"
                          % (mut, SUB_TITLE.get(k, "FIXED").title(), fixed_from[k])
                          if k in FIXED_UNDER else "")
        self.refresh_groups(scoped, parts)
        shown = [fl for fl in scoped if shown_kind(fl)]
        shown.sort(key=lambda fl: fl["updated_at"] or "", reverse=True)
        shown.sort(key=lambda fl: (LIST_ORDER.get(fl["kind"], 99),
                                   fl["kind"] == "fixed" and was_of(fl) == "scrap"))
        self._shown, self._parts = shown, parts
        self.refresh_list_only()

    def box_clicked(self, kind):
        """A TOTALS box is also a filter: only that type in the list; the
        ALL OPEN box (or the lit box again) shows everything open (A1)."""
        only = self.bar.only_flags
        lit = [k for k, t in only.items() if t.isChecked()]
        self.bar.blockSignals(True)
        for t in only.values():
            t.setChecked(False)
        if kind != "all" and lit != [kind]:
            if kind in only:
                only[kind].setChecked(True)
            if kind == "fixed" and "fixed" in self.bar.exclude_flags:
                self.bar.exclude_flags["fixed"].setChecked(False)
                self.bar.exclude_flags["removed"].setChecked(False)
        elif "fixed" in self.bar.exclude_flags:
            self.bar.exclude_flags["fixed"].setChecked(True)
            self.bar.exclude_flags["removed"].setChecked(True)
        self.bar.blockSignals(False)
        self.bar.paint_chips()
        for k, b in self.boxes.items():
            on = (k == "all" and not [k2 for k2, t in only.items() if t.isChecked()]) or \
                 (k in only and only[k].isChecked())
            b.title.setStyleSheet("color: %s;" % (ctx.TH["accent"] if on else ctx.TH["muted"]))
        self.refresh_all()

    def refresh_list_only(self):
        if not self.built or not hasattr(self, "_shown"):
            return
        q = norm_text(self.find.text() or "")
        last = ctx.db.serial_last_points()
        today = datetime.date.today()
        rows, bgs = [], []
        items = []
        for f in self._shown:
            part_txt = self._parts[f["serial_key"]][1]
            if q and q not in f["serial_key"] and q not in norm_text(part_txt):
                continue
            t = KIND_TITLE.get(f["kind"], f["kind"])
            if f["kind"] == "fixed":
                was = f.get("resolved_from") or ""
                t = ("REMOVED" if was == "scrap" else
                     "FIXED (%s)" % KIND_TITLE.get(was, was.upper()) if was else "FIXED")
            ld, lop = last.get(f["serial_key"]) or (None, None)
            idle = ""
            d = parse_date(ld) if ld else None
            if d:
                idle = max(0, (today - d).days)
            kind = f["kind"]
            if kind == "fixed" and f.get("resolved_from") == "scrap":
                kind = "removed"
            items.append(([t, part_txt, f["serial_no"], f["reason"] or "", f["entry_date"] or "",
                           str(lop) if lop not in (None, "") else "-", (str(ld)[:10] if ld else "-"),
                           idle], kind))
        # one block per type, each under its own heading row ("TECO · 4")
        heads, spans, fgs, bold = [], {}, {}, []
        block = None
        for vals, kind in items:
            if kind != block:
                block = kind
                n = sum(1 for _v, k in items if k == kind)
                title = "REMOVED" if kind == "removed" else KIND_TITLE.get(kind, kind)
                r = len(rows)
                rows.append(["%s  ·  %d" % (title, n)] + [""] * (len(LIST_COLS) - 1))
                bgs.append(None)
                bold.append(True)
                spans[(r, 0)] = 3
                fgs[(r, 0)] = ctx.flag_text(kind)
                heads.append(r)
            rows.append(vals)
            bgs.append(kind)
            bold.append(False)
            if vals[-1] != "" and vals[-1] >= 28:
                fgs[(len(rows) - 1, len(LIST_COLS) - 1)] = ctx.TH["danger"]
            elif vals[-1] != "" and vals[-1] >= 14:
                fgs[(len(rows) - 1, len(LIST_COLS) - 1)] = ctx.TH["wf_wait"]
        self.head_rows = set(heads)
        self.list_card.set_sub("%d shown · grouped by type" % len(items))
        cols = autofit_columns(LIST_COLS, [v for v, _k in items] or [[""] * len(LIST_COLS)], max_w=420)
        cols[0] = (cols[0][0], max(cols[0][1] + 34, 150))   # room for the dot and the heading
        self.table.set_data(cols, rows, bgs, bold, frozen=3, cell_span=spans, cell_fg=fgs)

    def refresh_groups(self, scoped, parts):
        keys = [k for k, _t in GROUP_COLS]
        tally = {}
        for fl in scoped:
            k = fl["kind"]
            if k == "fixed":
                k = "removed" if fl.get("resolved_from") == "scrap" else "fixed"
            if k not in keys:
                continue
            part = parts[fl["serial_key"]][0]
            g = ((part or {}).get("part_group") or "").strip() or "(NO GROUP)"
            tally.setdefault(g, dict.fromkeys(keys, 0))[k] += 1
        names = sorted(tally, key=lambda g: (g == "(NO GROUP)", g))
        self.grp_rows = [[g] + [tally[g][k] for k in keys] for g in names]
        while self.grp_grid.count():
            it = self.grp_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.grp_grid.addWidget(label("Group", section=True), 0, 0)
        for c, (k, t) in enumerate(GROUP_COLS, start=1):
            h = label(t, section=True)
            h.setAlignment(Qt.AlignRight)
            self.grp_grid.addWidget(h, 0, c)
        if not names:
            self.grp_grid.addWidget(label("-", muted=True), 1, 0)
        for r, g in enumerate(names, start=1):
            self.grp_grid.addWidget(QLabel(g), r, 0)
            for c, (k, _t) in enumerate(GROUP_COLS, start=1):
                v = tally[g][k]
                lb = QLabel(str(v) if v else "·")
                lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                col = ctx.flag_text(k) if v else ctx.TH["muted"]
                lb.setStyleSheet("color: %s; font-weight: 700; font-size: 10pt;" % col)
                self.grp_grid.addWidget(lb, r, c)
        self.grp_btn.setText("By part group (%d)  ▾" % len(names))

    def toggle_groups(self):
        if self.grp_pop.isVisible():
            self.grp_pop.hide()
            return
        self.grp_pop.adjustSize()
        pos = self.grp_btn.mapToGlobal(self.grp_btn.rect().bottomRight())
        self.grp_pop.move(QPoint(pos.x() - self.grp_pop.width(), pos.y() + 4))
        self.grp_pop.show()

    def refresh_theme(self):
        for b in self.boxes.values():
            b.paint()
        self.pick_note_color(self.note_color)
        self.sync_outcome_ui()
        if self.built:
            self.refresh_all()

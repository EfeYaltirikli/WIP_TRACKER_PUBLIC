# -*- coding: utf-8 -*-
"""DATA & SETTINGS - the Qt page (new design).

Left: LOAD DATA (pick the data set, ADD or CHANGE, header row, one file or
several at once), STORED DATA (rows per data set, delete, edit traveler,
delete everything, the data warnings) and the IMPORT LOG.  Right: the
settings as sections, one open at a time - start-up time, data health,
shop rules, shared database, working week, merged parts, data cleaning &
date format - and, for the ADMIN and CO-ADMINs only, the ADMIN sections:
users & roles, open sessions, idle close & maintenance.

Everything that writes is admin-only, enforced twice as on the Tk page:
the buttons are disabled for everybody else, and guard_admin() stops the
action itself.  The import itself (load_file) is the Tk page's, with Qt
dialogs.
"""

import datetime
import os
import sqlite3

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QSpinBox,
                               QDoubleSpinBox, QMessageBox, QDialog, QLineEdit, QListWidget,
                               QListWidgetItem, QFileDialog, QPlainTextEdit, QFrame, QPushButton,
                               QLabel, QApplication, QSizePolicy)

import wip_core
from wip_core import (DATASET_LABEL, DATASET_ORDER, UPLOAD_ROWS, MAPPING_VERSION, FIELD_SETS,
                      SAP_COLUMNS, WEEKDAY_NAMES, DB_NAME, DUPLICATE_RULE_TEXT, ROLE_TITLE,
                      Database, WFAnalyzer, auto_map, detect_dataset, apply_rules, data_health,
                      default_db_path, fmt_hour, guess_column, index_col, read_rules, save_rules,
                      invalidate_caches, iso_year_week, list_sheets, read_table, sap_read,
                      save_prefs, read_flags, sniff_header_row, week_bounds, week_offset,
                      wf_canonical_from_sap, wf_read_traveler, write_db_pointer,
                      check_part_merges, merge_name, norm_text)
from qt_widgets import (ctx, Card, DataTable, Toggle, Seg, FlowLayout, label, button, hbox,
                        IconButton, page_layout, page_head, draw_icon)
from qt_theme import MONO

PAGE_WEEK_PAGES = [("analysis", "RW Analysis"), ("cause", "Cause Op"), ("waterfall", "Waterfall"),
                   ("mrbscrap", "MRB / Scrap / TECO"), ("bottleneck", "Bottleneck"),
                   ("b1000", "B1000 Live"), ("forecast", "Forecast"), ("acc", "Custom Analysis")]
DEFAULT = "Default"
SHORT = {"rw": "SAP RW", "comp": "COMP description", "parts": "ENG x PART",
         "shipment": "Shipment report", "traveler": "Traveler", "flags": "MRB / Scrap / TECO"}


def _dm(d):
    """2026-06-09 -> 09.06"""
    s = str(d or "")
    return "%s.%s" % (s[8:10], s[5:7]) if len(s) >= 10 else s


def guard_admin(verb="change"):
    """True when the current account may NOT do this.  v69: the role is
    read from the FILE at the moment of the click - an admin on another
    computer may have changed it since this copy signed in."""
    name = ctx.username()
    if name:
        try:
            fresh = ctx.db.user(name)
        except sqlite3.Error:
            fresh = None
        if fresh is not None and ctx.user is not None:
            if not fresh.get("active"):
                fresh = dict(fresh, role="")
            if fresh.get("role") != ctx.user.get("role"):
                ctx.user.update(fresh)
                if ctx.window is not None:
                    ctx.window.paint_header()
                    for p in ctx.window.pages:
                        if p is not None and hasattr(p, "user_changed"):
                            p.user_changed()
    if ctx.is_admin():
        return False
    QMessageBox.warning(ctx.window, "Admin only",
                        "Only an admin can %s the shared data.\n\nYou are signed in as %s.  "
                        "You can read this page, but not change it - ask an admin to make the "
                        "change." % (verb, ctx.username() or "a standard user"))
    return True


def busy_text(e):
    """What a failed admin write says: busy (somebody is saving) or a real
    error."""
    if wip_core.is_lock_error(e):
        return ("Another user is saving to the shared database right now, so this could not be "
                "saved.  Nothing changed - try again in a moment.")
    return "Could not save: %s" % e


def ask(title, text, parent=None):
    return QMessageBox.question(parent or ctx.window, title, text) == QMessageBox.Yes


# =====================================================================
#  DIALOGS used by the import
# =====================================================================
class MappingDialog(QDialog):
    """Match the file's columns to the fields (a data set without a fixed
    layout, or a file whose headings could not be matched)."""

    def __init__(self, parent, headers, fields, title, preset=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.fields = fields
        self.result_map = None
        lay = QVBoxLayout(self)
        lay.addWidget(label("Match the file columns with the fields below.  Fields marked * "
                            "are mandatory.", muted=True, wrap=True))
        g = QGridLayout()
        self.opts = ["(not used)"] + ["%d - %s" % (i + 1, h or "(blank)")
                                      for i, h in enumerate(headers)]
        self.boxes = {}
        for i, (key, lbl, req, kws) in enumerate(fields):
            g.addWidget(label(("* " if req else "   ") + lbl), i, 0)
            cb = QComboBox()
            cb.addItems(self.opts)
            cb.setMinimumWidth(320)
            idx = preset.get(key) if preset else None
            if idx is None or not (0 <= idx < len(headers)):
                idx = guess_column(headers, kws)
            cb.setCurrentIndex(idx + 1 if idx is not None else 0)
            self.boxes[key] = cb
            g.addWidget(cb, i, 1)
        lay.addLayout(g)
        lay.addWidget(hbox(None, button("Cancel", slot=self.reject),
                           button("OK", primary=True, slot=self._ok)))

    def _ok(self):
        mapping, missing = {}, []
        for key, lbl, req, _ in self.fields:
            i = self.boxes[key].currentIndex()
            if i <= 0:
                mapping[key] = None
                if req:
                    missing.append(lbl)
            else:
                mapping[key] = i - 1
        if missing:
            QMessageBox.warning(self, "Missing field", "Mandatory fields not selected:\n\n- " +
                                "\n- ".join(missing))
            return
        self.result_map = mapping
        self.accept()


def ask_flag_conflicts(conflicts, filename):
    """Serials in the file that already carry a record: "replace", "keep"
    or None (cancel the file)."""
    dlg = QDialog(ctx.window)
    dlg.setWindowTitle("Serial numbers already in the database")
    dlg.resize(780, 480)
    lay = QVBoxLayout(dlg)
    lay.addWidget(label("%d of the serial numbers already have a record here" % len(conflicts),
                        obj="CardTitle"))
    lay.addWidget(label("%s wants to write these serials, and each one already carries a record. "
                        "Replacing overwrites the type, the reason and the date now in the "
                        "database. Everything else in the file is written either way." % filename,
                        muted=True, wrap=True))
    t = DataTable()

    def summary(kind, reason):
        kind = (kind or "").upper()
        reason = (reason or "").replace("\n", " ").strip()
        return "%s - %s" % (kind, reason) if reason else kind
    t.set_data([("SERIAL", 120), ("IN THE DATABASE NOW", 300), ("IN THE FILE", 300)],
               [[c["serial"], summary(c["old_kind"], c["old_reason"]),
                 summary(c["new_kind"], c["new_reason"])] for c in conflicts])
    lay.addWidget(t, 1)
    ans = {"v": None}

    def choose(v):
        ans["v"] = v
        dlg.accept()
    lay.addWidget(hbox(button("Replace them", slot=lambda: choose("replace")),
                       button("Keep what is here", slot=lambda: choose("keep")), None,
                       button("Cancel this file", slot=lambda: choose(None))))
    dlg.exec()
    return ans["v"]


def load_file(path, dataset=None, replace=True, skip_duplicates=True, log=None, header_row=0):
    """The import, as on the Tk page (page_upload.load_file)."""
    def say(m):
        if log:
            log(m)
    t0 = datetime.datetime.now()
    try:
        sheets = list_sheets(path)
    except Exception as e:
        say("FAILED  %s  (%s)" % (os.path.basename(path), e))
        return None
    sheet = sheets[0] if sheets else None
    if sheets and len(sheets) > 1 and dataset:
        hint = {"rw": ["rw", "sap", "data"], "comp": ["comp"], "parts": ["eng", "part"],
                "traveler": ["travel", "route"]}.get(dataset, [])
        for sname in sheets:
            if any(h in sname.lower() for h in hint):
                sheet = sname
                break

    if dataset == "flags":
        try:
            records, issues = read_flags(path, sheet)
        except Exception as e:
            say("FAILED  %s  (%s)" % (os.path.basename(path), e))
            return None
        overwrite = True
        if not replace:
            conflicts = ctx.db.flag_conflicts(records)
            if conflicts:
                choice = ask_flag_conflicts(conflicts, os.path.basename(path))
                if choice is None:
                    say("CANCELLED  %s  (%d serial(s) already here)"
                        % (os.path.basename(path), len(conflicts)))
                    return None
                overwrite = (choice == "replace")
        tally = ctx.db.flag_import(records, replace=replace, overwrite=overwrite)
        n = tally["added"] + tally["updated"]
        secs = (datetime.datetime.now() - t0).total_seconds()
        line = ("%-16s %-28s %d new, %d replaced in %.1fs (%s)"
                % (DATASET_LABEL["flags"], os.path.basename(path)[:28], tally["added"],
                   tally["updated"], secs, "changed" if replace else "added"))
        if tally["kept"]:
            line += ", %d left as they were" % tally["kept"]
        if issues["no_serial"]:
            line += ", %d without a serial number" % issues["no_serial"]
        if issues["bad_kind"]:
            line += ", %d with an unknown type" % issues["bad_kind"]
        say(line)
        return {"loaded": n, "dataset": "flags", "file": os.path.basename(path)}

    if dataset == "traveler":
        try:
            routes, display = wf_read_traveler(path, sheet)
        except Exception as e:
            say("FAILED  %s  (%s)" % (os.path.basename(path), e))
            return None
        if not routes:
            say("TRAVELER EMPTY OR UNRECOGNISED  %s" % os.path.basename(path))
            return None
        n = ctx.db.wf_import_traveler(routes, display, replace=replace)
        secs = (datetime.datetime.now() - t0).total_seconds()
        line = ("%-16s %-28s %d part(s), %d rows in %.1fs (%s)"
                % (DATASET_LABEL["traveler"], os.path.basename(path)[:28], len(routes), n, secs,
                   "changed" if replace else "added"))
        collapsed = getattr(ctx.db, "_last_traveler_collapsed", 0)
        if collapsed:
            line += ", %d repeated operation number(s) collapsed into one row each" % collapsed
        say(line)
        for note in (wip_core.TRAVELER_NOTES or [])[:20]:
            say("   TRAVELER  %s" % note)
        # 2.1.3: every MACHINE GROUP name in the file against COMP
        try:
            glines, gok, gbad = wip_core.traveler_group_report(ctx.db, routes)
        except Exception as e:
            glines, gok, gbad = ["could not be checked (%s)" % e], [], []
        if glines:
            say("   MACHINE GROUPS  %d matched in COMP, %d not" % (len(gok), len(gbad)))
            for gl in glines:
                say("      %s" % gl)
        invalidate_caches()
        if ctx.wf_an is not None:
            ctx.wf_an._cache.clear()
        return {"loaded": n, "dataset": "traveler", "file": os.path.basename(path)}

    try:
        hrow = int(header_row) if header_row else sniff_header_row(path, sheet)
        headers, rows, skipped_headers = read_table(path, sheet, hrow, progress=ctx.db.progress)
    except Exception as e:
        say("FAILED  %s  (%s)" % (os.path.basename(path), e))
        return None
    if not rows:
        say("EMPTY   %s" % os.path.basename(path))
        return None
    if dataset is None:
        dataset, _ = detect_dataset(headers)
        if dataset is None:
            say("UNKNOWN TYPE  %s" % os.path.basename(path))
            return None
    day_first = ctx.db.get_meta("date_first", "month") == "day"
    sap_info, sap_rows = None, None
    if dataset == "rw":
        sap_rows, sap_info = sap_read(path, sheet, hrow, day_first=True, log=say,
                                      progress=ctx.db.progress,
                                      table=(headers, rows, skipped_headers))
        res = ctx.db.import_rw(sap_rows, replace=replace, day_first=True,
                               skip_duplicates=skip_duplicates)
        try:
            m = (sap_info or {}).get("mapping") or {}
            ctx.db.set_meta("sap_missing_cols", [
                ad for alan, ad in (("last_x", "Son (AH)"), ("unit", "Unit (M)"),
                                    ("seq", "Seq (H)"), ("std_hour", "Toplam Is"))
                if m.get(alan) is None])
        except Exception:
            pass
    else:
        preset = ctx.db.get_meta("mapping_" + dataset, None)
        if isinstance(preset, dict) and preset.get("_v") != MAPPING_VERSION:
            preset = None
        mapping, missing = auto_map(headers, FIELD_SETS[dataset])
        if preset:
            for k, v in preset.items():
                if k != "_v" and v is not None and 0 <= v < len(headers):
                    mapping[k] = v
            missing = [lbl for k, lbl, req, _ in FIELD_SETS[dataset]
                       if req and mapping.get(k) is None]
        if missing and dataset == "comp" and len(headers) >= 3:
            for i, (k, _l, _r, _kw) in enumerate(FIELD_SETS["comp"]):
                if mapping.get(k) is None:
                    mapping[k] = i
            missing = []
            say("COMP headers were blank - using the first three columns.")
        if missing:
            dlg = MappingDialog(ctx.window, headers, FIELD_SETS[dataset],
                                "Column mapping - %s" % DATASET_LABEL[dataset], mapping)
            if dlg.exec() != QDialog.Accepted or not dlg.result_map:
                say("CANCELLED  %s" % os.path.basename(path))
                return None
            mapping = dlg.result_map
        stored = dict(mapping)
        stored["_v"] = MAPPING_VERSION
        ctx.db.set_meta("mapping_" + dataset, stored)
        records = [{k: (r[i] if (i is not None and i < len(r)) else None)
                    for k, i in mapping.items() if k != "_v"} for r in rows]
        res = (ctx.db.import_comp(records, replace=replace) if dataset == "comp"
               else ctx.db.import_shipments(records, replace=replace, day_first=day_first)
               if dataset == "shipment" else ctx.db.import_parts(records, replace=replace))
    res["dataset"] = dataset
    res["file"] = os.path.basename(path)
    secs = (datetime.datetime.now() - t0).total_seconds()
    line = "%-16s %-28s header row %d, loaded %d in %.1fs" % (
        DATASET_LABEL[dataset], res["file"][:28], hrow, res["loaded"], secs)
    if res.get("ops"):
        line += ", %d production steps" % res["ops"]
    if res.get("removed"):
        line += ", %d REMOVED over the hour limit" % res["removed"]
    if res["duplicates"]:
        line += ", %d duplicates skipped" % res["duplicates"]
    if res["skipped"]:
        line += ", %d rows w/o material" % res["skipped"]
    if res["no_date"]:
        line += ", %d unreadable dates" % res["no_date"]
    if skipped_headers:
        line += ", %d repeated header row(s)" % skipped_headers
    say(line)
    if dataset == "rw" and res.get("duplicates"):
        say("   " + DUPLICATE_RULE_TEXT)
    if dataset == "rw" and sap_info:
        for field, _letter, _expected, _kw, _req in SAP_COLUMNS[:8]:
            ci = sap_info["mapping"].get(field)
            say("   %-14s %-3s <- %s" % (field, index_col(ci) if ci is not None else "-",
                                         sap_info["headers"][ci]
                                         if ci is not None and ci < len(sap_info["headers"])
                                         else "(NONE)"))
        if res.get("removed"):
            QMessageBox.warning(ctx.window, "Rows removed",
                                "%d row(s) were left out because a single rework entry was "
                                "longer than %s hours.\n\nOpen Removed rows on the RW Analysis "
                                "page to see them." % (res["removed"],
                                                       fmt_hour(ctx.db.max_rw_hour())))
        try:
            _wf_records, wf_issues = wf_canonical_from_sap(sap_rows)
            wf_n = ctx.db.wf_rebuild_part_data()
            say("WATERFALL PART DATA  %d canonical rows from %d read" % (wf_n, len(sap_rows)))
            try:
                k = ctx.db.x_rule_check()
                ctx.db.set_meta("x_rule_check", k)
                if k["serials"]:
                    say("X CHECK  %d serial(s) moved past a step without its X" % k["serials"])
            except Exception as e:
                say("X CHECK  could not be worked out: %s" % e)
            parts = []
            if wf_issues["no_serial"]:
                parts.append("%d w/o serial number" % wf_issues["no_serial"])
            if wf_issues["no_date"]:
                parts.append("%d w/o a usable date" % wf_issues["no_date"])
            if wf_issues["bad_operation"]:
                parts.append("%d w/o a numeric operation" % wf_issues["bad_operation"])
            if wf_issues["merged_duplicates"]:
                parts.append("%d merged as repeat entries of the same material/serial/operation"
                             % wf_issues["merged_duplicates"])
            if parts:
                say("   accounted for: " + ", ".join(parts))
        except Exception as e:
            say("WATERFALL PART DATA could not be updated  (%s)" % e)
            # 2.1.4: not just a log line - the waterfall would quietly keep
            # the old hours while the RW pages show the new rows
            QMessageBox.warning(ctx.window, "Waterfall not updated",
                                "The rework rows were loaded, but the waterfall's operation "
                                "table could not be rebuilt:\n\n%s\n\nThe waterfall still "
                                "shows the data from before this upload. It is rebuilt "
                                "automatically the next time the program opens, or upload "
                                "the file again." % e)
    invalidate_caches()
    if ctx.wf_an is not None:
        ctx.wf_an._cache.clear()
    return res


# =====================================================================
#  ACCORDION SECTION
# =====================================================================
class Section(QWidget):
    """One settings section: a head that opens it, the body under it."""

    def __init__(self, owner, key, title, admin=False):
        super().__init__()
        self.owner, self.key, self.title, self.admin = owner, key, title, admin
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.head = QPushButton(("      " if admin else "") + title.replace("&", "&&"))
        self.head.setObjectName("SecHead")
        self.head.setCheckable(True)
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.clicked.connect(lambda _=False: owner.open_section(key, toggle=True))
        lay.addWidget(self.head)
        self.body = QWidget()
        self.body.setObjectName("SecBody")
        self.blay = QVBoxLayout(self.body)
        self.blay.setContentsMargins(22, 12, 22, 16)
        self.blay.setSpacing(8)
        lay.addWidget(self.body)
        self.body.hide()

    def set_open(self, on):
        self.head.setChecked(on)
        self.body.setVisible(on)


NAV_ICONS = {"load": "upload", "health": "heart", "clean": "wand", "rules": "lines",
             "week": "calendar", "merge": "merge", "shared": "db", "startup": "clock",
             "users": "users", "sessions": "monitor", "idle": "power"}


class NavItem(QPushButton):
    """A line of the settings navigation: an icon, the name, a badge."""

    def __init__(self, key, title):
        super().__init__(title.replace("&", "&&"))
        self.key = key
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_icon(p, NAV_ICONS.get(self.key, "settings"), 22, self.height() / 2,
                  ctx.TH["text"] if self.isChecked() else ctx.TH["muted"], 1.4)
        p.end()


class DsTile(QFrame):
    """A data set as a card (SET1): its name, how many rows, the dates the
    rows cover.  A click makes it the one the next file goes into."""

    def __init__(self, page, key):
        super().__init__()
        self.page, self.key = page, key
        self.setObjectName("dstile")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(86)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(0)
        self.name = label(SHORT[key])
        self.name.setStyleSheet("font-weight: 700;")
        self.n = QLabel("0")
        self.span = label("—", muted=True)
        for w in (self.name, self.n, self.span):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self.name)
        lay.addWidget(self.n)
        lay.addWidget(self.span)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.page.ds.set(self.key, emit=True)

    def paint(self, picked):
        th = ctx.TH
        self.setStyleSheet("QFrame#dstile { background: %s; border: 1px solid %s; "
                           "border-radius: 10px; }"
                           % (th["sel_row"] if picked else th["surf2"],
                              th["accent"] if picked else th["border"]))
        self.n.setStyleSheet("font-size: 16pt; font-weight: 700; color: %s;" % th["text"])


class DropZone(QFrame):
    """Files dropped here go the same way as Select file(s): one file into
    the picked data set, several are told apart by their columns."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.paint(False)

    def paint(self, hot):
        th = ctx.TH
        self.setStyleSheet("QFrame#dropzone { border: 1.5px dashed %s; border-radius: 12px; "
                           "background: %s; }" % (th["accent"] if hot else th["border"],
                                                  th["sel_row"] if hot else "transparent"))

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self.paint(True)

    def dragLeaveEvent(self, ev):
        self.paint(False)

    def dropEvent(self, ev):
        self.paint(False)
        ps = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if ps:
            ev.acceptProposedAction()
            self.page.take_files(ps)


# =====================================================================
#  THE PAGE
# =====================================================================
class Page(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.built = False
        self.write_widgets = []
        self.paths = {k: "" for k in UPLOAD_ROWS}
        self.multi = {}
        self.sess_timer = QTimer(self)
        self.sess_timer.timeout.connect(self.tick_sessions)

        outer = page_layout(self)
        self.db_pill = QLabel("")
        page_head(outer, "Data & Settings", "Load data, check its health, the shop's settings "
                                            "— and, for the admin, users and sessions",
                  self.db_pill)
        self.admin_note = label("", wrap=True)
        self.admin_note.setStyleSheet("color: %s;" % ctx.TH["wf_wait"])
        self.admin_note.hide()
        outer.addWidget(self.admin_note)

        # v69 (SET1): a navigation on the left (DATA / SHOP / ADMIN), the
        # picked part on the right - one at a time
        cols = QHBoxLayout()
        cols.setSpacing(18)
        outer.addLayout(cols, 1)
        nav = QWidget()
        nav.setFixedWidth(270)
        self.nav_lay = QVBoxLayout(nav)
        self.nav_lay.setContentsMargins(0, 0, 0, 0)
        self.nav_lay.setSpacing(2)
        cols.addWidget(nav, 0, Qt.AlignTop)
        right = QVBoxLayout()
        right.setSpacing(14)
        cols.addLayout(right, 1)
        self.nav_btns, self.nav_badges, self.nav_heads = {}, {}, {}
        for head, entries in (("DATA", (("load", "Load & stored data"), ("health", "Data health"),
                                        ("clean", "Data cleaning & dates"))),
                              ("SHOP", (("rules", "Shop rules"), ("week", "Working week"),
                                        ("merge", "Merged parts"), ("shared", "Shared database"),
                                        ("startup", "Start-up time"))),
                              ("ADMIN", (("users", "Users & roles"), ("sessions", "Open sessions"),
                                         ("idle", "Idle close & maintenance")))):
            h = label(head, section=True)
            h.setContentsMargins(14, 12, 0, 4)
            self.nav_heads[head] = h
            self.nav_lay.addWidget(h)
            for key, title in entries:
                b = NavItem(key, title)
                b.clicked.connect(lambda _=False, k=key: self.open_section(k))
                badge = QLabel("", b)
                badge.setAlignment(Qt.AlignCenter)
                badge.hide()
                self.nav_btns[key] = b
                self.nav_badges[key] = badge
                self.nav_lay.addWidget(b)
        self.nav_lay.addStretch(1)
        self.admin_head = self.nav_heads["ADMIN"]

        # ---------------- load data ----------------
        self.load_page = QWidget()
        lp = QVBoxLayout(self.load_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(14)
        right.addWidget(self.load_page)
        load = Card("Load data", "files are checked before anything is written")
        # the data sets as cards: name, rows, the dates the rows cover; the
        # picked one takes the next file
        self.ds = Seg([(SHORT[k], k) for k in UPLOAD_ROWS], "rw")
        self.ds.changed.connect(lambda _v: (self.paint_file_line(), self.paint_tiles()))
        self.ds.hide()
        tiles = QWidget()
        tl = QHBoxLayout(tiles)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(10)
        self.tiles = {}
        for k in UPLOAD_ROWS:
            t = DsTile(self, k)
            self.tiles[k] = t
            tl.addWidget(t, 1)
        load.add(tiles)
        # v73: whether this copy can read old .xls files - says at once if
        # xlrd made it into the .exe
        try:
            import xlrd
            xls_txt = "Old Excel (.xls) reader: available (xlrd %s)" % getattr(xlrd, "__version__", "")
        except Exception:
            xls_txt = ("Old Excel (.xls) reader: NOT in this program - .xls files cannot be read "
                       "(save them as .xlsx, or rebuild with xlrd - see OKU.txt)")
        self.xls_lbl = label(xls_txt, muted=True)
        load.add(self.xls_lbl)
        self.mode = Seg([("Add", "add"), ("Change", "replace")],
                        ctx.prefs.get("upload_mode", "add"))
        self.mode.changed.connect(self._mode_saved)
        self.hdr = QSpinBox()
        self.hdr.setRange(0, 50)
        self.hdr.setFixedWidth(80)
        self.hdr.setToolTip("Header row (0 = detect)")
        self.drop = DropZone(self)
        # the text and the controls sit side by side, the controls go under
        # the text on a narrow window
        dz = FlowLayout(self.drop, 14)
        dz.setContentsMargins(22, 16, 22, 16)
        tw = QWidget()
        tl_ = QHBoxLayout(tw)
        tl_.setContentsMargins(0, 0, 0, 0)
        tl_.setSpacing(14)
        arrow = QLabel("↑")
        arrow.setStyleSheet("font-size: 22pt; color: %s;" % ctx.TH["accent"])
        tl_.addWidget(arrow)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        self.drop_title = label("")
        b_sel = button("Select file(s)...", slot=self.pick_files)
        txt.addWidget(hbox(self.drop_title, label("or", muted=True), b_sel, None, spacing=8))
        self.file_lbl = label("", muted=True)
        txt.addWidget(self.file_lbl)
        tl_.addLayout(txt, 1)
        tw.setMinimumWidth(380)
        dz.addWidget(tw)
        b_up = button("Upload", primary=True, slot=self.upload_smart)
        dz.addWidget(hbox(self.mode, label("Header row", muted=True), self.hdr, b_up, spacing=10))
        load.add(self.drop)
        self.mode_lbl = label("", muted=True)
        self.multi_lbl = label("", muted=True, wrap=True)
        b_clr = button("Clear selection", slot=self.clear_paths)
        load.add(hbox(self.mode_lbl, None, b_clr, spacing=10))
        load.add(self.multi_lbl)
        self.write_widgets += [b_up, self.mode, self.hdr]
        lp.addWidget(load)

        # ---------------- stored data ----------------
        store = Card("Stored data")
        b_trav = button("Edit traveler", slot=self.edit_traveler)
        b_wipe = button("Delete everything...", danger=True, slot=self.wipe)
        store.add_action(b_trav)
        store.add_action(b_wipe)
        self.store_grid = QGridLayout()
        self.store_grid.setHorizontalSpacing(16)
        self.store_grid.setVerticalSpacing(4)
        for c, h in enumerate(("Dataset", "Rows", "From", "To", "")):
            hl = label(h, muted=True)
            hl.setStyleSheet("font-weight: 600;")
            self.store_grid.addWidget(hl, 0, c, alignment=Qt.AlignRight if c == 1 else Qt.AlignLeft)
        self.store_rows = {}
        for r, key in enumerate(DATASET_ORDER + ["traveler", "flags"], start=1):
            n, d0, d1 = label("0"), label("—", muted=True), label("—", muted=True)
            n.setStyleSheet("font-family: '%s'; font-weight: 600;" % MONO)
            b = button("Delete", danger=True, slot=lambda k=key: self.delete_dataset(k))
            b.setStyleSheet("border: none; background: transparent;")
            nm = label(SHORT[key])
            nm.setStyleSheet("font-weight: 600;")
            self.store_grid.addWidget(nm, r, 0)
            self.store_grid.addWidget(n, r, 1, alignment=Qt.AlignRight)
            self.store_grid.addWidget(d0, r, 2)
            self.store_grid.addWidget(d1, r, 3)
            self.store_grid.addWidget(b, r, 4, alignment=Qt.AlignRight)
            self.store_rows[key] = (n, d0, d1)
            self.write_widgets.append(b)
        self.store_grid.setColumnStretch(0, 1)
        store.body.addLayout(self.store_grid)
        self.extra_lbl = label("", muted=True)
        store.add(self.extra_lbl)
        self.warn_lbl = label("", wrap=True)
        self.warn_lbl.setStyleSheet("color: %s;" % ctx.TH["wf_wait"])
        self.warn_lbl.linkActivated.connect(lambda _l: self.show_unknown_wcs())
        store.add(self.warn_lbl)
        self.write_widgets += [b_trav, b_wipe]
        lp.addWidget(store)

        # ---------------- import log ----------------
        logc = Card("Import log")
        logc.add_action(button("Clear", slot=lambda: self.log_box.clear()))
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont(MONO, 10))
        self.log_box.setMinimumHeight(140)
        logc.add(self.log_box)
        lp.addWidget(logc)

        # ---------------- settings sections: one card each ----------------
        self.sections, self.sec_cards = {}, {}
        for key, title in (("startup", "Start-up time"), ("health", "Data health"),
                           ("rules", "Shop rules"), ("shared", "Shared database"),
                           ("week", "Working week"), ("merge", "Merged parts"),
                           ("clean", "Data cleaning & date format"),
                           ("users", "Users & roles"), ("sessions", "Open sessions"),
                           ("idle", "Idle close & maintenance")):
            s = Section(self, key, title, admin=key in ("users", "sessions", "idle"))
            self.sections[key] = s
            card = Card(title)
            s.blay.setContentsMargins(0, 4, 0, 4)
            card.add(s.body)
            card.hide()
            self.sec_cards[key] = card
            right.addWidget(card)
        right.addStretch(1)
        self.build_startup()
        self.build_health()
        self.build_rules()
        self.build_shared()
        self.build_week()
        self.build_merge()
        self.build_clean()
        self.build_users()
        self.build_sessions()
        self.build_idle()
        self.open_section("load")

    # =================================================================
    def _mode_saved(self, v):
        self.paint_mode_lbl()
        ctx.prefs["upload_mode"] = v
        try:
            save_prefs(ctx.prefs)
        except Exception:
            pass

    def log(self, msg):
        self.log_box.appendPlainText("%s  %s" % (datetime.datetime.now().strftime("%H:%M"), msg))
        QApplication.processEvents()

    def open_section(self, key, toggle=False):
        """Show one part of the page (SET1 navigation); "load" is the load
        & stored data page, the rest are the settings sections."""
        if key in ("users", "sessions", "idle") and not ctx.is_admin():
            return
        if key != "load" and key not in self.sections:
            return
        self.load_page.setVisible(key == "load")
        for k, x in self.sections.items():
            x.set_open(k == key)
            self.sec_cards[k].setVisible(k == key)
        for k, b in self.nav_btns.items():
            b.setChecked(k == key)
        self.cur_section = key
        {"health": self.refresh_health, "users": self.reload_users,
         "sessions": self.reload_sessions, "idle": self.paint_maintenance,
         "startup": self.show_startup, "merge": self.paint_merges,
         "week": self.paint_week}.get(key, lambda: None)()
        if key == "sessions":
            self.sess_timer.start(6000)
        else:
            self.sess_timer.stop()

    def paint_nav(self):
        th = ctx.TH
        for k, b in self.nav_btns.items():
            b.setStyleSheet(
                "QPushButton#NavItem { text-align: left; padding: 9px 14px 9px 42px; border: none; "
                "border-radius: 8px; background: transparent; color: %s; font-size: 10.5pt; } "
                "QPushButton#NavItem:hover { background: %s; } "
                "QPushButton#NavItem:checked { background: %s; color: %s; font-weight: 700; "
                "border-left: 3px solid %s; }"
                % (th["text"], th["surf2"], th["sel_row"], th["text"], th["accent"]))
            bd = self.nav_badges[k]
            bd.setGeometry(b.width() - 34 if b.width() > 60 else 220, 8, 22, 22)

    def set_badge(self, key, n, color):
        bd = self.nav_badges[key]
        bd.setText(str(n) if n else "")
        bd.setVisible(bool(n))
        bd.setStyleSheet("background: %s; color: #FFFFFF; border-radius: 11px; font-weight: 700; "
                         "font-size: 9pt;" % color)
        b = self.nav_btns[key]
        bd.setGeometry(max(60, b.width() - 34), max(0, (b.height() - 22) // 2), 22, 22)

    def paint_badges(self):
        """Data health: how many checks are not OK.  Open sessions: how many
        copies of the program are open."""
        try:
            bad = sum(1 for c in data_health(ctx.db) if c["level"] != "ok")
        except Exception:
            bad = 0
        self.set_badge("health", bad, ctx.TH["wf_wait"])
        try:
            n = len(list(ctx.db.all_sessions())) if ctx.is_admin() else 0
        except Exception:
            n = 0
        self.set_badge("sessions", n, ctx.TH["accent"])

    def paint_db_pill(self):
        shared = os.path.normcase(os.path.abspath(ctx.db.path)) != \
            os.path.normcase(os.path.abspath(default_db_path()))
        col = "#3FB86A" if shared else ctx.TH["muted"]
        self.db_pill.setText("●  %s" % ("Shared database connected" if shared
                                        else "Database in the program folder"))
        self.db_pill.setToolTip(ctx.db.path)
        self.db_pill.setStyleSheet("color: %s; background: %s; border-radius: 14px; "
                                   "padding: 6px 14px; font-weight: 600;"
                                   % (col, ctx.TH["surf2"]))

    def paint_mode_lbl(self):
        self.mode_lbl.setText("Add · keep stored rows, add new ones" if self.mode.get() == "add"
                              else "Change · the stored rows of that data set are replaced")

    def paint_tiles(self):
        sp = ctx.db.rw_span()
        cur = self.ds.get()
        for k, t in self.tiles.items():
            t.n.setText("{:,}".format(self.row_count(k)).replace(",", " "))
            t.span.setText(("%s – %s" % (_dm(sp["d0"]), _dm(sp["d1"])))
                           if k == "rw" and sp else "—")
            t.paint(k == cur)
        self.drop_title.setText("<b>Drop %s files here</b>" % SHORT[cur])

    def pick_files(self):
        ps, _x = QFileDialog.getOpenFileNames(
            self, "Select file(s) - %s" % DATASET_LABEL[self.ds.get()], "",
            "Excel / CSV / MHTML (*.xlsx *.xlsm *.xls *.csv *.txt *.tsv *.mht *.mhtml *.html "
            "*.htm);;All files (*)")
        if ps:
            self.take_files(ps)

    def take_files(self, ps):
        """One file goes into the picked data set; several are told apart by
        their columns (as Select multiple did)."""
        if len(ps) == 1:
            self.multi = {}
            self.paths[self.ds.get()] = ps[0]
            self.paint_multi()
            self.paint_file_line()
            return
        self.detect_files(ps)

    def upload_smart(self):
        if self.multi:
            self.upload_all()
        else:
            self.upload_one()

    def on_show(self):
        self.built = True
        self.refresh_status()
        self.paint_admin()
        self.paint_nav()
        self.paint_db_pill()
        self.paint_badges()

    def reload(self):
        if self.built:
            self.refresh_status()

    def user_changed(self):
        self.paint_admin()

    def paint_admin(self):
        """Writes are greyed out for everybody but the admin / co-admins;
        the ADMIN sections are not shown to them at all."""
        admin = ctx.is_admin()
        for w in self.write_widgets:
            w.setEnabled(admin)
        for k in ("users", "sessions", "idle"):
            self.nav_btns[k].setVisible(admin)
            if not admin and self.sections[k].head.isChecked():
                self.open_section("load")
        self.admin_head.setVisible(admin)
        if admin:
            self.admin_note.hide()
        else:
            self.admin_note.setText("Read only - you are signed in as %s.  Only an admin can "
                                    "upload, delete or change anything on this page.  Everything "
                                    "here is still yours to read." % (ctx.username() or "a user"))
            self.admin_note.show()

    # ---------------- load data ----------------
    def paint_file_line(self):
        k = self.ds.get()
        p = self.paths.get(k) or ""
        n = "{:,}".format(self.row_count(k)).replace(",", " ")
        son = ctx.db.last_upload_at()
        if p:
            self.file_lbl.setText("%s   ·   ready to upload into %s (%s rows now)"
                                  % (os.path.basename(p), SHORT[k], n))
        else:
            self.file_lbl.setText("%s · %s rows%s" % (SHORT[k], n, (" · last loaded %s"
                                                                   % son.strftime("%d.%m.%Y %H:%M"))
                                                     if son else ""))

    def detect_files(self, ps):
        for p in ps:
            try:
                sheets = list_sheets(p)
                hrow = sniff_header_row(p, sheets[0] if sheets else None)
                headers, _r, _s = read_table(p, sheets[0] if sheets else None, hrow, max_rows=1)
                ds, _ = detect_dataset(headers)
            except Exception as e:
                self.log("Could not read %s (%s)" % (os.path.basename(p), e))
                continue
            if ds is None:
                self.log("Could not identify %s - pick its data set and use Select file"
                         % os.path.basename(p))
                continue
            self.multi[ds] = p
            self.log("Detected %-16s -> %s" % (DATASET_LABEL[ds], os.path.basename(p)))
        self.paint_multi()

    def paint_multi(self):
        self.multi_lbl.setText("   ·   ".join("%s: %s" % (SHORT[k], os.path.basename(p))
                                              for k, p in self.multi.items()))

    def clear_paths(self):
        self.multi = {}
        self.paths = {k: "" for k in UPLOAD_ROWS}
        self.paint_multi()
        self.paint_file_line()

    def _replace(self):
        return self.mode.get() == "replace"

    def _busy(self, on):
        if on:
            ctx.busy(True, "Loading file...")

            def tick(done, total):
                ctx.status(("Loading file...   %s of %s rows" % (f"{done:,}", f"{total:,}"))
                           if total else "Reading file...   %s rows" % f"{done:,}")
                QApplication.processEvents()
            ctx.db.progress = tick
        else:
            ctx.db.progress = None
            ctx.busy(False)

    def upload_one(self):
        if guard_admin("upload"):
            return
        k = self.ds.get()
        p = self.paths.get(k)
        if not p:
            QMessageBox.warning(self, "No file", "Select a file for %s first." % DATASET_LABEL[k])
            return
        rep = self._replace()
        if rep and self.row_count(k) and not ask(
                "Confirm CHANGE", "The stored %s data (%d rows) will be deleted and replaced.\n"
                "Continue?" % (DATASET_LABEL[k], self.row_count(k))):
            return
        self._busy(True)
        try:
            ok = load_file(p, dataset=k, replace=rep, log=self.log, header_row=self.hdr.value())
        finally:
            self._busy(False)
        if ok:
            self.after_upload()

    def upload_all(self):
        if guard_admin("upload"):
            return
        keys = [k for k in UPLOAD_ROWS if self.multi.get(k)]
        if not keys:
            QMessageBox.warning(self, "No file", "Select at least one file (Select multiple...).")
            return
        if self._replace() and any(self.row_count(k) for k in keys):
            if not ask("Confirm CHANGE", "Stored data will be replaced for the selected data "
                       "sets.\nContinue?"):
                return
        self._busy(True)
        try:
            ok = sum(1 for k in keys if load_file(self.multi[k], dataset=k, replace=self._replace(),
                                                  log=self.log, header_row=self.hdr.value()))
        finally:
            self._busy(False)
        self.log("--- %d of %d files processed ---" % (ok, len(keys)))
        self.after_upload()

    def after_upload(self):
        self.refresh_status()
        if "reload_all_pages" in ctx.hooks:
            ctx.hooks["reload_all_pages"]()
        if "refresh_status" in ctx.hooks:
            ctx.hooks["refresh_status"]()
        if self.sections["health"].head.isChecked():
            self.refresh_health()
        self.paint_badges()

    # ---------------- stored data ----------------
    def row_count(self, key):
        if key == "traveler":
            return ctx.db.wf_counts()["traveler_rows"]
        if key == "flags":
            return len(ctx.db.flag_all())
        return ctx.db.counts()[key]

    def refresh_status(self):
        c = ctx.db.counts()
        sp = ctx.db.rw_span()
        for k, (n, d0, d1) in self.store_rows.items():
            n.setText("{:,}".format(self.row_count(k)).replace(",", " "))
            if k == "rw" and sp:
                d0.setText(str(sp["d0"]))
                d1.setText(str(sp["d1"]))
            else:
                d0.setText("—")
                d1.setText("—")
        try:
            self.extra_lbl.setText("%s production steps · %d rows removed at import"
                                   % ("{:,}".format(ctx.db.ops_count()).replace(",", " "),
                                      ctx.db.discarded_count()))
        except Exception:
            self.extra_lbl.setText("")
        warn = []
        unknown = ctx.db.unknown_work_centers()
        if unknown:
            warn.append("<a href='wc' style='color:%s'>%d work centre(s) not defined in COMP - %s"
                        "  (click to see all)</a>" % (ctx.TH["wf_wait"], len(unknown),
                                                       ", ".join(unknown[:8]) +
                                                       (", ..." if len(unknown) > 8 else "")))
        try:
            k = ctx.db.get_meta("x_rule_check", None) or {}
            miss = ctx.db.get_meta("sap_missing_cols", None) or []
        except Exception:
            k, miss = {}, []
        if k.get("serials"):
            most = sorted((k.get("steps") or {}).items(), key=lambda kv: -kv[1])[:4]
            warn.append("X check: %d serial(s) moved past a step without its X%s"
                        % (k["serials"], ("  (most: %s)" % ", ".join("%s x%d" % kv for kv in most))
                           if most else ""))
        if miss:
            warn.append("Not in the last SAP file: %s" % ", ".join(miss))
        if c["shipment"]:
            warn.append("<span style='color:%s'>Sold serials on file: %d (from the shipment "
                        "report)</span>" % (ctx.TH["muted"], len(ctx.db.sold_serial_keys())))
        self.warn_lbl.setText("<br>".join(warn))
        self.warn_lbl.setVisible(bool(warn))
        self.paint_file_line()
        self.paint_tiles()
        self.paint_mode_lbl()

    def show_unknown_wcs(self):
        unknown = ctx.db.unknown_work_centers()
        dlg = QDialog(self)
        dlg.setWindowTitle("Work centres not defined in COMP")
        dlg.resize(440, 480)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label("%d work centre(s) not defined in COMP" % len(unknown), obj="CardTitle"))
        lay.addWidget(label("These codes appear in the rework data but have no row in the COMP "
                            "file, so they have no description and no class.  Add them to COMP and "
                            "upload it again.", muted=True, wrap=True))
        lst = QListWidget()
        lst.addItems(["%3d.  %s" % (i, wc) for i, wc in enumerate(unknown, 1)])
        lay.addWidget(lst, 1)
        lay.addWidget(hbox(button("Copy list", slot=lambda: QApplication.clipboard().setText(
            "\n".join(unknown))), None, button("Close", slot=dlg.accept)))
        dlg.exec()

    def delete_dataset(self, key):
        if guard_admin("delete"):
            return
        n = self.row_count(key)
        if not n:
            QMessageBox.information(self, "Empty", "No rows stored for %s." % DATASET_LABEL[key])
            return
        if not ask("Confirm delete", "Delete all %d rows of %s?" % (n, DATASET_LABEL[key])):
            return
        if key == "traveler":
            ctx.db.wf_clear_traveler()
        elif key == "flags":
            ctx.db.conn.execute("DELETE FROM serial_flags")
            ctx.db.conn.commit()
            ctx.db.bump_annotations()
        else:
            ctx.db.clear(key)
            if key == "rw":
                ctx.db.wf_clear_part_data()
        invalidate_caches()
        if ctx.wf_an is not None:
            ctx.wf_an._cache.clear()
        self.log("DELETED all rows of %s" % DATASET_LABEL[key])
        self.after_upload()

    def wipe(self):
        if guard_admin("delete"):
            return
        if not ask("Confirm", "Delete ALL stored data?\nThis cannot be undone."):
            return
        ctx.db.clear_all()
        ctx.db.wf_clear_traveler()
        ctx.db.wf_clear_part_data()
        invalidate_caches()
        if ctx.wf_an is not None:
            ctx.wf_an._cache.clear()
        self.log("DELETED all data")
        self.after_upload()

    def edit_traveler(self):
        if guard_admin("edit the traveler"):
            return
        parts = ctx.db.wf_parts()
        if not parts:
            QMessageBox.information(self, "No traveler data", "Upload a TRAVELER file first.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit traveler")
        dlg.resize(580, 660)
        lay = QVBoxLayout(dlg)
        cb = QComboBox()
        for pk, disp in parts:
            cb.addItem(disp, pk)
        lay.addWidget(hbox(label("Part"), cb, None))
        lay.addWidget(label("Untick an operation to leave it out of every analysis for this "
                            "part - the route stays intact, it can be ticked back any time.",
                            muted=True, wrap=True))
        lst = QListWidget()
        lay.addWidget(lst, 1)

        def load_part():
            lst.clear()
            pk = cb.currentData()
            for r in ctx.db.wf_traveler_full(pk):
                it = QListWidgetItem("%-8s %s" % (r.get("label") or r["op"], r["desc"]))
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if r["active"] else Qt.Unchecked)
                it.setData(Qt.UserRole, (r["op"], r.get("seq") or ""))
                lst.addItem(it)
        cb.currentIndexChanged.connect(lambda _i: load_part())
        load_part()

        def save():
            if guard_admin("edit the traveler of"):
                return
            active = {lst.item(i).data(Qt.UserRole): lst.item(i).checkState() == Qt.Checked
                      for i in range(lst.count())}
            if not any(active.values()):
                QMessageBox.warning(dlg, "Nothing left", "At least one operation must stay ticked.")
                return
            ctx.db.wf_set_traveler_active(cb.currentData(), active)
            invalidate_caches()
            if ctx.wf_an is not None:
                ctx.wf_an._cache.clear()
            ctx.status("Traveler updated for %s." % cb.currentText())
            dlg.accept()
            self.after_upload()
        lay.addWidget(hbox(button("Select all", slot=lambda: [lst.item(i).setCheckState(Qt.Checked)
                                                              for i in range(lst.count())]), None,
                           button("Cancel", slot=dlg.reject),
                           button("Save", primary=True, slot=save)))
        dlg.exec()

    # ---------------- sections ----------------
    def build_startup(self):
        self.start_lbl = label("-")
        self.start_lbl.setFont(QFont(MONO, 10))
        self.start_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sections["startup"].blay.addWidget(self.start_lbl)

    def show_startup(self):
        info = getattr(ctx, "startup", None) or {}
        total = info.get("total") or ctx.hooks.get("startup_seconds")
        lines = ["TOTAL %.1f s" % total] if total else ["-"]
        if info.get("before_python") is not None:
            lines.append("program files %.1f s" % info["before_python"])
        if info.get("database") is not None:
            lines.append("database open %.1f s" % info["database"])
        if info.get("pages") is not None:
            lines.append("pages %.1f s" % info["pages"])
        self.start_lbl.setText("\n".join(lines))

    def build_health(self):
        self.health_box = QVBoxLayout()
        self.health_box.setSpacing(4)
        self.sections["health"].blay.addLayout(self.health_box)

    def refresh_health(self):
        while self.health_box.count():
            it = self.health_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        try:
            checks = data_health(ctx.db)
        except Exception as e:
            self.health_box.addWidget(label("could not be checked: %s" % e, muted=True))
            return
        colors = {"ok": "#3FB86A", "warn": "#E6B53C", "bad": "#E05252"}
        for c in checks:
            row = QWidget()
            g = QHBoxLayout(row)
            g.setContentsMargins(0, 0, 0, 0)
            dot = label("●")
            dot.setStyleSheet("color: %s;" % colors.get(c["level"], ctx.TH["muted"]))
            dot.setFixedWidth(14)
            t = label(c["title"])
            t.setFixedWidth(150)
            t.setStyleSheet("font-weight: 600;")
            d = label(c["detail"], muted=(c["level"] == "ok"), wrap=True)
            g.addWidget(dot, 0, Qt.AlignTop)
            g.addWidget(t, 0, Qt.AlignTop)
            g.addWidget(d, 1)
            if c["rows"]:
                d.setCursor(Qt.PointingHandCursor)
                d.mousePressEvent = lambda _e, cc=c: self.show_health_rows(cc)
            self.health_box.addWidget(row)

    def show_health_rows(self, check):
        dlg = QDialog(self)
        dlg.setWindowTitle(check["title"])
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(check["detail"], wrap=True))
        lst = QListWidget()
        lst.addItems(["   ".join(str(x) for x in row if str(x)) for row in check["rows"]])
        lay.addWidget(lst, 1)
        lay.addWidget(hbox(None, button("Close", slot=dlg.accept)))
        dlg.exec()

    def build_rules(self):
        b = self.sections["rules"].blay
        cur = read_rules()
        b.addWidget(label("Std margin", muted=True))
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0, 100)
        self.tol.setDecimals(1)
        self.tol.setValue(cur["std_tolerance"] * 100)
        self.tol.setFixedWidth(150)
        b.addWidget(hbox(self.tol, label("%", muted=True), None))
        b.addWidget(label("An operation counts as finished this far under its standard time.",
                          muted=True, wrap=True))
        b.addWidget(label("Multi-part machines", muted=True))
        self.multi_ed = QLineEdit(cur["multi_part_groups"])
        b.addWidget(self.multi_ed)
        b.addWidget(label("Machine groups that really do run several parts at once.  The shift "
                          "timetable is on the Forecast page.", muted=True, wrap=True))
        self.rules_msg = label("", muted=True)
        sb = button("Save rules", primary=True, slot=self.save_rules)
        b.addWidget(hbox(self.rules_msg, None, sb))
        self.write_widgets += [sb, self.tol, self.multi_ed]

    def save_rules(self):
        if guard_admin("change the shop rules for"):
            return
        now = save_rules(ctx.db, std_tolerance=self.tol.value() / 100.0,
                         multi_part_groups=self.multi_ed.text())
        self.tol.setValue(now["std_tolerance"] * 100)
        self.multi_ed.setText(now["multi_part_groups"])
        self.rules_msg.setText("saved - every page now reads the data this way")
        if not now.get("changed"):
            self.rules_msg.setText("saved - nothing changed")
            return
        self.log("SHOP RULES  margin %g%%, multi-part: %s"
                 % (now["std_tolerance"] * 100, now["multi_part_groups"] or "(none)"))
        self.after_upload()

    def build_shared(self):
        b = self.sections["shared"].blay
        b.addWidget(label("Database file", muted=True))
        self.dbpath = QLineEdit(ctx.db.path)
        self.dbpath.setReadOnly(True)
        self.dbpath.setFont(QFont(MONO, 10))
        b.addWidget(self.dbpath)
        b1 = button("Shared folder...", slot=self.choose_shared)
        b2 = button("Program folder", slot=lambda: self.switch_database(default_db_path()))
        b.addWidget(hbox(b1, b2, None))
        self.sync_lbl = label("", muted=True)
        b.addWidget(self.sync_lbl)
        self.write_widgets += [b1, b2]

    def switch_database(self, new_path):
        if guard_admin("switch the database of"):
            return
        try:
            fresh = Database(new_path)
        except Exception as e:
            QMessageBox.critical(self, "Cannot open database", str(e))
            return
        ctx.db.close()
        ctx.db = fresh
        apply_rules(fresh)
        ctx.wf_an = WFAnalyzer(ctx.db)
        invalidate_caches()
        ctx.prefs["db_path"] = new_path
        save_prefs(ctx.prefs)
        self.dbpath.setText(new_path)
        if write_db_pointer(new_path):
            self.log("Database switched to %s (shared location recorded for other users)" % new_path)
        else:
            self.log("Database switched to %s (could not record the shared location)" % new_path)
        self.after_upload()

    def choose_shared(self):
        if guard_admin("move"):
            return
        folder = QFileDialog.getExistingDirectory(self, "Select the shared folder")
        if not folder:
            return
        new_path = os.path.join(folder, DB_NAME)
        if os.path.abspath(new_path) == os.path.abspath(ctx.db.path):
            return
        exists = os.path.isfile(new_path)
        if ask("Switch database", ("An existing database was found there and will be used."
                                   if exists else "A new empty database will be created there.")
               + "\n\n" + new_path + "\n\nContinue?"):
            self.switch_database(new_path)

    def build_week(self):
        b = self.sections["week"].blay
        self.week_cb = QComboBox()
        self.week_cb.addItems(WEEKDAY_NAMES)
        self.week_cb.activated.connect(lambda _i: self.on_week_start())
        rb = button("Re-number rows", slot=self.renumber)
        b.addWidget(hbox(label("Starts on", muted=True), self.week_cb, rb, None))
        self.week_info = label("", muted=True)
        b.addWidget(self.week_info)
        b.addWidget(label("Per page", muted=True))
        g = QGridLayout()
        self.page_week = {}
        for i, (k, name) in enumerate(PAGE_WEEK_PAGES):
            cb = QComboBox()
            cb.addItems([DEFAULT] + WEEKDAY_NAMES)
            cb.activated.connect(lambda _i, kk=k: self.on_page_week(kk))
            g.addWidget(label(name), i, 0)
            g.addWidget(cb, i, 1, alignment=Qt.AlignRight)
            self.page_week[k] = cb
            self.write_widgets.append(cb)
        b.addLayout(g)
        b.addWidget(label("A page left on Default uses the week start above.  Only the week NUMBER "
                          "changes - no row is re-written.", muted=True, wrap=True))
        self.write_widgets += [self.week_cb, rb]

    def paint_week(self):
        ws = ctx.db.week_start()
        self.week_cb.setCurrentIndex(ws)
        y, w = iso_year_week(datetime.date.today(), week_offset(ws))
        a, bb = week_bounds(y, w, ws)
        self.week_info.setText("%s to %s.  Today is FW%s (%s .. %s)."
                               % (WEEKDAY_NAMES[ws], WEEKDAY_NAMES[(ws + 6) % 7], w, a, bb)
                               if a else "Today is FW%s." % w)
        own = ctx.db.page_week_starts()
        for k, cb in self.page_week.items():
            g = own.get(k)
            cb.setCurrentIndex(0 if g is None else g + 1)

    def renumber(self):
        if guard_admin("re-number the weeks of"):
            return
        n = ctx.db.recompute_weeks()
        invalidate_caches()
        self.paint_week()
        self.log("Re-numbered %d rows for a %s week start." % (n, WEEKDAY_NAMES[ctx.db.week_start()]))
        self.after_upload()

    def on_week_start(self):
        new = self.week_cb.currentIndex()
        if new == ctx.db.week_start() and ctx.db.week_numbers_are_current():
            return
        if guard_admin("change the week start of"):
            self.paint_week()
            return
        ctx.db.set_week_start(new)
        invalidate_caches()
        self.paint_week()
        self.log("Week start changed to %s - rows re-numbered." % WEEKDAY_NAMES[new])
        self.after_upload()

    def on_page_week(self, k):
        if guard_admin("change the week start of"):
            self.paint_week()
            return
        i = self.page_week[k].currentIndex()
        ctx.db.set_page_week_start(k, None if i == 0 else i - 1)
        invalidate_caches()
        self.log("Week start for %s: %s" % (k, self.page_week[k].currentText()))
        if "reload_all_pages" in ctx.hooks:
            ctx.hooks["reload_all_pages"]()

    def build_merge(self):
        b = self.sections["merge"].blay
        self.merge_list = QListWidget()
        self.merge_list.setMaximumHeight(130)
        self.merge_list.itemDoubleClicked.connect(lambda _i: self.edit_merge(self.merge_list.currentRow()))
        b.addWidget(self.merge_list)
        n = button("New...", slot=lambda: self.edit_merge(None))
        e = button("Edit...", slot=lambda: self.edit_merge(self.merge_list.currentRow()))
        d = button("Delete", danger=True, slot=self.delete_merge)
        b.addWidget(hbox(n, e, d, None))
        self.merge_warn = label("", wrap=True)
        self.merge_warn.setStyleSheet("color: %s;" % ctx.TH["wf_wait"])
        b.addWidget(self.merge_warn)
        b.addWidget(label("Merge on", muted=True))
        g = QGridLayout()
        self.merge_pages = {}
        for i, (k, name) in enumerate(PAGE_WEEK_PAGES):
            t = Toggle(name, slot=lambda kk=k: self.on_merge_page(kk))
            g.addWidget(t, i // 2, i % 2)
            self.merge_pages[k] = t
            self.write_widgets.append(t)
        b.addLayout(g)
        b.addWidget(label("A ticked page shows each merged part as one part (serials and hours "
                          "added up); an unticked page keeps the part numbers apart.",
                          muted=True, wrap=True))
        self.write_widgets += [n, e, d]

    def paint_merges(self):
        self.merge_list.clear()
        groups = ctx.db.part_merges()
        for g in groups:
            self.merge_list.addItem("%s   =  %s" % (g["name"], " + ".join(g["members"])))
        _err, warns = check_part_merges(ctx.db, groups)
        self.merge_warn.setText("\n".join(warns))
        pages = ctx.db.merge_pages()
        for k, t in self.merge_pages.items():
            t.blockSignals(True)
            t.setChecked(bool(pages.get(k)))
            t.blockSignals(False)

    def save_merges(self, groups, what):
        errors, warns = check_part_merges(ctx.db, groups)
        if errors:
            QMessageBox.critical(self, "Merged parts", "\n".join(errors))
            return False
        ctx.db.set_part_merges(groups)
        invalidate_caches()
        self.log("Merged parts: %s." % what)
        self.paint_merges()
        self.after_upload()
        if warns:
            QMessageBox.warning(self, "Merged parts", "\n".join(warns))
        return True

    def on_merge_page(self, k):
        if guard_admin("change the merged parts of"):
            self.paint_merges()
            return
        on = self.merge_pages[k].isChecked()
        ctx.db.set_merge_page(k, on)
        invalidate_caches()
        self.log("Merged parts on %s: %s" % (dict(PAGE_WEEK_PAGES).get(k, k),
                                             "MERGED" if on else "separate"))
        self.after_upload()

    def edit_merge(self, index):
        if guard_admin("merge the parts of"):
            return
        groups = [dict(g) for g in ctx.db.part_merges()]
        if index is not None and not 0 <= index < len(groups):
            index = None
        cur = groups[index] if index is not None else None
        cands = {}
        for p in ctx.db.all_parts():
            k = p["part_key"] or norm_text(p["part_no"])
            if k:
                cands.setdefault(k, p["part_no"])
        for pk, disp in ctx.db.wf_parts():
            if pk:
                cands.setdefault(pk, disp)
        other = {m for i, g in enumerate(groups) if i != index for m in g["members"]}
        keys = sorted(k for k in cands if k not in other)
        picked = list(cur["members"]) if cur else []
        dlg = QDialog(self)
        dlg.setWindowTitle("Merged part")
        dlg.resize(480, 540)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label("Tick the parts to count as one.  The first one ticked gives the route, "
                            "part group and engineer.", muted=True, wrap=True))
        find = QLineEdit()
        find.setPlaceholderText("Find")
        lay.addWidget(find)
        lst = QListWidget()
        lay.addWidget(lst, 1)
        sel_lbl = label("", wrap=True)
        lay.addWidget(sel_lbl)
        name = QLineEdit(cur["name"] if cur else "")
        auto = {"name": merge_name(picked) if picked else ""}
        lay.addWidget(hbox(label("Name"), name))

        def fill():
            q = norm_text(find.text())
            lst.blockSignals(True)
            lst.clear()
            for k in keys:
                if q and q not in k and k not in picked:
                    continue
                it = QListWidgetItem(k)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if k in picked else Qt.Unchecked)
                lst.addItem(it)
            lst.blockSignals(False)

        def changed(it):
            k = it.text()
            if it.checkState() == Qt.Checked and k not in picked:
                picked.append(k)
            elif it.checkState() != Qt.Checked and k in picked:
                picked.remove(k)
            sel_lbl.setText("  +  ".join(picked) or "(nothing picked)")
            if name.text() in ("", auto["name"]):
                auto["name"] = merge_name(picked)
                name.setText(auto["name"])
        find.textChanged.connect(lambda _t: fill())
        lst.itemChanged.connect(changed)
        fill()
        sel_lbl.setText("  +  ".join(picked) or "(nothing picked)")

        def ok():
            new = {"name": name.text().strip() or merge_name(picked), "members": list(picked)}
            lst_new = list(groups)
            if index is None:
                lst_new.append(new)
            else:
                lst_new[index] = new
            if self.save_merges(lst_new, "%s saved (%s)" % (new["name"], " + ".join(picked))):
                dlg.accept()
        lay.addWidget(hbox(None, button("Cancel", slot=dlg.reject),
                           button("Save", primary=True, slot=ok)))
        dlg.exec()

    def delete_merge(self):
        i = self.merge_list.currentRow()
        if i < 0 or guard_admin("merge the parts of"):
            return
        groups = [dict(g) for g in ctx.db.part_merges()]
        g = groups.pop(i)
        if ask("Merged parts", "Show %s as separate parts again?" % " and ".join(g["members"])):
            self.save_merges(groups, "%s removed" % g["name"])

    def build_clean(self):
        b = self.sections["clean"].blay
        self.max_hour = QDoubleSpinBox()
        self.max_hour.setRange(1, 999)
        self.max_hour.setDecimals(1)
        self.max_hour.setValue(float(ctx.db.max_rw_hour()))
        sv = button("Save", slot=self.save_max_hour)
        b.addWidget(hbox(label("Max rework hour in one row", muted=True), self.max_hour, sv, None))
        self.date_pref = Seg([("MM/DD/YYYY", "month"), ("DD/MM/YYYY", "day")],
                             ctx.db.get_meta("date_first", "month"))
        self.date_pref.changed.connect(self.save_date_pref)
        b.addWidget(hbox(label("Dates in uploads", muted=True), self.date_pref, None))
        self.write_widgets += [self.max_hour, sv, self.date_pref]

    def save_max_hour(self):
        if guard_admin("change the limits of"):
            return
        v = self.max_hour.value()
        ctx.db.set_meta("max_rw_hour", v)
        self.log("Maximum rework hour per row set to %s." % fmt_hour(v))

    def save_date_pref(self, which):
        if guard_admin("change the date format of"):
            self.date_pref.set(ctx.db.get_meta("date_first", "month"))
            return
        ctx.db.set_meta("date_first", which)

    # ---------------- ADMIN: users & roles ----------------
    def build_users(self):
        b = self.sections["users"].blay
        b.addWidget(label("Only the admin and co-admins see this section.  There is one ADMIN and "
                          "nobody can change that account.  A CO-ADMIN can change everybody "
                          "else's role, including their own.", muted=True, wrap=True))
        self.users_t = DataTable()
        self.users_t.fit_rows(10)
        self.users_t.on_row_click = lambda ri, row: self._user_picked(ri)
        b.addWidget(self.users_t)
        self.user_sel = label("Selected: -", muted=True)
        self.role_cb = QComboBox()
        self.role_cb.addItems([ROLE_TITLE[r] for r in ROLE_TITLE if r != "admin"])
        self._roles = [r for r in ROLE_TITLE if r != "admin"]
        b.addWidget(hbox(self.user_sel, self.role_cb,
                         button("Set role", primary=True, slot=self.set_role),
                         button("Reset password", slot=self.reset_pw), None, spacing=8))
        b.addWidget(hbox(button("Enable / disable", slot=self.toggle_user), None))
        b.addWidget(label("Roles you can give: " + " · ".join(ROLE_TITLE[r] for r in self._roles),
                          muted=True, wrap=True))
        b.addWidget(hbox(IconButton("download", "", self.export_users,
                                    text="  Download user information"),
                         button("Upload user information", slot=self.import_users), None))
        self.notes_t = Toggle("Show every user's private notes", slot=self._notes)
        b.addWidget(self.notes_t)
        self._picked_user = None

    def reload_users(self):
        self.users = ctx.db.users(active_only=False)
        rows = []
        for u in self.users:
            rows.append(["%s%s" % (u["username"], "  🔒" if u["username"] == wip_core.OWNER_USERNAME
                                   else ""), u["full_name"],
                         ROLE_TITLE.get(u["role"], u["role"] or ""),
                         "set" if u["pw_hash"] else "not set yet", u["last_login"] or "—",
                         "yes" if u["active"] else "disabled"])
        cfg = {}
        for ri, u in enumerate(self.users):
            if u["role"] in ("admin", "coadmin"):
                cfg[(ri, 2)] = ctx.TH["accent"]
            if not u["pw_hash"]:
                cfg[(ri, 3)] = ctx.TH["wf_wait"]
            if not u["active"]:
                cfg[(ri, 5)] = ctx.TH["danger"]
        self.users_t.set_data([("USER", 110), ("NAME", 170), ("ROLE", 100), ("PASSWORD", 100),
                               ("LAST SIGN-IN", 140), ("ACTIVE", 80)], rows, None, None,
                              cell_fg=cfg)
        self.notes_t.blockSignals(True)
        self.notes_t.setChecked(bool(ctx.show_all_notes))
        self.notes_t.blockSignals(False)

    def _user_picked(self, ri):
        if 0 <= ri < len(self.users):
            u = self.users[ri]
            self._picked_user = u["username"]
            self.user_sel.setText("Selected: %s" % u["username"])
            if u["role"] in self._roles:
                self.role_cb.setCurrentIndex(self._roles.index(u["role"]))

    def _guarded(self, title, fn):
        if guard_admin("change the users of"):
            return
        try:
            fn()
        except (PermissionError, ValueError) as e:
            QMessageBox.warning(self, title, str(e))
        except sqlite3.Error as e:
            QMessageBox.warning(self, title, busy_text(e))
        self.reload_users()
        me = ctx.username()
        if me:
            fresh = ctx.db.user(me)
            if fresh:
                ctx.user.update(fresh)
                if ctx.window is not None:
                    ctx.window.paint_header()
                self.paint_admin()

    def _need_user(self):
        if not self._picked_user:
            QMessageBox.information(self, "Users", "Pick a user from the list first.")
            return None
        return self._picked_user

    def set_role(self):
        u = self._need_user()
        if u:
            role = self._roles[self.role_cb.currentIndex()]
            self._guarded("Set role", lambda: ctx.db.set_user_role(u, role))

    def reset_pw(self):
        u = self._need_user()
        if u and ask("Reset password", "Clear %s's password?  They will choose a new one at their "
                     "next sign-in." % u):
            self._guarded("Reset password", lambda: ctx.db.clear_password(u))

    def toggle_user(self):
        u = self._need_user()
        if u:
            row = ctx.db.user(u)
            self._guarded("Enable / disable", lambda: ctx.db.set_user_active(u, not row["active"]))

    def _notes(self):
        if self.notes_t.isChecked() and guard_admin("read everybody's private notes in"):
            self.notes_t.blockSignals(True)
            self.notes_t.setChecked(False)
            self.notes_t.blockSignals(False)
            return
        ctx.show_all_notes = self.notes_t.isChecked()
        for p in (ctx.window.pages if ctx.window is not None else []):
            if p is not None and hasattr(p, "notes_visibility_changed"):
                try:
                    p.notes_visibility_changed()
                except Exception:
                    pass

    def export_users(self):
        if guard_admin("download the accounts of"):
            return
        path, _x = QFileDialog.getSaveFileName(
            self, "Save user information",
            "WIP_TRACKER_users_%s.json" % datetime.date.today().isoformat(), "User backup (*.json)")
        if not path:
            return
        try:
            info = ctx.db.users_export(path)
        except Exception as e:
            QMessageBox.critical(self, "Download user information", str(e))
            return
        QMessageBox.information(self, "Download user information",
                                "%d account(s) written, %d of them with a password set.\n\n%s\n\n"
                                "The file holds salted password hashes - keep it somewhere the "
                                "shared database is not." % (info["users"], info["with_password"],
                                                             path))

    def import_users(self):
        if guard_admin("upload the accounts of"):
            return
        path, _x = QFileDialog.getOpenFileName(self, "Open user information", "",
                                               "User backup (*.json)")
        if not path:
            return
        replace = ask("Upload user information",
                      "Overwrite the accounts that already exist here?\n\nYES - the file wins: role "
                      "and password are restored from it.\n\nNO - only accounts missing here are "
                      "added.\n\nEither way, nobody is deleted.")
        try:
            info = ctx.db.users_import(path, replace=replace, by=ctx.username() or None)
        except Exception as e:
            QMessageBox.critical(self, "Upload user information", str(e))
            return
        self.reload_users()
        QMessageBox.information(self, "Upload user information",
                                "%d account(s) in the file (saved %s).\n\n%d added\n%d overwritten\n"
                                "%d left alone" % (info["in_file"], info["exported_at"] or "?",
                                                   info["added"], info["updated"], info["skipped"]))

    # ---------------- ADMIN: open sessions ----------------
    def build_sessions(self):
        b = self.sections["sessions"].blay
        b.addWidget(label("Closing is a request: the other copy saves and closes itself within "
                          "a minute.", muted=True, wrap=True))
        self.sess_t = DataTable()
        self.sess_t.fit_rows(8)
        self.sess_t.on_row_click = lambda ri, row: self._sess_picked(ri)
        b.addWidget(self.sess_t)
        self.sess_diag = label("", muted=True, wrap=True)
        b.addWidget(self.sess_diag)
        b.addWidget(hbox(button("Refresh", slot=self.refresh_sessions_now),
                         button("Close selected", danger=True, slot=self.close_selected),
                         button("Close all others", danger=True, slot=self.close_others),
                         button("Forget selected", slot=self.forget_selected), None, spacing=8))
        self._sess = []
        self._sess_sel = None

    def _sess_picked(self, ri):
        if 0 <= ri < len(self._sess):
            self._sess_sel = self._sess[ri]["session_id"]

    def tick_sessions(self):
        if not self.isVisible() or not self.sections["sessions"].head.isChecked():
            self.sess_timer.stop()
            return
        try:
            self.reload_sessions()
        except Exception as e:
            self.sess_diag.setText("refresh failed: %s" % e)

    def refresh_sessions_now(self):
        try:
            ctx.db._session_purged = 0.0
        except Exception:
            pass
        self.reload_sessions()

    def reload_sessions(self):
        now = datetime.datetime.now()
        self._sess = list(ctx.db.all_sessions())
        rows, cfg = [], {}
        for ri, s in enumerate(self._sess):
            ago = None
            try:
                ago = (now - datetime.datetime.fromisoformat(s["last_seen"])).total_seconds()
            except Exception:
                pass
            seen = ((s["last_seen"] or "")[-8:] if ago is None else
                    "now" if ago < 20 else "%d s ago" % ago if ago < 90 else
                    "%d min ago" % (ago // 60) if ago < 5400 else "%.1f h ago" % (ago / 3600.0))
            if s["close_requested"]:
                state = "closing"
                cfg[(ri, 4)] = ctx.TH["danger"]
            elif ago is not None and ago > wip_core.SESSION_STALE_SECONDS:
                state = "idle (stale)"
                cfg[(ri, 4)] = ctx.TH["wf_wait"]
            else:
                state = "active"
            me = " (this copy)" if s["session_id"] == ctx.session_id else ""
            rows.append([s["username"] or "?", (s["host"] or "?") + me,
                         (s["started_at"] or "")[-8:-3], seen, state])
        self.sess_t.set_data([("USER", 110), ("COMPUTER", 220), ("OPENED", 80), ("LAST SEEN", 110),
                              ("STATE", 110)], rows, None, None, cell_fg=cfg)
        db = ctx.db
        bits = ["%d session row(s) in the file" % len(rows)]
        if getattr(db, "read_only", False):
            bits.append("THIS COPY CANNOT WRITE to the database, so it cannot ask anybody to close")
        bits.append("database: %s journal, %s" % (str(getattr(db, "journal_mode", "?")).upper(),
                                                  "on a network share" if getattr(db, "on_network",
                                                                                  False)
                                                  else "on this computer's own disk"))
        self.sess_diag.setText("   ·   ".join(bits))

    def close_selected(self):
        if guard_admin("close other people's copies of"):
            return
        sid = self._sess_sel
        if not sid or sid == ctx.session_id:
            QMessageBox.information(self, "Open sessions", "Pick a session other than this one.")
            return
        self._ask_and_send([sid])

    def close_others(self):
        if guard_admin("close other people's copies of"):
            return
        ids = [s["session_id"] for s in ctx.db.live_sessions() if s["session_id"] != ctx.session_id]
        if not ids:
            QMessageBox.information(self, "Open sessions", "Nobody else has it open.")
            return
        self._ask_and_send(ids)

    def _ask_and_send(self, ids):
        if ask("Close sessions", "Ask %d other copy/copies of the program to close?" % len(ids)):
            try:
                ctx.db.request_session_close(ids, by=ctx.username())
            except sqlite3.Error as e:
                QMessageBox.warning(self, "Close sessions", busy_text(e))
            self.reload_sessions()

    def forget_selected(self):
        if guard_admin("change the session list of"):
            return
        sid = self._sess_sel
        if not sid or sid == ctx.session_id:
            QMessageBox.information(self, "Open sessions", "Pick a session other than this one.")
            return
        if ask("Forget session", "Remove this session row from the list?\n\nThis does NOT close "
               "that copy - it only forgets it here.  Use it for a copy that crashed."):
            try:
                ctx.db.session_end(sid)
            except Exception:
                pass
            self.reload_sessions()

    # ---------------- ADMIN: idle close & maintenance ----------------
    def build_idle(self):
        b = self.sections["idle"].blay
        t = label("Close idle copies")
        t.setStyleSheet("font-weight: 700;")
        b.addWidget(t)
        b.addWidget(label("Close after", muted=True))
        self.idle = QSpinBox()
        self.idle.setRange(0, 24 * 60)
        self.idle.setValue(int(read_rules().get("idle_minutes") or 0))
        self.idle.setFixedWidth(150)
        b.addWidget(hbox(self.idle, label("minutes (0 = never)", muted=True),
                         button("Save", primary=True, slot=self.save_idle), None, spacing=8))
        self.idle_msg = label("", muted=True)
        b.addWidget(self.idle_msg)
        b.addWidget(label("Applies to everybody, on every machine.  A copy left idle this long "
                          "saves and closes itself.", muted=True, wrap=True))
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setObjectName("Sep")
        b.addWidget(sep)
        self.maint_lbl = label("")
        b.addWidget(self.maint_lbl)
        b.addWidget(label("While it is on, every copy closes itself on its next check and nobody "
                          "can sign in except the admin — for replacing the database or a new "
                          "version.", muted=True, wrap=True))
        self.maint_btn = button("Turn on", danger=True, slot=self.toggle_maintenance)
        b.addWidget(hbox(self.maint_btn, None))

    def save_idle(self):
        if guard_admin("change the idle close time of"):
            return
        try:
            now = save_rules(ctx.db, idle_minutes=float(self.idle.value()))
        except sqlite3.Error as e:
            QMessageBox.warning(self, "Idle close", busy_text(e))
            return
        self.idle.setValue(int(now["idle_minutes"] or 0))
        self.idle_msg.setText("saved - copies now close after %g minutes idle" % now["idle_minutes"]
                              if now["idle_minutes"] else
                              "saved - copies are never closed for being idle")

    def paint_maintenance(self):
        try:
            on, who, when = ctx.db.maintenance()
        except sqlite3.Error:
            self.maint_lbl.setText("<b>Maintenance mode</b> · could not be read (the file is busy)")
            return
        if on:
            self.maint_lbl.setText("<b>Maintenance mode</b> · <span style='color:%s'>ON</span> - "
                                   "turned on by %s at %s" % (ctx.TH["danger"], who or "?",
                                                               str(when)[-8:] or "?"))
            self.maint_btn.setText("Turn off")
        else:
            self.maint_lbl.setText("<b>Maintenance mode</b> · <span style='color:%s'>off</span>"
                                   % ctx.TH["muted"])
            self.maint_btn.setText("Turn on")

    def toggle_maintenance(self):
        if guard_admin("switch maintenance mode of"):
            return
        try:
            on, _w, _n = ctx.db.maintenance()
        except sqlite3.Error as e:
            QMessageBox.warning(self, "Maintenance mode", busy_text(e))
            return
        if on:
            try:
                ctx.db.set_maintenance(False, by=ctx.username())
            except sqlite3.Error as e:
                QMessageBox.warning(self, "Maintenance mode", busy_text(e))
            self.paint_maintenance()
            return
        if not ask("Maintenance mode", "Turn maintenance mode ON?\n\nEvery other copy closes itself "
                   "within about ten seconds, and nobody but an admin can open the program until "
                   "you turn it off again."):
            return
        try:
            ctx.db.set_maintenance(True, by=ctx.username())
        except sqlite3.Error as e:
            QMessageBox.warning(self, "Maintenance mode", busy_text(e))
        self.paint_maintenance()

    def refresh_theme(self):
        self.refresh_status()
        self.paint_nav()
        self.paint_db_pill()
        self.paint_badges()

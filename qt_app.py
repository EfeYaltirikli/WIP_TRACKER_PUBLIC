# -*- coding: utf-8 -*-
"""WIP TRACKER - Qt edition (new design).  Run this file.

The data and every calculation are the same wip_core the Tk program uses,
on the same database; only the screens are new.  Pages are built the first
time they are opened, so the window is up straight away.
"""

import datetime
import importlib
import os
import shutil
import sqlite3
import sys
import threading
import time
import traceback

from PySide6.QtCore import Qt, QTimer, QEvent, QObject, QEventLoop
from PySide6.QtGui import QIcon, QAction, QCursor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QHBoxLayout, QVBoxLayout,
                               QLabel, QStackedWidget, QMenu, QMessageBox, QStatusBar)

import wip_core
from wip_core import (Database, WFAnalyzer, APP_TITLE, APP_CREDIT, ensure_app_dir,
                      load_prefs, save_prefs, read_db_pointer, default_db_path)
import qt_theme
import qt_widgets
from qt_widgets import ctx, NavRail, label, LoadingOverlay, FilterBar
import qt_auth

# (rail key, short name, module, page title)
PAGES = [
    ("rw", "RW", "qt_page_analysis", "RW Analysis"),
    ("cause", "Cause", "qt_page_cause", "Cause Op Analysis"),
    ("wf", "Waterfall", "qt_page_waterfall", "Waterfall"),
    ("lt", "Lead Time", "qt_page_leadtime", "Lead Time Analysis"),
    ("mrb", "MRB", "qt_page_mrbscrap", "MRB / Scrap / TECO"),
    ("bn", "Bottleneck", "qt_page_bottleneck", "Part Bottleneck"),
    ("b1000", "B1000", "qt_page_b1000", "B1000 Live"),
    ("fc", "Forecast", "qt_page_forecast", "Forecast"),
    ("acc", "Custom", "qt_page_acc", "Custom Analysis"),
]
SETTINGS = ("settings", "Settings", "qt_page_settings", "Data & Settings")
# The version under the name in the header (from v75 on: 2.0.1).
#   x.y.Z  small fixes          x.Y.0  a middle-sized update
#   X.0.0  a full renewal (interface or logic)
VERSION = "2.4.0"


def _for_pyinstaller():
    """Never called.  The pages are imported when first opened
    (importlib), which PyInstaller cannot see - these lines let it find
    them so the built program carries every page."""
    import qt_page_analysis, qt_page_cause, qt_page_waterfall, qt_page_mrbscrap  # noqa
    import qt_page_bottleneck, qt_page_b1000, qt_page_forecast, qt_page_acc      # noqa
    import qt_page_leadtime                                                      # noqa
    import qt_page_settings, wip_layout_assets, wip_logo_data                    # noqa
    try:
        import xlrd                                                               # noqa: F401
    except ImportError:
        pass


class Placeholder(QWidget):
    """A page that is not ported to the new design yet."""

    def __init__(self, title):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.addWidget(label(title, obj="PageTitle"))
        lay.addWidget(label("This page is being moved to the new design.  Until it is "
                            "finished, use it in the current WIP TRACKER program.",
                            muted=True, wrap=True))
        lay.addStretch(1)

    def on_show(self):
        pass

    def reload(self):
        pass


class ActivityFilter(QObject):
    """Any key or mouse press anywhere counts as 'somebody is here'."""

    def __init__(self, win):
        super().__init__()
        self.win = win

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
                 QEvent.Wheel, QEvent.MouseMove, QEvent.HoverMove, QEvent.TouchBegin):
            self.win.note_activity()
        elif t == QEvent.Show and ctx.sheet and obj is not self.win \
                and isinstance(obj, QWidget) and obj.isWindow():
            # v73: a pop-up opened for the first time after a day / night
            # switch (a filter drawer, a page menu) takes the theme on NOW -
            # it used to keep the theme the program started in: light
            # drawer, dark fields in night mode
            self.win.theme_window(obj)
        return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)       # v71: the name only, no version
        self.last_activity = time.time()
        self.idle_box, self.idle_warned, self.closing = None, False, False
        self.shared = {"version": 0, "annotations": None, "settings": None}
        self.drawn = {}
        self.poll_n = 0
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._top_bar())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        root.addLayout(row, 1)
        all_pages = PAGES + [SETTINGS]
        self.rail = NavRail([(k, s, i) for i, (k, s, _m, _t) in enumerate(PAGES)],
                            [(SETTINGS[0], SETTINGS[1], len(PAGES))])
        self.rail.picked.connect(self.show_page)
        row.addWidget(self.rail)
        self.stack = QStackedWidget()
        row.addWidget(self.stack, 1)
        self.loading = LoadingOverlay(self.stack)
        self.warm = None                 # v72: the background preparation
        self.theme_due = set()           # v73: pages to recolour when next shown
        self.warm_check = QTimer(self)
        self.warm_check.timeout.connect(self._warm_done)
        ctx.loading = self.loading
        self.pages = [None] * len(all_pages)
        for _ in all_pages:
            self.stack.addWidget(QWidget())
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_lbl = QLabel("Ready.")
        self.sync_lbl = QLabel("")
        # 2.2.0: the local copy of the shared data (wip_core, THE LOCAL COPY)
        self.copy_lbl = QLabel("")
        self.copy_state = None           # what copy_tick last saw: busy / ok / off
        self.warm_wait = False           # the preparation waits for the copy
        self.reload_wait = False         # so does the reload after somebody's update
        self.copy_timer = QTimer(self)
        self.copy_timer.timeout.connect(self.copy_tick)
        sb.addWidget(self.status_lbl, 1)
        sb.addPermanentWidget(self.copy_lbl)
        sb.addPermanentWidget(self.sync_lbl)
        ctx.status = self.status_lbl.setText
        ctx.window = self
        ctx.hooks["show_page"] = self.show_page
        ctx.hooks["page_index"] = self.page_index
        # Until the MRB page is first opened (pages are built on first
        # visit) this builds it, which then takes the hook over itself.
        ctx.hooks["mrbscrap_load_serial"] = (
            lambda serial_no: self.build_page(self.page_index("mrb"))
            .load_serial_from_elsewhere(serial_no))
        ctx.hooks["reload_all_pages"] = self.reload_all_pages
        ctx.hooks["refresh_status"] = self.refresh_last_updated

    # ---- top bar ----
    def _top_bar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(62)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 6, 16, 6)
        lay.setSpacing(12)
        pm = qt_auth.logo_pixmap(44)
        if pm is not None:
            lg = QLabel()
            lg.setPixmap(pm)
            lay.addWidget(lg)
        sep = QFrame()
        sep.setFixedSize(1, 34)
        sep.setObjectName("TopSep")
        lay.addWidget(sep)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        txt.addWidget(label(APP_TITLE, obj="AppTitle"))
        txt.addWidget(label("v%s  ·  %s" % (VERSION, APP_CREDIT), obj="AppCredit"))
        lay.addLayout(txt)
        lay.addStretch(1)
        clock = qt_widgets.IconButton("clock", size=22)
        clock.setProperty("flat", True)
        clock.setEnabled(False)
        lay.addWidget(clock)
        self.last_lbl = label("", obj="LastUpdated")
        lay.addWidget(self.last_lbl)
        lay.addSpacing(4)
        self.theme_btn = qt_widgets.IconButton("sun", "Day / night", self.toggle_theme, size=40)
        lay.addWidget(self.theme_btn)
        self.user_btn = qt_widgets.IconButton("user", "", self.user_menu, text=" ")
        self.user_btn.setFixedHeight(40)
        lay.addWidget(self.user_btn)
        return bar

    def refresh_last_updated(self):
        try:
            son = ctx.db.last_upload_at()
        except Exception:
            son = None
        self.last_lbl.setText("Last updated  <b>%s</b>" % (son.strftime("%d.%m.%Y %H:%M")
                                                         if son else "-"))

    def paint_header(self):
        self.theme_btn.set_icon("moon" if ctx.TH["name"] == "day" else "sun")
        u = ctx.user
        if u:
            role = qt_auth.ROLE_TITLE.get(u["role"], u["role"] or "")
            role = role if u["role"] == "gmy" else role.title()
            self.user_btn.setText("       %s · %s" % (u["full_name"], role))
        else:
            self.user_btn.setText("       Not signed in")

    # ---- theme ----
    def apply_theme(self):
        """v73: day / night, fast.  The style sheet goes on the WINDOW, not
        on the whole application - Qt then restyles only this window's
        widgets (7x quicker); the application keeps the sheet it started
        with for windows that have no parent.  Only the page on screen is
        redrawn now; the others when they are next opened."""
        app = QApplication.instance()
        sheet = qt_theme.stylesheet(ctx.TH)
        ctx.sheet = sheet
        app.setPalette(qt_theme.palette(ctx.TH))
        if not app.styleSheet():
            app.setStyleSheet(sheet)
        self.setStyleSheet(sheet)
        self.unbake_all()
        self.sync_toplevels()
        self.rail.update()
        cur = self.stack.currentIndex()
        self.theme_due = set()
        for i, p in enumerate(self.pages):
            if p is None:
                continue
            if i == cur:
                self.repaint_page(p)
            else:
                self.theme_due.add(i)
        self.paint_header()

    def theme_window(self, w):
        sheet = ctx.sheet
        try:
            cur = w.styleSheet()
            if cur == sheet or isinstance(w, QMenu) or w.inherits("QTipLabel"):
                return
            if cur and cur not in self._theme_sheets():
                return               # a window with a sheet of its own
            w.setStyleSheet(sheet)
        except RuntimeError:
            return
        # A page part can be shown for a moment BEFORE it is put in its
        # place (it is a window of its own for that moment).  Once it is
        # in the page it must inherit the page's theme again, or it keeps
        # this one for good - the dark Route card in the day theme.
        QTimer.singleShot(0, lambda: self._unbake(w))

    def _unbake(self, w):
        try:
            if not w.isWindow() and w.styleSheet() in self._theme_sheets():
                w.setStyleSheet("")
        except RuntimeError:
            pass

    def unbake_all(self):
        """On a day / night switch: anything inside the window that carries
        a theme sheet of its own lets go of it (it inherits the window's)."""
        sheets = self._theme_sheets()
        for w in QApplication.instance().allWidgets():
            try:
                if not w.isWindow() and w.styleSheet() in sheets:
                    w.setStyleSheet("")
            except RuntimeError:
                pass

    def _theme_sheets(self):
        if getattr(self, "_sheets", None) is None:
            self._sheets = {qt_theme.stylesheet(qt_theme.theme(n)) for n in ("day", "night")}
        return self._sheets

    def sync_toplevels(self):
        """Windows with no parent (a module open in its own window, the
        pages' pop-up menus) do not inherit the window's sheet - they are
        given it here."""
        for w in QApplication.instance().topLevelWidgets():
            if w is not self:
                self.theme_window(w)

    def repaint_page(self, page):
        """The theme on one page: its tables, then the page's own colours."""
        for t in list(ctx.tables):
            try:
                if page.isAncestorOf(t):
                    t.refresh_theme()
            except RuntimeError:
                ctx.tables.remove(t)
        if hasattr(page, "refresh_theme"):
            try:
                page.refresh_theme()
            except Exception:
                traceback.print_exc()

    def toggle_theme(self):
        ctx.TH = qt_theme.theme("night" if ctx.TH["name"] == "day" else "day")
        ctx.prefs["theme"] = ctx.TH["name"]
        save_prefs(ctx.prefs)
        self.apply_theme()
        try:
            # v73: kept with the account too, so it follows the person
            ctx.db.set_user_pref(ctx.username(), "theme", ctx.TH["name"])
        except Exception:
            pass

    # ---- user ----
    def user_menu(self):
        if not ctx.user:
            return
        m = QMenu(self)
        m.addAction("Change my password...",
                    lambda: qt_auth.ChangePasswordDialog(self, ctx.db, ctx.username()).exec())
        if ctx.is_admin():
            m.addSeparator()
            m.addAction("Users and open sessions...",
                        lambda: self.show_page(len(PAGES), section="users"))
            a = QAction("Show every user's private notes", m)
            a.setCheckable(True)
            a.setChecked(ctx.show_all_notes)
            a.toggled.connect(self._notes_toggled)
            m.addAction(a)
        m.addSeparator()
        m.addAction("Sign out", self.sign_out)
        m.exec(self.user_btn.mapToGlobal(self.user_btn.rect().bottomLeft()))

    def _notes_toggled(self, on):
        ctx.show_all_notes = on
        for p in self.pages:
            if p is not None and hasattr(p, "notes_visibility_changed"):
                p.notes_visibility_changed()

    def sign_in(self):
        user = qt_auth.sign_in(self, ctx.db)
        if not user:
            return False
        ctx.user = user
        ctx.show_all_notes = False
        # v73: the account's own day / night choice, wherever it signs in
        try:
            mine = ctx.db.user_pref(user["username"]).get("theme")
        except Exception:
            mine = None
        if mine in ("day", "night") and mine != ctx.TH["name"]:
            ctx.TH = qt_theme.theme(mine)
            ctx.prefs["theme"] = mine
            save_prefs(ctx.prefs)
            self.apply_theme()
        try:
            if ctx.session_id:
                ctx.db.session_end(ctx.session_id)
            ctx.session_id = ctx.db.session_open(user["username"])
        except Exception:
            ctx.session_id = None
        self.paint_header()
        for p in self.pages:
            if p is not None and hasattr(p, "user_changed"):
                p.user_changed()
        FilterBar.reload_all()           # 2.1.1: this account's own filters
        return True

    def sign_out(self):
        FilterBar.flush_all()
        ctx.user = None
        self.paint_header()
        if not self.sign_in():
            self.close()

    # ---- pages ----
    def page_index(self, key):
        for i, (k, _s, _m, _t) in enumerate(PAGES + [SETTINGS]):
            if k == key:
                return i
        return -1

    def build_page(self, idx):
        if self.pages[idx] is not None:
            return self.pages[idx]
        key, _s, mod_name, title = (PAGES + [SETTINGS])[idx]
        t0 = time.time()
        try:
            mod = importlib.import_module(mod_name)
            page = mod.Page()
        except ModuleNotFoundError as e:
            if e.name != mod_name:
                raise
            page = Placeholder(title)
        old = self.stack.widget(idx)
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(idx, page)
        self.pages[idx] = page
        if ctx.sheet:
            self.sync_toplevels()            # its pop-ups get the theme on now
        ctx.hooks.setdefault("page_times", []).append((title, time.time() - t0))
        return page

    def show_page(self, idx, section=None):
        title = (PAGES + [SETTINGS])[idx][3]
        ver = ctx.db.data_version()
        first = self.pages[idx] is None
        need = first or self.drawn.get(idx) != ver
        if need:
            # v71: the loading card goes up BEFORE the page is built
            ctx.busy(True, "Opening %s..." % title)
        try:
            if need and (PAGES + [SETTINGS])[idx][0] in self.WARM_PAGES:
                # 2.1.1: MRB / B1000 need only the first part of the
                # preparation; only Forecast waits for every route
                self.wait_warm(full=(PAGES + [SETTINGS])[idx][0] == "fc")
            page = self.build_page(idx)
            self.stack.setCurrentIndex(idx)
            self.rail.set_current(idx)
            if idx in self.theme_due:            # the theme changed while it was hidden
                self.theme_due.discard(idx)
                self.repaint_page(page)
            if self.drawn.get(idx) != ver:
                self.drawn[idx] = ver
                try:
                    page.on_show()
                except Exception:
                    traceback.print_exc()
                    self.status_lbl.setText("Could not draw this page - see the console.")
        finally:
            if need:
                ctx.busy(False)
                if self.status_lbl.text().startswith("Opening "):
                    self.status_lbl.setText("Ready.")
        if section and hasattr(page, "open_section"):
            page.open_section(section)

    # ---- v72: the slow pages prepared in the background ----
    WARM_PAGES = ("mrb", "b1000", "fc")

    def start_warm(self):
        """Works out, on a second read-only connection in a background
        thread, what MRB / SCRAP / TECO and B1000 Live need when they open
        (the last place of every serial, the floor positions) - so those
        pages open from memory instead of reading the whole production
        table over the network while the window waits.  Started after
        sign-in and again whenever the shared data changes."""
        if self.warm is not None and self.warm.is_alive():
            return
        db = ctx.db
        # 2.2.0: while the data is being copied to this computer the
        # preparation waits for it - it then reads the copy, not the share
        st = db._mirror
        if st["enabled"] and st["busy"]:
            self.warm_wait = True
            return
        self.warm_wait = False
        try:
            eng = ctx.home_engineer() or wip_core.ALL
            scope = wip_core.allowed_part_keys(db, eng, wip_core.ALL, [], db.merge_on("b1000"))
        except Exception:
            return
        reader = db.reader_copy()
        # v75: Forecast too - every part's route worked out once, into the
        # SAME memory the page reads (the analyser's cache is shared)
        main_an = ctx.wf_an
        an = WFAnalyzer(reader)
        for k in ("add_rework_to_lead", "exclude_clean", "dedupe_ops",
                  "exclude_serial_keys", "only_serial_keys"):
            setattr(an, k, getattr(main_an, k))
        an._cache = main_an._cache
        try:
            fc_parts = [pk for pk, _d in db.wf_parts(db.merge_on("forecast"))]
        except Exception:
            fc_parts = []
        if scope:
            fc_parts.sort(key=lambda pk: pk not in scope)    # this person's parts first

        base_done = threading.Event()
        self.warm_base = base_done

        def job():
            try:
                reader.open_reader()
                reader.serial_last_points()
                reader.op_desc_map()
                wip_core.live_positions(reader, part_keys=scope, d1=None, d2=None,
                                        exclude_sold=True)
                base_done.set()
                for pk in fc_parts:
                    an.analyze_all_time(pk)
            except Exception:
                traceback.print_exc()
            finally:
                base_done.set()
                try:
                    reader.conn.close()
                except Exception:
                    pass

        self.warm = threading.Thread(target=job, name="warm", daemon=True)
        self.warm.start()
        self.warm_check.start(400)

    # ---- 2.2.0: the local copy ----
    def copy_tick(self):
        """Shows where the local copy stands and, the moment a new one is
        ready, does what was waiting for it."""
        try:
            st = ctx.db._mirror
        except Exception:
            return
        if not st["enabled"]:
            self.copy_lbl.setText("")
            return
        now = "busy" if st["busy"] else ("ok" if st["ok"] else "off")
        if now == "busy":
            self.copy_lbl.setText("Local copy  %d%%   |" % st["pct"])
        elif now == "ok":
            self.copy_lbl.setText("Local copy ✓   |")
        else:
            self.copy_lbl.setText("Local copy –   |")
        self.copy_lbl.setToolTip(ctx.db.mirror_text())
        if now == self.copy_state:
            return
        was, self.copy_state = self.copy_state, now
        if now == "busy":
            return
        # a copy was finished (ok) or given up on (off): go on either way
        if self.reload_wait:
            self.reload_wait = False
            self.apply_data_change()
        elif self.warm_wait:
            self.start_warm()
        if now == "ok" and was == "busy":
            self.status_lbl.setText("The data is on this computer now - pages read it locally.")

    def wait_copy(self, seconds=180):
        """At start-up: the copy is made first (the window shows how far it
        is) so the pages open from this computer, not from the share.  After
        `seconds` the program carries on and the copy finishes behind it."""
        st = ctx.db._mirror
        if not (st["enabled"] and st["busy"]):
            return
        t0 = time.time()
        ctx.busy(True, "Copying the data to this computer...")
        try:
            while st["busy"] and time.time() - t0 < seconds:
                self.loading.show_msg("Copying the data to this computer...  %d%%" % st["pct"])
                QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
                time.sleep(0.05)
        finally:
            ctx.busy(False)
        self.copy_tick()

    def apply_data_change(self):
        """The shared data changed (somebody's upload): everything worked
        out from the old data is dropped and the pages are drawn again."""
        wip_core.invalidate_caches()
        ctx.db.forget_meta()
        ctx.db.forget_reads()
        self.start_warm()
        self.reload_all_pages(redraw_current=True)
        self.refresh_last_updated()
        self.status_lbl.setText("Data reloaded - another user updated the shared database.")

    def _warm_done(self):
        if self.warm is not None and self.warm.is_alive():
            return
        self.warm_check.stop()
        try:
            ctx.db.flush_agg()           # one short write; skipped if busy
        except Exception:
            pass

    def wait_warm(self, full=True):
        """A slow page asked for while it is still being prepared waits for
        that (the window keeps drawing) instead of doing the work twice.
        full=False: only for the first part (last places, floor positions),
        not for the Forecast routes after it."""
        base = getattr(self, "warm_base", None)
        while self.warm is not None and self.warm.is_alive():
            if not full and base is not None and base.is_set():
                break
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            self.warm.join(0.03)

    def reload_all_pages(self, redraw_current=False):
        self.drawn.clear()
        for p in self.pages:
            if p is not None and hasattr(p, "reload"):
                try:
                    p.reload()
                except Exception:
                    traceback.print_exc()
        if redraw_current:
            self.show_page(self.stack.currentIndex())

    # ---- background: shared database, sessions, idle ----
    def poll_shared(self):
        # v69: once a close is under way nothing more is asked - a second
        # reason (maintenance after a close request) must not open a second
        # countdown on top of the first
        if self.closing:
            return
        try:
            # somebody else is in the middle of a write (an upload): skip
            # this check rather than wait on it - the next one is 8 s away
            if ctx.db.busy_now():
                self.sync_lbl.setText("Another user is saving to the shared database - "
                                      "checking again in a moment")
                return
        except Exception:
            pass
        try:
            self.poll_n += 1
            asked = None
            if ctx.session_id:
                if self.poll_n % 5 == 1:
                    asked = ctx.db.session_beat(ctx.session_id, ctx.username())
                    if self.check_account():
                        return
                else:
                    asked = ctx.db.session_close_requested(ctx.session_id)
            if asked:
                who = asked.split("|", 1)[1] if "|" in asked else ""
                # forgetting our row is tidying up; a busy file must not stop
                # the copy from doing what the admin asked
                try:
                    ctx.db.session_end(ctx.session_id)
                except Exception:
                    pass
                ctx.session_id = None
                self.close_countdown("An administrator%s has asked this copy of WIP TRACKER "
                                     "to close, usually so a new version can be installed."
                                     % ((" (%s)" % who) if who else ""))
                return
            try:
                bakim, kim, _t = ctx.db.maintenance()
            except Exception:
                bakim, kim = False, ""
            if bakim and not ctx.is_admin() and ctx.user:
                if ctx.session_id:
                    try:
                        ctx.db.session_end(ctx.session_id)
                    except Exception:
                        pass
                    ctx.session_id = None
                self.close_countdown("An administrator%s has put the shared database into "
                                     "MAINTENANCE MODE.\n\nOpen the program again once they "
                                     "say it is finished." % ((" (%s)" % kim) if kim else ""))
                return
            sv = ctx.db.settings_version()
            if sv != self.shared["settings"]:
                first = self.shared["settings"] is None
                self.shared["settings"] = sv
                # our own setting change was put into effect where it was made
                if not first and sv != getattr(ctx.db, "own_settings_version", None):
                    ctx.db.forget_meta()
                    try:
                        wip_core.apply_rules(ctx.db)
                    except Exception:
                        pass
                    self.reload_all_pages(redraw_current=True)
                    self.status_lbl.setText("Settings reloaded - another user changed a shop rule.")
            av = ctx.db.annotation_version()
            if av != self.shared["annotations"]:
                self.shared["annotations"] = av
                for p in self.pages:
                    if p is not None and hasattr(p, "annotations_changed"):
                        p.annotations_changed()
            try:
                ctx.db.mirror_check()            # 2.3.0: is the local copy still the data?
                if self.warm is None or not self.warm.is_alive():
                    ctx.db.flush_agg()           # summaries worked out since: one short write
            except Exception:
                pass
            v = ctx.db.data_version()
            if v != self.shared["version"]:
                first = self.shared["version"] == 0
                self.shared["version"] = v
                if not first:
                    st = ctx.db.mirror_check()
                    if st["enabled"] and st["busy"]:
                        # 2.2.0: the new data is copied to this computer
                        # first and the pages are drawn from the copy -
                        # not twice, once slowly from the share
                        self.reload_wait = True
                        self.copy_state = None
                        self.status_lbl.setText("Another user updated the shared database - "
                                                "copying the new data to this computer...")
                    else:
                        self.apply_data_change()
            if getattr(ctx.db, "read_only", False):
                self.sync_lbl.setText("READ ONLY - nothing you change here can be saved")
            else:
                self.sync_lbl.setText("Connected  |  %s"
                                      % datetime.datetime.now().strftime("%H:%M:%S"))
        except Exception as e:
            self.sync_lbl.setText("Shared database not reachable: %s" % e)

    def check_account(self):
        """v69: the signed-in account as the FILE has it now.  An admin on
        another computer may have changed this person's role or switched
        the account off; the copy that is open must follow at once - an
        ex co-admin must not keep the admin pages (or the way past
        maintenance mode) until they happen to restart.  True = closing."""
        name = ctx.username()
        if not name or ctx.user is None:
            return False
        try:
            fresh = ctx.db.user(name)
        except Exception:
            return False            # busy file: ask again next time
        if fresh is None or not fresh.get("active"):
            if ctx.session_id:
                try:
                    ctx.db.session_end(ctx.session_id)
                except Exception:
                    pass
                ctx.session_id = None
            self.close_countdown("Your account has been switched off by an administrator.")
            return True
        if fresh.get("role") != ctx.user.get("role"):
            ctx.user.update(fresh)
            self.paint_header()
            for p in self.pages:
                if p is not None and hasattr(p, "user_changed"):
                    try:
                        p.user_changed()
                    except Exception:
                        traceback.print_exc()
            self.status_lbl.setText("Your role was changed by an administrator: %s."
                                    % qt_auth.ROLE_TITLE.get(fresh["role"], fresh["role"]))
        return False

    def note_activity(self):
        self.last_activity = time.time()
        if self.idle_box is not None:
            box, self.idle_box = self.idle_box, None
            self.idle_warned = False
            try:
                box.close()
            except Exception:
                pass

    def b1000_unsaved(self):
        for p in self.pages:
            if p is not None and hasattr(p, "unsaved"):
                try:
                    if p.unsaved():
                        return True
                except Exception:
                    pass
        return False

    def check_idle(self):
        """Warns a minute out, then closes (v67) - but never while the B1000
        plan has unsaved changes."""
        if self.closing:
            return
        # just moving the mouse over the program (no click) gives most
        # widgets no event at all, so the pointer itself is checked too
        try:
            pos = QCursor.pos()
            if (QApplication.activeWindow() is not None
                    and getattr(self, "_last_cursor", None) not in (None, pos)):
                self.note_activity()
            self._last_cursor = pos
        except Exception:
            pass
        lim = float(wip_core.IDLE_MINUTES or 0) * 60.0
        if lim <= 0:
            return
        gone = time.time() - self.last_activity
        if gone >= lim:
            if self.b1000_unsaved():
                if not self.idle_warned:
                    self.idle_warned = True
                    self.show_idle_warning(unsaved=True)
                return
            self.closing = True
            self.close()
            return
        if gone >= max(0.0, lim - 60.0) and not self.idle_warned:
            self.idle_warned = True
            self.show_idle_warning()

    def show_idle_warning(self, unsaved=False):
        m = wip_core.IDLE_MINUTES
        if unsaved:
            text = ("Nothing has been touched for %g minutes, but the B1000 plan has "
                    "unsaved changes - so this copy is NOT closing.\n\nSave the plan, "
                    "or close the program yourself when you are done." % m)
        else:
            text = ("Nothing has been touched for %g minutes, so this copy will close "
                    "itself in about a minute.\n\nA copy left open holds the shared "
                    "database and stops updates going out.\n\n"
                    "Move the mouse or press a key to stay." % m)
        box = QMessageBox(self)
        box.setWindowTitle("Still there?")
        box.setText("WIP TRACKER is about to close")
        box.setInformativeText(text)
        box.addButton("I'm here", QMessageBox.AcceptRole)
        box.setWindowModality(Qt.NonModal)
        box.finished.connect(lambda _r: self.note_activity())
        self.idle_box = box
        box.show()

    def close_countdown(self, reason, seconds=10):
        self.closing = True
        box = QMessageBox(self)
        box.setWindowTitle("WIP TRACKER - closing")
        box.setText(reason + "\n\nClosing in %d s." % seconds)
        box.addButton("Close now", QMessageBox.DestructiveRole)
        left = {"n": seconds}

        def tick():
            left["n"] -= 1
            if left["n"] <= 0:
                box.done(0)
                self.close()
                return
            box.setText(reason + "\n\nClosing in %d s." % left["n"])
        t = QTimer(box)
        t.timeout.connect(tick)
        t.start(1000)
        box.finished.connect(lambda _r: self.close())
        box.show()

    def closeEvent(self, ev):
        FilterBar.flush_all()
        try:
            if ctx.session_id:
                ctx.db.session_end(ctx.session_id)
        except Exception:
            pass
        ev.accept()


def launch():
    t_start = time.time()
    ensure_app_dir()
    prefs = load_prefs()
    db_path = read_db_pointer() or prefs.get("db_path") or default_db_path()
    db = Database(db_path)
    # 2.2.0: on a share the data is copied to this computer, in the
    # background, from this moment (the sign-in window is up meanwhile)
    try:
        db.mirror_start()
    except Exception:
        traceback.print_exc()
    wip_core.apply_rules(db)
    set_windows_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    ctx.prefs = prefs
    ctx.db = db
    ctx.wf_an = WFAnalyzer(db)
    ctx.TH = qt_theme.theme(prefs.get("theme", "night"))
    ctx.hooks["startup_t0"] = t_start
    win = MainWindow()
    try:
        icon = app_icon()
        app.setWindowIcon(icon)
        win.setWindowIcon(icon)
    except Exception:
        pass
    win.apply_theme()
    win.refresh_last_updated()
    win.resize(1440, 900)
    win.show()
    set_windows_taskbar_icon(win)
    # v70: kept on the window - an unreferenced filter was thrown away at
    # once, so no movement was ever seen and the idle warning came after
    # five minutes however busy the person was
    win.activity_filter = ActivityFilter(win)
    app.installEventFilter(win.activity_filter)
    win.copy_timer.start(700)
    if not win.sign_in():
        return 0
    win.wait_copy()
    win.show_page(win.page_index(prefs.get("qt_last_page", "rw")) if False else 0)
    t1 = QTimer(win); t1.timeout.connect(win.poll_shared); t1.start(8000)
    t2 = QTimer(win); t2.timeout.connect(win.check_idle); t2.start(5000)
    QTimer.singleShot(1500, win.poll_shared)
    QTimer.singleShot(200, win.start_warm)
    ctx.hooks["startup_seconds"] = time.time() - t_start
    return app.exec()


def app_icon():
    """v72: the program's icon built from the PNGs embedded in
    wip_logo_data - no file to find and no image plug-in to load, so it
    shows the same when the program runs from a shared folder."""
    import base64
    from PySide6.QtGui import QPixmap
    from wip_logo_data import ICON_PNG_B64
    icon = QIcon()
    for _size, b64 in sorted(ICON_PNG_B64.items()):
        pm = QPixmap()
        if pm.loadFromData(base64.b64decode("".join(b64) if isinstance(b64, tuple) else b64),
                           "PNG"):
            icon.addPixmap(pm)
    if icon.isNull():
        icon = QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wip_icon.ico"))
    return icon


def set_windows_app_id():
    """v72: tells Windows which program this is, so the taskbar takes the
    WINDOW's icon instead of working one out from the .exe's path - which
    on a network path often comes back empty."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TEI.WIPTRACKER")
    except Exception:
        pass


def set_windows_taskbar_icon(win):
    """2.1.1: the taskbar icon when the program runs from the shared
    folder.  Windows may not read the icon out of an .exe on a network
    path, and then the taskbar button stays blank.  So the icon is written
    to this computer once (~/.rw_analysis/wip_icon.ico), loaded from there
    and handed to the window directly (WM_SETICON big + small, and the
    window class's icon)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        local = os.path.join(wip_core.APP_DIR, "wip_icon.ico")
        src = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                           "wip_icon.ico")
        if not os.path.isfile(src):
            src = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "wip_icon.ico")
        if os.path.isfile(src) and (not os.path.isfile(local)
                                    or os.path.getsize(local) != os.path.getsize(src)):
            ensure_app_dir()
            shutil.copyfile(src, local)
        if not os.path.isfile(local):
            return
        u32 = ctypes.windll.user32
        u32.LoadImageW.restype = wintypes.HANDLE
        u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]
        set_cls = getattr(u32, "SetClassLongPtrW", None) or u32.SetClassLongW
        set_cls.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
        hwnd = int(win.winId())
        big = u32.LoadImageW(None, local, IMAGE_ICON, u32.GetSystemMetrics(11),
                             u32.GetSystemMetrics(12), LR_LOADFROMFILE)
        small = u32.LoadImageW(None, local, IMAGE_ICON, u32.GetSystemMetrics(49),
                               u32.GetSystemMetrics(50), LR_LOADFROMFILE)
        if big:
            u32.SendMessageW(hwnd, WM_SETICON, 1, big)
            set_cls(hwnd, -14, big)          # GCLP_HICON
        if small:
            u32.SendMessageW(hwnd, WM_SETICON, 0, small)
            set_cls(hwnd, -34, small)        # GCLP_HICONSM
        win._taskbar_icons = (big, small)    # kept for the life of the window
    except Exception:
        traceback.print_exc()


def main():
    try:
        return launch()
    except sqlite3.Error as e:
        traceback.print_exc()
        if wip_core.is_lock_error(e):
            title = "The data is busy right now"
            body = ("Another user is saving to the shared database at the moment - most "
                    "likely an upload, or a first start after an update.\n\nNothing is "
                    "wrong and nothing is lost.  Wait a moment and open the program again."
                    "\n\nIf it keeps happening, check whether somebody has left an upload "
                    "running.")
        elif "malformed" in str(e).lower() or "not a database" in str(e).lower():
            title = "The database file is damaged"
            body = ("The shared database cannot be read.\n\nDO NOT DELETE IT and do not "
                    "overwrite it.  Take a copy of the file, then run KURTARMA.py on the "
                    "copy - the step-by-step is in OKU.txt under 'VERITABANI BOZULDUYSA'."
                    "\n\nIt recovers everything that is still readable and writes a clean "
                    "database next to it.")
        else:
            title, body = "The database could not be opened", str(e)
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, title, body + "\n\n" + "-" * 30 + "\n\n" + str(e))
        except Exception:
            pass
        return 1
    except Exception as e:
        traceback.print_exc()
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "WIP TRACKER", "The program could not start:\n\n%s" % e)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

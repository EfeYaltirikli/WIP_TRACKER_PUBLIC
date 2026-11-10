# -*- coding: utf-8 -*-
"""WIP TRACKER (Qt) - sign-in and password change.

Same rules as the Tk program (wip_auth): pick your name, type your
password; on a first sign-in you choose one (twice).  MAINTENANCE MODE
lets only the admin / co-admin in.
"""

import getpass
import sys

from PySide6.QtCore import Qt, QSettings, QTimer, QEvent, QObject
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QComboBox, QLineEdit,
                               QLabel, QMessageBox)

import wip_core
from wip_core import ROLE_TITLE  # noqa: F401  (qt_app reads qt_auth.ROLE_TITLE)
from qt_widgets import ctx, label, hbox, button

MIN_PW = 6


def logo_pixmap(height=40):
    try:
        import base64
        from wip_logo_data import LOGO_PNG_B64
        pm = QPixmap()
        pm.loadFromData(base64.b64decode(LOGO_PNG_B64))
        if height and pm.height() != height:
            pm = pm.scaledToHeight(height, Qt.SmoothTransformation)
        return pm
    except Exception:
        return None


def caps_lock_on():
    """2.1.1: Caps Lock state from the keyboard itself - Windows only (Qt
    has no call for it).  None = cannot tell here."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except Exception:
        return None


class CapsWatch(QObject):
    """Shows `warn` under a password field while Caps Lock is on.  On
    Windows it reads the key itself (also before anything is typed);
    elsewhere it guesses from the letters typed: a capital without Shift
    means on, a small letter without Shift means off."""

    def __init__(self, dialog, fields, warn):
        super().__init__(dialog)
        self.warn = warn
        self.guess = False
        warn.setVisible(False)
        for f in fields:
            f.installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(250)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress:
            t = ev.text()
            if len(t) == 1 and t.isalpha() and t.upper() != t.lower():
                shift = bool(ev.modifiers() & Qt.ShiftModifier)
                self.guess = t.isupper() != shift
            elif ev.key() == Qt.Key_CapsLock:
                self.guess = not self.guess
            QTimer.singleShot(0, self.update)
        return False

    def update(self):
        on = caps_lock_on()
        if on is None:
            on = self.guess
        if on == self.warn.isHidden():
            self.warn.setVisible(on)


class LoginDialog(QDialog):
    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.result_user = None
        self.setWindowTitle("WIP TRACKER - sign in")
        self.setModal(True)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(6)
        pm = logo_pixmap(48)
        if pm is not None:
            lg = QLabel()
            lg.setPixmap(pm)
            lay.addWidget(lg)
        lay.addWidget(label("WIP TRACKER", obj="PageTitle"))
        lay.addWidget(label("Sign in to continue", muted=True))
        lay.addSpacing(10)
        users = db.users()
        self.by_label = {"%s   (%s)" % (u["full_name"], u["username"]): u for u in users}
        lay.addWidget(label("User", section=True))
        self.user_cb = QComboBox()
        self.user_cb.addItems(sorted(self.by_label))
        # this computer's last sign-in comes first (kept on the computer,
        # not in the shared file); none yet -> the Windows login name
        last = remembered_user()
        for i in range(self.user_cb.count()):
            if self.by_label[self.user_cb.itemText(i)]["username"].upper() == last:
                self.user_cb.setCurrentIndex(i)
                break
        self.user_cb.currentIndexChanged.connect(lambda _i: self._sync())
        lay.addWidget(self.user_cb)
        self.prompt = label("Password", section=True)
        lay.addSpacing(6)
        lay.addWidget(self.prompt)
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.Password)
        lay.addWidget(self.pw1)
        # 2.1.1: Caps Lock warning right under the password
        self.caps = label("Caps Lock is on", wrap=True)
        self.caps.setStyleSheet("color: %s; font-weight: 600;" % ctx.TH["danger"])
        lay.addWidget(self.caps)
        self.rep_lbl = label("Repeat password", section=True)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        lay.addWidget(self.rep_lbl)
        lay.addWidget(self.pw2)
        self.hint = label("", muted=True, wrap=True)
        lay.addWidget(self.hint)
        lay.addSpacing(8)
        self.ok_btn = button("Sign in", primary=True, slot=self._submit)
        lay.addWidget(hbox(self.ok_btn, button("Quit", slot=self.reject), None))
        self.pw1.returnPressed.connect(self._submit)
        self.pw2.returnPressed.connect(self._submit)
        self.caps_watch = CapsWatch(self, (self.pw1, self.pw2), self.caps)
        self._sync()
        self.pw1.setFocus()

    def selected(self):
        return self.by_label.get(self.user_cb.currentText())

    def _sync(self):
        u = self.selected()
        first = bool(u) and not u["pw_hash"]
        self.prompt.setText("Choose a password" if first else "Password")
        self.rep_lbl.setVisible(first)
        self.pw2.setVisible(first)
        self.hint.setStyleSheet("")
        self.hint.setText("First sign-in: pick a password of at least %d characters.  "
                          "It is yours from then on." % MIN_PW if first else "")
        self.pw1.clear()
        self.pw2.clear()

    def _fail(self, msg):
        self.hint.setText(msg)
        self.hint.setStyleSheet("color: %s;" % ctx.TH["danger"])

    def _submit(self):
        u = self.selected()
        if not u:
            self._fail("Pick a user first.")
            return
        pw = self.pw1.text()
        if not u["pw_hash"]:
            if len(pw) < MIN_PW:
                self._fail("The password needs at least %d characters." % MIN_PW)
                return
            if pw != self.pw2.text():
                self._fail("The two passwords are not the same.")
                return
            if getattr(self.db, "read_only", False):
                self._fail("This copy of the database is read-only, so a new password "
                           "cannot be saved. Ask an admin for write access.")
                return
            try:
                self.db.set_password(u["username"], pw)
            except Exception as e:
                self._fail("The password could not be saved - another user is saving to the "
                           "shared database right now.  Try again in a moment.  (%s)" % e)
                return
        elif not self.db.check_password(u["username"], pw):
            self._fail("Wrong password.")
            return
        try:
            bakim, kim, _nz = self.db.maintenance()
        except Exception:
            bakim, kim = False, ""
        rol = (self.db.user(u["username"]) or {}).get("role")
        if bakim and rol not in wip_core.ADMIN_ROLES:
            self._fail("MAINTENANCE MODE: an administrator%s is working on the shared "
                       "database and the program cannot be opened right now."
                       % ((" (%s)" % kim) if kim else ""))
            return
        self.db.note_login(u["username"])
        remember_user(u["username"])
        self.result_user = self.db.user(u["username"])
        self.accept()


def _settings():
    return QSettings("WIP_TRACKER", "WIP_TRACKER")


def remembered_user():
    try:
        v = _settings().value("last_user", "")
        if v:
            return str(v).upper()
    except Exception:
        pass
    try:
        return getpass.getuser().upper()
    except Exception:
        return ""


def remember_user(username):
    try:
        _settings().setValue("last_user", username)
    except Exception:
        pass


def sign_in(parent, db):
    """Returns the signed-in user row, or None if the dialog was closed."""
    try:
        db.seed_users()
    except Exception:
        pass
    dlg = LoginDialog(parent, db)
    if dlg.exec() == QDialog.Accepted:
        return dlg.result_user
    return None


class ChangePasswordDialog(QDialog):
    def __init__(self, parent, db, username):
        super().__init__(parent)
        self.db, self.username = db, username
        self.setWindowTitle("Change password")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        self.e = {}
        for key, cap in (("old", "Current password"), ("new", "New password"),
                         ("rep", "Repeat new password")):
            lay.addWidget(label(cap, section=True))
            ed = QLineEdit()
            ed.setEchoMode(QLineEdit.Password)
            lay.addWidget(ed)
            self.e[key] = ed
        self.hint = label("", wrap=True)
        self.hint.setStyleSheet("color: %s;" % ctx.TH["danger"])
        lay.addWidget(self.hint)
        lay.addWidget(hbox(button("Save", primary=True, slot=self._save),
                           button("Cancel", slot=self.reject), None))

    def _save(self):
        if not self.db.check_password(self.username, self.e["old"].text()):
            self.hint.setText("The current password is wrong.")
            return
        new = self.e["new"].text()
        if len(new) < MIN_PW:
            self.hint.setText("The new password needs at least %d characters." % MIN_PW)
            return
        if new != self.e["rep"].text():
            self.hint.setText("The two new passwords are not the same.")
            return
        self.db.set_password(self.username, new)
        self.accept()
        QMessageBox.information(self.parent(), "Change password", "Your password has been changed.")

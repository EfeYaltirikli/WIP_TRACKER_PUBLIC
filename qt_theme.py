# -*- coding: utf-8 -*-
"""WIP TRACKER (Qt) - colours and the application style sheet.

The palette is the Grafit design the user picked: soft greys stepping up
from the page to the cards, one blue accent, quiet borders.  Colour on
screen means TECO / MRB / SCRAP / REPAIR; everything else stays neutral.
The same keys as wip_core.THEMES are used, so every number that reads a
theme colour (heat maps, waterfall states) keeps working.
"""

from wip_core import THEMES

# Literal flag colours: the same in both themes, the same on every page.
FLAG_COLORS = {"teco": "#C9A400", "mrb": "#7A3FA0", "mrbent": "#9D4EDD",
               "scrap": "#B23A3A", "repair": "#1F7A3F", "note": "#1F4E9C",
               "fixed": "#2F7F8F", "removed": "#6E1B2A"}   # removed: scrap's red, darker
# Lighter versions for TEXT on a dark page (numbers in the TOTALS boxes).
FLAG_TEXT_NIGHT = {"teco": "#E3C14A", "mrb": "#B98AE0", "mrbent": "#D6B8F5",
                   "scrap": "#EE7B7B", "repair": "#5CC98A", "note": "#7FA8F0",
                   "fixed": "#55C1D0", "removed": "#D9707F"}
FLAG_TEXT_DAY = {"teco": "#8A6D00", "mrb": "#6E3596", "mrbent": "#8E44C8",
                 "scrap": "#B3302F", "repair": "#1D7A44", "note": "#2457A8",
                 "fixed": "#1F7684", "removed": "#7A1C2B"}

FONT = "Segoe UI"          # Windows; Qt falls back on its own elsewhere
MONO = "Consolas"


def theme(name):
    th = dict(THEMES.get(name, THEMES["night"]))
    th["flag_text"] = FLAG_TEXT_NIGHT if th["name"] == "night" else FLAG_TEXT_DAY
    # A second surface step for cards inside cards and for inputs.
    th.setdefault("surf2", "#2A2A2A" if th["name"] == "night" else "#F7F8FA")
    if th["name"] == "day":
        # v69 design: a light rail in day mode, the page's own panel colour
        th.update(rail_bg="#FFFFFF", rail_fg="#5E6470", rail_active_bg="#E3ECFC",
                  rail_active_fg="#1F63D6")
    return th


def _arrow_png(direction, color):
    """A small triangle for the spin box buttons, drawn once per colour into
    the temp folder (a style sheet can only take an image from a file)."""
    import os
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "wip_tracker_arrow_%s_%s.png"
                        % (direction, color.lstrip("#")))
    if not os.path.exists(path):
        try:
            from PySide6.QtCore import QPointF, Qt
            from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF
            img = QImage(16, 10, QImage.Format_ARGB32)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            pts = ((1, 9), (15, 9), (8, 1)) if direction == "up" else ((1, 1), (15, 1), (8, 9))
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
            p.end()
            img.save(path)
        except Exception:
            return ""
    return path.replace("\\", "/")


def stylesheet(th):
    """The whole look in one place.  Widgets get an objectName or a
    property ("card", "chip", "primary"...) and the sheet does the rest."""
    t = dict(th)
    for d in ("up", "down"):
        t["arrow_" + d] = _arrow_png(d, th["muted"])
        t["arrow_%s_on" % d] = _arrow_png(d, th["accent"])
    return """
* { font-family: "%(font)s"; font-size: 10pt; }
QMainWindow, QWidget#Page, QWidget#Body, QStackedWidget { background: %(bg)s; }
QWidget { color: %(text)s; }
QToolTip { background: %(card)s; color: %(text)s; border: 1px solid %(border)s; padding: 4px; }

/* ---------- top bar ---------- */
QFrame#TopBar { background: %(header)s; border-bottom: 1px solid %(border)s; }
QLabel#GrandTotal { background: %(subtotal)s; border-top: 2px solid %(accent)s; border-radius: 0 0 8px 8px; padding: 8px 12px; font-size: 10.5pt; }
QLabel#AppTitle { color: %(text)s; font-size: 11pt; font-weight: 700; letter-spacing: .5px; }
QLabel#AppCredit { color: %(muted)s; font-size: 8.5pt; }
QFrame#TopSep { background: %(border)s; }
QLabel#LastUpdated { color: %(header_sub)s; }
QLabel#LastUpdated b { color: %(header_txt)s; }

/* ---------- page head ---------- */
QLabel#PageTitle { font-size: 17pt; font-weight: 700; color: %(text)s; }
QLabel#PageSub { color: %(muted)s; }
QLabel[muted="true"] { color: %(muted)s; }
QLabel[section="true"] { color: %(muted)s; font-size: 10pt; font-weight: 700; letter-spacing: 1px; }

/* ---------- cards ---------- */
QFrame[card="true"] { background: %(card)s; border: 1px solid %(border)s; border-radius: 10px; }
QFrame[card="true"] QLabel { background: transparent; }
QLabel#CardTitle { font-size: 11.5pt; font-weight: 700; }
QFrame[popup="true"] { background: %(card)s; border: 1px solid %(accent)s; border-radius: 10px; }
QFrame[stub="true"] { border: 1px dashed %(border)s; border-radius: 10px; background: transparent; }

/* ---------- buttons ---------- */
QPushButton { background: transparent; color: %(text)s; border: 1px solid %(border)s;
              border-radius: 8px; padding: 4px 12px; min-height: 18px; }
QPushButton:hover { background: %(head_cell)s; }
QPushButton:pressed { background: %(subtotal)s; }
QPushButton:disabled { color: %(muted)s; border-color: %(border)s; }
QPushButton[primary="true"] { background: %(accent)s; color: #FFFFFF; border: none; font-weight: 600; }
QPushButton[primary="true"]:hover { background: %(accent_hover)s; }
QPushButton[danger="true"] { color: %(danger)s; }
QPushButton[flat="true"] { border: none; background: transparent; }
QPushButton[flat="true"]:hover { color: %(accent)s; }
QPushButton[icon="true"] { padding: 4px; min-width: 22px; }
QPushButton[chip="true"] { border-radius: 8px; padding: 3px 12px; background: %(surf2)s; }
QPushButton[chip="true"][on="true"] { border-color: %(accent)s; background: %(sel_row)s; color: %(text)s; }
QPushButton[toggle="true"]:checked { border-color: %(accent)s; background: %(sel_row)s; font-weight: 600; }
QPushButton[seg="true"] { border: none; border-radius: 6px; padding: 3px 10px; color: %(muted)s; }
QPushButton[seg="true"]:checked { background: %(card)s; color: %(text)s; font-weight: 600; }
QFrame[segbox="true"] { background: %(field)s; border: 1px solid %(border)s; border-radius: 8px; }

/* ---------- inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {
    background: %(field)s; color: %(text)s; border: 1px solid %(border)s;
    border-radius: 7px; padding: 5px 8px; selection-background-color: %(accent)s; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border-color: %(accent)s; }
QLineEdit:disabled, QComboBox:disabled { color: %(muted)s; }
QComboBox::drop-down { border: none; width: 18px; }
/* the arrows drawn where they are clicked (unset, they were painted off
   their real hit area and a click at the arrow sometimes did nothing) */
QSpinBox, QDoubleSpinBox, QDateEdit { padding-right: 24px; }
QSpinBox::up-button, QDoubleSpinBox::up-button, QDateEdit::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 22px;
    border: none; border-left: 1px solid %(border)s; border-top-right-radius: 7px;
    background: transparent; }
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateEdit::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 22px;
    border: none; border-left: 1px solid %(border)s; border-bottom-right-radius: 7px;
    background: transparent; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover,
QDateEdit::up-button:hover, QDateEdit::down-button:hover { background: %(sel_row)s; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateEdit::up-arrow {
    image: url("%(arrow_up)s"); width: 8px; height: 5px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateEdit::down-arrow {
    image: url("%(arrow_down)s"); width: 8px; height: 5px; }
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover, QDateEdit::up-arrow:hover {
    image: url("%(arrow_up_on)s"); }
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover, QDateEdit::down-arrow:hover {
    image: url("%(arrow_down_on)s"); }
QCheckBox, QRadioButton { spacing: 6px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; }

/* ---------- tables ---------- */
QTableView { background: %(card)s; alternate-background-color: %(row_b)s; color: %(text)s;
             gridline-color: %(grid)s; border: none; selection-background-color: %(sel_row)s;
             selection-color: %(text)s; }
QHeaderView::section { background: %(card)s; color: %(text)s; border: none;
                       border-bottom: 1px solid %(border)s; padding: 8px 8px; font-weight: 600; }
QTableCornerButton::section { background: %(card)s; border: none; }
QHeaderView { background: %(card)s; }
QTableView::item { padding-left: 8px; padding-right: 6px; }

/* ---------- scrolling ---------- */
QScrollArea { border: none; background: transparent; }
QAbstractScrollArea::corner { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
QScrollBar::handle { background: %(border)s; border-radius: 5px; min-height: 30px; min-width: 30px; }
QScrollBar::handle:hover { background: %(grid_strong)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ---------- splitters / status ---------- */
QSplitter::handle { background: %(bg)s; }
QStatusBar { background: %(header)s; color: %(muted)s; border-top: 1px solid %(border)s; }
QStatusBar QLabel { color: %(muted)s; }
QMenu { background: %(card)s; color: %(text)s; border: 1px solid %(border)s; }
QMenu::item:selected { background: %(sel_row)s; }
QDialog { background: %(bg)s; }
QPushButton#SecHead { text-align: left; border: none; border-top: 1px solid %(border)s;
                      border-radius: 0; padding: 14px 22px; font-size: 11pt; background: transparent; }
QPushButton#SecHead:checked { background: %(sel_row)s; font-weight: 700; }
QWidget#SecBody { background: transparent; }
QFrame#Sep { background: %(border)s; }
QTabWidget::pane { border: none; }
""" % dict(t, font=FONT)


def palette(th):
    """v73: the theme's colours as the application palette too.  The style
    sheet covers what it names; the parts it does not reach (inside a
    calendar, a list's empty area, a drop-down's popup) otherwise take the
    WINDOWS colours - with Windows in dark mode they stayed black in the
    day theme."""
    from PySide6.QtGui import QPalette, QColor
    pal = QPalette()
    roles = ((QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "field"),
             (QPalette.AlternateBase, "row_b"), (QPalette.Text, "text"),
             (QPalette.Button, "card"), (QPalette.ButtonText, "text"),
             (QPalette.ToolTipBase, "card"), (QPalette.ToolTipText, "text"),
             (QPalette.Highlight, "accent"), (QPalette.PlaceholderText, "muted"),
             (QPalette.Light, "card"), (QPalette.Midlight, "border"), (QPalette.Mid, "border"),
             (QPalette.Dark, "border"), (QPalette.Shadow, "border"))
    for role, key in roles:
        col = QColor(th.get(key, "#808080"))
        for grp in (QPalette.Active, QPalette.Inactive):
            pal.setColor(grp, role, col)
        pal.setColor(QPalette.Disabled, role, col)
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(th.get("muted", "#808080")))
    return pal

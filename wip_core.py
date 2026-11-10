# -*- coding: utf-8 -*-
"""
WIP TRACKER

Rework analysis (SAP export) and waterfall lead-time / WIP tracking
(SAP part data + traveler route), sharing one SQLite database.

SAP EXPORT - FIXED COLUMN POSITIONS
  Material          E    part number
  Order             F    serial number
  OpAc              G    operation number
  Work ctr          J    work centre
  Rework Iscilik    Q    rework labour hours
  Cause             AA   cause operation
  Kyt tarihi        C    record date
  Time              Z    record time
  Operation         I    operation description   (optional)
  Short desc        K    work centre name        (optional)

  The position is what counts; the header text is only used to verify it.
  If the expected header is not in its column, the same header is looked up
  elsewhere and the reader moves there, so an extra column inserted by SAP
  never causes the wrong data to be read silently.

STORAGE
  <program folder>/rw_data.db, or ~/.rw_analysis/rw_data.db if that folder
  isn't writable (SQLite - survives closing the program).

INSTALL
  pip install openpyxl
  python wip_tracker.py

Linux may also need:  sudo apt install python3-tk
"""

import os
import re
import sys
import csv
import json
import email
import hashlib
import sqlite3
import threading
import calendar
import bisect
import datetime
import secrets
import socket
import time
import unicodedata
from html.parser import HTMLParser

# The interface installs a callback here so the long, single-threaded
# passes below can keep the busy spinner turning as they go.  It stays
# None everywhere else (scripts, tests), so the core never depends on the
# GUI being there.
PROGRESS_HOOK = None


def tick(message=None):
   # v72: only the screen's own thread may move the spinner - a background
   # copy (reader_copy) waiting on a lock must not touch the window
   if PROGRESS_HOOK is not None and threading.current_thread() is threading.main_thread():
       PROGRESS_HOOK(message)


# ---------------------------------------------------------------------------
#  WAITING OUT SOMEBODY ELSE'S WRITE
# ---------------------------------------------------------------------------
# How long a READ will keep trying before it gives up.  Reading must never
# fail because another person is saving: on a shared folder one user's
# import, or a schema upgrade rebuilding a table, holds the file for
# several seconds, and every other copy of the program was reading with a
# 1.5 second patience and then throwing.  That is what put "database is
# locked" on other people's screens while somebody was simply uploading.
#
# The wait is spent in short attempts rather than one long one on purpose:
# SQLite's own busy_timeout blocks inside the C call, where nothing can
# move the spinner, so a 30 second timeout would look exactly like a
# freeze.  Short attempts with a tick between them is the same patience,
# spent visibly.
READ_WAIT_SECONDS = 30.0
# A write is somebody's typing, and losing it is worse than
# waiting for it, so a short write waits longer than a read.
WRITE_WAIT_SECONDS = 45.0
_LOCK_WORDS = ("database is locked", "database table is locked",
               "database schema is locked", "database is busy")


def is_lock_error(exc):
    """Is this SQLite saying 'somebody else is writing', rather than a
    real fault?  Those are worth waiting out; nothing else is."""
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    return any(w in msg for w in _LOCK_WORDS)


def with_lock_retry(fn, seconds=None, on_wait=None):
    """Runs `fn`, waiting out another process's write and trying again.

    Anything that is not a lock is raised straight away - a real error
    must not be hidden behind thirty seconds of retrying.
    """
    deadline = time.time() + (READ_WAIT_SECONDS if seconds is None else seconds)
    delay = 0.15
    waited = False
    while True:
        try:
            out = fn()
            if waited and on_wait:
                on_wait(None)
            return out
        except sqlite3.Error as e:
            if not is_lock_error(e) or time.time() >= deadline:
                raise
            waited = True
            if on_wait:
                on_wait(e)
            tick("waiting for another user to finish saving")
            time.sleep(delay)
            delay = min(1.0, delay * 1.6)


APP_TITLE = "WIP TRACKER"
APP_CREDIT = "created by EYALTIRIKLI"
APP_DIR = os.path.join(os.path.expanduser("~"), ".rw_analysis")
DB_NAME = "rw_data.db"
DB_PATH = os.path.join(APP_DIR, DB_NAME)      # fallback only
PREFS_PATH = os.path.join(APP_DIR, "prefs.json")

ALL = "-- ALL --"
UNKNOWN = "-"
MAPPING_VERSION = 4
# ADMIN_PASSWORD is gone with the data locks.  A password typed into a
# box, the same one for everybody, shared by whoever needed it that week,
# was never the thing standing between the data and a mistake - the
# ACCOUNT is.  Who may change what is ctx.is_admin(), and nothing else.

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]
DEFAULT_WEEK_START = 4          # Friday
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]

# Cause operations that carry no meaning of their own.
CAUSE_OP_LABELS = {"870": "UNKNOWN"}

# A single rework entry longer than this many hours is treated as a data error.
DEFAULT_MAX_RW_HOUR = 50.0


def ensure_app_dir():
   if not os.path.isdir(APP_DIR):
       os.makedirs(APP_DIR, exist_ok=True)


def program_dir():
   """Folder the program itself sits in."""
   try:
       if getattr(sys, "frozen", False):
           return os.path.dirname(os.path.abspath(sys.executable))
       return os.path.dirname(os.path.abspath(__file__))
   except Exception:
       return os.getcwd()


def default_db_path():
   """
   The database lives next to the program by default.  Put the program in a
   shared folder and everybody automatically works on the same data.  If that
   folder cannot be written to, the private folder is used instead.
   """
   folder = program_dir()
   probe = os.path.join(folder, ".rw_write_test")
   try:
       with open(probe, "w") as f:
           f.write("x")
       os.remove(probe)
       return os.path.join(folder, DB_NAME)
   except Exception:
       ensure_app_dir()
       return DB_PATH


DB_POINTER_NAME = "shared_db_location.txt"


def read_db_pointer():
   """
   Whichever database an admin last pointed everyone at, if that choice
   was ever recorded - a plain text file sitting next to the program
   itself, so every copy of the program (one user's machine, another's,
   a fresh install nobody has touched yet) reads the same answer without
   each person having to pick the shared folder themselves.  Personal
   choices (which filters, which theme) stay in each user's own prefs
   file; only "where is the database" is meant to be the same for
   everyone, so this is the one thing kept next to the program instead.
   """
   try:
       path = os.path.join(program_dir(), DB_POINTER_NAME)
       with open(path, "r", encoding="utf-8") as f:
           value = f.read().strip()
       return value or None
   except Exception:
       return None


def write_db_pointer(db_path):
   """Records the shared database location for every other copy of the
   program to pick up.  Best-effort: if the program's own folder is not
   writable from here, personal prefs still remember the choice for this
   one machine, so this quietly does nothing rather than failing the
   switch itself."""
   try:
       path = os.path.join(program_dir(), DB_POINTER_NAME)
       with open(path, "w", encoding="utf-8") as f:
           f.write(db_path)
       return True
   except Exception:
       return False


def week_offset(week_start):
   """Days to add to a date before taking its ISO week number."""
   return (7 - int(week_start)) % 7


def load_prefs():
   """Per user settings.  Everything shared lives in the database instead."""
   try:
       with open(PREFS_PATH, "r", encoding="utf-8") as f:
           d = json.load(f)
       return d if isinstance(d, dict) else {}
   except Exception:
       return {}


def save_prefs(d):
   ensure_app_dir()
   try:
       with open(PREFS_PATH, "w", encoding="utf-8") as f:
           json.dump(d, f, ensure_ascii=False, indent=1)
   except Exception:
       pass


# ===========================================================================
#  1. THEMES
# ===========================================================================

THEMES = {
   # NEW DESIGN (Grafit): soft greys stepping up from the page to the
   # cards, one blue accent, quiet borders.  Colour on screen means TECO /
   # MRB / SCRAP / REPAIR; everything else stays neutral.  The keys are
   # the same as before, so every page follows without being touched.
   "day": {
       "name": "day",
       "bg": "#F2F4F7", "card": "#FFFFFF", "text": "#1A1F26", "muted": "#5D6876",
       "header": "#FFFFFF", "header_txt": "#1A1F26", "header_sub": "#5D6876",
       "accent": "#1F63D6", "accent_hover": "#174FB0", "danger": "#B3302F",
       "border": "#DCE1E8", "grid": "#D3D9E1", "grid_strong": "#5D6876",
       "head_cell": "#EEF1F5", "head_cell_txt": "#1A1F26",
       "row_a": "#FFFFFF", "row_b": "#F6F8FA", "subtotal": "#E3EAF4",
       "count": "#FBEBC9", "hour": "#F6D9D0", "field": "#FFFFFF",
       "dim": "#E7EAEE",
       "metric_txt": "#1A1F26", "tab_off": "#E7EAEE", "log_bg": "#FAFBFC",
       "credit": "#7D8794",
       "sel_row": "#DCE8FB", "sel_edge": "#1F63D6",
       # Lighter than the old #2E7D46, which read as nearly black-green.
       "wf_green": "#43A363", "wf_avg_bg": "#2C4A63", "wf_avg_fg": "#FFFFFF",
       "wf_warn": "#F2C94C", "wf_lt_border": "#1A1F26",
       "wf_wait": "#E3B93F", "wf_awaiting": "#1E8C8C",
       "wf_running": "#D97A24",
       "title_fg": "#1A1F26", "rail_bg": "#1B2A3A", "rail_fg": "#AFC0D2",
       "rail_active_bg": "#2B4560", "rail_active_fg": "#FFFFFF",
   },
   "night": {
       "name": "night",
       "bg": "#181818", "card": "#202020", "text": "#ECECEC", "muted": "#A0A0A0",
       "header": "#141414", "header_txt": "#ECECEC", "header_sub": "#A0A0A0",
       "accent": "#4F8AEB", "accent_hover": "#6A9DF0", "danger": "#C2453A",
       "border": "#2E2E2E", "grid": "#303030", "grid_strong": "#8A8A8A",
       "head_cell": "#272727", "head_cell_txt": "#D8D8D8",
       "row_a": "#202020", "row_b": "#262626", "subtotal": "#2D3442",
       "count": "#4A3F22", "hour": "#4A2C26", "field": "#1B1B1B",
       "dim": "#151515",
       "metric_txt": "#FFE9BC", "tab_off": "#262626", "log_bg": "#1B1B1B",
       "credit": "#7C7C7C",
       "sel_row": "#1F2A3D", "sel_edge": "#5B95F5",
       "wf_green": "#3D9A5B", "wf_avg_bg": "#2A3A52", "wf_avg_fg": "#FFFFFF",
       "wf_warn": "#F2C94C", "wf_lt_border": "#E0E0E0",
       "wf_wait": "#E0C04A", "wf_awaiting": "#2AA8A0",
       "wf_running": "#E08838",
       "title_fg": "#ECECEC", "rail_bg": "#141414", "rail_fg": "#9E9E9E",
       "rail_active_bg": "#262A31", "rail_active_fg": "#F0F0F0",
   },
}

WF_STATUS_COLOR_KEY = {"passed": "wf_green", "current": "danger",
                      "current_wait": "wf_wait",
                      # Booked but not yet up to its standard time: the
                      # part is on that machine right now, which is a
                      # different thing from queueing for the step.
                      "running": "wf_running",
                      "future": "row_b", "done_all": "accent",
                      "awaiting_shipment": "wf_awaiting"}
WF_LT_BORDER_WIDTH = 3


# ===========================================================================
#  2. FIELD DEFINITIONS
# ===========================================================================

COMP_FIELDS = [
   # "comp no" is listed first and matched as a whole word before "comp"
   # gets anywhere near it - otherwise the class column's own "comp"
   # keyword would grab the code column first (both contain "comp").
   ("work_center", "Work centre code", True,
    ["comp no", "is yeri", "isyeri", "work center", "work centre", "workcenter",
     "wc", "code"]),
   ("wc_name", "Work centre description", False,
    ["place", "name", "ad", "description", "aciklama", "machine", "tanim"]),
   ("comp", "COMP short description (class)", True,
    ["short description", "comp", "category", "kategori", "class", "sinif",
     "type", "tip", "group"]),
]

PART_FIELDS = [
   ("part_no", "Part number", True,
    ["part no", "partno", "part number", "part", "material", "malzeme"]),
   ("part_group", "Part group / STG", False,
    ["stg", "part group", "group", "grup", "family", "aile", "stage"]),
   ("engineer", "Engineer", True,
    ["engineer", "muhendis", "owner", "sorumlu", "responsible"]),
]

# ---------------------------------------------------------------------------
#  SAP EXPORT - FIXED COLUMN POSITIONS
#
#  Every piece of information always arrives in the same column, so position
#  is what the reader goes by.  The header text is a check, not the key.
# ---------------------------------------------------------------------------
# Words that rule a header OUT for a field: the plain labour column is
# "Iscilik", and "Rework Iscilik", "Setup Iscilik" and "Toplam Iscilik" all
# contain that word too.
# Bumped whenever the rebuild's progress rule changes - see _migrate.
PROG_RULE = "machine-else-labour-v1"

SAP_HEADER_NOT = {"labour_hour": ("rework", "setup", "toplam")}

SAP_COLUMNS = [
   # (field, column, expected header, verification keywords, required)
   ("material",       "E",  "Material",         ["material", "malzeme"],            True),
   ("serial_no",      "F",  "Order",            ["order", "serial", "seri"],        True),
   ("reject_op",      "G",  "OpAc",             ["opac", "op.no", "op no", "opno"], True),
   ("wc_code",        "J",  "Work ctr",         ["work ctr", "work center",
                                                 "work centre", "is yeri"],         True),
   ("rw_hour",        "Q",  "Rework Iscilik",   ["rework iscilik", "rework isc",
                                                 "rework is"],                      True),
   ("cause_op",       "AA", "Cause",            ["cause"],                          True),
   ("rw_date",        "C",  "Kyt tarihi",       ["kyt tarihi", "kayit tarihi",
                                                 "tarih"],                          True),
   ("time",           "Z",  "Time",             ["time", "saat"],                   True),
   # SEQUENCE and LAST-ENTRY MARK (v52).  One operation can be written
   # several times with a sequence after it - 280/1, 280/3, 280/4 - and a
   # booking belongs to the sequence it names.  The X in the "last"
   # column is what says the part is off that step.  Both are optional:
   # an export made before they existed simply has neither.
   ("seq",            "H",  "Seq. Number",      ["seq", "sekans", "sira no"],       False),
   ("last_x",         "AH", "Son",              ["son", "last", "final"],           False),
   # SETUP (v62).  An "H" in the Unit column marks a SETUP booking: the
   # machine was being set up, the part was not being cut.  A setup row's
   # X does not finish the operation, and its hours are not progress.
   ("unit",           "M",  "Unit",             ["unit", "birim"],                  False),
   # Optional: read when present, left empty otherwise.
   ("reject_op_desc", "I",  "Operation",        ["operation", "redlenen"],          False),
   ("wc_desc",        "K",  "Short desc",       ["short desc", "short description"], False),
   ("std_hour",       "R",  "Toplam Is",        ["toplam is", "toplam iscilik"],    False),
   # PROGRESS ON A STEP (v62, the shop's rule): machine time (column O),
   # and where a booking carries none - some operations are booked as
   # labour only - that booking's labour (column P, Iscilik).  Toplam Is
   # is NOT progress: it adds up every operator's labour on the machine,
   # so two people on one machine would run the part through twice as
   # fast.  Setup and rework bookings are left out of progress entirely.
   ("labour_hour",    "P",  "Iscilik",          ["iscilik", "labour", "labor"],     False),
   ("machine_hour",   "O",  "Makina sure",      ["activity to conf",
                                                 "activity to be conf",
                                                 "act. to conf", "makine sure",
                                                 "makina sure"],               False),
   ("cause_desc",     None, "Cause description", ["cause tan", "cause desc"],       False),
   ("cause_code",     None, "Cause code",       ["cause cod"],                      False),
   ("emp_no",         None, "Yaka No",          ["yaka no", "yaka",
                                                 "personel no", "employee no"],     False),
   ("emp_name",       None, "Name of employee", ["name of employee", "employee name",
                                                 "ad soyad"],                       False),
]

# Only used by the manual column-mapping dialog; a normal upload goes through
# SAP_COLUMNS above.
RW_FIELDS = [
   ("material", "MATERIAL  (col E)", True, ["material", "malzeme"]),
   ("serial_no", "SERIAL NO  (col F - Order)", False, ["order", "serial", "seri"]),
   ("reject_op", "OPERATION NUMBER  (col G - OpAc)", False, ["opac", "op.no", "op no"]),
   ("reject_op_desc", "OPERATION DESCRIPTION  (col I)", False, ["operation", "redlenen"]),
   ("cause_op", "CAUSE OP  (col AA)", False, ["cause"]),
   ("cause_code", "CAUSE CODE", False, ["cause cod"]),
   ("cause_desc", "CAUSE DESCRIPTION", False, ["cause tan", "cause desc"]),
   ("rw_hour", "REWORK LABOUR HOUR  (col Q)", True, ["rework iscilik", "rework isc"]),
   ("rw_date", "DATE  (col C - Kyt tarihi)", True, ["kyt tarihi", "tarih", "date"]),
   ("wc_code", "WORK CENTRE  (col J)", False, ["work ctr", "work center"]),
   ("wc_desc", "WORK CENTRE NAME  (col K)", False, ["short desc"]),
   ("emp_no", "EMPLOYEE NUMBER", False, ["yaka no", "employee no"]),
   ("emp_name", "EMPLOYEE NAME", False, ["name of employee", "employee name"]),
   ("order_no", "ORDER NUMBER (only if separate)", False, ["order number", "order no"]),
   ("entered_by", "ENTERED BY", False, ["entered by", "kaydi giren"]),
]

# ---------------------------------------------------------------------------
#  SHIPMENT / SALES REPORT ("Sevkiyatlar Raporu") - header-matched like COMP
#  and ENG x PART, since it is an ad-hoc report export rather than a fixed
#  SAP transaction dump.  A serial's presence here (with an Ac.GI date) is
#  what "sold" / "shipped" means for the WIP filter and for Waterfall.
# ---------------------------------------------------------------------------
SHIPMENT_FIELDS = [
   ("material", "Material / part number", True, ["material", "malzeme"]),
   ("serial_no", "Serial number", True, ["seri no", "serial", "seri"]),
   ("qty", "Quantity", False, ["qty", "miktar", "adet"]),
   ("irsaliye", "Delivery / irsaliye no", False, ["irsaliye", "delivery"]),
   ("ac_gi_date", "Actual GI date (ship/sale date)", True,
    ["ac.gi date", "ac gi date", "gi date", "gi tarihi", "sevk tarihi",
     "actual gi"]),
   ("po_no", "PO number", False, ["po numaras", "po no", "purchase order"]),
   ("poitem", "PO item", False, ["poitem", "po item"]),
   ("sales_doc", "Sales document", False, ["sales doc", "satis belge"]),
   ("ship_to", "Ship-to", False, ["ship-to", "ship to"]),
   ("sorg", "Sales org", False, ["sorg", "sales org"]),
   ("created_on", "Created on", False, ["created on", "olusturma"]),
]

FIELD_SETS = {"comp": COMP_FIELDS, "parts": PART_FIELDS, "rw": RW_FIELDS,
             "shipment": SHIPMENT_FIELDS}
DATASET_LABEL = {"rw": "SAP RW", "comp": "COMP DESCRIPTION", "parts": "ENG x PART",
                "shipment": "SHIPMENT REPORT", "traveler": "TRAVELER",
                "flags": "MRB / SCRAP / TECO"}
DATASET_ORDER = ["rw", "comp", "parts", "shipment"]
# The 4 inputs the whole app runs on, shown together in one upload panel:
# the SAP export feeds both the rework analysis and the waterfall page in
# one pass, COMP and ENG x PART are lookup tables, and TRAVELER is the
# waterfall's own route data.
# TRAVELER and the MRB/SCRAP/TECO records are not SAP exports, so they sit
# after the four that are - but they load through the same panel.
UPLOAD_ROWS = DATASET_ORDER + ["traveler", "flags"]


# ===========================================================================
#  3. HELPERS
# ===========================================================================

def fold(value):
   """
   Lower case a header for matching.

   Turkish needs care: "Rework Iscilik".lower() keeps a combining dot on the
   capital I, so a plain substring test against "rework isc" fails.  The
   Turkish letters are replaced first and the remaining accents stripped.
   """
   if value is None:
       return ""
   t = str(value)
   for a, b in (("\u0130", "I"), ("\u0131", "i"), ("\u015e", "S"), ("\u015f", "s"),
                ("\u00c7", "C"), ("\u00e7", "c"), ("\u011e", "G"), ("\u011f", "g"),
                ("\u00dc", "U"), ("\u00fc", "u"), ("\u00d6", "O"), ("\u00f6", "o")):
       t = t.replace(a, b)
   t = unicodedata.normalize("NFKD", t)
   t = "".join(c for c in t if not unicodedata.combining(c))
   return t.lower().strip()


def index_col(idx):
   """Zero based index -> Excel column letter.  2->'C', 16->'Q', 26->'AA'."""
   if idx is None or idx < 0:
       return "?"
   out, n = "", idx + 1
   while n:
       n, rem = divmod(n - 1, 26)
       out = chr(65 + rem) + out
   return out


def col_index(letter):
   """Excel column letter -> zero based index.  'C'->2, 'Q'->16, 'AA'->26."""
   if not letter:
       return None
   n = 0
   for ch in str(letter).upper():
       if not ("A" <= ch <= "Z"):
           return None
       n = n * 26 + (ord(ch) - 64)
   return n - 1


_WS_RE = re.compile(r"\s+")


def norm_text(value):
   """
   Upper case, whitespace removed.

   A 350,000 row import calls this several million times, so the regular
   expression only runs when the string actually contains whitespace.
   """
   if value is None:
       return ""
   if type(value) is not str:
       value = str(value)
   s = value.strip().upper()
   if "\u00a0" in s:
       s = s.replace("\u00a0", " ")
   if " " in s or "\t" in s or "\n" in s:
       s = _WS_RE.sub("", s)
   return s


def shipment_serial_key(serial_no):
   """The shipment/sales report's own serial numbers carry a 'TMT' prefix
   the shop-floor data (SAP RW, traveler, production steps) does not -
   TMT2G7RR there is the same part as 2G7RR everywhere else.  Stripped
   before it is ever compared to a serial_key from another table."""
   s = norm_text(serial_no)
   if s.startswith("TMT"):
       s = s[3:]
   # Some report rows carry an extra 'RTD' in front: RTD2G7RR is 2G7RR.
   if s.startswith("RTD") and len(s) > 3:
       s = s[3:]
   return s


_OP_KEY_CACHE = {}


_OP_DECIMAL = re.compile(r"^(\d+)[.,]0+$")


def op_display(value):
   """2.1.3: an operation number as the shop writes it - "580.00" from a
   decimal-formatted export shows as 580; anything else is left alone."""
   t = clean_str(value)
   m = _OP_DECIMAL.match(t)
   return m.group(1) if m else t


_OP_SHOWN_CACHE = {}


def op_shown(value):
   """2.2.0: an operation number the way the TRAVELER writes it.  The
   traveler holds plain numbers (600), the export writes the same step
   0600 on one row and 600 on another - and a table grouped by that text
   then showed one operation as two.  A number loses its leading zeros
   (and a ".00"); anything that is not a number is left as typed."""
   hit = _OP_SHOWN_CACHE.get(value)
   if hit is not None:
       return hit
   t = op_display(value)
   out = (t.lstrip("0") or "0") if t.isdigit() else t
   if len(_OP_SHOWN_CACHE) < 100000:
       _OP_SHOWN_CACHE[value] = out
   return out


def op_key(value):
   """
   Operation numbers appear both padded and unpadded: the reject operation is
   written 0600 while the cause operation of the same step is written 600.
   Leading zeros are dropped so the two can be matched.

   A shop has a few dozen operation numbers and an import asks about them
   millions of times, so the answers are memoised - the cache can never
   grow past the number of distinct operation values in the data.
   """
   try:
       hit = _OP_KEY_CACHE.get(value, _MISSING)
   except TypeError:                       # unhashable, should not happen
       hit = _MISSING
       value = str(value)
   if hit is not _MISSING:
       return hit
   t = norm_text(value)
   # 2.1.3: an export that wrote the number as a decimal - 580.00, 580,0
   # - is the same operation as 580.
   m = _OP_DECIMAL.match(t)
   if m:
       t = m.group(1)
   out = t.lstrip("0") or ("0" if t else "")
   if len(_OP_KEY_CACHE) < 100000:
       _OP_KEY_CACHE[value] = out
   return out


def is_setup_mark(value):
   """Unit column (M) "H" = a SETUP booking (v62)."""
   return clean_str(value).upper() == "H"


def is_last_mark(value):
   """Is this the LAST-entry mark?

   SAP writes an X in the "last" column on the booking that takes the
   part off that operation.  Anything that is not plainly a no - X, x, 1,
   yes, evet - counts as the mark; a blank never does.
   """
   t = norm_text(value)
   if not t or t in ("0", "N", "NO", "HAYIR", "FALSE", "-"):
       return False
   return True


def seq_key(value):
   """A sequence number, normalised - "01", 1 and 1.0 are the same seq.

   Empty when there is none, which is what an ordinary operation has.

   ZERO MEANS NO SEQUENCE.  SAP writes the sequence column sometimes as
   0 and sometimes as nothing at all for the same step, so "130" and
   "130/0" are the same piece of work - and read as two, they are two
   route lines, two standard times and two queues where the shop has
   one.
   """
   t = norm_text(value)
   if not t:
       return ""
   try:
       n = int(float(t))
   except ValueError:
       return t
   return "" if n == 0 else str(n)


def step_key(op, seq=None):
   """THE IDENTITY OF ONE PIECE OF WORK on the route.

   Without sequences this is just the operation number, exactly as
   before.  With them, one operation is written several times - 280/1,
   280/3, 280/4 - and each of those is a separate piece of work with its
   own standard time, booked against its own sequence.  Matching a
   booking to the route therefore has to compare BOTH, or the hours of
   three different steps end up in one pile.

   Sequences ARE in route order (v62): 280/0 -> 280/2 -> 280/7, each one
   a step of its own, closed by its own X like any other.
   """
   o = op_key(op)
   q = seq_key(seq)
   return ("%s/%s" % (o, q)) if q else o


def split_step(value):
   """"280/1" -> (280, "1").   "280" -> (280, "").

   The traveler writes the two together in one cell, because that is how
   the shop writes it on paper.
   """
   t = clean_str(value)
   if not t:
       return None, ""
   seq = ""
   if "/" in t:
       t, _sep, seq = t.partition("/")
   t = t.strip()
   try:
       op = int(float(t))
   except (TypeError, ValueError):
       return None, seq_key(seq)
   return op, seq_key(seq)


def clean_str(value):
   if value is None:
       return ""
   if isinstance(value, float) and value.is_integer():
       return str(int(value))
   return str(value).strip()


def parse_float(value):
   if value is None:
       return 0.0
   if isinstance(value, (int, float)):
       return float(value)
   s = str(value).strip().replace(" ", "")
   if not s:
       return 0.0
   if "," in s and "." in s:
       if s.rfind(",") > s.rfind("."):
           s = s.replace(".", "").replace(",", ".")
       else:
           s = s.replace(",", "")
   elif "," in s:
       s = s.replace(",", ".")
   try:
       return float(s)
   except ValueError:
       return 0.0


DATE_UNAMBIGUOUS = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
                   "%d.%m.%Y", "%d-%m-%Y", "%d.%m.%y"]
DATE_MONTH_FIRST = ["%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%d/%m/%y"]
DATE_DAY_FIRST = ["%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y"]


def parse_date(value, day_first=False):
   if value is None:
       return None
   if isinstance(value, datetime.datetime):
       return value.date()
   if isinstance(value, datetime.date):
       return value
   if isinstance(value, (int, float)):
       try:
           return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(value))
       except Exception:
           return None
   s = str(value).strip().split(" ")[0]
   if not s:
       return None
   for fmt in DATE_UNAMBIGUOUS + (DATE_DAY_FIRST if day_first else DATE_MONTH_FIRST):
       try:
           return datetime.datetime.strptime(s, fmt).date()
       except ValueError:
           continue
   return None


def iso_year_week(d, offset=0):
   """
   Year and week number of a date.

   offset shifts the date before the ISO calculation, which is how the
   Friday-to-Thursday working week is expressed: with offset 3 a Friday
   already belongs to the week of the Monday that follows it.
   """
   if d is None:
       return (None, None)
   if offset:
       d = d + datetime.timedelta(days=int(offset))
   iso = d.isocalendar()
   return (int(iso[0]), int(iso[1]))


def week_bounds(year, week, week_start):
   """First and last calendar day of a shifted week."""
   off = week_offset(week_start)
   try:
       monday = datetime.date.fromisocalendar(int(year), int(week), 1)
   except Exception:
       return None, None
   start = monday - datetime.timedelta(days=off)
   return start, start + datetime.timedelta(days=6)


def fmt_hour(v):
   return ("%.2f" % float(v or 0)).rstrip("0").rstrip(".") or "0"


def guess_column(headers, keywords):
   low = [fold(h) for h in headers]
   for kw in keywords:
       for i, h in enumerate(low):
           if h == kw:
               return i
   for kw in keywords:
       for i, h in enumerate(low):
           if h.startswith(kw):
               return i
   for kw in keywords:
       for i, h in enumerate(low):
           if kw in h:
               return i
   return None


def auto_map(headers, fields):
   """Maps columns in field order; a column is never used twice."""
   mapping, missing, used = {}, [], set()
   low = [fold(h) for h in headers]
   for key, label, required, keywords in fields:
       idx = None
       for stage in (0, 1, 2):
           for kw in keywords:
               for i, h in enumerate(low):
                   if i in used:
                       continue
                   if (stage == 0 and h == kw) or (stage == 1 and h.startswith(kw)) \
                           or (stage == 2 and kw in h):
                       idx = i
                       break
               if idx is not None:
                   break
           if idx is not None:
               break
       mapping[key] = idx
       if idx is not None:
           used.add(idx)
       elif required:
           missing.append(label)
   return mapping, missing


def detect_dataset(headers):
   # A literal '+' header cell is the wide traveler layout's own unambiguous
   # marker (see wf_detect_traveler_format) - checked first since nothing
   # else in a real header row ever looks like that.
   if any(clean_str(h) == "+" for h in headers):
       return "traveler", 99

   # The exported MRB/SCRAP/TECO sheet names itself; nothing else has a
   # TYPE column holding these words.
   low_all = " | ".join(fold(h) for h in headers)
   if ("serial" in low_all or "seri" in low_all) and (
           "type" in low_all or "kind" in low_all) and (
           "reason" in low_all or "note" in low_all):
       return "flags", 90

   scores = {}
   for key, fields in FIELD_SETS.items():
       s = 0
       for fkey, label, required, kws in fields:
           if guess_column(headers, kws) is not None:
               s += 2 if required else 1
       scores[key] = s
   # Long-layout traveler: Part / Operation / Predecessor all present.
   trav_hits = sum(1 for kws in (WF_TRAVELER_PART_KEYWORDS, WF_TRAVELER_OP_KEYWORDS,
                                 WF_TRAVELER_PRED_KEYWORDS)
                   if guess_column(headers, kws) is not None)
   scores["traveler"] = trav_hits * 2 if trav_hits >= 3 else 0
   low = " | ".join(fold(h) for h in headers)
   if "engineer" in low or "muhendis" in low:
       scores["parts"] += 6
   if "stg" in low:
       scores["parts"] += 3
   if "comp" in low or "kategori" in low or "short description" in low:
       scores["comp"] += 4
   for kw in ("cause", "toplam saat", "rw&rp", "redlenen", "op.no",
              "rework isc", "rework is", "work ctr", "opac", "kyt tarihi"):
       if kw in low:
           scores["rw"] += 3
   best = max(scores, key=lambda k: scores[k])
   return (best, scores[best]) if scores[best] > 0 else (None, 0)


# ===========================================================================
#  4. FILE READING
# ===========================================================================

class _TableParser(HTMLParser):
   """Collects every <table> of an HTML document as a list of rows."""

   def __init__(self):
       super().__init__(convert_charrefs=True)
       self.tables, self._t, self._r, self._c, self._span = [], None, None, None, 1

   def handle_starttag(self, tag, attrs):
       a = dict(attrs)
       if tag == "table":
           if self._t is not None:
               self.tables.append(self._t)
           self._t = []
       elif tag == "tr" and self._t is not None:
           self._r = []
       elif tag in ("td", "th") and self._r is not None:
           self._c = []
           try:
               self._span = max(1, int(a.get("colspan") or 1))
           except ValueError:
               self._span = 1

   def handle_endtag(self, tag):
       if tag in ("td", "th") and self._c is not None:
           text = re.sub(r"\s+", " ", "".join(self._c)).strip()
           self._r.append(text)
           for _ in range(self._span - 1):
               self._r.append("")
           self._c = None
       elif tag == "tr" and self._r is not None:
           self._t.append(self._r)
           self._r = None
       elif tag == "table" and self._t is not None:
           self.tables.append(self._t)
           self._t = None

   def handle_data(self, data):
       if self._c is not None:
           self._c.append(data)

   def close(self):
       super().close()
       if self._t:
           self.tables.append(self._t)


def _file_head(path, n=8192):
   try:
       with open(path, "rb") as f:
           return f.read(n)
   except OSError:
       return b""


def _decode_head(head):
   """The start of a text file as text, whatever it was saved as (SAP
   writes UTF-16 with a byte-order mark; Excel's text export the Windows
   Turkish code page)."""
   for bom, enc in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
                    (b"\xef\xbb\xbf", "utf-8-sig")):
       if head.startswith(bom):
           return head.decode(enc, "ignore"), enc
   if b"\x00" in head:
       return None, None
   try:
       return head.decode("utf-8"), "utf-8"
   except UnicodeDecodeError:
       return head.decode("cp1254", "ignore"), "cp1254"


SS_NS = "urn:schemas-microsoft-com:office:spreadsheet"


def open_xlsx(path):
   """v73: openpyxl by CONTENT, not by name.  openpyxl refuses anything
   not called .xlsx / .xlsm - a real .xlsx saved as "x.xls" got "does
   not support the old .xls format, use xlrd", and xlrd cannot read it.
   Handed an open file it goes by what is inside."""
   import openpyxl
   ext = os.path.splitext(path)[1].lower()
   if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
       return openpyxl.load_workbook(path, read_only=True, data_only=True)
   f = open(path, "rb")
   try:
       return openpyxl.load_workbook(f, read_only=True, data_only=True)
   except Exception:
       f.close()
       raise


def looks_like_spreadsheet_xml(path):
   """v73: True for an "XML Spreadsheet 2003" file - what SAP and older
   Excel save under an .xls name.  It is neither an old binary workbook
   (xlrd) nor a new one (openpyxl), so both refused it and the message
   said "use xlrd" although xlrd was there."""
   txt, _enc = _decode_head(_file_head(path))
   if not txt:
       return False
   low = txt.lower()
   return SS_NS in low or ("<?xml" in low and "<workbook" in low and "<worksheet" in low)


def looks_like_text_table(path):
   """v73: True for a tab / semicolon / comma separated text file whatever
   its extension - SAP's "spreadsheet" download is often exactly that,
   named .xls."""
   head = _file_head(path)
   if not head or head.startswith(b"PK") or head.startswith(b"\xd0\xcf\x11\xe0"):
       return False
   txt, _enc = _decode_head(head)
   if not txt:
       return False
   low = txt.lstrip().lower()
   if low.startswith("<"):
       return False                          # HTML / XML, read elsewhere
   lines = [l for l in txt.splitlines()[:5] if l.strip()]
   return bool(lines) and any(("\t" in l) or (";" in l) or ("," in l) for l in lines)


def read_text_table_rows(path):
   with open(path, "rb") as f:
       raw = f.read()
   _txt, enc = _decode_head(raw[:8192])
   text = raw.decode(enc or "utf-8", "replace")
   if text and text[0] == "\ufeff":
       text = text[1:]
   sample = text[:8192]
   try:
       dialect = csv.Sniffer().sniff(sample, delimiters="\t;,|")
   except Exception:
       dialect = csv.excel_tab if "\t" in sample else csv.excel
   return [r for r in csv.reader(text.splitlines(), dialect)]


def _ss_sheets(path):
   import xml.etree.ElementTree as ET
   root = ET.parse(path).getroot()
   return root.findall("{%s}Worksheet" % SS_NS)


def spreadsheet_xml_sheet_names(path):
   return [ws.get("{%s}Name" % SS_NS) or "Sheet%d" % (i + 1)
           for i, ws in enumerate(_ss_sheets(path))]


def read_spreadsheet_xml_rows(path, sheet_name=None):
   """Rows out of an XML Spreadsheet 2003 file.  Skipped cells and rows
   (ss:Index) are kept as blanks so a fixed column such as "Q" still lands
   on column Q; numbers and dates come back as numbers and datetimes."""
   q = "{%s}" % SS_NS
   sheets = _ss_sheets(path)
   if not sheets:
       return []
   ws = sheets[0]
   if sheet_name:
       for w in sheets:
           if w.get(q + "Name") == sheet_name:
               ws = w
               break
   table = ws.find(q + "Table")
   if table is None:
       return []
   rows = []
   for row in table.findall(q + "Row"):
       ri = row.get(q + "Index")
       if ri:
           while len(rows) < int(ri) - 1:
               rows.append([])
       vals = []
       for cell in row.findall(q + "Cell"):
           ci = cell.get(q + "Index")
           if ci:
               while len(vals) < int(ci) - 1:
                   vals.append(None)
           data = cell.find(q + "Data")
           v = None
           if data is not None:
               t = data.get(q + "Type") or "String"
               raw = "".join(data.itertext())
               if t == "Number":
                   try:
                       f = float(raw)
                       v = int(f) if f.is_integer() else f
                   except ValueError:
                       v = raw
               elif t == "DateTime":
                   try:
                       v = datetime.datetime.fromisoformat(raw.split(".")[0])
                   except ValueError:
                       v = raw
               elif t == "Boolean":
                   v = raw.strip() in ("1", "true", "True")
               else:
                   v = raw
           vals.append(v)
           span = cell.get(q + "MergeAcross")
           if span:
               vals.extend([None] * int(span))
       rows.append(vals)
   return rows


def looks_like_html(path):
   """True for HTML and MHTML files, whatever extension they carry."""
   try:
       with open(path, "rb") as f:
           head = f.read(8192).lower()
   except OSError:
       return False
   return (b"<html" in head or b"<table" in head or b"mime-version" in head
           or b"content-type: multipart" in head)


def _extract_html(path):
   """Returns the HTML text of a plain HTML file or of an MHTML container."""
   with open(path, "rb") as f:
       raw = f.read()
   low = raw[:4096].lower()
   if low.lstrip().startswith(b"mime-version") or b"content-type: multipart" in low:
       try:
           msg = email.message_from_bytes(raw)
           chunks = []
           for part in (msg.walk() if msg.is_multipart() else [msg]):
               if part.get_content_type() in ("text/html", "application/xhtml+xml"):
                   payload = part.get_payload(decode=True) or b""
                   cs = part.get_content_charset() or "utf-8"
                   try:
                       chunks.append(payload.decode(cs, "replace"))
                   except LookupError:
                       chunks.append(payload.decode("utf-8", "replace"))
           if chunks:
               return "\n".join(chunks)
       except Exception:
           pass
   for cs in ("utf-8-sig", "utf-8", "cp1254", "latin-1"):
       try:
           return raw.decode(cs)
       except UnicodeDecodeError:
           continue
   return raw.decode("utf-8", "replace")


def read_html_rows(path):
   """Returns the biggest table of an HTML / MHTML file as a list of rows."""
   parser = _TableParser()
   parser.feed(_extract_html(path))
   parser.close()
   if not parser.tables:
       return []
   best = max(parser.tables,
              key=lambda t: (len(t), max((len(r) for r in t), default=0)))
   width = max((len(r) for r in best), default=0)
   return [r + [""] * (width - len(r)) for r in best]


def looks_like_biff_xls(path):
   """True for a real Excel 97-2003 .xls - the old binary format.

   Plenty of "xls" files out of SAP and similar systems are actually HTML
   or MHTML with an .xls name, which looks_like_html() already catches;
   this is the other case, an actual OLE2 compound file, which openpyxl
   cannot read at all.
   """
   try:
       with open(path, "rb") as f:
           head = f.read(8)
   except OSError:
       return False
   # OLE2 compound document magic (Excel 97-2003), and the much rarer
   # raw BIFF stream some exporters still emit.
   return head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or head[:2] in (
       b"\x09\x08", b"\x09\x04", b"\x09\x02", b"\x09\x00")


def _xls_rows(path, sheet_name=None, progress=None):
   """Rows out of a binary .xls, via xlrd (the only thing that reads it).

   Dates come back as numbers in this format, so they are turned into real
   datetimes here - otherwise every date column would arrive as 45903.0
   and parse_date would throw the whole row away.
   """
   try:
       import xlrd
   except ImportError:
       raise RuntimeError(
           "This is an Excel 97-2003 (.xls) file and reading it needs the "
           "'xlrd' package.  Install it with:  pip install xlrd")
   book = xlrd.open_workbook(path, on_demand=True)
   try:
       sh = book.sheet_by_name(sheet_name) if sheet_name else book.sheet_by_index(0)
       datemode = book.datemode
       rows = []
       for i in range(sh.nrows):
           out = []
           for cell in sh.row(i):
               v = cell.value
               if cell.ctype == xlrd.XL_CELL_DATE:
                   try:
                       y, mo, d, h, mi, s = xlrd.xldate_as_tuple(v, datemode)
                       v = (datetime.datetime(y, mo, d, h, mi, s)
                            if (y or mo or d) else datetime.time(h, mi, s))
                   except Exception:
                       pass
               elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                   v = bool(v)
               elif cell.ctype == xlrd.XL_CELL_EMPTY:
                   v = None
               out.append(v)
           rows.append(out)
           if progress is not None and (i & 8191) == 0:
               progress(i, sh.nrows)
       return rows
   finally:
       try:
           book.release_resources()
       except Exception:
           pass


def list_sheets(path):
   ext = os.path.splitext(path)[1].lower()
   if ext in (".csv", ".txt", ".tsv", ".mht", ".mhtml", ".html", ".htm"):
       return []
   if looks_like_spreadsheet_xml(path):
       try:
           return spreadsheet_xml_sheet_names(path)
       except Exception:
           return []
   if looks_like_html(path):
       return []
   if looks_like_text_table(path):
       return []
   if looks_like_biff_xls(path):
       try:
           import xlrd
           book = xlrd.open_workbook(path, on_demand=True)
           names = list(book.sheet_names())
           try:
               book.release_resources()
           except Exception:
               pass
           return names
       except Exception:
           return []
   try:
       wb = open_xlsx(path)
       names = list(wb.sheetnames)
       wb.close()
       return names
   except Exception:
       return []


def read_serial_list_file(path):
   """
   A serial-number list has no structure worth guessing at: every
   non-empty cell, on every sheet, is one serial number.

   It goes through the same reader as every other file, so a list pasted
   into an old .xls, or saved out of SAP as MHTML, works exactly like an
   .xlsx one - this used to call openpyxl directly and quietly refused
   anything it could not open.
   """
   out = []
   sheets = list_sheets(path) or [None]
   for sheet in sheets:
       try:
           rows = _raw_rows(path, sheet)
       except Exception:
           continue
       for row in rows:
           for v in row:
               if v is None:
                   continue
               s = str(v).strip()
               if s:
                   out.append(s)
   return out


def _raw_rows(path, sheet_name=None, progress=None):
   """
   Reads any supported file into a plain list of rows.

   Blank leading columns are kept exactly as they are, so that a fixed
   column position such as "Q" still lands on column Q.
   """
   ext = os.path.splitext(path)[1].lower()
   if ext in (".mht", ".mhtml", ".html", ".htm"):
       return read_html_rows(path)
   if ext in (".csv", ".txt", ".tsv"):
       with open(path, "r", encoding="utf-8-sig", newline="") as f:
           sample = f.read(8192)
           f.seek(0)
           try:
               dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
           except Exception:
               dialect = csv.excel
           return [r for r in csv.reader(f, dialect)]
   if looks_like_spreadsheet_xml(path):
       # v73: "XML Spreadsheet 2003" with an .xls name (it also contains
       # "<Table", so it must be caught before the HTML check)
       return read_spreadsheet_xml_rows(path, sheet_name)
   if looks_like_html(path):
       # An .xls / .xlsx that is really an HTML or MHTML export.
       return read_html_rows(path)
   if looks_like_biff_xls(path):
       # A real Excel 97-2003 workbook - openpyxl cannot open these at
       # all, so it never gets the chance to fail on one.
       return _xls_rows(path, sheet_name, progress=progress)
   if looks_like_text_table(path):
       # v73: tab / semicolon separated text under an .xls name (SAP)
       return read_text_table_rows(path)
   try:
       wb = open_xlsx(path)
   except Exception:
       rows = read_html_rows(path)
       if rows:
           return rows
       try:
           return _xls_rows(path, sheet_name, progress=progress)
       except Exception:
           pass
       if ext == ".xls":
           # say what the file really is instead of openpyxl's "use xlrd"
           head = _file_head(path, 16)
           raise RuntimeError(
               "This .xls file is not in a format the program can read (it is not an old "
               "Excel workbook, an XML spreadsheet, a web page or a text table; it starts "
               "with %r).  Open it in Excel and save it as .xlsx." % head[:8])
       raise
   ws = wb[sheet_name] if sheet_name else wb.active
   # Reading half a million rows out of a workbook takes the better part
   # of a minute no matter what, so it reports progress as it goes - that
   # is what keeps the busy spinner turning instead of the window looking
   # dead for the whole file.
   rows = []
   append = rows.append
   for i, r in enumerate(ws.iter_rows(values_only=True)):
       append(list(r))
       if progress is not None and (i & 8191) == 0:
           progress(i, None)
   wb.close()
   return rows


def _row_is_repeated_header(row, folded_headers):
   """
   True for a data row that is actually the header line printed again -
   common in large exports stitched together from several pulls, each with
   its own header row and a blank gap before it.
   """
   # A repeated header line prints every column, so its first column always
   # matches.  Testing that one cell first turns a 26-column comparison per
   # row into a single one for the millions of rows that are ordinary data.
   for i, fh in enumerate(folded_headers):
       if not fh:
           continue
       if i >= len(row) or fold(clean_str(row[i])) != fh:
           return False
       break
   checked = matched = 0
   for i, fh in enumerate(folded_headers):
       if not fh or i >= len(row):
           continue
       checked += 1
       if fold(clean_str(row[i])) == fh:
           matched += 1
   return checked >= 2 and matched >= max(2, int(round(checked * 0.6)))


def read_table(path, sheet_name=None, header_row=1, max_rows=None, progress=None):
   """Returns (headers, body_rows, repeated_header_rows_skipped)."""
   rows = _raw_rows(path, sheet_name, progress=progress)
   if not rows:
       return [], [], 0
   hidx = min(max(0, header_row - 1), len(rows) - 1)
   headers = [clean_str(c) for c in rows[hidx]]
   folded_headers = [fold(h) for h in headers]
   body = []
   skipped_headers = 0
   append = body.append
   for r in rows[hidx + 1:]:
       if r is None:
           continue
       # Blank-row test without str() on every cell - a number is never
       # blank, so only the strings need stripping.
       blank = True
       for c in r:
           if c is not None and (c.strip() if isinstance(c, str) else True):
               blank = False
               break
       if blank:
           continue
       if _row_is_repeated_header(r, folded_headers):
           skipped_headers += 1
           continue
       # _raw_rows() already built a fresh list per row and nothing else
       # keeps a reference to them, so copying again is pure waste.
       append(r if type(r) is list else list(r))
       if max_rows and len(body) >= max_rows:
           break
   return headers, body, skipped_headers


def _header_score(cells):
   """How many mandatory fields of any data set this row could be a header for."""
   headers = [clean_str(c) for c in cells]
   # A literal '+' cell is the wide traveler layout's own header marker - it
   # never appears anywhere else, so it settles the question outright rather
   # than being outscored by some unrelated row that happens to match a few
   # RW/COMP/PARTS keywords by accident.
   if any(h == "+" for h in headers):
       return 1000
   best = 0
   for fields in FIELD_SETS.values():
       s = sum(1 for k, l, req, kw in fields
               if req and guess_column(headers, kw) is not None)
       best = max(best, s)
   # a SAP export is recognised by its own fixed headers as well
   s = sum(1 for f, letter, exp, kw, req in SAP_COLUMNS
           if req and guess_column(headers, kw) is not None)
   return max(best, s)


def find_header_row(rows, max_scan=20):
   """
   Finds the header row.

   Counting filled cells alone is not enough: a title line above the table,
   or a header whose right hand cells are blank, would be picked instead.
   Candidate rows are first scored by how many known column names they
   contain, and only then by how full they are.
   """
   cands = []
   for i, r in enumerate(rows[:max_scan]):
       if r is None:
           continue
       fill = sum(1 for c in r if c is not None and str(c).strip() != "")
       if fill == 0:
           continue
       cands.append((i, _header_score(r), fill))
   if not cands:
       return 1
   best_score = max(c[1] for c in cands)
   if best_score > 0:
       for i, s, _f in cands:
           if s == best_score:
               return i + 1
   best_fill = max(c[2] for c in cands)
   for i, _s, f in cands:
       if f == best_fill:
           return i + 1
   return 1


def sniff_header_row(path, sheet_name=None):
   try:
       rows = _raw_rows(path, sheet_name)[:24]
   except Exception:
       return 1
   return find_header_row(rows)


# ---------------------------------------------------------------------------
#  SAP READER  -  fixed columns, verified by header text
# ---------------------------------------------------------------------------

def sap_mapping(headers, log=None):
   """
   Works out which column each SAP field sits in.

   1. the fixed column, when it carries the expected header
   2. otherwise the column that does carry that header, wherever it is
   3. otherwise the fixed column, but only when its header cell is empty

   If the fixed column holds some other column's header the field is left
   empty instead: reading the wrong column is worse than reading nothing.
   Returns (mapping, notes).
   """
   folded = [fold(h) for h in headers]
   mapping, notes = {}, []

   def matches(idx, keywords, field=None):
       if idx is None or idx >= len(folded):
           return False
       h = folded[idx]
       if any(w in h for w in SAP_HEADER_NOT.get(field, ())):
           return False
       return bool(h) and any(kw in h for kw in keywords)

   for field, letter, expected, keywords, required in SAP_COLUMNS:
       fixed = col_index(letter) if letter else None
       fixed_header = folded[fixed] if (fixed is not None and fixed < len(folded)) else None

       if matches(fixed, keywords, field):
           mapping[field] = fixed
           continue

       found = None
       for i, h in enumerate(folded):
           if matches(i, keywords, field):
               found = i
               break
       if found is not None:
           mapping[field] = found
           if fixed is not None and found != fixed:
               notes.append("%s: expected in column %s, found '%s' in column %s"
                            % (field, letter,
                               headers[found] if found < len(headers) else "?",
                               index_col(found)))
           continue

       if fixed is not None and fixed < len(headers) and not fixed_header:
           mapping[field] = fixed
           notes.append("%s: no '%s' header, using the unlabelled column %s"
                        % (field, expected, letter))
       else:
           mapping[field] = None
           msg = "%s: no '%s' header found" % (field, expected)
           if fixed_header:
               msg += " (column %s holds '%s', not used)" % (
                   letter, headers[fixed] if fixed < len(headers) else "?")
           if required:
               msg = "REQUIRED " + msg
           notes.append(msg)

   if log:
       for n in notes:
           log("   " + n)
   return mapping, notes


def sap_parse_time(value):
   """The record time in column Z: text, datetime, or an Excel day fraction."""
   if value is None:
       return None
   if isinstance(value, datetime.datetime):
       return value.time()
   if isinstance(value, datetime.time):
       return value
   if isinstance(value, (int, float)):
       try:
           secs = int(round((float(value) % 1.0) * 86400))
           return datetime.time(secs // 3600 % 24, secs % 3600 // 60, secs % 60)
       except Exception:
           return None
   s = str(value).strip()
   if not s:
       return None
   if " " in s:                      # "21.08.2026 09:05"
       s = s.split(" ")[-1]
   for fmt in ("%H:%M:%S", "%H:%M", "%H.%M.%S", "%H.%M"):
       try:
           return datetime.datetime.strptime(s, fmt).time()
       except ValueError:
           continue
   return None


def sap_read(path, sheet_name=None, header_row=1, day_first=True, log=None,
             progress=None, table=None):
   """
   Reads the SAP export ONCE and produces the record list that both the
   rework analysis and the waterfall use.  Feeding both from the same read
   means the two can never disagree about what the file said.

   `table` accepts an already-read (headers, rows, skipped) triple.  The
   upload page reads the workbook to detect the dataset before it knows the
   file is a SAP export, and re-reading half a million rows off disk purely
   to throw the first read away doubled the import time.
   """
   if table is not None:
       headers, rows, skipped = table
   else:
       headers, rows, skipped = read_table(path, sheet_name, header_row,
                                           progress=progress)
   mapping, notes = sap_mapping(headers, log=log)
   out, bad_date, no_material = [], 0, 0
   # The column positions are identical for every row, so they are read
   # out of the mapping once here.  Defining a `cell()` closure inside the
   # loop meant building half a million closures and calling them seven
   # million times just to look up the same handful of indices.
   NONE = -1
   def col(key):
       i = mapping.get(key)
       return NONE if i is None else i
   (i_mat, i_ser, i_rej, i_rejd, i_cau, i_ccode, i_cdesc, i_wc, i_wcd,
    i_emp, i_empn, i_hour, i_std, i_mach, i_date, i_time, i_seq, i_last,
    i_unit, i_lab) = (
       col("material"), col("serial_no"), col("reject_op"),
       col("reject_op_desc"), col("cause_op"), col("cause_code"),
       col("cause_desc"), col("wc_code"), col("wc_desc"), col("emp_no"),
       col("emp_name"), col("rw_hour"), col("std_hour"), col("machine_hour"),
       col("rw_date"), col("time"), col("seq"), col("last_x"), col("unit"),
       col("labour_hour"))
   append = out.append
   for n_row, r in enumerate(rows):
       ln = len(r)
       material = clean_str(r[i_mat] if 0 <= i_mat < ln else None)
       if not material:
           no_material += 1
           continue
       d = parse_date(r[i_date] if 0 <= i_date < ln else None,
                      day_first=day_first)
       if d is None:
           bad_date += 1
       t = sap_parse_time(r[i_time] if 0 <= i_time < ln else None) or datetime.time.min
       append({
           "material": material,
           "serial_no": clean_str(r[i_ser] if 0 <= i_ser < ln else None),
           "order_no": "",
           "reject_op": clean_str(r[i_rej] if 0 <= i_rej < ln else None),
           "reject_op_desc": clean_str(r[i_rejd] if 0 <= i_rejd < ln else None),
           "cause_op": clean_str(r[i_cau] if 0 <= i_cau < ln else None),
           "cause_code": clean_str(r[i_ccode] if 0 <= i_ccode < ln else None),
           "cause_desc": clean_str(r[i_cdesc] if 0 <= i_cdesc < ln else None),
           "wc_code": clean_str(r[i_wc] if 0 <= i_wc < ln else None),
           "wc_desc": clean_str(r[i_wcd] if 0 <= i_wcd < ln else None),
           "emp_no": clean_str(r[i_emp] if 0 <= i_emp < ln else None),
           "emp_name": clean_str(r[i_empn] if 0 <= i_empn < ln else None),
           "entered_by": "",
           "rw_hour": parse_float(r[i_hour] if 0 <= i_hour < ln else None),
           "std_hour": parse_float(r[i_std] if 0 <= i_std < ln else None),
           "machine_hour": parse_float(r[i_mach] if 0 <= i_mach < ln else None),
           "rw_date": d,
           "dt": datetime.datetime.combine(d, t) if d else None,
           "seq": clean_str(r[i_seq] if 0 <= i_seq < ln else None),
           "last_x": clean_str(r[i_last] if 0 <= i_last < ln else None),
           "unit": clean_str(r[i_unit] if 0 <= i_unit < ln else None),
           "labour_hour": (parse_float(r[i_lab]) if 0 <= i_lab < ln
                           else None),
       })
       if progress is not None and (n_row & 8191) == 0:
           progress(n_row, len(rows))
   info = {"headers": headers, "mapping": mapping, "notes": notes,
           "repeated_headers": skipped, "bad_date": bad_date,
           "no_material": no_material, "rows_in_file": len(rows)}
   return out, info


def wf_canonical_from_sap(records):
   """
   Builds the waterfall's canonical table from sap_read() records: one row
   per (material, serial, operation).

     - the LARGEST record date of that triple is kept
     - rework hours are summed

   So even when an operation was booked several times, when the part last
   left that operation stays correct.
   """
   canon = {}
   no_serial = no_dt = bad_opac = 0
   for r in records:
       if not r.get("serial_no"):
           no_serial += 1
           continue
       if r.get("dt") is None:
           no_dt += 1
           continue
       try:
           opac = int(float(str(r.get("reject_op")).strip()))
       except (TypeError, ValueError):
           bad_opac += 1
           continue
       key = (norm_text(r["material"]), norm_text(r["serial_no"]), opac)
       cur = canon.get(key)
       if cur is None:
           canon[key] = {"material": r["material"], "serial": r["serial_no"],
                         "opac": opac, "dt": r["dt"],
                         "rework": float(r.get("rw_hour") or 0.0),
                         "std_hour": float(r.get("std_hour") or 0.0),
                         "machine_hour": float(r.get("machine_hour") or 0.0)}
       else:
           if r["dt"] > cur["dt"]:
               cur["dt"] = r["dt"]
           cur["rework"] += float(r.get("rw_hour") or 0.0)
           cur["std_hour"] += float(r.get("std_hour") or 0.0)
           cur["machine_hour"] += float(r.get("machine_hour") or 0.0)
   merged = len(records) - no_serial - no_dt - bad_opac - len(canon)
   issues = {"no_serial": no_serial, "no_date": no_dt, "bad_operation": bad_opac,
             "merged_duplicates": max(0, merged)}
   return list(canon.values()), issues


# ===========================================================================
#  5. DATABASE
# ===========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS comp (
   work_center TEXT PRIMARY KEY, wc_name TEXT, comp TEXT);
CREATE TABLE IF NOT EXISTS parts (
   part_no TEXT PRIMARY KEY, part_key TEXT, part_group TEXT, engineer TEXT);
CREATE TABLE IF NOT EXISTS rw (
   id INTEGER PRIMARY KEY AUTOINCREMENT, row_hash TEXT,
   material TEXT, material_key TEXT, order_no TEXT, serial_no TEXT,
   reject_op TEXT, reject_op_desc TEXT, cause_op TEXT, cause_code TEXT,
   cause_desc TEXT, rw_hour REAL, rw_date TEXT, iso_year INTEGER,
   iso_week INTEGER, wc_code TEXT, wc_desc TEXT, emp_no TEXT, emp_name TEXT,
   entered_by TEXT, imported_at TEXT,
   -- The hours and the full stamp of the booking itself.  The waterfall's
   -- one-row-per-operation table is a sum of these, so keeping them means
   -- it can be rebuilt exactly after any import instead of being patched.
   tot_hour REAL DEFAULT 0, mach_hour REAL DEFAULT 0, entry_dt TEXT,
   seq TEXT DEFAULT '', last_x INTEGER DEFAULT 0);

-- Production steps: rework labour time zero.  Used to tell which machine ran
-- the cause operation of a serial number and who operated it.
CREATE TABLE IF NOT EXISTS ops (
   id INTEGER PRIMARY KEY AUTOINCREMENT, row_hash TEXT,
   material TEXT, serial_no TEXT, serial_key TEXT,
   op_no TEXT, op_key TEXT, op_desc TEXT,
   wc_code TEXT, wc_desc TEXT, emp_no TEXT, emp_name TEXT,
   op_date TEXT, imported_at TEXT,
   tot_hour REAL DEFAULT 0, mach_hour REAL DEFAULT 0, entry_dt TEXT,
   -- Which sequence of the operation this booking belongs to, and
   -- whether SAP marked it the LAST entry for that step (the X in the
   -- "last" column): with sequences, an entry only counts towards the
   -- sequence it names, and the X is what says the part is off it.
   seq TEXT DEFAULT '', last_x INTEGER DEFAULT 0,
   -- 1 when the Unit column (M) said "H": a SETUP booking.  Its X does not
   -- close the step and its hours are not progress on the part (v62).
   setup INTEGER DEFAULT 0,
   -- The plain labour column (P).  NULL on rows imported before it was
   -- read; progress then falls back to Toplam Is (v62).
   lab_hour REAL);
CREATE INDEX IF NOT EXISTS ix_ops_lookup ON ops (serial_key, op_key);
CREATE INDEX IF NOT EXISTS ix_ops_date ON ops (op_date);
-- The MACHINES filter asks "which serials went through this work centre
-- between these dates", on both tables, on every refresh.
CREATE INDEX IF NOT EXISTS ix_ops_wc ON ops (wc_code, op_date);
CREATE INDEX IF NOT EXISTS ix_rw_wc  ON rw  (wc_code, rw_date);
CREATE INDEX IF NOT EXISTS ix_ops_material ON ops (material);
-- B1000 LIVE asks "where was each serial last booked" over the whole ops
-- table on every refresh.  Grouping by serial and taking the newest row
-- needs these two: the serial/date pair so the group-by reads in order
-- instead of sorting half a million rows, and the normalised material
-- because the part filter compares UPPER(REPLACE(...)) and an index on the
-- bare column cannot serve that.
-- Stored newest-first, and carrying id, so that finding each serial's
-- latest booking is a seek to the front of its group rather than a read
-- through all of it: ascending, or without id, the same query takes twice
-- as long on half a million rows.
CREATE INDEX IF NOT EXISTS ix_ops_serial_date
   ON ops (serial_key, op_date DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_ops_matkey ON ops (UPPER(REPLACE(material,' ','')));

-- Rows thrown out at import time, kept so they can be shown to the user.
CREATE TABLE IF NOT EXISTS discarded (
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   material TEXT, serial_no TEXT, reject_op TEXT, cause_op TEXT,
   rw_hour REAL, rw_date TEXT, wc_code TEXT, emp_name TEXT,
   reason TEXT, source TEXT, imported_at TEXT);
CREATE INDEX IF NOT EXISTS ix_rw_week ON rw (iso_year, iso_week);
CREATE INDEX IF NOT EXISTS ix_rw_mat  ON rw (material_key);
CREATE INDEX IF NOT EXISTS ix_rw_hash ON rw (row_hash);
CREATE INDEX IF NOT EXISTS ix_rw_ser  ON rw (serial_no);
-- Serial lookups normalise the number before comparing it, and an index on
-- the bare column cannot serve a query that wraps the column in UPPER and
-- REPLACE - SQLite falls back to reading the whole table, which on a full
-- year of rework is tens of milliseconds for ONE serial.  An index on the
-- same expression is what makes clicking a serial number instant.
CREATE INDEX IF NOT EXISTS ix_rw_serkey ON rw (UPPER(REPLACE(serial_no,' ','')));
CREATE INDEX IF NOT EXISTS ix_rw_date ON rw (rw_date);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Waterfall lead time analysis: one canonical row per
-- (material, serial, operation).
-- std_hour is SAP's Toplam Iscilik (labour) and machine_hour its Activity
-- to conf (time on the machine).  Where a part is now is decided on the
-- MACHINE time: two operators on one machine book twice the labour for one
-- cutting, which used to run a part through its operation twice as fast as
-- it really went.
CREATE TABLE IF NOT EXISTS wf_part_data (
   material TEXT, material_key TEXT, serial TEXT, serial_key TEXT,
   opac INTEGER, dt TEXT, rework REAL, std_hour REAL DEFAULT 0,
   machine_hour REAL DEFAULT 0,
   -- The FIRST entry against this operation, where dt is the LAST one.
   -- Lead time is measured from the first entry on the first operation
   -- to the last entry on the last one, so both ends are needed.
   dt_first TEXT, dt_ops_first TEXT,
   -- The latest entry against this operation that was NOT a rework.  Where
   -- the part IS gets measured from this, never from `dt`: see
   -- op_progress_for_route.  NULL when the operation only ever carried
   -- rework bookings.
   dt_ops TEXT,
   -- The sequence this row is for, and whether any booking against it
   -- carried the LAST-entry X.  The key includes the sequence: 280/1 and
   -- 280/3 are separate pieces of work and their hours must not be added
   -- together.
   seq TEXT DEFAULT '', done_x INTEGER DEFAULT 0,
   -- THE CLOSING RULE (v62).  done_x is "a PRODUCTION booking that is not
   -- a setup carried the X"; x_dt is the latest such booking - the day the
   -- step was finished.  prog is the Toplam Is hours of production
   -- bookings that are not setups: how far the part is through the step.
   x_dt TEXT, prog REAL DEFAULT 0,
   -- 2.2.0: the machine and labour hours of PRODUCTION bookings only
   -- (machine_hour / std_hour above include the rework bookings).  The
   -- lead time page's OPERATION TIME is read from these, so a rework
   -- never counts as operation time.  NULL on a row written by an older
   -- program - the reader then falls back to the old columns.
   mach_ops REAL, lab_ops REAL,
   PRIMARY KEY (material_key, serial_key, opac, seq));
CREATE INDEX IF NOT EXISTS ix_wf_part_serial ON wf_part_data (material_key, serial_key);

-- Traveler route: one row per part per column position, in process order.
-- active=0 is an operation the user has unticked in EDIT TRAVELER - kept
-- on file (not deleted) so it can be switched back on, but left out of
-- every route every analysis page reads.
-- The route, plus what each step needs.  std_hour is the standard time
-- for the operation and wc_group is the family of machines allowed to do
-- it - the group name matches COMP's own class column, which is what ties
-- "operation 80 needs a LIECHTI" to the actual work centre numbers.
CREATE TABLE IF NOT EXISTS wf_traveler (
   part TEXT, part_key TEXT, position INTEGER,
   op INTEGER, pred INTEGER, descr TEXT, active INTEGER DEFAULT 1,
   std_hour REAL DEFAULT 0, wc_group TEXT DEFAULT '',
   -- SEQUENCE NUMBER.  One operation can be written several times with a
   -- sequence after it - 280/1, 280/3, 280/4 - and each of those is its
   -- own piece of work with its own standard time.  Empty for an
   -- ordinary operation.  See step_key().
   seq TEXT DEFAULT '',
   PRIMARY KEY (part_key, position));
CREATE INDEX IF NOT EXISTS ix_wf_traveler_part ON wf_traveler (part_key);

-- Shipment / sales report: one row per serial per delivery.  A serial with
-- a row here (any date) has actually left the plant - that is what "sold"
-- means everywhere else in this file.
CREATE TABLE IF NOT EXISTS shipment (
   id INTEGER PRIMARY KEY AUTOINCREMENT, row_hash TEXT UNIQUE,
   material TEXT, material_key TEXT, serial_no TEXT, serial_key TEXT,
   qty REAL, irsaliye TEXT, ac_gi_date TEXT, po_no TEXT, poitem TEXT,
   sales_doc TEXT, ship_to TEXT, sorg TEXT, created_on TEXT, imported_at TEXT);
CREATE INDEX IF NOT EXISTS ix_shipment_serial ON shipment (serial_key);
CREATE INDEX IF NOT EXISTS ix_shipment_material ON shipment (material_key);

-- MRB / SCRAP / TECO serial flags.  One active row per serial: TECO is the
-- "at risk" state (with an MRB and a SCRAP probability); when a TECO case
-- is later resolved the same row is either deleted (fixed) or its kind is
-- switched to 'mrb' / 'scrap' - the serial's history lives in one place
-- instead of being split across three tables.
CREATE TABLE IF NOT EXISTS serial_flags (
   serial_key TEXT PRIMARY KEY, serial_no TEXT, kind TEXT,
   reason TEXT, entry_date TEXT, mrb_prob TEXT, scrap_prob TEXT,
   entered_by TEXT, created_at TEXT, updated_at TEXT,
   -- A case that reached "fixed" keeps its row instead of being deleted:
   -- what it WAS is the whole point of the question "how many MRB parts
   -- did we actually put right this year".
   resolved_from TEXT, resolved_at TEXT, resolved_by TEXT,
   -- A part number typed by hand (v67), for an old serial the data no
   -- longer knows - used only when the data cannot find the part.
   part_no TEXT);
CREATE INDEX IF NOT EXISTS ix_serial_flags_kind ON serial_flags (kind);

-- Accounts.  They live in the shared database, not in each machine's own
-- settings, so one person's password works from any computer that opens
-- the same folder and an account only has to be defined once.
-- pw_hash is empty until the person's first sign-in, which is what forces
-- them to choose their own password before they can get in.
CREATE TABLE IF NOT EXISTS app_user (
   username TEXT PRIMARY KEY, full_name TEXT, role TEXT,
   pw_hash TEXT, pw_salt TEXT, active INTEGER DEFAULT 1,
   created_at TEXT, last_login TEXT);

-- Private per-user notes on a serial.  Deliberately separate from
-- serial_flags: a flag is shop-floor state everybody must see, while one
-- of these is one person's own note on a part they are following, and
-- nobody else's account shows it.  (An admin can switch on a view of all
-- of them - see notes_for_serial.)
CREATE TABLE IF NOT EXISTS user_note (
   serial_key TEXT, username TEXT, note TEXT, color TEXT,
   private INTEGER DEFAULT 0,
   created_at TEXT, updated_at TEXT,
   PRIMARY KEY (serial_key, username));
CREATE INDEX IF NOT EXISTS ix_user_note_user ON user_note (username);

-- Who has the program open right now.  Every running copy writes a row and
-- keeps a heartbeat on it; a row whose heartbeat has gone stale is simply a
-- copy that was closed or crashed.  An admin uses this to see who is still
-- in before publishing a new build, and to ask those copies to close.
CREATE TABLE IF NOT EXISTS app_session (
   session_id TEXT PRIMARY KEY, username TEXT, host TEXT, pid INTEGER,
   started_at TEXT, last_seen TEXT, close_requested TEXT, closed_at TEXT);
CREATE INDEX IF NOT EXISTS ix_app_session_seen ON app_session (last_seen);

-- The B1000 floor plan.  Saving makes a new version rather than
-- overwriting, so a layout can be compared with how it looked before and
-- a bad edit is never the end of it.  One version is marked active and
-- that is what everybody opens.
CREATE TABLE IF NOT EXISTS layout (
   id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, canvas_w INTEGER,
   canvas_h INTEGER, version INTEGER, active INTEGER DEFAULT 0,
   saved_by TEXT, saved_at TEXT, note TEXT);
-- A waiting zone says what it holds either by listing operation numbers
-- (`ops`) or, more usefully, by naming machine groups (`groups`) - "this is
-- the HNK and MAKINO waiting area".  The group names are COMP's own class
-- names, so the zone follows the machines instead of having to be re-typed
-- every time an operation moves to a different cell.
CREATE TABLE IF NOT EXISTS layout_item (
   id INTEGER PRIMARY KEY AUTOINCREMENT, layout_id INTEGER, kind TEXT,
   asset TEXT, label TEXT, wc_code TEXT, ops TEXT, groups TEXT DEFAULT '',
   x REAL, y REAL, w REAL, h REAL, rot REAL DEFAULT 0,
   color TEXT, z INTEGER DEFAULT 0,
   -- A zone can also be narrowed to one or more PART GROUPS: "the HNK
   -- waiting area, but only for disks".  Empty means every part.
   part_groups TEXT DEFAULT '',
   -- A zone takes what stands in it when it moves (carry), and can be
   -- locked once its bay is laid out so a stray drag cannot undo it.
   carry INTEGER DEFAULT 1, locked INTEGER DEFAULT 0,
   -- HOW MANY PEOPLE are on this station.  A machine is one machine and
   -- its capacity is the shift; a bench like MASK / DEMASK is a machine
   -- on paper but its capacity is however many people are put on it, so
   -- two people there is two shifts of capacity.  0 means "an ordinary
   -- machine", which is one.
   people INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_layout_item ON layout_item (layout_id);
"""

# The waterfall's derived table, on its own, because a database made
# before sequences existed has to DROP and recreate it: the primary key
# gained a column and no ALTER can do that.
WF_PART_DATA_SQL = """
CREATE TABLE IF NOT EXISTS wf_part_data (
   material TEXT, material_key TEXT, serial TEXT, serial_key TEXT,
   opac INTEGER, dt TEXT, rework REAL, std_hour REAL DEFAULT 0,
   machine_hour REAL DEFAULT 0,
   -- The FIRST entry against this operation, where dt is the LAST one.
   -- Lead time is measured from the first entry on the first operation
   -- to the last entry on the last one, so both ends are needed.
   dt_first TEXT, dt_ops_first TEXT,
   -- The latest entry against this operation that was NOT a rework.  Where
   -- the part IS gets measured from this, never from `dt`: see
   -- op_progress_for_route.  NULL when the operation only ever carried
   -- rework bookings.
   dt_ops TEXT,
   -- The sequence this row is for, and whether any booking against it
   -- carried the LAST-entry X.  The key includes the sequence: 280/1 and
   -- 280/3 are separate pieces of work and their hours must not be added
   -- together.
   seq TEXT DEFAULT '', done_x INTEGER DEFAULT 0,
   -- THE CLOSING RULE (v62).  done_x is "a PRODUCTION booking that is not
   -- a setup carried the X"; x_dt is the latest such booking - the day the
   -- step was finished.  prog is the Toplam Is hours of production
   -- bookings that are not setups: how far the part is through the step.
   x_dt TEXT, prog REAL DEFAULT 0,
   -- 2.2.0: the machine and labour hours of PRODUCTION bookings only
   -- (machine_hour / std_hour above include the rework bookings).  The
   -- lead time page's OPERATION TIME is read from these, so a rework
   -- never counts as operation time.  NULL on a row written by an older
   -- program - the reader then falls back to the old columns.
   mach_ops REAL, lab_ops REAL,
   PRIMARY KEY (material_key, serial_key, opac, seq));
CREATE INDEX IF NOT EXISTS ix_wf_part_serial ON wf_part_data (material_key, serial_key);
"""


# A session whose heartbeat is older than this is treated as gone.
SESSION_STALE_SECONDS = 150
# HOW LONG A DEAD COPY'S ROW IS STILL WORTH SHOWING.
#
# A stale row is kept ON PURPOSE for a while: an admin who has just asked
# a copy to close needs to see that it stopped answering, and a copy that
# crashed a minute ago is still news.  A copy that died two days ago is
# not - it only makes the list longer and makes the admin wonder whether
# those people are still in.  Past this, the row is dropped from the
# screen and deleted from the file.
SESSION_FORGET_SECONDS = 3600

# Bumped whenever SCHEMA or Database._build_schema() changes, so a database
# built by an older version gets brought up to date once and every open
# after that skips the setup entirely.
SCHEMA_VERSION = 27

# The people who get an account the first time the app opens a database.
# Roles: admin sees and changes everything; sef / manager / leader / user
# are ordinary accounts today and are the hook for per-role permissions
# later.  Everyone picks their own password on first sign-in.
#
# An account is a username, a role and a password - nothing else.  Employee
# numbers were held here once and are not: they are personal data the app
# has no use for, so they are neither asked for nor stored.
SEED_USERS = [
   ("caglaru",  "Çağlar Ünal",             "user"),
   ("efey",     "Efe Yaltırıklı",          "admin"),
   ("fatihk",   "Fatih Köroğlu",           "user"),
   ("hamzat",   "Hamza Turhan",            "user"),
   ("huseyink", "Hüseyin Kurt",            "user"),
   ("melihe",   "Melih Esendemir",         "leader"),
   ("muratk",   "Murat Karamahmutoğlu",    "gmy"),
   ("enesk",    "Muhammed Enes Karakoyun", "user"),
   ("omero",    "Ömer Özer",               "user"),
   ("umutc",    "Umut Çengel",             "user"),
   ("yasars",   "Yaşar Sır",               "manager"),
   ("sef",      "Şef",                     "sef"),
]

# The colours a private note can be marked in.  Deliberately clear of the
# MRB/SCRAP/TECO palette, so a serial someone has marked for themselves is
# never mistaken for shop-floor state everybody is looking at.
USER_NOTE_COLORS = [
   ("BLUE",   "#1F4E9C"),
   ("TEAL",   "#0F7B7B"),
   ("GREEN",  "#2E7D32"),
   ("ORANGE", "#C2620F"),
   ("PINK",   "#B03060"),
   ("GREY",   "#4A5560"),
]

ROLE_TITLE = {"admin": "ADMIN", "coadmin": "CO-ADMIN", "gmy": "GMY", "sef": "ŞEF",
             "manager": "MANAGER", "leader": "LEADER", "user": "USER"}
# 2.2.0: GMY = Genel Müdür Yardımcısı.  Sees everybody's parts like a
# manager; not an admin.
# THE ONE ADMIN (v67).  Only this account is ADMIN and nobody - not even a
# co-admin - can change its role, password or on/off state.  A CO-ADMIN may
# do everything else an admin does, including changing other people's
# roles and their own; they cannot make anybody ADMIN.
OWNER_USERNAME = "efey"
ADMIN_ROLES = ("admin", "coadmin")

# v72: the roles that look after everybody's parts start every page on
# ALL engineers; anybody else starts on their own parts, when the PART
# list (engineer column) names them.
OVERVIEW_ROLES = ("gmy", "manager", "leader", "sef")
_TR_FOLD = str.maketrans("ÇĞİIÖŞÜçğıiöşü", "CGIIOSUCGIIOSU")


def _name_tokens(text):
    t = (text or "").translate(_TR_FOLD).upper()
    return [w for w in re.split(r"[^A-Z0-9]+", t) if w]


def engineer_for_user(user, engineers):
    """The engineer name in the PART list that is this account, or None.

    The list is typed by hand, so the same person may appear as "Efe
    Yaltırıklı", "EFE YALTIRIKLI", "E. YALTIRIKLI" or as the username;
    Turkish letters and dots do not matter.  A manager, leader or şef
    gets None (= ALL) whatever the list says."""
    if not user or (user.get("role") or "") in OVERVIEW_ROLES:
        return None
    full = _name_tokens(user.get("full_name"))
    uname = _name_tokens(user.get("username"))
    for eng in engineers or ():
        e = _name_tokens(eng)
        if not e:
            continue
        if e == full or (uname and e == uname):
            return eng
        # initial(s) + surname: "E. YALTIRIKLI", "M. E. KARAKOYUN"
        if (len(full) >= 2 and e[-1] == full[-1] and len(e) >= 2
                and all(len(w) == 1 for w in e[:-1])
                and e[0] == full[0][0]):
            return eng
    return None


PW_ROUNDS = 120000


def process_started_at():
   """When THIS process actually started, not when Python got here.

   A PyInstaller build spends its first seconds before a single line of
   this program runs: unpacking its bundle, loading a hundred DLLs, and
   being scanned by the virus checker while it does.  Timing from inside
   Python cannot see any of that, which is why "the program takes forty
   seconds" and "the code takes one second" were both true and neither
   was useful.

   Windows can tell us the real creation time.  Everywhere else this
   returns None and the start-up report simply leaves that line out.
   """
   if os.name != "nt":
       return None
   try:
       import ctypes
       from ctypes import wintypes

       class FILETIME(ctypes.Structure):
           _fields_ = [("dwLowDateTime", wintypes.DWORD),
                       ("dwHighDateTime", wintypes.DWORD)]

       created = FILETIME()
       other = (FILETIME(), FILETIME(), FILETIME())
       ok = ctypes.windll.kernel32.GetProcessTimes(
           ctypes.windll.kernel32.GetCurrentProcess(),
           ctypes.byref(created), ctypes.byref(other[0]),
           ctypes.byref(other[1]), ctypes.byref(other[2]))
       if not ok:
           return None
       # FILETIME counts 100-nanosecond intervals since 1601-01-01 UTC.
       ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
       epoch = datetime.datetime(1601, 1, 1)
       return epoch + datetime.timedelta(microseconds=ticks / 10.0)
   except Exception:
       return None


def program_folder():
   """Where the program itself is running from."""
   try:
       if getattr(sys, "frozen", False):
           return os.path.dirname(os.path.abspath(sys.executable))
       return os.path.dirname(os.path.abspath(__file__))
   except Exception:
       return ""


def is_network_path(path):
   """True when the file lives on a network share rather than a local disk.

   This decides the journal mode, and getting it wrong is expensive:
   SQLite's WAL needs a memory-mapped shared file (-shm) that every
   process attached to the database can see coherently, and SMB/NFS do
   not provide that.  On a share it produces exactly the symptoms it is
   supposed to prevent - stalls once a second person opens the file, and
   a close that hangs because the last connection cannot checkpoint.
   """
   try:
       p = os.path.abspath(path)
   except Exception:
       return False
   if p.startswith("\\\\") or p.startswith("//"):
       return True                      # UNC path
   if os.name == "nt":
       try:
           import ctypes
           drive = os.path.splitdrive(p)[0]
           if drive:
               DRIVE_REMOTE = 4
               return ctypes.windll.kernel32.GetDriveTypeW(
                   drive + "\\") == DRIVE_REMOTE
       except Exception:
           return False
       return False
   # macOS/Linux: a mounted share shows up under /Volumes or /mnt.  Only
   # used for testing here - the shop floor runs Windows.
   return p.startswith("/Volumes/") or p.startswith("/mnt/")


def uname(username):
   """Usernames are matched case-insensitively and stored lower case.
   (norm_text() is the wrong tool here - it upper-cases and strips for
   part/serial matching.)"""
   return (username or "").strip().lower()


def hash_password(password, salt):
   """PBKDF2-SHA256.  The database sits in a folder everyone can open, so
   what is stored has to be useless to whoever reads the file - a salted,
   slow hash rather than the password itself."""
   return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                              (salt or "").encode("utf-8"),
                              PW_ROUNDS).hex()

# The duplicate rule, in order.  Two rows are the same row only when column 1
# matches, and column 2, and column 3 ... all the way to the end of this list.
# The moment any one column differs the row is a new row and gets loaded.
# Comparing part / serial / operation alone would treat two genuinely
# different rework entries as one and silently drop the second.
#
# The comparison is done by hashing the columns in this order rather than by
# looping over them per candidate pair: on a 350,000 row file that is the
# difference between seconds and hours, and the outcome is identical.
FINGERPRINT_FIELDS = ["material", "serial_no", "reject_op", "rw_hour", "rw_date",
                     "cause_op", "wc_code", "reject_op_desc", "cause_code",
                     "cause_desc", "wc_desc", "emp_no", "emp_name", "order_no",
                     "entered_by"]

DUPLICATE_RULE_TEXT = ("duplicate rule: a row is skipped only when every one of "
                      "these matches, in order - " + ", ".join(FINGERPRINT_FIELDS))


def row_fingerprint(rec):
   parts = []
   for k in FINGERPRINT_FIELDS:
       if k == "rw_hour":
           parts.append("%.4f" % parse_float(rec.get("rw_hour")))
       elif k == "rw_date":
           parts.append(str(rec.get("_date") or ""))
       else:
           parts.append(norm_text(rec.get(k)))
   return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()



def flag_kind_in(kind, kinds):
    """Is a serial flagged `kind` covered by the filter `kinds`?

    MRB ENTERED (v67) is an MRB whose entry has been made.  It is counted
    and filtered SEPARATELY from MRB (MRB = entry not made yet, so what is
    still left), with its own button and colour.  Kept as the one place
    this rule lives, should it change."""
    return kind in kinds

# ---------------------------------------------------------------------------
#  THE LOCAL COPY  (2.2.0)
# ---------------------------------------------------------------------------
# The database sits in a shared folder, and every page read its big tables
# from there: a page of the file per network round trip, hundreds of
# thousands of them.  On the desktop the same program is quick.
#
# So the data is brought to this computer ONCE.  The whole file is copied
# into "WIP TRACKER LOCAL COPY" on the Desktop (one sequential read of the
# share - the fastest thing a share can do; see mirror_default_dir), and from then on every SELECT that only
# touches the big DATA tables is answered from the copy:
#
#     rw, ops, wf_part_data, shipment, discarded, parts, comp, wf_traveler
#
# These change only through an import, a deletion or a traveler / part
# list edit, and every one of those moves data_version.  The copy carries
# the data_version it was made at; when the shared one differs the copy is
# switched off at once (reads go to the share again, exactly as before)
# and a new one is made in the background.  Everything else - settings,
# TECO / MRB records, notes, accounts, layouts, sessions - is small, can
# change at any moment, and is ALWAYS read from the share.  Every write
# goes to the share.
#
# A copy is never changed after it is made (a new one is a new file), so
# it is opened immutable: no locks, no journal, and nobody reading it can
# hold up somebody saving to the share.
#
# MAKING IT SAFELY.  A plain file copy while another program commits could
# catch half a write.  So: the file's change counter is read, the journal
# is checked to be idle, the file is copied, the journal and the counter
# are checked again - any difference and it is copied again.  If the file
# will not hold still, SQLite's own backup takes a consistent snapshot
# instead.  The copy is then checked (quick_check) before it is trusted.
MIRROR_TABLES = frozenset(("rw", "ops", "wf_part_data", "shipment", "discarded",
                           "parts", "comp", "wf_traveler"))
# Names that send a statement to the share whatever else is in it.
MIRROR_NEVER = frozenset(("meta", "serial_flags", "user_note", "app_user", "layout",
                          "layout_item", "agg_cache", "app_session", "sqlite_master",
                          "sqlite_sequence", "sqlite_temp_master", "sess", "temp",
                          "main", "pragma_table_info"))
MIRROR_FOLDER = "WIP TRACKER LOCAL COPY"
MIRROR_OLD_DIR = os.path.join(APP_DIR, "local_copy")     # where 2.3.0 first put it
MIRROR_NOTE = "BU KLASOR SILINEBILIR - READ ME.txt"
MIRROR_NOTE_TEXT = (
    "WIP TRACKER - YEREL KOPYA / LOCAL COPY\r\n"
    "======================================\r\n\r\n"
    "Bu klasordeki .db dosyasi, ortak alandaki veritabaninin bu bilgisayardaki\r\n"
    "KOPYASIDIR.  Program hizli calissin diye buyuk tablolari buradan okur.\r\n\r\n"
    "- Bu klasoru SILEBILIRSINIZ (program kapaliyken).  Hicbir veri kaybolmaz:\r\n"
    "  asil veri ortak alandadir.  Program bir sonraki acilista kopyayi yeniden\r\n"
    "  olusturur (acilis o sefer biraz uzun surer).\r\n"
    "- Buradaki dosyayi degistirmeyin, baska yere tasimayin.\r\n"
    "- Icinde uretim verisi vardir: klasoru baskasiyla paylasmayin.\r\n\r\n"
    "This folder holds a copy of the shared database so the program reads it\r\n"
    "from this computer.  It can be deleted while the program is closed; it is\r\n"
    "made again the next time the program starts.\r\n")


def desktop_dir():
   """This account's Desktop as Windows has it (it may have been moved),
   or ~/Desktop elsewhere.  None when there is none."""
   p = None
   if os.name == "nt":
      try:
         import ctypes
         buf = ctypes.create_unicode_buffer(520)
         # CSIDL_DESKTOPDIRECTORY = 0x10, current path
         if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:
            p = buf.value
      except Exception:
         p = None
   if not p:
      p = os.path.join(os.path.expanduser("~"), "Desktop")
   return p if os.path.isdir(p) else None


def mirror_default_dir():
   """Where the local copy is kept: a folder ON THE DESKTOP, in plain
   sight, so whoever it bothers can see what it is and delete it.

   A Desktop that is synced to OneDrive is the exception: every new copy
   (hundreds of MB, after every upload) would be sent to the cloud, and
   the production data with it.  The folder then goes into the user's own
   folder instead (C:\\Users\\name) - still visible, not synced."""
   env = os.environ.get("WIP_LOCAL_COPY_DIR")
   if env:
      return env
   desk = desktop_dir()
   if desk and "onedrive" not in desk.lower():
      return os.path.join(desk, MIRROR_FOLDER)
   return os.path.join(os.path.expanduser("~"), MIRROR_FOLDER)


MIRROR_DIR = mirror_default_dir()
_MIRROR_ROUTE = {}
_MIRROR_FROM = re.compile(r"\b(?:from|join)\s+([a-z_][a-z0-9_.]*)")
_MIRROR_WORD = re.compile(r"[a-z_][a-z0-9_]*")


def mirror_routable(sql):
   """May this statement be answered from the local copy?  Only a SELECT
   whose every table is a mirrored one.  Anything not understood goes to
   the share - being slow is the worst that can happen there."""
   hit = _MIRROR_ROUTE.get(sql)
   if hit is not None:
      return hit
   low = sql.lstrip().lower()
   ok = False
   if low.startswith("select"):
      tabs = _MIRROR_FROM.findall(low)
      if tabs and all(t in MIRROR_TABLES for t in tabs):
         ok = not (set(_MIRROR_WORD.findall(low)) & MIRROR_NEVER)
   if len(_MIRROR_ROUTE) > 4000:
      _MIRROR_ROUTE.clear()
   _MIRROR_ROUTE[sql] = ok
   return ok


class MirrorConn:
   """The Database's connection: the share's own connection, with the
   reads the local copy can answer sent there instead.  Everything else
   (writes, commits, pragmas, the small tables) is the share's connection
   untouched."""
   __slots__ = ("_real", "_state", "_local", "_gen", "_db")

   def __init__(self, real, db):
      self._real = real
      self._db = db
      self._state = db.__dict__.setdefault("_mirror", mirror_state())
      self._local = None
      self._gen = -1

   def _local_conn(self, st):
      if self._gen != st["gen"]:
         self._drop_local()
         self._gen = st["gen"]
         try:
            import pathlib
            uri = pathlib.Path(st["path"]).as_uri() + "?mode=ro&immutable=1"
            c = sqlite3.connect(uri, uri=True, timeout=5)
            c.row_factory = self._real.row_factory
            for pragma in ("PRAGMA cache_size = -262144", "PRAGMA temp_store = MEMORY",
                           "PRAGMA mmap_size = 1073741824"):
               try:
                  c.execute(pragma)
               except sqlite3.Error:
                  pass
            try:
               c.set_progress_handler(Database._progress_tick, 200000)
            except Exception:
               pass
            self._local = c
         except Exception as e:
            st["ok"] = False
            st["error"] = "could not open the local copy: %s" % e
            self._local = None
      return self._local

   def _drop_local(self):
      if self._local is not None:
         try:
            self._local.close()
         except Exception:
            pass
         self._local = None

   def execute(self, sql, params=()):
      st = self._state
      if st["ok"] and not self._real.in_transaction and mirror_routable(sql):
         # The shared data version, as this copy last read it, is not the
         # local copy's: somebody changed the data.  This read goes to the
         # share; mirror_check makes the new copy.
         # (st["live"] = the shared data version as last read, see get_meta)
         if st["live"] is None or st["live"] == st["raw"]:
            loc = self._local_conn(st)
            if loc is not None:
               try:
                  cur = loc.execute(sql, params)
                  st["hits"] += 1
                  return cur
               except sqlite3.Error as e:
                  # never let the copy be the reason something fails
                  st["ok"] = False
                  st["error"] = "local copy: %s" % e
      return self._real.execute(sql, params)

   def close(self):
      self._drop_local()
      return self._real.close()

   @property
   def row_factory(self):
      return self._real.row_factory

   @row_factory.setter
   def row_factory(self, v):
      self._real.row_factory = v
      if self._local is not None:
         self._local.row_factory = v

   def __getattr__(self, name):
      return getattr(self._real, name)


def mirror_state():
   return {"enabled": False, "ok": False, "path": None, "raw": None, "live": None, "gen": 0,
           "busy": False, "pct": 0, "msg": "", "error": "", "wgen": 0, "hits": 0,
           "sync": False, "thread": None, "secs": None, "how": ""}


def _mirror_counter(path):
   """The file change counter in the database header (bytes 24-27): moves
   on every committed write."""
   with open(path, "rb") as f:
      f.seek(24)
      return f.read(4)


def _mirror_journal_idle(path):
   """No write is in flight: the rollback journal is absent or empty.
   READ rather than asked for its size - a share may answer a size from
   its directory cache."""
   try:
      with open(path + "-journal", "rb") as f:
         return not f.read(1)
   except FileNotFoundError:
      return True
   except OSError:
      return False


def _mirror_stamp(conn):
   """What identifies the data a copy holds: the data version, and the
   size marks of the big tables (a restored backup can carry an old
   version number)."""
   out = []
   for sql in ("SELECT value FROM meta WHERE key='data_version'",
               "SELECT value FROM meta WHERE key='last_upload_at'",
               "SELECT MAX(id) FROM rw", "SELECT MAX(id) FROM ops",
               "SELECT MAX(id) FROM shipment", "SELECT MAX(rowid) FROM wf_part_data",
               "SELECT COUNT(*) FROM wf_traveler", "SELECT COUNT(*) FROM parts",
               "SELECT COUNT(*) FROM comp"):
      try:
         r = conn.execute(sql).fetchone()
         out.append(r[0] if r else None)
      except sqlite3.Error:
         out.append("?")
   return out


def _mirror_copy_file(src, dst, st):
   size = max(1, os.path.getsize(src))
   done = 0
   with open(src, "rb") as fi, open(dst, "wb") as fo:
      while True:
         buf = fi.read(8 * 1024 * 1024)
         if not buf:
            break
         fo.write(buf)
         done += len(buf)
         st["pct"] = min(99, int(done * 100 / size))
   return done


def mirror_make(shared_path, dst, st, wal=False):
   """A consistent copy of the shared file at `dst`.  Returns how it was
   made ("copy" / "backup"); raises when it could not be."""
   if os.path.exists(dst):
      os.remove(dst)
   if not wal:
      for _try in range(2):
         # wait a moment for a write in flight to finish
         for _w in range(40):
            if _mirror_journal_idle(shared_path):
               break
            time.sleep(0.15)
         try:
            c1 = _mirror_counter(shared_path)
            if not _mirror_journal_idle(shared_path):
               continue
            n = _mirror_copy_file(shared_path, dst, st)
            idle = _mirror_journal_idle(shared_path)
            c2 = _mirror_counter(shared_path)
            if idle and c1 == c2 and n == os.path.getsize(shared_path):
               return "copy"
         except OSError:
            pass
         st["msg"] = "the shared file changed while it was copied - again"
   if os.path.exists(dst):
      os.remove(dst)
   if wal:
      # a local WAL file: SQLite's own backup, one step - a snapshot
      src = sqlite3.connect(shared_path, timeout=30)
      out = sqlite3.connect(dst)
      try:
         with out:
            src.backup(out, pages=-1)
      finally:
         out.close()
         src.close()
      return "backup"
   # THE FILE WOULD NOT HOLD STILL: copy it under a read lock.  While a
   # reader holds the file no writer can change a single page of it (it
   # waits, as it does for any long read) - so the copy is whole.  A hot
   # journal is rolled back by SQLite before the lock is given.
   src = sqlite3.connect(shared_path, timeout=60, isolation_level=None)
   try:
      src.execute("BEGIN")
      src.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
      _mirror_copy_file(shared_path, dst, st)
      src.execute("ROLLBACK")
   finally:
      src.close()
   return "locked copy"


def _mirror_cleanup(base, keep=()):
   """Old copies of this database are removed (one still open somewhere
   simply stays until next time)."""
   keep = {os.path.abspath(k) for k in keep if k}
   try:
      names = [n for n in os.listdir(MIRROR_DIR) if n.startswith(base)]
   except OSError:
      return
   # ONE copy is kept (2.3.0, second build): the one in use.  A file some
   # connection still has open cannot be removed on Windows - it goes the
   # next time.
   for n in sorted(names):
      p = os.path.abspath(os.path.join(MIRROR_DIR, n))
      if p in keep:
         continue
      try:
         os.remove(p)
      except OSError:
         pass


def mirror_housekeeping():
   """The note that says what the folder is, and the folder the first
   2.3.0 build used (hidden in ~/.rw_analysis) emptied."""
   try:
      note = os.path.join(MIRROR_DIR, MIRROR_NOTE)
      if not os.path.exists(note):
         with open(note, "w", encoding="utf-8", newline="") as f:
            f.write(MIRROR_NOTE_TEXT)
   except OSError:
      pass
   try:
      if (os.path.isdir(MIRROR_OLD_DIR)
            and os.path.abspath(MIRROR_OLD_DIR) != os.path.abspath(MIRROR_DIR)):
         import shutil
         shutil.rmtree(MIRROR_OLD_DIR, ignore_errors=True)
   except OSError:
      pass


def mirror_verify(path):
   """(raw data version, stamp) of a finished copy; raises when it is not
   a sound database."""
   c = sqlite3.connect(path)
   try:
      c.execute("PRAGMA journal_mode = OFF")
      r = c.execute("PRAGMA quick_check(1)").fetchone()
      if not r or str(r[0]).lower() != "ok":
         raise sqlite3.DatabaseError("quick_check: %s" % (r[0] if r else "?"))
      st = _mirror_stamp(c)
      return st[0], st
   finally:
      c.close()


class Database:
   """
   All shared content lives here.  The file may sit on a network drive so
   that several people work on the same data: writes are short, a busy
   timeout covers the moment another user is committing, and a version
   counter lets every running copy notice that somebody else changed
   something.
   """

   def __init__(self, path=DB_PATH):
       self.path = path
       self._meta_cache = None       # see the meta section below
       self._read_cache = {}         # see _cached_rows below
       # Set by the UI to a callback(done, total) so a long import can
       # keep the busy spinner turning; None everywhere else.
       self.progress = None
       folder = os.path.dirname(path) or "."
       if not os.path.isdir(folder):
           os.makedirs(folder, exist_ok=True)
       self.conn = sqlite3.connect(path, timeout=20)
       self.conn.row_factory = sqlite3.Row
       # 2.2.0: reads of the big data tables may be answered from a copy
       # on this computer - see THE LOCAL COPY above
       self._mirror = mirror_state()
       self.conn = MirrorConn(self.conn, self)
       # THE ONE PLACE THE READ CACHE IS THROWN AWAY.
       #
       # A few small lookups are answered from memory so that a database
       # in a shared folder is not asked the same question forty times
       # (see _cached_rows).  The danger of any such cache is the day
       # somebody adds a write that forgets to clear it, and the program
       # then shows data that is quietly out of date.
       #
       # So nothing has to remember: SQLite reports every statement it
       # runs, and anything that is not a plain read empties the cache.
       # One cheap string check per statement, and no way to add a write
       # that slips past it.
       self.conn.set_trace_callback(self._on_statement)
       # Deliberately SHORT, and not the whole patience: SQLite waits for a
       # lock inside the C call where nothing can move the spinner, so a
       # long timeout here is an invisible freeze.  The real waiting is
       # done by with_lock_retry() around the read, which ticks between
       # attempts.  (There used to be a second, contradictory PRAGMA
       # further down that quietly overrode a 20 second setting with this
       # one - there is now just the one.)
       self.conn.execute("PRAGMA busy_timeout = 1500")
       # These are all PERFORMANCE settings, and not one of them is worth
       # failing to open the program over.  Several of them touch the file
       # header, so while somebody else is mid-write they raise "database
       # is locked" - and that used to come out as a traceback box on
       # start-up for a user who only wanted to look at the data.  They are
       # applied best-effort: a locked file simply runs with SQLite's
       # defaults this session.
       #
       #   synchronous NORMAL  a commit still survives the program
       #                       crashing, only a power cut can lose the
       #                       last one, and it is several times faster
       #                       than FULL on a network drive
       #   cache_size          a page read from a shared folder costs a
       #                       network round trip and the same pages are
       #                       read over and over as the user moves
       #                       between tabs; a cache big enough for the
       #                       working set reads the file across the
       #                       network about once a session.  A ceiling,
       #                       not an allocation
       #   mmap_size           read in bigger gulps; on SMB the
       #                       per-request overhead dwarfs the bytes moved
       # WHETHER THIS FILE IS ON A SHARE decides more than the journal
       # mode, so it is worked out before the pragmas rather than after.
       self.on_network = is_network_path(path)
       pragmas = ["PRAGMA synchronous = NORMAL",
                  "PRAGMA cache_size = -262144",          # up to 256 MB
                  "PRAGMA temp_store = MEMORY"]
       if not self.on_network:
           # MEMORY MAPPING IS FOR A LOCAL DISK ONLY.
           #
           # Reading the file through a mapping is a real win locally.
           # Over SMB it is not safe: the mapping is not kept coherent
           # between machines, so one copy can go on reading pages that
           # another machine has already overwritten - which looks exactly
           # like "I cannot see what the others are doing".  SQLite's own
           # documentation says not to use it on a network filesystem.
           pragmas.append("PRAGMA mmap_size = 268435456")
       for pragma in pragmas:
           try:
               self.conn.execute(pragma)
           except sqlite3.Error:
               pass
       # SQLite calls this every so often while it is grinding through a
       # long query, which is the only moment anything can move the
       # spinner - the interface is otherwise stuck inside execute().
       try:
           self.conn.set_progress_handler(self._progress_tick, 200000)
       except Exception:
           pass

       # Several people open the same file at once.  WAL is the right
       # journal for that on a local disk - readers carry straight on
       # through somebody else's write instead of waiting for it.
       #
       # On a network share it is the wrong answer and actively harmful:
       # WAL needs a memory-mapped -shm file shared coherently between
       # processes, which SMB cannot provide.  What you get instead is
       # stalls as soon as a second person opens the file, and a close
       # that hangs while the last connection tries to checkpoint.  So the
       # share gets TRUNCATE - the ordinary rollback journal, but reusing
       # one journal file rather than deleting and recreating it for every
       # transaction, which over SMB is the slow part.
       self.journal_mode = "delete"
       try:
           want = "TRUNCATE" if self.on_network else "WAL"
           # ASK FIRST, SET ONLY IF IT HAS TO CHANGE.
           #
           # Setting the journal mode needs the file to ITSELF - an
           # exclusive lock.  Doing it unconditionally meant every copy
           # took that lock every time it started, so five people opening
           # the program at nine in the morning queued behind one another
           # for a setting that was already correct.  Reading the mode
           # takes no lock at all, and on a file that has been opened
           # before the answer is already the one we want, so nobody waits.
           row = self.conn.execute("PRAGMA journal_mode").fetchone()
           self.journal_mode = (row[0] if row else "delete").lower()
           if self.journal_mode != want.lower():
               # It really does need changing.  Worth a short wait, but
               # not worth failing over: whatever journal the file already
               # has works, it is just slower.
               row = with_lock_retry(
                   lambda: self.conn.execute(
                       "PRAGMA journal_mode = %s" % want).fetchone(),
                   seconds=10.0)
               self.journal_mode = (row[0] if row else "delete").lower()
       except sqlite3.Error:
           try:
               row = self.conn.execute("PRAGMA journal_mode").fetchone()
               self.journal_mode = (row[0] if row else "delete").lower()
           except sqlite3.Error:
               pass
       if self.journal_mode == "wal":
           # Check often enough that the -wal file cannot grow without
           # bound on a share nobody ever closes the app on.
           self.conn.execute("PRAGMA wal_autocheckpoint = 2000")
       # Building the schema is a few dozen statements, and every one of
       # them is a round trip to a database that may be sitting in a shared
       # folder.  It only has to happen when the schema actually changed,
       # so the file carries the version it was built at and an unchanged
       # file is opened without a single DDL statement.
       # Can this copy write at all?  A shared folder is often set so that
       # only one person may change what is in it, and everybody else gets
       # read access.  That must mean "you can look at everything but not
       # alter it" - not "the program refuses to start", which is what
       # happened while every start-up step assumed it could write.
       self.read_only = not self._can_write()
       needs_setup = True
       try:
           rows = with_lock_retry(
               lambda: self.conn.execute(
                   "SELECT value FROM meta WHERE key='schema_version'"
               ).fetchall())
           row = rows[0] if rows else None
           needs_setup = not row or str(row[0]) != str(SCHEMA_VERSION)
       except sqlite3.Error:
           needs_setup = True          # no meta table yet - a fresh file
       if needs_setup and not self.read_only:
           self._build_schema()
           self._meta_cache = None
       if not self.read_only:
           self._heal_partdata()
       self._attach_sessions()
       # the tests run every page of the suite through the local copy
       if os.environ.get("WIP_LOCAL_COPY") == "test":
           self.mirror_start(force=True, sync=True)

   # -- the local copy (see THE LOCAL COPY above) ------------------------
   def mirror_wanted(self, force=False):
       """On a share, unless switched off (prefs "local_copy": false, or
       WIP_LOCAL_COPY=0)."""
       env = os.environ.get("WIP_LOCAL_COPY", "")
       if env == "0":
           return False
       if force or env in ("1", "test"):
           return True
       try:
           if load_prefs().get("local_copy") is False:
               return False
       except Exception:
           pass
       return bool(self.on_network)

   def _mirror_base(self):
       return "db_%s" % hashlib.sha1(
           os.path.abspath(self.path).lower().encode("utf-8")).hexdigest()[:12]

   def mirror_start(self, force=False, sync=False):
       """Switches the local copy on: an existing copy of the same data is
       used at once, otherwise one is made (in a background thread unless
       sync).  Safe to call again."""
       st = self._mirror
       if not self.mirror_wanted(force):
           return False
       st["enabled"] = True
       st["sync"] = bool(sync)
       if st["ok"] or st["busy"]:
           return True
       try:
           os.makedirs(MIRROR_DIR, exist_ok=True)
       except OSError as e:
           st["error"] = "no folder for the local copy: %s" % e
           st["enabled"] = False
           return False
       mirror_housekeeping()
       if self._mirror_reuse():
           _mirror_cleanup(self._mirror_base(), keep=(st["path"],))
           return True
       self._mirror_build(sync)
       return True

   def _mirror_reuse(self):
       """A copy left by an earlier session that still holds exactly the
       shared data: used as it is, nothing is copied."""
       st = self._mirror
       base = self._mirror_base()
       try:
           here = with_lock_retry(lambda: _mirror_stamp(self.conn._real), seconds=5.0)
       except Exception:
           return False
       try:
           names = sorted((n for n in os.listdir(MIRROR_DIR)
                           if n.startswith(base) and n.endswith(".db")), reverse=True)
       except OSError:
           return False
       for n in names:
           p = os.path.join(MIRROR_DIR, n)
           try:
               c = sqlite3.connect(p)
               try:
                   there = _mirror_stamp(c)
               finally:
                   c.close()
           except Exception:
               continue
           if there == here and "?" not in there:
               st.update(path=p, raw=there[0], ok=True, how="kept", error="",
                         gen=st["gen"] + 1, pct=100, msg="")
               return True
       return False

   def _mirror_build(self, sync=False):
       st = self._mirror
       if st["busy"]:
           return
       st.update(busy=True, pct=0, msg="", error="")
       wgen = st["wgen"]
       shared, base = self.path, self._mirror_base()
       wal = self.journal_mode == "wal"

       def job():
           t0 = time.time()
           tmp = os.path.join(MIRROR_DIR, "%s.part" % base)
           try:
               # the folder may have been deleted from the Desktop meanwhile
               os.makedirs(MIRROR_DIR, exist_ok=True)
               mirror_housekeeping()
               need = os.path.getsize(shared)
               try:
                   import shutil
                   free = shutil.disk_usage(MIRROR_DIR).free
                   if free < need * 1.2 + 200 * 1024 * 1024:
                       raise OSError("not enough room on this computer (%d MB free, "
                                     "%d MB needed)" % (free >> 20, (need * 1.2) / 1048576))
               except (AttributeError, ValueError):
                   pass
               how = mirror_make(shared, tmp, st, wal)
               raw, _stamp = mirror_verify(tmp)
               final = os.path.join(MIRROR_DIR, "%s_%s.db" % (
                   base, datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")))
               os.replace(tmp, final)
               if st["wgen"] != wgen:
                   # this copy of the program wrote to the data meanwhile
                   raise RuntimeError("the data changed while it was copied")
               old = st["path"]
               st.update(path=final, raw=raw, how=how, gen=st["gen"] + 1,
                         secs=time.time() - t0, pct=100, ok=True)
               _mirror_cleanup(base, keep=(final, old))
           except Exception as e:
               st["error"] = str(e)[:200]
               try:
                   if os.path.exists(tmp):
                       os.remove(tmp)
               except OSError:
                   pass
           finally:
               st["busy"] = False

       if sync:
           job()
       else:
           th = threading.Thread(target=job, name="local-copy", daemon=True)
           st["thread"] = th
           th.start()

   def mirror_check(self):
       """Called now and then (the window's shared-data check, and after
       this copy's own changes): a copy that no longer holds the shared
       data is switched off and a new one started.  Returns the state."""
       st = self._mirror
       if not st["enabled"]:
           return st
       if st["ok"]:
           try:
               r = self.conn._real.execute(
                   "SELECT value FROM meta WHERE key='data_version'").fetchone()
               st["live"] = r[0] if r else None
               if st["live"] != st["raw"]:
                   st["ok"] = False
           except sqlite3.Error:
               return st                 # somebody is writing: ask again later
       if (not st["ok"] and not st["busy"] and not getattr(self, "_in_txn", False)
               and not self.conn._real.in_transaction):
           if not self._mirror_reuse():
               self._mirror_build(st["sync"])
       return st

   def mirror_off(self):
       """This copy of the program is changing the data: its local copy
       is out of date from this moment."""
       st = self._mirror
       st["wgen"] += 1
       st["ok"] = False
       st["live"] = None          # the version is about to move: read again

   def mirror_text(self):
       """One line for the status bar, '' when there is nothing to say."""
       st = self._mirror
       if not st["enabled"]:
           return ""
       if st["busy"]:
           return "Copying the data to this computer  %d%%" % st["pct"]
       if st["ok"]:
           return ("Data is read from this computer (local copy)\n%s"
                   % os.path.dirname(st["path"] or ""))
       if st["error"]:
           return "Local copy not available: %s" % st["error"]
       return "Local copy is being refreshed"

   def _heal_partdata(self):
       """2.1.4: an import whose waterfall rebuild never finished (see
       wf_rebuild_part_data) is finished by the next copy that opens.
       Left alone for two minutes so the importing copy can do its own."""
       try:
           row = self.conn.execute(
               "SELECT value FROM meta WHERE key='partdata_pending'").fetchone()
           if not row or (datetime.datetime.now() - datetime.datetime.fromisoformat(
                   json.loads(row[0]))).total_seconds() <= 120:
               return
       except Exception:
           return
       try:
           self.wf_rebuild_part_data()
       except Exception as e:
           try:
               self.write_sql("INSERT OR REPLACE INTO meta (key, value) "
                              "VALUES ('partdata_rebuild_failed', ?)",
                              (json.dumps(str(e)[:200]),))
           except Exception:
               pass

   # -- who has the program open: a file of its own (v75) ----------------
   # Every open copy writes a heartbeat.  In the shared database file that
   # write changed the file, and SQLite then throws away every OTHER copy's
   # page cache (it cannot know what changed) - so with ten people in, the
   # big tables were read across the network again every few seconds, and
   # MRB / B1000 / Forecast opened slowly however often they had been
   # opened.  The heartbeats now go to <name>_sessions.db next to it; the
   # data file changes only when the data does.
   T_SESS = "app_session"

   def _attach_sessions(self):
       base, _ext = os.path.splitext(self.path)
       spath = base + "_sessions.db"
       try:
           self.conn.execute("ATTACH DATABASE ? AS sess", (spath,))
       except sqlite3.Error:
           return                  # cannot open it: the old table in the file
       try:
           mode = "TRUNCATE" if self.on_network else "WAL"
           try:
               self.conn.execute("PRAGMA sess.journal_mode = %s" % mode).fetchone()
           except sqlite3.Error:
               pass
           self.conn.execute(
               "CREATE TABLE IF NOT EXISTS sess.app_session ("
               "session_id TEXT PRIMARY KEY, username TEXT, host TEXT, pid INTEGER, "
               "started_at TEXT, last_seen TEXT, close_requested TEXT, closed_at TEXT)")
           self.conn.execute("CREATE INDEX IF NOT EXISTS sess.ix_app_session_seen "
                             "ON app_session (last_seen)")
           self.conn.commit()
           self.T_SESS = "sess.app_session"
       except sqlite3.Error:
           try:
               self.conn.rollback()
           except sqlite3.Error:
               pass
           try:
               self.conn.execute("SELECT 1 FROM sess.app_session LIMIT 1")
               self.T_SESS = "sess.app_session"    # read-only, but it is there
           except sqlite3.Error:
               try:
                   self.conn.execute("DETACH DATABASE sess")
               except sqlite3.Error:
                   pass

   def txn_begin(self):
       """Starts one all-or-nothing write.

       Everything between this and txn_commit() lands together or not at
       all.  The imports used to delete first, COMMIT, and then insert;
       for the length of the import everybody else on the share read a
       database with a hole in it, and anything that interrupted it - a
       dropped network connection is enough - left the deleted rows gone
       with nothing to roll back to.

       With one transaction the other copies read the OLD data right up
       to the instant the new data appears, and an interrupted import
       leaves the old data exactly as it was.
       """
       self.conn.commit()
       self._in_txn = True
       with_lock_retry(lambda: self.conn.execute("BEGIN IMMEDIATE"),
                       seconds=WRITE_WAIT_SECONDS)

   def txn_commit(self):
       try:
           # 2.1.4: a COMMIT that meets a reader (on the share the journal
           # is TRUNCATE, so somebody still reading - this copy's own
           # background preparation too - keeps the file) returns "locked"
           # with the transaction still open; it is simply tried again.
           with_lock_retry(lambda: self.conn.execute("COMMIT"),
                           seconds=WRITE_WAIT_SECONDS)
       finally:
           self._in_txn = False
           # An import just landed; everything remembered from before it
           # describes a database that no longer exists.
           self._read_cache = {}
           self._meta_cache = None

   def txn_rollback(self):
       self._in_txn = False
       try:
           self.conn.execute("ROLLBACK")
       except sqlite3.Error:
           pass

   def write_sql(self, sql, params=()):
       """A short write that waits out somebody else's write.

       Reads already wait (see rows()); writes did not, so saving a TECO
       note while somebody was uploading raised "database is locked" and
       the note was lost.  Only safe for SHORT, self-contained statements
       - a retry re-runs the statement, so it must not be half of a
       transaction somebody else is depending on.
       """
       def once():
           self.conn.execute(sql, params)
           self.conn.commit()
       out = with_lock_retry(once, seconds=WRITE_WAIT_SECONDS)
       self._read_cache = {}
       return out

   def busy_now(self, ms=250):
       """v69: is another copy writing to the file at this moment?  Asked
       with a short wait (ms) before the screen's background check, so a
       long upload by somebody else makes this copy skip one check
       instead of freezing for several seconds.  On a local disk (WAL)
       reading never waits and this is always False."""
       try:
           old = self.conn.execute("PRAGMA busy_timeout").fetchone()[0]
       except sqlite3.Error:
           old = 1500
       try:
           self.conn.execute("PRAGMA busy_timeout = %d" % int(ms))
           self.conn.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
           return False
       except sqlite3.Error as e:
           return is_lock_error(e)
       finally:
           try:
               self.conn.execute("PRAGMA busy_timeout = %d" % int(old))
           except sqlite3.Error:
               pass

   def _write_waiting(self, fn, seconds=WRITE_WAIT_SECONDS):
       """v69: runs a small write (`fn` does the statements, this does the
       commit) and waits out another copy's write instead of failing.  A
       failed attempt is rolled back first, so a retry never piles onto a
       half-open transaction.  For the account and session writes, which
       used to go straight to the connection and could raise "database is
       locked" when two people signed in or an admin acted at the same
       moment as somebody's upload."""
       def once():
           try:
               fn()
               self.conn.commit()
           except sqlite3.Error:
               try:
                   self.conn.rollback()
               except sqlite3.Error:
                   pass
               raise
       return with_lock_retry(once, seconds=seconds)

   # A handful of small lookups are asked for again and again: the part
   # list, the flag map, the serials that have shipped, the years with
   # rework in them.  On a local disk that costs nothing.  But this
   # program is used on a database in a SHARED FOLDER, where every query
   # is a round trip over the network, and start-up alone asked for these
   # forty-odd times before the window appeared.
   #
   # So the ROWS are kept, not the structures built from them.  Rebuilding
   # a dict from rows already in memory is free, and it means no caller
   # can ever be handed a list somebody else has since changed - the bug
   # a shared-object cache would have introduced in exchange for nothing.
   #
   # Everything here is dropped the moment the data could have moved: any
   # write through this connection, and any sign that another copy of the
   # program changed the file (see forget_reads' callers).
   def forget_reads(self):
       self._read_cache = {}

   _READ_ONLY_SQL = ("SELECT", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK",
                     "EXPLAIN", "RELEASE", "SAVEPOINT")
   # The tables the read cache actually holds answers about.  A write to
   # anything else cannot make a cached answer wrong.
   #
   # This matters with several people in the file at once: every copy of
   # the program writes a heartbeat into app_session every forty seconds,
   # and while ANY write threw the whole cache away, each of those
   # heartbeats made the next page refresh ask the database everything
   # again - over the network, for nothing.  Sessions, accounts and
   # private notes are nobody's cached answer.
   _CACHED_TABLES = ("meta", "parts", "comp", "rw", "ops", "shipment",
                     "serial_flags", "wf_traveler", "wf_part_data")
   _WRITE_HEADS = ("INSERT", "UPDATE", "DELETE", "REPLACE")

   def _on_statement(self, sql):
       """Called by SQLite for every statement - see __init__."""
       text = (sql or "").lstrip()
       head = text[:9].upper()
       for ok in self._READ_ONLY_SQL:
           if head.startswith(ok):
               return
       # Which table is being written?  Anything this cannot read with
       # confidence clears the cache - being wrong about freshness is far
       # worse than one extra query.
       table = ""
       up = text.upper()
       for verb, after in (("INSERT INTO", 11), ("REPLACE INTO", 12),
                           ("INSERT OR REPLACE INTO", 22),
                           ("INSERT OR IGNORE INTO", 21),
                           ("UPDATE OR IGNORE", 16),
                           ("DELETE FROM", 11), ("UPDATE", 6)):
           if up.startswith(verb):
               table = text[after:].strip().split(None, 1)[0].strip("`\"[]();")
               break
       # 2.2.0: a change to a mirrored table (or anything this cannot name:
       # a schema change, a script) makes this copy's local copy out of date
       tl = table.lower()
       if not table or tl in MIRROR_TABLES or (tl not in MIRROR_NEVER
                                                and "." not in tl):
           st = self.__dict__.get("_mirror")
           if st is not None and st["enabled"]:
               st["wgen"] += 1
               st["ok"] = False
       if table and table.lower() not in self._CACHED_TABLES:
           if up.startswith(self._WRITE_HEADS):
               return
       self._read_cache = {}
       self._meta_cache = None
       self.__dict__["_live_meta"] = {}
       # A write to a DATA table from this copy: the stored summaries
       # (_agg) may be stale until the data version moves, so this copy
       # stops trusting them and works each one out again (v66).
       # A TECO/MRB/SCRAP record is not data any summary is made from, so
       # saving one no longer throws them all away (v67) - it did, and
       # every page read the big tables again for the rest of the session.
       if (table and table.lower() not in ("meta", "serial_flags")
               and up.startswith(self._WRITE_HEADS)):
           self.__dict__["_agg_mem"] = {}
           self.__dict__["_agg_dirty"] = True

   def _cached_rows(self, sql, params=()):
       key = (sql, params)
       hit = self._read_cache.get(key)
       if hit is None:
           hit = list(self.rows(sql, params))
           self._read_cache[key] = hit
       return hit

   def write_batch(self, statements):
       """Several short writes as ONE transaction.

       Two writes mean two exclusive locks and two flushes to disk, and on
       a shared folder that is twice the wait - for the person typing AND
       for everybody else waiting on the file.  Saving a TECO note was
       exactly that: the note itself, then a second, separate write to
       bump the version that tells the other copies to look again.

       Inside an import (txn_begin) the statements just join the
       transaction already open rather than trying to start another.
       """
       if getattr(self, "_in_txn", False):
           for sql, params in statements:
               self.conn.execute(sql, params)
           return

       def once():
           # Python's sqlite3 opens a transaction of its own on the first
           # write, so a statement run earlier may still have one pending -
           # and BEGIN inside a transaction is an error.  txn_begin closes
           # the same way before it starts.
           if self.conn.in_transaction:
               self.conn.commit()
           self.conn.execute("BEGIN IMMEDIATE")
           try:
               for sql, params in statements:
                   self.conn.execute(sql, params)
               self.conn.execute("COMMIT")
           except Exception:
               try:
                   self.conn.execute("ROLLBACK")
               except sqlite3.Error:
                   pass
               raise
       with_lock_retry(once, seconds=WRITE_WAIT_SECONDS)
       self._read_cache = {}
       self._meta_cache = None

   # Adding one to a counter in the settings table WITHOUT reading it
   # first.  The read was a round trip of its own, and worse, it was a
   # round trip between two writes - so two people saving at the same
   # moment could both read the same number and one of the two bumps
   # would be lost, leaving a copy that never noticed the other's change.
   BUMP_META_SQL = (
       "INSERT INTO meta (key,value) VALUES (?,'1') "
       "ON CONFLICT(key) DO UPDATE SET "
       "value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)")

   def rows(self, sql, params=()):
       """A read that waits out somebody else's write instead of failing.

       Reading is not writing: another person saving must never be able to
       stop this copy from LOOKING at the data.  Locking a data set in
       DATA & SETTINGS closes it to CHANGE, and that is all it has ever
       done - it does not and must not close it to reading either.
       """
       return with_lock_retry(
           lambda: self.conn.execute(sql, params).fetchall())

   @staticmethod
   def _progress_tick():
       """Called by SQLite while it grinds through a long query - the only
       moment anything can move the spinner, since the interface is stuck
       inside execute() until the query returns."""
       tick()
       return 0            # anything non-zero would abort the query

   def _can_write(self):
       """One real write that changes nothing.

       BEGIN IMMEDIATE is not enough - SQLite defers the actual write and
       happily starts the transaction on a file it will later refuse to
       modify.  Writing user_version back to the value it already has
       touches the file header for real, so a read-only file says so here
       rather than three screens later.
       """
       def probe():
           v = self.conn.execute("PRAGMA user_version").fetchone()[0]
           self.conn.execute("PRAGMA user_version = %d" % int(v))
           self.conn.commit()
           return True
       try:
           # A LOCK IS NOT READ-ONLY.  This runs on every start-up, so if
           # somebody happened to be uploading at that moment the probe
           # failed and the copy spent the whole session insisting it
           # could not write - telling a person with full access to go and
           # ask an admin for it.  A busy file is waited out; only a real
           # refusal answers no.
           return bool(with_lock_retry(probe, seconds=WRITE_WAIT_SECONDS))
       except sqlite3.Error:
           return False

   def _build_schema(self):
       """Creates anything missing and adds columns older files lack.

       Every step is written so that running it on a database that already
       has it does nothing, because two people can open a new build at the
       same moment and both end up here.
       """
       self.conn.executescript(SCHEMA)
       cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(rw)")}
       for name in ("row_hash", "wc_desc", "emp_no", "emp_name", "entry_dt"):
           if name not in cols:
               self.conn.execute("ALTER TABLE rw ADD COLUMN %s TEXT" % name)
       for name in ("tot_hour", "mach_hour"):
           if name not in cols:
               self.conn.execute(
                   "ALTER TABLE rw ADD COLUMN %s REAL DEFAULT 0" % name)
       ops_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(ops)")}
       if "entry_dt" not in ops_cols:
           self.conn.execute("ALTER TABLE ops ADD COLUMN entry_dt TEXT")
       # SEQUENCE + LAST-ENTRY X (v52).  Old databases have neither; the
       # columns come in empty and every rule falls back to what it did
       # before, so an export made before the new columns existed keeps
       # reading exactly as it used to.
       for _t, _cols in (("ops", ops_cols), ("rw", cols)):
           if _cols and "seq" not in _cols:
               self.conn.execute(
                   "ALTER TABLE %s ADD COLUMN seq TEXT DEFAULT ''" % _t)
           if _cols and "last_x" not in _cols:
               self.conn.execute(
                   "ALTER TABLE %s ADD COLUMN last_x INTEGER DEFAULT 0" % _t)
       if ops_cols and "setup" not in ops_cols:
           # Rows imported before v62 carry no setup mark; they read as
           # production until the data is uploaded again.
           self.conn.execute(
               "ALTER TABLE ops ADD COLUMN setup INTEGER DEFAULT 0")
       if ops_cols and "lab_hour" not in ops_cols:
           self.conn.execute("ALTER TABLE ops ADD COLUMN lab_hour REAL")
       for name in ("tot_hour", "mach_hour"):
           if name not in ops_cols:
               self.conn.execute(
                   "ALTER TABLE ops ADD COLUMN %s REAL DEFAULT 0" % name)
       wf_cols = {r["name"] for r in self.conn.execute(
           "PRAGMA table_info(wf_part_data)")}
       if "std_hour" not in wf_cols:
           self.conn.execute(
               "ALTER TABLE wf_part_data ADD COLUMN std_hour REAL DEFAULT 0")
       if "machine_hour" not in wf_cols:
           self.conn.execute(
               "ALTER TABLE wf_part_data ADD COLUMN machine_hour REAL DEFAULT 0")
       needs_partdata_rebuild = False
       flag_cols = {r["name"] for r in self.conn.execute(
           "PRAGMA table_info(serial_flags)")}
       for name in ("resolved_from", "resolved_at", "resolved_by", "part_no"):
           if flag_cols and name not in flag_cols:
               self.conn.execute(
                   "ALTER TABLE serial_flags ADD COLUMN %s TEXT" % name)
       for _ad in ("dt_first", "dt_ops_first"):
           if wf_cols and _ad not in wf_cols:
               self.conn.execute(
                   "ALTER TABLE wf_part_data ADD COLUMN %s TEXT" % _ad)
               # Same reasoning as dt_ops below: ALTER leaves them NULL on
               # every existing row, and lead time is measured from them.
               needs_partdata_rebuild = True
       if wf_cols and "seq" not in wf_cols:
           # The KEY itself changes - (part, serial, operation) becomes
           # (part, serial, operation, sequence) - so this one cannot be
           # an ALTER.  The table is derived from ops/rw anyway, so it is
           # dropped and rebuilt rather than migrated in place.
           self.conn.execute("DROP TABLE IF EXISTS wf_part_data")
           self.conn.executescript(WF_PART_DATA_SQL)
           wf_cols = {r["name"] for r in self.conn.execute(
               "PRAGMA table_info(wf_part_data)")}
           needs_partdata_rebuild = True
       for _ad, _tip in (("x_dt", "TEXT"), ("prog", "REAL DEFAULT 0")):
           if wf_cols and _ad not in wf_cols:
               self.conn.execute(
                   "ALTER TABLE wf_part_data ADD COLUMN %s %s" % (_ad, _tip))
               needs_partdata_rebuild = True
       # The progress figure is worked out by the rebuild; when its rule
       # changes, every stored figure has to be worked out again.
       _kural = self.conn.execute(
           "SELECT value FROM meta WHERE key='prog_rule'").fetchone()
       if not _kural or _kural[0] != json.dumps(PROG_RULE):
           needs_partdata_rebuild = True
           self.conn.execute(
               "INSERT INTO meta (key,value) VALUES ('prog_rule',?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (json.dumps(PROG_RULE),))
       # Shipment keys stored before the RTD prefix was stripped.
       _rtd = self.conn.execute(
           "SELECT id, material_key, serial_key, irsaliye, po_no, poitem "
           "FROM shipment WHERE serial_key LIKE 'RTD_%'").fetchall()
       for _r in _rtd:
           _sk = shipment_serial_key(_r["serial_key"])
           _h = hashlib.sha1("|".join([
               _r["material_key"] or "", _sk, _r["irsaliye"] or "",
               _r["po_no"] or "", _r["poitem"] or ""]).encode("utf-8")).hexdigest()
           self.conn.execute(
               "UPDATE OR IGNORE shipment SET serial_key=?, row_hash=? WHERE id=?",
               (_sk, _h, _r["id"]))
       if _rtd:
           # the stored "sold" summaries were counted with the old keys
           try:
               self.conn.execute("DELETE FROM agg_cache")
           except sqlite3.Error:
               pass                    # no summaries stored yet
       if wf_cols and "mach_ops" not in wf_cols:
           # 2.2.0 (schema 27): operation time without the rework hours
           self.conn.execute("ALTER TABLE wf_part_data ADD COLUMN mach_ops REAL")
           self.conn.execute("ALTER TABLE wf_part_data ADD COLUMN lab_ops REAL")
           needs_partdata_rebuild = True
       if "dt_ops" not in wf_cols:
           self.conn.execute("ALTER TABLE wf_part_data ADD COLUMN dt_ops TEXT")
           # ALTER TABLE leaves the new column NULL on every existing row,
           # and a NULL dt_ops means "this operation only ever carried
           # rework" - so without a rebuild every row would say that, the
           # position walk would fall back to the old rule, and the fix
           # would quietly do nothing on exactly the databases it is for.
           needs_partdata_rebuild = True
       # A database made before employee numbers were dropped still has
       # the column.  Nothing reads it any more; it is left in place
       # because rebuilding a table to remove one unused column buys
       # nothing, and the values are cleared so the data really is gone.
       user_cols = {r["name"] for r in self.conn.execute(
           "PRAGMA table_info(app_user)")}
       if "sicil" in user_cols:
           self.conn.execute("UPDATE app_user SET sicil='' WHERE sicil<>''")
       li_cols = {r["name"] for r in self.conn.execute(
           "PRAGMA table_info(layout_item)")}
       if li_cols and "groups" not in li_cols:
           self.conn.execute(
               "ALTER TABLE layout_item ADD COLUMN groups TEXT DEFAULT ''")
       if li_cols and "carry" not in li_cols:
           self.conn.execute(
               "ALTER TABLE layout_item ADD COLUMN carry INTEGER DEFAULT 1")
       if li_cols and "locked" not in li_cols:
           self.conn.execute(
               "ALTER TABLE layout_item ADD COLUMN locked INTEGER DEFAULT 0")
       if li_cols and "part_groups" not in li_cols:
           self.conn.execute(
               "ALTER TABLE layout_item ADD COLUMN part_groups TEXT DEFAULT ''")
       if li_cols and "people" not in li_cols:
           self.conn.execute(
               "ALTER TABLE layout_item ADD COLUMN people INTEGER DEFAULT 0")
       trav_cols = {r["name"] for r in self.conn.execute(
           "PRAGMA table_info(wf_traveler)")}
       if trav_cols and "seq" not in trav_cols:
           self.conn.execute(
               "ALTER TABLE wf_traveler ADD COLUMN seq TEXT DEFAULT ''")
       if "active" not in trav_cols:
           self.conn.execute(
               "ALTER TABLE wf_traveler ADD COLUMN active INTEGER DEFAULT 1")
       if trav_cols and "std_hour" not in trav_cols:
           self.conn.execute(
               "ALTER TABLE wf_traveler ADD COLUMN std_hour REAL DEFAULT 0")
       if trav_cols and "wc_group" not in trav_cols:
           self.conn.execute(
               "ALTER TABLE wf_traveler ADD COLUMN wc_group TEXT DEFAULT ''")
       # A private note's colour.  CREATE TABLE IF NOT EXISTS leaves an
       # existing table exactly as it was, so a database made before the
       # colour existed needs the column added here - otherwise every read
       # of a note's colour fails, and the pages that paint serials by it
       # (the analysis raw table, the waterfall) stop rendering entirely.
       note_cols = {r["name"] for r in self.conn.execute(
           "PRAGMA table_info(user_note)")}
       if note_cols and "color" not in note_cols:
           self.conn.execute("ALTER TABLE user_note ADD COLUMN color TEXT")
       if note_cols and "private" not in note_cols:
           # Notes written before this column existed were private by
           # definition - that was the only behaviour - so they stay that
           # way rather than suddenly becoming visible to everyone.
           self.conn.execute(
               "ALTER TABLE user_note ADD COLUMN private INTEGER DEFAULT 1")
       # The data locks are gone: who may change the shared data is the
       # account's role now, not a padlock with a shared password.  A
       # database written by an older build still carries the padlock
       # rows, and a stale "locked_sap = 1" nobody can see or clear is
       # exactly the kind of thing that would be blamed a year from now,
       # so they are dropped on the way past.
       self.conn.execute(
           "DELETE FROM meta WHERE key LIKE 'locked_%'")
       self.conn.execute(
           "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
           (str(SCHEMA_VERSION),))
       self.conn.commit()
       if needs_partdata_rebuild:
           # Recomputed straight from rw/ops, which are untouched, so this
           # costs one pass over the bookings and nothing is lost.
           try:
               self.wf_rebuild_part_data()
           except Exception as e:
               # An upgrade must never stop the program opening.  But it
               # must not fail SILENTLY either: without this rebuild the
               # dt_ops column stays empty, every operation looks like it
               # only ever carried rework, and the position rule quietly
               # falls back to the behaviour the rebuild exists to
               # replace.  The failure is recorded so DATA HEALTH can say
               # so out loud instead of the shop wondering why the plan
               # reads wrong.
               try:
                   self.conn.execute(
                       "INSERT OR REPLACE INTO meta (key, value) "
                       "VALUES ('partdata_rebuild_failed', ?)",
                       (json.dumps(str(e)[:200]),))
                   self.conn.commit()
               except sqlite3.Error:
                   pass
           else:
               try:
                   self.conn.execute(
                       "DELETE FROM meta WHERE key='partdata_rebuild_failed'")
                   self.conn.commit()
               except sqlite3.Error:
                   pass

   # -- shared change counter ------------------------------------------
   def data_version(self):
       return self.get_meta("data_version", 0) or 0

   def settings_version(self):
       """Moves whenever ANY copy changes a setting - see LIVE_META."""
       return self.get_meta("settings_version", 0) or 0

   def bump_version(self):
       """Says "the underlying data changed" - an import, a deletion, a
       traveler edit.  Every client throws its caches away and recomputes
       when it sees this move, so it must only be used for changes that
       really do alter what an analysis would produce."""
       self.set_meta("data_version", (self.data_version() or 0) + 1)
       # 2.2.0: the local copy holds the data as it was before this change
       st = self.__dict__.get("_mirror")
       if st is not None and st["enabled"]:
           self.mirror_off()
           if st["sync"]:
               self.mirror_check()

   def note_upload(self):
       """Stamps when data was last UPLOADED (v67) - what the header's
       LAST UPDATED shows.  Only the imports call it: a traveler edit or a
       deletion also moves data_version, but is not "new data"."""
       self.set_meta("last_upload_at",
                     datetime.datetime.now().isoformat(timespec="seconds"))

   def last_upload_at(self):
       """When data was last uploaded, as a datetime, or None.  A database
       loaded before v67 has no stamp yet: the newest import time stored
       on the rework rows stands in for it."""
       v = self.get_meta("last_upload_at", "") or ""
       if not v:
           try:
               v = self.conn.execute(
                   "SELECT MAX(imported_at) FROM rw").fetchone()[0] or ""
           except sqlite3.Error:
               v = ""
       try:
           return datetime.datetime.fromisoformat(str(v)) if v else None
       except ValueError:
           return None

   def annotation_version(self):
       return self.get_meta("annotation_version", 0) or 0

   def bump_annotations(self):
       """Says "a marking changed" - an MRB/SCRAP/TECO flag or somebody's
       private note.

       These change how serials are COLOURED, not what any figure comes
       out as, so they deliberately do not touch data_version.  They used
       to: saving one note invalidated every cache in every running copy
       and made all of them recompute from scratch.  With several people
       entering flags at once that is a rebuild per keystroke-ish, which
       is where the minutes-long freezes came from.
       """
       self.set_meta("annotation_version", (self.annotation_version() or 0) + 1)

   # -- meta ------------------------------------------------------------
   #
   # The settings table is tiny and read constantly: the week start, the
   # shop rules, the maximum rework hour, a dozen display preferences.
   # Over a shared folder every one of those reads is a network round
   # trip, and start-up alone made thirty-nine of them before the window
   # appeared.  So the table is read ONCE, in full, and kept.
   #
   # Two keys are deliberately never cached.  data_version and
   # annotation_version are how this copy notices that SOMEBODY ELSE
   # changed something; a cached answer there would mean never noticing,
   # which is a far worse bug than a slow start-up.  A move in either of
   # them also throws the rest of the cache away, because whoever made
   # that change may have changed a setting too.
   # ALWAYS READ FROM THE FILE, never from this copy's memory.
   #
   # settings_version is what makes every OTHER copy notice a settings
   # change.  The settings are held in memory because on a shared folder
   # each read is a round trip - but that cache is per copy and nothing
   # another machine does can clear it.  So an admin could change the
   # shift length, or switch MAINTENANCE MODE on, and the copies already
   # running would carry on with what they read at start-up: measured,
   # "everybody out" reached nobody who was already in.
   #
   # Now every settings write moves this counter, every copy reads it on
   # its poll, and a move throws that copy's settings cache away.
   # `maintenance` is in here as well so it is right even between polls.
   LIVE_META = ("data_version", "annotation_version", "settings_version",
                "maintenance")

   LIVE_TTL = 1.0

   def forget_meta(self):
       """Read the settings from the file again on the next ask."""
       self._meta_cache = None
       self.__dict__["_live_meta"] = {}

   def _meta_all(self):
       if self._meta_cache is None:
           try:
               self._meta_cache = {
                   r["key"]: r["value"] for r in
                   self.conn.execute("SELECT key, value FROM meta")}
           except sqlite3.Error:
               return {}
       return self._meta_cache

   def set_meta(self, key, value, _bump=True):
       # Settings are shared, so a read-only copy simply keeps whatever the
       # file already says rather than failing.
       if self.read_only:
           return
       raw = json.dumps(value, ensure_ascii=False)
       if key not in self.LIVE_META:
           try:
               r = self.conn.execute(
                   "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
           except sqlite3.Error:
               r = None
           if r is not None and r["value"] == raw:
               return                 # same value: no write, nobody woken
       self.write_sql("INSERT INTO meta (key,value) VALUES (?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, raw))
       if key == "settings_version":
           # this copy's own change: its poll must not reload for it
           self.own_settings_version = value
       # Tell the other copies.  The version keys themselves are excluded
       # or this would never stop; so is a write that did not change the
       # value, which would wake everybody for nothing.
       if _bump and key not in self.LIVE_META:
           try:
               self.set_meta("settings_version",
                             (self.get_meta("settings_version", 0) or 0) + 1,
                             _bump=False)
           except sqlite3.Error:
               pass
       # The write itself has already emptied the cache (_on_statement),
       # so the next read comes from the file - which is what we want
       # after a change, and one query is a small price for never having
       # to reason about whether this one was kept in step.

   def get_meta(self, key, default=None):
       if key in self.LIVE_META:
           # 2.2.0: ON A SHARE the answer is kept for a second.  These are
           # asked for constantly - every cached figure checks the data
           # version before it is handed out, hundreds of times while one
           # page is drawn - and each ask was a round trip to the share.
           # Another person's change is still seen within the second (the
           # window itself only looks every eight); this copy's own
           # changes clear it at once (_on_statement, forget_meta).
           live = self.__dict__.setdefault("_live_meta", {})
           hit = live.get(key)
           now = time.time()
           if hit is not None and now - hit[0] < self.LIVE_TTL and self.on_network:
               raw = hit[1]
           else:
               r = self.conn.execute(
                   "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
               raw = r["value"] if r else None
               live[key] = (now, raw)
               if key == "data_version":
                   # what the local copy is checked against (MirrorConn)
                   self._mirror["live"] = raw
       else:
           raw = self._meta_all().get(key)
       if raw is None:
           return default
       try:
           return json.loads(raw)
       except Exception:
           return default

   # -- working week ----------------------------------------------------
   # WHICH DAY THE FACTORY WEEK STARTS ON - and it need not be the same
   # answer on every page.
   #
   # Rework is reviewed on a Friday and the bottleneck is walked on a
   # Monday; asking both to use one week start means one of them is
   # always reading a week that ends in the middle of its own meeting.
   # So there is a default, and any page may be given its own.
   PAGE_WEEK_KEY = "week_start_pages"

   def week_start(self, page=None):
       """The week start for one page, or the default when it has none."""
       if page:
           try:
               ozel = self.get_meta(self.PAGE_WEEK_KEY, None) or {}
               v = ozel.get(str(page))
               if v is not None and str(v) != "":
                   return int(v) % 7
           except Exception:
               pass
       v = self.get_meta("week_start", DEFAULT_WEEK_START)
       try:
           return int(v) % 7
       except Exception:
           return DEFAULT_WEEK_START

   def page_week_starts(self):
       """{page: weekday} for the pages given their own start."""
       try:
           ham = self.get_meta(self.PAGE_WEEK_KEY, None) or {}
       except Exception:
           return {}
       out = {}
       for k, v in ham.items():
           try:
               out[str(k)] = int(v) % 7
           except (TypeError, ValueError):
               continue
       return out

   def set_page_week_start(self, page, weekday):
       """weekday=None puts that page back on the default."""
       ozel = self.page_week_starts()
       if weekday is None:
           ozel.pop(str(page), None)
       else:
           ozel[str(page)] = int(weekday) % 7
       self.set_meta(self.PAGE_WEEK_KEY, ozel)
       return ozel

   def set_week_start(self, weekday):
       self.set_meta("week_start", int(weekday) % 7)
       self.recompute_weeks()

   def week_numbers_are_current(self):
       return self.get_meta("week_applied", None) == self.week_start()

   def recompute_weeks(self):
       """Re-labels every stored row after the week start day changed.

       Every row on the same date lands in the same factory week, and a
       few hundred thousand rework rows only ever cover a few hundred
       dates - so this updates one date at a time instead of one row at a
       time.  The old version issued an UPDATE per row, which on a
       database sitting in a shared folder meant hundreds of thousands of
       round trips and turned a new user's first start-up into several
       minutes of apparent hanging.
       """
       off = week_offset(self.week_start())
       dates = [r["rw_date"] for r in self.conn.execute(
           "SELECT DISTINCT rw_date FROM rw WHERE rw_date IS NOT NULL")]
       params, n = [], 0
       for ds in dates:
           try:
               d = datetime.date.fromisoformat(ds)
           except Exception:
               continue
           y, w = iso_year_week(d, off)
           params.append((y, w, ds))
       self.conn.executemany(
           "UPDATE rw SET iso_year=?, iso_week=? WHERE rw_date=?", params)
       n = self.conn.execute(
           "SELECT COUNT(*) FROM rw WHERE rw_date IS NOT NULL").fetchone()[0]
       self.set_meta("week_applied", self.week_start())
       self.conn.commit()
       self.bump_version()
       return n


   # -- stored summaries (v66) ---------------------------------------------
   #
   # WHY THE PROGRAM OPENED SLOWLY FROM THE SHARED FOLDER.  Start-up asked
   # about twenty questions that each READ A WHOLE BIG TABLE: how many
   # rework rows, which years, every operation's name, every serial, every
   # sold serial...  On a local disk that is a blink.  On a network share
   # every one of them drags the table (tens of MB) across the network,
   # and that was most of the 40-50 seconds.
   #
   # The answers only change when the DATA does, so the first copy to ask
   # after a change works each one out once and leaves it in `agg_cache`,
   # stamped with the data version.  Every copy after that reads one small
   # row instead of a whole table.  A new import moves the data version,
   # which makes every stored answer stale at once - nothing can be served
   # out of date.
   AGG_TABLE_SQL = ("CREATE TABLE IF NOT EXISTS agg_cache "
                    "(key TEXT PRIMARY KEY, version INTEGER, value TEXT)")

   def _agg(self, key, compute):
       ver = self.data_version()
       mem = self.__dict__.setdefault("_agg_mem", {})
       hit = mem.get(key)
       if hit is not None and hit[0] == ver:
           return hit[1]
       value, found = None, False
       try:
           if self.__dict__.get("_agg_dirty"):
               raise ValueError("this copy wrote data - recount")
           r = self.conn.execute(
               "SELECT version, value FROM agg_cache WHERE key=?",
               (key,)).fetchone()
           if r is not None and r["version"] == ver:
               value, found = json.loads(r["value"]), True
       except (sqlite3.Error, ValueError, TypeError):
           found = False
       if not found:
           value = compute()
           mem[key] = (ver, value)
           # 2.2.0: with the local copy on, working a summary out is quick
           # and a write to the share is the slow part - so it is not
           # stored now but with the others, in one write (flush_agg)
           if (self.__dict__.get("_reader") or self._mirror["ok"]
                   or not self._agg_store(key, ver, value)):
               # a background copy (v72) or a busy file: stored later
               self.__dict__.setdefault("_agg_unsaved", set()).add(key)
           return value
       mem[key] = (ver, value)
       return value

   _AGG_PUT = ("INSERT INTO agg_cache (key, version, value) "
               "VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
               "version=excluded.version, value=excluded.value")

   def _agg_store(self, items_or_key, ver=None, value=None):
       """v72: stores summaries in the file in ONE short write, and gives up
       at once when somebody else holds the file.  It used to be two
       writes, each waiting up to 45 seconds for the lock - on a busy
       shared folder that was the page freezing while it opened.  A
       summary not stored is simply worked out again next time."""
       if self.read_only:
           return False
       items = (items_or_key if isinstance(items_or_key, list)
                else [(items_or_key, ver, value)])
       rows = [(k, v, json.dumps(val, ensure_ascii=False)) for k, v, val in items]
       try:
           self.conn.execute("PRAGMA busy_timeout = 300")
       except sqlite3.Error:
           pass
       try:
           try:
               self.conn.executemany(self._AGG_PUT, rows)
           except sqlite3.OperationalError as e:
               if "no such table" not in str(e):
                   raise
               self.conn.execute(self.AGG_TABLE_SQL)
               self.conn.executemany(self._AGG_PUT, rows)
           self.conn.commit()
           return True
       except sqlite3.Error:
           try:
               self.conn.rollback()
           except sqlite3.Error:
               pass
           return False
       finally:
           try:
               self.conn.execute("PRAGMA busy_timeout = 1500")
           except sqlite3.Error:
               pass

   def flush_agg(self):
       """v72: stores what a background copy worked out (one short write,
       skipped if the file is busy - it is tried again next time)."""
       keys = self.__dict__.get("_agg_unsaved")
       if not keys:
           return
       mem = self.__dict__.get("_agg_mem", {})
       ver = self.data_version()
       items = [(k, mem[k][0], mem[k][1]) for k in list(keys)
                if k in mem and mem[k][0] == ver]
       if not items:
           keys.clear()
           return
       if self._agg_store(items):
           keys.clear()

   def reader_copy(self):
       """v72: a READ-ONLY twin for background work (warming the slow
       pages).  It shares this copy's summaries in memory, never writes,
       and opens its own connection - call open_reader() in the thread
       that uses it, since a connection belongs to one thread."""
       r = object.__new__(type(self))
       r.__dict__.update(self.__dict__)
       r.__dict__.update({
           "conn": None, "_reader": True, "read_only": True, "progress": None,
           "_read_cache": {}, "_meta_cache": None, "_latest_inner": False,
           "_in_txn": False, "cache_id": self.__dict__.get("cache_id", id(self)),
           "_agg_mem": self.__dict__.setdefault("_agg_mem", {}),
           "_agg_unsaved": self.__dict__.setdefault("_agg_unsaved", set())})
       return r

   def open_reader(self):
       self.conn = sqlite3.connect(self.path, timeout=20)
       self.conn.row_factory = sqlite3.Row
       # the background work reads the big tables from the local copy too
       # (its own connection to it - a connection belongs to one thread)
       self.conn = MirrorConn(self.conn, self)
       for pragma in ("PRAGMA busy_timeout = 1500", "PRAGMA cache_size = -131072",
                      "PRAGMA query_only = 1"):
           try:
               self.conn.execute(pragma)
           except sqlite3.Error:
               pass

   # -- counts ----------------------------------------------------------
   def counts(self):
       return self._agg("counts", lambda: {
           t: self.conn.execute("SELECT COUNT(*) n FROM %s" % t).fetchone()["n"]
           for t in ("comp", "parts", "rw", "shipment")})

   def rw_span(self):
       def hesap():
           r = self.conn.execute(
               "SELECT MIN(iso_year) y0, MAX(iso_year) y1, MIN(iso_week) w0,"
               " MAX(iso_week) w1, MIN(rw_date) d0, MAX(rw_date) d1"
               " FROM rw WHERE iso_week IS NOT NULL").fetchone()
           return dict(r) if r and r["y0"] is not None else None
       return self._agg("rw_span", hesap)

   def rw_years(self):
       return self._agg("rw_years", lambda: [
           r["iso_year"] for r in self.conn.execute(
               "SELECT DISTINCT iso_year FROM rw WHERE iso_year IS NOT NULL"
               " ORDER BY iso_year")])

   def wipe_rows(self, table):
       """clear() without the commit - for use inside a transaction."""
       self.conn.execute("DELETE FROM %s" % table)
       if table == "rw":
           for extra in ("ops", "discarded"):
               self.conn.execute("DELETE FROM %s" % extra)
               self.conn.execute(
                   "DELETE FROM sqlite_sequence WHERE name=?", (extra,))
           self.conn.execute("DELETE FROM sqlite_sequence WHERE name='rw'")

   def clear(self, table):
       self.conn.execute("DELETE FROM %s" % table)
       if table == "rw":
           # the production steps and the discarded rows come from the same
           # file, so they go with it
           for extra in ("ops", "discarded"):
               self.conn.execute("DELETE FROM %s" % extra)
               self.conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (extra,))
           self.conn.execute("DELETE FROM sqlite_sequence WHERE name='rw'")
       self.conn.commit()
       self.bump_version()

   def clear_all(self):
       for t in ("comp", "parts", "rw", "shipment"):
           self.clear(t)

   # -- imports ---------------------------------------------------------
   def import_comp(self, records, replace=True):
       # Delete and insert land together; a reader never sees the gap and
       # an interrupted import leaves the old list alone.
       self.txn_begin()
       try:
           if replace:
               self.wipe_rows("comp")
           n = 0
           for r in records:
               code = norm_text(r.get("work_center"))
               if not code:
                   continue
               self.conn.execute(
                   "INSERT INTO comp (work_center,wc_name,comp) VALUES (?,?,?) "
                   "ON CONFLICT(work_center) DO UPDATE SET wc_name=excluded.wc_name,"
                   " comp=excluded.comp",
                   (code, clean_str(r.get("wc_name")), clean_str(r.get("comp")) or UNKNOWN))
               n += 1
           self.txn_commit()
       except BaseException:
           # An import that throws must not leave the transaction
           # open: on a shared file that would hold the write lock
           # until this copy was closed.
           self.txn_rollback()
           raise
       self.bump_version()
       self.note_upload()
       return {"loaded": n, "skipped": 0, "duplicates": 0, "no_date": 0,
               "ops": 0, "removed": 0}

   def import_parts(self, records, replace=True):
       # Delete and insert land together; a reader never sees the gap and
       # an interrupted import leaves the old list alone.
       self.txn_begin()
       try:
           if replace:
               self.wipe_rows("parts")
           n = 0
           for r in records:
               pn = clean_str(r.get("part_no"))
               if not pn:
                   continue
               self.conn.execute(
                   "INSERT INTO parts (part_no,part_key,part_group,engineer)"
                   " VALUES (?,?,?,?) "
                   "ON CONFLICT(part_no) DO UPDATE SET part_key=excluded.part_key,"
                   " part_group=excluded.part_group, engineer=excluded.engineer",
                   (pn, norm_text(pn), clean_str(r.get("part_group")),
                    clean_str(r.get("engineer")).upper()))
               n += 1
           self.txn_commit()
       except BaseException:
           # An import that throws must not leave the transaction
           # open: on a shared file that would hold the write lock
           # until this copy was closed.
           self.txn_rollback()
           raise
       self.bump_version()
       self.note_upload()
       return {"loaded": n, "skipped": 0, "duplicates": 0, "no_date": 0,
               "ops": 0, "removed": 0}

   def import_shipments(self, records, replace=True, day_first=True):
       self.txn_begin()
       try:
           if replace:
               self.wipe_rows("shipment")
           n, skipped, no_date = 0, 0, 0
           # The Ac.GI date is only printed once per delivery in the source
           # report (a merged cell) - every other row of that same irsaliye
           # shipped the same day, it just reads blank here, so it is carried
           # forward within the group rather than left empty.
           last_gi_by_irsaliye = {}
           stamp = datetime.datetime.now().isoformat(timespec="seconds")
           rows_to_insert = []
           for r in records:
               material = clean_str(r.get("material"))
               serial_no = clean_str(r.get("serial_no"))
               if not material or not serial_no:
                   skipped += 1
                   continue
               irs = clean_str(r.get("irsaliye"))
               d = parse_date(r.get("ac_gi_date"), day_first=day_first)
               if d is not None:
                   last_gi_by_irsaliye[irs] = d
               else:
                   d = last_gi_by_irsaliye.get(irs)
               if d is None:
                   no_date += 1
               sk = shipment_serial_key(serial_no)
               row_hash = hashlib.sha1("|".join([
                   norm_text(material), sk, irs, clean_str(r.get("po_no")),
                   clean_str(r.get("poitem"))]).encode("utf-8")).hexdigest()
               rows_to_insert.append((
                   row_hash, material, norm_text(material), serial_no, sk,
                   parse_float(r.get("qty")) or None, irs,
                   d.isoformat() if d else None, clean_str(r.get("po_no")),
                   clean_str(r.get("poitem")), clean_str(r.get("sales_doc")),
                   clean_str(r.get("ship_to")), clean_str(r.get("sorg")),
                   clean_str(r.get("created_on")), stamp))
               n += 1
           before = self.conn.execute("SELECT COUNT(*) n FROM shipment").fetchone()["n"]
           self._bulk(
               "INSERT INTO shipment (row_hash,material,material_key,serial_no,"
               "serial_key,qty,irsaliye,ac_gi_date,po_no,poitem,sales_doc,"
               "ship_to,sorg,created_on,imported_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(row_hash) DO NOTHING", rows_to_insert)
           after = self.conn.execute("SELECT COUNT(*) n FROM shipment").fetchone()["n"]
           self.txn_commit()
       except BaseException:
           # An import that throws must not leave the transaction
           # open: on a shared file that would hold the write lock
           # until this copy was closed.
           self.txn_rollback()
           raise
       self.bump_version()
       self.note_upload()
       return {"loaded": after - before, "skipped": skipped,
               "duplicates": n - (after - before), "no_date": no_date,
               "ops": 0, "removed": 0}

   def sold_serial_keys(self):
       """Every serial that has ever shipped - the shop-floor's shipment
       report is the single source of truth for "sold", used both by the
       WIP filter (WIP = not sold) and by Waterfall's "Sold" status.  A
       serial counts as sold the moment its row is on file here at all -
       the Ac.GI date is used for filtering/counting by date elsewhere,
       but is not required for the sold/not-sold fact itself, since a
       report row can be missing that date (blank source cell the
       forward-fill did not reach) while still being a real shipment."""
       sold = set(self._agg("sold_serial_keys", lambda: [
           r["serial_key"] for r in self.conn.execute(
               "SELECT DISTINCT serial_key FROM shipment WHERE serial_key<>''")]))
       # The report's RTD prefix is stripped (shipment_serial_key); a
       # shop-floor serial still written RTD2G7RR is the same sold unit.
       sold.update(["RTD" + k for k in sold])
       return sold

   def sold_keys_between(self, d1=None, d2=None):
       """2.2.0: the serials whose SALE date (shipment report, Ac.GI) falls
       in d1..d2 (either end open).  With the RTD spelling too, like
       sold_serial_keys.  A shipment row without a date is in only when no
       range is asked for."""
       clauses, params = ["serial_key<>''"], []
       if d1 or d2:
           clauses += ["ac_gi_date IS NOT NULL", "ac_gi_date<>''"]
       if d1:
           clauses.append("ac_gi_date>=?")
           params.append(d1.isoformat())
       if d2:
           clauses.append("ac_gi_date<=?")
           params.append(d2.isoformat())
       sold = {r["serial_key"] for r in self.conn.execute(
           "SELECT DISTINCT serial_key FROM shipment WHERE %s"
           % " AND ".join(clauses), params)}
       sold.update(["RTD" + k for k in sold])
       return sold

   def _bulk(self, sql, rows, chunk=4000, progress=None):
       """executemany in chunks - one round trip per few thousand rows
       instead of one per row, which is what made a big import crawl.
       Committing per chunk (instead of only once at the very end) means
       a large import on a shared network file holds the write lock for
       one chunk at a time, so someone else opening the app in the
       meantime is not stuck waiting for the whole import to finish."""
       for i in range(0, len(rows), chunk):
           self.conn.executemany(sql, rows[i:i + chunk])
           # Inside an import's single transaction this must NOT commit:
           # committing half an import is what left the shared database
           # with a hole in it for everybody else.
           if not getattr(self, "_in_txn", False):
               self.conn.commit()
           # An import is the one genuinely long job in the app, and it
           # already runs in chunks - so this is where the spinner gets to
           # actually turn instead of sitting frozen.
           cb = progress or self.progress
           if cb is not None:
               cb(min(i + chunk, len(rows)), len(rows))

   def begin_bulk(self):
       """Speeds up a big import - but NEVER on a shared network file.

       synchronous = OFF tells SQLite not to wait for the filesystem to
       confirm a write.  On a local disk the worst case is losing the last
       transaction after a power cut.  On an SMB share it is worse than
       that: the connection dropping mid-import is the same thing as a
       crash, the writes that were still in flight never land, and the
       file is left MALFORMED rather than merely out of date.

       An import here is seconds, not minutes, so the speed this bought
       was never worth a corrupted shared database.  On a share it stays
       NORMAL; only a local file gets the fast path.
       """
       try:
           self.conn.commit()
           if not self.on_network:
               self.conn.execute("PRAGMA synchronous = OFF")
       except Exception:
           pass

   def end_bulk(self):
       try:
           self.conn.commit()
           self.conn.execute("PRAGMA synchronous = NORMAL")
       except Exception:
           pass

   def max_rw_hour(self):
       try:
           return float(self.get_meta("max_rw_hour", DEFAULT_MAX_RW_HOUR))
       except Exception:
           return DEFAULT_MAX_RW_HOUR

   def import_rw(self, records, replace=False, day_first=False, skip_duplicates=True):
       """
       Splits one SAP export into three groups.

       rework hour > limit : a data error - stored in `discarded` and reported
       rework hour > 0     : the rework record itself - stored in `rw`
       rework hour = 0     : a normal production step - stored in `ops`, which
                             is what tells us later which machine ran the cause
                             operation of a serial number and who operated it

       Only the rework rows take part in the analysis, so the zero rows never
       slow it down; they are only consulted through an indexed lookup.

       ADDING to what is already stored works by DATE, not by comparing
       rows: every date present in the new file is cleared out of the
       database first, and the file's own rows go in whole.  Comparing
       rows field by field looked safe but was not - a booking that had
       since been corrected in SAP counted as "different" and was added
       alongside the old one, while a re-exported row that had changed in
       some field nobody looks at silently doubled up.  Replacing whole
       days is something you can reason about: after the import, those
       days read exactly as the file says, and every other day is
       untouched.

       Rows are collected in memory and written with executemany in chunks;
       one statement per row is what made a 350,000 row file take minutes.
       """
       # What to clear is worked out here but NOT done here: the delete and
       # the insert have to land together, or everybody else on the share
       # reads a database with a hole in it - and an interrupted import
       # used to leave the deleted rows simply gone.
       wipe_all = bool(replace)
       wipe_days = []
       if not replace and skip_duplicates:
           wipe_days = sorted({r["rw_date"].isoformat() for r in records
                               if r.get("rw_date")})
           skip_duplicates = False
       limit = self.max_rw_hour()
       existing, existing_ops = {}, {}
       if skip_duplicates and not replace:
           for r in self.conn.execute(
                   "SELECT row_hash, COUNT(*) n FROM rw WHERE row_hash IS NOT NULL "
                   "GROUP BY row_hash"):
               existing[r["row_hash"]] = r["n"]
           for r in self.conn.execute(
                   "SELECT row_hash, COUNT(*) n FROM ops WHERE row_hash IS NOT NULL "
                   "GROUP BY row_hash"):
               existing_ops[r["row_hash"]] = r["n"]
       off = week_offset(self.week_start())
       stamp = datetime.datetime.now().isoformat(timespec="seconds")
       loaded = skipped = dup = no_date = ops_loaded = removed = 0
       seen, seen_ops = {}, {}
       rw_batch, ops_batch, bad_batch = [], [], []

       self.begin_bulk()
       try:
           # The delete and the insert land TOGETHER.  They used to be two
           # commits, so for the length of the import everybody else on
           # the share read a database with a hole in it - and anything
           # that interrupted the import left the deleted rows simply
           # gone, with nothing to roll back to.
           self.txn_begin()
           if wipe_all:
               self.wipe_rows("rw")
           for chunk in [wipe_days[i:i + 400]
                         for i in range(0, len(wipe_days), 400)]:
               ph = ",".join("?" * len(chunk))
               self.conn.execute(
                   "DELETE FROM rw WHERE rw_date IN (%s)" % ph, chunk)
               self.conn.execute(
                   "DELETE FROM ops WHERE op_date IN (%s)" % ph, chunk)
           for r in records:
               material = clean_str(r.get("material"))
               if not material:
                   skipped += 1
                   continue
               raw_date = r.get("rw_date")
               d = raw_date if isinstance(raw_date, datetime.date) else parse_date(
                   raw_date, day_first=day_first)
               date_s = d.isoformat() if d else ""
               r = dict(r)
               r["_date"] = date_s
               hours = parse_float(r.get("rw_hour"))
               serial = clean_str(r.get("serial_no"))
               h = row_fingerprint(r)

               # ---- production step: no rework time ----
               if hours <= 0:
                   seen_ops[h] = seen_ops.get(h, 0) + 1
                   if skip_duplicates and seen_ops[h] <= existing_ops.get(h, 0):
                       continue
                   ops_batch.append(
                       (h, material, serial, norm_text(serial),
                        clean_str(r.get("reject_op")), op_key(r.get("reject_op")),
                        clean_str(r.get("reject_op_desc")), norm_text(r.get("wc_code")),
                        clean_str(r.get("wc_desc")), clean_str(r.get("emp_no")),
                        clean_str(r.get("emp_name")), date_s or None, stamp,
                        float(r.get("std_hour") or 0.0),
                        float(r.get("machine_hour") or 0.0),
                        r["dt"].isoformat() if r.get("dt") else None,
                        seq_key(r.get("seq")),
                        1 if is_last_mark(r.get("last_x")) else 0,
                        1 if is_setup_mark(r.get("unit")) else 0,
                        r.get("labour_hour")))
                   ops_loaded += 1
                   continue

               # ---- single entry longer than the limit: data error ----
               if hours > limit:
                   bad_batch.append(
                       (material, serial, clean_str(r.get("reject_op")),
                        clean_str(r.get("cause_op")), hours, date_s,
                        norm_text(r.get("wc_code")), clean_str(r.get("emp_name")),
                        "rework hour %.2f is above the %.0f hour limit" % (hours, limit),
                        "", stamp))
                   removed += 1
                   continue

               if d is None:
                   no_date += 1
               seen[h] = seen.get(h, 0) + 1
               if skip_duplicates and seen[h] <= existing.get(h, 0):
                   dup += 1
                   continue
               iy, iw = iso_year_week(d, off)
               rw_batch.append(
                   (h, material, norm_text(material), clean_str(r.get("order_no")),
                    serial, clean_str(r.get("reject_op")),
                    clean_str(r.get("reject_op_desc")), clean_str(r.get("cause_op")),
                    clean_str(r.get("cause_code")), clean_str(r.get("cause_desc")),
                    hours, date_s or None, iy, iw,
                    norm_text(r.get("wc_code")), clean_str(r.get("wc_desc")),
                    clean_str(r.get("emp_no")), clean_str(r.get("emp_name")),
                    clean_str(r.get("entered_by")), stamp,
                    float(r.get("std_hour") or 0.0),
                    float(r.get("machine_hour") or 0.0),
                    r["dt"].isoformat() if r.get("dt") else None,
                    seq_key(r.get("seq")),
                    1 if is_last_mark(r.get("last_x")) else 0))
               loaded += 1

           if ops_batch:
               self._bulk(
                   "INSERT INTO ops (row_hash,material,serial_no,serial_key,op_no,"
                   "op_key,op_desc,wc_code,wc_desc,emp_no,emp_name,op_date,"
                   "imported_at,tot_hour,mach_hour,entry_dt,seq,last_x,setup,"
                   "lab_hour) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ops_batch)
           if bad_batch:
               self._bulk(
                   "INSERT INTO discarded (material,serial_no,reject_op,cause_op,"
                   "rw_hour,rw_date,wc_code,emp_name,reason,source,imported_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)", bad_batch)
           if rw_batch:
               self._bulk(
                   "INSERT INTO rw (row_hash,material,material_key,order_no,serial_no,"
                   "reject_op,reject_op_desc,cause_op,cause_code,cause_desc,rw_hour,"
                   "rw_date,iso_year,iso_week,wc_code,wc_desc,emp_no,emp_name,"
                   "entered_by,imported_at,tot_hour,mach_hour,entry_dt,seq,last_x) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   rw_batch)
           self.conn.execute(
               "INSERT INTO meta (key,value) VALUES (?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               ("week_applied", json.dumps(self.week_start())))
           # 2.1.4: wf_part_data is now behind rw/ops until it is rebuilt
           self.conn.execute(
               "INSERT OR REPLACE INTO meta (key,value) VALUES ('partdata_pending',?)",
               (json.dumps(stamp),))
           self.txn_commit()
       except BaseException:
           self.txn_rollback()
           raise
       finally:
           self.end_bulk()
       self.bump_version()
       self.note_upload()
       return {"loaded": loaded, "skipped": skipped, "duplicates": dup,
               "no_date": no_date, "ops": ops_loaded, "removed": removed}

   # -- operation lookup -------------------------------------------------
   def cause_op_index(self, serial_keys=None, wanted_pairs=None):
       """
       {(serial key, operation key): (work centre, machine, employee no, name)}

       Built from the production steps, so a rework record can show where its
       cause operation was carried out and by whom.

       With 350,000 production steps, walking the whole table on every single
       analysis was the main reason switching pages felt slow.  Passing the
       serials that are actually on screen turns it into an indexed lookup of
       a few hundred rows.

       `wanted_pairs` narrows it further: a serial has an entry for every
       operation on its route, but a rework row only ever asks about the
       one operation it names as the cause.  Keeping just those pairs is
       the difference between a dictionary the size of the production-step
       table and one the size of the rework table.
       """
       idx = {}
       sql = ("SELECT serial_key, op_key, wc_code, wc_desc, emp_no, emp_name, op_date "
              "FROM ops WHERE serial_key<>'' AND op_key<>''")
       if serial_keys is None:
           rows = self.conn.execute(sql + " ORDER BY op_date")
           self._fill_cause_index(idx, rows, wanted_pairs)
           return idx
       keys = [k for k in serial_keys if k]
       # Past a few thousand serials the repeated IN-lookups cost more than
       # one straight pass over the table does, so it switches strategy.
       if len(keys) > 3000:
           wanted_serials = set(keys)
           rows = self.conn.execute(sql + " ORDER BY op_date")
           self._fill_cause_index(idx, rows, wanted_pairs, wanted_serials)
           return idx
       for i in range(0, len(keys), 400):
           chunk = keys[i:i + 400]
           ph = ",".join("?" * len(chunk))
           rows = self.conn.execute(
               sql + " AND serial_key IN (%s) ORDER BY op_date" % ph, tuple(chunk))
           self._fill_cause_index(idx, rows, wanted_pairs)
       return idx

   @staticmethod
   def _fill_cause_index(idx, rows, wanted_pairs=None, wanted_serials=None):
       for r in rows:
           sk = r["serial_key"]
           if wanted_serials is not None and sk not in wanted_serials:
               continue
           key = (sk, r["op_key"])
           if wanted_pairs is not None and key not in wanted_pairs:
               continue
           value = (r["wc_code"] or "", r["wc_desc"] or "",
                    r["emp_no"] or "", r["emp_name"] or "")
           old = idx.get(key)
           # a later row wins, but never replaces filled details with blanks
           if old is None or (any(value) and (value[1] or value[3])):
               idx[key] = value

   def rework_rows_for(self, serial, op):
       """Every rework record booked on one serial at one operation."""
       want = op_key(op)
       out = []
       for r in self.conn.execute(
               "SELECT * FROM rw WHERE UPPER(REPLACE(serial_no,' ','')) = ? "
               "ORDER BY rw_date, id", (norm_text(serial),)):
           if op_key(r["reject_op"]) == want:
               out.append(r)
       return out

   def wf_last_ops(self):
       """{part_key: last operation number of that part's traveler route}."""
       out = {}
       for r in self.conn.execute(
               "SELECT part_key, op FROM wf_traveler w WHERE position = "
               "(SELECT MAX(position) FROM wf_traveler x WHERE x.part_key=w.part_key)"):
           out[r["part_key"]] = r["op"]
       return out

   def discarded_rows(self, limit=500):
       return [dict(r) for r in self.conn.execute(
           "SELECT * FROM discarded ORDER BY id DESC LIMIT ?", (limit,))]

   def discarded_count(self):
       return self._agg("discarded_count", lambda: self.conn.execute(
           "SELECT COUNT(*) n FROM discarded").fetchone()["n"])

   def ops_count(self):
       return self._agg("ops_count", lambda: self.conn.execute(
           "SELECT COUNT(*) n FROM ops").fetchone()["n"])

   def all_parts(self, merge=False):
       """The PART DATA list.  merge=True: the parts put together on the
       DATA page ("1000A10G02/G03") come back as ONE part, carrying the
       first member's group and engineer and a `members` list."""
       base = [dict(r) for r in self._cached_rows(
           "SELECT * FROM parts ORDER BY part_group, part_no")]
       if not merge:
           return base
       groups = self.part_merges()
       if not groups:
           return base
       member_of = {m: g for g in groups for m in g["members"]}
       by_key = {(p["part_key"] or norm_text(p["part_no"])): p for p in base}
       out, done = [], set()
       for p in base:
           g = member_of.get(p["part_key"] or norm_text(p["part_no"]))
           if g is None:
               out.append(p)
               continue
           if g["key"] in done:
               continue
           done.add(g["key"])
           out.append(self._merged_part(g, by_key))
       for g in groups:
           if g["key"] not in done:
               out.append(self._merged_part(g, by_key))
       return out

   def _merged_part(self, g, by_key):
       first = next((by_key[m] for m in g["members"] if m in by_key), None)
       p = dict(first) if first else {"part_group": "", "engineer": ""}
       p.update(part_no=g["name"], part_key=g["key"],
                members=list(g["members"]))
       return p

   def engineers(self):
       return [r["engineer"] for r in self._cached_rows(
           "SELECT DISTINCT engineer FROM parts WHERE engineer<>'' ORDER BY engineer")]

   def serials(self):
       return self._agg("rw_serials", lambda: [
           r["serial_no"] for r in self.conn.execute(
               "SELECT DISTINCT serial_no FROM rw WHERE serial_no<>'' "
               "ORDER BY serial_no")])

   def all_serial_numbers(self):
       """Every serial number on file anywhere - production steps (ops)
       plus rework entries (rw) - since a serial worth flagging MRB/SCRAP/
       TECO may never have had a rework booking at all.

       Two whole-table passes, so worked out once per data version and
       kept in agg_cache (v67) like the other start-up answers."""
       return self._agg("all_serials", lambda: sorted({
           r["serial_no"] for r in self.conn.execute(
               "SELECT DISTINCT serial_no FROM ops WHERE serial_no<>'' "
               "UNION SELECT DISTINCT serial_no FROM rw WHERE serial_no<>''")}))

   def comp_map(self):
       return {r["work_center"]: (r["wc_name"], r["comp"] or UNKNOWN)
               for r in self._cached_rows("SELECT * FROM comp")}

   # -----------------------------------------------------------------
   #  MRB / SCRAP / TECO serial flags
   # -----------------------------------------------------------------
   def flag_set(self, serial_key, serial_no, kind, reason, entry_date,
               mrb_prob="", scrap_prob="", entered_by="", part_no=None):
       # This is somebody typing a judgement about a part, and it is the
       # one kind of data in here that no SAP export can rebuild.  It
       # waits out another user's write rather than failing: losing the
       # note is far worse than waiting for it.
       now = datetime.datetime.now().isoformat(timespec="seconds")
       row = self.flag_get(serial_key)
       if part_no is None:                 # not given: keep what was typed
           part_no = (row or {}).get("part_no") or ""
       # The note and the "look again" signal go in TOGETHER - one lock,
       # one flush.  They were two separate writes, and this is the thing
       # people do all day long.
       self.write_batch([
           ("INSERT INTO serial_flags (serial_key,serial_no,kind,reason,"
            "entry_date,mrb_prob,scrap_prob,entered_by,created_at,updated_at,"
            "part_no) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(serial_key) DO UPDATE SET serial_no=excluded.serial_no,"
            "kind=excluded.kind, reason=excluded.reason, "
            "entry_date=excluded.entry_date, mrb_prob=excluded.mrb_prob, "
            "scrap_prob=excluded.scrap_prob, entered_by=excluded.entered_by, "
            "updated_at=excluded.updated_at, part_no=excluded.part_no",
            (serial_key, serial_no, kind, reason, entry_date, mrb_prob,
             scrap_prob, entered_by, (row["created_at"] if row else now), now,
             part_no)),
           (self.BUMP_META_SQL, ("annotation_version",)),
       ])

   def flag_resolve(self, serial_key, resolution, by=""):
       """A case reaching an outcome.

       'mrb' / 'scrap' moves the same row into that module instead of
       creating a new one, so its reason, date and probabilities stay
       attached.

       'fixed' USED TO DELETE THE ROW, which threw away the only record
       that the problem had ever existed - and with it the answer to "how
       many MRB parts did we put right this year".  It now keeps the row,
       marks it fixed, and remembers WHAT IT WAS before it was fixed.
       """
       now = datetime.datetime.now().isoformat(timespec="seconds")
       if resolution == "fixed":
           row = self.flag_get(serial_key)
           was = (row or {}).get("kind") or ""
           yazi = ("UPDATE serial_flags SET kind='fixed', resolved_from=?, "
                   "resolved_at=?, resolved_by=?, updated_at=? "
                   "WHERE serial_key=?", (was, now, by, now, serial_key))
       else:
           yazi = ("UPDATE serial_flags SET kind=?, updated_at=? "
                   "WHERE serial_key=?", (resolution, now, serial_key))
       # One transaction, like flag_set - see write_batch.
       self.write_batch([yazi, (self.BUMP_META_SQL, ("annotation_version",))])

   def flag_delete(self, serial_key):
       # One transaction, like flag_set - see write_batch.
       self.write_batch([
           ("DELETE FROM serial_flags WHERE serial_key=?", (serial_key,)),
           (self.BUMP_META_SQL, ("annotation_version",)),
       ])

   # -- undo / redo on the MRB / SCRAP / TECO page (v66) ----------------
   # A snapshot is the serial's flag row and the user's own note row,
   # exactly as stored (None = no row).  Putting one back rewrites both
   # rows in one transaction, so an undo can never leave half a change.
   def flag_snapshot(self, serial_key, username=None):
       f = self.conn.execute("SELECT * FROM serial_flags WHERE serial_key=?",
                             (serial_key,)).fetchone()
       n = None
       if username:
           n = self.conn.execute(
               "SELECT * FROM user_note WHERE serial_key=? AND username=?",
               (serial_key, username)).fetchone()
       return {"serial_key": serial_key, "username": username,
               "flag": dict(f) if f else None, "note": dict(n) if n else None}

   def flag_restore(self, snap):
       sk, user = snap["serial_key"], snap.get("username")
       yazi = [("DELETE FROM serial_flags WHERE serial_key=?", (sk,))]
       for tablo, row in (("serial_flags", snap.get("flag")),):
           if row:
               cols = list(row)
               yazi.append(("INSERT INTO %s (%s) VALUES (%s)"
                            % (tablo, ",".join(cols), ",".join("?" * len(cols))),
                            tuple(row[c] for c in cols)))
       if user:
           yazi.append(("DELETE FROM user_note WHERE serial_key=? AND username=?",
                        (sk, user)))
           row = snap.get("note")
           if row:
               cols = list(row)
               yazi.append(("INSERT INTO user_note (%s) VALUES (%s)"
                            % (",".join(cols), ",".join("?" * len(cols))),
                            tuple(row[c] for c in cols)))
       yazi.append((self.BUMP_META_SQL, ("annotation_version",)))
       self.write_batch(yazi)

   def flag_get(self, serial_key):
       r = self.conn.execute(
           "SELECT * FROM serial_flags WHERE serial_key=?", (serial_key,)).fetchone()
       return dict(r) if r else None

   def flag_list(self, kind):
       return [dict(r) for r in self._cached_rows(
           "SELECT * FROM serial_flags WHERE kind=? ORDER BY updated_at DESC",
           (kind,))]

   def flag_conflicts(self, records):
       """Which serials in the file already hold a record here.

       A flag record is somebody's judgement about a part, typed by hand -
       overwriting one silently is not something an import should do on
       its own.  This is what the upload asks about before it writes.

       Returns [{serial, old_kind, old_reason, new_kind, new_reason}, ...].
       """
       out = []
       for r in records:
           serial = clean_str(r.get("serial_no"))
           if not serial:
               continue
           cur = self.flag_get(norm_text(serial))
           if cur:
               out.append({"serial": serial,
                           "old_kind": cur["kind"], "old_reason": cur["reason"] or "",
                           "new_kind": r.get("kind") or "",
                           "new_reason": r.get("reason") or ""})
       return out

   def flag_import(self, records, replace=False, overwrite=True):
       """Loads exported flag records back in.

       A serial holds one record, so a row in the file replaces whatever
       that serial had - the same rule the page itself follows.  CHANGE
       clears the lot first; ADD leaves serials the file does not mention
       alone.

       `overwrite=False` keeps what is already here and only writes serials
       that have no record yet, for when the answer to "this file would
       change 12 existing records" is no.

       Returns {"added": n, "updated": n, "kept": n}.
       """
       if replace:
           self.conn.execute("DELETE FROM serial_flags")
       now = datetime.datetime.now().isoformat(timespec="seconds")
       tally = {"added": 0, "updated": 0, "kept": 0}
       for r in records:
           serial = clean_str(r.get("serial_no"))
           if not serial:
               continue
           existing = None if replace else self.flag_get(norm_text(serial))
           if existing and not overwrite:
               tally["kept"] += 1
               continue
           self.conn.execute(
               "INSERT INTO serial_flags (serial_key,serial_no,kind,reason,"
               "entry_date,mrb_prob,scrap_prob,entered_by,created_at,"
               "updated_at,part_no) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(serial_key) DO UPDATE SET "
               "serial_no=excluded.serial_no, kind=excluded.kind, "
               "reason=excluded.reason, entry_date=excluded.entry_date, "
               "mrb_prob=excluded.mrb_prob, scrap_prob=excluded.scrap_prob, "
               "entered_by=excluded.entered_by, updated_at=excluded.updated_at, "
               "part_no=excluded.part_no",
               (norm_text(serial), serial, r.get("kind") or "note",
                r.get("reason") or "", r.get("entry_date") or "",
                r.get("mrb_prob") or "", r.get("scrap_prob") or "",
                r.get("entered_by") or "", now, now,
                r.get("part_no") or ""))
           tally["updated" if existing else "added"] += 1
       self.conn.commit()
       self.bump_annotations()
       return tally

   def flag_rows(self):
       """Every record in one (cached) read - the MRB/SCRAP/TECO page
       sorts them into kinds itself instead of asking once per kind."""
       return [dict(r) for r in self._cached_rows(
           "SELECT * FROM serial_flags ORDER BY updated_at DESC")]

   def serial_last_dates(self):
       """{serial_key: date of its last booking} for every serial - what
       the flag list's LAST OP DATE column shows.  One pass, kept in
       agg_cache per data version (v67), so the other copies read one row
       instead of grouping the production steps again."""
       return self._agg("serial_last_dates", lambda: {
           r["serial_key"]: r["op_date"] for r in self.latest_booking()
           if r["serial_key"]})

   def serial_last_points(self):
       """{serial_key: (date, op_no)} of the newest booking of every serial -
       the MRB / SCRAP / TECO list's LAST OP and LAST OP DATE (v69).  The
       same latest_booking() pass as serial_last_dates, cached the same way."""
       return self._agg("serial_last_points", lambda: {
           r["serial_key"]: (r["op_date"], r["op_no"]) for r in self.latest_booking()
           if r["serial_key"]})

   def flag_all(self):
       """Every record, newest first - what the export writes."""
       return [dict(r) for r in self.conn.execute(
           "SELECT * FROM serial_flags ORDER BY updated_at DESC")]

   def flag_map(self):
       """{serial_key: kind} for every flagged serial - the cheap lookup
       every analysis page's row-colouring and exclude-checkboxes use."""
       # v73: a SCRAP taken off the books (fixed, was scrap) reads as
       # "removed" here, so the pages colour and filter it on its own
       return {r["serial_key"]: r["kind"] for r in
               self._cached_rows(
                   "SELECT serial_key, CASE WHEN kind='fixed' AND resolved_from='scrap' "
                   "THEN 'removed' ELSE kind END AS kind FROM serial_flags")}

   def flag_keys(self, kinds):
       """The serials whose flag is one of `kinds` - what every page's
       EXCLUDE / ONLY buttons mean (see flag_kind_in)."""
       kinds = set(kinds or ())
       if not kinds:
           return set()
       return {sk for sk, k in self.flag_map().items() if flag_kind_in(k, kinds)}

   # -- accounts --------------------------------------------------------
   def seed_users(self):
       """Puts the defined accounts into a database that does not have them
       yet.  Existing rows are left completely alone - re-running this must
       never reset somebody's password or demote them."""
       have = {r["username"] for r in
               self.conn.execute("SELECT username FROM app_user")}
       now = datetime.datetime.now().isoformat(timespec="seconds")
       new = [(u, full, role, "", "", 1, now, None)
              for u, full, role in SEED_USERS if u not in have]
       if new and self.read_only:
           return 0            # nothing to add them to; sign-in still works
       if new:
           self.conn.executemany(
               "INSERT INTO app_user (username,full_name,role,pw_hash,"
               "pw_salt,active,created_at,last_login) VALUES (?,?,?,?,?,?,?,?)",
               new)
           self.conn.commit()
       self.enforce_owner()
       return len(new)

   def users(self, active_only=True):
       sql = "SELECT * FROM app_user"
       if active_only:
           sql += " WHERE active=1"
       return [dict(r) for r in
               self.conn.execute(sql + " ORDER BY full_name")]

   def user(self, username):
       r = self.conn.execute("SELECT * FROM app_user WHERE username=?",
                             (uname(username),)).fetchone()
       return dict(r) if r else None

   # -- per-account preferences (v73) ------------------------------------
   # Kept in the shared file so they follow the PERSON to any computer
   # (day / night so far).  A meta key of their own that does not move
   # settings_version: nobody else's copy has to reload for it.
   def user_pref(self, username):
       v = self.get_meta("user_pref:" + uname(username), {})
       return v if isinstance(v, dict) else {}

   def set_user_pref(self, username, key, value):
       """One short write; a busy file is skipped (the choice still holds
       on this computer, and is stored the next time)."""
       if self.read_only or not username:
           return False
       prefs = dict(self.user_pref(username))
       if prefs.get(key) == value:
           return True
       prefs[key] = value
       try:
           self.conn.execute("PRAGMA busy_timeout = 300")
       except sqlite3.Error:
           pass
       try:
           self.conn.execute("INSERT INTO meta (key,value) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             ("user_pref:" + uname(username), json.dumps(prefs, ensure_ascii=False)))
           self.conn.commit()
           return True
       except sqlite3.Error:
           try:
               self.conn.rollback()
           except sqlite3.Error:
               pass
           return False
       finally:
           try:
               self.conn.execute("PRAGMA busy_timeout = 1500")
           except sqlite3.Error:
               pass

   def set_password(self, username, password):
       salt = secrets.token_hex(16)
       self._write_waiting(lambda: self.conn.execute(
           "UPDATE app_user SET pw_hash=?, pw_salt=? WHERE username=?",
           (hash_password(password, salt), salt, uname(username))))

   def check_password(self, username, password):
       u = self.user(username)
       if not u or not u["pw_hash"]:
           return False
       return secrets.compare_digest(
           hash_password(password, u["pw_salt"]), u["pw_hash"])

   def note_login(self, username):
       """The "last sign-in" stamp.  It is only shown in the user list, so
       a file that stays busy must not stop anybody from signing in."""
       if self.read_only:
           return
       stamp = datetime.datetime.now().isoformat(timespec="seconds")
       try:
           self._write_waiting(lambda: self.conn.execute(
               "UPDATE app_user SET last_login=? WHERE username=?",
               (stamp, uname(username))), seconds=8.0)
       except sqlite3.Error:
           pass

   def _guard_owner(self, username, what):
       """Nobody changes the one admin's account (see OWNER_USERNAME)."""
       if uname(username) == OWNER_USERNAME:
           raise PermissionError(
               "%s belongs to the admin and cannot be changed." % what)

   def set_user_role(self, username, role):
       self._guard_owner(username, "The role")
       if role == "admin":
           raise PermissionError("There is only one admin; choose CO-ADMIN.")
       if role not in ROLE_TITLE:
           raise ValueError("Unknown role: %s" % role)
       self._write_waiting(lambda: self.conn.execute(
           "UPDATE app_user SET role=? WHERE username=?", (role, uname(username))))

   def set_user_active(self, username, active):
       self._guard_owner(username, "Switching the account on or off")
       self._write_waiting(lambda: self.conn.execute(
           "UPDATE app_user SET active=? WHERE username=?",
           (1 if active else 0, uname(username))))

   def enforce_owner(self):
       """The owner is ADMIN and switched on; nobody else is ADMIN (an
       older database or an imported file may say otherwise - such an
       account becomes CO-ADMIN).  Returns True when something changed."""
       if self.read_only:
           return False
       cur = self.conn.execute(
           "SELECT COUNT(*) FROM app_user WHERE (username=? AND "
           "(role<>'admin' OR active<>1)) OR (username<>? AND role='admin')",
           (OWNER_USERNAME, OWNER_USERNAME)).fetchone()[0]
       if not cur:
           return False
       self.conn.execute("UPDATE app_user SET role='coadmin' "
                         "WHERE username<>? AND role='admin'", (OWNER_USERNAME,))
       self.conn.execute("UPDATE app_user SET role='admin', active=1 "
                         "WHERE username=?", (OWNER_USERNAME,))
       self.conn.commit()
       return True

   def clear_password(self, username):
       """Admin's reset - the next sign-in makes them choose a new one."""
       self._guard_owner(username, "The password")
       self._write_waiting(lambda: self.conn.execute(
           "UPDATE app_user SET pw_hash='', pw_salt='' WHERE username=?",
           (uname(username),)))

   def users_export(self, path):
       """Writes every account - including its password - to a JSON file.

       The accounts live in the shared database and nowhere else.  Lose
       that file and you lose not just the data but everybody's way in:
       each person has to be defined again and each one has to pick a new
       password, which is a bad morning on top of an already bad one.

       WHAT IS IN THE FILE.  The stored password is a salted hash, never
       the password itself - nothing here can turn it back into the text
       somebody types.  But the hash IS what signs a person in, so the
       file is still a key to every account: keep it where the database
       itself is not, and where other people are not.

       Inactive accounts go in too.  A restore that quietly dropped the
       people somebody had switched off would switch them all back on.
       """
       rows = [dict(r) for r in self.conn.execute(
           "SELECT username, full_name, role, pw_hash, pw_salt, active, "
           "created_at, last_login FROM app_user ORDER BY username")]
       doc = {
           "format": "wip-tracker-users",
           "format_version": 1,
           "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "database": os.path.basename(self.path or ""),
           "note": "Contains salted password hashes - keep this file safe.",
           "users": rows,
       }
       with open(path, "w", encoding="utf-8") as f:
           json.dump(doc, f, ensure_ascii=False, indent=1)
       return {"path": path, "users": len(rows),
               "with_password": sum(1 for r in rows if r.get("pw_hash"))}

   def users_import(self, path, replace=False, by=None):
       """Reads accounts back from a file written by `users_export`.

       Two ways, and the difference matters after a loss:

         replace=False (the safe one)  Accounts in the file that are not
             here are added.  Accounts that ARE here are left exactly as
             they are - nobody's password or role is touched.  This is
             what you want when somebody has been added since the backup.

         replace=True                  The file wins: every account in it
             is written over the one here, password and role and all.
             This is the restore after a database has been rebuilt from
             nothing.

       Accounts here that the file does not mention are never deleted,
       either way.  Deleting somebody because they joined after the backup
       was taken would be the worst thing this could do.
       """
       with open(path, "r", encoding="utf-8") as f:
           doc = json.load(f)
       if not isinstance(doc, dict) or doc.get("format") != "wip-tracker-users":
           raise ValueError("This is not a WIP TRACKER user file.")
       rows = doc.get("users") or []
       if not rows:
           raise ValueError("The file carries no accounts.")
       have = {r["username"] for r in
               self.conn.execute("SELECT username FROM app_user")}
       added = updated = skipped = 0
       now = datetime.datetime.now().isoformat(timespec="seconds")
       for r in rows:
           u = uname(r.get("username") or "")
           if not u:
               continue
           # The admin's own account is never written from a file unless
           # the admin is the one loading it (v67) - except on a database
           # rebuilt from nothing, where it has no password yet and the
           # backup is the only way back in.  Its role stays ADMIN either
           # way (enforce_owner below).
           if (u == OWNER_USERNAME and u in have
                   and uname(by or "") != OWNER_USERNAME
                   and (self.user(u) or {}).get("pw_hash")):
               skipped += 1
               continue
           vals = (r.get("full_name") or "", r.get("role") or "user",
                   r.get("pw_hash") or "", r.get("pw_salt") or "",
                   1 if r.get("active", 1) else 0,
                   r.get("created_at") or now, r.get("last_login"))
           if u not in have:
               self.conn.execute(
                   "INSERT INTO app_user (username,full_name,role,pw_hash,"
                   "pw_salt,active,created_at,last_login) "
                   "VALUES (?,?,?,?,?,?,?,?)", (u,) + vals)
               added += 1
           elif replace:
               self.conn.execute(
                   "UPDATE app_user SET full_name=?, role=?, pw_hash=?, "
                   "pw_salt=?, active=?, created_at=?, last_login=? "
                   "WHERE username=?", vals + (u,))
               updated += 1
           else:
               skipped += 1
       self.conn.commit()
       self.enforce_owner()
       return {"added": added, "updated": updated, "skipped": skipped,
               "in_file": len(rows),
               "exported_at": doc.get("exported_at") or ""}

   # -- B1000 floor plan -------------------------------------------------
   def layout_active(self, name="B1000"):
       """The version everybody opens, or None if none has been saved."""
       r = self.conn.execute(
           "SELECT * FROM layout WHERE name=? AND active=1 "
           "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
       return dict(r) if r else None

   def layout_versions(self, name="B1000"):
       return [dict(r) for r in self.conn.execute(
           "SELECT id, version, saved_by, saved_at, note, active FROM layout "
           "WHERE name=? ORDER BY version DESC", (name,))]

   def layout_items(self, layout_id):
       return [dict(r) for r in self.conn.execute(
           "SELECT * FROM layout_item WHERE layout_id=? ORDER BY z, id",
           (layout_id,))]

   def layout_save(self, name, canvas, items, saved_by="", note=""):
       """Writes a new version and makes it the active one.

       The whole plan goes in one transaction: a half-written layout that
       somebody else opens mid-save would be worse than no layout at all.
       """
       if self.read_only:
           return None
       cur = self.conn.execute(
           "SELECT COALESCE(MAX(version), 0) FROM layout WHERE name=?",
           (name,)).fetchone()[0]
       ver = int(cur) + 1
       now = datetime.datetime.now().isoformat(timespec="seconds")
       # The plan is hand-drawn work; waiting for somebody else's write is
       # always better than refusing to save it.
       with_lock_retry(lambda: self.conn.execute("BEGIN IMMEDIATE"),
                       seconds=WRITE_WAIT_SECONDS)
       try:
           self.conn.execute("UPDATE layout SET active=0 WHERE name=?", (name,))
           c = self.conn.execute(
               "INSERT INTO layout (name,canvas_w,canvas_h,version,active,"
               "saved_by,saved_at,note) VALUES (?,?,?,?,1,?,?,?)",
               (name, int(canvas[0]), int(canvas[1]), ver, saved_by, now, note))
           lid = c.lastrowid
           self.conn.executemany(
               "INSERT INTO layout_item (layout_id,kind,asset,label,wc_code,"
               "ops,groups,part_groups,x,y,w,h,rot,color,z,carry,locked,"
               "people) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               [(lid, it.get("kind", ""), it.get("asset", ""),
                 it.get("label", ""), it.get("wc_code", ""),
                 ",".join(str(o) for o in it.get("ops", []) if str(o).strip()),
                 ",".join(str(g) for g in it.get("groups", []) if str(g).strip()),
                 ",".join(str(g) for g in it.get("part_groups", [])
                          if str(g).strip()),
                 it["x"], it["y"], it["w"], it["h"], it.get("rot", 0),
                 it.get("color", ""), i,
                 1 if it.get("carry", True) else 0,
                 1 if it.get("locked") else 0,
                 int(it.get("people") or 0))
                for i, it in enumerate(items)])
           self.conn.execute("COMMIT")
       except Exception:
           self.conn.execute("ROLLBACK")
           raise
       self.bump_version()
       return lid

   def layout_export(self, path, layout_id=None, name="B1000"):
       """Writes one saved plan to a JSON file on this machine.

       The floor plan is drawn by hand and lives nowhere else - no Excel
       export rebuilds it.  Losing the shared database loses it, so this
       puts a copy somewhere that does not share the database's fate.

       JSON rather than a database copy on purpose: it is one small text
       file, it can be read and diffed years later without this program,
       and restoring it does not touch anything else in the database.
       """
       lay = (dict(self.conn.execute("SELECT * FROM layout WHERE id=?",
                                     (layout_id,)).fetchone())
              if layout_id else self.layout_active(name))
       if not lay:
           return None
       items = self.layout_items(lay["id"])
       for it in items:
           it.pop("id", None)
           it.pop("layout_id", None)
       doc = {
           "format": "wip-tracker-layout",
           "format_version": 1,
           "name": lay.get("name") or name,
           "canvas_w": lay.get("canvas_w"), "canvas_h": lay.get("canvas_h"),
           "version": lay.get("version"),
           "saved_by": lay.get("saved_by") or "",
           "saved_at": lay.get("saved_at") or "",
           "note": lay.get("note") or "",
           "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "items": items,
       }
       with open(path, "w", encoding="utf-8") as f:
           json.dump(doc, f, ensure_ascii=False, indent=1)
       return {"path": path, "items": len(items), "version": lay.get("version"),
               "name": doc["name"]}

   def layout_import(self, path, saved_by=""):
       """Reads a plan back and saves it as a NEW version.

       Nothing is overwritten: the file becomes the next version and the
       active one, and every version already on file stays where it is, so
       a restore can itself be undone from the version list.
       """
       with open(path, "r", encoding="utf-8") as f:
           doc = json.load(f)
       if not isinstance(doc, dict) or doc.get("format") != "wip-tracker-layout":
           raise ValueError("This is not a WIP TRACKER layout file.")
       items = doc.get("items") or []
       if not items:
           raise ValueError("The file carries no plan items.")
       for it in items:
           # The file stores these as the comma-joined text the table holds;
           # layout_save expects lists, so they are split back here.
           for key in ("ops", "groups", "part_groups"):
               v = it.get(key)
               if isinstance(v, str):
                   it[key] = [p for p in v.split(",") if p.strip()]
               elif v is None:
                   it[key] = []
           it["carry"] = bool(it.get("carry", 1))
           it["locked"] = bool(it.get("locked", 0))
           try:
               it["people"] = int(it.get("people") or 0)
           except Exception:
               it["people"] = 0
       canvas = (int(doc.get("canvas_w") or 2000),
                 int(doc.get("canvas_h") or 1200))
       note = "restored from %s" % os.path.basename(path)
       if doc.get("version") is not None:
           note += " (was version %s)" % doc["version"]
       lid = self.layout_save(doc.get("name") or "B1000", canvas, items,
                              saved_by=saved_by, note=note)
       return {"layout_id": lid, "items": len(items),
               "name": doc.get("name") or "B1000",
               "was_version": doc.get("version"),
               "saved_at": doc.get("saved_at") or ""}

   # -- open sessions ---------------------------------------------------
   def session_open(self, username):
       """Registers this running copy and returns its id, or None when the
       file cannot be written - being unable to announce yourself is not a
       reason to refuse to run."""
       if self.read_only:
           return None
       sid = secrets.token_hex(8)
       now = datetime.datetime.now().isoformat(timespec="seconds")
       try:
           host = socket.gethostname()
       except Exception:
           host = "?"
       try:
           self._write_waiting(lambda: self.conn.execute(
               "INSERT INTO " + self.T_SESS + " (session_id,username,host,pid,started_at,"
               "last_seen,close_requested,closed_at) VALUES (?,?,?,?,?,?,NULL,NULL)",
               (sid, username or "", host, os.getpid(), now, now)), seconds=10.0)
       except sqlite3.Error:
           return None      # runs without a session row rather than not at all
       return sid

   def session_beat(self, session_id, username=None):
       if self.read_only or not session_id:
           return None
       """Keeps this copy's row alive and reports back whether an admin has
       asked it to close.  One small write every poll - cheap enough to run
       on the same timer the shared-change check already uses."""
       now = datetime.datetime.now().isoformat(timespec="seconds")
       # v69: ONE attempt (the connection's own short busy wait).  The
       # heartbeat runs on the screen's timer; waiting out somebody's long
       # upload here froze every other copy.  A missed beat is harmless -
       # the next one comes in forty seconds.
       try:
           if username is None:
               self.conn.execute(
                   "UPDATE " + self.T_SESS + " SET last_seen=? WHERE session_id=?",
                   (now, session_id))
           else:
               self.conn.execute(
                   "UPDATE " + self.T_SESS + " SET last_seen=?, username=? "
                   "WHERE session_id=?", (now, username, session_id))
           self.conn.commit()
       except sqlite3.Error:
           try:
               self.conn.rollback()
           except sqlite3.Error:
               pass
           return None      # busy: the close request is read next time
       return self.session_close_requested(session_id)

   def session_close_requested(self, session_id):
       """Has an admin asked this copy to close?  A READ, nothing else.

       The heartbeat is a write, and on a shared folder every write is a
       lock somebody may be waiting on, so it only runs every fifth poll.
       Asking whether an admin wants this copy gone costs nothing, so it
       is asked every poll - otherwise "close selected" took the best part
       of a minute to do anything and looked broken, while the dialog next
       to it promised about ten seconds.
       """
       if not session_id:
           return None
       # v69: one read with the connection's short busy wait, not the
       # thirty-second patient read - this is asked every few seconds from
       # the screen's timer, and a busy file must not freeze the screen.
       try:
           r = self.conn.execute("SELECT close_requested FROM " + self.T_SESS + " "
                                 "WHERE session_id=?", (session_id,)).fetchone()
       except sqlite3.Error:
           return None
       return r["close_requested"] if r else None

   def session_end(self, session_id):
       if self.read_only or not session_id:
           return
       self._write_waiting(lambda: self.conn.execute(
           "DELETE FROM " + self.T_SESS + " WHERE session_id=?", (session_id,)), seconds=5.0)

   def all_sessions(self, keep_seconds=None):
       """Every session row worth looking at, newest first.

       live_sessions() hides the stale ones, which is right for deciding
       "is anybody else in" - but wrong for an admin looking at the list
       and asking why a copy will not close.  A row that has stopped
       answering is exactly what they need to see, so stale rows are
       still returned and the screen says which is which.

       WHAT IS NOT RETURNED IS THE LONG DEAD.  A copy that was closed or
       crashed days ago left its row behind, and that row sat on the
       screen for ever: the list filled up with people who went home on
       Monday, which is the opposite of what a screen called OPEN
       SESSIONS is for.

       It sat there because nothing was clearing it.  The tidy-up lived
       in live_sessions(), and live_sessions() is only reached by the
       CLOSE ALL OTHERS button - so on a copy where nobody pressed that
       button, stale rows were never deleted at all.  The tidy-up
       belongs where the list is actually READ, which is here.

       Deleting is a write on a file other people are using, so it
       happens at most once a minute; the filter below is what keeps the
       answer honest in between.
       """
       kes = SESSION_FORGET_SECONDS if keep_seconds is None else keep_seconds
       cutoff = (datetime.datetime.now()
                 - datetime.timedelta(seconds=kes)
                 ).isoformat(timespec="seconds")
       now = time.time()
       if (not self.read_only
               and now - getattr(self, "_session_purged", 0.0) > 60.0):
           self._session_purged = now
           try:
               self.write_sql("DELETE FROM " + self.T_SESS + " WHERE last_seen<?",
                              (cutoff,))
           except sqlite3.Error:
               pass
       rows = self.rows(
           "SELECT s.*, u.full_name FROM " + self.T_SESS + " s "
           "LEFT JOIN app_user u ON u.username = s.username "
           "WHERE s.last_seen>=? ORDER BY s.last_seen DESC", (cutoff,))
       return [dict(r) for r in rows]

   def live_sessions(self):
       """Every copy whose heartbeat is still fresh, newest first.  Stale
       rows are cleared out on the way past, so the list stays honest
       without anything having to tidy up on a schedule."""
       cutoff = (datetime.datetime.now()
                 - datetime.timedelta(seconds=SESSION_STALE_SECONDS)
                 ).isoformat(timespec="seconds")
       # Tidying stale rows is a WRITE, and this is called every few
       # seconds while the admin dialog is open - which turned looking at
       # the list into a write every six seconds on a file other people
       # are trying to use.  Stale rows are filtered out of the answer
       # regardless; the delete only needs to happen now and then.
       now = time.time()
       if (not self.read_only
               and now - getattr(self, "_session_purged", 0.0) > 60.0):
           self._session_purged = now
           try:
               self.write_sql("DELETE FROM " + self.T_SESS + " WHERE last_seen<?",
                              (cutoff,))
           except sqlite3.Error:
               pass
       rows = self.rows(
           "SELECT s.*, u.full_name FROM " + self.T_SESS + " s "
           "LEFT JOIN app_user u ON u.username = s.username "
           "WHERE s.last_seen>=? ORDER BY s.last_seen DESC", (cutoff,))
       return [dict(r) for r in rows]

   # -- maintenance mode ------------------------------------------------
   #
   # "Everybody out" as a STATE, not a message.  Asking each copy to close
   # one at a time only reaches the copies that are running and polling
   # right now; somebody who opens the program thirty seconds later walks
   # straight back into the file an admin is trying to replace.
   #
   # With maintenance mode on: every copy closes itself on its next poll,
   # and a copy that starts refuses to open - except for an admin, who
   # must be able to get in to turn it off again.
   def maintenance(self):
       """(on, who turned it on, when) - off is (False, '', '')."""
       raw = self.get_meta("maintenance", "") or ""
       if not raw:
           return False, "", ""
       parts = str(raw).split("|")
       return True, (parts[1] if len(parts) > 1 else ""), parts[0]

   def set_maintenance(self, on, by=""):
       if on:
           self.set_meta("maintenance", "%s|%s" % (
               datetime.datetime.now().isoformat(timespec="seconds"), by))
       else:
           self.set_meta("maintenance", "")

   def request_session_close(self, session_ids, by=""):
       """Asks those copies to shut themselves down.  It is a request in the
       database, not a kill: the other program notices it on its next poll
       and closes itself cleanly, so nobody loses a half-finished import."""
       stamp = "%s|%s" % (datetime.datetime.now().isoformat(timespec="seconds"),
                          by)
       self._write_waiting(lambda: self.conn.executemany(
           "UPDATE " + self.T_SESS + " SET close_requested=? WHERE session_id=?",
           [(stamp, sid) for sid in session_ids]))
       return len(session_ids)

   # -- per-user notes --------------------------------------------------
   # A note is shared by default: whoever writes it can tick "hide from
   # others" and only then does it become theirs alone.  The colour is
   # always the writer's own choice and travels with the note.
   def set_user_note(self, serial_key, username, note, color=None,
                     private=False):
       now = datetime.datetime.now().isoformat(timespec="seconds")
       if not (note or "").strip():
           yazi = ("DELETE FROM user_note WHERE serial_key=? AND username=?",
                   (serial_key, username))
       else:
           yazi = ("INSERT INTO user_note (serial_key,username,note,color,"
                   "private,created_at,updated_at) VALUES (?,?,?,?,?,?,?) "
                   "ON CONFLICT(serial_key,username) DO UPDATE SET "
                   "note=excluded.note, color=excluded.color, "
                   "private=excluded.private, updated_at=excluded.updated_at",
                   (serial_key, username, note.strip(),
                    color or USER_NOTE_COLORS[0][1],
                    1 if private else 0, now, now))
       # The note and the signal together - see write_batch.
       self.write_batch([yazi, (self.BUMP_META_SQL, ("annotation_version",))])

   def user_note_row(self, serial_key, username):
       """The writer's own note, with its colour and whether it is hidden
       - what the entry form and the serial popup load back."""
       r = self.conn.execute(
           "SELECT note, color, private FROM user_note "
           "WHERE serial_key=? AND username=?",
           (serial_key, username)).fetchone()
       return (dict(r) if r else
               {"note": "", "color": USER_NOTE_COLORS[0][1], "private": 0})

   def _note_visibility_sql(self, username, all_users, prefix=""):
       """The WHERE fragment for "notes this account is allowed to see".

       A note is shared unless its writer ticked "hide from others";
       everyone therefore sees their own notes plus everybody else's
       shared ones.  `all_users` is the admin's deliberate override and
       lifts the last restriction.
       """
       if all_users:
           return "1=1", []
       p = prefix
       return "(%susername=? OR COALESCE(%sprivate,0)=0)" % (p, p), [username]

   def user_note_colors(self, username, all_users=False):
       """{serial_key: colour} for the notes this account may see - one
       query, so a page can colour its serials without asking per row.

       Where two people wrote on the same serial the reader's own note
       wins, so the colour a person picked stays the colour they see.
       """
       where, params = self._note_visibility_sql(username, all_users)
       rows = self.conn.execute(
           "SELECT serial_key, color, username FROM user_note WHERE %s "
           "ORDER BY (username=?) ASC, updated_at ASC" % where,
           params + [username]).fetchall()
       return {r["serial_key"]: (r["color"] or USER_NOTE_COLORS[0][1])
               for r in rows}

   def notes_for_serial(self, serial_key, username, all_users=False):
       """Every note on this serial that this account may see: their own,
       plus anyone else's that was not marked hidden.  An admin who has
       switched the all-users view on sees the hidden ones too."""
       where, params = self._note_visibility_sql(username, all_users, "n.")
       rows = self.conn.execute(
           "SELECT n.*, u.full_name FROM user_note n "
           "LEFT JOIN app_user u ON u.username = n.username "
           "WHERE n.serial_key=? AND %s ORDER BY n.updated_at DESC" % where,
           [serial_key] + params).fetchall()
       return [dict(r) for r in rows]

   # -- where a serial was last booked ----------------------------------
   #
   # Bookings live in two tables: production steps in `ops`, rework in
   # `rw`.  That split is useful for the rework analysis, which wants only
   # the rework rows - and a trap for everything else, because a serial's
   # most recent booking is just as likely to be a rework as a production
   # step.  Reading only `ops` left one serial in five sitting at an older
   # operation than it was really at, twice, in two different pages.
   #
   # So ASKING WHERE A SERIAL IS GOES THROUGH HERE.  The two tables stay
   # as storage; this is the only sanctioned way to read a position out of
   # them, and it is written once, correctly.
   def latest_booking(self, serial_keys=None, part_keys=None, d1=None, d2=None):
       """The newest booking of any kind for each serial.

       Rows come back with serial_key / serial_no / material / op_no /
       wc_code / op_date, whichever table they came from.

       The two tables are grouped SEPARATELY and merged in Python rather
       than in one UNION: a union is opaque to the planner, so the index
       that makes "latest per serial" a seek instead of a sort stops being
       used, and at half a million rows the query went from a fifth of a
       second to well over one.

       SQLite guarantees that when a query carries exactly one min/max
       aggregate, the other, unaggregated columns come from the row that
       produced it.  The key it maxes over is stamp, then operation number,
       then id - the same order the waterfall's route walk uses, so the
       pages cannot disagree about where a part is.

       A REWORK BOOKING NEVER WINS over a production one; see the merge
       below.
       """
       def one(table, ser, op, date_col):
           clauses = ["%s<>''" % ser, "%s IS NOT NULL" % date_col,
                      "%s<>''" % date_col]
           params = []
           if part_keys:
               pk = sorted({k for k in part_keys if k})
               clauses.append("UPPER(REPLACE(material,' ','')) IN (%s)"
                              % ",".join("?" * len(pk)))
               params.extend(pk)
           sk = sorted({k for k in (serial_keys or ()) if k})
           if sk and len(sk) <= 900:
               clauses.append("%s IN (%s)" % (ser, ",".join("?" * len(sk))))
               params.extend(sk)
           if d1:
               clauses.append("%s>=?" % date_col)
               params.append(d1.isoformat())
           if d2:
               clauses.append("%s<=?" % date_col)
               params.append(d2.isoformat())
           sql = ("SELECT %s AS serial_key, serial_no, material, %s AS op_no,"
                  "       COALESCE(seq,'') AS seq, wc_code, %s AS op_date,"
                  "       MAX(COALESCE(entry_dt, %s) || '#'"
                  "           || printf('%%08d', CAST(%s AS INTEGER))"
                  "           || '#' || printf('%%012d', id)) AS _latest "
                  "FROM %s WHERE %s GROUP BY %s"
                  % (ser, op, date_col, date_col, op, table,
                     " AND ".join(clauses), ser))
           return self.rows(sql, params)

       # PRODUCTION ENTRIES DECIDE, REWORK ONLY FILLS IN.
       #
       # A rework is booked against the operation that carries the defect,
       # often after the part has moved past it, so letting a rework stamp
       # win would put the part back at an operation it left.  The floor
       # plan and the waterfall have to agree about where a part is, and
       # the waterfall walks production entries only - see
       # op_progress_for_route.  Rework is used only for a serial that has
       # no production entry at all, where it is the only thing there is.
       # v71: the unfiltered answer (every serial) is asked by more than
       # one page - MRB / SCRAP / TECO, B1000 Live - and each time it is a
       # pass over the whole production table, which on a shared folder
       # is the slow part of opening those pages.  Kept until the data
       # changes (data_version), like the other per-version answers.
       whole = not (serial_keys or part_keys or d1 or d2)
       if whole and not self.__dict__.get("_latest_inner"):
           # kept per data version, in memory AND in the file (agg_cache),
           # so the other copies and the next start get it without the pass
           self._latest_inner = True
           try:
               return [dict(r) for r in self._agg("latest_booking_all",
                                                  lambda: self.latest_booking())]
           finally:
               self._latest_inner = False
       best = {}
       for r in one("ops", "serial_key", "op_no", "op_date"):
           cur = best.get(r["serial_key"])
           if cur is None or r["_latest"] > cur["_latest"]:
               best[r["serial_key"]] = r
       for r in one("rw", "UPPER(REPLACE(serial_no,' ',''))", "reject_op",
                    "rw_date"):
           if r["serial_key"] not in best:
               best[r["serial_key"]] = r
       return [dict(r) for r in best.values()]

   def serial_last_ops(self, serial_keys):
       """Where each of MANY serials was last booked, in one go.

       Asked one serial at a time - which is what the MRB/SCRAP/TECO list
       used to do - this was two queries per flagged serial, and each
       `latest_booking` groups over the whole production-step table.  With
       a few hundred records on a shared folder that is a few hundred
       trips across the network AND a few hundred passes over half a
       million rows, every time somebody pressed SAVE.  It is the reason
       typing a TECO note froze the screen.

       One query for the bookings, one for the descriptions.
       """
       keys = sorted({k for k in (serial_keys or ()) if k})
       if not keys:
           return {}
       # Remembered per data version (v67): the flag list asks for the same
       # serials on every refresh, and only a new import can move them.
       mem = self.__dict__.setdefault("_last_op_mem", {"ver": None, "map": {}})
       ver = self.data_version()
       if mem["ver"] != ver:
           mem["ver"], mem["map"] = ver, {}
       todo = [k for k in keys if k not in mem["map"]]
       if todo:
           got = self._serial_last_ops_read(todo)
           for k in todo:
               mem["map"][k] = got.get(k)
       return {k: dict(mem["map"][k]) for k in keys if mem["map"].get(k)}

   def _serial_last_ops_read(self, keys):
       out = {}
       CHUNK = 800                  # stays under SQLite's parameter limit
       for i in range(0, len(keys), CHUNK):
           part = keys[i:i + CHUNK]
           rows = self.latest_booking(serial_keys=part)
           for r in rows:
               out[r["serial_key"]] = {"op_no": r["op_no"],
                                       "op_date": r["op_date"],
                                       "op_desc": ""}
           # The description of the operation each one landed on.  Asked
           # for the (serial, operation) pairs actually found, so a serial
           # with no booking costs nothing.
           pairs = [(sk, v["op_no"]) for sk, v in out.items()
                    if sk in part and v["op_no"]]
           for j in range(0, len(pairs), CHUNK):
               chunk = pairs[j:j + CHUNK]
               ph = ",".join("(?,?)" for _ in chunk)
               params = [x for pair in chunk for x in pair]
               for r in self.rows(
                       "SELECT serial_key, op_no, op_desc FROM ops "
                       "WHERE (serial_key, op_no) IN (%s) AND op_desc<>'' "
                       "GROUP BY serial_key, op_no" % ph, params):
                   if r["serial_key"] in out:
                       out[r["serial_key"]]["op_desc"] = r["op_desc"]
       return out

   def serial_last_op(self, serial_key):
       """Where one serial was last booked - used by MRB/SCRAP/TECO as a
       simple, part-agnostic "where is it now", distinct from Waterfall's
       route-aware predicted position.

       For a LIST of serials use `serial_last_ops`; calling this in a loop
       is what made the flag list slow.
       """
       return self.serial_last_ops([serial_key]).get(norm_text(serial_key))

   def unknown_work_centers(self):
       """Work centre codes that show up in the stored RW data but have no
       description on file in the COMP table - the analysis pages fall back
       to showing the bare code for these, which usually means COMP is
       missing a row for them."""
       known = set(self.comp_map())
       codes = set(self._agg("rw_wc_codes", lambda: [
           r["wc_code"] for r in self.conn.execute(
               "SELECT DISTINCT wc_code FROM rw WHERE wc_code<>''")]))
       return sorted(codes - known)

   def work_centers(self, part_keys=None, d1=None, d2=None, ops_only=True):
       """The work centres worth offering in the MACHINES filter.

       Scoped, not a catalogue: with a part and a date range in hand it
       lists the machines that part actually passed through in that window,
       which is a handful rather than the eighty in the plant.  Anything
       outside the scope can still be typed in by hand.

       `ops_only` reads the production steps and not the rework records -
       "which machines cut this part" is a question about normal operation
       time entries, and a rework booking is not one of those.
       """
       key = (id(self), self.data_version(), tuple(sorted(part_keys or ())),
              d1, d2, ops_only)
       if _WC_CACHE["key"] == key:
           return _WC_CACHE["rows"]
       comp = self.comp_map()
       codes = set()
       for table, datecol, matexpr in (
               ("ops", "op_date", "UPPER(REPLACE(material,' ',''))"),
               ("rw", "rw_date", "material_key")):
           if ops_only and table == "rw":
               continue
           where, params = ["wc_code<>''"], []
           if part_keys:
               pk = sorted({k for k in part_keys if k})
               where.append("%s IN (%s)" % (matexpr, ",".join("?" * len(pk))))
               params.extend(pk)
           if d1:
               where.append("%s>=?" % datecol)
               params.append(d1.isoformat())
           if d2:
               where.append("%s<=?" % datecol)
               params.append(d2.isoformat())
           codes |= {r["wc_code"] for r in self.conn.execute(
               "SELECT DISTINCT wc_code FROM %s WHERE %s"
               % (table, " AND ".join(where)), params)}
       out = []
       for c in sorted(codes):
           name, cls = comp.get(c, ("", UNKNOWN))
           out.append({"code": c, "name": name or "", "comp": cls or UNKNOWN})
       _WC_CACHE["key"], _WC_CACHE["rows"] = key, out
       return out

   def serials_at_work_centers(self, codes, d1=None, d2=None, limit=None,
                               part_keys=None):
       """Serials that were worked on at any of these work centres inside
       the date window, most recent first.

       Both tables are searched: `ops` is every production step booked
       against a serial, `rw` is the rework records.  A part that only ever
       passed through the machine cleanly appears in the first and not the
       second, and leaving it out would make the filter mean "reworked
       there" rather than "cut there".

       `part_keys` narrows it to those parts BEFORE the limit is applied,
       which is the whole point: "the last ten" has to mean the last ten of
       the part being looked at, not the last ten in the plant of which
       only two happen to be that part.

       `limit` caps how many come back - the "show me the last N" case -
       and None means all of them.
       """
       codes = [c for c in (codes or []) if c]
       if not codes:
           return []
       ph = ",".join("?" * len(codes))
       out, seen = [], set()
       # `ops` stores the normalised key in its own column; `rw` only keeps
       # the printed serial number, so it is normalised in the query - the
       # same expression ix_rw_serkey indexes.
       pkeys = sorted({k for k in (part_keys or []) if k}) or None
       pph = ",".join("?" * len(pkeys)) if pkeys else ""
       for table, datecol, keyexpr, matexpr in (
               ("ops", "op_date", "serial_key",
                "UPPER(REPLACE(material,' ',''))"),
               ("rw", "rw_date", "UPPER(REPLACE(serial_no,' ',''))",
                "material_key")):
           where = ["%s<>''" % keyexpr, "wc_code IN (%s)" % ph]
           params = list(codes)
           if pkeys:
               where.append("%s IN (%s)" % (matexpr, pph))
               params.extend(pkeys)
           if d1:
               where.append("%s>=?" % datecol)
               params.append(d1.isoformat())
           if d2:
               where.append("%s<=?" % datecol)
               params.append(d2.isoformat())
           sql = ("SELECT %s AS serial_key, MAX(%s) AS d FROM %s WHERE %s "
                  "GROUP BY %s" % (keyexpr, datecol, table,
                                   " AND ".join(where), keyexpr))
           for r in self.conn.execute(sql, params):
               sk = r["serial_key"]
               if sk in seen:
                   continue
               seen.add(sk)
               out.append((r["d"] or "", sk))
       out.sort(reverse=True)
       keys = [sk for _d, sk in out]
       return keys[:limit] if limit else keys

   def op_desc_map_for(self, part_keys):
       """Operation descriptions as THIS part's traveler defines them.

       The same operation number can be named differently on different
       parts' routes, so looking at one part should show that part's
       wording.  Anything its route does not cover falls back to the
       plant-wide map.
       """
       keys = sorted({k for k in (part_keys or ()) if k})
       if not keys:
           return self.op_desc_map()
       ck = (id(self), self.data_version(), tuple(keys))
       if _OPDESC_PART_CACHE["key"] == ck:
           return _OPDESC_PART_CACHE["map"]
       base = dict(self.op_desc_map())
       ph = ",".join("?" * len(keys))
       for r in self.conn.execute(
               "SELECT op, descr FROM wf_traveler WHERE descr<>'' "
               "AND part_key IN (%s)" % ph, keys):
           k = op_key(r["op"])
           if k:
               base[k] = r["descr"]        # the part's own route wins
       _OPDESC_PART_CACHE["key"], _OPDESC_PART_CACHE["map"] = ck, base
       return base

   def traveler_desc_by_part(self):
       """2.1.3: {part key: {step key: description}} straight from each
       part's own traveler (unticked lines too - a rework can be booked on
       one).  Both 280/3 and plain 280 are keys; a plain operation number
       takes its first line's text.  Merged parts answer with their first
       member that has a route.  Cached on the data version."""
       ck = (id(self), self.data_version(), self._meta_all().get(self.PART_MERGE_KEY))
       hit = self.__dict__.get("_trav_desc_cache")
       if hit is not None and hit[0] == ck:
           return hit[1]
       out = {}
       for r in self.conn.execute(
               "SELECT part_key, op, COALESCE(seq,'') AS seq, descr FROM wf_traveler "
               "WHERE descr<>'' ORDER BY part_key, position"):
           d = out.setdefault(r["part_key"], {})
           d.setdefault(step_key(r["op"], r["seq"]), r["descr"])
           d.setdefault(op_key(r["op"]), r["descr"])
       for g in self.part_merges():
           for mem in g["members"]:
               if out.get(mem):
                   out.setdefault(g["key"], out[mem])
                   break
       self._trav_desc_cache = (ck, out)
       return out

   def op_desc_map(self):
       """
       {operation key: description}, traveler first.

       Cached on the data version: it is three grouped queries, one of them
       over every production step on file, and both the analysis lookups
       and the per-part map rebuild it from scratch on every page refresh.  The traveler route is
       the authoritative definition of what each operation is; the SAP
       export's own text for the same operation can vary row to row, so it
       only fills gaps the traveler does not cover.
       """
       ck = (id(self), self.data_version())
       if _OPDESC_CACHE["key"] == ck:
           return _OPDESC_CACHE["map"]
       m = self._agg("op_desc_map", self._op_desc_map_compute)
       _OPDESC_CACHE["key"], _OPDESC_CACHE["map"] = ck, m
       return m

   def _op_desc_map_compute(self):
       m = {}
       for r in self.conn.execute(
               "SELECT op, descr, COUNT(*) n FROM wf_traveler "
               "WHERE descr<>'' GROUP BY op, descr ORDER BY n DESC"):
           k = op_key(r["op"])
           if k and k not in m:
               m[k] = r["descr"]
       for r in self.conn.execute(
               "SELECT reject_op, reject_op_desc, COUNT(*) n FROM rw "
               "WHERE reject_op<>'' AND reject_op_desc<>'' "
               "GROUP BY reject_op, reject_op_desc ORDER BY n DESC"):
           k = op_key(r["reject_op"])
           if k and k not in m:
               m[k] = r["reject_op_desc"]
       # The production steps name every operation of a part, including the
       # ones that only ever appear as a cause operation.
       for r in self.conn.execute(
               "SELECT op_key, op_desc, COUNT(*) n FROM ops "
               "WHERE op_key<>'' AND op_desc<>'' "
               "GROUP BY op_key, op_desc ORDER BY n DESC"):
           if r["op_key"] not in m:
               m[r["op_key"]] = r["op_desc"]
       return m

   # -- waterfall --------------------------------------------------------

   def wf_rebuild_part_data(self):
       """Rebuilds the waterfall's per-operation table from the stored rows.

       wf_part_data is one row per (material, serial, operation) holding the
       SUM of the hours booked against it and the LATEST stamp.  Adding a
       file used to patch those sums, which cannot be made to come out
       right: re-importing a day either double-counted it or, when rows
       were skipped as duplicates, dropped bookings entirely.

       Since every booking is now kept in rw/ops with its own hours and
       stamp, the aggregate is simply recomputed from them.  Whatever the
       row tables say, this says - so replacing a day's rows and rebuilding
       leaves the totals exactly right, every time.

       2.1.4: ONE transaction that waits out somebody else's write, like
       the import does.  It used to give up after 1.5 seconds with
       "database is locked" - on the share, right after an import moved
       data_version and every other copy started reading - and the
       waterfall kept the old hours while the RW pages had the new rows.
       The import leaves 'partdata_pending'; only a finished rebuild
       clears it, so a failed one is redone when the program next opens.
       """
       def once():
           self.txn_begin()
           try:
               n = self._wf_rebuild_part_data()
               self.conn.execute("DELETE FROM meta WHERE key IN "
                                 "('partdata_pending', 'partdata_rebuild_failed')")
               self.txn_commit()
           except BaseException:
               self.txn_rollback()
               raise
           return n
       # A lock met half way (a big write spilling to the file needs it to
       # itself) rolls the attempt back and starts it again.
       n = with_lock_retry(once, seconds=WRITE_WAIT_SECONDS)
       self.bump_version()
       return n

   def _wf_rebuild_part_data(self):
       self.conn.execute("DELETE FROM wf_part_data")
       # THE SEQUENCE IS PART OF THE KEY.  280/1 and 280/3 are separate
       # pieces of work with their own standard times, so their hours are
       # summed separately; adding them together made one of them look
       # finished on the other one's hours.  done_x is "any booking
       # against this step carried the LAST-entry X".
       self.conn.execute(
           "INSERT INTO wf_part_data (material, material_key, serial, "
           "  serial_key, opac, seq, dt, dt_ops, dt_first, dt_ops_first, "
           "  rework, std_hour, machine_hour, done_x, x_dt, prog, "
           "  mach_ops, lab_ops) "
           "SELECT material, material_key, serial_no, "
           "       serial_key, opac, seq, MAX(dt), MAX(dt_ops), "
           "       MIN(dt), MIN(dt_ops), "
           "       SUM(rework), SUM(tot), SUM(mach), MAX(kapanis), "
           "       MAX(CASE WHEN kapanis=1 THEN dt END), SUM(ilerleme), "
           "       SUM(mach_p), SUM(tot_p) "
           "FROM ("
           "  SELECT material, UPPER(REPLACE(material,' ','')) material_key,"
           "         serial_no, serial_key,"
           "         CAST(op_key AS INTEGER) opac, COALESCE(seq,'') seq,"
           "         COALESCE(entry_dt, op_date) dt,"
           "         COALESCE(entry_dt, op_date) dt_ops,"
           "         0.0 rework, tot_hour tot, mach_hour mach,"
           # THE X THAT CLOSES A STEP: a production booking (this table
           # holds no rework) that is not a setup.  A setup's X means the
           # machine was set up, not that the part is finished (v62).
           "         CASE WHEN COALESCE(last_x,0)=1 AND COALESCE(setup,0)=0"
           "              THEN 1 ELSE 0 END kapanis,"
           # PROGRESS, booking by booking: machine time where it has
           # some, else its own labour (P); a row imported before P was
           # read falls back to Toplam Is.  Setups are not progress.
           "         CASE WHEN COALESCE(setup,0)<>0 THEN 0"
           "              WHEN COALESCE(mach_hour,0)>0 THEN mach_hour"
           "              ELSE COALESCE(lab_hour, tot_hour, 0) END"
           "              ilerleme,"
           # 2.2.0: the same hours once more, PRODUCTION only
           "         COALESCE(mach_hour,0) mach_p, COALESCE(tot_hour,0) tot_p"
           "  FROM ops WHERE serial_key<>'' AND op_key<>''"
           "  UNION ALL"
           "  SELECT material, UPPER(REPLACE(material,' ','')) material_key,"
           "         serial_no,"
           "         UPPER(REPLACE(serial_no,' ','')) serial_key,"
           "         CAST(reject_op AS INTEGER) opac, COALESCE(seq,'') seq,"
           "         COALESCE(entry_dt, rw_date) dt,"
           "         NULL dt_ops,"
           "         rw_hour rework, tot_hour tot, mach_hour mach,"
           # A REWORK booking never closes a step and is never progress -
           # whatever its X says.  It used to count: MAX(last_x) ran over
           # both tables, so an X on a rework row finished the operation.
           "         0 kapanis, 0.0 ilerleme, 0.0 mach_p, 0.0 tot_p"
           "  FROM rw WHERE serial_no<>'' AND reject_op<>''"
           ") WHERE dt IS NOT NULL AND dt<>'' "
           "GROUP BY material_key, serial_key, opac, seq")
       return self.conn.execute(
           "SELECT COUNT(*) n FROM wf_part_data").fetchone()["n"]

   def wf_import_traveler(self, routes, display, replace=True):
       """
       routes: {part_key: [{position,op,pred,desc}, ...]}.
       replace=True wipes every part's route first (a full re-upload).
       replace=False only replaces the route of the parts present in
       `routes`, leaving every other already-stored part untouched.

       An operation number appears once per part.  A traveler often lists
       the same operation several times with slightly different wording;
       keeping all of them would repeat the same column in every view that
       walks the route.  The first occurrence wins, including its
       description, and the positions are renumbered so the route stays a
       clean 0..n-1 sequence.  The number of collapsed rows is returned so
       the import log can say what happened.

       Standard times are the exception: they are ADDED UP rather than
       discarded.  Two lines carrying the same operation number are two
       passes of one step, so the work the step represents is the sum of
       both - taking only the first would under-state it, and the standard
       time is what decides whether a part has finished the operation.

       A SEQUENCE NUMBER CHANGES THIS.  280/1, 280/3 and 280/4 are not
       repeats of one step to be collapsed into one - they are three
       pieces of work, each with its own standard time, each booked
       against its own sequence, and each finishing on its own.  So they
       are kept as three lines, and only lines that carry the SAME
       sequence (or no sequence at all) are merged.
       """
       if replace:
           self.conn.execute("DELETE FROM wf_traveler")
       else:
           for part_key in routes:
               self.conn.execute("DELETE FROM wf_traveler WHERE part_key=?",
                                 (part_key,))
       n = collapsed = 0
       for part_key, cols in routes.items():
           part_name = display.get(part_key, part_key)
           # One row per operation number, in the order first seen, with the
           # standard times of any repeat lines added into it.
           merged, order = {}, []
           for c in cols:
               seq = seq_key(c.get("seq"))
               key = step_key(c["op"], seq)
               std = float(c.get("std_hour") or 0.0)
               if key in merged:
                   collapsed += 1
                   merged[key]["std_hour"] += std
                   # A repeat line may be the one that names the machine
                   # family, so a blank on the first line gets filled in.
                   if not merged[key]["wc_group"]:
                       merged[key]["wc_group"] = c.get("wc_group") or ""
                   continue
               order.append(key)
               merged[key] = {"op": c["op"], "seq": seq, "pred": c["pred"],
                              "desc": c["desc"], "std_hour": std,
                              "wc_group": c.get("wc_group") or "",
                              "active": 1 if c.get("active", 1) else 0}
           for pos, key in enumerate(order):
               m = merged[key]
               self.conn.execute(
                   "INSERT INTO wf_traveler (part,part_key,position,op,seq,pred,"
                   "descr,std_hour,wc_group,active) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (part_name, part_key, pos, m["op"], m["seq"], m["pred"],
                    m["desc"], m["std_hour"], m["wc_group"], m["active"]))
               n += 1
       self.conn.commit()
       self.bump_version()
       self.note_upload()
       self._last_traveler_collapsed = collapsed
       return n

   def part_group_map(self):
       """{material key: part group} for every part on file.

       The floor plan asks this once a refresh so a zone can be narrowed
       to "disks only"; asking per serial would be a query per symbol.
       """
       return {norm_text(r["part_no"]): (r["part_group"] or "")
               for r in self.rows("SELECT part_no, part_group FROM parts")}

   def wf_parts(self, merge=False):
       """[(part_key, display_name), ...] - parts that have a traveler route.

       merge=True: merged parts (see part_merges) come back as one entry,
       (merged key, "1000A10G02/G03"), where their first member stood."""
       base = [(r["part_key"], r["part"]) for r in self._cached_rows(
           "SELECT DISTINCT part_key, part FROM wf_traveler ORDER BY part")]
       if not merge:
           return base
       member_of = {m: g for g in self.part_merges() for m in g["members"]}
       if not member_of:
           return base
       out, done = [], set()
       for pk, disp in base:
           g = member_of.get(pk)
           if g is None:
               out.append((pk, disp))
           elif g["key"] not in done:
               done.add(g["key"])
               out.append((g["key"], g["name"]))
       return out

   # -- merged parts (v65) ----------------------------------------------
   # Two part numbers that are really one job - 1000A10G02 and 1000A10G03,
   # same traveler - can be counted together as "1000A10G02/G03".  The
   # definition lives in the shared database; each PAGE decides whether it
   # shows them merged (DATA & SETTINGS > MERGED PARTS).
   #
   # A merged part's key is its name, normalised.  wf_traveler_for and
   # wf_resolve_material_keys ALWAYS understand it, whatever a page's
   # switch says - the switch only decides whether the part LISTS offer
   # the merged part or its members.  So a saved selection never breaks.
   PART_MERGE_KEY = "part_merges"
   PART_MERGE_PAGES_KEY = "part_merge_pages"

   def part_merges(self):
       """[{"name", "key", "members": [part key, ...]}, ...]"""
       raw = self._meta_all().get(self.PART_MERGE_KEY)
       hit = getattr(self, "_merge_cache", None)
       if hit is not None and hit[0] == raw:
           return hit[1]
       try:
           ham = json.loads(raw) if raw else []
       except Exception:
           ham = []
       out, seen = [], set()
       for g in ham if isinstance(ham, list) else []:
           members = list(dict.fromkeys(
               norm_text(m) for m in (g.get("members") or []) if norm_text(m)))
           name = clean_str(g.get("name")) or merge_name(members)
           key = norm_text(name)
           if len(members) < 2 or not key or key in seen:
               continue
           seen.add(key)
           out.append({"name": name, "key": key, "members": members})
       self._merge_cache = (raw, out)
       return out

   def set_part_merges(self, groups):
       """Saves the merged-part list (checked with check_part_merges
       first by the caller).  Changes what the pages compute, so every
       copy's caches go."""
       clean = []
       for g in groups:
           members = [norm_text(m) for m in g.get("members") or []
                      if norm_text(m)]
           clean.append({"name": clean_str(g.get("name"))
                         or merge_name(members), "members": members})
       self.set_meta(self.PART_MERGE_KEY, clean)
       self._merge_cache = None
       self.bump_version()

   def merge_group(self, key):
       """The merged part whose KEY this is, else None."""
       if not key:
           return None
       groups = self.part_merges()
       if not groups:
           return None
       for g in groups:
           if g["key"] == key:
               return g
       return None

   def merge_pages(self):
       """{page: True} for the pages that show merged parts."""
       try:
           ham = self.get_meta(self.PART_MERGE_PAGES_KEY, None) or {}
       except Exception:
           return {}
       return {str(k): True for k, v in ham.items() if v}

   def merge_on(self, page):
       return bool(self.merge_pages().get(str(page))) and bool(self.part_merges())

   def set_merge_page(self, page, on):
       pages = self.merge_pages()
       if on:
           pages[str(page)] = True
       else:
           pages.pop(str(page), None)
       self.set_meta(self.PART_MERGE_PAGES_KEY, pages)

   def wf_traveler_for(self, part_key):
       """The route every analysis page walks - active operations only,
       renumbered into a clean 0..n-1 position sequence so a gap left by
       an unticked operation never shows up as a hole in the route.

       A merged part walks its FIRST member's route (the members share
       one traveler; the DATA page warns when they do not)."""
       g = self.merge_group(part_key)
       if g is not None:
           for m in g["members"]:
               route = self.wf_traveler_for(m)
               if route:
                   return route
           return []
       rows = [dict(r) for r in self._cached_rows(
           "SELECT position, op, COALESCE(seq,'') AS seq, pred, descr AS desc, "
           "COALESCE(std_hour,0) AS std_hour, COALESCE(wc_group,'') AS wc_group "
           "FROM wf_traveler WHERE part_key=? AND active<>0 ORDER BY position",
           (part_key,))]
       toplam = {}
       for r in rows:
           toplam[r["op"]] = toplam.get(r["op"], 0.0) + (r["std_hour"] or 0.0)
       for i, r in enumerate(rows):
           r["position"] = i
           # `step` is what a booking is matched against, `label` is what
           # the shop calls it - 280/1 rather than 280.
           r["step"] = step_key(r["op"], r["seq"])
           r["label"] = ("%s/%s" % (r["op"], r["seq"])) if r["seq"] else str(r["op"])
           # The WHOLE operation's standard time: its sequences added up.
           # "280/1 is 5 h of the operation's 12 h" is the sentence a
           # traveler is checked against.
           r["op_std_hour"] = toplam.get(r["op"], 0.0)
       return rows

   def wf_traveler_full(self, part_key):
       """Every operation on file for this part, active or not - what
       EDIT TRAVELER shows, so a previously unticked operation can be
       ticked back on."""
       rows = [dict(r) for r in self.conn.execute(
           "SELECT position, op, COALESCE(seq,'') AS seq, pred, descr AS desc, "
           "active FROM wf_traveler WHERE part_key=? ORDER BY position",
           (part_key,))]
       for r in rows:
           r["step"] = step_key(r["op"], r["seq"])
           r["label"] = ("%s/%s" % (r["op"], r["seq"])) if r["seq"] else str(r["op"])
       return rows

   def wf_set_traveler_active(self, part_key, active_ops):
       """active_ops: {(op, seq): bool} - or {op: bool} as it used to be,
       which still means "the whole operation" - EDIT TRAVELER's SAVE.
       """
       for key, active in active_ops.items():
           if isinstance(key, tuple):
               op, seq = key
               self.conn.execute(
                   "UPDATE wf_traveler SET active=? WHERE part_key=? "
                   "AND op=? AND COALESCE(seq,'')=?",
                   (1 if active else 0, part_key, op, seq or ""))
           else:
               self.conn.execute(
                   "UPDATE wf_traveler SET active=? WHERE part_key=? AND op=?",
                   (1 if active else 0, part_key, key))
       self.conn.commit()
       self.bump_version()

   def wf_material_keys(self):
       return self._agg("wf_material_keys", lambda: [
           r["material_key"] for r in self.conn.execute(
               "SELECT DISTINCT material_key FROM wf_part_data "
               "ORDER BY material_key")])

   def ops_materials(self):
       """Every material string in the production steps (stored summary)."""
       return self._agg("ops_materials", lambda: [
           r["material"] for r in self.conn.execute(
               "SELECT DISTINCT material FROM ops")])

   def x_rule_check(self):
       """Serials that moved PAST a step without that step's X (v62).

       The part is further along - it booked a later step - so the step is
       finished all the same, but the X that should have closed it was
       never booked.  The shop wants to know how many and where: it is a
       data-entry gap, and on the BOTTLENECK page those steps can only be
       counted on the day of their last entry, not the day they closed.

       Returns {"serials": n, "steps": {step label: n}}.  Worked out once,
       when data is uploaded (it walks every part) - the DATA page shows
       the stored answer.
       """
       serial_n, adim_n = 0, {}
       for part_key, _d in self.wf_parts():
           route = self.wf_traveler_for(part_key)
           if not route:
               continue
           pos_of = {route_step(c): i for i, c in enumerate(route)}
           per = {}
           for r in self.wf_part_data_for(part_key):
               i = pos_of.get(r.get("step") or step_key(r["opac"], r.get("seq")))
               if i is None or not r.get("dt_ops"):
                   continue
               d = per.setdefault(r["serial_key"], {})
               d[i] = d.get(i, False) or bool(r.get("done_x"))
           for adimlar in per.values():
               en_ileri = max(adimlar)
               eksik = [i for i, x in adimlar.items() if i < en_ileri and not x]
               if eksik:
                   serial_n += 1
                   for i in eksik:
                       ad = str(route[i].get("label") or route[i]["op"])
                       adim_n[ad] = adim_n.get(ad, 0) + 1
       return {"serials": serial_n, "steps": adim_n}

   def wf_resolve_material_keys(self, part_key, min_len=6):
       """
       Every material key belonging to this traveler part.

       SAP and the traveler do not always spell the part number identically -
       one may carry 2466M52G where the other carries 2466M52G04 - and an
       exact comparison then quietly finds nothing and the matrix comes up
       empty.  Same prefix rule the rework side uses in match_part().
       """
       if not part_key:
           return set()
       g = self.merge_group(part_key)
       if g is not None:
           keys = set()
           for m in g["members"]:
               keys |= self.wf_resolve_material_keys(m, min_len)
           return keys
       keys = set()
       for mk in self.wf_material_keys():
           if not mk:
               continue
           if (mk == part_key
                   or (len(mk) >= min_len and part_key.startswith(mk))
                   or (len(part_key) >= min_len and mk.startswith(part_key))):
               keys.add(mk)
       return keys

   def wf_part_data_for(self, part_key):
       ck = (self.__dict__.get("cache_id", id(self)), self.data_version(), part_key)
       hit = _PARTDATA_CACHE.get(ck)
       if hit is not None:
           return hit
       out = self._read_part_data(part_key)
       # Two parts on screen at once is the most anybody does; this is a
       # handful of lists, not a leak.
       if len(_PARTDATA_CACHE) > 4:
           _PARTDATA_CACHE.clear()
       _PARTDATA_CACHE[ck] = out
       return out

   def _read_part_data(self, part_key):
       keys = self.wf_resolve_material_keys(part_key)
       if not keys:
           return []
       placeholders = ",".join("?" * len(keys))
       out = []
       # 2.2.0: read by POSITION and with the repeated work remembered -
       # this runs over every step record of the part (tens of thousands on
       # a real export) the first time a page asks for it.
       iso = datetime.datetime.fromisoformat
       steps = {}

       def when(text, fallback):
           # A database written before a column existed has it empty; the
           # fallback then stands in (the last entry for the first one,
           # which is exactly right for an operation booked only once).
           if not text:
               return fallback
           try:
               return iso(text)
           except Exception:
               return fallback
       # 2.2.0: production-only hours; a database this copy could not
       # upgrade (read-only) has no such columns - NULL then, like a row
       # written by an older program
       has_ops = self.__dict__.get("_pd_has_ops")
       if has_ops is None:
           has_ops = self._pd_has_ops = "mach_ops" in {
               r["name"] for r in self.conn.execute("PRAGMA table_info(wf_part_data)")}
       extra = "mach_ops, lab_ops" if has_ops else "NULL, NULL"
       for (material, serial, serial_key, opac, seq, done_x, dt_s, dt_ops_s,
            dt_first_s, dt_ops_first_s, rework, std_hour, machine_hour, x_dt_s,
            prog, mach_ops, lab_ops) in self.rows(
               "SELECT material, serial, serial_key, opac, "
               "COALESCE(seq,'') AS seq, COALESCE(done_x,0) AS done_x, "
               "dt, dt_ops, dt_first, dt_ops_first, rework, "
               "std_hour, machine_hour, x_dt, COALESCE(prog,0) AS prog, %s "
               "FROM wf_part_data WHERE material_key IN (%s)" % (extra, placeholders),
               tuple(keys)):
           try:
               dt = iso(dt_s)
           except Exception:
               continue
           dt_ops = when(dt_ops_s, None)
           sk = (opac, seq)
           step = steps.get(sk)
           if step is None:
               step = steps[sk] = step_key(opac, seq)
           # The column is called std_hour on disk for historical reasons,
           # but it holds SAP's Toplam Iscilik: the labour actually booked
           # against the operation, rework included.  It is handed on as
           # `labour_hour` so it can never be confused with the traveler's
           # std_hour, which is the standard time the step is meant to take.
           out.append({"material": material, "serial": serial,
                       "serial_key": serial_key, "opac": opac,
                       "seq": seq or "",
                       "step": step,
                       # SAP says the part is OFF this step, whatever the
                       # hours add up to - see step_is_done().
                       "done_x": bool(done_x),
                       "dt": dt, "dt_ops": dt_ops,
                       "dt_first": when(dt_first_s, dt),
                       "dt_ops_first": when(dt_ops_first_s, dt_ops),
                       "rework": rework,
                       # THE DAY THE STEP WAS CLOSED: the latest booking that
                       # carried the X and was not a setup (v62).  None when
                       # no such booking exists.
                       "x_dt": when(x_dt_s, None),
                       # PROGRESS ON THE STEP (v62): the Toplam Is hours of
                       # production bookings that were not setups.  This only
                       # ever SHOWS how far along a step is - the X alone
                       # decides whether it is finished.
                       "labour": prog or 0.0,
                       "machine_hour": machine_hour or 0.0,
                       "labour_hour": std_hour or 0.0,
                       # OPERATION TIME (2.2.0): production bookings only,
                       # never a rework's hours.  Falls back to the totals
                       # on a row an older program wrote.
                       "machine_ops": (machine_hour or 0.0) if mach_ops is None
                       else mach_ops,
                       "labour_ops": (std_hour or 0.0) if lab_ops is None
                       else lab_ops})
       return out

   def labour_by_op(self, material_keys, serial_keys=None):
       """{(serial key, operation key): (machine hours, last booking,
       last PRODUCTION booking)}.

       One query for a whole set of serials - the floor plan needs the
       figure for every part it is showing at once, and asking per serial
       turned one screen into hundreds of round trips.

       The third value is dt_ops: the last entry that was not a rework.
       It is None on an operation that only ever carried rework, and that
       is what tells the floor plan which operations count when it works
       out how far along the route a part has got.
       """
       mk = [k for k in (material_keys or ()) if k]
       if not mk:
           return {}
       sql = ("SELECT serial_key, opac, COALESCE(seq,'') AS seq, "
              "COALESCE(done_x,0) AS done_x, COALESCE(prog,0) AS prog, "
              "dt, dt_ops "
              "FROM wf_part_data WHERE material_key IN (%s)"
              % ",".join("?" * len(mk)))
       params = list(mk)
       sk = sorted({k for k in (serial_keys or ()) if k})
       # Naming every serial narrows the read, but a bound-variable list has
       # a hard limit in SQLite and a big shop would sail past it.  Beyond a
       # point the material filter is selective enough on its own: the extra
       # rows simply never get looked up.
       if sk and len(sk) <= 900:
           sql += " AND serial_key IN (%s)" % ",".join("?" * len(sk))
           params.extend(sk)
       out = {}
       for r in self.rows(sql, params):
           try:
               dt = datetime.datetime.fromisoformat(r["dt"])
           except Exception:
               dt = None
           try:
               dt_ops = datetime.datetime.fromisoformat(r["dt_ops"])
           except Exception:
               dt_ops = None
           # Machine time, falling back to labour on rows imported before
           # machine time was read - see wf_part_data_for().
           # Keyed by STEP, so a booking against 280/3 is not read as
           # work on 280/1.
           # Progress is the Toplam Is of non-setup production bookings
           # (v62) - the same figure the waterfall shows.  It never decides
           # whether a step is finished: done_x does.
           out[(r["serial_key"], step_key(r["opac"], r["seq"]))] = (
               r["prog"] or 0.0, dt, dt_ops, bool(r["done_x"]))
       return out

   def wf_counts(self):
       def hesap():
           pd = self.conn.execute(
               "SELECT COUNT(*) n FROM wf_part_data").fetchone()["n"]
           tp = self.conn.execute(
               "SELECT COUNT(DISTINCT part_key) n FROM wf_traveler").fetchone()["n"]
           tr = self.conn.execute(
               "SELECT COUNT(*) n FROM wf_traveler").fetchone()["n"]
           return {"part_data": pd, "traveler_parts": tp, "traveler_rows": tr}
       return self._agg("wf_counts", hesap)

   def wf_years(self):
       def hesap():
           years = set()
           for r in self.conn.execute(
                   "SELECT DISTINCT substr(dt,1,4) y FROM wf_part_data"):
               try:
                   years.add(int(r["y"]))
               except (TypeError, ValueError):
                   pass
           return sorted(years)
       return self._agg("wf_years", hesap)

   def wf_machine_time_counts(self):
       """(operation records, those carrying machine time) - DATA HEALTH."""
       def hesap():
           r = self.conn.execute(
               "SELECT COUNT(*) n, SUM(machine_hour>0) m FROM wf_part_data"
           ).fetchone()
           return [r["n"] or 0, r["m"] or 0]
       return tuple(self._agg("wf_machine_time", hesap))

   def wf_clear_traveler(self):
       self.conn.execute("DELETE FROM wf_traveler")
       self.conn.commit()
       self.bump_version()

   def wf_clear_part_data(self):
       self.conn.execute("DELETE FROM wf_part_data")
       self.conn.commit()
       self.bump_version()

   def close(self):
       """Closing must never be able to hang.

       sqlite3.close() can BLOCK rather than raise - in WAL mode the last
       connection standing tries to checkpoint, and with a 20 second busy
       timeout a colleague's open copy could hold this one on screen long
       after the person pressed the X.  So the timeout is cut right down
       first, the checkpoint is attempted passively (it gives up rather
       than waiting for anyone), and only then does the handle close.
       Leaving a checkpoint undone costs nothing: the next process to open
       the file does it.
       """
       try:
           self.conn.execute("PRAGMA busy_timeout = 400")
       except Exception:
           pass
       if self.journal_mode == "wal":
           try:
               self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
           except Exception:
               pass
       try:
           self.conn.close()
       except Exception:
           pass


# ===========================================================================
#  6. PART MATCHING + ANALYSIS ENGINE
# ===========================================================================

def merge_name(members):
   """The shop's way of writing two part numbers as one:
   1000A10G02 + 1000A10G03 -> "1000A10G02/G03".  What they share is cut
   back to its last letter, so the part after the slash still reads as a
   part suffix (G03), not a bare "3"."""
   members = [clean_str(m).upper() for m in members if clean_str(m)]
   if not members:
       return ""
   if len(members) == 1:
       return members[0]
   ortak = os.path.commonprefix(members)
   cut = 0
   for i in range(len(ortak) - 1, -1, -1):
       if ortak[i].isalpha():
           cut = i
           break
   return members[0] + "".join("/" + m[cut:] for m in members[1:])


def check_part_merges(db, groups):
   """(errors, warnings) for a merged-part list about to be saved.

   errors stop the save: fewer than two members, a part in two merged
   parts, two merged parts with one name, a name that is already a real
   part number.  warnings do not: members whose travelers differ (the
   first member's route is used - user decision, v65).
   """
   errors, warnings = [], []
   real = {p["part_key"] or norm_text(p["part_no"]) for p in db.all_parts()}
   real |= {pk for pk, _d in db.wf_parts()}
   seen_m, seen_n = {}, set()
   for g in groups:
       members = [norm_text(m) for m in g.get("members") or [] if norm_text(m)]
       name = clean_str(g.get("name")) or merge_name(members)
       key = norm_text(name)
       if len(set(members)) < 2:
           errors.append("%s: pick at least two parts." % (name or "(no name)"))
           continue
       if key in seen_n:
           errors.append("%s: the name is used twice." % name)
       seen_n.add(key)
       if key in real and key not in members:
           errors.append("%s: that name is already a part number." % name)
       for m in members:
           if m in seen_m and seen_m[m] != key:
               errors.append("%s is in two merged parts (%s, %s)."
                             % (m, seen_m[m], name))
           seen_m[m] = key
       routes = {}
       for m in members:
           r = db.wf_traveler_for(m)
           if r:
               routes[m] = [c["step"] for c in r]
       if len({tuple(v) for v in routes.values()}) > 1:
           first = next(m for m in members if m in routes)
           diff = [m for m in routes if routes[m] != routes[first]]
           warnings.append("%s: traveler of %s differs from %s - the route "
                           "of %s is used." % (name, ", ".join(diff), first,
                                                first))
       missing = [m for m in members if m not in routes]
       if routes and missing:
           warnings.append("%s: no traveler for %s." % (name, ", ".join(missing)))
   return errors, warnings


def build_part_index(parts):
   # A merged part (all_parts(merge=True)) answers for each of its
   # members as well, so a rework row on 1000A10G03 lands on
   # "1000A10G02/G03".
   exact, lst = {}, []
   merged = False
   for p in parts:
       k = p.get("part_key") or norm_text(p.get("part_no"))
       if not k:
           continue
       exact[k] = p
       lst.append((k, p))
       for m in p.get("members") or ():
           merged = True
           exact[m] = p
           lst.append((m, p))
   lst.sort(key=lambda x: -len(x[0]))
   return {"exact": exact, "list": lst, "merge": merged}


def match_part(material_key, index, min_len=6):
   if not material_key:
       return None
   if material_key in index["exact"]:
       return index["exact"][material_key]
   for key, part in index["list"]:
       if len(material_key) >= min_len and key.startswith(material_key):
           return part
       if len(key) >= min_len and material_key.startswith(key):
           return part
   return None


def relabel_weeks(records, week_start):
   """iso_year / iso_week of each record re-made from its rw_date on the
   given week start - the stored ones belong to the default start."""
   off = week_offset(week_start)
   memo = {}
   for rec in records:
       ds = rec.get("rw_date")
       if not ds:
           continue
       yw = memo.get(ds)
       if yw is None:
           d = parse_date(ds)
           if d is None:
               continue
           yw = memo[ds] = iso_year_week(d, off)
       rec["iso_year"], rec["iso_week"] = yw


def build_time_clause(f):
   mode = f.get("time_mode", "week")
   params, clauses = [], []
   # THE PAGE'S OWN WEEK (v66 fix).  iso_year / iso_week on a stored row
   # were worked out at import from the DEFAULT week start.  A page given
   # its own start (DATA > WORKING WEEK > PER PAGE) - RW ANALYSIS on
   # Friday - asking for "FW39" by those columns got the default's FW39
   # (Mon 21 - Sun 27 Sep) instead of its own (Fri 18 - Thu 24 Sep).  With
   # f["wk_day"] set, a week filter is the week's DATES on that start.
   if mode in ("week", "weeks") and f.get("wk_day") is not None:
       d1, d2 = time_mode_to_date_range(f, int(f["wk_day"]))
       if d1:
           clauses.append("rw_date >= ?")
           params.append(d1.isoformat())
       if d2:
           clauses.append("rw_date <= ?")
           params.append(d2.isoformat())
       if not d1 and not d2 and f.get("year"):
           clauses.append("substr(rw_date,1,4) = ?")
           params.append("%04d" % int(f["year"]))
       return (" AND ".join(clauses) if clauses else "1=1"), params
   if mode == "week":
       if f.get("year"):
           clauses.append("iso_year = ?")
           params.append(int(f["year"]))
       if f.get("week") is not None:
           clauses.append("iso_week = ?")
           params.append(int(f["week"]))
   elif mode == "weeks":
       if f.get("year"):
           clauses.append("iso_year = ?")
           params.append(int(f["year"]))
       ws, we = f.get("week_start"), f.get("week_end")
       if ws is not None and we is not None:
           if ws <= we:
               clauses.append("iso_week BETWEEN ? AND ?")
               params += [int(ws), int(we)]
           else:
               clauses.append("(iso_week >= ? OR iso_week <= ?)")
               params += [int(ws), int(we)]
   elif mode == "month":
       if f.get("year") and f.get("month"):
           y, mo = int(f["year"]), int(f["month"])
           last_day = calendar.monthrange(y, mo)[1]
           clauses.append("rw_date BETWEEN ? AND ?")
           params += ["%04d-%02d-01" % (y, mo), "%04d-%02d-%02d" % (y, mo, last_day)]
   elif mode == "dates":
       if f.get("date_start"):
           clauses.append("rw_date >= ?")
           params.append(f["date_start"])
       if f.get("date_end"):
           clauses.append("rw_date <= ?")
           params.append(f["date_end"])
   return (" AND ".join(clauses) if clauses else "1=1"), params


def allowed_part_keys(db, engineer, part_group, part_no, merge=False):
   # part_no may be a single string (or the ALL sentinel), or a list / set /
   # tuple of part numbers meaning "any of these" (multi-select on the
   # ANALYSIS and CAUSE OP pages) - normalised to a set once up front so the
   # rest of this function does not need to care which it got.
   multi = isinstance(part_no, (list, set, tuple))
   part_nos = set(part_no) if multi else None
   part_no_is_all = (not multi and part_no in (None, ALL)) or (multi and not part_nos)
   if engineer in (None, ALL) and part_group in (None, ALL) and part_no_is_all:
       return None
   keys = set()
   for p in db.all_parts(merge):
       if engineer not in (None, ALL) and (p["engineer"] or "").upper() != engineer.upper():
           continue
       if part_group not in (None, ALL) and (p["part_group"] or "") != part_group:
           continue
       if multi:
           if part_nos and p["part_no"] not in part_nos:
               continue
       elif part_no not in (None, ALL) and p["part_no"] != part_no:
           continue
       keys.add(p["part_key"] or norm_text(p["part_no"]))
       # A merged part stands for its members too: pages that filter the
       # raw material column (floor plan, FTY) need the real part numbers.
       keys.update(p.get("members") or ())
   if multi:
       for pn in (part_nos or ()):
           keys.add(norm_text(pn))
   elif part_no not in (None, ALL):
       keys.add(norm_text(part_no))
   for k in list(keys):
       g = db.merge_group(k)
       if g is not None:
           keys.update(g["members"])
   return keys


def enrich(rows, index, comp, opdesc, cause_index=None):
   """Turns raw `rw` rows into the record dicts every analysis works from.

   This runs over every single rework row - a few hundred thousand of them
   on a real export - so everything that does not actually vary row to row
   is worked out once and reused:  the part a material key belongs to, and
   the whole work-centre description block, both repeat across tens of
   thousands of rows for the same handful of parts and work centres, and
   recomputing them (each one several norm_text calls deep) per row was
   the single most expensive thing this function did.
   """
   cause_index = cause_index or {}
   out = []
   if not rows:
       return out
   # sqlite3.Row.keys() builds a fresh list on every call - the columns are
   # the same for every row, so ask once.
   # Column positions, once.  sqlite3.Row looks a name up by scanning its
   # column list on every single access, and this loop reads a couple of
   # dozen fields from each of several hundred thousand rows - reading by
   # position instead turns millions of scans into millions of array
   # lookups, which is the single biggest saving available here.
   names = list(rows[0].keys())
   pos = {n: i for i, n in enumerate(names)}
   cols = set(names)
   has_wc_desc = "wc_desc" in cols
   has_emp_no = "emp_no" in cols
   has_emp_name = "emp_name" in cols
   (i_id, i_mat, i_matkey, i_order, i_serial, i_rej, i_rejd, i_cau, i_ccode,
    i_cdesc, i_hour, i_date, i_year, i_week, i_wc, i_entered) = (
       pos["id"], pos["material"], pos["material_key"], pos["order_no"],
       pos["serial_no"], pos["reject_op"], pos["reject_op_desc"],
       pos["cause_op"], pos["cause_code"], pos["cause_desc"], pos["rw_hour"],
       pos["rw_date"], pos["iso_year"], pos["iso_week"], pos["wc_code"],
       pos["entered_by"])
   i_seq = pos["seq"] if "seq" in cols else -1
   i_wcd = pos["wc_desc"] if has_wc_desc else -1
   i_empno = pos["emp_no"] if has_emp_no else -1
   i_empname = pos["emp_name"] if has_emp_name else -1

   part_cache = {}
   wc_cache = {}
   by_part = getattr(opdesc, "by_part", None)
   # A few hundred thousand rows carry only a few tens of thousands of
   # distinct serial numbers, so normalising is remembered rather than
   # repeated.
   skey_cache = {}
   rejd_cache = {}
   out_append = out.append
   for n_row, r in enumerate(rows):
       if (n_row & 32767) == 0:
           tick()
       mk = r[i_matkey]
       hit = part_cache.get(mk)
       if hit is None:
           part = match_part(mk, index)
           # 2.1.3: this part's own traveler wording, when it has a route
           hit = part_cache[mk] = (part, by_part.get(part["part_key"] if part else mk)
                                   if by_part else None)
       part, part_desc = hit

       wc = r[i_wc] or ""
       own = clean_str(r[i_wcd]) if has_wc_desc else ""
       wc_key = (wc, own)
       cached = wc_cache.get(wc_key)
       if cached is None:
           wc_name, comp_cat = comp.get(wc, ("", UNKNOWN))
           nwc = norm_text(wc)
           # Depending on how the COMP file is arranged, the useful text
           # sits either in the name column or in the category column.
           # Show whichever actually says something, never a copy of the
           # code itself.
           # COMP is the authority on what a work centre is: it is the
           # maintained list, while the export's own text for the same
           # machine varies row to row.  The export only fills a gap COMP
           # does not cover.
           # 2.1.3: the short group name the user gave the work centre in
           # COMP comes first; COMP's long name only when there is none.
           wc_desc = (comp_cat if (comp_cat and comp_cat != UNKNOWN
                                   and norm_text(comp_cat) != nwc) else "")
           if not wc_desc and wc_name and norm_text(wc_name) != nwc:
               wc_desc = wc_name
           if not wc_desc and own and norm_text(own) != nwc:
               wc_desc = own
           cached = (wc_desc or UNKNOWN, comp_cat)
           wc_cache[wc_key] = cached
       wc_desc, comp_cat = cached

       serial = r[i_serial] or ""
       skey = skey_cache.get(serial)
       if skey is None:
           skey = skey_cache[serial] = norm_text(serial)
       # where the cause operation was carried out, and by whom
       cause_op_value = op_shown(r[i_cau] or "")
       rej_shown = op_shown(r[i_rej]) if r[i_rej] else ""
       cause_k = op_key(cause_op_value)
       c_wc, c_machine, c_emp_no, c_emp_name = cause_index.get(
           (skey, cause_k), ("", "", "", ""))
       out_append({
           "id": r[i_id], "material": r[i_mat],
           "part_no": part["part_no"] if part else r[i_mat],
           "part_key": part["part_key"] if part else mk,
           "part_group": part["part_group"] if part else "",
           "engineer": part["engineer"] if part else "",
           "serial_no": serial,
           # The grouping key every caller needs, worked out here once
           # instead of by unit_key() again in each pass over the records.
           "unit": skey or ("#%s" % r[i_id]),
           "order_no": r[i_order] or "",
           # The sequence belongs to the operation: a rework booked
           # against 280/3 is not the same station's problem as one
           # booked against 280/1.  Written the way the shop writes it
           # when there is one, plain when there is not.
           "reject_op": (("%s/%s" % (rej_shown, r[i_seq]))
                         if (i_seq >= 0 and r[i_seq] and rej_shown)
                         else (rej_shown or UNKNOWN)),
           "reject_seq": (r[i_seq] or "") if i_seq >= 0 else "",
           # opdesc is traveler-first (see Database.op_desc_map) - the
           # traveler's own text for an operation wins over the SAP export's,
           # which can vary row to row for the same operation.
           "reject_op_desc": _rejd(rejd_cache, opdesc, r[i_rej],
                                   r[i_rejd], part_desc,
                                   r[i_seq] if i_seq >= 0 else ""),
           "cause_op": cause_op_value or UNKNOWN,
           "cause_op_desc": (CAUSE_OP_LABELS.get(cause_k)
                             or (part_desc or {}).get(cause_k)
                             or opdesc.get(cause_k)
                             or r[i_cdesc] or UNKNOWN),
           "cause_wc": c_wc or UNKNOWN,
           "cause_machine": c_machine or UNKNOWN,
           "cause_emp_no": c_emp_no or UNKNOWN,
           "cause_emp_name": c_emp_name or UNKNOWN,
           "cause_code": r[i_ccode] or "",
           "rw_hour": float(r[i_hour] or 0.0),
           "rw_date": r[i_date] or "",
           "iso_year": r[i_year], "iso_week": r[i_week],
           "wc_code": wc, "wc_name": wc_desc, "comp": comp_cat,
           "emp_no": r[i_empno] if has_emp_no else "",
           "emp_name": r[i_empname] if has_emp_name else "",
           "entered_by": r[i_entered] or "",
       })
   return out


def _rejd(cache, opdesc, rej, own, part_desc=None, seq=""):
   """The reject operation's description, remembered per operation - the
   same handful of operations repeat across the whole result.  2.1.3: the
   part's own traveler first (280/3 by its sequence, then plain 280)."""
   key = (rej, own, id(part_desc), seq)
   v = cache.get(key)
   if v is None:
       v = None
       if part_desc:
           v = (part_desc.get(step_key(rej, seq)) if seq else None) \
               or part_desc.get(op_key(rej))
       v = cache[key] = (v or opdesc.get(op_key(rej)) or own or UNKNOWN)
   return v


_MISSING = object()


def unit_key(rec):
   """The serial a record belongs to.  enrich() already worked this out
   and stored it, so the common path is a plain dict hit; the fallback is
   only for records that did not come from enrich()."""
   cached = rec.get("unit") if isinstance(rec, dict) else None
   if cached:
       return cached
   return norm_text(rec["serial_no"]) or ("#%s" % rec["id"])


def time_mode_to_date_range(f, week_start):
   """
   The current filter, translated into a plain (start, end) date pair - used
   to scope the `ops` table (which only has a raw date, not a week number).
   """
   m = f.get("time_mode", "week")
   if m == "week":
       if f.get("year") and f.get("week") is not None:
           return week_bounds(f["year"], f["week"], week_start)
       return None, None
   if m == "weeks":
       ws, we = f.get("week_start"), f.get("week_end")
       if f.get("year") and ws is not None and we is not None:
           a, _ = week_bounds(f["year"], ws, week_start)
           _, b = (week_bounds(f["year"], we, week_start) if we >= ws
                   else week_bounds(f["year"] + 1, we, week_start))
           return a, b
       return None, None
   if m == "month":
       if f.get("year") and f.get("month"):
           last_day = calendar.monthrange(f["year"], f["month"])[1]
           return (datetime.date(f["year"], f["month"], 1),
                   datetime.date(f["year"], f["month"], last_day))
       return None, None
   if m == "dates":
       d1 = parse_date(f.get("date_start")) if f.get("date_start") else None
       d2 = parse_date(f.get("date_end")) if f.get("date_end") else None
       return d1, d2
   return None, None


def compute_scope_fty(db, f, defective_serial_keys):
   """
   FTY for the current filter scope: serials with no rework over every serial
   seen in production.  The `ops` table is the population of everything that
   actually went through production, since `rw` only holds defective ones.
   """
   d1, d2 = time_mode_to_date_range(
       f, int(f["wk_day"]) if f.get("wk_day") is not None
       else db.week_start())
   clauses, params = [], []
   if d1:
       clauses.append("op_date >= ?")
       params.append(d1.isoformat())
   if d2:
       clauses.append("op_date <= ?")
       params.append(d2.isoformat())
   where = " AND ".join(clauses) if clauses else "1=1"
   allowed = allowed_part_keys(db, f.get("engineer"), f.get("part_group"),
                               f.get("part_no"), bool(f.get("merge")))
   total = set()
   for r in db.conn.execute(
           "SELECT DISTINCT material, serial_key FROM ops WHERE %s" % where, params):
       if allowed is not None and norm_text(r["material"]) not in allowed:
           continue
       total.add(r["serial_key"])
   total |= defective_serial_keys       # a defective serial always counts
   if not total:
       return None
   n_bad = len(defective_serial_keys & total)
   return (len(total) - n_bad) / len(total) * 100.0


# ---------------------------------------------------------------------------
#  Lookup caches
#
#  comp_map / op_desc_map / the part index are rebuilt from scratch on every
#  analysis otherwise.  On a 350,000 row database that is most of the time it
#  takes to switch pages.  They only change when the data does, so they are
#  cached against the shared change counter.
# ---------------------------------------------------------------------------
_LOOKUP_CACHE = {"key": None, "comp": None, "opdesc": None, "index": None,
                "index_merged": None}
_WC_CACHE = {"key": None, "rows": []}
_OPDESC_PART_CACHE = {"key": None, "map": {}}
# The plant-wide operation names: three grouped queries, one over
# every production step on file.  Both the analysis lookups and the
# per-part map rebuild it, on every refresh of every page.
_OPDESC_CACHE = {"key": None, "map": {}}


def lookups(db, merge=False):
   """merge=True: the part index puts merged parts' members under the
   merged part (see Database.part_merges)."""
   key = (id(db), db.data_version())
   if _LOOKUP_CACHE["key"] != key:
       _LOOKUP_CACHE["key"] = key
       _LOOKUP_CACHE["comp"] = db.comp_map()
       _LOOKUP_CACHE["opdesc"] = OpDescMap(db.op_desc_map())
       _LOOKUP_CACHE["index"] = build_part_index(db.all_parts())
       _LOOKUP_CACHE["index_merged"] = None
   # 2.1.3: each part's own traveler wording rides along on the map, so
   # enrich() can name an operation the way THAT part's traveler does
   try:
       _LOOKUP_CACHE["opdesc"].by_part = db.traveler_desc_by_part()
   except Exception:
       pass
   if merge:
       if _LOOKUP_CACHE["index_merged"] is None:
           _LOOKUP_CACHE["index_merged"] = build_part_index(db.all_parts(True))
       return (_LOOKUP_CACHE["index_merged"], _LOOKUP_CACHE["comp"],
               _LOOKUP_CACHE["opdesc"])
   return (_LOOKUP_CACHE["index"], _LOOKUP_CACHE["comp"], _LOOKUP_CACHE["opdesc"])


class OpDescMap(dict):
   """{operation key: description} plus `by_part` - see lookups()."""
   by_part = None


_CACHE_GEN = [0]


def invalidate_caches():
   _CACHE_GEN[0] += 1
   _LOOKUP_CACHE["key"] = None
   _WC_CACHE["key"] = None
   _OPDESC_PART_CACHE["key"] = None
   _OPDESC_CACHE["key"] = None
   _FLAG_PART_CACHE["key"] = None
   _SERIAL_MAT_CACHE["key"] = None
   _WIP_CACHE.clear()
   _RESULT_CACHE.clear()
   _LIVE_CACHE.clear()
   _PARTDATA_CACHE.clear()


_WIP_CACHE = {}
_RESULT_CACHE = {}
# Where every serial is - the floor plan's whole answer.  It costs a
# quarter of a second on a real export and nothing in it changes until the
# data does, yet it was recomputed on every filter tick and every tab
# visit.  One entry per (filter, data version); the version is part of the
# key, so a stale one can never be handed back.
_LIVE_CACHE = {}
# One part's whole operation history.  The bottleneck page reads it three
# times in one refresh (matrix, flow, wait ranking) and on a big export
# that is a quarter of a second each.  Keyed on the data version, so it
# can never hand back something stale.
_PARTDATA_CACHE = {}


def wip_serial_keys(db, part_keys=None):
   """
   WIP = a serial that has not been sold/shipped yet (see
   `Database.sold_serial_keys`, built from the shipment report).  Finishing
   every traveler operation is not enough to drop a serial out of WIP by
   itself - it can sit fully built in finished-goods for a while before it
   actually ships, and until then it is still inventory on the floor, not a
   closed unit; only the shipment report saying so takes it out.

   Returns {(material key, serial key), ...}.  Empty when no traveler route
   is loaded, since without a route there is no serial universe to test.
   """
   cache_key = (getattr(db, "cache_id", id(db)), db.data_version(), tuple(sorted(part_keys)) if part_keys else None)
   hit = _WIP_CACHE.get(cache_key)
   if hit is not None:
       return hit
   sold = db.sold_serial_keys()
   part_keys_with_route = db.wf_last_ops()
   wip = set()
   for part_key in part_keys_with_route:
       if part_keys and part_key not in part_keys:
           continue
       mat_keys = db.wf_resolve_material_keys(part_key)
       if not mat_keys:
           continue
       ph = ",".join("?" * len(mat_keys))
       seen = {r["serial_key"] for r in db.conn.execute(
           "SELECT DISTINCT serial_key FROM wf_part_data WHERE material_key IN (%s)"
           % ph, tuple(mat_keys))}
       for sk in seen - sold:
           wip.add((part_key, sk))
   _WIP_CACHE.clear()
   _WIP_CACHE[cache_key] = wip
   return wip


def wip_serials_flat(db):
   """Just the serial keys, for the analysis page where the part of a rework
   row is matched through the part index rather than the traveler key."""
   return {sk for _pk, sk in wip_serial_keys(db)}


def run_analysis(db, f):
   # f["merge"]: this page shows merged parts as one (DATA > MERGED PARTS).
   merge = bool(f.get("merge"))
   index, comp, opdesc = lookups(db, merge)
   where, params = build_time_clause(f)
   # With a part selected, operation names come from THAT part's traveler -
   # the same operation number can be worded differently on another route.
   scope_parts = allowed_part_keys(db, f.get("engineer"), f.get("part_group"),
                                   f.get("part_no"), merge)
   if scope_parts:
       opdesc = db.op_desc_map_for(scope_parts)
   # No ORDER BY: the rows are grouped by serial and each group sorted by
   # date further down anyway, so sorting a few hundred thousand rows in
   # SQLite first is work whose result is immediately discarded.
   rows = db.conn.execute(
       "SELECT * FROM rw WHERE %s" % where, params).fetchall()

   allowed = scope_parts
   # Filter on the raw rows first: enrich() is the expensive step, so the
   # fewer rows reach it the better.  Every serial number is normalised
   # exactly once here and carried alongside its row - the filters below,
   # the cause-operation index and enrich() all need it, and norm_text on
   # a few hundred thousand rows four times over was pure waste.
   if allowed is not None:
       part_of = {}
       kept = []
       for r in rows:
           mk = r["material_key"]
           pk = part_of.get(mk, _MISSING)
           if pk is _MISSING:
               pk = (match_part(mk, index) or {}).get("part_key", mk)
               part_of[mk] = pk
           if pk in allowed:
               kept.append(r)
       rows = kept

   # Normalising every serial number costs a pass over the whole result,
   # and most views never filter on serials at all - so it only happens
   # when one of the filters below, the cause-operation index or the flag
   # colouring is actually going to read it.
   needs_serials = bool(
       f.get("wip_only") or f.get("serials") or f.get("exclude_flag_kinds")
       or f.get("only_flag_kinds") or f.get("need_cause_details"))
   if needs_serials:
       pairs = [(r, norm_text(r["serial_no"])) for r in rows]
   else:
       pairs = [(r, "") for r in rows]
   if f.get("wip_only"):
       wip = wip_serials_flat(db)
       pairs = [p for p in pairs if p[1] in wip]
   rej = {op_key(x) for x in (f.get("reject_ops") or [])} or None
   if rej:
       pairs = [p for p in pairs if op_key(p[0]["reject_op"]) in rej]
   cau = {op_key(x) for x in (f.get("cause_ops") or [])} or None
   if cau:
       pairs = [p for p in pairs if op_key(p[0]["cause_op"]) in cau]
   sel_serials = {norm_text(x) for x in (f.get("serials") or [])} or None
   if sel_serials:
       pairs = [p for p in pairs if p[1] in sel_serials]
   exclude_kinds = set(f.get("exclude_flag_kinds") or ())
   if exclude_kinds:
       bad = db.flag_keys(exclude_kinds)
       if bad:
           pairs = [p for p in pairs if p[1] not in bad]
   only_kinds = set(f.get("only_flag_kinds") or ())
   if only_kinds:
       keep = db.flag_keys(only_kinds)
       pairs = [p for p in pairs if p[1] in keep]

   rows = [p[0] for p in pairs]
   # Where the cause operation was carried out, and by whom, is only ever
   # read by the PER RAW DATA table's last four columns and the Excel
   # export.  Working it out means indexing the production-step table, so
   # it is skipped entirely unless somebody is actually going to look at
   # it - which is what `need_cause_details` says.  Only the (serial,
   # cause operation) combinations these rows ask about are indexed even
   # then; an entry per operation per serial would be a dictionary the
   # size of the whole production-step table.
   if f.get("need_cause_details"):
       wanted_pairs = {(sk, op_key(r["cause_op"])) for r, sk in pairs}
       cause_index = db.cause_op_index({sk for _r, sk in pairs},
                                       wanted_pairs=wanted_pairs)
   else:
       cause_index = None
   del pairs
   tick("Analysing...   %s rows" % f"{len(rows):,}")
   records = enrich(rows, index, comp, opdesc, cause_index)
   # Week labels on the page's own week start (see build_time_clause).
   if f.get("wk_day") is not None:
       relabel_weeks(records, int(f["wk_day"]))

   tick("Analysing...   grouping %s records" % f"{len(records):,}")
   groups = {}
   for r in records:
       groups.setdefault(r["unit"], []).append(r)
   for g in groups.values():
       g.sort(key=lambda x: (x["rw_date"] or "9999-99-99", x["id"]))
   ordered = sorted(groups.items(),
                    key=lambda kv: (kv[1][0]["rw_date"] or "9999-99-99",
                                    kv[1][0]["serial_no"]))

   # One pass builds the cross-tab, all four group-by breakdowns and the
   # totals together.  Each of these used to walk every record on its own,
   # and the reject/cause key lists used `in` on a plain list - a linear
   # scan per row, which on a full-year export was the slowest line here.
   reject_keys, cause_keys, matrix = {}, {}, {}
   by_comp, by_engineer, by_part, by_week = {}, {}, {}, {}
   defective_serial_keys = set()
   part_nos = set()
   total_hour = 0.0

   def _bucket(store, key, unit, hour):
       g = store.get(key)
       if g is None:
           g = store[key] = {"units": set(), "hour": 0.0, "rows": 0}
       g["units"].add(unit)
       g["hour"] += hour
       g["rows"] += 1

   tick("Analysing...   building the cross-tab")
   for n_rec, r in enumerate(records):
       if (n_rec & 32767) == 0:
           tick()
       unit = r["unit"]
       hour = r["rw_hour"]
       rk = (r["reject_op"], r["reject_op_desc"])
       ck = (r["cause_op"], r["cause_op_desc"])
       reject_keys[rk] = None
       cause_keys[ck] = None
       cell = matrix.get((rk, ck))
       if cell is None:
           cell = matrix[(rk, ck)] = {"units": set(), "hour": 0.0, "rows": 0}
       cell["units"].add(unit)
       cell["hour"] += hour
       cell["rows"] += 1
       _bucket(by_comp, r["comp"] or UNKNOWN, unit, hour)
       _bucket(by_engineer, r["engineer"] or UNKNOWN, unit, hour)
       _bucket(by_part, r["part_no"] or UNKNOWN, unit, hour)
       _bucket(by_week, r["iso_week"] or UNKNOWN, unit, hour)
       defective_serial_keys.add(unit)
       part_nos.add(r["part_no"])
       total_hour += hour

   reject_keys = sorted(reject_keys, key=lambda x: x[0])
   cause_keys = sorted(cause_keys, key=lambda x: x[0])
   return {
       "filters": f, "records": records, "groups": ordered,
       "part_count": len(defective_serial_keys),
       "rw_hour": total_hour,
       "row_count": len(records),
       "distinct_parts": len(part_nos),
       "reject_keys": reject_keys, "cause_keys": cause_keys, "matrix": matrix,
       "by_comp": by_comp, "by_engineer": by_engineer,
       "by_part": by_part, "by_week": by_week,
       "unknown_work_centers": sorted({wc for wc in
                                       (r["wc_code"] for r in records)
                                       if wc and wc not in comp}),
       "fty": compute_scope_fty(db, f, defective_serial_keys),
   }


def data_health(db):
   """What the answers are standing on, and where it is thin.

   Every position the app reports rests on three things being present and
   right: the traveler's standard times, the machine group on each step,
   and SAP's machine time on each booking.  When one is missing the app
   does not fail - it quietly falls back to a cruder reading, which is
   worse, because nothing on screen says so.  This is that list.

   Returns a list of checks, each with a level:

       "ok"    nothing to do
       "warn"  works, but some answers are being read the old way
       "bad"   something the app needs is not there at all
   """
   out = []

   def add(level, title, detail, rows=None):
       out.append({"level": level, "title": title, "detail": detail,
                   "rows": rows or []})

   # --- an upgrade that did not finish ----------------------------------
   # The schema upgrade rebuilds wf_part_data so that dt_ops (the last
   # NON-REWORK entry) is filled in.  If that rebuild failed, every
   # operation looks like it only ever carried rework and the position
   # rule falls back to counting rework stamps - which is exactly the bug
   # the rebuild exists to remove.  Silence here would be the worst of
   # both worlds, so it is the first thing this panel says.
   try:
       kusur = db.get_meta("partdata_rebuild_failed", "")
   except Exception:
       kusur = ""
   try:
       bekleyen = db.get_meta("partdata_pending", "")
   except Exception:
       bekleyen = ""
   if bekleyen and not kusur:
       add("bad", "WATERFALL DATA",
           "The last SAP upload (%s) reached the RW tables but the "
           "waterfall's operation table was not rebuilt yet, so the "
           "waterfall still shows the data from before it.  Re-open the "
           "program or re-upload the SAP export." % str(bekleyen)[:19])
   if kusur:
       add("bad", "UPGRADE",
           "The operation table was not rebuilt after the last upgrade "
           "(%s).  Until it is, a part's position is worked out from "
           "rework entries as well as production ones, which can put a "
           "finished part back at an earlier operation.  Re-upload the "
           "SAP export, or ask for the rebuild to be run again."
           % str(kusur)[:120])

   # --- the traveler ----------------------------------------------------
   parts = db.wf_parts()
   if not parts:
       add("bad", "TRAVELER", "No route loaded at all - without one there "
                              "is no operation order to place a part in.")
       return out

   missing_std, missing_grp, total_steps = [], [], 0
   groups = {norm_text(g) for g in work_centers_by_group(db)}
   unknown_group = {}
   for part_key, shown in parts:
       for step in db.wf_traveler_for(part_key):
           total_steps += 1
           if not step["std_hour"]:
               missing_std.append((shown, step["op"], step["desc"]))
           if not step["wc_group"]:
               missing_grp.append((shown, step["op"], step["desc"]))
           elif norm_text(step["wc_group"]) not in groups:
               unknown_group.setdefault(step["wc_group"], 0)
               unknown_group[step["wc_group"]] += 1

   if not missing_std:
       add("ok", "STANDARD TIMES",
           "Every one of the %d route steps has a standard time." % total_steps)
   else:
       add("bad" if len(missing_std) == total_steps else "warn",
           "STANDARD TIMES",
           "%d of %d route steps have no standard time.  On those steps the "
           "app cannot tell PROCESSING from WAITING and falls back to "
           "'a booking means it happened'."
           % (len(missing_std), total_steps),
           [["%s" % p, "%s" % op, d or ""] for p, op, d in missing_std[:400]])

   if not missing_grp and not unknown_group:
       add("ok", "MACHINE GROUPS",
           "Every route step names a machine group, and every name exists "
           "in COMP.")
   else:
       detail = []
       if missing_grp:
           detail.append("%d route steps name no machine group" % len(missing_grp))
       if unknown_group:
           detail.append("%d group names are not in COMP (%s)"
                         % (len(unknown_group),
                            ", ".join(sorted(unknown_group)[:6])))
       add("warn", "MACHINE GROUPS",
           "; ".join(detail) + ".  Waiting areas defined by machine group "
           "cannot collect parts for these steps.",
           [["%s" % p, "%s" % op, d or ""] for p, op, d in missing_grp[:400]]
           + [[g, "NOT IN COMP", "%d step(s)" % n]
              for g, n in sorted(unknown_group.items())])

   # --- machine time on the bookings ------------------------------------
   n, with_machine = db.wf_machine_time_counts()
   if not n:
       add("bad", "MACHINE TIME", "No SAP data loaded.")
   elif with_machine == 0:
       add("bad", "MACHINE TIME",
           "None of the %d operation records carry machine time.  This data "
           "was imported before the Activity to conf column (U) was read, so "
           "every position is being worked out from LABOUR hours - which run "
           "a part through its operation faster than the machine did.  "
           "Re-upload the SAP export to fix it." % n)
   elif with_machine < n:
       add("warn", "MACHINE TIME",
           "%d of %d operation records have no machine time and fall back to "
           "labour hours." % (n - with_machine, n))
   else:
       add("ok", "MACHINE TIME",
           "All %d operation records carry machine time." % n)

   # --- work centres the plan and the data disagree about ---------------
   unknown_wc = db.unknown_work_centers()
   if unknown_wc:
       add("warn", "WORK CENTRES",
           "%d work centre(s) appear in the data but have no COMP row, so "
           "they have no name and belong to no machine group."
           % len(unknown_wc),
           [[w, "", ""] for w in sorted(unknown_wc)[:400]])
   else:
       add("ok", "WORK CENTRES", "Every work centre in the data is in COMP.")

   # --- parts with a route but no data, and the other way round ---------
   have_data = set(db.wf_material_keys())
   no_data = [shown for part_key, shown in parts
              if not (set(db.wf_resolve_material_keys(part_key)) & have_data)]
   if no_data:
       add("warn", "PARTS WITHOUT DATA",
           "%d part(s) have a traveler route but no SAP records: %s"
           % (len(no_data), ", ".join(no_data[:8])))
   else:
       add("ok", "PARTS", "Every part with a route has SAP records.")
   return out


def work_centers_by_group(db):
   """{group name: [work centre codes]} straight out of COMP.

   The traveler names the family an operation needs - "operation 80 wants a
   LIECHTI" - and COMP's class column is where that name turns into actual
   machine numbers.  Matching is done on a normalised name so LIECHTI,
   Liechti and "LIECHTI " are one group.
   """
   out = {}
   for r in db.conn.execute("SELECT work_center, comp FROM comp"):
       g = norm_text(r["comp"])
       if g and r["work_center"]:
           out.setdefault(g, []).append(r["work_center"])
   return out


def traveler_group_report(db, routes):
   """2.1.3: the traveler's MACHINE GROUP names matched against COMP, for
   the upload log.  One line per name in the file: the COMP machines it
   finds, or that it finds none.  A name that is itself a work centre
   code counts as that machine (the same rule the capacity count uses).
   Returns (lines, matched names, unmatched names)."""
   by_group = work_centers_by_group(db)
   codes = {norm_text(r["work_center"]): r["work_center"]
            for r in db.conn.execute("SELECT work_center FROM comp")
            if r["work_center"]}
   names, steps, empty = {}, {}, 0
   for route in routes.values():
      for c in route:
         g = clean_str(c.get("wc_group"))
         if not g:
            empty += 1
            continue
         k = norm_text(g)
         names.setdefault(k, g)
         steps[k] = steps.get(k, 0) + 1
   lines, ok, bad = [], [], []
   for k in sorted(names, key=lambda x: names[x]):
      if k in by_group:
         ms = sorted(by_group[k])
         lines.append("%-24s -> COMP: %d machine(s)  %s%s  (%d step(s))"
                      % (names[k], len(ms), ", ".join(ms[:8]),
                         " ..." if len(ms) > 8 else "", steps[k]))
         ok.append(names[k])
      elif k in codes:
         lines.append("%-24s -> COMP: work centre %s  (%d step(s))"
                      % (names[k], codes[k], steps[k]))
         ok.append(names[k])
      else:
         lines.append("%-24s -> NOT IN COMP  (%d step(s))" % (names[k], steps[k]))
         bad.append(names[k])
   if empty:
      lines.append("%d step(s) have no machine group" % empty)
   return lines, ok, bad


def live_positions(db, part_keys=None, d1=None, d2=None, exclude_sold=True):
   """Where every serial was last seen, and what it is waiting for.

   The SAP time entries say that an operation WAS COMPLETED at a machine on
   a date.  They do not say a part is being cut right now - nothing in the
   data does - so this reports what is actually knowable:

       last_wc   the work centre of the serial's most recent booking
       last_op   the operation it completed there
       next_op   the next operation on that part's traveler, i.e. what it
                 is waiting for
       days      how long since that booking

   With standard times on the traveler there is one more thing the data
   does support.  Labour is booked a shift at a time, so on a step longer
   than a shift the booked hours can be compared with the standard time to
   tell whether the operation has actually finished:

       running   the booked labour has not reached the standard time, so
                 the part is still ON that machine - `remaining` says
                 roughly how much longer, `eta` when it should come off
       waiting   the step is finished and the part is queueing for `next_op`

   Every serial carries `booked` / `std_hour` / `pct` so the plan can show
   a filling counter rather than just a date.  Parts whose traveler has no
   standard time keep the old reading: a booking means the step happened.
   """
   cache_key = (getattr(db, "cache_id", id(db)), db.data_version(),
                tuple(sorted(part_keys)) if part_keys else None,
                d1.isoformat() if d1 else None,
                d2.isoformat() if d2 else None, bool(exclude_sold))
   hit = _LIVE_CACHE.get(cache_key)
   if hit is not None:
       return hit

   rows = db.latest_booking(part_keys=part_keys, d1=d1, d2=d2)
   sold = db.sold_serial_keys() if exclude_sold else set()
   today = datetime.date.today()
   now = datetime.datetime.now()
   routes, stds, groups = {}, {}, {}
   step_keys, step_ops = {}, {}
   # Booked labour for every serial on screen in one query rather than one
   # per part sitting on the plan.
   labour = db.labour_by_op({norm_text(r["material"]) for r in rows},
                            {r["serial_key"] for r in rows})
   # Which family of machines an operation needs.  The traveler says so
   # directly; where it does not, the work centre that has historically run
   # that operation is looked up in COMP instead, so a partly filled
   # traveler still puts parts in the right waiting area.
   comp_of = {r["work_center"]: norm_text(r["comp"])
              for r in db.conn.execute("SELECT work_center, comp FROM comp")}
   # v71: a pass over the whole production table - worked out once per
   # data version and kept (also in the file, for the other copies)
   def _wc_rows():
       return [[r["material"], r["op_no"], r["seq"], r["wc_code"]]
               for r in db.conn.execute(
                   "SELECT material, op_no, COALESCE(seq,'') AS seq, wc_code, "
                   "COUNT(*) n FROM ops WHERE wc_code<>'' "
                   "GROUP BY material, op_no, seq, wc_code ORDER BY n DESC")]
   wc_of_op = {}
   for mat, op_no, seq, wc in db._agg("wc_of_op", _wc_rows):
       wc_of_op.setdefault((norm_text(mat), step_key(op_no, seq)), wc)
   out = []
   for r in rows:
       sk = r["serial_key"]
       if sk in sold:
           continue
       mk = norm_text(r["material"])
       route = routes.get(mk)
       if route is None:
           steps = db.wf_traveler_for(mk)
           # The route is walked by STEP - an operation written 280/1,
           # 280/3, 280/4 is three places a part can be, each with its
           # own booking and its own standard time.  `route` keeps what
           # the shop calls each one (280/1) for display.
           route = routes[mk] = [c["label"] for c in steps]
           stds[mk] = {c["step"]: c["std_hour"] for c in steps}
           groups[mk] = {c["step"]: norm_text(c["wc_group"])
                         for c in steps if c["wc_group"]}
           step_keys[mk] = [c["step"] for c in steps]
           step_ops[mk] = [c["op"] for c in steps]
       keys = step_keys[mk]
       # WHERE THE PART IS: the FURTHEST operation on the route carrying a
       # production entry - not the most recent one by date.  The
       # waterfall walks the route the same way (op_progress_for_route);
       # if these two disagree the floor plan and the matrix put the same
       # serial in two different places, which has happened before and is
       # the hardest kind of wrong to notice.
       #
       # latest_booking() hands over one row per serial chosen by
       # timestamp, because it has no route to consult.  Here the route IS
       # known, so the choice is made again properly and the row's own
       # operation is only the fallback for a serial whose bookings are
       # all off-route.
       def kayit(k, _sk=sk):
           # A step's own bookings only (v62): a booking is never lent to
           # another sequence of its operation.
           return labour.get((_sk, k))

       ileri = -1
       for pos, k in enumerate(keys):
           vals = kayit(k)
           if vals and len(vals) > 2 and vals[2] is not None:
               ileri = pos
       if ileri < 0:
           # Nothing but rework anywhere: the furthest rework entry is all
           # there is to go on, same exception the waterfall makes.
           for pos, k in enumerate(keys):
               if kayit(k):
                   ileri = pos
       # A booking's own step: its operation plus the sequence it names.
       ok = step_key(r["op_no"], row_get(r, "seq"))
       nxt = nxt_key = ""
       if ileri >= 0:
           ok = keys[ileri]
           if ileri + 1 < len(route):
               nxt, nxt_key = route[ileri + 1], keys[ileri + 1]
       elif ok in keys:
           i = keys.index(ok)
           if i + 1 < len(route):
               nxt, nxt_key = route[i + 1], keys[i + 1]
       d = parse_date(r["op_date"])
       vals = kayit(ok) or (0.0, None, None, False)
       booked, last_dt = vals[0], vals[1]
       bitti_x = vals[3] if len(vals) > 3 else False
       # The work centre has to follow the operation the part is actually
       # at; the row's wc_code belongs to whatever booking happened to be
       # newest, which is now a different operation.
       wc_here = ((r["wc_code"] or "")
                  if step_key(r["op_no"], row_get(r, "seq")) == ok else "")
       if not wc_here:
           wc_here = wc_of_op.get((mk, ok), "") or (r["wc_code"] or "")
       p = op_progress(booked, stds.get(mk, {}).get(ok), last_dt, now)
       # THE X DECIDES (v62), exactly as on the waterfall: closed by a
       # non-setup X -> off this step, waiting for the next; otherwise it
       # is being worked here - however many hours are on it.
       uretimde = bool(vals and len(vals) > 2 and vals[2] is not None)
       if p is not None:
           if bitti_x:
               p = dict(p, running=False, idle=False, remaining=0.0,
                        pct=1.0, pct_est=1.0, complete=True)
           else:
               p = dict(p, running=uretimde, complete=False)
       nk = nxt_key
       next_group = groups.get(mk, {}).get(nk, "")
       if not next_group and nk:
           next_group = comp_of.get(wc_of_op.get((mk, nk), ""), "")
       rec = {
           "serial_key": sk, "serial": r["serial_no"] or sk,
           "material": r["material"], "part_key": mk,
           "last_op": (route[ileri] if ileri >= 0 else r["op_no"]),
           "last_wc": wc_here,
           "next_op": str(nxt or ""), "next_group": next_group,
           "date": r["op_date"],
           # The booking's own timestamp, not just its date - deciding
           # which of two parts at one machine is the live one needs the
           # hour, and the ops table only carries the day.
           "last_entry": last_dt,
           "days": (today - d).days if d else None,
           "booked": booked, "std_hour": None, "pct": None,
           "remaining": None, "running": False, "stalled": False,
           "waited": 0.0, "booked_pct": None, "queued": False, "eta": None}
       if p is not None:
           rec.update({
               "std_hour": p["std"], "pct": p["pct_est"],
               "remaining": p["remaining"], "running": p["running"],
               "stalled": p["idle"], "waited": p["waited"],
               "booked_pct": p["pct_booked"]})
           if p["running"] and p["remaining"] > 0:
               rec["eta"] = now + datetime.timedelta(hours=p["remaining"])
       out.append(rec)
   out = _one_part_per_machine(db, out)
   # One filter at a time is what anybody looks at; the cache is a handful
   # of entries, not a leak.
   if len(_LIVE_CACHE) > 8:
       _LIVE_CACHE.clear()
   _LIVE_CACHE[cache_key] = out
   return out


# Machine groups that genuinely hold several parts at once - a cleaning
# line, an etch tank, an FPI booth.  These are COMP CLASS names, matched
# whole, exactly as the waiting zones match them.
#
# They used to be matched as a substring of the class OR the work centre's
# description, which is far too loose: a milling centre described "INSPECT
# ALCOHOL CLEAN" was read as a cleaning line, and 364 parts came up as
# being cut on it at once.  Editable in DATA & SETTINGS - see apply_rules().
MULTI_PART_GROUPS = ("ULTRAPOLISH", "CLEAN", "BLUE ETCH", "FPI")


def _one_part_per_machine(db, rows):
   """A machine can only be cutting one part at a time.

   Several serials can carry an unfinished booking at the same work centre
   - one is on it, the others were left there or are queued for it.  The
   floor cannot tell them apart from the bookings alone, but the most
   recently booked one is the one somebody is working on, so that serial
   keeps `running` and the rest are reported as standing at the machine.

   Ultrapolish cells really do run several parts together, so they are
   left alone.
   """
   comp_of = {r["work_center"]: norm_text(r["comp"])
              for r in db.conn.execute("SELECT work_center, comp FROM comp")}
   multi = set(MULTI_PART_GROUPS)

   def many_at_once(wc):
       return comp_of.get(wc, "") in multi

   by_wc = {}
   for r in rows:
       if r.get("running") and r["last_wc"] and not many_at_once(r["last_wc"]):
           by_wc.setdefault(r["last_wc"], []).append(r)
   for wc, here in by_wc.items():
       if len(here) < 2:
           continue
       # Latest time entry wins - that is the one somebody is on.  Where
       # two carry the same stamp the one furthest through its operation
       # takes it, being the one about to come off.
       #
       # The serial number is the last word.  Two parts can carry the same
       # stamp AND the same percentage (SAP writes a whole day at once, and
       # identical parts take identical hours); without a final tie-break
       # the winner depended on the order the rows happened to arrive in,
       # so the plan could show a different part on the machine from one
       # refresh to the next with nothing in the data having changed.
       here.sort(key=lambda r: (r["last_entry"] or datetime.datetime.min,
                                r.get("pct") or 0.0,
                                r.get("serial_key") or r.get("serial") or ""))
       for r in here[:-1]:
           r["running"] = False
           r["queued"] = True
           r["eta"] = None
   for r in rows:
       r.setdefault("queued", False)
   return rows


def totals_by_part(db, f):
   """Rework hours and rejected parts for EVERY part in the time window.

   Deliberately ignores the engineer / part-group / part selection: this is
   the shop-floor picture the current selection is one slice of, so you can
   see at a glance whether the part you are looking at is the problem or
   just one of many.  Laid out the way the ENG x PART file is - engineer,
   group, part - so it reads like the list people already know.
   """
   where, params = build_time_clause(f)
   rows = db.conn.execute(
       "SELECT material_key, COUNT(DISTINCT UPPER(REPLACE(serial_no,' ',''))) n, "
       "COALESCE(SUM(rw_hour), 0) h, COUNT(*) e "
       "FROM rw WHERE %s GROUP BY material_key" % where, params).fetchall()
   index = build_part_index(db.all_parts(bool(f.get("merge"))))
   out = []
   merged = {}
   for r in rows:
       mk = r["material_key"]
       part = match_part(mk, index)
       rec = {
           "part_no": (part or {}).get("part_no") or mk or UNKNOWN,
           "part_group": (part or {}).get("part_group") or "",
           "engineer": (part or {}).get("engineer") or "",
           "parts": r["n"], "hour": r["h"], "entries": r["e"]}
       # A merged part's members are ONE row: their serials and hours
       # added up (a serial belongs to one material number).
       if part and part.get("members"):
           have = merged.get(rec["part_no"])
           if have is not None:
               for k in ("parts", "hour", "entries"):
                   have[k] += rec[k]
               continue
           merged[rec["part_no"]] = rec
       out.append(rec)
   out.sort(key=lambda x: -x["hour"])
   return out


def all_parts_totals(db, f):
   """
   Rejected parts and rework hours for EVERY part in the same time window,
   ignoring the engineer / part group / part selection - the shop floor
   total that the current selection is a slice of.  A plain aggregate, so
   it costs nothing next to the analysis itself.
   """
   where, params = build_time_clause(f)
   row = db.conn.execute(
       "SELECT COUNT(DISTINCT serial_no) n, COALESCE(SUM(rw_hour), 0) h "
       "FROM rw WHERE %s" % where, params).fetchone()
   return (row["n"] or 0), float(row["h"] or 0.0)


def cause_op_trend(db, f, weeks):
   """Each cause operation's share of the rework hours, week by week.

   The CAUSE OP TREND module used to get this by running the WHOLE
   analysis once per week - eight enrichments of the entire export, eight
   part-index walks, eight scope-FTY passes - to end up using one number
   from each.  On a large export that was most of a second every time the
   page was opened, and it evicted the page's own result from the cache on
   the way through, so the next page had to recompute as well.

   What it actually needs is a sum per (week, cause operation), which is
   one grouped query.  Only the scope filters apply: the trend walks its
   own weeks, so the time filter is replaced, and anything narrowing by
   serial is handled by the caller falling back to the full analysis.

   `weeks` is [(iso_year, iso_week), ...].  Returns
   {(year, week): {"total": hours, "by_op": {cause op: hours}}}.
   """
   out = {(y, w): {"total": 0.0, "by_op": {}} for y, w in weeks}
   if not weeks:
       return out
   pairs = sorted(set(weeks))
   wk_day = f.get("wk_day")
   if wk_day is not None:
       # The page's own weeks (see build_time_clause): the rows are read
       # by DATE over the whole span and each date put in its own week.
       off = week_offset(int(wk_day))
       spans = [week_bounds(y, w, int(wk_day)) for y, w in pairs]
       spans = [sp for sp in spans if sp[0]]
       if not spans:
           return out
       lo = min(a for a, _b in spans).isoformat()
       hi = max(b for _a, b in spans).isoformat()
       iso_of = {}

       def week_of(ds):
           yw = iso_of.get(ds)
           if yw is None:
               d = parse_date(ds)
               yw = iso_of[ds] = iso_year_week(d, off) if d else None
           return yw
       source = ("SELECT rw_date d, material_key mk, cause_op op, "
                 "SUM(rw_hour) h FROM rw WHERE rw_date BETWEEN ? AND ? "
                 "GROUP BY rw_date, material_key, cause_op")
       sparams = [lo, hi]
   else:
       week_of = None
       clause = " OR ".join("(iso_year=? AND iso_week=?)" for _ in pairs)
       source = ("SELECT iso_year y, iso_week w, material_key mk, "
                 "cause_op op, SUM(rw_hour) h "
                 "FROM rw WHERE (%s) GROUP BY iso_year, iso_week, "
                 "material_key, cause_op" % clause)
       sparams = [v for pair in pairs for v in pair]
   merge = bool(f.get("merge"))
   scope = allowed_part_keys(db, f.get("engineer"), f.get("part_group"),
                             f.get("part_no"), merge)
   index = build_part_index(db.all_parts(merge)) if scope is not None else None
   part_of = {}
   for r in db.conn.execute(source, sparams):
       if scope is not None:
           mk = r["mk"]
           pk = part_of.get(mk, _MISSING)
           if pk is _MISSING:
               pk = (match_part(mk, index) or {}).get("part_key", mk)
               part_of[mk] = pk
           if pk not in scope:
               continue
       key = (week_of(r["d"]) if week_of is not None
              else (r["y"], r["w"]))
       cell = out.get(tuple(key) if key else None)
       if cell is None:
           continue
       hours = r["h"] or 0.0
       cell["total"] += hours
       op = op_shown(r["op"]) if r["op"] else UNKNOWN     # 0600 = 600
       cell["by_op"][op] = cell["by_op"].get(op, 0.0) + hours
   return out


def run_analysis_cached(db, f):
   """
   Switching back to a page that is already showing the right answer should
   not recompute it.  The signature covers everything the result depends on.

   `need_cause_details` is deliberately left out of that signature: a
   result computed WITH the cause-operation details is a superset of the
   same result without them, so opening and closing the PER RAW DATA panel
   reuses what is already in hand instead of re-running the whole analysis
   each way.  Only one result is kept - on a full-year export it holds a
   few hundred thousand records, and a second copy is real memory.
   """
   want_details = bool(f.get("need_cause_details"))
   key_f = {k: v for k, v in f.items() if k != "need_cause_details"}
   sig = (id(db), db.data_version(), json.dumps(key_f, sort_keys=True, default=str))
   hit = _RESULT_CACHE.get(sig)
   if hit is not None:
       res, had_details = hit
       if had_details or not want_details:
           return res
   res = run_analysis(db, f)
   _RESULT_CACHE.clear()          # one entry is enough; keeps memory flat
   _RESULT_CACHE[sig] = (res, want_details)
   return res


def serial_history(db, serial):
   index, comp, opdesc = lookups(db)
   rows = db.conn.execute(
       "SELECT * FROM rw WHERE UPPER(REPLACE(serial_no,' ','')) = ? ORDER BY rw_date, id",
       (norm_text(serial),)).fetchall()
   recs = enrich(rows, index, comp, opdesc,
                 db.cause_op_index({norm_text(serial)}))
   recs.sort(key=lambda x: (x["rw_date"] or "9999-99-99", x["id"]))
   return recs


def materials_for_serials(db, serial_keys):
   """{serial_key: material key} for many serials in two queries, not two
   per serial - from the production steps, else the rework table."""
   keys = [k for k in dict.fromkeys(serial_keys) if k]
   if not keys:
       return {}
   cache = _SERIAL_MAT_CACHE
   ck = (id(db), db.data_version())
   if cache["key"] != ck:
       cache["key"], cache["map"] = ck, {}
   known = cache["map"]
   todo = [k for k in keys if k not in known]
   CHUNK = 400                     # stays clear of any SQLite parameter limit
   for i in range(0, len(todo), CHUNK):
       part = todo[i:i + CHUNK]
       ph = ",".join("?" * len(part))
       mat = {}
       for r in db.conn.execute(
               "SELECT serial_key, material FROM ops WHERE serial_key IN (%s) "
               "GROUP BY serial_key" % ph, part):
           mat[r["serial_key"]] = norm_text(r["material"])
       missing = [k for k in part if k not in mat]
       if missing:
           ph2 = ",".join("?" * len(missing))
           for r in db.conn.execute(
                   "SELECT UPPER(REPLACE(serial_no,' ','')) AS sk, material_key "
                   "FROM rw WHERE UPPER(REPLACE(serial_no,' ','')) IN (%s) "
                   "GROUP BY sk" % ph2, missing):
               mat[r["sk"]] = r["material_key"]
       for k in part:
           known[k] = mat.get(k)
   return {k: known.get(k) for k in keys}


_SERIAL_MAT_CACHE = {"key": None, "map": {}}


def parts_for_serials(db, index, serial_keys):
   """{serial_key: part} for many serials in two queries, not two per serial.

   The MRB/SCRAP/TECO page needs the part behind every flagged serial on
   every refresh.  Asking one serial at a time is fine on a local disk and
   ruinous on a shared folder, where each question is a separate round
   trip: four hundred flagged serials meant eight hundred of them before a
   single row appeared on screen.
   """
   keys = [k for k in dict.fromkeys(serial_keys) if k]
   if not keys:
       return {}
   cache = _FLAG_PART_CACHE
   ck = (id(db), db.data_version(), bool(index.get("merge")))
   if cache["key"] != ck:
       cache["key"], cache["map"] = ck, {}
   known = cache["map"]
   todo = [k for k in keys if k not in known]
   mat = materials_for_serials(db, todo)
   for k in todo:
       mk = mat.get(k)
       known[k] = match_part(mk, index) if mk else None
   return {k: known.get(k) for k in keys}


_FLAG_PART_CACHE = {"key": None, "map": {}}


# ===========================================================================
#  7. EXCEL EXPORT
# ===========================================================================

RAW_EXPORT_COLS = [
   ("part_no", "PART NO"), ("serial_no", "SERIAL NO"), ("reject_op", "REJECT OP"),
   ("reject_op_desc", "REJECT OP DESCRIPTION"), ("cause_op", "CAUSE OP"),
   ("cause_op_desc", "CAUSE OP DESCRIPTION"), ("rw_hour", "RW HOUR"),
   ("wc_code", "WORK CENTRE"), ("wc_name", "WORK CENTRE DESCRIPTION"),
   ("rw_date", "RW DATE"), ("iso_week", "FW WEEK"), ("engineer", "ENGINEER"),
   ("cause_wc", "CAUSE OP WORK CENTRE"), ("cause_machine", "CAUSE OP MACHINE"),
   ("cause_emp_no", "EMPLOYEE NO"), ("cause_emp_name", "EMPLOYEE NAME"),
]


def export_to_excel(result, path, serial_recs=None, serial_no=None):
   import openpyxl
   from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
   from openpyxl.utils import get_column_letter

   thin = Side(style="thin", color="9AA7B4")
   med = Side(style="medium", color="30414F")
   box = Border(left=thin, right=thin, top=thin, bottom=thin)
   wb = openpyxl.Workbook()
   hf = Font(bold=True, color="FFFFFF")
   hfill = PatternFill("solid", fgColor="12324F")

   def head(ws, row, n):
       for c in range(1, n + 1):
           cell = ws.cell(row=row, column=c)
           cell.font = hf
           cell.fill = hfill
           cell.border = box
           cell.alignment = Alignment(horizontal="center", vertical="center",
                                      wrap_text=True)

   def autosize(ws, limit=40):
       for col in range(1, ws.max_column + 1):
           w = 10
           for row in range(1, min(ws.max_row, 400) + 1):
               v = ws.cell(row=row, column=col).value
               if v is not None:
                   w = max(w, min(len(str(v)) + 2, limit))
           ws.column_dimensions[get_column_letter(col)].width = w

   f = result["filters"]
   ws = wb.active
   ws.title = "SUMMARY"
   ws["A1"] = "REWORK ANALYSIS REPORT"
   ws["A1"].font = Font(bold=True, size=14)
   info = [("Engineer", f.get("engineer") or ALL),
           ("Part group", f.get("part_group") or ALL),
           ("Part number", f.get("part_no") or ALL),
           ("Time frame", f.get("time_label", "")),
           ("Reject operations", ", ".join(f.get("reject_ops") or []) or ALL),
           ("Cause operations", ", ".join(f.get("cause_ops") or []) or ALL),
           ("PART COUNT", result["part_count"]),
           ("TOTAL RW HOUR", round(result["rw_hour"], 3)),
           ("FTY", ("%.1f%%" % result["fty"]) if result.get("fty") is not None else "-"),
           ("Created", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))]
   r = 3
   for k, v in info:
       ws.cell(row=r, column=1, value=k).font = Font(bold=True)
       ws.cell(row=r, column=2, value=v)
       r += 1
   r += 1
   ws.cell(row=r, column=1, value="PER COMP").font = Font(bold=True, size=12)
   r += 1
   for i, h in enumerate(["COMP", "PART COUNT", "RW HOUR"], start=1):
       ws.cell(row=r, column=i, value=h)
   head(ws, r, 3)
   r += 1
   for k in sorted(result["by_comp"], key=lambda x: -result["by_comp"][x]["hour"]):
       g = result["by_comp"][k]
       ws.cell(row=r, column=1, value=k).border = box
       ws.cell(row=r, column=2, value=len(g["units"])).border = box
       ws.cell(row=r, column=3, value=round(g["hour"], 3)).border = box
       r += 1
   autosize(ws)

   ws2 = wb.create_sheet("PER RAW DATA")
   for i, (_, h) in enumerate(RAW_EXPORT_COLS, start=1):
       ws2.cell(row=1, column=i, value=h)
   head(ws2, 1, len(RAW_EXPORT_COLS))
   rr = 2
   ncol = len(RAW_EXPORT_COLS)
   for _, recs in result["groups"]:
       start = rr
       for rec in recs:
           for ci, (k, _) in enumerate(RAW_EXPORT_COLS, start=1):
               c = ws2.cell(row=rr, column=ci, value=rec.get(k))
               c.border = box
           rr += 1
       for ci in range(1, ncol + 1):
           top = ws2.cell(row=start, column=ci)
           bot = ws2.cell(row=rr - 1, column=ci)
           top.border = Border(left=med if ci == 1 else thin,
                               right=med if ci == ncol else thin, top=med, bottom=thin)
           bot.border = Border(left=med if ci == 1 else thin,
                               right=med if ci == ncol else thin, top=thin, bottom=med)
       if len(recs) > 1:
           for mc in (1, 2):        # PART NO and SERIAL NO
               ws2.merge_cells(start_row=start, start_column=mc,
                               end_row=rr - 1, end_column=mc)
               ws2.cell(row=start, column=mc).alignment = Alignment(
                   horizontal="center", vertical="center")
   ws2.freeze_panes = "A2"
   autosize(ws2)

   ws3 = wb.create_sheet("CROSS CHART")
   ws3.cell(row=1, column=1, value="REJECT OP x CAUSE OP").font = Font(bold=True, size=12)
   hr = 3
   ws3.cell(row=hr, column=1, value="REJECT OP DESCRIPTION")
   ws3.cell(row=hr, column=2, value="REJECT OP")
   ws3.cell(row=hr, column=3, value="TYPE")
   for ci, (code, desc) in enumerate(result["cause_keys"], start=4):
       ws3.cell(row=hr - 1, column=ci, value=desc).font = Font(bold=True, size=8)
       ws3.cell(row=hr, column=ci, value=code)
   last = 4 + len(result["cause_keys"])
   ws3.cell(row=hr, column=last, value="SUM")
   head(ws3, hr, last)
   rr = hr + 1
   for rk in result["reject_keys"]:
       start = rr
       for measure in ("units", "hour"):
           fill = PatternFill("solid",
                              fgColor="FBE7C2" if measure == "units" else "F7D2C8")
           ws3.cell(row=rr, column=1, value=rk[1]).border = box
           ws3.cell(row=rr, column=2, value=rk[0]).border = box
           c = ws3.cell(row=rr, column=3,
                        value="PART COUNT" if measure == "units" else "RW HOUR")
           c.fill = fill
           c.border = box
           c.font = Font(bold=True, size=8)
           total = 0
           for ci, ck in enumerate(result["cause_keys"], start=4):
               cell = result["matrix"].get((rk, ck))
               val = 0 if not cell else (len(cell["units"]) if measure == "units"
                                         else cell["hour"])
               total += val
               cc = ws3.cell(row=rr, column=ci,
                             value=round(val, 3) if measure == "hour" else val)
               cc.border = box
               if val:
                   cc.fill = fill
           sc = ws3.cell(row=rr, column=last,
                         value=round(total, 3) if measure == "hour" else total)
           sc.font = Font(bold=True)
           sc.border = box
           rr += 1
       for ci in range(1, last + 1):
           for rw_ in (start, rr - 1):
               cur = ws3.cell(row=rw_, column=ci)
               cur.border = Border(left=med if ci == 1 else thin,
                                   right=med if ci == last else thin,
                                   top=med if rw_ == start else thin,
                                   bottom=med if rw_ == rr - 1 else thin)
       ws3.merge_cells(start_row=start, start_column=1, end_row=rr - 1, end_column=1)
       ws3.merge_cells(start_row=start, start_column=2, end_row=rr - 1, end_column=2)
       ws3.cell(row=start, column=1).alignment = Alignment(vertical="center",
                                                           wrap_text=True)
       ws3.cell(row=start, column=2).alignment = Alignment(horizontal="center",
                                                           vertical="center")
   autosize(ws3, limit=20)

   if serial_recs:
       ws4 = wb.create_sheet("PER SERIAL")
       ws4.cell(row=1, column=1,
                value="SERIAL: %s" % (serial_no or "")).font = Font(bold=True, size=12)
       for i, (_, h) in enumerate(RAW_EXPORT_COLS, start=1):
           ws4.cell(row=3, column=i, value=h)
       head(ws4, 3, len(RAW_EXPORT_COLS))
       for ri, rec in enumerate(serial_recs, start=4):
           for ci, (k, _) in enumerate(RAW_EXPORT_COLS, start=1):
               ws4.cell(row=ri, column=ci, value=rec.get(k)).border = box
       autosize(ws4)

   wb.save(path)
   return path


def _xl_open():
   import openpyxl
   from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
   thin = Side(style="thin", color="9AA7B4")
   box = Border(left=thin, right=thin, top=thin, bottom=thin)
   wb = openpyxl.Workbook()
   styles = {"hf": Font(bold=True, color="FFFFFF"),
             "hfill": PatternFill("solid", fgColor="12324F"),
             "box": box, "Font": Font, "PatternFill": PatternFill,
             "Alignment": Alignment}
   return wb, styles


def _xl_head(ws, row, n, st):
   for c in range(1, n + 1):
       cell = ws.cell(row=row, column=c)
       cell.font = st["hf"]
       cell.fill = st["hfill"]
       cell.border = st["box"]
       cell.alignment = st["Alignment"](horizontal="center", vertical="center",
                                        wrap_text=True)


def _xl_autosize(ws, limit=40):
   from openpyxl.utils import get_column_letter
   for col in range(1, ws.max_column + 1):
       w = 10
       for row in range(1, min(ws.max_row, 400) + 1):
           v = ws.cell(row=row, column=col).value
           if v is not None:
               w = max(w, min(len(str(v)) + 2, limit))
       ws.column_dimensions[get_column_letter(col)].width = w


def ops_materials_for(db, part_key, min_len=6):
   """
   The material strings in the production-step table that belong to one
   traveler part.  SAP and the traveler do not always spell a part number
   identically, so the same prefix rule the rest of the app uses applies
   here too.
   """
   if not part_key:
       return []
   g = db.merge_group(part_key)
   if g is not None:
       out = []
       for m in g["members"]:
           out += [x for x in ops_materials_for(db, m, min_len) if x not in out]
       return out
   out = []
   for material in db.ops_materials():
       mk = norm_text(material)
       if not mk:
           continue
       if (mk == part_key
               or (len(mk) >= min_len and part_key.startswith(mk))
               or (len(part_key) >= min_len and mk.startswith(part_key))):
           out.append(material)
   return out


def bottleneck_matrix(db, part_key, d1, d2, max_days=120, serial_keys=None,
                      exclude_clean=False, dedupe_ops=False,
                      exclude_serial_keys=None):
   """
   How many parts sat at each traveler operation on each day.

   One row per traveler operation, one column per calendar day of the
   selected range, and in every cell the number of DIFFERENT serial
   numbers that were booked at that operation on that day.

   Rework entries are deliberately left out: the count comes from the
   production-step table (`ops`, the rows whose rework labour time is
   zero), which is the record of parts actually moving through the shop.
   Counting rework bookings as well would show the same part twice on the
   day it was reworked and inflate exactly the operation being examined.

   Returns a dict with route, days, counts {(op_key, day): n}, per-row and
   per-column totals, or None when the part has no traveler route.
   """
   route = wf_filter_duplicate_ops(
       wf_filter_clean(db.wf_traveler_for(part_key), exclude_clean), dedupe_ops)
   if not route:
       return None
   materials = ops_materials_for(db, part_key)
   if not materials:
       return {"route": route, "days": [], "counts": {}, "row_total": {},
               "row_distinct": {}, "col_total": {}, "materials": [],
               "truncated": 0}

   ph = ",".join("?" * len(materials))
   clauses = ["material IN (%s)" % ph, "op_date IS NOT NULL", "op_date <> ''"]
   params = list(materials)
   if serial_keys:
       clauses.append("serial_key IN (%s)" % ",".join("?" * len(serial_keys)))
       params.extend(sorted(serial_keys))
   if exclude_serial_keys:
       clauses.append("serial_key NOT IN (%s)" % ",".join("?" * len(exclude_serial_keys)))
       params.extend(sorted(exclude_serial_keys))
   if d1:
       clauses.append("op_date >= ?")
       params.append(d1.isoformat())
   if d2:
       clauses.append("op_date <= ?")
       params.append(d2.isoformat())
   where = " AND ".join(clauses)

   if not d1 or not d2:
       row = db.conn.execute(
           "SELECT MIN(op_date) a, MAX(op_date) b FROM ops WHERE %s" % where,
           params).fetchone()
       d1 = d1 or (parse_date(row["a"]) if row and row["a"] else None)
       d2 = d2 or (parse_date(row["b"]) if row and row["b"] else None)
   if not d1 or not d2 or d2 < d1:
       return {"route": route, "days": [], "counts": {}, "row_total": {},
               "row_distinct": {}, "col_total": {}, "materials": materials,
               "truncated": 0}

   days = []
   day = d1
   while day <= d2:
       days.append(day)
       day += datetime.timedelta(days=1)
   truncated = 0
   if len(days) > max_days:
       # A year-wide range would draw hundreds of columns nobody can read;
       # the most recent stretch is what a bottleneck view is about.
       truncated = len(days) - max_days
       days = days[-max_days:]
       d1 = days[0]
       clauses.append("op_date >= ?")
       params.append(d1.isoformat())
       where = " AND ".join(clauses)

   # Counted per STEP: an operation written 280/1, 280/3, 280/4 is three
   # rows on this page and a booking belongs to the one it names.
   counts = {}
   for r in db.conn.execute(
           "SELECT op_key, COALESCE(seq,'') AS seq, op_date, "
           "COUNT(DISTINCT serial_key) n FROM ops "
           "WHERE %s AND op_key <> '' GROUP BY op_key, seq, op_date" % where,
           params):
       d = parse_date(r["op_date"])
       if d is not None:
           counts[(step_key(r["op_key"], r["seq"]), d)] = r["n"]

   row_distinct = {}
   for r in db.conn.execute(
           "SELECT op_key, COALESCE(seq,'') AS seq, "
           "COUNT(DISTINCT serial_key) n FROM ops "
           "WHERE %s AND op_key <> '' GROUP BY op_key, seq" % where, params):
       row_distinct[step_key(r["op_key"], r["seq"])] = r["n"]

   row_total, col_total = {}, {}
   for (ok, d), n in counts.items():
       row_total[ok] = row_total.get(ok, 0) + n
       col_total[d] = col_total.get(d, 0) + n

   return {"route": route, "days": days, "counts": counts,
           "row_total": row_total, "row_distinct": row_distinct,
           "col_total": col_total, "materials": materials,
           "truncated": truncated}


def bottleneck_flow(db, part_key, d1, d2, serial_keys=None,
                    exclude_clean=False, dedupe_ops=False,
                    exclude_serial_keys=None, count_without_x=True,
                    with_serials=False):
   """
   Day by day, per step: how many serials FINISHED it that day, and how
   many were WAITING at it as of that day.

   FINISHED ON A DAY - the shop's own rule and Excel formula (v62):

       a serial finished a step on the day of the X that closed it - the
       latest production booking against the step that carried the X and
       was not a setup.  One serial counts once per step, on that day.

   A step the serial moved PAST without its X being booked is finished
   too (the part cannot be further along otherwise), on the day of its
   last production entry.  `count_without_x` decides whether those are
   counted; the Excel formula counts only the X ones, so turning it off
   gives exactly the formula's numbers.

   WAITING at a step on a day is where the part stood at the end of that
   day, by the same rule as the waterfall (op_progress_for_route):

       the furthest step it had a production entry on by then; if that
       step was closed by an X by then, it is waiting at the NEXT step,
       otherwise it is still at that one (being worked, or queued at it).

   Returns route, days, and {(position, day): n} series:
       completed         finished that day (X, plus X-less if counted)
   and per position: passed (distinct serials finished inside the range)
   and waiting_end (distinct serials at the step at the end of the range).
       completed_x       finished that day by an X
       completed_no_x    finished that day without an X
       queue             waiting / being worked there at the end of the day
       arrived / left    joined / left that step's queue that day
       skipped           left it without a production entry on it

       queue(D) = queue(D-1) + arrived(D) - left(D)

   None when the part has no traveler route.
   """
   route = wf_filter_duplicate_ops(
       wf_filter_clean(db.wf_traveler_for(part_key), exclude_clean), dedupe_ops)
   if not route:
       return None
   pos_of = {route_step(c): c["position"] for c in route}
   n_pos = len(route)

   # Per serial, per step: when it was first reached, when it was closed
   # by an X, and its last production entry.
   by_serial = {}
   # A production booking against a step the traveler does not list still
   # proves the part got as far as the last route step before it - the
   # same evidence the waterfall takes (op_progress_for_route).  Kept as
   # (first day, position) arrivals: it moves the part, it closes nothing.
   rota_disi = {}
   sira_keys, sira_best = _rota_bilgisi(route)[1]
   for r in db.wf_part_data_for(part_key):
       pos = pos_of.get(r.get("step") or step_key(r["opac"], r.get("seq")))
       if serial_keys and r["serial_key"] not in serial_keys:
           continue
       if exclude_serial_keys and r["serial_key"] in exclude_serial_keys:
           continue
       if not r.get("dt_ops"):
           continue              # rework only: it neither moves nor closes
       if pos is None:
           hedef = _adim_sirasi(r["opac"], r.get("seq"))
           # 2.1.1: the furthest route step at or before it, by bisect
           j = bisect.bisect_right(sira_keys, hedef)
           if j:
               ilk = (r.get("dt_ops_first") or r["dt_ops"]).date()
               rota_disi.setdefault(r["serial_key"], []).append(
                   (ilk, sira_best[j - 1]))
           continue
       ilk = r.get("dt_ops_first") or r["dt_ops"]
       x_gun = r["x_dt"].date() if (r.get("done_x") and r.get("x_dt")) \
           else None
       onceki = by_serial.setdefault(r["serial_key"], {}).get(pos)
       kayit = (ilk.date(), x_gun, r["dt_ops"].date())
       if onceki:                # the same step under two material codes
           kayit = (min(onceki[0], kayit[0]),
                    max([g for g in (onceki[1], kayit[1]) if g] or [None]),
                    max(onceki[2], kayit[2]))
       by_serial[r["serial_key"]][pos] = kayit

   all_days = set()
   for adimlar in by_serial.values():
       for ilk, x_gun, son_g in adimlar.values():
           all_days.update(g for g in (ilk, x_gun, son_g) if g)
   for olaylar in rota_disi.values():
       all_days.update(g for g, _p in olaylar)
   bos = {"route": route, "days": [], "completed": {}, "completed_x": {},
          "completed_no_x": {}, "queue": {}, "arrived": {}, "left": {},
          "skipped": {}, "passed": {}, "waiting_end": {}}
   if not all_days:
       return bos
   d1 = d1 or min(all_days)
   d2 = d2 or max(all_days)
   days = []
   d = d1
   while d <= d2:
       days.append(d)
       d += datetime.timedelta(days=1)
   gun_kumesi = set(days)

   queue, arrived, left, skipped = {}, {}, {}, {}
   completed, completed_x, completed_no_x = {}, {}, {}
   # v70 with_serials: WHICH serials make each WAITING / FINISHED number
   # (the screen lists them on hover).  Collected at the very same place
   # the numbers are counted, so the two can never disagree.
   queue_who, completed_who = {}, {}

   def say(hedef, pos, gun, n=1):
       hedef[(pos, gun)] = hedef.get((pos, gun), 0) + n

   def kim(hedef, pos, gun, sk):
       if with_serials:
           hedef.setdefault((pos, gun), []).append(sk)

   for sk in set(by_serial) | set(rota_disi):
       adimlar = by_serial.get(sk, {})
       disi = rota_disi.get(sk, [])
       # FINISHED: every step behind the furthest one reached, and that one
       # too when its X is in.
       en_ileri = max(list(adimlar) + [p for _g, p in disi])
       for pos, (ilk, x_gun, son_g) in adimlar.items():
           if x_gun is not None:
               if x_gun in gun_kumesi:
                   say(completed_x, pos, x_gun)
                   say(completed, pos, x_gun)
                   kim(completed_who, pos, x_gun, sk)
           elif pos < en_ileri:
               if son_g in gun_kumesi:
                   say(completed_no_x, pos, son_g)
                   if count_without_x:
                       say(completed, pos, son_g)
                       kim(completed_who, pos, son_g, sk)

       # WAITING: walk the calendar.  Steps in the order they were first
       # reached, so each day only has to move an index forward.
       ulasma = sorted([(ilk, pos) for pos, (ilk, _x, _s) in adimlar.items()]
                       + disi)
       idx, reached = 0, -1
       onceki = None
       for day in days:
           while idx < len(ulasma) and ulasma[idx][0] <= day:
               if ulasma[idx][1] > reached:
                   reached = ulasma[idx][1]
               idx += 1
           simdi = None
           if reached >= 0:
               x_gun = (adimlar.get(reached) or (None, None, None))[1]
               if x_gun is not None and x_gun <= day:
                   if reached + 1 < n_pos:
                       simdi = reached + 1
               else:
                   simdi = reached
           if simdi is not None:
               say(queue, simdi, day)
               kim(queue_who, simdi, day, sk)
           if simdi != onceki:
               if simdi is not None:
                   say(arrived, simdi, day)
               if onceki is not None:
                   say(left, onceki, day)
                   if onceki not in adimlar:
                       say(skipped, onceki, day)
           onceki = simdi

   # THE TWO SUMMARY COLUMNS (v62), per route position:
   #   passed        distinct serials that FINISHED the step inside the
   #                 range - a serial finishes a step once, so this is the
   #                 FINISHED row added up (and follows count_without_x)
   #   waiting_end   distinct serials standing at the step at the end of
   #                 the range's last day - the WAITING column of that day
   passed, waiting_end = {}, {}
   for (pos, _g), n in completed.items():
       passed[pos] = passed.get(pos, 0) + n
   son_gun = days[-1]
   for (pos, g), n in queue.items():
       if g == son_gun:
           waiting_end[pos] = n
   out = {"route": route, "days": days, "completed": completed,
          "completed_x": completed_x, "completed_no_x": completed_no_x,
          "queue": queue, "arrived": arrived, "left": left,
          "skipped": skipped, "passed": passed, "waiting_end": waiting_end}
   if with_serials:
       out["queue_who"] = queue_who
       out["completed_who"] = completed_who
   return out


def sold_count_by_date_for_part(db, part_key, d1, d2):
   """Distinct serials of ONE part sold per day - the Bottleneck day-by-day
   table's own summary row, so "how many left today" sits right next to
   "how many are still waiting" instead of only being visible elsewhere."""
   mat_keys = db.wf_resolve_material_keys(part_key)
   if not mat_keys:
       return {}
   ph = ",".join("?" * len(mat_keys))
   clauses = ["serial_key<>''", "ac_gi_date IS NOT NULL", "ac_gi_date<>''",
             "material_key IN (%s)" % ph]
   params = list(mat_keys)
   if d1:
       clauses.append("ac_gi_date>=?")
       params.append(d1.isoformat())
   if d2:
       clauses.append("ac_gi_date<=?")
       params.append(d2.isoformat())
   out = {}
   for r in db.conn.execute(
           "SELECT ac_gi_date d, COUNT(DISTINCT serial_key) n FROM shipment "
           "WHERE %s GROUP BY ac_gi_date" % " AND ".join(clauses), params):
       d = parse_date(r["d"])
       if d:
           out[d] = r["n"]
   return out


def bottleneck_wait_ranking(db, part_key, d1, d2, serial_keys=None,
                            exclude_clean=False, dedupe_ops=False,
                            exclude_serial_keys=None):
   """
   The day-by-day load only shows how many parts sat at an operation, not
   where the shop is actually losing the most time.  Here, for every
   serial, the single longest gap between two consecutive route steps is
   found (using the waterfall's per-operation timestamps), and each
   operation is scored by how many serials had their worst wait land on
   it - the operation a serial's own journey compares itself against
   changes over the month, so this only means anything per serial, then
   tallied across serials.  Returns a list sorted by that count, or None
   when the part has no traveler route.
   """
   route = wf_filter_duplicate_ops(
       wf_filter_clean(db.wf_traveler_for(part_key), exclude_clean), dedupe_ops)
   if not route:
       return None
   pos_of = {route_step(c): c["position"] for c in route}

   by_serial = {}
   for r in db.wf_part_data_for(part_key):
       if serial_keys and r["serial_key"] not in serial_keys:
           continue
       if exclude_serial_keys and r["serial_key"] in exclude_serial_keys:
           continue
       if d1 and r["dt"].date() < d1:
           continue
       if d2 and r["dt"].date() > d2:
           continue
       if (r.get("step") or step_key(r["opac"], r.get("seq"))) not in pos_of:
           continue
       by_serial.setdefault(r["serial_key"], []).append(r)

   tally = {}
   for recs in by_serial.values():
       recs.sort(key=lambda r: pos_of[r.get("step")
                                      or step_key(r["opac"], r.get("seq"))])
       best_op, best_hours = None, -1.0
       for i in range(1, len(recs)):
           prev, cur = recs[i - 1], recs[i]
           if (pos_of[cur.get("step") or step_key(cur["opac"], cur.get("seq"))]
                   != pos_of[prev.get("step")
                             or step_key(prev["opac"], prev.get("seq"))] + 1):
               continue    # only a clean, consecutive step is a real gap
           hrs = (cur["dt"] - prev["dt"]).total_seconds() / 3600.0
           if hrs > best_hours:
               best_hours, best_op = hrs, (cur.get("step")
                                           or step_key(cur["opac"],
                                                       cur.get("seq")))
       if best_op is not None:
           t = tally.setdefault(best_op, {"n": 0, "total_hours": 0.0})
           t["n"] += 1
           t["total_hours"] += best_hours

   out = []
   for c in route:
       t = tally.get(route_step(c))
       if t:
           out.append({"op": c.get("label") or c["op"], "desc": c["desc"],
                       "n_serials": t["n"],
                       "avg_hours": t["total_hours"] / t["n"]})
   out.sort(key=lambda x: -x["n_serials"])
   return out


LT_DATE_MODES = ("ops", "serial", "sold")
_LT_CACHE = {}


def lead_time_analysis(db, part_key, d1, d2, serial_keys=None,
                       exclude_clean=True, dedupe_ops=True,
                       exclude_serial_keys=None, date_mode="ops"):
   """Memoised _lead_time_analysis (2.1.3): the waterfall asks for the same
   figures on every refresh while the module or Predicted pos. is on.
   Keyed by the data version and invalidate_caches(), so new data or a
   rule change is never answered from memory.  Callers only read it."""
   try:
      ver = db.data_version()
   except Exception:
      ver = None
   key = (db.__dict__.get("cache_id", id(db)), ver, _CACHE_GEN[0], part_key,
          d1, d2, frozenset(serial_keys) if serial_keys else None,
          bool(exclude_clean), bool(dedupe_ops),
          frozenset(exclude_serial_keys) if exclude_serial_keys else None,
          date_mode)
   hit = _LT_CACHE.get(key)
   if hit is not None:
      return hit
   out = _lead_time_analysis(db, part_key, d1, d2, serial_keys, exclude_clean,
                             dedupe_ops, exclude_serial_keys, date_mode)
   if len(_LT_CACHE) > 40:
      _LT_CACHE.clear()
   _LT_CACHE[key] = out
   return out


def lead_entries(db, part_key, d1, d2, serial_keys=None,
                 exclude_serial_keys=None, date_mode="ops"):
   """{serial key: {step key: record}} - the records a lead time figure is
   worked out from, picked by the date range one of three ways:

       "ops"     only the operation records inside the range
       "serial"  the serials with any record inside the range, with ALL
                 their records
       "sold"    (2.2.0) the serials SOLD inside the range (shipment
                 report), with ALL their records
   """
   sold = db.sold_keys_between(d1, d2) if date_mode == "sold" else None
   by_serial = {}
   in_window = set()
   for r in db.wf_part_data_for(part_key):
       sk = r["serial_key"]
       if serial_keys and sk not in serial_keys:
           continue
       if exclude_serial_keys and sk in exclude_serial_keys:
           continue
       if sold is not None:
           if sk not in sold:
               continue
       elif not ((d1 and r["dt"].date() < d1) or (d2 and r["dt"].date() > d2)):
           in_window.add(sk)
       elif date_mode != "serial":
           continue
       by_serial.setdefault(sk, {})[
           r.get("step") or step_key(r["opac"], r.get("seq"))] = r
   if date_mode == "serial":
       # BY SERIAL: the serials that passed through the range, each with
       # every record it has.
       by_serial = {k: v for k, v in by_serial.items() if k in in_window}
   return by_serial


# ---------------------------------------------------------------------------
#  LEAD TIME ANALYSIS - NAMED OPERATION GROUPS  (2.2.0)
# ---------------------------------------------------------------------------
# On the LEAD TIME ANALYSIS page the user puts a part's operations into
# groups of their own naming - TURN = 130, 140, 150 - and may give several
# parts operations under the same name.
#
# THE CHAIN IS NEVER BROKEN.  A group is measured like the steps it is
# made of, each from the operation before it:
#
#     route 1 2 3 4 5 6 7,   groups {1,2,3} {4,5} {6,7}
#     {1,2,3}  completion of 1  ->  X of 3     (nothing comes before 1)
#     {4,5}    completion of 3  ->  X of 5
#     {6,7}    completion of 5  ->  X of 7
#
# so the groups of a route add up to the route, with no gap and nothing
# counted twice.  Operations picked with a hole between them (1,2 and 6)
# are measured run by run and added up; a serial needs every run.
#
# A SEQUENCED OPERATION IS ONE OPERATION HERE.  280/0, 280/2, 280/7 are
# picked as 280; its completion is that of the sequence with the LAST
# time entry (its X; passed without one - as the start of a group only -
# its last entry).  Its operation time is all its sequences added up.
#
# OPERATION TIME is the work content of the group's own operations
# (lead_cut_hours: machine, else labour - never a rework's hours).
# Negative and far-off figures are dropped per part and group
# (lead_keep_mask).  A group's figure is the average over EVERY kept
# serial of every part in it (pooled); a SUB GROUP is the same over the
# parts that carry its name; each part's own average is the detail.
#
# Stored in meta (shared) under LT_GROUPS_KEY as {set name: definition}:
#     {"parts":  [{"part_key", "sub"}],                (order, sub group)
#      "groups": [{"name", "color", "ops": {part_key: [op, ...]}}]}
# The first 2.2.0 build stored {"ranges": [{"part_key","from","to"}]} per
# group; lt_groups_normalize reads those too.
LT_GROUPS_KEY = "lt_op_groups"
LT_GROUP_COLORS = ["#4F8EF7", "#2FBF71", "#F2A93B", "#E5567A", "#9B6CF2",
                   "#26B8C7", "#F27A3D", "#7FB341", "#D05FC6", "#8A97A8"]


def lt_groups_load(db):
   v = db.get_meta(LT_GROUPS_KEY, None)
   return dict(v) if isinstance(v, dict) else {}


def lt_groups_save(db, name, definition):
   allv = lt_groups_load(db)
   allv[name] = definition
   db.set_meta(LT_GROUPS_KEY, allv)


def lt_groups_delete(db, name):
   allv = lt_groups_load(db)
   if allv.pop(name, None) is not None:
      db.set_meta(LT_GROUPS_KEY, allv)


def lt_part_ops(db, part_key):
   """A part's route for grouping: one entry per OPERATION NUMBER, in
   route order - [{"op", "desc", "steps": [step keys]}].  The sequences of
   one operation are its `steps`."""
   out, by = [], {}
   for c in db.wf_traveler_for(part_key):
      k = op_key(c["op"])
      o = by.get(k)
      if o is None:
         o = by[k] = {"op": k, "desc": c.get("desc") or "", "steps": []}
         out.append(o)
      o["steps"].append(route_step(c))
   return out


def lt_groups_normalize(db, definition):
   """A definition in today's shape, whatever build wrote it.  Operation
   numbers come back as text keys, every part of every group is in
   `parts`, every group has a colour."""
   d = definition if isinstance(definition, dict) else {}
   parts, seen = [], set()
   for p in d.get("parts") or []:
      pk = p.get("part_key")
      if pk and pk not in seen:
         seen.add(pk)
         parts.append({"part_key": pk, "sub": (p.get("sub") or "").strip()})
   groups = []
   for gi, g in enumerate(d.get("groups") or []):
      ops = {}
      for pk, lst in (g.get("ops") or {}).items():
         keys = []
         for o in lst or []:
            k = op_key(o)
            if k and k not in keys:
               keys.append(k)
         if keys:
            ops[pk] = keys
      for rg in g.get("ranges") or []:           # the first 2.2.0 shape
         pk = rg.get("part_key")
         route = lt_part_ops(db, pk) if pk else []
         where = {}
         for i, o in enumerate(route):
            for st in o["steps"]:
               where[st] = i
         a, b = where.get(rg.get("from")), where.get(rg.get("to"))
         if a is None or b is None or b <= a:
            continue
         cur = ops.setdefault(pk, [])
         for o in route[a + 1:b + 1]:
            if o["op"] not in cur:
               cur.append(o["op"])
      for pk in ops:
         if pk not in seen:
            seen.add(pk)
            parts.append({"part_key": pk, "sub": ""})
      groups.append({"name": (g.get("name") or "").strip(),
                     "color": g.get("color") or LT_GROUP_COLORS[gi % len(LT_GROUP_COLORS)],
                     "ops": ops})
   return {"parts": parts, "groups": groups}


def lt_runs(route_ops, chosen):
   """The chosen operations as runs of neighbours on the route:
   ([(first position, last position)], [operations not on the route])."""
   pos = {o["op"]: i for i, o in enumerate(route_ops)}
   idx = sorted({pos[k] for k in chosen if k in pos})
   missing = [k for k in chosen if k not in pos]
   runs = []
   for i in idx:
      if runs and i == runs[-1][1] + 1:
         runs[-1][1] = i
      else:
         runs.append([i, i])
   return [tuple(r) for r in runs], missing


def lt_run_bounds(a, b):
   """(position the run is measured FROM, first position whose work
   counts) - the operation before the run; the route's first operation
   has nothing before it, so a run starting there is measured from its
   own completion and its work is not in it."""
   return (a - 1, a) if a > 0 else (a, a + 1)


def lead_op_record(entries, steps):
   """(record, operation time) of one operation for one serial: the
   record of the sequence with the LAST time entry, and the operation
   time of all its sequences.  (None, 0.0) when nothing was booked."""
   best, cut = None, 0.0
   for st in steps:
      r = entries.get(st)
      if r is None:
         continue
      cut += lead_cut_hours(r)
      if best is None or r["dt"] > best["dt"]:
         best = r
   return best, cut


def lead_group_values(route_ops, entries_by_serial, chosen):
   """([(serial key, lead hours, operation hours)], problem text) for one
   part's share of a group - every serial that has all of it."""
   runs, missing = lt_runs(route_ops, chosen)
   runs = [(a, b) for a, b in runs if not (a == 0 and b == 0)]
   if not runs:
      if missing:
         return [], "operation %s not on the route" % ", ".join(missing)
      return [], ("only the first operation - nothing comes before it"
                  if chosen else "no operations picked")
   need = set()
   for a, b in runs:
      ref, first = lt_run_bounds(a, b)
      need.add(ref)
      need.update(range(first, b + 1))
   need = sorted(need)
   out = []
   for sk, entries in entries_by_serial.items():
      recs = {i: lead_op_record(entries, route_ops[i]["steps"]) for i in need}
      total = cut = 0.0
      for a, b in runs:
         ref, first = lt_run_bounds(a, b)
         hrs = lead_gap_hours(recs[ref][0], recs[b][0])
         if hrs is None:
            total = None
            break
         total += hrs
         cut += sum(recs[i][1] for i in range(first, b + 1))
      if total is not None:
         out.append((sk, total, cut))
   return out, ("operation %s not on the route" % ", ".join(missing)) if missing else ""


def _lead_split(vals):
   """(lead, operation time, queue, efficiency, n, overlap) of kept
   (lead, operation time) pairs."""
   n = len(vals)
   if not n:
      return None, None, None, None, 0, False
   lead = sum(v[0] for v in vals) / n
   booked = sum(v[1] for v in vals) / n
   overlap = booked > lead
   cut = min(booked, lead)
   return (lead, cut, max(0.0, lead - cut), (cut / lead) if lead else None,
           n, overlap)


_LG_KEYS = ("avg_hours", "cut_hours", "queue_hours", "efficiency", "n", "overlap")
_LG_CACHE = {}


def lead_group_analysis(db, definition, d1, d2, serial_keys=None,
                        exclude_serial_keys=None, date_mode="ops"):
   """Memoised _lead_group_analysis - the page asks again on every unit
   or theme change.  Keyed by the data version, like lead_time_analysis."""
   try:
      ver = db.data_version()
   except Exception:
      ver = None
   key = (db.__dict__.get("cache_id", id(db)), ver, _CACHE_GEN[0],
          json.dumps(definition, sort_keys=True, default=str), d1, d2,
          frozenset(serial_keys) if serial_keys else None,
          frozenset(exclude_serial_keys) if exclude_serial_keys else None,
          date_mode)
   hit = _LG_CACHE.get(key)
   if hit is None:
      if len(_LG_CACHE) > 20:
         _LG_CACHE.clear()
      hit = _LG_CACHE[key] = _lead_group_analysis(
          db, definition, d1, d2, serial_keys, exclude_serial_keys, date_mode)
   return hit


def _lead_group_analysis(db, definition, d1, d2, serial_keys=None,
                         exclude_serial_keys=None, date_mode="ops"):
   """The named operation groups of one saved set, in its own order:

   [{"name", "color", figures, "serials": [(serial key, lead, op time,
                                            part key)],
     "subs":  [{"name", figures, "parts": [part rows]}],   (named sub groups)
     "parts": [{"part_key", "sub", "first", "last", "ref", "n_ops",
                "runs", figures, "problem", "serials": [(key, lead, op)]}],
     "names": {serial key: serial as written}}]

   figures = avg_hours, cut_hours (operation time), queue_hours,
   efficiency, n, overlap.  `ref` is the operation the part's stretch is
   measured from; `problem` says why a part gave nothing."""
   d = lt_groups_normalize(db, definition)
   sub_of = {p["part_key"]: p["sub"] for p in d["parts"]}
   order = {p["part_key"]: i for i, p in enumerate(d["parts"])}
   route_cache, entry_cache = {}, {}
   out = []
   for g in d["groups"]:
      pooled, parts, names = [], [], {}
      for pk in sorted(g["ops"], key=lambda k: order.get(k, 1 << 30)):
         chosen = g["ops"][pk]
         if pk not in route_cache:
            route_cache[pk] = lt_part_ops(db, pk)
            entry_cache[pk] = lead_entries(db, pk, d1, d2, serial_keys,
                                           exclude_serial_keys, date_mode)
         route = route_cache[pk]
         runs, _missing = lt_runs(route, chosen)
         row = {"part_key": pk, "sub": sub_of.get(pk, ""), "n_ops": len(chosen),
                "runs": len(runs), "first": "", "last": "", "ref": "", "problem": ""}
         if runs:
            row["first"] = route[runs[0][0]]["op"]
            row["last"] = route[runs[-1][1]]["op"]
            row["ref"] = route[lt_run_bounds(*runs[0])[0]]["op"]
         if not route:
            vals, row["problem"] = [], "no traveler route"
         else:
            vals, row["problem"] = lead_group_values(route, entry_cache[pk], chosen)
         kept = [v for v, keep in zip(vals, lead_keep_mask([v[1] for v in vals]))
                 if keep]
         for sk, _h, _c in kept:
            if sk not in names:
               for r in entry_cache[pk][sk].values():
                  names[sk] = r.get("serial") or sk
                  break
         row["serials"] = [(sk, h, min(c, h)) for sk, h, c in kept]
         row.update(zip(_LG_KEYS, _lead_split([(h, c) for _sk, h, c in kept])))
         pooled.extend((sk, h, c, pk) for sk, h, c in kept)
         parts.append(row)
      grp = {"name": g["name"], "color": g["color"], "parts": parts, "names": names,
             "serials": [(sk, h, min(c, h), pk) for sk, h, c, pk in pooled]}
      grp.update(zip(_LG_KEYS, _lead_split([(h, c) for _sk, h, c, _pk in pooled])))
      subs, by = [], {}
      for row in parts:
         if not row["sub"]:
            continue
         sb = by.get(row["sub"])
         if sb is None:
            sb = by[row["sub"]] = {"name": row["sub"], "parts": []}
            subs.append(sb)
         sb["parts"].append(row)
      for sb in subs:
         # the rows keep the capped figure; the split needs what was booked
         raw = {(sk, pk): c for sk, _h, c, pk in pooled}
         vals = [(h, raw.get((sk, row["part_key"]), c))
                 for row in sb["parts"] for sk, h, c in row["serials"]]
         sb.update(zip(_LG_KEYS, _lead_split(vals)))
      grp["subs"] = subs
      out.append(grp)
   return out


def _lead_time_analysis(db, part_key, d1, d2, serial_keys=None,
                        exclude_clean=True, dedupe_ops=True,
                        exclude_serial_keys=None, date_mode="ops"):
   """
   One row per operation-to-operation step in the traveler: this
   operation's own last entry minus the immediately preceding operation's
   last entry, averaged across every serial that has both.  Clean and
   repeated-op-number columns are removed from the route by default first
   (they carry no real time entry, or would only measure the same
   timestamp against itself), so what is left is the shop's real,
   station-by-station lead time - not the traveler's own predecessor-
   based groupings, which can span several stations under one figure.

   Each step is also split into the two things that make it up:

       cut_hours     machine time booked against the operation the part is
                     arriving at - the part of the step that was cutting
       queue_hours   what is left - the part that was waiting
       efficiency    cut / lead, the share of the step spent cutting

   The split is what makes the figure actionable.  A long step is not
   automatically a slow machine: it is usually a queue, and a queue is
   removable without buying anything.  Note that `cut_hours` is the whole
   of the arriving operation's machine time, so a step's queue is measured
   from the previous operation's last booking to this one's START - which
   is what somebody standing on the floor would call the wait.

   Where more machine time is booked than the step is long, the two
   operations' bookings overlap and the split cannot be resolved: the step
   is reported as all cutting, `overlap` is set on it, `cut_booked` keeps
   the raw figure, and the result's `n_overlap` counts how many steps that
   happened on.

   2.1.2: a step runs from the previous operation's COMPLETION (its X, or
   its last entry when it was passed without one) to this operation's X,
   in plain clock hours - see lead_gap_hours.  The cut part is the
   arriving operation's machine time, or its labour when it has no
   machine time.  Negative and far-off gaps are left out (lead_keep_mask).

   THE DATE RANGE, two ways (2.1.2, `date_mode`):
       "ops"     only the operation records inside the range are used - a
                 step counts when both of its operations' records fall in it
       "serial"  the serials with any record inside the range are picked,
                 and ALL their records are used, whatever their dates - no
                 step of theirs is skipped for being booked outside it

   Returns a dict with route and `steps` (one entry per transition, in
   route order), or None when the part has no traveler route.  The totals
   `total_lead` / `total_cut` / `total_queue` / `flow_efficiency` are the
   same split taken across the whole route.
   """
   route = wf_filter_duplicate_ops(
       wf_filter_clean(db.wf_traveler_for(part_key), exclude_clean), dedupe_ops)
   if not route:
       return None

   by_serial = lead_entries(db, part_key, d1, d2, serial_keys,
                            exclude_serial_keys, date_mode)

   # SEQUENCES ARE STEPS (v62).  280/0, 280/2, 280/7 are worked in traveler
   # order, each with its own standard time, so the move from one to the
   # next is a real step and is measured like any other.  They used to be
   # left out as "the same station in any order" - which also left this
   # list SHORTER than the route, so everything reading it by route
   # position (the lead-time prediction) was one step out after the first
   # sequenced operation.  A negative gap (bookings out of order) is still
   # dropped below.
   # 2.1.2: FROM COMPLETION TO COMPLETION, in clock hours.  The arriving
   # operation must be closed by its X; the one before it is completed by
   # its X or, without one, by its last entry.  A previous step nobody
   # booked hands over to the nearest earlier one that was.  Negative and
   # far-off gaps are dropped per step (lead_keep_mask).
   atla = set()
   step_sum, step_n, cut_sum = {}, {}, {}
   adaylar = {}                      # i -> [(hours, cut hours)]
   rsteps = [route_step(c) for c in route]      # 2.2.0: once, not per serial
   finished, names = set(), {}
   for sk, entries in by_serial.items():
       if lead_x_dt(entries.get(rsteps[-1])) is not None:
           finished.add(sk)
       for r in entries.values():
           names[sk] = r.get("serial") or sk
           break
       for i in range(len(route) - 1):
           r2 = entries.get(rsteps[i + 1])
           if r2 is None or lead_x_dt(r2) is None:
               continue
           r1 = None
           for j in range(i, -1, -1):
               r1 = entries.get(rsteps[j])
               if r1:
                   break
           hrs = lead_gap_hours(r1, r2)
           if hrs is None:
               continue
           # The work content of the operation the part is arriving at:
           # machine time, or labour where the operation has no machine.
           # Everything else in the step was queue.
           adaylar.setdefault(i, []).append((hrs, lead_cut_hours(r2), sk))
   # 2.2.0: the kept figures per serial too - the page charts them.
   # {step index: [(serial key, lead, cut)]}, {serial key: [lead, cut, steps]}
   step_serials, per_serial = {}, {}
   for i, vals in adaylar.items():
       kept = step_serials[i] = []
       for (hrs, cut, sk), keep in zip(vals, lead_keep_mask([v[0] for v in vals])):
           if keep:
               step_sum[i] = step_sum.get(i, 0.0) + hrs
               step_n[i] = step_n.get(i, 0) + 1
               cut_sum[i] = cut_sum.get(i, 0.0) + cut
               c = min(cut, hrs)
               kept.append((sk, hrs, c))
               t = per_serial.get(sk)
               if t is None:
                   per_serial[sk] = [hrs, c, 1]
               else:
                   t[0] += hrs
                   t[1] += c
                   t[2] += 1

   steps = []
   tot_lead = tot_cut = 0.0
   n_overlap = 0
   for i in range(len(route) - 1):
       if i in atla:
           continue
       n = step_n.get(i, 0)
       lead = (step_sum[i] / n) if n else None
       booked = (cut_sum[i] / n) if n else None
       # More machine time booked against the arriving operation than there
       # is step to put it in.  That is not a 300%-efficient step - it means
       # the two operations' bookings overlap, so the part was already being
       # worked on before the previous operation's last entry.  All that can
       # honestly be said is that the step was all cutting and no queue; the
       # raw figure stays on the step as `cut_booked`, and the count of such
       # steps is reported so the reading is not taken as cleaner than it is.
       overlap = bool(lead is not None and booked is not None and booked > lead)
       cut = min(booked, lead) if overlap else booked
       queue = max(0.0, lead - cut) if (lead is not None and cut is not None) else None
       if lead is not None:
           tot_lead += lead
           tot_cut += cut or 0.0
           n_overlap += overlap
       steps.append({
           "from_op": route[i].get("label") or route[i]["op"],
           "from_desc": route[i]["desc"],
           "to_op": route[i + 1].get("label") or route[i + 1]["op"],
           "to_desc": route[i + 1]["desc"],
           "avg_hours": lead, "n": n,
           "cut_hours": cut, "cut_booked": booked, "overlap": overlap,
           "queue_hours": queue,
           # What share of the step was cutting.  Low is not automatically
           # bad - an inspection step has little machine time by nature -
           # but a long step with almost no machine time in it is queue,
           # and queue is the thing that can be removed without buying
           # anything.
           "efficiency": (cut / lead) if (lead and cut is not None) else None,
           "serials": step_serials.get(i, []),
       })
   return {"route": route, "steps": steps, "date_mode": date_mode,
           "per_serial": per_serial, "serial_names": names,
           "finished": finished,
           "n_serials": len(by_serial), "n_completed_pairs": sum(step_n.values()),
           "total_lead": tot_lead, "total_cut": tot_cut,
           "total_queue": max(0.0, tot_lead - tot_cut),
           "n_overlap": n_overlap,
           "flow_efficiency": (tot_cut / tot_lead) if tot_lead else None}


# ---------------------------------------------------------------------------
#  ACC  -  A KANBAN BOARD OVER A HAND-PICKED SET OF STEPS
# ---------------------------------------------------------------------------
# The user picks parts, and from each part's traveler the steps they care
# about, and drops every step into a group of their own naming (MASK,
# COATING, BAKE ...).  Different parts - and different steps of one part -
# can share a group.  One step per part is the START: it only lets the part
# onto the board and is never drawn.
#
# EVERY STEP IN BETWEEN IS IGNORED.  The part is placed by the ordinary
# rule (op_progress_for_route) applied to a SHORT route made of the start
# step and the chosen steps only, with only their bookings:
#
#     furthest chosen step with a production entry;
#     closed by its X  -> waiting at the NEXT chosen step
#     not closed       -> at that step (being worked - counted, marked)
#
# So a part that left the start step and has no entry on the next chosen
# step yet is waiting there, whatever it did in between.  A part still on
# the start step, or one that never reached it, is not on the board; one
# whose LAST chosen step is closed by its X has finished and left it.
#
# The definitions live in meta (shared by every copy of the program) under
# ACC_META_KEY as {name: definition}:
#     {"parts":  [{"part_key", "color", "stg", "start"}],
#      "steps":  [{"part_key", "step", "group"}],
#      "groups": [group name, ...]            (column order)
#      "targets": {stg name: pieces per week}}
ACC_META_KEY = "acc_analyses"
ACC_COLORS = ["#4FA3C7", "#2F6E9E", "#E3C04F", "#7FB3D5", "#48C0A8",
           "#C77D4F", "#8E7CC3", "#6AA84F", "#D46A8C", "#A0A0A0"]
# The flags that take a serial off the board unless asked for.
ACC_FLAG_KINDS = ("teco", "mrb", "mrbent", "scrap")


def acc_load(db):
   """{name: definition} - every saved ACC analysis."""
   v = db.get_meta(ACC_META_KEY, None)
   return dict(v) if isinstance(v, dict) else {}


def acc_save(db, name, definition):
   allv = acc_load(db)
   allv[name] = definition
   db.set_meta(ACC_META_KEY, allv)


def acc_delete(db, name):
   allv = acc_load(db)
   if allv.pop(name, None) is not None:
      db.set_meta(ACC_META_KEY, allv)


def acc_check(definition, routes):
   """What is wrong with a definition, as a list of sentences (empty = fine).

   `routes` is {part_key: traveler route}.  Every part needs a start step
   and at least one chosen step AFTER it on the traveler; a chosen step
   needs a group.
   """
   out = []
   steps = definition.get("steps") or []
   for p in definition.get("parts") or []:
      pk = p.get("part_key")
      name = short_part(pk) or pk
      route = routes.get(pk) or []
      pos = {route_step(c): i for i, c in enumerate(route)}
      if not route:
         out.append("%s: no traveler route." % name)
         continue
      start = p.get("start")
      if not start or start not in pos:
         out.append("%s: no START step chosen." % name)
         continue
      own = [s for s in steps if s.get("part_key") == pk]
      if not own:
         out.append("%s: no step put in a group." % name)
      for s in own:
         if not (s.get("group") or "").strip():
            out.append("%s %s: no group." % (name, s.get("step")))
         if s.get("step") not in pos:
            out.append("%s %s: not on the traveler any more."
                     % (name, s.get("step")))
         elif pos[s["step"]] <= pos[start]:
            out.append("%s %s: comes before (or is) the START step."
                     % (name, s.get("step")))
   return out


def acc_short_route(route, start, chosen):
   """The start step and the chosen steps, in traveler order."""
   return [c for c in route
         if route_step(c) == start or route_step(c) in chosen]


def acc_serial_state(short, opmap):
   """Where one serial stands on a short route.

   Returns (position, running, finished_at):
      position     index into `short` where it is, None when it is not
                on the board (never reached the start step, or still
                on it, or finished)
      running      True when it is being worked there (entry, no X)
      finished_at  the X date of the last step when that closed it
   """
   if not short:
      return None, False, None
   _done, running, _prog, here = op_progress_for_route(short, opmap)
   if here is None:
      return None, False, None
   rec = opmap.get(route_step(short[here]))
   closed = bool(rec and rec.get("done_x"))
   last = len(short) - 1
   if closed:
      if here == last:
         return None, False, (rec.get("x_dt") or rec.get("dt"))
      return here + 1, False, None
   if here == 0:
      return None, False, None        # still on the start step
   return here, running is not None, None


def acc_board(db, definition, now=None, exclude_serial_keys=None,
              d1=None, d2=None, exclude_finished_keys=None):
   """Everything the ACC page draws, in one pass over the chosen parts.

   boxes     {(part_key, step): [{"serial", "serial_key", "days",
              "running"}]}  - on the board now, longest wait first
   finished  {(part_key, date): n} - serials that FINISHED their LAST
              chosen step that day, for d1..d2 (both dates)
   labels    {(part_key, step): "350"} - the step as the traveler writes it
   problems  acc_check() sentences

   FINISHED IS THE BOTTLENECK PAGE'S NUMBER (bottleneck_flow, 'Count
   finished without X' on): the day of the X that closed the step, or -
   for a serial that moved on to a LATER traveler step without that X -
   the day of its last production entry on it.  The same "moved past"
   rule closes the chosen steps on the board, so a part that went on
   without its X is not left there as being worked for months.  Steps in
   between that were not chosen still decide nothing about where it
   WAITS: that is only ever the next chosen step.

   `exclude_serial_keys` leaves serials off the board,
   `exclude_finished_keys` out of the finished count (None = the same).
   `days` is calendar days since the serial's last entry on any of its
   chosen steps (the start step included), to one decimal.
   """
   now = now or datetime.datetime.now()
   bad = exclude_serial_keys or ()
   bad_fin = bad if exclude_finished_keys is None else exclude_finished_keys
   steps = definition.get("steps") or []
   routes = {p["part_key"]: db.wf_traveler_for(p["part_key"])
             for p in definition.get("parts") or [] if p.get("part_key")}
   boxes, finished, labels = {}, {}, {}
   for p in definition.get("parts") or []:
      pk = p.get("part_key")
      route = routes.get(pk) or []
      chosen = {s["step"] for s in steps
                if s.get("part_key") == pk and s.get("step")}
      short = acc_short_route(route, p.get("start"), chosen)
      if len(short) < 2 or route_step(short[0]) != p.get("start"):
         continue
      keys = {route_step(c) for c in short}
      for c in short:
         labels[(pk, route_step(c))] = str(c.get("label") or c["op"])
      pos_of = {route_step(c): i for i, c in enumerate(route)}
      sira = [_adim_sirasi(c["op"], c.get("seq")) for c in route]
      full_pos = {route_step(c): pos_of[route_step(c)] for c in short}
      # Per serial: the chosen steps' records (the same step under two
      # material codes merged, as the bottleneck does) and the furthest
      # traveler step with a production entry - any step, chosen or not.
      per = {}
      for r in db.wf_part_data_for(pk):
         sk = r["serial_key"]
         if sk in bad and sk in bad_fin:
            continue
         d = per.setdefault(sk, [r["serial"], {}, -1])
         if r.get("dt_ops"):
            i = pos_of.get(r["step"])
            if i is None:        # off the traveler: reached the step before
               hedef = _adim_sirasi(r["opac"], r.get("seq"))
               i = max([j for j, q in enumerate(sira) if q <= hedef] or [-1])
            d[2] = max(d[2], i)
         if r["step"] not in keys:
            continue
         old = d[1].get(r["step"])
         if old is None:
            d[1][r["step"]] = dict(r)
            continue
         m = dict(old if old["dt"] >= r["dt"] else r)
         m["done_x"] = bool(old.get("done_x") or r.get("done_x"))
         xs = [x for x in (old.get("x_dt"), r.get("x_dt")) if x]
         m["x_dt"] = max(xs) if xs else None
         ops_ = [x for x in (old.get("dt_ops"), r.get("dt_ops")) if x]
         m["dt_ops"] = max(ops_) if ops_ else None
         d[1][r["step"]] = m
      last_key = route_step(short[-1])
      for sk, (serial, opmap, en_ileri) in per.items():
         if not opmap:
            continue
         # A chosen step the serial has moved PAST (a production entry on
         # a later traveler step) is closed, X or not - on the day of its
         # last production entry when there is no X.
         for k, rec in opmap.items():
            if (not rec.get("done_x") and rec.get("dt_ops")
                  and full_pos[k] < en_ileri):
               rec["done_x"] = True
               rec["x_dt"] = rec["dt_ops"]
         rec_last = opmap.get(last_key)
         if sk not in bad_fin and rec_last and rec_last.get("done_x"):
            day = (rec_last.get("x_dt") or rec_last["dt"]).date()
            if (d1 is None or day >= d1) and (d2 is None or day <= d2):
               finished[(pk, day)] = finished.get((pk, day), 0) + 1
         if sk in bad or en_ileri > full_pos[last_key]:
            continue             # gone past the last chosen step
         pos, running, _fin = acc_serial_state(short, opmap)
         if pos is None:
            continue
         last = max(r["dt"] for r in opmap.values())
         days = max(0.0, (now - last).total_seconds() / 86400.0)
         boxes.setdefault((pk, route_step(short[pos])), []).append(
            {"serial": serial, "serial_key": sk,
             "days": round(days, 1), "running": running})
   for v in boxes.values():
      v.sort(key=lambda e: (-e["days"], e["serial"]))
   return {"boxes": boxes, "finished": finished, "labels": labels,
           "problems": acc_check(definition, routes)}


def export_leadtime_to_excel(path, part_disp, time_label, data, unit="hours",
                             groups=None, group_set=""):
   """One row per operation transition, exactly as the page shows it.
   2.2.0: in the unit the page shows (days = hours / 24), and the named
   operation groups on a sheet of their own."""
   wb, st = _xl_open()
   ws = wb.active
   ws.title = "LEAD TIME"
   ws["A1"] = "LEAD TIME ANALYSIS"
   ws["A1"].font = st["Font"](bold=True, size=14)
   u = "d" if unit == "days" else "h"
   k = 24.0 if unit == "days" else 1.0

   def conv(v, nd=2):
       return round(v / k, nd) if v is not None else None
   mode_txt = {"serial": "By serial - serials in the range, all their operations",
               "sold": "Sold in range - serials sold in the range, all their operations"}
   info = [("Part", part_disp), ("Time frame", time_label)]
   if data:
       info += [
           ("Date filter", mode_txt.get(data.get("date_mode"),
                                        "By op date - operation records in the range")),
           ("Traveler operations (after filtering)", len(data["route"])),
           ("Serials seen", data["n_serials"]),
           ("Total lead time (%s)" % u, conv(data.get("total_lead") or 0.0)),
           ("  of which operation time (%s)" % u, conv(data.get("total_cut") or 0.0)),
           ("  of which queueing (%s)" % u, conv(data.get("total_queue") or 0.0)),
           ("Flow efficiency",
            "%.1f%%" % (data["flow_efficiency"] * 100.0)
            if data.get("flow_efficiency") else ""),
           ("Steps whose bookings overlap", data.get("n_overlap") or 0)]
   info += [("Lead time", "clock time between completions (X to X); operation "
                          "time = machine time, labour where there is none - "
                          "production bookings only, never rework"),
            ("Shop calendar", "%g shift(s) x %g h, %d day(s) a week"
             % (SHIFTS_PER_DAY, SHIFT_HOURS, WORK_DAYS)),
            ("Created", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))]
   r = 3
   for kk, v in info:
       ws.cell(row=r, column=1, value=kk).font = st["Font"](bold=True)
       ws.cell(row=r, column=2, value=v)
       r += 1
   _xl_autosize(ws)

   if data:
       ws2 = wb.create_sheet("STEPS")
       # 2.1.3: one operation per row - its lead time runs from the operation
       # on the row above, so a FROM column only repeated that row.
       head = ["OP", "DESCRIPTION",
               "AVG LEAD TIME (%s)" % u, "OPERATION TIME (%s)" % u, "QUEUE (%s)" % u,
               "FLOW EFFICIENCY (%)", "SERIALS"]
       for i, h in enumerate(head, start=1):
           ws2.cell(row=1, column=i, value=h)
       _xl_head(ws2, 1, len(head), st)
       rr = 2
       for s in data["steps"]:
           vals = [s["to_op"], s["to_desc"], conv(s["avg_hours"]),
                   conv(s["cut_hours"]), conv(s["queue_hours"]),
                   round(s["efficiency"] * 100.0, 1) if s["efficiency"] is not None else None,
                   s["n"]]
           for ci, v in enumerate(vals, start=1):
               ws2.cell(row=rr, column=ci, value=v).border = st["box"]
           rr += 1
       _xl_autosize(ws2)

   if groups:
       ws3 = wb.create_sheet("OP GROUPS")
       ws3["A1"] = "Operation groups - %s" % (group_set or "")
       ws3["A1"].font = st["Font"](bold=True, size=12)
       head = ["GROUP", "SUB GROUP", "PART", "FIRST OP", "LAST OP", "MEASURED FROM",
               "AVG LEAD TIME (%s)" % u, "OPERATION TIME (%s)" % u, "QUEUE (%s)" % u,
               "FLOW EFFICIENCY (%)", "SERIALS"]
       for i, h in enumerate(head, start=1):
           ws3.cell(row=3, column=i, value=h)
       _xl_head(ws3, 3, len(head), st)
       rr = [4]

       def line(lead, d, bold, last=None):
           vals = lead + [conv(d["avg_hours"]), conv(d["cut_hours"]), conv(d["queue_hours"]),
                          round(d["efficiency"] * 100.0, 1)
                          if d["efficiency"] is not None else None,
                          d["n"] if last is None else last]
           for ci, v in enumerate(vals, start=1):
               c = ws3.cell(row=rr[0], column=ci, value=v)
               c.border = st["box"]
               if bold:
                   c.font = st["Font"](bold=True)
           rr[0] += 1
       for g in groups:
           line([g["name"], "ALL", "ALL PARTS", "", "", ""], g, True)
           for sb in g.get("subs") or []:
               line([g["name"], sb["name"], "%d part(s)" % len(sb["parts"]), "", "", ""],
                    sb, True)
           for p in g["parts"]:
               line([g["name"], p.get("sub") or "", p.get("part") or p["part_key"],
                     p["first"], p["last"], p["ref"]], p, False,
                     p["problem"] or None)
       _xl_autosize(ws3)
   wb.save(path)


def export_bottleneck_to_excel(path, part_disp, time_label, data):
   """The day-by-day operation load exactly as the page shows it."""
   wb, st = _xl_open()
   ws = wb.active
   ws.title = "SUMMARY"
   ws["A1"] = "PART BOTTLENECK ANALYSIS"
   ws["A1"].font = st["Font"](bold=True, size=14)
   days = data["days"]
   info = [("Part", part_disp), ("Time frame", time_label),
           ("Days shown", len(days)),
           ("First day", days[0].isoformat() if days else "-"),
           ("Last day", days[-1].isoformat() if days else "-"),
           ("Traveler operations", len(data["route"])),
           ("Counted from", "production steps only - rework entries excluded"),
           ("Created", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))]
   if data["truncated"]:
       info.append(("Days left out", "%d earlier day(s)" % data["truncated"]))
   r = 3
   for k, v in info:
       ws.cell(row=r, column=1, value=k).font = st["Font"](bold=True)
       ws.cell(row=r, column=2, value=v)
       r += 1
   _xl_autosize(ws)

   ws2 = wb.create_sheet("DAILY LOAD")
   head = ["OPERATION", "DESCRIPTION", "DISTINCT SERIALS", "TOTAL ENTRIES"]
   for i, h in enumerate(head, start=1):
       ws2.cell(row=1, column=i, value=h)
   for ci, d in enumerate(days, start=len(head) + 1):
       ws2.cell(row=1, column=ci, value=d.isoformat())
   _xl_head(ws2, 1, len(head) + len(days), st)
   rr = 2
   for col in data["route"]:
       # The matrix is keyed by STEP (280/2), not by operation number -
       # op_key here wrote every sequenced step out as zeros.
       ok = route_step(col)
       ws2.cell(row=rr, column=1,
                value=str(col.get("label") or col["op"])).border = st["box"]
       ws2.cell(row=rr, column=2, value=col["desc"]).border = st["box"]
       ws2.cell(row=rr, column=3,
                value=data["row_distinct"].get(ok, 0)).border = st["box"]
       ws2.cell(row=rr, column=4,
                value=data["row_total"].get(ok, 0)).border = st["box"]
       peak = max((data["counts"].get((ok, d), 0) for d in days), default=0)
       for ci, d in enumerate(days, start=len(head) + 1):
           n = data["counts"].get((ok, d), 0)
           cell = ws2.cell(row=rr, column=ci)
           cell.border = st["box"]
           if n:
               cell.value = n
               if n == peak and peak > 0:
                   cell.fill = st["PatternFill"]("solid", fgColor="F7D2C8")
       rr += 1
   ws2.cell(row=rr, column=1, value="ALL OPERATIONS").font = st["Font"](bold=True)
   for ci, d in enumerate(days, start=len(head) + 1):
       c = ws2.cell(row=rr, column=ci, value=data["col_total"].get(d, 0))
       c.font = st["Font"](bold=True)
       c.border = st["box"]
   ws2.freeze_panes = "E2"
   _xl_autosize(ws2, limit=14)

   wb.save(path)
   return path


def export_waterfall_to_excel(result, path, part_disp, unit, time_label):
   """
   The waterfall as a spreadsheet: one row per serial, one column per
   traveler operation holding the moment that operation was booked, with
   the rework hours of that step shown underneath it when there are any.
   """
   wb, st = _xl_open()
   route = result["route"]
   rows = result["rows"]

   ws = wb.active
   ws.title = "SUMMARY"
   ws["A1"] = "WATERFALL LEAD TIME REPORT"
   ws["A1"].font = st["Font"](bold=True, size=14)
   info = [("Part", part_disp), ("Time frame", time_label),
           ("Unit", "days" if unit == "days" else "hours (h:mm)"),
           ("Serials", result["n_serials"]),
           ("Traveler operations", len(route)),
           ("Completed", result["n_completed"]),
           ("In progress (WIP)", result["n_inprogress"]),
           ("Serials with rework", result["n_rework"]),
           ("Total rework hours", round(result["rework_hours"], 3)),
           ("FTY", ("%.1f%%" % result["fty"]) if result["fty"] is not None else "-"),
           ("Average wait per part",
            format_wf_hours(result["total_avg_hours"], unit)
            if result["total_avg_hours"] is not None else "-"),
           ("Created", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))]
   r = 3
   for k, v in info:
       ws.cell(row=r, column=1, value=k).font = st["Font"](bold=True)
       ws.cell(row=r, column=2, value=v)
       r += 1
   if result.get("total_avg_warning"):
       ws.cell(row=r + 1, column=1, value=result["total_avg_warning"])
   _xl_autosize(ws)

   ws2 = wb.create_sheet("MATRIX")
   head = ["SERIAL", "CURRENT OP", "STATUS", "WAITING", "REMAINING"]
   for i, h in enumerate(head, start=1):
       ws2.cell(row=2, column=i, value=h)
   for ci, col in enumerate(route, start=len(head) + 1):
       ws2.cell(row=1, column=ci, value=col["desc"]).font = st["Font"](bold=True,
                                                                       size=8)
       ws2.cell(row=2, column=ci, value=col["op"])
   _xl_head(ws2, 2, len(head) + len(route), st)
   rr = 3
   for row in rows:
       ws2.cell(row=rr, column=1, value=row["serial"]).border = st["box"]
       ws2.cell(row=rr, column=2,
                value=wf_current_op(row, route) or "").border = st["box"]
       ws2.cell(row=rr, column=3, value=row["status"]).border = st["box"]
       ws2.cell(row=rr, column=4,
                value=format_wf_hours(row["waiting_hours"], unit)
                if row["waiting_hours"] is not None else "").border = st["box"]
       ws2.cell(row=rr, column=5,
                value=wf_format_remaining(row["remaining"], unit)).border = st["box"]
       for ci, col in enumerate(route, start=len(head) + 1):
           rec = row["opmap"].get(route_step(col))
           cell = ws2.cell(row=rr, column=ci)
           cell.border = st["box"]
           if rec:
               text = rec["dt"].strftime("%Y-%m-%d %H:%M")
               if rec["rework"] > 0:
                   text += "  RW %s" % fmt_hour(rec["rework"])
                   cell.fill = st["PatternFill"]("solid", fgColor="F7D2C8")
               cell.value = text
       rr += 1
   ws2.freeze_panes = "F3"
   _xl_autosize(ws2, limit=22)

   ws3 = wb.create_sheet("LEAD TIMES")
   for i, h in enumerate(["FROM OP", "TO OP", "TO OP DESCRIPTION", "AVERAGE",
                          "SERIALS USED", "SERIALS EXCLUDED"], start=1):
       ws3.cell(row=1, column=i, value=h)
   _xl_head(ws3, 1, 6, st)
   rr = 2
   for end_pos in sorted(result["averages"]):
       info_a = result["averages"][end_pos]
       d = info_a["def"]
       ws3.cell(row=rr, column=1, value=d["pred"]).border = st["box"]
       ws3.cell(row=rr, column=2, value=d["op"]).border = st["box"]
       ws3.cell(row=rr, column=3, value=d["desc"]).border = st["box"]
       ws3.cell(row=rr, column=4,
                value=format_wf_hours(info_a["avg_hours"], unit)
                if info_a["avg_hours"] is not None else "-").border = st["box"]
       ws3.cell(row=rr, column=5, value=info_a["n_used"]).border = st["box"]
       ws3.cell(row=rr, column=6, value=info_a["n_excluded"]).border = st["box"]
       rr += 1
   _xl_autosize(ws3)

   wb.save(path)
   return path


# ---------------------------------------------------------------------------
#  MRB / SCRAP / TECO  -  out to Excel and back again
# ---------------------------------------------------------------------------
# These records are the one thing in the database that came from people
# rather than from SAP: somebody typed each reason.  They are also the one
# thing no upload can rebuild, so being able to take them out, keep them,
# hand them to somebody, and put them back matters more than for anything
# else the app holds.
#
# The sheet that comes out is the sheet that goes back in - same headers,
# read by name, so a column moved or a note edited in Excel still loads.
FLAG_EXPORT_COLUMNS = [
   ("serial_no", "SERIAL"),
   ("kind", "TYPE"),
   ("part_no", "PART (BY HAND)"),
   ("reason", "REASON"),
   ("entry_date", "ENTRY DATE"),
   ("mrb_prob", "MRB %"),
   ("scrap_prob", "SCRAP %"),
   ("entered_by", "ENTERED BY"),
   # What a fixed case used to be, and when it was put right - the year-end
   # question is "how many MRB parts did we fix", and that cannot be
   # answered from the type alone once the type says 'fixed'.
   ("resolved_from", "WAS"),
   ("resolved_at", "FIXED ON"),
   ("resolved_by", "FIXED BY"),
   ("created_at", "CREATED"),
   ("updated_at", "UPDATED"),
]

FLAG_FIELDS = [
   ("serial_no", "SERIAL NUMBER", True, ["serial", "seri"]),
   ("kind", "TYPE  (teco / mrb / scrap / repair / note)", True,
    ["type", "kind", "tur"]),
   ("reason", "REASON / NOTE", False, ["reason", "note", "sebep", "aciklama"]),
   ("entry_date", "ENTRY DATE", False, ["entry date", "giris tarih", "tarih"]),
   ("mrb_prob", "MRB PROBABILITY", False, ["mrb"]),
   ("scrap_prob", "SCRAP PROBABILITY", False, ["scrap"]),
   ("entered_by", "ENTERED BY", False, ["entered", "giren", "kullanici"]),
   ("part_no", "PART (BY HAND)", False, ["part (by hand)", "part by hand"]),
]

FLAG_VALID_KINDS = ("teco", "mrb", "scrap", "repair", "note",
                    # outcomes, not something you type in
                    "mrbent", "fixed")
# How the kinds may be written in a sheet besides their own key.
FLAG_KIND_ALIASES = {"mrb entered": "mrbent", "mrbentered": "mrbent",
                     "mrb_entered": "mrbent", "mrb ent.": "mrbent",
                     "mrb ent": "mrbent"}


def export_flags_to_excel(path, rows, title="MRB / SCRAP / TECO"):
   """Writes the flag records exactly as they can be read back."""
   wb, st = _xl_open()
   ws = wb.active
   ws.title = "RECORDS"
   for i, (_key, head) in enumerate(FLAG_EXPORT_COLUMNS, start=1):
       ws.cell(row=1, column=i, value=head)
   _xl_head(ws, 1, len(FLAG_EXPORT_COLUMNS), st)
   for r, rec in enumerate(rows, start=2):
       for c, (key, _head) in enumerate(FLAG_EXPORT_COLUMNS, start=1):
           v = rec.get(key)
           if key == "kind":
               v = (v or "").upper()
           ws.cell(row=r, column=c, value=v).border = st["box"]
   _xl_autosize(ws)

   note = wb.create_sheet("HOW TO USE")
   lines = [
       (title, True),
       ("", False),
       ("This sheet can be loaded back into WIP TRACKER:", True),
       ("   DATA & SETTINGS  >  MRB / SCRAP / TECO  >  choose this file",
        False),
       ("", False),
       ("SERIAL and TYPE are required; everything else may be left empty.",
        False),
       ("TYPE must be one of: TECO, MRB, SCRAP, REPAIR, NOTE.", False),
       ("A serial holds ONE record - loading a row replaces what that "
        "serial had.", False),
       ("CHANGE clears every record first; ADD only touches the serials "
        "in the file.", False),
       ("Columns are found by their heading, so their order does not "
        "matter.", False),
       ("", False),
       ("Created %s" % datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        False),
   ]
   for r, (text, bold) in enumerate(lines, start=1):
       cell = note.cell(row=r, column=1, value=text)
       if bold:
           cell.font = st["Font"](bold=True)
   _xl_autosize(note)
   wb.save(path)


def read_flags(path, sheet_name=None, header_row=None):
   """Reads a flag sheet back into records, with what was wrong in it."""
   if header_row is None:
       header_row = sniff_header_row(path, sheet_name)
   headers, rows, _skipped = read_table(path, sheet_name, header_row)
   mapping, missing = auto_map(headers, FLAG_FIELDS)
   if missing:
       raise ValueError("Missing column(s): %s" % ", ".join(missing))
   out, issues = [], {"no_serial": 0, "bad_kind": 0, "rows_in_file": len(rows)}
   for r in rows:
       rec = {k: (clean_str(r[i]) if (i is not None and i < len(r)) else "")
              for k, i in mapping.items()}
       if not rec.get("serial_no"):
           issues["no_serial"] += 1
           continue
       kind = fold(rec.get("kind") or "")
       kind = FLAG_KIND_ALIASES.get(kind, kind)
       if kind not in FLAG_VALID_KINDS:
           issues["bad_kind"] += 1
           continue
       rec["kind"] = kind
       d = parse_date(rec.get("entry_date"))
       rec["entry_date"] = d.isoformat() if d else (rec.get("entry_date") or "")
       out.append(rec)
   issues["loaded"] = len(out)
   return out, issues


def export_cause_to_excel(path, header_info, overall_cols, overall_rows,
                         detail_cols, detail_rows,
                         workcenter_cols=None, workcenter_rows=None):
   """The cause-op tables exactly as they are shown on screen."""
   wb, st = _xl_open()
   ws = wb.active
   ws.title = "SUMMARY"
   ws["A1"] = "CAUSE OP ANALYSIS"
   ws["A1"].font = st["Font"](bold=True, size=14)
   r = 3
   for k, v in header_info:
       ws.cell(row=r, column=1, value=k).font = st["Font"](bold=True)
       ws.cell(row=r, column=2, value=v)
       r += 1
   _xl_autosize(ws)

   ws2 = wb.create_sheet("OVERALL CAUSE SHARE")
   for i, h in enumerate(overall_cols, start=1):
       ws2.cell(row=1, column=i, value=h)
   _xl_head(ws2, 1, len(overall_cols), st)
   for ri, row in enumerate(overall_rows, start=2):
       for ci, v in enumerate(row, start=1):
           ws2.cell(row=ri, column=ci, value=v).border = st["box"]
   ws2.freeze_panes = "A2"
   _xl_autosize(ws2)

   ws3 = wb.create_sheet("PER PART")
   for i, h in enumerate(detail_cols, start=1):
       ws3.cell(row=1, column=i, value=h)
   _xl_head(ws3, 1, len(detail_cols), st)
   for ri, row in enumerate(detail_rows, start=2):
       for ci, v in enumerate(row, start=1):
           ws3.cell(row=ri, column=ci, value=v).border = st["box"]
   ws3.freeze_panes = "A2"
   _xl_autosize(ws3)

   if workcenter_cols and workcenter_rows is not None:
       ws4 = wb.create_sheet("BY REWORK WORKCENTER")
       for i, h in enumerate(workcenter_cols, start=1):
           ws4.cell(row=1, column=i, value=h)
       _xl_head(ws4, 1, len(workcenter_cols), st)
       for ri, row in enumerate(workcenter_rows, start=2):
           for ci, v in enumerate(row, start=1):
               ws4.cell(row=ri, column=ci, value=v).border = st["box"]
       ws4.freeze_panes = "A2"
       _xl_autosize(ws4)

   wb.save(path)
   return path


# ===========================================================================
#  8. WATERFALL LEAD TIME ANALYSIS  -  DATA LAYER
# ===========================================================================

WF_TRAVELER_PART_KEYWORDS = ["parca", "part"]
WF_TRAVELER_OP_KEYWORDS = ["operasyon", "op"]
WF_TRAVELER_PRED_KEYWORDS = ["oncul", "onceki", "predecessor", "pred"]
WF_TRAVELER_DESC_KEYWORDS = ["operasyon tanim", "operasyon adi", "tanim",
                             "aciklama", "description", "desc", "operation",
                             "op desc", "islem", "adi", "name"]


def wf_detect_traveler_format(rows):
   """Which of the three traveler shapes this file is.

   'wide'     repeating  +  |  -  |  PART_NO  column blocks
   'stacked'  a part name on its own, its column headers on the next row,
              then that part's operations, then the next part below
   'long'     one flat table with a part column on every row
   """
   # Stacked is tested first.  Both shapes can carry a "+" column, so the
   # presence of one no longer settles it - what does is whether a part
   # number stands alone above its own header row.  In a wide file the
   # part numbers sit IN the header row, so no block is found there.
   if _stacked_blocks(rows):
       return "stacked"
   for row in rows[:10]:
       for c in row:
           if clean_str(c) == "+":
               return "wide"
   return "long"


def _stacked_blocks(rows):
   """Finds the (part name, header row, first data row) of each block.

   A block starts where a cell stands alone on its row - that is the part
   number - and the row under it names the columns.  Written that way a
   traveler reads as a person would write it by hand, one part after
   another, and nothing has to repeat the part number on every line.
   """
   blocks = []
   for i, row in enumerate(rows[:-1]):
       filled = [(j, clean_str(c)) for j, c in enumerate(row) if clean_str(c)]
       if len(filled) != 1:
           continue
       col, part = filled[0]
       nxt = rows[i + 1]
       plain = [clean_str(c) for c in nxt]
       heads = [fold(c) for c in plain]
       # The operation column is either named, or written as the shop's
       # own "+".  Missing the second case dropped whole blocks whose
       # headers happened to use no word containing "op".
       has_op = ("+" in plain
                 or guess_column(plain, WF_TRAVELER_OP_KEYWORDS) is not None)
       if not has_op or not any(heads):
           continue
       # A part number is not a word like "TRAVELER" or a stray note - it
       # has to look like a part code, i.e. carry a digit.
       if not any(ch.isdigit() for ch in part):
           continue
       blocks.append({"part": part, "col": col, "header_row": i + 1,
                      "first_row": i + 2})
   return blocks


WF_TRAVELER_EXCLUDE_KEYWORDS = ["cikar", "cikart", "exclude", "haric",
                                "pasif", "inactive", "iptal", "kullanilmiyor",
                                "skip", "atla"]

# Truthy marks people actually put in a tick column.
_EXCLUDE_MARKS = {"x", "X", "1", "e", "h", "evet", "yes", "y", "true", "var",
                  "✓", "✔", "*"}


def _is_excluded(v):
   t = clean_str(v)
   return bool(t) and fold(t) in {fold(m) for m in _EXCLUDE_MARKS}


WF_TRAVELER_STD_KEYWORDS = ["std", "standart", "standard", "sure", "süre",
                            "time", "saat", "hour", "dakika", "minute"]
WF_TRAVELER_GROUP_KEYWORDS = ["group", "grup", "tezgah grubu", "machine group",
                              "wc group", "makine grubu", "family", "aile",
                              "comp"]
# What the last traveler read had to say about itself - the upload log
# picks this up.  A module level list rather than a return value because
# the readers are called from several places that all want the routes.
TRAVELER_NOTES = []
WF_SEQ_KEYWORDS = ["seq", "sequence", "sira no", "sira", "sekans", "siralama"]


def wf_read_traveler_stacked(rows):
   """Part name on its own line, headers under it, operations below that.

   Columns are found by their header text rather than by position, so the
   order they are written in does not matter and a column that is not
   there simply comes back empty.
   """
   result, display = {}, {}
   blocks = _stacked_blocks(rows)
   for b in blocks:
       heads = [clean_str(c) for c in rows[b["header_row"]]]
       # "+" and "-" are the shop's own shorthand for the operation and the
       # one before it.  They are matched exactly and before anything else,
       # because a single character would otherwise be swallowed by the
       # loose keyword search.
       plus = next((i for i, h in enumerate(heads) if h == "+"), None)
       minus = next((i for i, h in enumerate(heads) if h == "-"), None)
       mapping = {
           "op": plus if plus is not None
                 else guess_column(heads, WF_TRAVELER_OP_KEYWORDS),
           "desc": guess_column(heads, WF_TRAVELER_DESC_KEYWORDS),
           "pred": minus if minus is not None
                   else guess_column(heads, WF_TRAVELER_PRED_KEYWORDS),
           "std": guess_column(heads, WF_TRAVELER_STD_KEYWORDS),
           "group": guess_column(heads, WF_TRAVELER_GROUP_KEYWORDS),
           # A ticked row is one to leave out of the route - the same thing
           # EDIT TRAVELER does by hand, said in the file instead.
           "exclude": guess_column(heads, WF_TRAVELER_EXCLUDE_KEYWORDS),
           # A separate sequence column, for a traveler that writes 280
           # and 1 side by side instead of 280/1 in the one cell.
           "seq": guess_column(heads, WF_SEQ_KEYWORDS),
       }
       if mapping["op"] is None:
           continue
       # "Operasyon" and "Operasyon Tanım" both contain the same word, so
       # a loose match can land two fields on one column.  The operation
       # number column wins and the other field gives up rather than
       # silently repeating it.
       for k in ("desc", "pred", "std", "group", "exclude", "seq"):
           if mapping[k] == mapping["op"]:
               mapping[k] = None
       # This block ends at the next part name - found independently of
       # whether that block itself parsed, so a header this reader does
       # not recognise can never swallow the part below it.
       stop = len(rows)
       for j in range(b["first_row"], len(rows)):
           filled = [clean_str(c) for c in rows[j] if clean_str(c)]
           if len(filled) == 1 and any(ch.isdigit() for ch in filled[0]):
               stop = j
               break
       part_key = norm_text(b["part"])
       display[part_key] = b["part"]
       out, pos = [], 0
       for r in rows[b["first_row"]:stop]:
           def cell(key):
               i = mapping.get(key)
               return r[i] if (i is not None and i < len(r)) else None
           raw = clean_str(cell("op"))
           if not raw:
               continue
           # "280/1" - operation and sequence in the one cell, the way
           # the traveler is written on paper.
           op, seq = split_step(raw)
           if op is None:
               continue
           if not seq:
               seq = seq_key(cell("seq"))
           # 2.1.3: the predecessor column is no longer read (every lead
           # figure runs from the previous operation); a file that still
           # has one is fine, it is skipped.
           pred = 0
           out.append({"position": pos, "op": op, "seq": seq, "pred": pred,
                       "desc": clean_str(cell("desc")),
                       # Standard times are written in hours.
                       "std_hour": parse_float(cell("std")),
                       "wc_group": clean_str(cell("group")),
                       "active": 0 if _is_excluded(cell("exclude")) else 1})
           pos += 1
       if out:
           result[part_key] = out
   return result, display


def wf_read_traveler_wide(rows):
   """
   Wide layout: repeating blocks  +  |  PART_NO   (or the older
   +  |  -  |  PART_NO - the '-' predecessor column is skipped, 2.1.3).
   '+' = operation column; the part number column's own header text IS
   the part number and the cells under it are the descriptions.  Blocks may be separated by blank columns
   and may run to different lengths - each block stops on its own first blank
   operation cell.  Row order is the real process order and is never
   re-sorted; a repeated operation number in one block is kept as a separate
   row.
   """
   header = rows[0]
   blocks = []
   for i, c in enumerate(header):
       if clean_str(c) != "+":
           continue
       # 2.1.3: the predecessor column "-" is optional - "+ | PART" is
       # enough.  When it is there it is skipped; nothing reads it.
       part_col = i + 2 if (i + 1 < len(header) and clean_str(header[i + 1]) == "-") else i + 1
       part_name = clean_str(header[part_col]) if part_col < len(header) else ""
       if part_name:
           blocks.append({"op_col": i, "part_col": part_col, "part": part_name})
   # ANY EXTRA LABELLED COLUMNS THE BLOCK CARRIES.
   #
   # This layout was read as exactly three columns, so a standard time
   # written beside the operation was never picked up - every step came
   # back with no standard time at all, which is the one figure the
   # whole position model runs on.  Columns after the part number, up to
   # where the next block starts, are now matched by their heading.
   for bi, b in enumerate(blocks):
       son = blocks[bi + 1]["op_col"] if bi + 1 < len(blocks) else len(header)
       basliklar = [clean_str(header[i]) if i < len(header) else ""
                    for i in range(b["part_col"] + 1, son)]
       def bul(anahtarlar, _b=basliklar, _bas=b["part_col"] + 1):
           i = guess_column(_b, anahtarlar)
           return None if i is None else _bas + i
       b["std_col"] = bul(WF_TRAVELER_STD_KEYWORDS)
       b["seq_col"] = bul(WF_SEQ_KEYWORDS)
       b["group_col"] = bul(WF_TRAVELER_GROUP_KEYWORDS)
   result, display = {}, {}
   for b in blocks:
       part_key = norm_text(b["part"])
       display[part_key] = b["part"]
       out, pos = [], 0
       for row in rows[1:]:
           op_cell = row[b["op_col"]] if b["op_col"] < len(row) else None
           if op_cell is None or clean_str(op_cell) == "":
               continue
           op, seq = split_step(op_cell)
           if op is None:
               continue
           pred = 0
           desc = clean_str(row[b["part_col"]]) if b["part_col"] < len(row) else ""

           def hucre(anahtar, _row=row, _b=b):
               i = _b.get(anahtar)
               return _row[i] if (i is not None and i < len(_row)) else None
           if not seq:
               seq = seq_key(hucre("seq_col"))
           out.append({"position": pos, "op": op, "seq": seq, "pred": pred,
                       "desc": desc,
                       "std_hour": parse_float(hucre("std_col")),
                       "wc_group": clean_str(hucre("group_col"))})
           pos += 1
       result[part_key] = out
   return result, display


def wf_read_traveler_long(headers, rows):
   """Long layout: Part | Operation | Predecessor | Description."""
   mapping = {
       "part": guess_column(headers, WF_TRAVELER_PART_KEYWORDS),
       "op": guess_column(headers, WF_TRAVELER_OP_KEYWORDS),
       "pred": guess_column(headers, WF_TRAVELER_PRED_KEYWORDS),
       "desc": guess_column(headers, WF_TRAVELER_DESC_KEYWORDS),
       "std": guess_column(headers, WF_TRAVELER_STD_KEYWORDS),
       "group": guess_column(headers, WF_TRAVELER_GROUP_KEYWORDS),
       "seq": guess_column(headers, WF_SEQ_KEYWORDS),
   }
   result, display, counters = {}, {}, {}
   for r in rows:
       def cell(key):
           idx = mapping.get(key)
           return r[idx] if (idx is not None and idx < len(r)) else None
       part = clean_str(cell("part"))
       if not part:
           continue
       op, seq = split_step(cell("op"))
       if op is None:
           continue
       # A separate SEQ column is read too, when the file has one -
       # some exports write 280 and 1 in two columns instead of 280/1.
       if not seq:
           seq = seq_key(cell("seq"))
       pred = 0                  # 2.1.3: predecessor column not read
       desc = clean_str(cell("desc"))
       part_key = norm_text(part)
       display[part_key] = part
       pos = counters.get(part_key, 0)
       result.setdefault(part_key, []).append(
           {"position": pos, "op": op, "seq": seq, "pred": pred, "desc": desc,
            "std_hour": parse_float(cell("std")),
            "wc_group": clean_str(cell("group"))})
       counters[part_key] = pos + 1
   return result, display


def wf_read_traveler(path, sheet_name=None):
   """Returns (routes, display).  Auto-detects the wide vs. long layout.

   EVERY LINE IS A STEP.  A plain "130" is 130/0 - the zero sequence, a
   piece of work of its own with its own hours - never a total written
   above 130/1, 130/4 (user, 28.09).  Nothing is taken out of the route
   and no hours are moved between lines.  What the file lacks (steps
   with no standard time) goes to TRAVELER_NOTES for the upload log.
   """
   rows = _raw_rows(path, sheet_name)
   if not rows:
       return {}, {}
   fmt = wf_detect_traveler_format(rows)
   if fmt == "wide":
       routes, display = wf_read_traveler_wide(rows)
   elif fmt == "stacked":
       routes, display = wf_read_traveler_stacked(rows)
   else:
       hidx = find_header_row(rows)
       headers = [clean_str(c) for c in rows[hidx - 1]]
       routes, display = wf_read_traveler_long(headers, rows[hidx:])
   notlar = []
   for pk, yeni in routes.items():
       bos = [c for c in yeni if not (c.get("std_hour") or 0.0)]
       if bos:
           notlar.append(
               "%s  -  %d step(s) have NO standard time: %s.  A step with "
               "no standard time counts as finished on any entry at all, "
               "so its hours never hold a part."
               % (display.get(pk, pk), len(bos),
                  ", ".join(("%s/%s" % (c["op"], c["seq"])) if c.get("seq")
                            else str(c["op"]) for c in bos[:8])))
   TRAVELER_NOTES[:] = notlar
   return routes, display


def wf_filter_clean(route, exclude):
   """Drops traveler columns whose description mentions cleaning - those
   steps carry no time entry in practice, so leaving them in the route
   only shows as a permanent gap and a false wait. Positions are
   renumbered so the rest of the route stays a clean 0..n-1 sequence."""
   if not exclude:
       return route
   kept = [c for c in route if "clean" not in (c["desc"] or "").lower()]
   return [dict(c, position=i) for i, c in enumerate(kept)]


def wf_filter_duplicate_ops(route, exclude):
   """Keeps only one column for each STEP - some travelers list the same
   step more than once (repeatable inspection steps entered in no fixed
   order), which otherwise repeats the same column two or three times in
   a row.  The column kept is whichever occurrence actually carries a
   predecessor link, not simply the first one - the first repeat is often
   a filler entry with no predecessor, and picking it over one that does
   name a predecessor would silently drop that operation from every
   lead-time figure.  Positions are renumbered so the rest of the route
   stays a clean 0..n-1 sequence, in first-seen order.

   A SEQUENCE IS NOT A REPEAT.  130, 130/1 and 130/4 are three separate
   pieces of work with their own standard times and their own bookings;
   this used to key on the operation NUMBER, so with "one entry per op"
   ticked - which it is by default - all three collapsed into a single
   column and two thirds of that operation's work vanished from the
   route.  Only lines carrying the SAME sequence are repeats of each
   other.
   """
   if not exclude:
       return route
   best, order = {}, []
   for c in route:
       k = route_step(c)
       if k not in best:
           best[k] = c
           order.append(k)
       elif not best[k]["pred"] and c["pred"]:
           best[k] = c
   kept = [best[k] for k in order]
   return [dict(c, position=i) for i, c in enumerate(kept)]


def wf_build_lead_defs(route):
   """
   One definition per traveler column after the first, each measured from
   the column right before it (2.1.3).  The traveler's own predecessor
   column ("-") is no longer read for this - the user stopped writing it,
   and every other lead figure already runs from the previous operation.
   """
   defs = []
   for end_pos in range(1, len(route)):
       col, prev = route[end_pos], route[end_pos - 1]
       defs.append({"end_pos": end_pos, "start_pos": end_pos - 1,
                    "op": col["op"], "pred": prev.get("label") or prev["op"],
                    "desc": col["desc"]})
   return defs


def wf_auto_lead_defs(route, defs):
   """Lead-time definitions for the operations the traveler's own ones do
   not cover: each measured from the operation right before it (v67).
   A position counts as covered when it lies inside an explicit span
   [start_pos+1 .. end_pos]."""
   covered = set()
   for d in defs:
       covered.update(range(d["start_pos"] + 1, d["end_pos"] + 1))
   out = []
   for end_pos in range(1, len(route)):
       if end_pos in covered:
           continue
       col = route[end_pos]
       out.append({"end_pos": end_pos, "start_pos": end_pos - 1,
                   "op": col["op"], "pred": route[end_pos - 1]["op"],
                   "desc": col["desc"], "auto": True})
   return out


def _wf_overlaps(a, b):
   return not (a[1] < b[0] or a[0] > b[1])


def format_wf_hours(hours, unit):
   """Hours or days, always as a plain decimal with two places - 0.00.
   (Hours used to show as [h]:mm; the shop reads 0.00 more easily, and the
   lead-time boxes and REMAINING then read the same way in both units.)"""
   if hours is None:
       return ""
   hours = max(0.0, hours)
   if unit == "days":
       return "%.2f" % (hours / 24.0)
   return "%.2f" % hours


def wf_format_remaining(remaining, unit):
   kind, value = remaining
   if kind == "done":
       return "Done"
   if kind == "insufficient":
       return "Insufficient data"
   return format_wf_hours(value, unit)


# ===========================================================================
#  OPERATION PROGRESS
# ===========================================================================
#
# The plant runs three eight-hour shifts, and labour is booked one shift at
# a time.  So a booking on an operation does NOT mean the operation is
# finished - on a step whose standard time is longer than a shift it only
# means somebody worked on it for part of a shift.  Reading every booking
# as "done" pushed such a part onto the NEXT operation and reported it as
# waiting there, when in truth it was still on the machine being cut.
#
# What the data does support: the standard time from the traveler, the
# labour actually booked against that operation (SAP's Toplam Iscilik,
# which is the total including rework - not just the rework column), and
# when the last booking was made.  Those three give a real answer:
#
#     booked   9h of a 13h step, last entry an hour ago
#     -> about 10h of work has gone into it, roughly 3h left,
#        and the part is still AT that operation, not waiting for the next.
#
SHIFT_HOURS = 8.0
SHIFTS_PER_DAY = 3.0
WORK_DAYS = 7
# WHICH days the shop works, as weekday numbers (0 = Monday).  A count on
# its own ("six days a week") does not say WHICH six, so the program had
# to guess - and guessed wrong for a shop that is closed on a Friday
# rather than a Sunday.  The days are now ticked one by one in SHOP
# RULES, and the count is simply how many are ticked.
WORK_WEEKDAYS = (0, 1, 2, 3, 4, 5, 6)
IDLE_MINUTES = 0.0

# How far under its standard time a step may be booked and still count as
# finished.  Setup and tooling make the real time drift either side of the
# figure on the traveler, so there is room here for a margin - but for now
# the standard time is taken at face value: a step is done when the booked
# labour reaches it.  Raising this to 0.10 allows the 10% margin instead;
# it is the only number that needs changing.
STD_TOLERANCE = 0.0


# ---------------------------------------------------------------------------
#  SHOP RULES
# ---------------------------------------------------------------------------
# Three numbers decide how the data is read: how far under standard a step
# may be booked and still count as finished, how long a shift is, and which
# machine groups run several parts at once.  They belong to the shop, not to
# the program, so they live in the database and are editable in DATA &
# SETTINGS.  The values above are the defaults a fresh database starts with.
#
# They are copied into module globals rather than read per row: op_progress
# runs once per operation per serial, and a database lookup in that loop
# would be felt.
RULE_DEFAULTS = {"std_tolerance": 0.0, "shift_hours": 8.0,
                 "shifts_per_day": 3.0, "work_days": 7,
                 # Minutes of nobody touching the keyboard or mouse before
                 # this copy closes itself.  0 turns it off.  A copy left
                 # open overnight holds the shared file and is the reason a
                 # new build cannot go out in the morning, so this is set
                 # once by an admin and applies to everybody.
                 "idle_minutes": 0.0,
                 "multi_part_groups": "ULTRAPOLISH, CLEAN, BLUE ETCH, FPI"}


def apply_rules(db):
   """Loads the shop rules out of the database into the module."""
   global STD_TOLERANCE, SHIFT_HOURS, MULTI_PART_GROUPS
   global SHIFTS_PER_DAY, WORK_DAYS, IDLE_MINUTES, WORK_WEEKDAYS
   try:
       tol = float(db.get_meta("rule_std_tolerance",
                               RULE_DEFAULTS["std_tolerance"]))
       shift = float(db.get_meta("rule_shift_hours",
                                 RULE_DEFAULTS["shift_hours"]))
       per_day = float(db.get_meta("rule_shifts_per_day",
                                   RULE_DEFAULTS["shifts_per_day"]))
       days = int(float(db.get_meta("rule_work_days",
                                    RULE_DEFAULTS["work_days"])))
       idle = float(db.get_meta("rule_idle_minutes",
                                RULE_DEFAULTS["idle_minutes"]))
       groups = db.get_meta("rule_multi_part_groups",
                            RULE_DEFAULTS["multi_part_groups"])
       gunler = db.get_meta("rule_work_weekdays", None)
   except Exception:
       return read_rules()
   # Ticked days win.  A database written before they existed has none, so
   # the old count stands in - the first `days` weekdays from Monday,
   # which is what it used to mean.
   if gunler:
       try:
           WORK_WEEKDAYS = tuple(sorted({int(x) % 7 for x in gunler}))
       except Exception:
           WORK_WEEKDAYS = tuple(range(min(7, max(1, days))))
   else:
       WORK_WEEKDAYS = tuple(range(min(7, max(1, days))))
   if not WORK_WEEKDAYS:
       WORK_WEEKDAYS = (0,)
   STD_TOLERANCE = max(0.0, min(0.9, tol))
   SHIFT_HOURS = max(0.5, min(24.0, shift))
   SHIFTS_PER_DAY = max(1.0, min(3.0, per_day))
   WORK_DAYS = len(WORK_WEEKDAYS)
   _apply_shift_schedule(db)
   # Below a minute it would close on somebody reading the screen; above a
   # day it is not an idle timeout any more.
   IDLE_MINUTES = 0.0 if idle <= 0 else max(1.0, min(1440.0, idle))
   MULTI_PART_GROUPS = tuple(
       norm_text(g) for g in str(groups).replace(";", ",").split(",")
       if g.strip())
   return read_rules()


def _apply_shift_schedule(db):
   """Loads the timetable, or builds one from the old rules.

   A database written before the timetable existed carries only "N hours
   x M shifts on these days".  Rather than asking anybody to type it in
   again, that is turned into the equivalent timetable: M shifts of N
   hours starting at 07:00, on each ticked day.  So an old file opens
   with exactly the capacity it had, and the times can then be corrected
   where they are not right.
   """
   global SHIFT_SCHEDULE
   ham = None
   try:
       ham = db.get_meta("rule_shift_schedule", None)
   except Exception:
       ham = None
   takvim = {}
   if isinstance(ham, dict) and ham:
       for gun, vardiyalar in ham.items():
           try:
               g = int(gun) % 7
           except (TypeError, ValueError):
               continue
           liste = []
           for v in (vardiyalar or ()):
               try:
                   bas, bit = _hhmm(v[0]), _hhmm(v[1])
               except (TypeError, IndexError):
                   continue
               uzun = (bit - bas) if bit > bas else (bit + 24 * 60 - bas)
               if uzun > 0:
                   liste.append((bas, min(uzun, 24 * 60)))
           if liste:
               takvim[g] = sorted(liste)
   else:
       uzun = int(round(max(0.5, min(24.0, SHIFT_HOURS)) * 60))
       kac = int(max(1, min(3, round(SHIFTS_PER_DAY))))
       for g in WORK_WEEKDAYS:
           bas = 7 * 60
           liste = []
           for _i in range(kac):
               liste.append((bas % (24 * 60), uzun))
               bas += uzun
           takvim[int(g) % 7] = sorted(liste)
   SHIFT_SCHEDULE = takvim
   return SHIFT_SCHEDULE


def schedule_for_meta(takvim=None):
   """The timetable in the form it is stored and shown: {weekday:
   [["07:00","15:00"], ...]}."""
   takvim = SHIFT_SCHEDULE if takvim is None else takvim
   out = {}
   for gun, liste in takvim.items():
       out[str(int(gun) % 7)] = [[_mmhh(b), _mmhh(b + n)] for b, n in
                                 sorted(liste)]
   return out


def save_shift_schedule(db, schedule):
   """schedule: {weekday: [(start, end), ...]} with "HH:MM" strings.

   The working days follow from it - a day with no shift on it is a day
   the shop does not work, and keeping a separate list of ticked days
   beside the timetable is how the two get to disagree.
   """
   temiz = {}
   for gun, vardiyalar in (schedule or {}).items():
       try:
           g = int(gun) % 7
       except (TypeError, ValueError):
           continue
       liste = []
       for v in (vardiyalar or ()):
           bas, bit = clean_str(v[0]), clean_str(v[1])
           if not bas or not bit:
               continue
           if _hhmm(bas) == _hhmm(bit):
               continue                 # zero length is not a shift
           liste.append([bas, bit])
       if liste:
           temiz[str(g)] = liste
   gunler = sorted(int(g) for g in temiz) or [0]
   if (db.get_meta("rule_shift_schedule", None) == temiz
           and db.get_meta("rule_work_weekdays", None) == gunler):
       return apply_rules(db)          # nothing changed: no recount
   db.set_meta("rule_shift_schedule", temiz)
   db.set_meta("rule_work_weekdays", gunler)
   invalidate_caches()
   db.bump_version()
   return apply_rules(db)


def read_rules():
   """The rules as they stand, for showing and for saving back."""
   return {"std_tolerance": STD_TOLERANCE, "shift_hours": SHIFT_HOURS,
           "shifts_per_day": SHIFTS_PER_DAY, "work_days": WORK_DAYS,
           "work_weekdays": WORK_WEEKDAYS,
           "idle_minutes": IDLE_MINUTES,
           "shift_schedule": schedule_for_meta(),
           "multi_part_groups": ", ".join(MULTI_PART_GROUPS)}


def save_rules(db, std_tolerance=None, shift_hours=None,
               multi_part_groups=None, shifts_per_day=None, work_days=None,
               idle_minutes=None, work_weekdays=None):
   """Writes the rules back and puts them straight into effect.

   Only a rule that really changed and that the figures depend on says
   "the data changed" (bump_version): that throws away every copy's
   caches, and on the shared file the recount took a minute or two - for
   the idle close time, which no figure reads, that was pure waste."""
   before = read_rules()
   if std_tolerance is not None:
       db.set_meta("rule_std_tolerance", float(std_tolerance))
   if shift_hours is not None:
       db.set_meta("rule_shift_hours", float(shift_hours))
   if shifts_per_day is not None:
       db.set_meta("rule_shifts_per_day", float(shifts_per_day))
   if work_days is not None:
       db.set_meta("rule_work_days", int(work_days))
   if work_weekdays is not None:
       db.set_meta("rule_work_weekdays",
                   sorted({int(x) % 7 for x in work_weekdays}) or [0])
   if idle_minutes is not None:
       db.set_meta("rule_idle_minutes", float(idle_minutes))
   if multi_part_groups is not None:
       db.set_meta("rule_multi_part_groups", str(multi_part_groups))
   now = apply_rules(db)
   if any(before.get(k) != now.get(k) for k in now if k != "idle_minutes"):
       invalidate_caches()
       db.bump_version()
       now["changed"] = True
   return now


# ===========================================================================
#  THE SHIFT TIMETABLE
# ===========================================================================
#
# The shop does not work "7.5 hours times 3" - it works three named
# shifts, and which of them run depends on the day of the week:
#
#     07:00-15:00     15:00-23:00     23:00-07:00
#
# Saturday may run one of them, Sunday none.  A capacity worked out from
# one average day length cannot say that, and the night shift - which
# starts on one date and ends on the next - cannot be written as "N hours
# from midnight" at all.
#
# So the timetable is kept as it is worked: per weekday, a list of
# (start, end) clock times.  A shift whose end is not after its start
# runs through midnight and belongs to the day it STARTS on.
SHIFT_SCHEDULE = {}          # {weekday: [(start_min, length_min), ...]}


def _hhmm(value, fallback=0):
   """"07:30" -> 450 minutes past midnight."""
   t = clean_str(value)
   if not t:
       return fallback
   for sep in (":", "."):
       if sep in t:
           a, _s, b = t.partition(sep)
           try:
               return (int(float(a)) % 24) * 60 + min(59, max(0, int(float(b))))
           except ValueError:
               return fallback
   try:
       return (int(float(t)) % 24) * 60
   except ValueError:
       return fallback


def _mmhh(minutes):
   m = int(round(minutes)) % (24 * 60)
   return "%02d:%02d" % (m // 60, m % 60)


def shift_schedule():
   """{weekday: [(start minute, length in minutes), ...]}, as it runs."""
   return SHIFT_SCHEDULE


def day_shift_hours(weekday):
   """How many hours the shop works on that weekday."""
   return sum(n for _s, n in SHIFT_SCHEDULE.get(int(weekday) % 7, ())) / 60.0


def shop_hours_per_day():
   """How many hours the shop is open on an AVERAGE working day.

   Kept because a few older readings want one number; anything that
   actually has a date should ask day_shift_hours(), which can tell
   Saturday from Tuesday.
   """
   gunler = [day_shift_hours(g) for g in range(7)]
   calisan = [h for h in gunler if h > 0]
   return (sum(calisan) / len(calisan)) if calisan else 0.0


def shop_intervals(day):
   """The shop's open periods on one DATE, as (from, to) datetimes.

   A night shift is listed on the day it starts and runs into the next -
   which is exactly how the people working it describe it, and the only
   way "23:00-07:00" can be counted once rather than split in two.
   """
   out = []
   for start, length in SHIFT_SCHEDULE.get(day.weekday(), ()):
       bas = datetime.datetime.combine(day, datetime.time(0, 0)) \
           + datetime.timedelta(minutes=start)
       out.append((bas, bas + datetime.timedelta(minutes=length)))
   return out


def shop_hours_between(a, b):
   """Hours the shop was actually open between two bookings.

   Elapsed clock time is not the same thing as queue time.  A part that
   comes off a machine at 18:00 on Friday and is booked into the next
   operation at 08:00 on Monday did not queue for 62 hours; on a shop that
   does not work weekends it queued for 14.  Counting the shut hours as
   queue makes every step that straddles a weekend look like the worst
   one on the route, which is exactly the sort of wrong answer that gets
   an analysis stopped being trusted.

   THE SHIFT TIMETABLE IS WHAT IS COUNTED - the actual periods the shop
   is open on each weekday (see shop_intervals).  It used to assume the
   day began at midnight and ran for however many hours the shop worked,
   which no shop does: a part finished at 06:00 was credited with six
   hours of queue on a shop that only opens at 07:00, and a night shift
   running 23:00-07:00 was counted on the wrong side of midnight
   entirely.
   """
   if b <= a:
       return 0.0
   if not SHIFT_SCHEDULE:
       return (b - a).total_seconds() / 3600.0
   # Open around the clock, every day: then it is just the difference.
   if all(abs(day_shift_hours(g) - 24.0) < 1e-9 for g in range(7)):
       return (b - a).total_seconds() / 3600.0

   # Open hours up to b, less open hours up to a - the same sum the old
   # day-by-day walk made, without walking: this is asked tens of
   # thousands of times per lead-time analysis, and a queue that spans
   # months was a walk of months each time.
   dk_b, sn_b = _open_until(b)
   dk_a, sn_a = _open_until(a)
   return ((dk_b - dk_a) * 60.0 + (sn_b - sn_a)) / 3600.0


# 1 January 1900 was a Monday: counting whole days from there, day i is
# weekday i % 7.
_OPEN_BASE = datetime.date(1900, 1, 1).toordinal()


def _open_until(t):
   """How long the shop has been open from 1900 up to the moment t, as
   (whole minutes of the days before, seconds of the last two days).

   Shifts that started two or more days before t are over by then (none
   is longer than a day), so they count in full and only depend on the
   weekday - whole weeks times the week's hours plus the odd days.  The
   last two days are the only ones measured against the clock; a night
   shift starting the day before is among them.
   """
   g = t.toordinal()
   hafta = [sum(n for _s, n in SHIFT_SCHEDULE.get(w, ())) for w in range(7)]
   tam, artan = divmod(g - 1 - _OPEN_BASE, 7)
   dakika = tam * sum(hafta) + sum(hafta[:artan])
   saniye = 0.0
   for gun in (g - 1, g):
       for open_at, shut_at in shop_intervals(datetime.date.fromordinal(gun)):
           if t > open_at:
               saniye += (min(t, shut_at) - open_at).total_seconds()
   return dakika, saniye


# ---------------------------------------------------------------------------
#  WORDS ON SCREEN
# ---------------------------------------------------------------------------
# The shop floor reads these, so they are in the shop's language and in one
# place - they were half English, half Turkish, spread across two pages.
WORDS = {
   "processing": "ON MACHINE",      # booked, short of its standard time
   "waiting":    "waiting",         # finished; next operation not booked
   "queued":     "queued",          # another part is on that machine
   "idle":       "IDLE",            # booked, nothing happening
   "done":       "done",            # operation finished, part not moved on
   "no_wc":      "no WC",           # machine symbol with no work centre
   "on_floor":   "%d serial on the floor",
   "being_cut":  "%d on a machine",
   "is_waiting": "%d waiting, %d of them in a waiting area",
}


def short_part(material):
   """The short name people use for a part: 2564M10G02 -> M10.

   A full material number is too long to sit on a machine symbol, and the
   letter-and-number group in the middle is what everybody says out loud
   anyway.  Anything that does not fit that shape comes back trimmed
   rather than empty, so a part is never left unnamed.
   """
   t = clean_str(material).upper()
   if not t:
       return ""
   m = re.search(r"[A-Z]\d+", t)
   return m.group(0) if m else t[-6:]


def op_progress(booked, std, last_dt=None, now=None):
   """How far along one operation is for one serial.

   `booked` is the machine time booked against the operation (SAP's
   Activity to conf), `std` its traveler standard time.

   The rule is simply:

       booked >= standard   the operation is finished and the part is
                            waiting for the next one        -> WAITING
       0 < booked < standard   the part is on that machine   -> PROCESSING
       booked == 0          a record exists but no time was booked against
                            it, so nothing has been cut here: the part is
                            waiting AT this operation        -> WAITING

   That last case matters more than it sounds.  SAP carries confirmations
   with no hours on them, and reading one as "on the machine, 0 of 26 h
   done" puts a part on a machine that no work has been booked to - the
   floor plan then shows that machine as occupied, and the waterfall shows
   a step in progress that nobody has started.

   So a 26 h operation with two entries of 7.5 h has 15 h on it, 11 h of
   machine time still to go, and the part is still there.

   `last_dt` is recorded but takes no part in the decision - see the note
   on the clock below.

   Returns a dict, or None when the operation has no standard time - the
   caller then has nothing better than "a booking means it happened".
   """
   std = float(std or 0.0)
   if std <= 0:
       return None
   booked = max(0.0, float(booked or 0.0))
   now = now or datetime.datetime.now()
   lo = std * (1.0 - STD_TOLERANCE)
   hi = std * (1.0 + STD_TOLERANCE)

   elapsed = None
   if last_dt is not None:
       elapsed = max(0.0, (now - last_dt).total_seconds() / 3600.0)
   # THE CLOCK IS NOT PART OF THIS.  Where a part is, is decided on what
   # the shop floor actually booked against the operation and nothing
   # else; the time since the last entry is used only by the PREDICTION
   # view, which is explicitly a forecast.  Letting it count here meant an
   # operation "finished" itself overnight with nobody near it, which is
   # how parts that were plainly still on a machine came up as waiting.
   done_est = booked

   # 13 * 0.9 lands on 11.700000000000001, so a step booked at exactly the
   # tolerance figure a planner would write down misses the test by a
   # rounding error.  Compared to a hundredth of an hour, which is finer
   # than machine time is ever booked.
   complete = booked >= lo - 1e-9

   return {
       "std": std, "lo": lo, "hi": hi,
       "booked": booked,
       "done_est": done_est,
       "remaining": 0.0 if complete else max(0.0, std - booked),
       "pct": min(1.0, booked / std),
       # Kept for callers that ask for the estimate by name; with the clock
       # out of the sum the two are the same figure.
       "pct_est": min(1.0, booked / std),
       "pct_booked": booked / std,
       "complete": complete,
       "over": booked > hi,
       # Reported, never used to decide anything here.
       "elapsed": elapsed,
       "credit": 0.0,
       "by_clock": False,
       "waited": 0.0,
       "idle": False,
       "stalled": False,
       # Has anybody booked anything at all against this step.
       "started": booked > 0.0,
       # Booked short of standard AND actually booked: the part is on that
       # machine being cut.  Zero hours is not a machine running slowly, it
       # is a machine nothing has been booked to.
       "running": (not complete) and booked > 0.0,
   }


# ---------------------------------------------------------------------------
#  PREDICTION
# ---------------------------------------------------------------------------
# Everywhere else a part sits exactly where the bookings put it and the
# clock is not consulted.  These two numbers are the only place it is, and
# they belong to the shop, so they are named here rather than buried.
#
# PREDICT_IDLE_HOURS   nobody has booked anything against the operation for
#                      this long, so whatever else the arithmetic says, the
#                      part is not being cut any more - it has come off and
#                      is waiting for the next operation.
# PREDICT_MAX_DAYS     past this the part is not "probably a bit further
#                      along", it is lost, stuck or forgotten.  Projecting
#                      it forward would invent a position out of nothing,
#                      so prediction simply does not apply.
PREDICT_IDLE_HOURS = 6.0
PREDICT_MAX_DAYS = 20.0


# predict_position() and wf_predicted_by_hours() WERE REMOVED (v62): they
# moved a part off its step once "hours booked + hours since" reached the
# standard time.  The X alone finishes a step now, so a prediction built
# on the hours would contradict the position it starts from.  The one
# prediction left is by lead time - see wf_predicted_view.

def step_is_done(rec):
   """Is this step closed?  Only the X decides (v62) - a production
   booking that is not a setup carrying it.  The hours never finish a
   step."""
   return bool(rec and rec.get("done_x"))

def row_get(row, name, default=None):
   """`row[name]` for a dict OR an sqlite3.Row.

   Rows travel through these functions as either: a query hands back
   sqlite3.Row, which has no .get, while everything built in Python is a
   dict.  A column added in a later schema is missing from an old row,
   so the default matters.
   """
   try:
       v = row[name]
   except (KeyError, IndexError):
       return default
   return default if v is None else v


def route_step(col):
   """The step key of one route line, built on the spot when the line
   does not carry one - a route assembled by hand (a test, an older
   caller) still works."""
   k = col.get("step")
   return k if k else step_key(col.get("op"), col.get("seq"))


def _adim_sirasi(op, seq):
   """(operation, sequence) as numbers, for ordering steps.

   The traveler is always in ascending order, sequences included: 280/0
   comes first, then 280/2, then 280/7.  "280" and "280/0" are the same
   step.
   """
   try:
       o = int(float(op))
   except (TypeError, ValueError):
       o = 0
   try:
       q = int(float(seq or 0))
   except (TypeError, ValueError):
       q = 0
   return (o, q)


def _uretim_dt(rec):
   """The last PRODUCTION entry of a step record (dt_ops).  A record built
   by hand without that field - a test, a what-if - is taken as all
   production, its `dt` standing in.  Data read from the database always
   carries the field, None when the step only ever saw rework."""
   if not rec:
       return None
   return rec["dt_ops"] if "dt_ops" in rec else rec.get("dt")


# ---------------------------------------------------------------------------
#  LEAD TIME FROM COMPLETIONS  (2.1.2)
# ---------------------------------------------------------------------------
# A step's lead time is measured between two COMPLETIONS, in plain clock
# hours (not shift hours):
#
#     this operation closed by its X   (the X date and time)
#     minus
#     the previous operation's completion: its X, or - when it was passed
#     without one - its last production entry (last entry of any kind if
#     it only ever saw rework).
#
# An operation that is not closed by an X has no lead time yet.  A
# negative gap (bookings out of order) is dropped, and so is a gap more
# than LEAD_OUTLIER_X times the step's median - a part parked in a corner
# for two months says nothing about how long the step takes.
LEAD_OUTLIER_X = 3.0


def lead_done_dt(rec):
   """When an operation was completed: its X, else its last production
   entry, else its last entry.  None when there is no record."""
   if not rec:
      return None
   if rec.get("done_x") and rec.get("x_dt"):
      return rec["x_dt"]
   return _uretim_dt(rec) or rec.get("dt")


def lead_x_dt(rec):
   """The X date of an operation closed by one, else None."""
   if rec and rec.get("done_x") and rec.get("x_dt"):
      return rec["x_dt"]
   return None


def lead_gap_hours(prev_rec, rec):
   """Clock hours from the previous operation's completion to this one's
   X, or None when this one is not closed by an X or there is no previous
   record.  Can be negative - the caller drops those."""
   end = lead_x_dt(rec)
   start = lead_done_dt(prev_rec)
   if end is None or start is None:
      return None
   return (end - start).total_seconds() / 3600.0


def lead_keep_mask(values):
   """[True/False] per value: kept unless negative or more than
   LEAD_OUTLIER_X times the median of the non-negative ones (the median
   only counts from three values up - two figures cannot say which one is
   the odd one out)."""
   ok = sorted(v for v in values if v is not None and v >= 0)
   limit = None
   if len(ok) >= 3:
      m = len(ok) // 2
      med = ok[m] if len(ok) % 2 else (ok[m - 1] + ok[m]) / 2.0
      if med > 0:
         limit = med * LEAD_OUTLIER_X
   return [v is not None and v >= 0 and (limit is None or v <= limit)
           for v in values]


def lead_cut_hours(rec):
   """The OPERATION TIME of an operation: its machine time, or - for an
   operation that has none (hand work, inspection) - its labour.

   2.2.0: PRODUCTION bookings only - a rework's hours are never operation
   time.  A record built by hand without the production-only fields (a
   test, an older caller) is read from its totals."""
   if not rec:
      return 0.0
   m = rec.get("machine_ops")
   if m is None:
      return (rec.get("machine_hour") or 0.0) or (rec.get("labour_hour") or 0.0)
   return m or (rec.get("labour_ops") or 0.0)


_ROTA_BILGI = {}


def _rota_bilgisi(route):
   """2.1.1: what op_progress_for_route needs from the route alone, worked
   out once per route instead of once per serial (a 400-serial part used
   to rebuild it 400 times): {step: position}, and the steps' order keys
   sorted with, for each, the furthest position at or before it.  Held
   for the route object itself (the analyser hands the same list to every
   serial); a different list - even an equal one - is worked out anew."""
   hit = _ROTA_BILGI.get(id(route))
   if hit is not None and hit[0] is route and hit[1] == len(route):
       return hit[2], hit[3]
   adim = {route_step(c): i for i, c in enumerate(route)}
   sirali = sorted((_adim_sirasi(c["op"], c.get("seq")), i) for i, c in enumerate(route))
   keys, best, m = [], [], -1
   for k, i in sirali:
       m = max(m, i)
       keys.append(k)
       best.append(m)
   if len(_ROTA_BILGI) > 400:
       _ROTA_BILGI.clear()
   _ROTA_BILGI[id(route)] = (route, len(route), adim, (keys, best))
   return adim, (keys, best)


def op_progress_for_route(route, opmap, now=None):
   """Where one serial is on its route, and which steps are behind it.

   THE RULE (v62) - the shop's own, written down with its Excel formula:

     1  HOW FAR THE PART HAS GOT is the FURTHEST step along the traveler
        that carries a production time entry.  Neither the date of an
        entry nor a rework booking changes it: a rework booked against an
        early operation, or a late correction on one, does not pull the
        part back.
     2  THAT STEP IS FINISHED only when a production booking against it
        that is NOT a setup (Unit "H") carries the X in the Son column.
          - such an X            -> finished; the part is WAITING at the
                                    next step
          - only a setup's X, or
            no X at all          -> the part is PROCESSING at this step
     3  EVERY STEP BEFORE IT IS FINISHED, X or not - the part has moved on.
     4  THE HOURS NEVER DECIDE.  They only show how far through the step
        the part is (Toplam Is of non-setup production bookings against
        the standard time).

   Sequences are ordinary steps in traveler order - 280/0, 280/2, 280/7 -
   and follow exactly the same rule; there is no separate "an operation
   ends when all its sequences end" logic any more.  That logic, together
   with a fallback that lent one sequence's booking to another, is what
   left a base sequence blank behind two green ones.

   Returns (done_positions, running_pos, progress_by_pos, here_pos):

     done_positions  BOOKED steps that are finished: every booked step
                     before here_pos, and here_pos itself when it is
                     closed by an X.  A step behind the part that nobody
                     ever booked is passed, not "done" - it has no date.
     running_pos     here_pos while it is not closed, else None
     progress        per booked step: op_progress() for the hours, with
                     "complete" and "running" set by the rule above
     here_pos        the furthest step reached (rule 1)

   A booking against a step the traveler does not list only ever says
   how far the part got.
   """
   now = now or datetime.datetime.now()
   opmap = opmap or {}
   rota_adim, rota_sira = _rota_bilgisi(route)

   booked = sorted(rota_adim[k] for k in opmap if k in rota_adim)
   uretim = [i for i in booked
             if _uretim_dt(opmap[route_step(route[i])])]

   # A PRODUCTION booking against a step the traveler does not list still
   # proves the part got that far: it counts as having reached the last
   # route step that comes before it.
   rota_disi = -1
   for k, rec in opmap.items():
       if k in rota_adim or not _uretim_dt(rec):
           continue
       op_, sq_ = split_step(k)
       hedef = _adim_sirasi(op_, sq_)
       # the furthest route position whose step comes at or before it
       j = bisect.bisect_right(rota_sira[0], hedef)
       if j:
           rota_disi = max(rota_disi, rota_sira[1][j - 1])

   if uretim:
       here = max(uretim)
   elif booked:
       # Nothing but rework: those stamps are all there is, so the
       # furthest of them stands in rather than showing nothing.
       here = max(booked)
   else:
       here = None
   if rota_disi >= 0 and (here is None or rota_disi > here):
       here = rota_disi
   if here is None:
       return set(), None, {}, None

   rec_here = opmap.get(route_step(route[here]))
   kapali = bool(rec_here and rec_here.get("done_x"))

   done = {i for i in booked if i < here}
   if kapali:
       done.add(here)
   running = (here if (rec_here is not None and not kapali
                       and _uretim_dt(rec_here)) else None)

   progress = {}
   for i in booked:
       rec = opmap[route_step(route[i])]
       p = op_progress(rec.get("labour"), route[i].get("std_hour"),
                       rec.get("dt"), now)
       if p is None:
           continue
       p = dict(p)
       p["complete"] = i in done
       p["running"] = i == running
       progress[i] = p
   return done, running, progress, here


def wf_current_op(row, route):
   """2.1.2 CURRENT OP: the operation the waterfall matrix shows the part
   at, read by the same rule the matrix is painted with - the step it is
   being worked on, else the step it is waiting at (the one it stands on
   if not yet closed by its X, else the next one).  A part done with the
   route shows its last operation; a part not started shows None.  On a
   predicted view the row already carries the predicted position, so the
   predicted step is what comes back.  Label as on the route (280/2)."""
   if not route:
      return None

   def lbl(p):
      return route[p].get("label") or route[p]["op"]
   if row.get("all_done"):
      return lbl(len(route) - 1)
   run = row.get("running_pos")
   if run is not None:
      return lbl(run)
   here = row.get("here_pos")
   if here is None:
      here = row.get("last_done_pos", -1)
   if here is None or here < 0:
      return None
   if here in (row.get("done_positions") or ()) and here + 1 < len(route):
      return lbl(here + 1)
   return lbl(here)


def step_completion_dates(route, opmap, done):
   """{position: (datetime, closed_by_x)} for every finished step.

   A step closed by an X was finished on the day of that X (the latest
   non-setup production booking carrying it).  A step behind the part with
   no X is taken as finished on the day of its last production entry -
   the part moved on without the X being booked.  A step with no
   production entry at all has no date.
   """
   out = {}
   for i in done:
       rec = opmap.get(route_step(route[i]))
       if not rec:
           continue
       if rec.get("done_x") and rec.get("x_dt"):
           out[i] = (rec["x_dt"], True)
       elif _uretim_dt(rec):
           out[i] = (_uretim_dt(rec), False)
   return out


class WFAnalyzer:
   """All waterfall calculations, independent of the UI."""

   def __init__(self, db):
       self.db = db
       self.d_from = None
       self.d_to = None
       self.add_rework_to_lead = False
       self.exclude_clean = False
       self.dedupe_ops = False
       self.exclude_serial_keys = frozenset()
       self.only_serial_keys = None
       self._cache = {}

   # 2.1.1: the setters no longer empty the cache - every one of these
   # settings is part of analyze()'s cache key, so a filter switched off
   # and on again (or a date range picked and dropped) comes back from
   # memory, and the Forecast work prepared in the background survives a
   # visit to Waterfall.  The cache is kept to CACHE_MAX entries.
   CACHE_MAX = 1500

   def set_exclude_serial_keys(self, keys):
       keys = frozenset(keys or ())
       if keys != self.exclude_serial_keys:
           self.exclude_serial_keys = keys

   def set_only_serial_keys(self, keys):
       """None = no restriction; otherwise ONLY these serials are kept -
       the opposite of exclude_serial_keys, for a "just show me the
       TECO/SCRAP parts" look."""
       keys = frozenset(keys) if keys else None
       if keys != self.only_serial_keys:
           self.only_serial_keys = keys

   def set_date_range(self, d_from, d_to):
       self.d_from, self.d_to = d_from, d_to

   def set_add_rework(self, flag):
       self.add_rework_to_lead = bool(flag)

   def set_exclude_clean(self, flag):
       self.exclude_clean = bool(flag)

   def set_dedupe_ops(self, flag):
       self.dedupe_ops = bool(flag)

   def analyze_all_time(self, part_key):
       """analyze() ignoring whatever date range is currently selected -
       WIP status is about the shop floor's current state, not about one
       narrow time window."""
       saved = (self.d_from, self.d_to)
       self.d_from, self.d_to = None, None
       try:
           return self.analyze(part_key)
       finally:
           self.d_from, self.d_to = saved

   def parts(self, merge=False):
       return self.db.wf_parts(merge)

   def cutoff(self):
       """End of the selected range, or 'now' when no end date is set."""
       if self.d_to:
           return datetime.datetime.combine(self.d_to, datetime.time.max)
       return datetime.datetime.now()

   def _window(self):
       start = (datetime.datetime.combine(self.d_from, datetime.time.min)
                if self.d_from else None)
       end = (datetime.datetime.combine(self.d_to, datetime.time.max)
              if self.d_to else None)
       return start, end

   def analyze(self, part_key):
       ver = self._data_ver()
       cache_key = (part_key, self.d_from, self.d_to, self.add_rework_to_lead,
                    self.exclude_clean, self.dedupe_ops, self.exclude_serial_keys,
                    self.only_serial_keys, ver)
       hit = self._cache.get(cache_key)
       if hit is not None:
           return hit

       route = wf_filter_duplicate_ops(
           wf_filter_clean(self.db.wf_traveler_for(part_key), self.exclude_clean),
           self.dedupe_ops)
       result = {"part_key": part_key, "route": route, "rows": [],
                 "averages": {}, "lead_defs": [], "total_avg_hours": None,
                 "total_avg_n": 0, "total_avg_warning": None,
                 "n_serials": 0, "n_completed": 0, "n_inprogress": 0,
                 "n_rework": 0, "rework_hours": 0.0, "fty": None}
       if not route:
           self._cache_put(cache_key, result)
           return result

       cutoff = self.cutoff()
       start, end = self._window()
       sold = self.db.sold_serial_keys()
       # One fetch of the whole part's data, grouped by serial in memory.
       by_serial = {}
       for r in self.db.wf_part_data_for(part_key):
           if r["serial_key"] in self.exclude_serial_keys:
               continue
           if self.only_serial_keys is not None and r["serial_key"] not in self.only_serial_keys:
               continue
           by_serial.setdefault(r["serial_key"], []).append(r)

       rows = []
       for sk, all_recs in by_serial.items():
           if not (start is None and end is None):
               if not any((start is None or rc["dt"] >= start) and
                          (end is None or rc["dt"] <= end) for rc in all_recs):
                   continue    # not eligible: no timestamp inside the window
           hist = all_recs if end is None else [r for r in all_recs if r["dt"] <= end]
           opmap, serial_disp, last_move = {}, sk, None
           op_kayit = {}
           for r in hist:
               if r["dt"] is None or r["dt"] > cutoff:
                   continue
               # Keyed by STEP - 280/1 and 280/3 are different work.
               opmap[r.get("step") or step_key(r["opac"], r.get("seq"))] = r
               # AND by operation, for the fallback below.
               k_op = op_key(r["opac"])
               onceki_r = op_kayit.get(k_op)
               if onceki_r is None or r["dt"] > onceki_r["dt"]:
                   op_kayit[k_op] = r
               serial_disp = r["serial"]
               if last_move is None or r["dt"] > last_move:
                   last_move = r["dt"]
           last_op = max((r["opac"] for r in opmap.values()), default=None)
           # WHERE IT IS AND WHAT IS BEHIND IT - the one rule every page
           # reads (op_progress_for_route): furthest production entry,
           # closed only by a non-setup X, everything before it finished.
           done_positions, running_pos, prog, here_pos = op_progress_for_route(
               route, opmap, cutoff)
           # FINISHED WITH THE ROUTE = its LAST step closed by an X.  It
           # used to need a booking on every step as well, which a part
           # that passed an unbooked paper station could never have.
           all_done = (bool(route) and here_pos == len(route) - 1
                       and here_pos in done_positions)
           last_done_pos = max(done_positions) if done_positions else -1
           # Finishing every traveler operation is not the same as being
           # gone from the floor - a part can sit complete in finished
           # goods for a while before it actually ships, and the shipment
           # report is the only thing that says it really has.
           status = ("Sold" if all_done and sk in sold else
                     "Awaiting Shipment" if all_done else
                     "In Progress" if opmap else "Not Started")
           has_rework = any(rec["rework"] > 0 for rec in opmap.values())
           waiting_hours = None
           if opmap and not all_done:
               # Waiting is measured from the last booking the part
               # actually carries.  While it is still being cut that is the
               # running step's own booking, so a part on a machine right
               # now does not accumulate "waiting" days it is not waiting -
               # if the work really has stopped, the gap grows and it shows
               # up in the WAITING LONGER THAN filter on its own.
               # Measured from where the part actually is, not from the
               # last step that happened to be booked - on a route with
               # unbooked steps those are not the same position.
               ref = here_pos if here_pos is not None else last_done_pos
               if ref >= 0:
                   rec_ref = opmap.get(route_step(route[ref]))
                   last_dt = rec_ref["dt"] if rec_ref else last_move
                   waiting_hours = (cutoff - last_dt).total_seconds() / 3600.0
           run = prog.get(running_pos) if running_pos is not None else None
           rows.append({
               "serial_key": sk, "serial": serial_disp, "opmap": opmap,
               "last_op": last_op, "last_done_pos": last_done_pos,
               "all_done": all_done, "status": status, "has_rework": has_rework,
               "waiting_hours": waiting_hours, "last_move_dt": last_move,
               "cell_lead": {},
               # Where the part actually is while it is still being cut,
               # and how far through that step it has got.
               "running_pos": running_pos,
               "running_op": route[running_pos]["op"] if running_pos is not None else None,
               # Where the part IS, cut or not - what the matrix measures
               # from.  See op_progress_for_route.
               "here_pos": here_pos,
               "here_op": route[here_pos]["op"] if here_pos is not None else None,
               # WHICH LINES ARE BEHIND THE PART.  With sequences this is
               # the only honest answer to "is 280 done": 280/3 can be
               # finished while 280/1 is not.
               "done_positions": sorted(done_positions),
               # {position: (datetime, closed_by_x)} - the day each finished
               # step was finished (step_completion_dates).
               "done_dates": step_completion_dates(route, opmap,
                                                   done_positions),
               "here_step": (route_step(route[here_pos])
                             if here_pos is not None else None),
               "here_label": (route[here_pos].get("label")
                              if here_pos is not None else None),
               "here": prog.get(here_pos) if here_pos is not None else None,
               "progress": prog, "running": run,
           })

       # FURTHEST ALONG FIRST (v67).  By where the part is on ITS ROUTE -
       # the same position the matrix draws (op_progress_for_route) - not
       # by the highest operation number it ever touched: a route is not
       # always in number order, and a rework booking on a high number
       # used to lift a part above others that were really further on.
       # A part done with the whole route is furthest of all.  Equal
       # positions: the one that moved most recently first.
       n_route = len(route)

       def _konum(r):
           if r["all_done"]:
               return n_route
           p = r.get("here_pos")
           return p if p is not None else r["last_done_pos"]
       rows.sort(key=lambda r: (
           -_konum(r),
           -(r["last_move_dt"].timestamp() if r["last_move_dt"] else 0.0)))

       lead_defs = wf_build_lead_defs(route)
       averages = self.calculate_interval_averages(lead_defs, route, rows)
       # TOTAL LEAD TIME is now measured end to end - the last entry on
       # the last operation minus the first entry on the first one - and
       # REMAINING is worked out from it.  See whole_route_lead.
       route_total, route_n, route_curve = self.whole_route_lead(rows, route)
       self.calculate_remaining_times(rows, averages, route_total,
                                      route_curve, route)
       # LEAD TIME WHERE THE TRAVELER SAYS NOTHING (v67): an operation no
       # lead-time definition covers is measured from the operation just
       # before it, so every step shows a figure.  Display only - the
       # totals and REMAINING above keep using the traveler's own spans.
       auto_defs = wf_auto_lead_defs(route, lead_defs)
       shown_averages = dict(averages)
       if auto_defs:
           shown_averages.update(
               self.calculate_interval_averages(auto_defs, route, rows))
       if route_total is not None:
           total_hours, total_n, warn = route_total, route_n, None
       else:
           # Nothing has been through the whole route yet.  The old
           # step-by-step sum is all there is, and it is better than a
           # blank.
           total_hours, total_n, warn = \
               self.calculate_total_average_wait(averages)

       n_serials = len(rows)
       n_completed = sum(1 for r in rows if r["all_done"])
       n_rework = sum(1 for r in rows if r["has_rework"])
       rework_hours = sum(rec["rework"] for r in rows for rec in r["opmap"].values())
       fty = ((n_serials - n_rework) / n_serials * 100.0) if n_serials else None

       result.update({
           "rows": rows, "averages": shown_averages, "lead_defs": lead_defs,
           "lead_defs_auto": auto_defs,
           "total_avg_hours": total_hours, "total_avg_n": total_n,
           "total_avg_warning": warn, "n_serials": n_serials,
           "lead_total": route_total, "lead_total_n": route_n,
           "lead_curve": route_curve,
           "n_completed": n_completed, "n_inprogress": n_serials - n_completed,
           "n_rework": n_rework, "rework_hours": rework_hours, "fty": fty,
       })
       self._cache_put(cache_key, result)
       return result

   def _data_ver(self):
       """The data version for the cache key - read from the file at most
       once a second (a network round trip per part would add up on the
       Forecast page), and at once after invalidate_caches()."""
       now = time.monotonic()
       memo = self.__dict__.get("_ver_memo")
       if memo and memo[0] == _CACHE_GEN[0] and now - memo[1] < 1.0:
           return memo[2]
       try:
           ver = self.db.data_version()
       except Exception:
           ver = None
       self._ver_memo = (_CACHE_GEN[0], now, ver)
       return ver

   def _cache_put(self, key, value):
       c = self._cache
       c[key] = value
       # oldest first out; the background preparation shares this dict,
       # so a key the other thread has just taken is simply skipped
       while len(c) > self.CACHE_MAX:
           try:
               del c[next(iter(c))]
           except (KeyError, StopIteration, RuntimeError):
               break

   def calculate_interval_averages(self, defs, route, rows):
       """2.1.2: each span runs from the start operation's COMPLETION (its
       X, else its last entry; an unbooked start hands over to the nearest
       earlier booked step) to the end operation's X, in clock hours - see
       lead_gap_hours.  No X on the end operation: no figure yet.  Negative
       and far-off gaps (lead_keep_mask) are marked invalid and left out of
       the average."""
       averages = {}
       # Each row's stamps worked out once per route, not once per span:
       # the X of every step, and the completion of the nearest booked step
       # at or before each position.
       steps = [route_step(c) for c in route]
       stamps = []
       for row in rows:
           opmap = row["opmap"]
           recs = [opmap.get(st) for st in steps]
           xs = [lead_x_dt(r) for r in recs]
           done, last = [], None
           for r in recs:
               if r:
                   last = lead_done_dt(r)
               done.append(last)
           stamps.append((recs, xs, done))
           # whole_route_lead reads the same stamps
           row["_lead_xs"], row["_lead_done0"] = xs, (lead_done_dt(recs[0]) if recs else None)
       for d in defs:
           ep, sp = d["end_pos"], d["start_pos"]
           gaps = []
           for recs, xs, done in stamps:
               end, start = xs[ep], done[sp]
               if end is None or start is None:
                   gaps.append(None)
                   continue
               hrs = (end - start).total_seconds() / 3600.0
               if self.add_rework_to_lead:
                   hrs += recs[ep]["rework"]
               gaps.append(hrs)
           keep = lead_keep_mask(gaps)
           used_hours = []
           for row, hrs, ok in zip(rows, gaps, keep):
               if hrs is None:
                   cell = {"state": "incomplete", "hours": None}
               elif not ok:
                   cell = {"state": "invalid", "hours": None}
               else:
                   cell = {"state": "done", "hours": hrs}
                   used_hours.append(hrs)
               row["cell_lead"][d["end_pos"]] = cell
           avg = (sum(used_hours) / len(used_hours)) if used_hours else None
           averages[d["end_pos"]] = {
               "avg_hours": avg, "n_used": len(used_hours),
               "n_excluded": len(rows) - len(used_hours), "def": d,
           }
       return averages

   def calculate_total_average_wait(self, averages):
       used_ranges, total, n, overlap = [], 0.0, 0, False
       for end_pos in sorted(averages):
           info = averages[end_pos]
           if info["avg_hours"] is None:
               continue
           d = info["def"]
           rng = (d["start_pos"] + 1, d["end_pos"])
           if any(_wf_overlaps(rng, ur) for ur in used_ranges):
               overlap = True
               continue
           used_ranges.append(rng)
           total += info["avg_hours"]
           n += 1
       if n == 0:
           return None, 0, None
       warn = ("Some interval definitions overlap - only the first of each "
               "overlapping group is included in this total.") if overlap else None
       return total, n, warn

   def whole_route_lead(self, rows, route):
       """TOTAL LEAD TIME, measured end to end (2.1.2: from completions).

       One serial's lead time is:

           the X of the LAST operation of the route
           minus
           the completion of the FIRST operation - its X, or its last
           entry when it was passed without one

       in plain clock hours.  Negative and far-off figures are left out
       (lead_keep_mask).

       WHY END TO END.  Lead time used to be added up from the traveler's
       predecessor links, which needs both ends of every interval booked on
       the same serial - most intervals then had no average and REMAINING
       said "Insufficient data" for almost every part.

       Returns:
           total   average whole-route hours (None when nothing finished)
           n       how many serials that average is built from
           curve   {position: average hours from the start until that
                    step was closed by its X}
       """
       if not route:
           return None, 0, {}
       first_step, last_step = route_step(route[0]), route_step(route[-1])
       totals, total_rows = [], []
       reached = {}                      # position -> [hours from start]
       steps = [route_step(c) for c in route]
       for row in rows:
           opmap = row["opmap"]
           xs = row.get("_lead_xs")
           if xs is None or len(xs) != len(route):
               xs = [lead_x_dt(opmap.get(st)) for st in steps]
               start = lead_done_dt(opmap.get(first_step))
           else:
               start = row.get("_lead_done0")
           if start is None:
               continue
           row["lead_start"] = start
           # How long this serial took to CLOSE each step it has closed.
           # This is what turns one whole-route average into an answer for
           # a part standing half way along it.
           for pos, st in enumerate(xs):
               if st is not None:
                   reached.setdefault(pos, []).append(
                       (st - start).total_seconds() / 3600.0)
           end = xs[-1]
           if end is not None:
               totals.append((end - start).total_seconds() / 3600.0)
               total_rows.append(row)
       curve = {}
       for pos, v in reached.items():
           v = [h for h, ok in zip(v, lead_keep_mask(v)) if ok]
           if v:
               curve[pos] = sum(v) / len(v)
       kept = []
       for row, hrs, ok in zip(total_rows, totals, lead_keep_mask(totals)):
           if ok:
               kept.append(hrs)
               row["lead_total"] = hrs
       total = (sum(kept) / len(kept)) if kept else None
       return total, len(kept), curve

   def calculate_remaining_times(self, rows, averages, total=None,
                                 curve=None, route=None):
       """How much longer each part still has.

       From the whole-route lead time and the curve of "how long parts
       take to REACH this step":

           remaining = average whole-route lead  -  average time to reach
                       the step this part is standing on

       which is the same subtraction the total itself is, done one step
       in.  A step nobody has reached takes the nearest earlier step that
       somebody has, so a gap in the data costs accuracy rather than the
       whole answer - which is what "Insufficient data" cost before.
       """
       curve = curve or {}
       for row in rows:
           if row["all_done"]:
               row["remaining"] = ("done", None)
               continue
           if total is not None:
               # 2.1.2: the curve is "time until this step was CLOSED", so
               # the part is as far along as the last step behind it - the
               # one it stands on if that is closed by its X (v62: it is
               # then waiting at the next), else the one before.
               pos = row.get("here_pos")
               if pos is None:
                   pos = row.get("last_done_pos", -1)
                   pos = -1 if pos is None else pos
               elif pos not in (row.get("done_positions") or ()):
                   pos -= 1
               gecen = curve.get(pos)
               if gecen is None:
                   onceki = [p for p in curve if p <= pos]
                   gecen = curve[max(onceki)] if onceki else 0.0
               row["remaining"] = ("value", max(0.0, total - gecen))
               continue
           # REMAINING is a lead-time figure and nothing else: how long
           # this part has historically taken to get from where it stands
           # to the end of the route.  The standard time of the step it is
           # on is a different measure - work content, not elapsed time -
           # and mixing the two gave a number that was neither.
           #
           # The starting point is where the part actually stands, so a
           # step already under way is not charged again.
           run = row.get("running")
           from_pos = (row["running_pos"] if run is not None
                       else row["last_done_pos"])
           total, included = 0.0, 0
           cands = sorted((ep, info) for ep, info in averages.items()
                          if ep > from_pos and info["avg_hours"] is not None)
           used_ranges = []
           for ep, info in cands:
               d = info["def"]
               rng = (d["start_pos"] + 1, d["end_pos"])
               if any(_wf_overlaps(rng, ur) for ur in used_ranges):
                   continue
               used_ranges.append(rng)
               total += info["avg_hours"]
               included += 1
           row["remaining"] = (("value", total) if included else
                               ("insufficient", None))


def wf_predicted_view(result, db, part_key, exclude_clean=False,
                      dedupe_ops=False, cutoff=None, flag_map=None):
   """
   Alternate reading of an analyze() result: a WIP serial's position is
   normally the last operation carrying a timestamp.  Here it is advanced
   one station at a time for as long as the calendar time since the
   serial was last touched at all still covers how long that specific
   step (this position to the very next one) has historically taken.

   The per-step averages are the LEAD TIME page's own figures
   (`lead_time_analysis`, always learned across the whole history, not
   just whatever date range Waterfall currently has selected) - not the
   traveler's own lead-time groupings (`averages`/`lead_defs`), which can
   merge several positions under one predecessor-based average spanning
   more than one step and would misjudge exactly the serials this is
   meant to help with.  `exclude_clean`/`dedupe_ops` should match
   whatever Waterfall currently has set, so the route this walks is the
   very same one `result["route"]` already is.

   The elapsed time is measured from the serial's last touch of ANY
   kind, including an operation that clean/dedupe filtering itself
   drops from the route (a cleaning step logged five minutes ago is
   still proof the serial was just handled, even though it does not
   count as an advance in position) - using only the last *counted*
   position's timestamp would ignore that most recent evidence and
   could credit the serial with days it has not actually had.
   """
   route = result["route"]
   if not route:
       return result
   cutoff = cutoff or datetime.datetime.now()
   lt = lead_time_analysis(db, part_key, None, None,
                           exclude_clean=exclude_clean, dedupe_ops=dedupe_ops)
   if not lt:
       return result
   step_avg = {i: s["avg_hours"] for i, s in enumerate(lt["steps"])
              if s["avg_hours"] is not None}

   flag_map = flag_map or {}
   new_rows = []
   for row in result["rows"]:
       r = dict(row)
       r["predicted_from_pos"] = None
       # An MRB or SCRAP serial is no longer moving through the shop in
       # the normal sense - projecting it forward as if it will keep
       # advancing would be misleading, so it just keeps its real,
       # last-known position.
       # Where the part stands (v62: the X-based position); a part still
       # on its first step has no finished step yet but is on the route.
       nerede = (row["here_pos"] if row.get("here_pos") is not None
                 else row["last_done_pos"])
       last_dt = row.get("last_move_dt")
       if last_dt is None and nerede is not None and nerede >= 0:
           last_dt = (row["opmap"].get(route_step(route[nerede])) or {}).get("dt")
       if (not row["all_done"] and nerede is not None and nerede >= 0
               and last_dt is not None
               and flag_map.get(row["serial_key"]) not in ("mrb", "mrbent",
                                                           "scrap")):
           budget = (cutoff - last_dt).total_seconds() / 3600.0
           # PAST TWENTY DAYS THIS IS NOT A PREDICTION.  A part nobody has
           # booked anything against for three weeks is not "probably a
           # few stations further along" - it is stuck, lost, or sitting
           # in a corner, and walking it forward on historical averages
           # would march it to the end of the route on nothing but
           # elapsed time.  The same cap applies on the floor plan.
           if budget > PREDICT_MAX_DAYS * 24.0:
               budget = 0.0
           # This is the ONE place the clock is allowed to move a part.
           # Everywhere else a part sits where the bookings put it; here it
           # is projected forward by how long each step has historically
           # taken, spending the time gone by since it was last touched.
           #
           # The walk starts from where the part actually stands, which for
           # a part still being cut is the operation it is on - projecting
           # from the step before would spend that step's lead time twice.
           start_pos = pos = nerede
           # STEP BY STEP (v62): sequences are ordered steps like any
           # other, so the part is projected one traveler line at a time,
           # spending each line-to-line step's historical lead time.
           while pos + 1 < len(route):
               avg = step_avg.get(pos)
               if avg is None or budget < avg:
                   break
               budget -= avg
               pos += 1
           if pos != start_pos:
               r["predicted_from_pos"] = start_pos
               r["last_done_pos"] = pos
               r["last_op"] = route[pos]["op"]
               # The step it was being cut on is projected finished, so it
               # is no longer the running one - otherwise the matrix would
               # paint the part both moved on and still on the machine.
               r["running_pos"] = r["running_op"] = r["running"] = None
               # The projection moved it, so that is where it now is.
               r["here_pos"], r["here"] = pos, None
               r["here_op"] = route[pos]["op"]
               r["all_done"] = (pos == len(route) - 1)
               # A prediction reaching the end of the route is a forecast,
               # not an actual shipment - it keeps whatever sold/not-sold
               # status it already had (predicting forward never turns an
               # unsold serial into a sold one).
               if r["all_done"] and r["status"] != "Sold":
                   r["status"] = "Awaiting Shipment"
               elif not r["all_done"]:
                   r["status"] = "In Progress"
       new_rows.append(r)
   # Still furthest along first (v67) - now by the PREDICTED position, the
   # one the matrix draws, so the order matches what is on screen.
   n_route = len(route)

   def _konum(r):
       if r.get("all_done"):
           return n_route
       for k in ("predicted_pos", "here_pos"):
           if r.get(k) is not None:
               return r[k]
       return r.get("last_done_pos", -1)
   new_rows.sort(key=lambda r: (
       -_konum(r),
       -(r["last_move_dt"].timestamp() if r.get("last_move_dt") else 0.0)))
   return dict(result, rows=new_rows)



# ===========================================================================
#  FORECAST  -  how busy each machine group is about to be
# ===========================================================================
#
# Every other page in this program looks backwards: what happened, where
# things are now.  This one looks forwards, and it is built from two
# things the app already knows and nothing new:
#
#   WHAT IS COMING   Every serial still on the floor has a route, and
#                    every step of that route it has not finished yet is
#                    work that some machine group is going to have to do.
#                    The step's standard time is how many hours that is.
#
#   WHEN IT ARRIVES  The lead-time curve (see WFAnalyzer.whole_route_lead)
#                    says how long parts historically take to get from
#                    where this one stands to each step ahead of it.  Add
#                    that to today and you have the day the work lands.
#
# Against that goes CAPACITY, which comes from the floor plan: how many
# machines are drawn in each group, times the shift length, times the
# shifts per day - on the days the shop actually works.  Sixteen machines
# in HNK at 7.5 hours over 3 shifts is 360 hours of HNK capacity a day.

def capacity_units_by_group(db, layout_name="B1000"):
   """{machine group: how many SHIFTS of capacity it has}.

   A machine is one shift's worth: one part on it at a time.  A station
   like MASK / DEMASK is a machine on the plan but its capacity is
   however many people are put on it - two people is two shifts of work
   done in one shift of time.  So each symbol contributes its `people`
   count when it has one, and 1 when it does not.

   Returns (units, machine count) so the page can show both: "6 machines,
   11 shifts of capacity" says something a single number cannot.
   """
   units, sayi, _bosluk = _capacity_scan(db, layout_name)
   return units, sayi


def capacity_of_items(items, comp):
   """The same grouping rules, applied to a plan already in memory.

   The B1000 page has the drawing in front of it; making it ask the
   database for what it is already holding would put two more queries on
   every LIVE refresh, and on a shared drive those are not free.
   """
   return _group_machines(items, comp)


def _capacity_scan(db, layout_name="B1000"):
   """(units, machine count, gaps) - the one pass both callers share.

   HOW A DRAWN MACHINE FINDS ITS GROUP, in order:
     1. the group typed on the symbol itself (MACHINE GROUP), if any
     2. the group COMP gives its work centre code
     3. failing that, THE WORK CENTRE CODE ITSELF.

   Step 3 is new and it matters: a code COMP has not been told about is
   still a real machine standing on the floor, and a traveler that names
   that same code as its work centre group now finds it.  Before, the
   machine was dropped without a word.
   """
   lay = db.layout_active(layout_name)
   if not lay:
       return {}, {}, {"no_wc": [], "unknown_wc": {}}
   return _group_machines(db.layout_items(lay["id"]), db.comp_map())


def _group_machines(items, comp):
   units, sayi = {}, {}
   bosluk = {"no_wc": [], "unknown_wc": {}}
   for it in items or ():
       if it["kind"] != "machine":
           continue
       etiket = (it.get("label") or "").strip() or "(unnamed)"
       wc = (it.get("wc_code") or "").strip()
       # layout_item.groups is stored as a comma separated string; the
       # B1000 page splits it when it loads the plan, but here the rows
       # come straight out of the table.
       ham = it.get("groups") or ""
       if isinstance(ham, str):
           ham = ham.split(",")
       elle = [str(g).strip() for g in ham if str(g).strip()]
       if elle:
           grp = norm_text(elle[0])
       elif not wc:
           # Nothing to go on: no code, no typed group.  A machine like
           # this can never be matched to a route step, so counting it
           # would only invent capacity - it is reported instead.
           bosluk["no_wc"].append(etiket)
           continue
       else:
           grp = norm_text((comp.get(wc) or ("", UNKNOWN))[1])
           if not grp or grp == norm_text(UNKNOWN):
               grp = norm_text(wc)
               bosluk["unknown_wc"].setdefault(wc, []).append(etiket)
       if not grp:
           bosluk["no_wc"].append(etiket)
           continue
       kisi = 0
       try:
           kisi = int(it.get("people") or 0)
       except Exception:
           kisi = 0
       units[grp] = units.get(grp, 0.0) + (kisi if kisi > 0 else 1)
       sayi[grp] = sayi.get(grp, 0) + 1
   return units, sayi, bosluk


def manual_capacity(db):
   """{group: (machines, people)} typed in on the FORECAST page.

   The floor plan is the truth about what is DRAWN, and that is the right
   default.  But a forecast is a question about a period that has not
   happened yet - "if I put four more people on MASK next month, what
   happens" - and you cannot ask that by redrawing the floor.  So the
   numbers can be typed instead, and they are kept here rather than in
   one person's settings because the answer is a shop answer, not a
   personal one.
   """
   raw = db.get_meta("forecast_manual_capacity", None)
   if not isinstance(raw, dict):
       return {}
   out = {}
   for g, v in raw.items():
       try:
           mak, kisi = int(v[0]), int(v[1])
       except Exception:
           continue
       out[norm_text(g)] = (max(0, mak), max(0, kisi))
   return out


def set_manual_capacity(db, values):
   """values: {group: (machines, people)}"""
   db.set_meta("forecast_manual_capacity",
               {norm_text(g): [int(m), int(k)]
                for g, (m, k) in (values or {}).items()})


def capacity_from(db, source="plan", layout_name="B1000"):
   """(units, machines, people) per group, from the plan or from the page.

   A unit is one shift of work: one machine, or one person on a station
   whose output is people.  Where a group has both - machines drawn AND
   people typed - the PEOPLE win, because that is what a person typing
   them is saying.
   """
   birim, makine = capacity_units_by_group(db, layout_name)
   if source != "manual":
       kisi = {g: int(birim.get(g, 0)) for g in birim}
       return birim, makine, kisi
   elle = manual_capacity(db)
   if not elle:
       kisi = {g: int(birim.get(g, 0)) for g in birim}
       return birim, makine, kisi
   units, mak, kis = {}, {}, {}
   for g in set(elle) | set(birim):
       if g in elle:
           m, k = elle[g]
       else:
           m, k = makine.get(g, 0), int(birim.get(g, 0))
       mak[g] = m
       kis[g] = k
       units[g] = float(k if k > 0 else m)
   return units, mak, kis


def group_capacity_per_day(db, layout_name="B1000", source="plan", day=None):
   """{machine group: hours of capacity on a working day}.

   One unit of capacity is one machine or one person on a station whose
   output is people.  How many HOURS that unit is worth depends on the
   DAY: a Saturday running one shift is not a Tuesday running three, and
   the timetable is what says so.  Without a date this answers for an
   average working day, which is what a summary line wants.
   """
   bir = day_shift_hours(day.weekday()) if day is not None \
       else shop_hours_per_day()
   units, _mak, _kisi = capacity_from(db, source, layout_name)
   return {g: n * bir for g, n in units.items()}


def is_working_day(day, week_start=None):
   """Does the shop work on this date?

   The days themselves are ticked in SHOP RULES, so this is a lookup and
   not a guess.  `week_start` is accepted and ignored - it used to decide
   which days a count of six meant, which is exactly the guess that got
   it wrong for a shop closed on a Friday.
   """
   # A day the shop works is a day with a shift on it - the timetable is
   # the one place that is written down, so nothing can disagree with it.
   if SHIFT_SCHEDULE:
       return bool(SHIFT_SCHEDULE.get(day.weekday()))
   return day.weekday() in (WORK_WEEKDAYS or (0, 1, 2, 3, 4, 5, 6))


def _forecast_jobs(db, part_keys=None, exclude_sold=True, analyzer=None,
                   serial_keys=None, exclude_serial_keys=None,
                   max_wait_days=None, merge=False):
   """Every piece of work still to be done, as a flat list.

   One entry per (serial, remaining route step):
       serial, part, position, group, hours, order along the route

   This is what both forecasts are built from; they differ only in WHEN
   they decide each piece lands.

   max_wait_days LEAVES OUT THE PARTS THAT HAVE STOPPED.  A serial whose
   last booking is older than this has not moved in weeks; it is almost
   never about to arrive at a machine, but its whole remaining route is
   still counted as work coming, and a few dozen of them are enough to
   make every group look drowned.  Measured exactly as the WAITING
   LONGER THAN filter measures it: hours since the booking the part
   actually carries at the operation it is sitting on.
   """
   analyzer = analyzer or WFAnalyzer(db)
   sold = db.sold_serial_keys() if exclude_sold else set()
   bekleme_siniri = (max_wait_days * 24.0) if max_wait_days else None
   birakilan = 0
   jobs, curves, serials, no_curve = [], {}, 0, []
   # merge=True: a merged part is one part here - one lead curve over all
   # its members' serials, one row on the BY PART view.
   for part_key, _disp in db.wf_parts(merge):
       if part_keys and part_key not in part_keys:
           continue
       res = analyzer.analyze_all_time(part_key)
       route = res.get("route") or []
       if not route:
           continue
       curve = res.get("lead_curve") or {}
       curves[part_key] = curve
       if not curve:
           no_curve.append(part_key)
       for row in res.get("rows") or []:
           sk = row["serial_key"]
           if row.get("all_done") or sk in sold:
               continue
           if serial_keys and sk not in serial_keys:
               continue
           if exclude_serial_keys and sk in exclude_serial_keys:
               continue
           if bekleme_siniri is not None:
               bekledi = row.get("waiting_hours")
               if bekledi is not None and bekledi > bekleme_siniri:
                   birakilan += 1
                   continue
           here = row.get("here_pos")
           if here is None:
               here = row.get("last_done_pos", -1)
           here = -1 if here is None else here
           serials += 1
           adim = 0
           for pos in range(max(here, 0), len(route)):
               col = route[pos]
               grp = norm_text(col.get("wc_group") or "")
               std = float(col.get("std_hour") or 0.0)
               if not grp or std <= 0:
                   continue
               if pos == here:
                   # CLOSED BY ITS X (v62): nothing is left on it, however
                   # few hours were booked - the part is waiting for the
                   # next step.  Otherwise what is left is the standard
                   # less the progress booked so far.
                   if pos in set(row.get("done_positions") or ()):
                       continue
                   rec = row["opmap"].get(route_step(col))
                   yapilan = float((rec or {}).get("labour") or 0.0)
                   kalan = max(0.0, std - yapilan)
               else:
                   kalan = std
               if kalan <= 0:
                   continue
               jobs.append({"serial": row["serial_key"], "part": part_key,
                            "pos": pos, "here": here, "group": grp,
                            "hours": kalan, "step": adim})
               adim += 1
   return jobs, curves, serials, no_curve, birakilan


def _day_list(d1, d2):
   days, d = [], d1
   while d <= d2:
       days.append(d)
       d += datetime.timedelta(days=1)
   return days


# forecast_by_lead KALDIRILDI (v50).
#
# Lead suresi, parcalarin o adima TARIHSEL OLARAK ne zaman ulastigini
# soyler - yani atolyenin kuyrugunu ZATEN ICINDE TASIR.  Sonra onu bir
# kez daha kapasiteye karsi kuyruga sokmak ayni kuyrugu iki kez saymak
# oluyordu: olculen, 15.530 saatlik is icin 196.027 saat devir.  Sayi
# okunabilir degildi ve yaniltiyordu, o yuzden yontem kaldirildi.
# Geriye iki durust okuma kaldi: STANDARD ve MACHINE.


def forecast_by_standard(db, days, jobs, capacity):
   """WHEN each piece lands if the shop simply works through it.

   The other reading, and the one that answers "what if nothing else goes
   wrong": every operation takes its standard time, a machine does one
   part at a time, and a part cannot start its next operation until that
   group has room for it.

   So this walks the calendar forward filling each group's days up to
   their capacity.  A part that finishes at HNK on Tuesday joins the
   queue at the next group on Tuesday - and if that group is already full
   until Friday, it starts on Friday.  The waiting is not assumed, it
   comes out of the arithmetic.
   """
   d1, son = days[0], days[-1]
   # How much of each group's day is still free.
   free = {}
   for g, kap in capacity.items():
       for d in days:
           free[(g, d)] = kap if is_working_day(d) else 0.0

   def ilk_bos(grp, baslangic):
       for d in days:
           if d < baslangic:
               continue
           if free.get((grp, d), 0.0) > 1e-9:
               return d
       return None

   arrivals = {}
   parca_payi = {}                   # (grup, gun) -> {part_key: saat}
   tasan = [0.0]
   # A part that is further along is closer to leaving, so it gets the
   # machine first - the same order a shop works in.
   hazir = {}                        # serial -> the day it is free again
   for j in sorted(jobs, key=lambda x: (-x["here"], x["serial"], x["step"])):
       grp = j["group"]
       baslar = max(d1, hazir.get(j["serial"], d1))
       kalan = j["hours"]
       son_gun = baslar
       while kalan > 1e-9:
           gun = ilk_bos(grp, baslar)
           if gun is None:
               # Either this group has no capacity at all, or it is full
               # for the whole window.  The work does not fit, and piling
               # it onto the last day - which is what this used to do -
               # drew a mountain on the final column that people read as a
               # real peak.  It is left out and counted as overflow, which
               # the page says out loud.
               tasan[0] += kalan
               son_gun = son
               break
           yer = free[(grp, gun)]
           alinan = min(yer, kalan)
           free[(grp, gun)] = yer - alinan
           key = (grp, gun)
           arrivals[key] = arrivals.get(key, 0.0) + alinan
           # Hangi PARCANIN isi oldugu da tutuluyor: sayfa satirlari
           # tezgah grubu yerine parca grubuna cevirebilsin diye.
           pay = parca_payi.setdefault(key, {})
           pay[j["part"]] = pay.get(j["part"], 0.0) + alinan
           kalan -= alinan
           baslar = gun
           son_gun = gun
       # The part is free for its next operation the day after this one
       # finishes.
       hazir[j["serial"]] = son_gun + datetime.timedelta(days=1)
   return arrivals, tasan[0], parca_payi


def forecast_by_machine(db, days, jobs, units, per_machine_day):
   """TEZGAH TEZGAH: bir makine, bir parca, bitene kadar.

   Diger iki yontem isi bir GRUBUN gunluk saat havuzuna dagitir.  Saha
   oyle calismaz.  Bir tezgaha bir parca baglanir ve o parca bitmeden
   yenisi baglanamaz - o yuzden kagit uzerinde bos duran tezgah saatleri
   KULLANILAMAZ olabilir, ya da tersine bir parca gunun sonuna tasip
   ertesi gunun bastaki saatlerini de goturur.

   Kullanicinin ornegi, bire bir:
       70 operasyonundan inen parca 80'e baglanacak, 80 = 26 saat.
       Tezgah gunde 22.5 saat calisiyor.
       -> parca o tezgahin BUGUNKU 22.5 saatini ve YARINKI 3.5 saatini
          kaplar.  Yarinin kalan 19 saatine yeni bir parca baglanabilir.

   NASIL CALISIR
     - Her tezgah bir KAYNAKTIR: ne zaman bosalacagini tutan tek bir
       sayi.  Grupta kac tezgah varsa o kadar kaynak.
     - Zaman "atolye saati" ekseninde ilerler: calisilmayan gunler bu
       eksende hic yoktur, o yuzden hafta sonu bir isi uzatmaz, erteler.
     - Parcalar OLAY SIRASIYLA islenir: hangi parca once hazir olursa
       once tezgah bulur.  Bir parca bir adimi bitirmeden sonraki adimi
       icin sira beklemeye baslamaz.
     - SU AN BIR TEZGAHTA OLAN parca, o tezgahi bastan isgal eder:
       kalan suresi (standart - girilen) kadar.  Yani simulasyon bos bir
       atolyeden degil, SAHANIN SU ANKI HALINDEN baslar.

   NE OLCULUR, her (grup, gun) icin:
       busy      tezgahlarin gercekten kestigi saat
       idle      kapasite - busy: tezgah bos ama baglanacak parca yok
       queue     gun sonunda o grubu bekleyen is (saat) ve parca sayisi
       started   o gun tezgaha baglanan parca sayisi
       finished  o gun tezgahtan inen parca sayisi

   `idle` bu modun asil katkisidir: "kapasitem var ama kullanamiyorum"
   ilk kez burada gorunur.
   """
   import bisect
   import heapq
   # Calisilan gunler, sirayla.  ATOLYE SAATI EKSENI bunlarin uzerinde
   # kurulur - ama gunler ARTIK ESIT UZUNLUKTA DEGIL: uc vardiya donen
   # bir Sali 24 saat, tek vardiya donen bir Cumartesi 8 saattir.  O
   # yuzden eksen, gun uzunluklarinin kumulatif toplamidir:
   #   kesim[k] = k numarali gunun BASLADIGI atolye saati
   # Bir atolye saatinin hangi gune dustugu bisect ile bulunur.
   cgun = [d for d in days if is_working_day(d)]
   if not cgun:
       return {}, {}, [], 0.0, {}, {}
   gun_saat = [day_shift_hours(d.weekday()) for d in cgun]
   if per_machine_day and not any(gun_saat):
       gun_saat = [per_machine_day] * len(cgun)
   if not any(gun_saat):
       return {}, {}, [], 0.0, {}, {}
   kesim = [0.0]
   for h in gun_saat:
       kesim.append(kesim[-1] + h)
   ufuk = kesim[-1]                          # pencerenin sonu, atolye saati

   def gun_of(saat):
       """Hangi calisma gunune dusuyor - gunler esit uzunlukta degil."""
       i = bisect.bisect_right(kesim, saat) - 1
       return max(0, min(len(cgun) - 1, i))

   # Her grup icin tezgah kaynaklari.  Tezgahi olmayan grup simule
   # EDILMEZ - isi son gune yigmak yerine acikca soylenir.
   makine = {}
   kapasitesiz = set()
   for g in {j["group"] for j in jobs}:
       n = int(round(units.get(g, 0)))
       if n <= 0:
           kapasitesiz.add(g)
           continue
       makine[g] = [0.0] * n
       heapq.heapify(makine[g])

   # Her serinin adimlari, rota sirasinda
   adimlar = {}
   for j in jobs:
       if j["group"] in kapasitesiz:
           continue
       adimlar.setdefault(j["serial"], []).append(j)
   for sk in adimlar:
       adimlar[sk].sort(key=lambda x: x["pos"])

   # Olay kuyrugu: (hazir oldugu atolye saati, seri, kacinci adim)
   kuyruk = [(0.0, sk, 0) for sk in adimlar]
   heapq.heapify(kuyruk)

   araliklar = []          # (grup, baslangic, bitis, parca)
   bekleme = []            # (grup, hazir, baslangic, saat, parca, seri)
   tasan = 0.0            # pencereye sigmayan is
   while kuyruk:
       hazir, sk, i = heapq.heappop(kuyruk)
       if hazir >= ufuk:
           tasan += sum(x["hours"] for x in adimlar[sk][i:])
           continue
       job = adimlar[sk][i]
       g = job["group"]
       bos = heapq.heappop(makine[g])
       basla = max(bos, hazir)
       bit = basla + job["hours"]
       heapq.heappush(makine[g], bit)
       araliklar.append((g, basla, bit, job["part"]))
       bekleme.append((g, hazir, basla, job["hours"], job["part"], sk))
       if i + 1 < len(adimlar[sk]):
           heapq.heappush(kuyruk, (bit, sk, i + 1))

   # Atolye saati -> gun bazinda dagitim
   plan = {}

   def hucre(g, d):
       # busy_by / queue_by: ayni sayilarin PARCA PARCA dagilimi.  Sayfa
       # satirlari tezgah grubu yerine parca grubuna cevirebilsin diye
       # tutuluyor - "sirada bekleyenler hangi parca grubundan".
       return plan.setdefault((g, d), {
           "busy": 0.0, "idle": 0.0, "capacity": 0.0, "queue": 0.0,
           "queue_n": 0, "started": 0, "finished": 0, "arrived": 0.0,
           "busy_by": {}, "queue_by": {}, "queue_n_by": {}})

   def ekle(sozluk, anahtar, deger):
       sozluk[anahtar] = sozluk.get(anahtar, 0) + deger

   for g, a, b, parca in araliklar:
       k0 = gun_of(a)
       k1 = gun_of(b - 1e-9)
       for k in range(max(0, k0), min(k1, len(cgun) - 1) + 1):
           if k >= len(cgun):
               break
           gs, ge = kesim[k], kesim[k + 1]
           ortak = min(b, ge) - max(a, gs)
           if ortak > 0:
               c = hucre(g, cgun[k])
               c["busy"] += ortak
               ekle(c["busy_by"], parca, ortak)
       if 0 <= k0 < len(cgun):
           hucre(g, cgun[k0])["started"] += 1
       if 0 <= k1 < len(cgun):
           hucre(g, cgun[k1])["finished"] += 1
       if b > ufuk:
           tasan += b - ufuk

   # BEKLEYEN IS: hazir olmus ama henuz tezgaha baglanamamis.
   #
   # Makine modunda "calisan saat" kapasiteyi ASLA asamaz - bir tezgah
   # gununden fazlasini yapamaz.  O yuzden asiri yuk burada gorunur:
   # o gun yapilmayi bekleyen SAAT ve kac PARCA.  Kapasitenin ne kadar
   # asildigi = (calisan + bekleyen) / kapasite.
   # KAC PARCA BEKLIYOR - PARCA sayisi, is sayisi degil.
   #
   # Bu sayi eskiden bekleyen IS ARALIKLARINI sayiyordu.  Bir serinin
   # ayni gun iki adimi (biri bitip digeri siraya girerek) ayni grupta
   # bekleyebilir, ve ayni seri gun icinde iki ayri grupta gorunebilir -
   # ustelik ALL GROUPS satiri bunlari topluyordu.  Sonuc, sahadaki WIP
   # parca sayisindan BUYUK "bekleyen parca" rakamlariydi; boyle bir sey
   # mumkun degildir ve sayiyi okunamaz yapiyordu.
   #
   # Artik her (grup, gun) icin BIRBIRINDEN FARKLI SERILER sayiliyor, ve
   # gunun toplami da gruplarin toplami degil, gun icindeki farkli
   # serilerin BIRLESIMIDIR.
   kuyruk_seri = {}         # (grup, gun) -> {seri}
   kuyruk_seri_parca = {}   # (grup, gun, parca) -> {seri}
   gun_seri = {}            # gun -> {seri}
   gun_parca_seri = {}      # (parca, gun) -> {seri}
   for g, hazir, basla, saat, parca, seri in bekleme:
       k0 = gun_of(hazir)
       k1 = gun_of(basla)
       for k in range(max(0, k0), min(k1, len(cgun) - 1) + 1):
           if k >= len(cgun) or basla <= kesim[k + 1]:
               continue
           gun = cgun[k]
           c = hucre(g, gun)
           c["queue"] += saat
           ekle(c["queue_by"], parca, saat)
           kuyruk_seri.setdefault((g, gun), set()).add(seri)
           kuyruk_seri_parca.setdefault((g, gun, parca), set()).add(seri)
           gun_seri.setdefault(gun, set()).add(seri)
           gun_parca_seri.setdefault((parca, gun), set()).add(seri)

   for (g, gun), seriler in kuyruk_seri.items():
       hucre(g, gun)["queue_n"] = len(seriler)
   for (g, gun, parca), seriler in kuyruk_seri_parca.items():
       hucre(g, gun)["queue_n_by"][parca] = len(seriler)

   # Kapasite ve bosta kalan saat
   for g in makine:
       n = len(makine[g])
       for d in days:
           c = hucre(g, d)
           # The day's own length: a Saturday on one shift carries a
           # third of a Tuesday on three.
           c["capacity"] = (n * day_shift_hours(d.weekday())
                            if is_working_day(d) else 0.0)
           c["idle"] = max(0.0, c["capacity"] - c["busy"])
   gunluk = {d: len(s) for d, s in gun_seri.items()}
   parca_gunluk = {k: len(s) for k, s in gun_parca_seri.items()}
   return (plan, {g: len(m) for g, m in makine.items()}, sorted(kapasitesiz),
           tasan, gunluk, parca_gunluk)


def forecast_load(db, d1, d2, part_keys=None, exclude_sold=True,
                  analyzer=None, mode="machine", groups=None, source="plan",
                  serial_keys=None, exclude_serial_keys=None,
                  max_wait_days=None, merge=False):
   """Hours of work landing on each machine group, day by day - and what
   spills over when a day cannot absorb them.

   `mode` is "machine" (one machine, one part, until it is finished -
   the closer-to-reality reading) or "standard" (every step takes its
   standard time and the work is spread over the days that have room).

   CARRY-OVER is the point of the `waiting` figure.  Sixteen machines at
   360 hours a day, given 500 hours of work, do 360 of it - and the other
   140 is still there tomorrow, on top of whatever tomorrow brings.  The
   old table showed only what ARRIVED each day, so an overloaded group
   looked merely busy instead of falling further behind every day.

   Returns days, groups, and per (group, day):
       arrived   work reaching the group that day
       waiting   what it has to do that day  (arrived + yesterday's
                 leftover)
       done      what it can actually do     (capped by capacity)
       backlog   what is still waiting at the end of the day
   """
   today = datetime.date.today()
   d1 = d1 or today
   d2 = d2 or (d1 + datetime.timedelta(days=30))
   days = _day_list(d1, d2)
   capacity = group_capacity_per_day(db, source=source)
   units, machine_count, people_count = capacity_from(db, source)

   jobs, curves, serials, no_curve, durmus = _forecast_jobs(
       db, part_keys, exclude_sold, analyzer,
       serial_keys=serial_keys, exclude_serial_keys=exclude_serial_keys,
       max_wait_days=max_wait_days, merge=merge)
   # An average working day's worth of one machine - only for the summary
   # columns; the day by day figures use each day's own length.
   bir_makine = shop_hours_per_day()
   tasan, kapasitesiz = 0.0, []
   if mode == "machine":
       # TEZGAH TEZGAH: is bir havuza degil, TEK TEK MAKINELERE dagilir.
       # Sonucu dogrudan kendi plani olarak dondurur - devir aritmetigine
       # ihtiyaci yoktur, cunku bekleme zaten simulasyonun icinde olur.
       (mplan, mmak, kapasitesiz, tasan, kuyruk_gunluk,
        kuyruk_parca_gunluk) = forecast_by_machine(
           db, days, jobs, units, bir_makine)
       butun = sorted({g for g, _d in mplan})
       if groups:
           istenen = {norm_text(g) for g in groups}
           butun = [g for g in butun if g in istenen]
       plan = {}
       for g in butun:
           for d in days:
               c = mplan.get((g, d)) or {}
               kap = c.get("capacity", 0.0)
               calisan = c.get("busy", 0.0)
               kuyruk = c.get("queue", 0.0)
               plan[(g, d)] = {
                   "arrived": calisan, "waiting": calisan,
                   "done": calisan, "backlog": kuyruk,
                   "capacity": kap,
                   "busy": calisan, "idle": c.get("idle", 0.0),
                   # BIRIKENLE BERABER: o gun yapilan + o gun yapilamayip
                   # bekleyen.  Kapasiteyi asan tek olcu budur.
                   "with_queue": calisan + kuyruk,
                   "queue": kuyruk,
                   "queue_n": c.get("queue_n", 0),
                   "started": c.get("started", 0),
                   "finished": c.get("finished", 0),
                   # Ayni sayilarin parca parca dagilimi - sayfa
                   # satirlari PARCA GRUBUNA cevirebilsin diye.
                   "busy_by": c.get("busy_by") or {},
                   "queue_by": c.get("queue_by") or {},
                   "queue_n_by": c.get("queue_n_by") or {}}
       butun.sort(key=lambda g: (-sum(plan[(g, d)]["busy"] for d in days), g))
       return {"days": days, "groups": butun, "plan": plan,
               "capacity": {g: (mmak.get(g, 0) * bir_makine) for g in mmak},
               "machines": mmak, "units": units, "people": people_count,
               "source": source, "serials": serials, "no_curve": no_curve,
               "mode": mode, "overflow": tasan, "no_capacity": kapasitesiz,
               "dropped_idle": durmus, "max_wait_days": max_wait_days,
               # A serial waiting at two groups on the same day is ONE
               # part waiting, so the day's own total is a union and not
               # the sum of the group rows - see forecast_by_machine.
               "queue_n_day": kuyruk_gunluk,
               "queue_n_part_day": kuyruk_parca_gunluk,
               "all_groups": sorted(set(mmak) | set(capacity))}
   arrivals, tasan, parca_payi = forecast_by_standard(db, days, jobs, capacity)

   butun = sorted({g for g, _d in arrivals} | set(capacity))
   if groups:
       istenen = {norm_text(g) for g in groups}
       butun = [g for g in butun if g in istenen]

   plan, toplam_yuk = {}, {}
   for g in butun:
       backlog = 0.0
       for d in days:
           geldi = arrivals.get((g, d), 0.0)
           kap = capacity.get(g, 0.0) if is_working_day(d) else 0.0
           bekleyen = backlog + geldi
           yapilan = min(bekleyen, kap)
           backlog = bekleyen - yapilan
           # Ayni hucre, TEZGAH modundaki adlarla da okunabilsin: sayfa
           # tek bir dille ciziyor ve mod degistiginde sutunun anlami
           # degismiyor.  busy = o gun gercekten kesilen, with_queue =
           # onun ustune o gun yapilamayip bekleyen.
           plan[(g, d)] = {"arrived": geldi, "waiting": bekleyen,
                           "done": yapilan, "backlog": backlog,
                           "capacity": kap,
                           "busy": yapilan,
                           "idle": max(0.0, kap - yapilan),
                           "with_queue": bekleyen,
                           "queue": backlog,
                           "queue_n": 0,
                           "busy_by": parca_payi.get((g, d)) or {},
                           "queue_by": {}, "queue_n_by": {}}
       toplam_yuk[g] = sum(arrivals.get((g, d), 0.0) for d in days)

   butun.sort(key=lambda g: (-toplam_yuk.get(g, 0.0), g))
   return {"days": days, "groups": butun, "plan": plan,
           "capacity": capacity, "machines": machine_count, "units": units,
           "people": people_count, "source": source,
           "serials": serials, "no_curve": no_curve, "mode": mode,
           "overflow": tasan, "no_capacity": kapasitesiz,
           "dropped_idle": durmus, "max_wait_days": max_wait_days,
           "queue_n_day": {}, "queue_n_part_day": {},
           "all_groups": sorted({g for g, _d in arrivals} | set(capacity))}

# -*- coding: utf-8 -*-
"""
The B1000 placement library: the shapes the layout editor draws with.

They are kept as vector outlines rather than pictures, which is what lets
a machine be dropped on the plan at any size and stay crisp, be recoloured
without touching a file, and travel inside the shared database as a few
bytes of text.  Nothing here needs an imaging library.

Every drawing is authored in a 0..100 box and scaled to whatever size the
item has on the plan.  Thirty-six catalogue entries share about twenty
drawings between them - a Mazak i-700 and a DMG Mori are the same outline
at different proportions - so the drawing is named once and referred to.
"""

# ---------------------------------------------------------------------------
#  DRAWINGS  -  each a list of primitives in a 0..100 box
#
#  ("rect", x, y, w, h, radius)
#  ("circle", cx, cy, r)
#  ("path", "d", closed)      SVG path data: M L H V Q A Z, absolute
#  A primitive may carry a trailing "dash" flag as its last element.
# ---------------------------------------------------------------------------
DRAWINGS = {
    "liechti": [
        ("path", "M24 24H76V62Q76 85 50 85Q24 85 24 62Z", True),
        ("rect", 14, 4, 72, 21, 2),
        ("rect", 4, 6, 10, 17, 0),
        ("rect", 27, 26, 17, 10, 0),
        ("circle", 77, 65, 8),
        ("path", "M79 75A29 29 0 0 0 89 37", False, "dash"),
    ],
    "makino": [
        ("path", "M22 18H82V65H64V82H22Z", True),
        ("rect", 3, 20, 19, 30, 0),
        ("rect", 28, 3, 39, 15, 0),
        ("circle", 69, 43, 11),
        ("rect", 5, 59, 13, 14, 0),
        ("rect", 72, 73, 22, 8, 0),
    ],
    # HNK / Mazak i-700 / DMG Mori - a box with a hexagonal table and a
    # robot arc sweeping the corner
    "hex_cell": [
        ("path", "M18 20H85V79H18Z", True),
        ("rect", 32, 4, 43, 17, 0),
        ("path", "M54 38L68 30L81 38V55L68 64L54 55Z", True),
        ("rect", 3, 32, 15, 22, 0),
        ("rect", 23, 66, 22, 22, 0),
        ("path", "M84 62A22 22 0 0 1 62 84", False, "dash"),
    ],
    # Mazak i-400s / WFL / Puma - a bed with a hinged door panel
    "bed_door": [
        ("rect", 6, 24, 78, 43, 2),
        ("rect", 12, 30, 63, 17, 0),
        ("path", "M12 39H74M28 30V47M55 30V47", False),
        ("rect", 24, 68, 23, 12, 0),
        ("path", "M85 28L98 16V54L85 65Z", True),
        ("rect", 12, 14, 55, 7, 0),
    ],
    "broach": [
        ("rect", 8, 21, 82, 56, 0),
        ("rect", 23, 27, 61, 26, 0),
        ("path", "M23 35H84M23 45H84M34 27V53M68 27V53", False),
        ("rect", 1, 35, 20, 14, 0),
        ("rect", 25, 9, 55, 10, 0),
        ("rect", 41, 78, 37, 15, 0),
        ("rect", 84, 58, 13, 10, 0),
    ],
    # FIDIA / balancing - a round table with a crosshair between two columns
    "round_table": [
        ("rect", 16, 12, 68, 70, 0),
        ("circle", 51, 48, 20),
        ("path", "M31 48H71M51 28V68", False),
        ("rect", 4, 20, 12, 47, 0),
        ("rect", 84, 20, 12, 47, 0),
        ("rect", 29, 2, 43, 10, 0),
        ("rect", 41, 83, 23, 12, 0),
    ],
    # shot peen cabinets - a drum on a stand
    "shotpeen": [
        ("path", "M22 19H74V76H22Z", True),
        ("rect", 5, 30, 17, 22, 0),
        ("rect", 74, 35, 18, 24, 0),
        ("circle", 48, 43, 16),
        ("path", "M24 77L9 90M72 77L87 90M9 90Q48 104 87 90", False),
    ],
    "cmm": [
        ("rect", 9, 12, 81, 71, 0),
        ("rect", 19, 23, 54, 42, 0),
        ("path", "M27 22V55H65V22", False),
        ("rect", 77, 63, 10, 15, 0),
    ],
    # deburr / clean / AFM - a bench with a round window and a control strip
    "bench_round": [
        ("rect", 10, 12, 80, 74, 4),
        ("rect", 17, 20, 55, 40, 0),
        ("circle", 44, 40, 12),
        ("rect", 76, 25, 9, 20, 0),
        ("path", "M22 71H61M22 76H61M22 81H61", False),
        ("rect", 72, 70, 15, 10, 0),
    ],
    # robotic deburr / booth / plasma - a fenced cell with an arm
    "robot_cell": [
        ("path", "M8 8H92V90H8Z", True, "dash"),
        ("rect", 14, 16, 25, 55, 2),
        ("circle", 59, 57, 15),
        ("path", "M59 57L78 38L58 27", False),
        ("circle", 78, 38, 5),
        ("circle", 58, 27, 4),
        ("rect", 66, 73, 20, 12, 0),
    ],
    # Kardex / cupboard - twin doors with shelves
    "cabinet": [
        ("rect", 14, 9, 72, 80, 0),
        ("path", "M50 9V89M41 40V59M59 40V59M20 17H42M58 17H80M20 23H42M58 23H80",
         False),
    ],
    # oven / paint booth - a chamber with an open door and heat curls
    "oven": [
        ("rect", 12, 10, 68, 76, 3),
        ("rect", 21, 20, 48, 48, 0),
        ("path", "M80 10L96 23V75L80 86M89 38V60M30 77H63", False),
        ("path", "M32 55Q23 44 33 35M46 55Q37 44 47 35M60 55Q51 44 61 35", False),
    ],
    "office": [
        ("path", "M8 90V8H92V90H60M8 90H30", False),
        ("path", "M30 90V61M30 61A29 29 0 0 1 59 90", False, "dash"),
        ("rect", 19, 19, 53, 22, 2),
        ("rect", 26, 23, 20, 8, 0),
        ("rect", 35, 45, 20, 14, 5),
        ("rect", 77, 16, 9, 42, 0),
        ("rect", 65, 59, 17, 13, 4),
    ],
    # waiting area - a fenced bay with parked trolleys
    "waiting": [
        ("rect", 5, 5, 90, 90, 3, "dash"),
        ("rect", 15, 18, 18, 18, 5), ("path", "M15 23H33", False),
        ("rect", 15, 63, 18, 18, 5), ("path", "M15 68H33", False),
        ("rect", 41, 18, 18, 18, 5), ("path", "M41 23H59", False),
        ("rect", 41, 63, 18, 18, 5), ("path", "M41 68H59", False),
        ("rect", 67, 18, 18, 18, 5), ("path", "M67 23H85", False),
        ("rect", 67, 63, 18, 18, 5), ("path", "M67 68H85", False),
        ("rect", 30, 42, 40, 12, 2),
    ],
    "control_desk": [
        ("rect", 20, 18, 60, 57, 0),
        ("path", "M25 23H75V65H25Z", True),
        ("circle", 14, 42, 7),
        ("circle", 86, 42, 7),
        ("rect", 29, 81, 15, 11, 3),
        ("rect", 56, 81, 15, 11, 3),
        ("rect", 35, 5, 31, 10, 0),
    ],
    "packing_desk": [
        ("rect", 10, 22, 80, 47, 0),
        ("path", "M15 59H85", False),
        ("rect", 22, 30, 20, 21, 0), ("path", "M32 30V51", False),
        ("rect", 54, 30, 24, 21, 0), ("path", "M66 30V51", False),
        ("rect", 5, 77, 30, 15, 0),
        ("rect", 65, 77, 30, 15, 0),
        ("rect", 39, 5, 22, 12, 3),
    ],
    "rack": [
        ("rect", 8, 8, 84, 84, 0),
        ("path", "M8 29H92M8 50H92M8 71H92M36 8V92M64 8V92", False),
        ("path", "M13 13H30V24H13ZM41 34H58V45H41ZM69 55H86V66H69Z", True),
    ],
    "pallet": [
        ("rect", 9, 9, 82, 82, 0),
        ("path", "M9 25H91M9 41H91M9 57H91M9 73H91M26 9V91M74 9V91", False),
    ],
    "trolley": [
        ("rect", 16, 19, 67, 59, 3),
        ("rect", 23, 26, 53, 45, 0),
        ("path", "M83 23H94V74H83", False),
        ("circle", 25, 13, 5), ("circle", 25, 85, 5),
        ("circle", 73, 13, 5), ("circle", 73, 85, 5),
    ],
    "empty_area": [
        ("rect", 7, 7, 86, 86, 3, "dash"),
        ("path", "M14 29V14H29M71 14H86V29M86 71V86H71M29 86H14V71", False),
    ],
}

# ---------------------------------------------------------------------------
#  CATALOGUE  -  what the palette offers
#
#  kind "machine" carries a work centre code once placed;
#  kind "zone" claims operations; kind "prop" is decoration only.
# ---------------------------------------------------------------------------
MACHINE, ZONE, PROP = "machine", "zone", "prop"

CATALOG = [
    ("ASSET-01", "LIECHTI",              "liechti",      152, 200, MACHINE),
    ("ASSET-02", "MAKINO A81",           "makino",       312, 136, MACHINE),
    ("ASSET-03", "HNK NT75FS",           "hex_cell",     208, 224, MACHINE),
    ("ASSET-04", "MAZAK i-700",          "hex_cell",     144, 200, MACHINE),
    ("ASSET-05", "MAZAK i-400s",         "bed_door",     264, 144, MACHINE),
    ("ASSET-06", "WFL",                  "bed_door",     320, 120, MACHINE),
    ("ASSET-07", "FORST BROACH",         "broach",       480, 160, MACHINE),
    ("ASSET-08", "FIDIA",                "round_table",  240, 136, MACHINE),
    ("ASSET-09", "FIDIA LM / A019",      "round_table",  312, 128, MACHINE),
    ("ASSET-10", "DMG MORI TAIYO",       "hex_cell",     224, 184, MACHINE),
    ("ASSET-11", "PROGRESSIVE SHOTPEEN", "shotpeen",     200, 168, MACHINE),
    ("ASSET-12", "GLEASON P600S",        "shotpeen",     176, 144, MACHINE),
    ("ASSET-13", "CMM",                  "cmm",          128,  96, MACHINE),
    ("ASSET-14", "DEBURR",               "bench_round",  112,  64, MACHINE),
    ("ASSET-15", "ROBOTIC DEBURR",       "robot_cell",   184, 128, MACHINE),
    ("ASSET-16", "AYGIN CLEAN",          "bench_round",  112, 128, MACHINE),
    ("ASSET-17", "ALBA KARDEX",          "cabinet",      128, 112, MACHINE),
    ("ASSET-18", "BAKE FIRIN",           "oven",          96,  72, MACHINE),
    ("ASSET-19", "BOOTH",                "robot_cell",   272, 112, MACHINE),
    ("ASSET-20", "PLASMA",               "robot_cell",   272, 112, MACHINE),
    ("ASSET-21", "PUMA 5100",            "bed_door",     128, 256, MACHINE),
    ("ASSET-22", "PUMA",                 "bed_door",     128, 256, MACHINE),
    ("ASSET-23", "LEHMAN SHOT PEEN",     "shotpeen",     224, 200, MACHINE),
    ("ASSET-24", "RÖSLER SHOTPEEN",      "shotpeen",     200, 200, MACHINE),
    ("ASSET-25", "AFM",                  "bench_round",  168, 184, MACHINE),
    ("ASSET-26", "BALANCING",            "round_table",  136, 128, MACHINE),
    ("ASSET-27", "ŞEF ODASI",            "office",       280, 256, PROP),
    ("ASSET-28", "BEKLEME ALANI",        "waiting",      304, 256, ZONE),
    ("ASSET-29", "KONTROL MASASI",       "control_desk", 200, 168, PROP),
    ("ASSET-30", "PAKETLEME MASASI",     "packing_desk", 256, 168, PROP),
    ("ASSET-31", "RAF",                  "rack",         256, 112, ZONE),
    ("ASSET-32", "PALET",                "pallet",       128, 128, ZONE),
    ("ASSET-33", "TAŞIMA ARABASI",       "trolley",      136, 176, PROP),
    ("ASSET-34", "DOLAP",                "cabinet",      128,  88, PROP),
    ("ASSET-35", "BOŞ ALAN",             "empty_area",   320, 240, ZONE),
    ("ASSET-36", "BOYA KABINI",          "oven",         240, 216, MACHINE),
]

ASSET = {a[0]: {"id": a[0], "name": a[1], "drawing": a[2],
                "w": a[3], "h": a[4], "kind": a[5]} for a in CATALOG}

# Default colours by kind, overridable per item once placed.
KIND_COLOR = {MACHINE: "#7AABFF", ZONE: "#59D9AD", PROP: "#8FA3B7"}

# The bays of the empty B1000 plan, on a 1636 x 844 sheet.  Dropped in as
# the starting point so nobody has to draw the building before placing a
# single machine.
BASE_CANVAS = (1636, 844)


# ---------------------------------------------------------------------------
#  PATH -> POLYLINE
# ---------------------------------------------------------------------------
def _num(tokens, i, n):
    return [float(tokens[i + k]) for k in range(n)], i + n


def _arc(x0, y0, rx, ry, large, sweep, x1, y1, steps=14):
    """Endpoint-parameterised elliptical arc, flattened to points.

    Only circular arcs appear in this library, so the implementation stays
    at what those need rather than the full SVG specification.
    """
    import math
    if rx == 0 or ry == 0 or (x0 == x1 and y0 == y1):
        return [(x1, y1)]
    dx2, dy2 = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    lam = dx2 * dx2 / (rx * rx) + dy2 * dy2 / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * dy2 * dy2 - ry * ry * dx2 * dx2
    den = rx * rx * dy2 * dy2 + ry * ry * dx2 * dx2
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cx = co * rx * dy2 / ry + (x0 + x1) / 2.0
    cy = -co * ry * dx2 / rx + (y0 + y1) / 2.0
    t0 = math.atan2((y0 - cy) / ry, (x0 - cx) / rx)
    t1 = math.atan2((y1 - cy) / ry, (x1 - cx) / rx)
    d = t1 - t0
    if sweep and d < 0:
        d += 2 * math.pi
    elif not sweep and d > 0:
        d -= 2 * math.pi
    return [(cx + rx * math.cos(t0 + d * k / steps),
             cy + ry * math.sin(t0 + d * k / steps)) for k in range(1, steps + 1)]


def _quad(x0, y0, cx, cy, x1, y1, steps=10):
    out = []
    for k in range(1, steps + 1):
        t = k / steps
        u = 1 - t
        out.append((u * u * x0 + 2 * u * t * cx + t * t * x1,
                    u * u * y0 + 2 * u * t * cy + t * t * y1))
    return out


def path_to_polylines(d):
    """SVG path data -> [[(x, y), ...], ...], one list per pen-down run.

    Supports the absolute commands this library uses: M L H V Q A Z.  A
    path may lift the pen (a second M), which is why the result is a list
    of runs rather than a single sequence.
    """
    import re
    tokens = re.findall(r"[MLHVQAZmlhvqaz]|-?\d*\.?\d+", d)
    runs, cur = [], []
    x = y = sx = sy = 0.0
    i = 0
    cmd = None
    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy))
                    runs.append(cur)
                    cur = []
                x, y = sx, sy
                continue
        if cmd in "Mm":
            (a, b), i = _num(tokens, i, 2)
            if cur:
                runs.append(cur)
            x, y = a, b
            sx, sy = x, y
            cur = [(x, y)]
            cmd = "L"                      # further pairs continue the line
        elif cmd in "Ll":
            (a, b), i = _num(tokens, i, 2)
            x, y = a, b
            cur.append((x, y))
        elif cmd in "Hh":
            (a,), i = _num(tokens, i, 1)
            x = a
            cur.append((x, y))
        elif cmd in "Vv":
            (a,), i = _num(tokens, i, 1)
            y = a
            cur.append((x, y))
        elif cmd in "Qq":
            (cx, cy, a, b), i = _num(tokens, i, 4)
            cur.extend(_quad(x, y, cx, cy, a, b))
            x, y = a, b
        elif cmd in "Aa":
            (rx, ry, rot, large, sweep, a, b), i = _num(tokens, i, 7)
            cur.extend(_arc(x, y, rx, ry, int(large), int(sweep), a, b))
            x, y = a, b
        else:
            i += 1
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) > 1]


_POLY_CACHE = {}


def drawing_geometry(name):
    """The drawing as ready-to-scale primitives, worked out once.

    ("poly", [(x, y), ...], dashed) | ("rect", x, y, w, h, r, dashed)
    | ("circle", cx, cy, r, dashed)
    """
    if name in _POLY_CACHE:
        return _POLY_CACHE[name]
    out = []
    for prim in DRAWINGS.get(name, []):
        dashed = prim[-1] == "dash"
        if prim[0] == "rect":
            out.append(("rect", prim[1], prim[2], prim[3], prim[4], prim[5],
                        dashed))
        elif prim[0] == "circle":
            out.append(("circle", prim[1], prim[2], prim[3], dashed))
        elif prim[0] == "path":
            for run in path_to_polylines(prim[1]):
                out.append(("poly", run, dashed))
    _POLY_CACHE[name] = out
    return out

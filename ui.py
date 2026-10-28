#!/usr/bin/env python3
"""Primitive 2D thermal room simulator - Tkinter front end.

Edit racks / fans / cooling either in the property forms (changes apply live as you type)
or by dragging them on the canvas. Run the C++ simulator in the background and view the heatmap.
"""
import json
import math
import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config.cfg")
SRC = os.path.join(HERE, "thermal_sim.cpp")
EXE = os.path.join(HERE, "thermal_sim.exe" if os.name == "nt" else "thermal_sim")
CSVOUT = os.path.join(HERE, "temperature.csv")

ROOM_DEFAULTS = {"width": 100.0, "height": 100.0, "nx": 80, "ny": 80, "ambient_temp": 24.0,
                 "cool_temp": 18.0, "duration": 120.0, "dt": 0.05, "diffusivity": 0.35}
ROOM_FIELDS = [("width", "Room width"), ("height", "Room height"), ("nx", "Grid cells X (8-400)"),
               ("ny", "Grid cells Y (8-400)"), ("ambient_temp", "Ambient temp (°C)"),
               ("cool_temp", "Default cooling target (°C)"), ("duration", "Duration"),
               ("dt", "Max time step"), ("diffusivity", "Diffusivity")]
INT_KEYS = {"nx", "ny"}
DIRECTIONS = ("up", "down", "left", "right")
MODES = ("fixed", "exhaust")
KINDS = ("rack", "fan", "cooling")
KIND_TITLE = {"rack": "Rack", "fan": "Fan", "cooling": "Cooling"}
KIND_PREFIX = {"rack": "R", "fan": "F", "cooling": "C"}
GEOM = [("x", "X"), ("y", "Y"), ("w", "Width"), ("h", "Height")]
COMP_FIELDS = {
    "rack": GEOM + [("heat", "Heat rate")],
    "fan": GEOM + [("direction", "Direction"), ("strength", "Strength")],
    "cooling": GEOM + [("mode", "Mode"), ("strength", "Strength"), ("temp", "Target (°C)")],
}
CHOICES = {"direction": DIRECTIONS, "mode": MODES}
TEXT_KEYS = set(CHOICES)
COMP_COLOR = {"rack": "#e02020", "fan": "#00a83b", "cooling": "#1e90ff"}
HINTS = {
    "rack": "Constant heat source. Heat rate is °C/s added inside the rectangle.",
    "fan": "Blows along a corridor beyond the rectangle's edge. For left/right fans use a tall, narrow rectangle.",
    "cooling": "fixed: pulls cells toward the target temperature. exhaust: pulls toward ambient.",
}
DEFAULT_COMPONENT = {
    "rack": {"x": 40.0, "y": 10.0, "w": 15.0, "h": 15.0, "heat": 0.55},
    "fan": {"x": 40.0, "y": 35.0, "w": 18.0, "h": 4.0, "direction": "down", "strength": 1.8},
    "cooling": {"x": 95.0, "y": 60.0, "w": 5.0, "h": 25.0, "mode": "fixed", "strength": 2.5, "temp": 18.0},
}
MIN_SPAN = 4.0  # minimum colour range (°C), so a nearly uniform field is not stretched blue->red

DEFAULT_CONFIG = """# Primitive 2D thermal room configuration; positions use 0..100 coordinates.
width=100
height=100
nx=80
ny=80
ambient_temp=24
cool_temp=18
duration=120
dt=0.05
diffusivity=0.35
rack=6,8,16,16,0.55
rack=62,9,22,16,0.55
fan=27,34,22,4,down,1.8
fan=65,35,22,4,down,1.8
cooling=97,66,3,30,fixed,2.5,18
"""


# --------------------------------------------------------------------------- config model
def fmt(v):
    return format(v, ".10g")


def _num(s, what, lineno):
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"line {lineno}: '{s}' is not a number ({what})")
    if not math.isfinite(v):
        raise ValueError(f"line {lineno}: '{s}' is not a finite number ({what})")
    return v


def parse_config(text):
    """Return (room, racks, fans, coolings). Raises ValueError with a line number on bad input."""
    room = dict(ROOM_DEFAULTS)
    items = {k: [] for k in KINDS}
    pending_temp = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        s = raw.split("#", 1)[0].strip()
        if not s:
            continue
        if "=" not in s:
            raise ValueError(f"line {lineno}: expected key=value")
        k, v = s.split("=", 1)
        k = k.strip()
        a = [x.strip() for x in v.split(",")]
        if k in ROOM_DEFAULTS:
            val = _num(a[0], k, lineno)
            room[k] = int(val) if k in INT_KEYS else val
        elif k == "rack":
            if len(a) < 5:
                raise ValueError(f"line {lineno}: rack needs x,y,w,h,heat_rate")
            items["rack"].append({f: _num(a[i], f, lineno) for i, f in enumerate(("x", "y", "w", "h", "heat"))})
        elif k == "fan":
            if len(a) < 6:
                raise ValueError(f"line {lineno}: fan needs x,y,w,h,direction,strength")
            if a[4] not in DIRECTIONS:
                raise ValueError(f"line {lineno}: fan direction must be one of {', '.join(DIRECTIONS)}")
            d = {f: _num(a[i], f, lineno) for i, f in enumerate(("x", "y", "w", "h"))}
            d.update(direction=a[4], strength=_num(a[5], "strength", lineno))
            items["fan"].append(d)
        elif k == "cooling":
            if len(a) < 6:
                raise ValueError(f"line {lineno}: cooling needs x,y,w,h,mode,strength[,target]")
            if a[4] not in MODES:
                raise ValueError(f"line {lineno}: cooling mode must be fixed or exhaust")
            d = {f: _num(a[i], f, lineno) for i, f in enumerate(("x", "y", "w", "h"))}
            d.update(mode=a[4], strength=_num(a[5], "strength", lineno), temp=None)
            if len(a) >= 7 and a[6]:
                d["temp"] = _num(a[6], "target", lineno)
            else:
                pending_temp.append(d)
            items["cooling"].append(d)
    for d in pending_temp:  # cool_temp may appear after the cooling line, so resolve at the end
        d["temp"] = room["cool_temp"]
    return room, items["rack"], items["fan"], items["cooling"]


def serialize_config(room, items):
    L = ["# Primitive 2D thermal room configuration; positions use room units (default 0..100).",
         f"width={fmt(room['width'])}", f"height={fmt(room['height'])}",
         f"nx={int(room['nx'])}", f"ny={int(room['ny'])}",
         f"ambient_temp={fmt(room['ambient_temp'])}", f"cool_temp={fmt(room['cool_temp'])}",
         f"duration={fmt(room['duration'])}", f"dt={fmt(room['dt'])}", f"diffusivity={fmt(room['diffusivity'])}"]
    for r in items["rack"]:
        L.append("rack=" + ",".join(fmt(r[k]) for k in ("x", "y", "w", "h", "heat")))
    for f in items["fan"]:
        L.append("fan=" + ",".join([fmt(f[k]) for k in ("x", "y", "w", "h")] + [f["direction"], fmt(f["strength"])]))
    for q in items["cooling"]:
        L.append("cooling=" + ",".join([fmt(q[k]) for k in ("x", "y", "w", "h")] +
                                       [q["mode"], fmt(q["strength"]), fmt(q["temp"])]))
    return "\n".join(L) + "\n"


def validate_component(kind, c, W, H):
    """Return an error string, or None if the component is valid."""
    if c["x"] < 0 or c["y"] < 0:
        return "X and Y must be ≥ 0"
    if c["w"] <= 0 or c["h"] <= 0:
        return "Width and height must be positive"
    if c["x"] + c["w"] > W + 1e-9 or c["y"] + c["h"] > H + 1e-9:
        return f"Component must fit inside the {fmt(W)} × {fmt(H)} room"
    if kind == "rack" and c["heat"] < 0:
        return "Heat rate cannot be negative"
    if kind in ("fan", "cooling") and c["strength"] < 0:
        return "Strength cannot be negative"
    return None


def read_heat(path):
    with open(path, encoding="utf-8") as f:
        rows = [[float(v) for v in ln.split(",")] for ln in (l.strip() for l in f)
                if ln and not ln.startswith("#")]
    if not rows or any(len(r) != len(rows[0]) for r in rows):
        raise ValueError("temperature.csv is empty or not rectangular")
    return rows


# --------------------------------------------------------------------------- simulator build/run
def find_compiler():
    for name in ("g++", "clang++", "c++"):
        p = shutil.which(name)
        if p:
            return p
    return None


def ensure_binary():
    """Compile when the binary is missing or older than the source. Returns None or an error text."""
    if not os.path.exists(SRC):
        return f"Source file not found: {SRC}"
    if os.path.exists(EXE) and os.path.getmtime(EXE) >= os.path.getmtime(SRC):
        return None
    cc = find_compiler()
    if not cc:
        return "No C++ compiler found (g++ / clang++). Install one (e.g. MinGW-w64 on Windows) and try again."
    p = subprocess.run([cc, "-O2", "-std=c++17", SRC, "-o", EXE], capture_output=True, text=True, cwd=HERE)
    if p.returncode != 0:
        return "Compilation failed:\n\n" + (p.stderr or p.stdout)
    return None


# --------------------------------------------------------------------------- colours
def _lerp_stops(z):
    stops = [(0.0, (0, 30, 255)), (.25, (0, 220, 255)), (.5, (60, 230, 100)),
             (.72, (255, 235, 0)), (.86, (255, 110, 0)), (1.0, (220, 0, 0))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if a <= z <= b:
            q = (z - a) / (b - a)
            return tuple(round(ca[k] * (1 - q) + cb[k] * q) for k in range(3))
    return stops[-1][1]


LUT = ["#%02x%02x%02x" % _lerp_stops(i / 255) for i in range(256)]


class App:
    LEFT, TOP, BOTTOM = 46, 30, 26

    def __init__(self, root):
        self.root = root
        root.title("Thermal Room Simulator")
        root.geometry("1280x800")
        root.minsize(1000, 640)
        self.room = dict(ROOM_DEFAULTS)
        self.items = {k: [] for k in KINDS}
        self.sel = None            # (kind, index) or None
        self.heat = None           # list of rows, None when no valid result is available
        self.heat_img = None
        self.photo = None
        self.heat_range = (0.0, 1.0)
        self.last_metrics = None
        self.rev = 0               # bumped on every config change; lets us discard stale results
        self.running = False
        self.q = queue.Queue()
        self._filling = False
        self.errors = {}
        self.drag = None
        self.view = (0, 0, 1, 1)
        self.lists, self.comp_vars, self.err_labels, self.tab_frames = {}, {}, {}, {}
        self.room_vars = {}
        self.build_ui()
        self.load(initial=True)

    # ------------------------------------------------------------------ UI construction
    def build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        left = ttk.Frame(outer, width=380)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = ttk.Frame(outer)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        ttk.Label(left, text="Room components", font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        ttk.Label(left, text="Edit values (applied live) or drag items on the map.",
                  foreground="#555").pack(anchor="w", pady=(2, 6))
        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill="both", expand=True)
        for kind in KINDS:
            self.build_tab(kind)
        self.build_room_tab()

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(8, 0))
        self.run_btn = ttk.Button(btns, text="▶ Run simulation", command=self.run)
        self.run_btn.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Button(btns, text="Save config", command=self.save).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(btns, text="Reload config", command=self.load).grid(row=1, column=1, sticky="ew", padx=(0, 4))
        ttk.Button(btns, text="Reset defaults", command=self.reset).grid(row=1, column=2, sticky="ew")
        for c in range(3):
            btns.columnconfigure(c, weight=1)

        ttk.Label(right, text="Temperature map and layout", font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        self.canvas = tk.Canvas(right, bg="#fafafa", highlightthickness=1, highlightbackground="#888", takefocus=1)
        self.canvas.pack(fill="both", expand=True, pady=(6, 6))
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left")
        self.hover = ttk.Label(bar, text="", foreground="#555")
        self.hover.pack(side="right")

        cv = self.canvas
        cv.bind("<Configure>", lambda e: self.draw())
        cv.bind("<Button-1>", self.on_press)
        cv.bind("<B1-Motion>", self.on_drag)
        cv.bind("<ButtonRelease-1>", self.on_release)
        cv.bind("<Motion>", self.on_motion)
        cv.bind("<Leave>", lambda e: self.hover.config(text=""))
        cv.bind("<Delete>", lambda e: self.remove_selected())
        cv.bind("<BackSpace>", lambda e: self.remove_selected())
        for key, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
            cv.bind(f"<{key}>", lambda e, dx=dx, dy=dy: self.move_selected(dx, dy))
            cv.bind(f"<Shift-{key}>", lambda e, dx=dx, dy=dy: self.move_selected(5 * dx, 5 * dy))
        self.root.bind("<F5>", lambda e: self.run())
        self.root.bind("<Control-r>", lambda e: self.run())
        self.root.bind("<Control-s>", lambda e: self.save())

    def build_tab(self, kind):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text=KIND_TITLE[kind] + "s")
        self.tab_frames[kind] = tab
        top = ttk.Frame(tab)
        top.pack(fill="x")
        lb = tk.Listbox(top, height=6, exportselection=False, activestyle="none")
        sb = ttk.Scrollbar(top, orient="vertical", command=lb.yview)
        lb.config(yscrollcommand=sb.set)
        lb.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        lb.bind("<<ListboxSelect>>", lambda e, k=kind: self.on_list_select(k))
        self.lists[kind] = lb

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=6)
        ttk.Button(row, text="+ Add", command=lambda k=kind: self.add_component(k)).pack(side="left")
        ttk.Button(row, text="Duplicate", command=lambda k=kind: self.duplicate_component(k)).pack(side="left", padx=4)
        ttk.Button(row, text="Remove", command=lambda k=kind: self.remove_from_list(k)).pack(side="left")

        form = ttk.LabelFrame(tab, text="Properties", padding=8)
        form.pack(fill="x")
        vars_ = {}
        for r, (key, label) in enumerate(COMP_FIELDS[kind]):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            var = tk.StringVar()
            if key in CHOICES:
                w = ttk.Combobox(form, textvariable=var, values=CHOICES[key], state="readonly", width=14)
            else:
                w = ttk.Entry(form, textvariable=var, width=16)
            w.grid(row=r, column=1, sticky="ew", pady=2)
            var.trace_add("write", lambda *a, k=kind: self.on_form_change(k))
            vars_[key] = var
        form.columnconfigure(1, weight=1)
        self.comp_vars[kind] = vars_
        self.err_labels[kind] = ttk.Label(tab, text="", foreground="#c00", wraplength=320, justify="left")
        self.err_labels[kind].pack(anchor="w", pady=(6, 0))
        ttk.Label(tab, text=HINTS[kind], foreground="#666", wraplength=320, justify="left").pack(anchor="w", pady=(6, 0))

    def build_room_tab(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(tab, text="Room")
        self.tab_frames["room"] = tab
        for r, (key, label) in enumerate(ROOM_FIELDS):
            ttk.Label(tab, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            var = tk.StringVar()
            ttk.Entry(tab, textvariable=var, width=14).grid(row=r, column=1, sticky="ew", pady=4)
            var.trace_add("write", lambda *a: self.on_room_change())
            self.room_vars[key] = var
        tab.columnconfigure(1, weight=1)
        self.err_labels["room"] = ttk.Label(tab, text="", foreground="#c00", wraplength=330, justify="left")
        self.err_labels["room"].grid(row=len(ROOM_FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(tab, text="Component coordinates use the same units as the room width/height. "
                            "Grid cells set the heat-map resolution.",
                  foreground="#666", wraplength=330, justify="left").grid(
            row=len(ROOM_FIELDS) + 1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------ helpers
    def set_status(self, text):
        self.status.config(text=text)

    def set_error(self, key, msg):
        self.errors[key] = msg
        self.err_labels[key].config(text=msg)

    def describe(self, kind, i, c):
        base = f"{KIND_PREFIX[kind]}{i + 1}  ({fmt(c['x'])}, {fmt(c['y'])})  {fmt(c['w'])}×{fmt(c['h'])}"
        if kind == "rack":
            return f"{base}  heat {fmt(c['heat'])}"
        if kind == "fan":
            return f"{base}  {c['direction']} @ {fmt(c['strength'])}"
        return f"{base}  {c['mode']}, {fmt(c['temp'])}°C"

    def refresh_lists(self):
        for kind in KINDS:
            lb = self.lists[kind]
            lb.delete(0, "end")
            for i, c in enumerate(self.items[kind]):
                lb.insert("end", self.describe(kind, i, c))
            if self.sel and self.sel[0] == kind:
                lb.selection_set(self.sel[1])

    def refresh_line(self, kind, i):
        lb = self.lists[kind]
        lb.delete(i)
        lb.insert(i, self.describe(kind, i, self.items[kind][i]))
        if self.sel == (kind, i):
            lb.selection_set(i)

    def invalidate(self):
        """Config changed: drop the heat-map (it no longer matches) and ignore any in-flight result."""
        self.rev += 1
        if self.heat is not None:
            self.heat = self.heat_img = self.photo = None
            self.set_status("Layout changed - press Run simulation to update the map.")

    def has_errors(self):
        return any(self.errors.values())

    # ------------------------------------------------------------------ selection / forms
    def on_list_select(self, kind):
        s = self.lists[kind].curselection()
        if s:
            self.select(kind, s[0])

    def select(self, kind, i):
        self.sel = (kind, i) if kind is not None and i is not None else None
        for k in KINDS:
            self.lists[k].selection_clear(0, "end")
        if self.sel:
            self.lists[kind].selection_set(i)
            self.lists[kind].see(i)
            self.fill_form(kind)
            self.tabs.select(self.tab_frames[kind])
        self.draw()

    def fill_form(self, kind):
        c = self.items[kind][self.sel[1]]
        self._filling = True
        for key, var in self.comp_vars[kind].items():
            var.set(c[key] if key in TEXT_KEYS else fmt(c[key]))
        self._filling = False
        self.set_error(kind, "")

    def clear_form(self, kind):
        self._filling = True
        for var in self.comp_vars[kind].values():
            var.set("")
        self._filling = False
        self.set_error(kind, "")

    def parse_form(self, kind):
        d = {}
        for key, label in COMP_FIELDS[kind]:
            s = self.comp_vars[kind][key].get().strip()
            if key in TEXT_KEYS:
                if s not in CHOICES[key]:
                    raise ValueError(f"{label} must be one of {', '.join(CHOICES[key])}")
                d[key] = s
            else:
                try:
                    v = float(s)
                except ValueError:
                    raise ValueError(f"{label} must be a number")
                if not math.isfinite(v):
                    raise ValueError(f"{label} must be a finite number")
                d[key] = v
        return d

    def on_form_change(self, kind):
        if self._filling or not self.sel or self.sel[0] != kind:
            return
        try:
            d = self.parse_form(kind)
            err = validate_component(kind, d, self.room["width"], self.room["height"])
            if err:
                raise ValueError(err)
        except ValueError as e:
            self.set_error(kind, str(e) + "  (not applied)")
            return
        self.items[kind][self.sel[1]].update(d)
        self.set_error(kind, "")
        self.refresh_line(kind, self.sel[1])
        self.invalidate()
        self.draw()

    def parse_room(self):
        new = {}
        for key, label in ROOM_FIELDS:
            s = self.room_vars[key].get().strip()
            try:
                v = float(s)
            except ValueError:
                raise ValueError(f"{label} must be a number")
            if not math.isfinite(v):
                raise ValueError(f"{label} must be a finite number")
            if key in INT_KEYS:
                if v != int(v):
                    raise ValueError(f"{label} must be a whole number")
                v = int(v)
                if not 8 <= v <= 400:
                    raise ValueError(f"{label} must be between 8 and 400")
            new[key] = v
        if new["width"] <= 0 or new["height"] <= 0:
            raise ValueError("Room width and height must be positive")
        if new["dt"] <= 0:
            raise ValueError("Time step must be positive")
        if new["diffusivity"] < 0:
            raise ValueError("Diffusivity cannot be negative")
        if new["duration"] <= 0:
            raise ValueError("Duration must be positive")
        return new

    def on_room_change(self):
        if self._filling:
            return
        try:
            new = self.parse_room()
        except ValueError as e:
            self.set_error("room", str(e) + "  (not applied)")
            return
        self.set_error("room", "")
        self.room.update(new)
        outside = sum(1 for k in KINDS for c in self.items[k]
                      if validate_component(k, c, new["width"], new["height"]) and
                      (c["x"] + c["w"] > new["width"] + 1e-9 or c["y"] + c["h"] > new["height"] + 1e-9))
        if outside:
            self.set_status(f"Note: {outside} component(s) extend outside the room (they will be clipped).")
        self.invalidate()
        self.draw()

    def fill_room(self):
        self._filling = True
        for key, var in self.room_vars.items():
            var.set(str(self.room[key]) if key in INT_KEYS else fmt(self.room[key]))
        self._filling = False
        self.set_error("room", "")

    # ------------------------------------------------------------------ add / remove / move
    def add_component(self, kind, template=None):
        c = dict(template or DEFAULT_COMPONENT[kind])
        c["x"] = max(0.0, min(c["x"], self.room["width"] - c["w"]))
        c["y"] = max(0.0, min(c["y"], self.room["height"] - c["h"]))
        self.items[kind].append(c)
        self.sel = (kind, len(self.items[kind]) - 1)
        self.refresh_lists()
        self.invalidate()
        self.select(kind, self.sel[1])

    def duplicate_component(self, kind):
        s = self.lists[kind].curselection()
        if not s:
            self.set_status(f"Select a {kind} to duplicate.")
            return
        c = dict(self.items[kind][s[0]])
        c["x"] += 3
        c["y"] += 3
        self.add_component(kind, c)

    def remove_from_list(self, kind):
        s = self.lists[kind].curselection()
        if not s:
            self.set_status(f"Select a {kind} to remove.")
            return
        self.remove(kind, s[0])

    def remove_selected(self):
        if self.sel:
            self.remove(*self.sel)

    def remove(self, kind, i):
        del self.items[kind][i]
        self.sel = None
        self.refresh_lists()
        self.invalidate()
        if self.items[kind]:
            self.select(kind, min(i, len(self.items[kind]) - 1))
        else:
            self.clear_form(kind)
            self.draw()

    def move_to(self, kind, i, x, y):
        c = self.items[kind][i]
        x = max(0.0, min(round(x * 2) / 2, self.room["width"] - c["w"]))
        y = max(0.0, min(round(y * 2) / 2, self.room["height"] - c["h"]))
        if (x, y) == (c["x"], c["y"]):
            return False
        c["x"], c["y"] = x, y
        self.invalidate()
        if self.sel == (kind, i):
            self.fill_form(kind)
        self.refresh_line(kind, i)
        self.draw()
        return True

    def move_selected(self, dx, dy):
        if self.sel:
            c = self.items[self.sel[0]][self.sel[1]]
            self.move_to(*self.sel, c["x"] + dx, c["y"] + dy)
        return "break"

    # ------------------------------------------------------------------ load / save / reset
    def load(self, initial=False):
        try:
            if os.path.exists(CFG):
                with open(CFG, encoding="utf-8") as f:
                    text = f.read()
                msg = "Configuration loaded."
            else:
                text, msg = DEFAULT_CONFIG, "config.cfg not found - using the default layout."
            room, racks, fans, coolings = parse_config(text)
        except Exception as e:
            messagebox.showerror("Load failed", f"{e}\n\nThe default layout was loaded instead.")
            room, racks, fans, coolings = parse_config(DEFAULT_CONFIG)
            msg = "Load failed - defaults loaded."
        self.room = room
        self.items = {"rack": racks, "fan": fans, "cooling": coolings}
        self.sel = None
        self.heat = self.heat_img = self.photo = None
        self.rev += 1
        for k in KINDS:
            self.clear_form(k)
        self.fill_room()
        self.refresh_lists()
        self.set_status(msg + " Press Run simulation to see the map.")
        self.draw()

    def reset(self):
        if not messagebox.askyesno("Reset", "Replace the current layout and settings with the defaults?"):
            return
        room, racks, fans, coolings = parse_config(DEFAULT_CONFIG)
        self.room = room
        self.items = {"rack": racks, "fan": fans, "cooling": coolings}
        self.sel = None
        self.heat = self.heat_img = self.photo = None
        self.rev += 1
        for k in KINDS:
            self.clear_form(k)
        self.fill_room()
        self.refresh_lists()
        self.set_status("Defaults restored (not saved yet).")
        self.draw()

    def save(self):
        if self.has_errors():
            messagebox.showerror("Cannot save", "Fix the highlighted invalid field first:\n\n" +
                                 "\n".join(m for m in self.errors.values() if m))
            return False
        try:
            with open(CFG, "w", encoding="utf-8") as f:
                f.write(serialize_config(self.room, self.items))
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return False
        self.set_status("Configuration saved to config.cfg")
        return True

    # ------------------------------------------------------------------ simulation (background thread)
    def run(self):
        if self.running:
            return
        if not self.save():
            return
        self.running = True
        self.run_btn.state(["disabled"])
        self.root.config(cursor="watch")
        self.set_status("Running simulation...")
        threading.Thread(target=self._worker, args=(self.rev,), daemon=True).start()
        self.root.after(80, self._poll)

    def _worker(self, rev):
        try:
            err = ensure_binary()
            if err:
                self.q.put(("err", rev, err))
                return
            p = subprocess.run([EXE, CFG, CSVOUT], capture_output=True, text=True, cwd=HERE, timeout=600)
            if p.returncode != 0:
                self.q.put(("err", rev, (p.stderr or p.stdout or "The simulator exited with an error.").strip()))
                return
            self.q.put(("ok", rev, json.loads(p.stdout.strip().splitlines()[-1])))
        except subprocess.TimeoutExpired:
            self.q.put(("err", rev, "The simulation took longer than 10 minutes and was stopped."))
        except Exception as e:  # noqa: BLE001 - report anything to the user
            self.q.put(("err", rev, str(e)))

    def _poll(self):
        try:
            kind, rev, payload = self.q.get_nowait()
        except queue.Empty:
            self.root.after(80, self._poll)
            return
        self.running = False
        self.run_btn.state(["!disabled"])
        self.root.config(cursor="")
        if rev != self.rev:
            self.set_status("The layout changed while the simulation ran - result discarded. Run again.")
            return
        if kind == "err":
            self.set_status("Simulation failed.")
            messagebox.showerror("Simulation failed", payload)
            return
        try:
            self.set_heat(read_heat(CSVOUT))
        except Exception as e:  # noqa: BLE001
            self.set_status("Could not read the result.")
            messagebox.showerror("Result error", str(e))
            return
        m = payload
        self.last_metrics = m
        self.set_status(f"Avg rack {m['average_rack_temp']:.2f} °C  |  Max rack {m['max_rack_temp']:.2f} °C  |  "
                        f"Min {m['min_temp']:.2f} °C  |  {m['steps']} steps, dt {m['dt_used']:.4g}")
        self.draw()

    def set_heat(self, rows):
        vals = [v for r in rows for v in r]
        lo, hi = min(vals), max(vals)
        if hi - lo < MIN_SPAN:
            mid = (hi + lo) / 2
            lo, hi = mid - MIN_SPAN / 2, mid + MIN_SPAN / 2
        self.heat_range = (lo, hi)
        ny, nx = len(rows), len(rows[0])
        img = tk.PhotoImage(width=nx, height=ny)
        span = hi - lo
        data = " ".join("{" + " ".join(LUT[max(0, min(255, int(255 * (v - lo) / span)))] for v in row) + "}"
                        for row in rows)
        img.put(data)
        self.heat, self.heat_img = rows, img

    # ------------------------------------------------------------------ canvas interaction
    def to_room(self, px, py):
        ox, oy, sx, sy = self.view
        return (px - ox) / sx, (py - oy) / sy

    def hit(self, x, y):
        order = []
        if self.sel:
            order.append(self.sel)
        for kind in reversed(KINDS):
            order += [(kind, i) for i in reversed(range(len(self.items[kind])))]
        for kind, i in order:
            c = self.items[kind][i]
            if c["x"] <= x <= c["x"] + c["w"] and c["y"] <= y <= c["y"] + c["h"]:
                return kind, i
        return None

    def on_press(self, e):
        self.canvas.focus_set()
        x, y = self.to_room(e.x, e.y)
        h = self.hit(x, y)
        if h:
            c = self.items[h[0]][h[1]]
            self.drag = {"kind": h[0], "i": h[1], "dx": x - c["x"], "dy": y - c["y"]}
            if self.sel != h:
                self.select(*h)
        else:
            self.drag = None
            if self.sel:
                self.select(None, None)

    def on_drag(self, e):
        if not self.drag:
            return
        x, y = self.to_room(e.x, e.y)
        d = self.drag
        self.move_to(d["kind"], d["i"], x - d["dx"], y - d["dy"])
        self.on_motion(e)

    def on_release(self, e):
        self.drag = None

    def on_motion(self, e):
        x, y = self.to_room(e.x, e.y)
        W, H = self.room["width"], self.room["height"]
        if not (0 <= x <= W and 0 <= y <= H):
            self.hover.config(text="")
            return
        text = f"x={x:.1f}  y={y:.1f}"
        if self.heat:
            ny, nx = len(self.heat), len(self.heat[0])
            ix, iy = min(nx - 1, int(x / W * nx)), min(ny - 1, int(y / H * ny))
            text += f"   T={self.heat[iy][ix]:.2f} °C"
        self.hover.config(text=text)

    # ------------------------------------------------------------------ drawing
    def halo_text(self, x, y, text, color, **kw):
        for ox, oy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            self.canvas.create_text(x + ox, y + oy, text=text, fill="white", **kw)
        self.canvas.create_text(x, y, text=text, fill=color, **kw)

    def draw(self):
        cv = self.canvas
        cv.delete("all")
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if cw < 160 or ch < 120:
            return
        W, H = self.room["width"], self.room["height"]
        right = 100 if self.heat else 24
        aw, ah = cw - self.LEFT - right, ch - self.TOP - self.BOTTOM
        scale = min(aw / W, ah / H)
        pw, ph = W * scale, H * scale
        kx = ky = 1
        if self.heat:  # snap to whole pixels per cell so the zoomed image is exact
            ny, nx = len(self.heat), len(self.heat[0])
            kx, ky = max(1, round(pw / nx)), max(1, round(ph / ny))
            pw, ph = nx * kx, ny * ky
        ox, oy = self.LEFT + (aw - pw) / 2, self.TOP + (ah - ph) / 2
        sx, sy = pw / W, ph / H
        self.view = (ox, oy, sx, sy)

        if self.heat:
            self.photo = self.heat_img.zoom(kx, ky)
            cv.create_image(ox, oy, anchor="nw", image=self.photo)
        else:
            cv.create_rectangle(ox, oy, ox + pw, oy + ph, fill="white", outline="")
            cv.create_text(ox + pw / 2, oy + ph / 2, text="No simulation result yet\nPress ▶ Run simulation (F5)",
                           fill="#aaa", font=("TkDefaultFont", 12), justify="center")
        cv.create_rectangle(ox, oy, ox + pw, oy + ph, outline="black", width=2)

        for i in range(6):  # axis ticks in room units
            fx, fy = W * i / 5, H * i / 5
            cv.create_text(ox + fx * sx, oy - 10, text=f"{fx:g}", fill="#666", font=("TkDefaultFont", 8))
            cv.create_text(ox - 8, oy + fy * sy, text=f"{fy:g}", fill="#666", font=("TkDefaultFont", 8), anchor="e")

        for kind in KINDS:
            for i, c in enumerate(self.items[kind]):
                if self.sel != (kind, i):
                    self.draw_component(kind, i, c, False)
        if self.sel:
            self.draw_component(self.sel[0], self.sel[1], self.items[self.sel[0]][self.sel[1]], True)
        if self.heat:
            self.draw_colorbar(cw, oy, ph)

    def draw_component(self, kind, i, c, selected):
        cv = self.canvas
        ox, oy, sx, sy = self.view
        x0, y0 = ox + c["x"] * sx, oy + c["y"] * sy
        x1, y1 = x0 + c["w"] * sx, y0 + c["h"] * sy
        color = COMP_COLOR[kind]
        lw = 3 if selected else 2
        cv.create_rectangle(x0, y0, x1, y1, outline="white", width=lw + 3)
        cv.create_rectangle(x0, y0, x1, y1, outline=color, width=lw)
        if selected:
            cv.create_rectangle(x0 + 3, y0 + 3, x1 - 3, y1 - 3, outline="black", dash=(4, 3))
        label = f"{KIND_PREFIX[kind]}{i + 1}"
        if kind == "cooling":
            label += " " + ("EXH" if c["mode"] == "exhaust" else "COOL")
        self.halo_text(x0 + 4, y0 + 3, label, color, anchor="nw", font=("TkDefaultFont", 9, "bold"))
        if kind == "fan":
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            L = max(26, 0.1 * min(self.canvas.winfo_width(), self.canvas.winfo_height()))
            d = c["direction"]
            pts = {"down": (cx, y1, cx, y1 + L), "up": (cx, y0, cx, y0 - L),
                   "right": (x1, cy, x1 + L, cy), "left": (x0, cy, x0 - L, cy)}[d]
            cv.create_line(*pts, fill="white", width=7, arrow="last", arrowshape=(14, 16, 6))
            cv.create_line(*pts, fill=color, width=3, arrow="last", arrowshape=(12, 14, 5))

    def draw_colorbar(self, cw, oy, ph):
        cv = self.canvas
        lo, hi = self.heat_range
        x0, w, n = cw - 82, 16, 64
        for k in range(n):
            frac = 1 - k / (n - 1)
            cv.create_rectangle(x0, oy + ph * k / n, x0 + w, oy + ph * (k + 1) / n + 1,
                                fill=LUT[int(255 * frac)], outline="")
        cv.create_rectangle(x0, oy, x0 + w, oy + ph, outline="black")
        for k in range(5):
            frac = k / 4
            y = oy + ph * (1 - frac)
            cv.create_line(x0 + w, y, x0 + w + 4, y)
            cv.create_text(x0 + w + 7, y, text=f"{lo + (hi - lo) * frac:.1f}", anchor="w", font=("TkDefaultFont", 8))
        cv.create_text(x0, oy - 10, text="°C", anchor="w", font=("TkDefaultFont", 9, "bold"))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
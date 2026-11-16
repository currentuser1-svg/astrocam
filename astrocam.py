# -*- coding: utf-8 -*-
"""AstroCam - capture tool for UVC webcams (LifeCam Studio etc.) aimed at lucky imaging / stacking.

Camera -> ffmpeg (dshow) -> either
  * SER (Python writes it: mono Y or RGB, crop + optional object tracking, per-frame UTC timestamps), or
  * any ffmpeg output (raw AVI, FFV1, H.264/H.265, PNG/TIFF/FITS/JPEG sequences, MJPEG stream copy).
Camera controls (exposure, focus, ...) go through camctl.exe (DirectShow IAMCameraControl / IAMVideoProcAmp).
"""
import base64
import datetime as dt
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CAMCTL = os.path.join(APP_DIR, "camctl.exe")
G2R_CTL = os.path.join(APP_DIR, "go2rtc_ctl.ps1")
PRESET_DIR = os.path.join(APP_DIR, "presets")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
GO2RTC_YAML = r"C:\go2rtc\go2rtc.yaml"
G2R_CONTROLS = os.path.join(APP_DIR, "go2rtc_controls.json")   # camera settings go2rtc had before we took the camera
NOWIN = 0x08000000  # CREATE_NO_WINDOW


def find_ffmpeg():
    for p in (r"C:\go2rtc\ffmpeg.exe", shutil.which("ffmpeg") or ""):
        if p and os.path.isfile(p):
            return p
    return "ffmpeg"


# ----------------------------------------------------------------------------- output formats
# key: (label, kind, extension)   kind: ser | video | seq | copy
FORMATS = {
    "ser":      ("SER — для AutoStakkert / Siril / PIPP (рекомендуется)", "ser", ".ser"),
    "avi_raw":  ("AVI несжатый (rawvideo)", "video", ".avi"),
    "mkv_ffv1": ("MKV FFV1 — без потерь, ~в 2 раза меньше (архив)", "video", ".mkv"),
    "mp4_h264": ("MP4 H.264 — с потерями, качество/битрейт", "video", ".mp4"),
    "mp4_hevc": ("MP4 H.265 — с потерями, качество/битрейт", "video", ".mp4"),
    "seq_tif":  ("TIFF кадры — для DeepSkyStacker", "seq", ".tif"),
    "seq_png":  ("PNG кадры (без потерь)", "seq", ".png"),
    "seq_fits": ("FITS кадры (Siril и др.)", "seq", ".fits"),
    "seq_jpg":  ("JPEG кадры (с потерями)", "seq", ".jpg"),
    "mjpeg_copy": ("MJPEG как есть из камеры — без перекодирования (только вход MJPEG)", "copy", ".avi"),
}
ENCODERS = {
    "mp4_h264": [("libx264", "CPU x264"), ("h264_qsv", "Intel QSV"), ("h264_nvenc", "NVIDIA NVENC")],
    "mp4_hevc": [("libx265", "CPU x265"), ("hevc_qsv", "Intel QSV"), ("hevc_nvenc", "NVIDIA NVENC")],
}
NOISE = ("deprecated pixel format", "EOI missing", "Last message repeated")
STD_FPS = [1, 2, 3, 5, 7.5, 10, 15, 20, 24, 25, 30, 60]

DEFAULTS = {
    "device": "Microsoft® LifeCam Studio(TM)",
    "input": "yuyv422", "size": "1920x1080", "fps": "5",
    "duration": 60.0, "frames": 0, "skip": 10, "every": 1, "series": 1, "pause": 0.0,
    "crop_on": False, "crop": [0, 0, 0, 0], "track": False,
    "color": "mono", "fullrange": False,
    "format": "ser", "encoder": "", "qmode": "quality", "quality": 18, "bitrate": 8000, "preset": "medium",
    "outdir": os.path.join(APP_DIR, "captures"), "template": "{target}_{utc}", "target": "Moon",
    "observer": "", "telescope": "", "notes": "",
    "controls": {}, "release_go2rtc": True,
}

DAY_HINT = ("День: 1) запустите превью и наведите кроп на статичный объект (здание, вывеска, антенна); "
            "2) «Зафиксировать авто» на вкладке «Камера» — фокус, выдержка и баланс белого замрут; "
            "3) подстройте фокус по показателю «резкость», выдержку сделайте на 1–2 шага короче, если картинка не темнеет сильно — "
            "меньше смаза от дрожания воздуха; 4) 300–1000 кадров. Дальше AutoStakkert: Surface, Drizzle 1.5x.")

BUILTIN_PRESETS = {
    "Луна — сырой Y, 1080p": dict(input="yuyv422", size="1920x1080", fps="5", format="ser", color="mono",
                                   duration=120.0, frames=600, skip=10, crop_on=False, target="Moon",
                                   controls={"Exposure": [-9, False], "Sharpness": [0, False], "BacklightCompensation": [0, False]}),
    "Планета — RGB SER, кроп 400, ведение": dict(input="yuyv422", size="1280x720", fps="10", format="ser", color="color",
                                                 duration=90.0, frames=0, skip=10, crop_on=True, crop=[440, 160, 400, 400],
                                                 track=True, series=3, pause=5.0, target="Jupiter",
                                                 controls={"Exposure": [-6, False], "Sharpness": [0, False]}),
    "Звёзды — TIFF для DSS, макс. выдержка": dict(input="yuyv422", size="1920x1080", fps="5", format="seq_tif", color="color",
                                                  duration=0.0, frames=100, skip=10, every=1, crop_on=False, target="Stars",
                                                  controls={"Exposure": [0, False], "Sharpness": [0, False], "WhiteBalance": [4500, False]}),
    "Звёзды — SER моно, макс. выдержка (Siril)": dict(input="yuyv422", size="1920x1080", fps="5", format="ser", color="mono",
                                                      duration=0.0, frames=300, skip=10, crop_on=False, target="Stars",
                                                      controls={"Exposure": [0, False], "Sharpness": [0, False]}),
    "MJPEG 1080p30 без перекодирования": dict(input="mjpeg", size="1920x1080", fps="30", format="mjpeg_copy",
                                              duration=60.0, frames=0, skip=0, crop_on=False, target="Video"),
    "Видео H.264 (NVENC) 1080p30": dict(input="mjpeg", size="1920x1080", fps="30", format="mp4_h264", encoder="h264_nvenc",
                                        color="color", qmode="quality", quality=20, duration=60.0, frames=0, skip=0,
                                        crop_on=False, target="Video"),
    "Таймлапс — кадр раз в 4 с, H.264": dict(input="mjpeg", size="1920x1080", fps="7.5", format="mp4_h264", encoder="libx264",
                                             color="color", qmode="quality", quality=20, duration=3600.0, frames=0,
                                             every=30, skip=10, crop_on=False, target="Timelapse"),
    "День — сверхразрешение, цвет (SER)": dict(input="yuyv422", size="1920x1080", fps="5", format="ser", color="color",
                                              duration=0.0, frames=300, skip=10, every=1, series=1, crop_on=True,
                                              crop=[760, 340, 400, 400], track=False, target="Day",
                                              controls={"Sharpness": [0, False], "BacklightCompensation": [0, False]},
                                              hint=DAY_HINT),
    "День — сверхразрешение, моно Y (макс. детали)": dict(input="yuyv422", size="1920x1080", fps="5", format="ser", color="mono",
                                                         duration=0.0, frames=300, skip=10, every=1, series=1, crop_on=True,
                                                         crop=[760, 340, 400, 400], track=False, target="DayMono",
                                                         controls={"Sharpness": [0, False], "BacklightCompensation": [0, False]},
                                                         hint=DAY_HINT),
    "День — движущиеся объекты, MJPEG 30 к/с": dict(input="mjpeg", size="1920x1080", fps="30", format="mjpeg_copy",
                                                   duration=15.0, frames=0, skip=0, every=1, series=1, crop_on=False,
                                                   track=False, target="Moving",
                                                   controls={"Sharpness": [0, False], "BacklightCompensation": [0, False]},
                                                   hint="Машины и всё движущееся стекать нельзя — смажется. Снимайте короткой выдержкой "
                                                        "(«Зафиксировать авто», затем выдержку на 1–2 шага короче) и берите лучшие кадры: "
                                                        "«Лучшие кадры из записи…» на вкладке «Вывод»."),
}


# ----------------------------------------------------------------------------- helpers
def run(cmd, timeout=30):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NOWIN)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def list_devices(ffmpeg):
    try:
        rc, out, err = run([CAMCTL, "list"], 15)
        if rc == 0:
            return json.loads(out)
    except Exception:
        pass
    rc, out, err = run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    return re.findall(r'"([^"]+)" \(video\)', err)


def list_modes(ffmpeg, device):
    """{'yuyv422': {(1920,1080): (minfps, maxfps)}, 'mjpeg': {...}}"""
    rc, out, err = run([ffmpeg, "-hide_banner", "-list_options", "true", "-f", "dshow", "-i", "video=" + device], 30)
    modes = {}
    for m in re.finditer(r"(pixel_format|vcodec)=(\S+)\s+min s=(\d+)x(\d+) fps=([\d.]+) max s=(\d+)x(\d+) fps=([\d.]+)", err):
        fmt = m.group(2)
        size = (int(m.group(6)), int(m.group(7)))
        lo, hi = float(m.group(5)), float(m.group(8))
        old = modes.setdefault(fmt, {}).get(size)
        modes[fmt][size] = (min(lo, old[0]), max(hi, old[1])) if old else (lo, hi)
    return modes


def exposure_text(v):
    s = 2.0 ** v
    if s >= 1:
        return f"{s:g} с"
    return f"1/{round(1 / s)} с ({s * 1000:.3g} мс)"


def human_bytes(n):
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or u == "ТБ":
            return f"{n:.1f} {u}" if u != "Б" else f"{int(n)} Б"
        n /= 1024.0


def net_ticks(t):
    """unix time -> .NET ticks (SER timestamps)"""
    return int(round(t * 10_000_000)) + 621_355_968_000_000_000


def winjupos_time(t):
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return d.strftime("%Y-%m-%d-%H%M_") + str(int(d.second / 6))


class SerWriter:
    """SER v3 writer, 8 bit. color: 'mono' -> ColorID 0, 'rgb' -> ColorID 100"""

    def __init__(self, path, w, h, color, observer="", instrument="", telescope=""):
        self.path, self.w, self.h = path, w, h
        self.planes = 3 if color == "rgb" else 1
        self.f = open(path, "wb", buffering=8 * 1024 * 1024)
        self.ts = []
        self.t0 = time.time()
        hdr = b"LUCAM-RECORDER" + struct.pack("<7i", 0, 100 if color == "rgb" else 0, 0, w, h, 8, 0)
        for s in (observer, instrument, telescope):
            hdr += s.encode("ascii", "replace")[:40].ljust(40, b"\0")
        local = time.time() + (dt.datetime.now().astimezone().utcoffset().total_seconds())
        hdr += struct.pack("<qq", net_ticks(local), net_ticks(time.time()))
        assert len(hdr) == 178
        self.f.write(hdr)

    def write(self, frame_bytes, t):
        self.f.write(frame_bytes)
        self.ts.append(t)

    def close(self):
        if self.ts:
            self.f.write(struct.pack(f"<{len(self.ts)}q", *[net_ticks(t) for t in self.ts]))
            self.f.seek(38)
            self.f.write(struct.pack("<i", len(self.ts)))
            # header datetime = first frame
            self.f.seek(162)
            off = dt.datetime.now().astimezone().utcoffset().total_seconds()
            self.f.write(struct.pack("<qq", net_ticks(self.ts[0] + off), net_ticks(self.ts[0])))
        self.f.close()
        return len(self.ts)


# ----------------------------------------------------------------------------- ffmpeg stream
class FFStream:
    """Runs ffmpeg, delivers fixed-size raw frames from stdout to on_frame(bytes, t)."""

    def __init__(self, cmd, frame_bytes, on_frame, on_log, on_exit):
        self.cmd, self.frame_bytes = cmd, frame_bytes
        self.on_frame, self.on_log, self.on_exit = on_frame, on_log, on_exit
        self.drops = 0
        self.progress = {}
        self.p = None
        self.alive = False

    def start(self):
        self.on_log("ffmpeg " + " ".join(f'"{a}"' if " " in a else a for a in self.cmd[1:]))
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE if self.frame_bytes else subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, creationflags=NOWIN, bufsize=0)
        self.alive = True
        threading.Thread(target=self._err, daemon=True).start()
        if self.frame_bytes:
            threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _out(self):
        n = self.frame_bytes
        buf = bytearray()
        rd = self.p.stdout.read
        while True:
            try:
                chunk = rd(max(n - len(buf), 65536) if len(buf) < n else n)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= n:
                fr = bytes(buf[:n])
                del buf[:n]
                try:
                    self.on_frame(fr, time.time())
                except Exception as e:  # never kill the reader
                    self.on_log(f"frame handler error: {e!r}")

    def _err(self):
        tail = b""
        for raw in iter(lambda: self.p.stderr.read(4096), b""):
            tail += raw
            parts = re.split(rb"[\r\n]", tail)
            tail = parts.pop()
            for line in parts:
                s = line.decode("utf-8", "replace").strip()
                if not s:
                    continue
                if "=" in s and re.match(r"^[a-z0-9_]+=", s):
                    k, v = s.split("=", 1)
                    self.progress[k] = v
                    continue
                if "frame dropped" in s or "buffer full" in s:
                    self.drops += 1
                    if self.drops in (1, 10, 100) or self.drops % 1000 == 0:
                        self.on_log(f"⚠ ffmpeg: потеря кадров ({self.drops}): {s}")
                    continue
                if s.startswith("frame=") or s.startswith("size=") or any(n in s for n in NOISE):
                    continue
                self.on_log(s)

    def _wait(self):
        rc = self.p.wait()
        self.alive = False
        self.on_exit(rc)

    def stop(self, graceful=True, timeout=8):
        if not self.p or self.p.poll() is not None:
            return
        if graceful:
            try:
                self.p.stdin.write(b"q")
                self.p.stdin.flush()
                self.p.stdin.close()
            except Exception:
                pass
            try:
                self.p.wait(timeout)
                return
            except subprocess.TimeoutExpired:
                pass
        try:
            self.p.kill()
        except Exception:
            pass


# ----------------------------------------------------------------------------- application
class App:
    def __init__(self, root):
        self.root = root
        self.ffmpeg = find_ffmpeg()
        self.cfg = dict(DEFAULTS)
        self.cfg.update(self._load_json(SETTINGS_FILE, {}))
        self.modes = {}
        self.ctl_info = []            # camctl props
        self.ctl_widgets = {}
        self.ctl_pending = {}
        self.ctl_after = None
        self.stream = None            # FFStream (preview or capture)
        self.mode = "idle"            # idle | preview | capture
        self.latest = None            # (np.ndarray HxWx3|HxW, full_frame: bool, origin(x,y))
        self.latest_seq = 0
        self.shown_seq = -1
        self.cap = None               # capture state dict
        self.stop_req = False
        self.logq = queue.Queue()
        self.uiq = queue.Queue()      # callbacks from worker threads, run by tick()
        self.fps_meter = []
        self.focus_best = 0.0
        self.drag = None
        self.disp = None              # (scale, ox, oy) of current preview rendering
        self.photo = None
        self.zoom_photo = None
        self.released_by_us = False
        self._build_ui()
        self._apply_cfg_to_ui()
        self.refresh_devices(first=True)
        self.root.after(100, self.tick)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self):
        r = self.root
        r.title("AstroCam — захват для стекинга")
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.geometry(f"{min(1800, sw - 40)}x{min(1080, sh - 90)}+10+10")
        r.minsize(1100, 700)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=6)
        style.configure("Stat.TLabel", font=("Consolas", 10))

        top = ttk.Frame(r, padding=(6, 6, 6, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Пресет:").pack(side="left")
        self.v_preset = tk.StringVar()
        self.cb_preset = ttk.Combobox(top, textvariable=self.v_preset, width=44, state="readonly")
        self.cb_preset.pack(side="left", padx=4)
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self.load_preset(self.v_preset.get()))
        ttk.Button(top, text="Сохранить как…", command=self.save_preset).pack(side="left", padx=2)
        ttk.Button(top, text="Удалить", command=self.delete_preset).pack(side="left", padx=2)
        self.lbl_g2r = ttk.Label(top, text="", foreground="#a60")
        self.lbl_g2r.pack(side="right")
        self.btn_g2r = ttk.Button(top, text="go2rtc…", command=self.toggle_go2rtc)
        self.btn_g2r.pack(side="right", padx=4)

        body = ttk.Frame(r)
        body.pack(fill="both", expand=True, padx=6, pady=6)

        # ---- left: preview
        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(6, 0))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, bg="#111", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        info = ttk.Frame(left)
        info.pack(fill="x", pady=(4, 0))
        self.v_stretch = tk.BooleanVar(value=True)
        ttk.Checkbutton(info, text="Растянуть превью", variable=self.v_stretch,
                        command=lambda: setattr(self, "shown_seq", -1)).pack(side="left", padx=(0, 6))
        self.hist = tk.Canvas(info, width=260, height=70, bg="#1b1b1b", highlightthickness=0)
        self.hist.pack(side="left")
        self.zoom = tk.Canvas(info, width=180, height=70, bg="#1b1b1b", highlightthickness=0)
        self.zoom.pack(side="left", padx=6)
        self.v_stats = tk.StringVar(value="—")
        ttk.Label(info, textvariable=self.v_stats, style="Stat.TLabel", justify="left").pack(side="left", fill="x", expand=True)

        # ---- right: settings
        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        self._tab_camera(nb)
        self._tab_capture(nb)
        self._tab_output(nb)

        btns = ttk.Frame(right, padding=(0, 6))
        btns.pack(fill="x")
        self.btn_preview = ttk.Button(btns, text="▶ Превью", style="Big.TButton", command=self.toggle_preview)
        self.btn_preview.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.btn_rec = ttk.Button(btns, text="● Запись", style="Big.TButton", command=self.toggle_capture)
        self.btn_rec.pack(side="left", fill="x", expand=True, padx=3)
        ttk.Button(btns, text="📷 Снимок", command=self.snapshot).pack(side="left", padx=(3, 0))
        self.v_est = tk.StringVar()
        ttk.Label(right, textvariable=self.v_est, foreground="#555", wraplength=460, justify="left").pack(fill="x")
        self.pb = ttk.Progressbar(right, mode="determinate")
        self.pb.pack(fill="x", pady=3)
        self.v_capstat = tk.StringVar()
        ttk.Label(right, textvariable=self.v_capstat, style="Stat.TLabel").pack(fill="x")
        self.log = tk.Text(right, height=9, wrap="word", font=("Consolas", 8), bg="#fafafa")
        self.log.pack(fill="both", expand=False, pady=(4, 0))

    def _row(self, parent, label, widget_factory, row, tip=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 6))
        w = widget_factory(parent)
        w.grid(row=row, column=1, sticky="ew", pady=2)
        if tip:
            ttk.Label(parent, text=tip, foreground="#777", wraplength=420, justify="left").grid(row=row + 1, column=0, columnspan=2, sticky="w")
        return w

    def _tab_camera(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Камера")
        f.columnconfigure(1, weight=1)
        self.v_device = tk.StringVar()
        dev = ttk.Frame(f)
        self.cb_device = ttk.Combobox(dev, textvariable=self.v_device, state="readonly")
        self.cb_device.pack(side="left", fill="x", expand=True)
        self.cb_device.bind("<<ComboboxSelected>>", lambda e: self.on_device())
        ttk.Button(dev, text="⟳", width=3, command=self.refresh_devices).pack(side="left", padx=(3, 0))
        ttk.Label(f, text="Камера").grid(row=0, column=0, sticky="w")
        dev.grid(row=0, column=1, sticky="ew", pady=2)

        self.v_input = tk.StringVar()
        self.cb_input = self._row(f, "Поток с камеры", lambda p: ttk.Combobox(p, textvariable=self.v_input, state="readonly"), 1)
        self.cb_input.bind("<<ComboboxSelected>>", lambda e: self.on_input())
        self.v_size = tk.StringVar()
        self.cb_size = self._row(f, "Разрешение", lambda p: ttk.Combobox(p, textvariable=self.v_size, state="readonly"), 2)
        self.cb_size.bind("<<ComboboxSelected>>", lambda e: self.on_size())
        self.v_fps = tk.StringVar()
        self.cb_fps = self._row(f, "Кадров/с камеры", lambda p: ttk.Combobox(p, textvariable=self.v_fps, state="readonly", width=8), 3,
                                tip="RAW YUY2 упирается в USB 2.0: 1080p — 5 к/с, 720p — 10 к/с. MJPEG до 30 к/с, но с артефактами сжатия.")
        self.v_bw = tk.StringVar()
        ttk.Label(f, textvariable=self.v_bw, foreground="#555").grid(row=5, column=0, columnspan=2, sticky="w")

        ttk.Separator(f).grid(row=6, column=0, columnspan=2, sticky="ew", pady=6)
        hdr = ttk.Frame(f)
        hdr.grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Label(hdr, text="Настройки камеры (применяются сразу, сохраняются в пресет)", font=("Segoe UI", 9, "bold")).pack(side="left")
        bb = ttk.Frame(f)
        bb.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        ttk.Button(bb, text="Прочитать", command=self.read_controls).pack(side="left")
        ttk.Button(bb, text="Астро-база", command=self.astro_controls).pack(side="left", padx=3)
        ttk.Button(bb, text="По умолчанию", command=self.default_controls).pack(side="left")
        ttk.Button(bb, text="Окно драйвера…", command=self.driver_dialog).pack(side="left", padx=3)
        bb2 = ttk.Frame(f)
        bb2.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(bb2, text="Зафиксировать авто (выдержка, фокус, баланс белого)", command=self.lock_auto).pack(side="left")
        self.ctl_frame = ttk.Frame(f)
        self.ctl_frame.grid(row=9, column=0, columnspan=2, sticky="nsew")
        self.ctl_frame.columnconfigure(1, weight=1)
        self.v_expnote = tk.StringVar()
        ttk.Label(f, textvariable=self.v_expnote, foreground="#a60", wraplength=440, justify="left").grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _tab_capture(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Съёмка")
        f.columnconfigure(1, weight=1)
        self.v_duration = tk.DoubleVar()
        self._row(f, "Длительность, с", lambda p: ttk.Spinbox(p, textvariable=self.v_duration, from_=0, to=86400, increment=10, width=10), 0,
                  tip="0 = без ограничения. Стоп — по тому, что наступит раньше: время или число кадров.")
        self.v_frames = tk.IntVar()
        self._row(f, "Кадров сохранить", lambda p: ttk.Spinbox(p, textvariable=self.v_frames, from_=0, to=10_000_000, increment=100, width=10), 2,
                  tip="0 = без ограничения.")
        self.v_skip = tk.IntVar()
        self._row(f, "Пропустить первые", lambda p: ttk.Spinbox(p, textvariable=self.v_skip, from_=0, to=1000, width=10), 4,
                  tip="Кадры, пока камера применяет выдержку/фокус (10 хватает).")
        self.v_every = tk.IntVar()
        self._row(f, "Сохранять каждый N-й", lambda p: ttk.Spinbox(p, textvariable=self.v_every, from_=1, to=100000, width=10), 6,
                  tip="1 = все кадры. Больше — таймлапс (при 5 к/с N=25 → кадр раз в 5 с).")
        self.v_series = tk.IntVar()
        self._row(f, "Серий подряд", lambda p: ttk.Spinbox(p, textvariable=self.v_series, from_=1, to=1000, width=10), 8)
        self.v_pause = tk.DoubleVar()
        self._row(f, "Пауза между сериями, с", lambda p: ttk.Spinbox(p, textvariable=self.v_pause, from_=0, to=86400, increment=5, width=10), 9,
                  tip="Для планет: несколько роликов по 60–120 с, потом дерротация в WinJUPOS.")

        ttk.Separator(f).grid(row=11, column=0, columnspan=2, sticky="ew", pady=6)
        self.v_crop_on = tk.BooleanVar()
        ttk.Checkbutton(f, text="Кроп (область кадра) — выделите мышью на превью", variable=self.v_crop_on,
                        command=self.update_estimate).grid(row=12, column=0, columnspan=2, sticky="w")
        cf = ttk.Frame(f)
        cf.grid(row=13, column=0, columnspan=2, sticky="w", pady=2)
        self.v_cx, self.v_cy, self.v_cw, self.v_ch = (tk.IntVar() for _ in range(4))
        for i, (lbl, v) in enumerate((("X", self.v_cx), ("Y", self.v_cy), ("Ш", self.v_cw), ("В", self.v_ch))):
            ttk.Label(cf, text=lbl).grid(row=0, column=i * 2, padx=(6 if i else 0, 2))
            sb = ttk.Spinbox(cf, textvariable=v, from_=0, to=8192, increment=2, width=6, command=self.on_crop_fields)
            sb.grid(row=0, column=i * 2 + 1)
            sb.bind("<FocusOut>", lambda e: self.on_crop_fields())
            sb.bind("<Return>", lambda e: self.on_crop_fields())
        cb = ttk.Frame(f)
        cb.grid(row=14, column=0, columnspan=2, sticky="w", pady=2)
        for s in (256, 400, 512, 640, 800):
            ttk.Button(cb, text=str(s), width=4, command=lambda s=s: self.crop_size(s)).pack(side="left", padx=1)
        ttk.Button(cb, text="Центр", command=self.crop_center).pack(side="left", padx=(6, 1))
        ttk.Button(cb, text="На яркий объект", command=self.crop_to_object).pack(side="left", padx=1)
        ttk.Button(cb, text="Весь кадр", command=self.crop_full).pack(side="left", padx=1)
        self.v_track = tk.BooleanVar()
        ttk.Checkbutton(f, text="Вести кроп за ярким объектом (только SER) — компенсирует уход объекта без монтировки",
                        variable=self.v_track).grid(row=15, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _tab_output(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Вывод")
        f.columnconfigure(1, weight=1)
        self.v_format = tk.StringVar()
        self.cb_format = self._row(f, "Формат", lambda p: ttk.Combobox(p, state="readonly", values=[v[0] for v in FORMATS.values()]), 0)
        self.cb_format.bind("<<ComboboxSelected>>", lambda e: self.on_format())
        self.v_color = tk.StringVar()
        cf = ttk.Frame(f)
        ttk.Radiobutton(cf, text="Только яркость Y (моно)", value="mono", variable=self.v_color, command=self.update_estimate).pack(side="left")
        ttk.Radiobutton(cf, text="Цвет", value="color", variable=self.v_color, command=self.update_estimate).pack(side="left", padx=8)
        ttk.Label(f, text="Каналы").grid(row=1, column=0, sticky="w")
        cf.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="Моно Y из RAW = полное разрешение яркости без артефактов JPEG и цветовой интерполяции, данных в 3 раза меньше. "
                          "Лучший вариант для Луны и звёзд; для цветных планет — «Цвет».",
                  foreground="#777", wraplength=440, justify="left").grid(row=2, column=0, columnspan=2, sticky="w")
        self.v_fullrange = tk.BooleanVar()
        ttk.Checkbutton(f, text="Растянуть 16–235 → 0–255 (RAW YUY2 отдаёт «ТВ-диапазон»)", variable=self.v_fullrange).grid(row=3, column=0, columnspan=2, sticky="w")

        ttk.Separator(f).grid(row=4, column=0, columnspan=2, sticky="ew", pady=6)
        self.v_encoder = tk.StringVar()
        self.cb_encoder = self._row(f, "Кодировщик", lambda p: ttk.Combobox(p, textvariable=self.v_encoder, state="readonly"), 5)
        self.v_qmode = tk.StringVar()
        qf = ttk.Frame(f)
        ttk.Radiobutton(qf, text="Качество (CRF/CQ)", value="quality", variable=self.v_qmode, command=self.update_estimate).pack(side="left")
        ttk.Radiobutton(qf, text="Битрейт", value="bitrate", variable=self.v_qmode, command=self.update_estimate).pack(side="left", padx=8)
        ttk.Label(f, text="Режим").grid(row=6, column=0, sticky="w")
        qf.grid(row=6, column=1, sticky="w")
        self.v_quality = tk.IntVar()
        self._row(f, "Качество", lambda p: ttk.Spinbox(p, textvariable=self.v_quality, from_=0, to=51, width=6, command=self.update_estimate), 7,
                  tip="H.264/H.265: меньше = лучше (14–18 почти без потерь, 23 обычное). JPEG-кадры: 2 (лучшее) … 31.")
        self.v_bitrate = tk.IntVar()
        self._row(f, "Битрейт, кбит/с", lambda p: ttk.Spinbox(p, textvariable=self.v_bitrate, from_=100, to=200000, increment=500, width=8, command=self.update_estimate), 9)
        self.v_ppreset = tk.StringVar()
        self._row(f, "Пресет кодека", lambda p: ttk.Combobox(p, textvariable=self.v_ppreset, state="readonly", width=12,
                                                            values=["ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]), 10)

        ttk.Separator(f).grid(row=11, column=0, columnspan=2, sticky="ew", pady=6)
        self.v_outdir = tk.StringVar()
        of = ttk.Frame(f)
        ttk.Entry(of, textvariable=self.v_outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(of, text="…", width=3, command=self.pick_dir).pack(side="left", padx=2)
        ttk.Button(of, text="Открыть", command=lambda: os.startfile(self.v_outdir.get()) if os.path.isdir(self.v_outdir.get()) else None).pack(side="left")
        ttk.Label(f, text="Папка").grid(row=12, column=0, sticky="w")
        of.grid(row=12, column=1, sticky="ew", pady=2)
        self.v_template = tk.StringVar()
        self._row(f, "Имя файла", lambda p: ttk.Entry(p, textvariable=self.v_template), 13,
                  tip="{target} {utc} (WinJUPOS: 2026-11-16-0230_5, середина записи) {date} {time} {fmt} {size} {exp} {n} (номер серии)")
        self.v_target = tk.StringVar()
        self._row(f, "Объект", lambda p: ttk.Entry(p, textvariable=self.v_target), 15)
        self.v_observer = tk.StringVar()
        self._row(f, "Наблюдатель", lambda p: ttk.Entry(p, textvariable=self.v_observer), 16)
        self.v_telescope = tk.StringVar()
        self._row(f, "Оптика", lambda p: ttk.Entry(p, textvariable=self.v_telescope), 17)
        self.v_notes = tk.StringVar()
        self._row(f, "Заметки", lambda p: ttk.Entry(p, textvariable=self.v_notes), 18)
        tf = ttk.Frame(f)
        tf.grid(row=20, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(tf, text="Лучшие кадры из записи…", command=self.best_frames).pack(side="left")
        ttk.Label(tf, text=" сортирует кадры SER/AVI/MP4 по резкости, лучшие → PNG", foreground="#777").pack(side="left")
        self.v_release = tk.BooleanVar()
        ttk.Checkbutton(f, text="Автоматически забирать камеру у go2rtc и возвращать при выходе",
                        variable=self.v_release).grid(row=19, column=0, columnspan=2, sticky="w", pady=(6, 0))
        for v in (self.v_duration, self.v_frames, self.v_every, self.v_skip, self.v_series, self.v_cw, self.v_ch):
            v.trace_add("write", lambda *a: self.update_estimate())

    # ------------------------------------------------------------------ config <-> UI
    def _apply_cfg_to_ui(self, cfg=None):
        self._loading = True        # no clamping/estimates while fields are half-updated
        try:
            self._apply_cfg_to_ui_inner(cfg)
        finally:
            self._loading = False
        self.update_estimate()

    def _apply_cfg_to_ui_inner(self, cfg=None):
        c = cfg or self.cfg
        g = lambda k: c.get(k, DEFAULTS[k])
        self.v_device.set(g("device"))
        self.v_duration.set(g("duration")); self.v_frames.set(g("frames")); self.v_skip.set(g("skip"))
        self.v_every.set(g("every")); self.v_series.set(g("series")); self.v_pause.set(g("pause"))
        self.v_crop_on.set(g("crop_on"))
        x, y, w, h = g("crop")
        self.v_cx.set(x); self.v_cy.set(y); self.v_cw.set(w); self.v_ch.set(h)
        self.v_track.set(g("track"))
        self.v_color.set(g("color")); self.v_fullrange.set(g("fullrange"))
        fk = g("format") if g("format") in FORMATS else "ser"
        self.cb_format.set(FORMATS[fk][0])
        self.v_qmode.set(g("qmode")); self.v_quality.set(g("quality")); self.v_bitrate.set(g("bitrate")); self.v_ppreset.set(g("preset"))
        self.v_outdir.set(g("outdir")); self.v_template.set(g("template")); self.v_target.set(g("target"))
        self.v_observer.set(g("observer")); self.v_telescope.set(g("telescope")); self.v_notes.set(g("notes"))
        self.v_release.set(g("release_go2rtc"))
        self._want_input, self._want_size, self._want_fps = g("input"), g("size"), str(g("fps"))
        self._want_encoder = g("encoder")
        self.on_format(keep_encoder=True)
        if self.modes:
            self._fill_modes()

    def collect_cfg(self):
        def num(var, typ, default):
            try:
                return typ(var.get())
            except Exception:
                return default
        c = dict(
            device=self.v_device.get(), input=self.v_input.get(), size=self.v_size.get(), fps=self.v_fps.get(),
            duration=num(self.v_duration, float, 0.0), frames=num(self.v_frames, int, 0), skip=num(self.v_skip, int, 0),
            every=max(1, num(self.v_every, int, 1)), series=max(1, num(self.v_series, int, 1)), pause=num(self.v_pause, float, 0.0),
            crop_on=self.v_crop_on.get(), crop=[num(v, int, 0) for v in (self.v_cx, self.v_cy, self.v_cw, self.v_ch)],
            track=self.v_track.get(), color=self.v_color.get(), fullrange=self.v_fullrange.get(),
            format=self.format_key(), encoder=self.encoder_key(), qmode=self.v_qmode.get(),
            quality=num(self.v_quality, int, 18), bitrate=num(self.v_bitrate, int, 8000), preset=self.v_ppreset.get(),
            outdir=self.v_outdir.get(), template=self.v_template.get(), target=self.v_target.get(),
            observer=self.v_observer.get(), telescope=self.v_telescope.get(), notes=self.v_notes.get(),
            controls=self.current_controls(), release_go2rtc=self.v_release.get(),
        )
        return c

    def format_key(self):
        label = self.cb_format.get()
        for k, v in FORMATS.items():
            if v[0] == label:
                return k
        return "ser"

    def encoder_key(self):
        lab = self.v_encoder.get()
        for k, l in ENCODERS.get(self.format_key(), []):
            if l == lab:
                return k
        return ""

    # ------------------------------------------------------------------ devices / modes
    def refresh_devices(self, first=False):
        def work():
            devs = list_devices(self.ffmpeg)
            self.ui(lambda: self._set_devices(devs, first))
        threading.Thread(target=work, daemon=True).start()

    def _set_devices(self, devs, first):
        self.cb_device["values"] = devs
        if self.v_device.get() not in devs and devs:
            self.v_device.set(devs[0])
        self.on_device()
        self.update_go2rtc_label()
        if first and self.v_release.get() and self.go2rtc_uses_device():
            self.logline("go2rtc держит эту камеру — нажмите «Забрать у go2rtc» или просто ▶ Превью (заберу сам).")

    def on_device(self):
        dev = self.v_device.get()
        self.logline(f"Камера: {dev}")

        def work():
            modes = list_modes(self.ffmpeg, dev)
            self.ui(lambda: self._set_modes(modes))
        threading.Thread(target=work, daemon=True).start()
        self.read_controls()

    def _set_modes(self, modes):
        self.modes = modes
        if not modes:
            self.logline("Не удалось получить режимы камеры (занята другим приложением?). Используются сохранённые.")
            self.modes = {self._want_input: {tuple(map(int, self._want_size.split("x"))): (1, float(self._want_fps))}}
        self._fill_modes()

    def _fill_modes(self):
        names = {"yuyv422": "RAW YUY2 (несжатый)", "mjpeg": "MJPEG (сжатие в камере)", "nv12": "RAW NV12", "h264": "H.264 (камера)"}
        vals = [f"{k} — {names.get(k, k)}" for k in self.modes]
        self.cb_input["values"] = vals
        want = getattr(self, "_want_input", None)
        pick = next((v for v in vals if v.split(" ")[0] == want), vals[0] if vals else "")
        self.v_input.set(pick.split(" ")[0])
        self.cb_input.set(pick)
        self.on_input()

    def input_fmt(self):
        return self.cb_input.get().split(" ")[0] if self.cb_input.get() else self.v_input.get()

    def on_input(self):
        fmt = self.input_fmt()
        self.v_input.set(fmt)
        sizes = sorted(self.modes.get(fmt, {}), key=lambda s: s[0] * s[1], reverse=True)
        vals = [f"{w}x{h}" for w, h in sizes]
        self.cb_size["values"] = vals
        want = getattr(self, "_want_size", None)
        self.v_size.set(want if want in vals else (vals[0] if vals else ""))
        self.on_size()

    def on_size(self):
        fmt = self.input_fmt()
        try:
            w, h = map(int, self.v_size.get().split("x"))
        except ValueError:
            return
        lo, hi = self.modes.get(fmt, {}).get((w, h), (1, 30))
        fl = [f for f in STD_FPS if lo - 0.01 <= f <= hi + 0.01] or [hi]
        vals = [f"{f:g}" for f in fl]
        self.cb_fps["values"] = vals
        want = getattr(self, "_want_fps", None)
        self.v_fps.set(want if want in vals else vals[-1])
        self._want_input = self._want_size = self._want_fps = None
        if not getattr(self, "_loading", False):
            self.clamp_crop()
        self.update_estimate()

    def frame_size(self):
        try:
            return tuple(map(int, self.v_size.get().split("x")))
        except ValueError:
            return (1920, 1080)

    def fps(self):
        try:
            return float(self.v_fps.get())
        except ValueError:
            return 5.0

    # ------------------------------------------------------------------ camera controls
    def read_controls(self, apply_after=None):
        dev = self.v_device.get()

        def work():
            try:
                rc, out, err = run([CAMCTL, "get", dev], 15)
                info = json.loads(out)["props"] if rc == 0 else []
            except Exception as e:
                info, err = [], str(e)
            self.ui(lambda: self._build_controls(info, err, apply_after))
        threading.Thread(target=work, daemon=True).start()

    def _build_controls(self, info, err, apply_after):
        for w in self.ctl_frame.winfo_children():
            w.destroy()
        self.ctl_widgets = {}
        self.ctl_info = info
        if not info:
            ttk.Label(self.ctl_frame, text="Нет доступа к настройкам камеры. " + (err or "")[:200], foreground="#a00",
                      wraplength=430).grid(row=0, column=0, columnspan=3)
            return
        order = ["Exposure", "Focus", "Gain", "Brightness", "Contrast", "Gamma", "Saturation", "Sharpness", "WhiteBalance",
                 "BacklightCompensation", "Hue", "Zoom", "Pan", "Tilt"]
        info.sort(key=lambda p: order.index(p["name"]) if p["name"] in order else 99)
        names_ru = {"Exposure": "Выдержка", "Focus": "Фокус", "Gain": "Усиление", "Brightness": "Яркость", "Contrast": "Контраст",
                    "Gamma": "Гамма", "Saturation": "Насыщенность", "Sharpness": "Резкость", "WhiteBalance": "Баланс белого",
                    "BacklightCompensation": "Компенс. засветки", "Hue": "Оттенок", "Zoom": "Цифр. зум", "Pan": "Пан", "Tilt": "Наклон"}
        for i, p in enumerate(info):
            nm = p["name"]
            ttk.Label(self.ctl_frame, text=names_ru.get(nm, nm)).grid(row=i, column=0, sticky="w")
            var = tk.IntVar(value=p["value"])
            auto = tk.BooleanVar(value=p["auto"])
            lbl = ttk.Label(self.ctl_frame, width=16)
            sc = ttk.Scale(self.ctl_frame, from_=p["min"], to=p["max"], orient="horizontal",
                           command=lambda v, n=nm: self.on_ctl_slide(n, v))
            sc.set(p["value"])
            sc.grid(row=i, column=1, sticky="ew", padx=4)
            lbl.grid(row=i, column=2, sticky="w")
            if p["caps"] & 1:
                ttk.Checkbutton(self.ctl_frame, text="авто", variable=auto, command=lambda n=nm: self.on_ctl_auto(n)).grid(row=i, column=3)
            self.ctl_widgets[nm] = dict(var=var, auto=auto, scale=sc, label=lbl, info=p)
            self._ctl_label(nm)
        self.update_exp_note()
        if apply_after:
            self.apply_controls(apply_after)

    def _ctl_label(self, nm):
        w = self.ctl_widgets[nm]
        v = w["var"].get()
        txt = exposure_text(v) if nm == "Exposure" else str(v)
        if w["auto"].get():
            txt = "авто"
        w["label"].config(text=txt)
        if nm == "Exposure":
            self.update_exp_note()

    def on_ctl_slide(self, nm, v):
        w = self.ctl_widgets.get(nm)
        if not w:
            return
        iv = int(round(float(v)))
        if iv == w["var"].get() and not w["auto"].get():
            return
        w["var"].set(iv)
        w["auto"].set(False)
        self._ctl_label(nm)
        self.queue_ctl(nm, f"{iv}:manual")

    def on_ctl_auto(self, nm):
        w = self.ctl_widgets[nm]
        self._ctl_label(nm)
        self.queue_ctl(nm, "auto" if w["auto"].get() else f"{w['var'].get()}:manual")

    def queue_ctl(self, nm, spec):
        self.ctl_pending[nm] = spec
        if self.ctl_after:
            self.root.after_cancel(self.ctl_after)
        self.ctl_after = self.root.after(150, self._flush_ctl)

    def _flush_ctl(self):
        self.ctl_after = None
        specs = [f"{k}={v}" for k, v in self.ctl_pending.items()]
        self.ctl_pending = {}
        if specs:
            dev = self.v_device.get()
            threading.Thread(target=lambda: self._camctl_set(dev, specs), daemon=True).start()

    def _camctl_set(self, dev, specs):
        try:
            rc, out, err = run([CAMCTL, "set", dev] + specs, 15)
            if rc != 0:
                self.logline(f"camctl: {err.strip()[:300]}")
        except Exception as e:
            self.logline(f"camctl: {e}")

    def current_controls(self):
        return {nm: [w["var"].get(), bool(w["auto"].get())] for nm, w in self.ctl_widgets.items()}

    def apply_controls(self, controls, sync=False):
        specs = []
        for nm, (val, auto) in controls.items():
            w = self.ctl_widgets.get(nm)
            if w:
                w["var"].set(val); w["auto"].set(auto); w["scale"].set(val)
                self._ctl_label(nm)
            specs.append(f"{nm}=" + ("auto" if auto else f"{val}:manual"))
        if specs:
            dev = self.v_device.get()
            if sync:
                self._camctl_set(dev, specs)
            else:
                threading.Thread(target=lambda: self._camctl_set(dev, specs), daemon=True).start()

    def astro_controls(self):
        c = {"Sharpness": [0, False], "BacklightCompensation": [0, False], "Exposure": [self.ctl_widgets.get("Exposure", {}).get("var", tk.IntVar(value=-6)).get(), False]}
        if "WhiteBalance" in self.ctl_widgets:
            c["WhiteBalance"] = [self.ctl_widgets["WhiteBalance"]["var"].get(), False]
        if "Focus" in self.ctl_widgets:
            c["Focus"] = [self.ctl_widgets["Focus"]["var"].get(), False]
        self.apply_controls(c)
        self.logline("Астро-база: резкость 0 (иначе ореолы), компенсация засветки 0, ручные выдержка/фокус/баланс белого.")

    def default_controls(self):
        self.apply_controls({p["name"]: [p["default"], bool(p["caps"] & 1)] for p in self.ctl_info if p["name"] not in ("Pan", "Tilt", "Zoom")}
                            | {n: [0, False] for n in ("Pan", "Tilt", "Zoom") if n in self.ctl_widgets})

    def lock_auto(self):
        """let the camera find exposure/focus/WB automatically, then freeze those values as manual ones"""
        dev = self.v_device.get()
        names = [p["name"] for p in self.ctl_info if p["caps"] & 1 and p["name"] in ("Exposure", "Focus", "WhiteBalance")]
        if not names:
            return
        if self.mode == "idle":
            self.logline("«Зафиксировать авто»: запустите превью — камера подстраивается только во время потока.")
            return
        self.logline("Автонастройка… (4 с, держите объект в кадре)")

        def work():
            self._camctl_set(dev, [f"{n}=auto" for n in names])
            time.sleep(4)
            rc, out, err = run([CAMCTL, "get", dev], 15)
            if rc != 0:
                self.logline("camctl: " + err.strip()[:200])
                return
            vals = {p["name"]: p["value"] for p in json.loads(out)["props"]}
            self._camctl_set(dev, [f"{n}={vals[n]}:manual" for n in names if n in vals])
            self.logline("Зафиксировано: " + ", ".join(
                f"{n}={exposure_text(vals[n]) if n == 'Exposure' else vals[n]}" for n in names if n in vals))
            self.ui(self.read_controls)
        threading.Thread(target=work, daemon=True).start()

    def driver_dialog(self):
        dev = self.v_device.get()
        threading.Thread(target=lambda: run([CAMCTL, "dialog", dev], 3600), daemon=True).start()
        self.root.after(1500, lambda: None)

    def update_exp_note(self):
        w = self.ctl_widgets.get("Exposure")
        if not w:
            self.v_expnote.set("")
            return
        if w["auto"].get():
            self.v_expnote.set("Выдержка авто — для астро ставьте вручную: камера иначе пересвечивает Луну и «съедает» звёзды.")
            return
        exp = 2.0 ** w["var"].get()
        per = 1.0 / self.fps()
        if exp > per * 1.05:
            self.v_expnote.set(f"Выдержка {exposure_text(w['var'].get())} длиннее кадра при {self.fps():g} к/с ({per * 1000:.0f} мс): "
                               "камера либо укоротит выдержку до длины кадра, либо снизит частоту кадров (LifeCam в MJPEG так и делает). "
                               "Для максимальной выдержки ставьте минимальный fps.")
        else:
            self.v_expnote.set("")

    # ------------------------------------------------------------------ crop
    def clamp_crop(self):
        W, H = self.frame_size()
        w = max(16, min(W, self.v_cw.get() or W)) & ~1
        h = max(16, min(H, self.v_ch.get() or H)) & ~1
        x = max(0, min(W - w, self.v_cx.get())) & ~1
        y = max(0, min(H - h, self.v_cy.get())) & ~1
        for v, val in ((self.v_cx, x), (self.v_cy, y), (self.v_cw, w), (self.v_ch, h)):
            if v.get() != val:
                v.set(val)

    def crop_rect(self):
        """effective crop (x, y, w, h) in sensor pixels"""
        W, H = self.frame_size()
        if not self.v_crop_on.get():
            return (0, 0, W, H)
        try:
            self.clamp_crop()
        except tk.TclError:
            pass
        return (self.v_cx.get(), self.v_cy.get(), self.v_cw.get(), self.v_ch.get())

    def on_crop_fields(self):
        try:
            self.clamp_crop()
        except tk.TclError:
            return
        self.v_crop_on.set(True)
        self.shown_seq = -1
        self.update_estimate()

    def crop_size(self, s):
        W, H = self.frame_size()
        x, y, w, h = self.crop_rect() if self.v_crop_on.get() else (W // 2, H // 2, 0, 0)
        cx, cy = x + w // 2, y + h // 2
        s = min(s, W, H)
        self.v_cw.set(s); self.v_ch.set(s)
        self.v_cx.set(cx - s // 2); self.v_cy.set(cy - s // 2)
        self.on_crop_fields()

    def crop_center(self):
        W, H = self.frame_size()
        _, _, w, h = self.crop_rect()
        if not self.v_crop_on.get():
            w, h = min(W, H) // 2, min(W, H) // 2
            self.v_cw.set(w); self.v_ch.set(h)
        self.v_cx.set((W - w) // 2); self.v_cy.set((H - h) // 2)
        self.on_crop_fields()

    def crop_full(self):
        self.v_crop_on.set(False)
        self.shown_seq = -1
        self.update_estimate()

    def crop_to_object(self):
        if self.latest is None or not self.latest[1]:
            messagebox.showinfo("AstroCam", "Нужно запущенное превью (полный кадр).")
            return
        c = find_object(luma(self.latest[0]))
        if c is None:
            messagebox.showinfo("AstroCam", "Яркий объект не найден.")
            return
        W, H = self.frame_size()
        if not self.v_crop_on.get() or not self.v_cw.get():
            self.v_cw.set(400); self.v_ch.set(400)
        w, h = self.v_cw.get(), self.v_ch.get()
        self.v_cx.set(int(c[0] - w / 2)); self.v_cy.set(int(c[1] - h / 2))
        self.on_crop_fields()

    # mouse on preview
    def canvas_to_sensor(self, ex, ey):
        if not self.disp:
            return None
        s, ox, oy, fx, fy = self.disp
        return (fx + (ex - ox) / s, fy + (ey - oy) / s)

    def on_press(self, e):
        if self.mode == "capture" or not self.latest or not self.latest[1]:
            return
        p = self.canvas_to_sensor(e.x, e.y)
        if p:
            self.drag = p

    def on_drag(self, e):
        if not self.drag:
            return
        p = self.canvas_to_sensor(e.x, e.y)
        if not p or not self.disp:
            return
        s, ox, oy, fx, fy = self.disp
        self.canvas.delete("drag")
        x0, y0 = self.drag
        self.canvas.create_rectangle(ox + (x0 - fx) * s, oy + (y0 - fy) * s, e.x, e.y, outline="#ff0", dash=(4, 2), tags="drag")

    def on_release(self, e):
        if not self.drag:
            return
        p = self.canvas_to_sensor(e.x, e.y)
        x0, y0 = self.drag
        self.drag = None
        self.canvas.delete("drag")
        if not p:
            return
        x1, y1 = p
        x, y, w, h = int(min(x0, x1)), int(min(y0, y1)), int(abs(x1 - x0)), int(abs(y1 - y0))
        if w < 16 or h < 16:
            return
        self.v_cx.set(x); self.v_cy.set(y); self.v_cw.set(w); self.v_ch.set(h)
        self.on_crop_fields()

    # ------------------------------------------------------------------ format / estimate
    def on_format(self, keep_encoder=False):
        fk = self.format_key()
        encs = ENCODERS.get(fk, [])
        self.cb_encoder["values"] = [l for _, l in encs]
        want = getattr(self, "_want_encoder", "") if keep_encoder else ""
        lab = next((l for k, l in encs if k == want), encs[0][1] if encs else "")
        self.v_encoder.set(lab)
        self.cb_encoder.config(state="readonly" if encs else "disabled")
        self.update_estimate()

    def output_frame_bytes(self, cfg=None):
        x, y, w, h = self.crop_rect()
        planes = 1 if self.v_color.get() == "mono" else 3
        return w * h * planes

    def update_estimate(self):
        if getattr(self, "_loading", False):
            return
        try:
            fk = self.format_key()
            fps = self.fps()
            dur = float(self.v_duration.get() or 0)
            nfr = int(self.v_frames.get() or 0)
            every = max(1, int(self.v_every.get() or 1))
            series = max(1, int(self.v_series.get() or 1))
        except (tk.TclError, ValueError):
            return
        W, H = self.frame_size()
        fmt = self.input_fmt()
        raw_mb = W * H * 2 * fps / 1e6 if fmt == "yuyv422" else None
        self.v_bw.set(f"Поток с камеры: {W}×{H} @ {fps:g} к/с" + (f", {raw_mb:.0f} МБ/с по USB (предел USB 2.0 ≈ 35–40)" if raw_mb else ""))
        frames_by_t = dur * fps / every if dur > 0 else math.inf
        n = min(frames_by_t, nfr if nfr > 0 else math.inf)
        x, y, w, h = self.crop_rect()
        per = self.output_frame_bytes()
        kind = FORMATS[fk][1]
        if fk == "mkv_ffv1":
            per *= 0.5
        elif fk in ("seq_png",):
            per *= 0.6
        elif fk in ("seq_jpg", "mjpeg_copy"):
            per = w * h * 0.25
        elif kind == "video" and fk.startswith("mp4"):
            per = None
        parts = [f"Сохраняется: {w}×{h}, {'моно Y' if self.v_color.get() == 'mono' else 'цвет'}"]
        if fk == "mjpeg_copy":
            parts = [f"Сохраняется: {W}×{H} MJPEG как есть (кроп и моно недоступны)"]
        if n is math.inf:
            parts.append("без ограничения — стоп вручную")
        else:
            secs = n * every / fps
            parts.append(f"≈ {int(n)} кадров за {secs:.0f} с" + (f" × {series} серий" if series > 1 else ""))
            if per:
                parts.append(f"≈ {human_bytes(per * n * series)} ({human_bytes(per * fps / every)}/с)")
            elif self.v_qmode.get() == "bitrate":
                parts.append(f"≈ {human_bytes(self.v_bitrate.get() * 125 * secs * series)}")
        try:
            free = shutil.disk_usage(self.v_outdir.get() if os.path.isdir(self.v_outdir.get()) else os.path.splitdrive(self.v_outdir.get())[0] + "\\").free
            parts.append(f"свободно {human_bytes(free)}")
        except Exception:
            pass
        self.v_est.set("; ".join(parts))
        self.update_exp_note()

    # ------------------------------------------------------------------ go2rtc
    def go2rtc_uses_device(self):
        dev = self.v_device.get()
        try:
            with open(GO2RTC_YAML, encoding="utf-8") as f:
                for line in f:
                    if not line.lstrip().startswith("#") and dev in line:
                        return True
        except OSError:
            pass
        return False

    def go2rtc_released(self):
        return os.path.exists(GO2RTC_YAML + ".astrocam-orig")

    def update_go2rtc_label(self):
        if not os.path.exists(GO2RTC_YAML):
            self.btn_g2r.pack_forget(); self.lbl_g2r.config(text="")
            return
        if self.go2rtc_released():
            self.lbl_g2r.config(text="go2rtc: камера забрана для съёмки")
            self.btn_g2r.config(text="Вернуть камеру go2rtc")
        elif self.go2rtc_uses_device():
            self.lbl_g2r.config(text="go2rtc держит эту камеру")
            self.btn_g2r.config(text="Забрать у go2rtc")
        else:
            self.lbl_g2r.config(text="")
            self.btn_g2r.config(text="go2rtc: камера свободна")

    def toggle_go2rtc(self):
        if self.go2rtc_released():
            if self.mode != "idle":
                self.stop_all()
            self.go2rtc_action("restore")
        elif self.go2rtc_uses_device():
            self.go2rtc_action("release")

    def go2rtc_action(self, action, then=None):
        """Runs go2rtc_ctl.ps1 elevated (UAC). then() called on the Tk thread after success."""
        dev = self.v_device.get()
        b64 = base64.b64encode(dev.encode("utf-8")).decode()
        out = os.path.join(tempfile.gettempdir(), f"astrocam_g2r_{os.getpid()}.txt")
        inner = f"& '{G2R_CTL}' -Action {action} -DeviceB64 {b64} *>&1 | Out-File -Encoding utf8 '{out}'"
        outer = ("Start-Process pwsh -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                 f"'-NoProfile','-ExecutionPolicy','Bypass','-Command',\"{inner.replace(chr(34), chr(96) + chr(34))}\"")
        self.logline(f"go2rtc: {action} (нужно подтверждение UAC)…")
        self.btn_g2r.config(state="disabled")

        def work():
            try:
                saved = self._load_json(G2R_CONTROLS, {})
                if action == "release" and dev not in saved:
                    # remember the surveillance settings so they can be put back for go2rtc/Frigate
                    rc, o, e = run([CAMCTL, "get", dev], 15)
                    if rc == 0:
                        saved[dev] = {p["name"]: [p["value"], p["auto"]] for p in json.loads(o)["props"]}
                        with open(G2R_CONTROLS, "w", encoding="utf-8") as f:
                            json.dump(saved, f, ensure_ascii=False, indent=2)
                if action == "restore" and dev in saved:
                    self._camctl_set(dev, controls_specs(saved.pop(dev)))
                    with open(G2R_CONTROLS, "w", encoding="utf-8") as f:
                        json.dump(saved, f, ensure_ascii=False, indent=2)
                    self.logline("Настройки камеры возвращены к тем, что были у go2rtc")
                if os.path.exists(out):
                    os.remove(out)
                run(["pwsh", "-NoProfile", "-Command", outer], 120)
                msg = open(out, encoding="utf-8-sig").read().strip() if os.path.exists(out) else "нет ответа (UAC отменён?)"
            except Exception as e:
                msg = f"ошибка: {e}"
            ok = msg.startswith("Released") or msg.startswith("Restored") or msg.startswith("No go2rtc") or msg.startswith("Nothing")
            if action == "release" and msg.startswith("Released"):
                self.released_by_us = True
                time.sleep(2.5)  # let the old ffmpeg die and the device settle
            if action == "restore" and ok:
                self.released_by_us = False

            def done():
                self.logline("go2rtc: " + msg)
                self.btn_g2r.config(state="normal")
                self.update_go2rtc_label()
                if then and ok:
                    then()
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    def ensure_camera_free(self, then):
        if self.v_release.get() and not self.go2rtc_released() and self.go2rtc_uses_device():
            self.go2rtc_action("release", then)
        else:
            then()

    # ------------------------------------------------------------------ ffmpeg command building
    def input_args(self, cfg):
        a = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-progress", "pipe:2", "-stats_period", "0.5",
             "-f", "dshow", "-rtbufsize", "1500M", "-thread_queue_size", "512"]
        if cfg["input"] in ("mjpeg", "h264"):
            a += ["-vcodec", cfg["input"]]
        else:
            a += ["-pixel_format", cfg["input"]]
        a += ["-video_size", cfg["size"], "-framerate", str(cfg["fps"]), "-i", "video=" + cfg["device"]]
        return a

    def preview_cmd(self, cfg):
        W, H = map(int, cfg["size"].split("x"))
        f = float(cfg["fps"])
        vf = ("fps=10," if f > 10 else "") + "format=rgb24"
        cmd = self.input_args(cfg) + ["-an", "-vf", vf, "-sws_flags", "fast_bilinear", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        return cmd, W * H * 3

    def ser_cmd(self, cfg):
        """full frames to stdout; Python crops/tracks/writes SER"""
        W, H = map(int, cfg["size"].split("x"))
        if cfg["color"] == "mono":
            vf = planar_prefix(cfg) + "extractplanes=y"
            if cfg["fullrange"] and cfg["input"] != "mjpeg":
                vf += ",lut=c0='clip((val-16)*255/219,0,255)'"
            pix, planes = "gray", 1
        else:
            vf = "scale=in_range=" + ("pc" if cfg["input"] == "mjpeg" or cfg["fullrange"] else "tv") + ":out_range=pc,format=rgb24"
            pix, planes = "rgb24", 3
        cmd = self.input_args(cfg) + ["-an", "-vf", vf, "-sws_flags", "accurate_rnd+full_chroma_int+bitexact",
                                      "-f", "rawvideo", "-pix_fmt", pix, "pipe:1"]
        return cmd, W * H * planes, planes

    def file_cmd(self, cfg, path, x, y, w, h):
        """ffmpeg writes the file itself; a 5 fps preview of the crop comes back on stdout"""
        fk = cfg["format"]
        kind = FORMATS[fk][1]
        fps = float(cfg["fps"])
        dur = float(cfg["duration"] or 0)
        cmd = self.input_args(cfg)
        if dur > 0:
            i = cmd.index("-i")
            cmd[i:i] = ["-t", f"{dur + cfg['skip'] / fps:.3f}"]
        pw, ph = (w, h)
        if kind == "copy":
            cmd += ["-an", "-map", "0:v", "-c:v", "copy"]
            if cfg["frames"] > 0:
                cmd += ["-frames:v", str(cfg["frames"])]
            cmd += ["-y", path]
            return cmd, 0
        chain = []
        if (x, y, w, h) != (0, 0) + tuple(map(int, cfg["size"].split("x"))):
            chain.append(f"crop={w}:{h}:{x}:{y}")
        sel = []
        if cfg["skip"] > 0:
            sel.append(f"gte(n\\,{cfg['skip']})")
        if cfg["every"] > 1:
            sel.append(f"not(mod(n-{cfg['skip']}\\,{cfg['every']}))")
        if cfg["frames"] > 0:
            # stop the whole graph (record + preview branch) after the last needed input frame
            chain.append(f"trim=end_frame={cfg['skip'] + (cfg['frames'] - 1) * cfg['every'] + 1}")
        pre = ",".join(chain) or "null"
        rec = []
        if sel:
            rec.append("select='" + "*".join(sel) + "'")
            rec.append(f"setpts=N/({fps:g}*TB)")
        mono = cfg["color"] == "mono" and kind != "copy"
        tvsrc = cfg["input"] != "mjpeg"
        if mono:
            rec.append(planar_prefix(cfg) + "extractplanes=y")
            if cfg["fullrange"] and tvsrc:
                rec.append("lut=c0='clip((val-16)*255/219,0,255)'")
        enc = []
        if fk == "avi_raw":
            enc = ["-c:v", "rawvideo", "-pix_fmt", "gray" if mono else "bgr24", "-r", f"{fps:g}"]
        elif fk == "mkv_ffv1":
            enc = ["-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4", "-slicecrc", "1"]
            enc += ["-pix_fmt", "gray" if mono else "yuv422p"]   # keep the camera's 4:2:2, no RGB detour
            enc += ["-r", f"{fps:g}"]
        elif fk.startswith("mp4"):
            codec = cfg["encoder"] or ENCODERS[fk][0][0]
            q, br = int(cfg["quality"]), int(cfg["bitrate"])
            enc = ["-c:v", codec]
            if mono:
                rec.append("format=yuv420p")  # grey in a normal 4:2:0 stream, plays everywhere
            pixf = "nv12" if ("qsv" in codec or "nvenc" in codec) else "yuv420p"
            enc += ["-pix_fmt", pixf]
            if codec.startswith("lib"):
                enc += ["-preset", cfg["preset"] or "medium"]
                enc += (["-crf", str(q)] if cfg["qmode"] == "quality" else ["-b:v", f"{br}k", "-maxrate", f"{br * 3 // 2}k", "-bufsize", f"{br * 2}k"])
            elif "qsv" in codec:
                enc += ["-preset", "slow"]
                enc += (["-global_quality", str(q)] if cfg["qmode"] == "quality" else ["-b:v", f"{br}k", "-maxrate", f"{br * 3 // 2}k"])
            else:  # nvenc
                enc += ["-preset", "p6", "-tune", "hq"]
                enc += (["-rc", "vbr", "-cq", str(q), "-b:v", "0"] if cfg["qmode"] == "quality" else ["-rc", "vbr", "-b:v", f"{br}k", "-maxrate", f"{br * 3 // 2}k"])
            if fk == "mp4_hevc":
                enc += ["-tag:v", "hvc1"]
            enc += ["-movflags", "+faststart", "-r", f"{fps:g}"]
        elif fk == "seq_tif":
            enc = ["-c:v", "tiff", "-compression_algo", "deflate", "-pix_fmt", "gray" if mono else "rgb24"]
        elif fk == "seq_png":
            enc = ["-c:v", "png", "-pix_fmt", "gray" if mono else "rgb24"]
        elif fk == "seq_fits":
            enc = ["-c:v", "fits", "-pix_fmt", "gray" if mono else "gbrp"]
        elif fk == "seq_jpg":
            enc = ["-c:v", "mjpeg", "-q:v", str(max(2, min(31, int(cfg["quality"])))), "-pix_fmt", "yuvj444p"]
        if kind == "seq":
            enc += ["-f", "image2", "-start_number", "1", "-r", f"{fps:g}"]
        recf = ",".join(rec) or "null"
        fc = f"[0:v]{pre},split=2[a][b];[a]{recf}[rec];[b]fps=5,format=rgb24[pv]"
        cmd += ["-filter_complex", fc, "-map", "[rec]", "-an"] + enc
        if cfg["frames"] > 0:
            cmd += ["-frames:v", str(cfg["frames"])]
        cmd += ["-y", path, "-map", "[pv]", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        return cmd, pw * ph * 3

    # ------------------------------------------------------------------ preview
    def toggle_preview(self):
        if self.mode == "preview":
            self.stop_all()
        elif self.mode == "idle":
            self.ensure_camera_free(self.start_preview)

    def start_preview(self):
        cfg = self.collect_cfg()
        self.apply_controls(cfg["controls"], sync=True)
        cmd, fb = self.preview_cmd(cfg)
        W, H = map(int, cfg["size"].split("x"))

        def on_frame(b, t):
            self.latest = (np.frombuffer(b, np.uint8).reshape(H, W, 3), True, (0, 0))
            self.latest_seq += 1
            self.fps_meter.append(t)
        self.stream = FFStream(cmd, fb, on_frame, self.logline, lambda rc: self.ui(lambda: self.on_stream_exit(rc)))
        self.mode = "preview"
        self.focus_best = 0
        self.stream.start()
        self.update_buttons()

    def stop_all(self):
        self.stop_req = True
        if self.stream:
            self.stream.stop(graceful=self.mode == "capture")
        self.stream = None

    def on_stream_exit(self, rc):
        if self.mode == "preview":
            self.mode = "idle"
            if rc not in (0, None) and not self.stop_req:
                self.logline(f"Превью завершилось с кодом {rc} (камера занята?)")
            self.stop_req = False
            self.update_buttons()

    def update_buttons(self):
        self.btn_preview.config(text="■ Стоп превью" if self.mode == "preview" else "▶ Превью",
                                state="disabled" if self.mode == "capture" else "normal")
        self.btn_rec.config(text="■ Стоп записи" if self.mode == "capture" else "● Запись")

    # ------------------------------------------------------------------ capture
    def toggle_capture(self):
        if self.mode == "capture":
            self.stop_req = True
            if self.cap:
                self.cap["abort"] = True
            if self.stream:
                threading.Thread(target=self.stream.stop, daemon=True).start()
            return
        cfg = self.collect_cfg()
        if cfg["format"] == "mjpeg_copy" and cfg["input"] != "mjpeg":
            messagebox.showwarning("AstroCam", "«MJPEG как есть» требует поток MJPEG с камеры.")
            return
        try:
            os.makedirs(cfg["outdir"], exist_ok=True)
        except OSError as e:
            messagebox.showerror("AstroCam", f"Папка вывода: {e}")
            return
        if cfg["duration"] <= 0 and cfg["frames"] <= 0 and cfg["series"] > 1:
            messagebox.showwarning("AstroCam", "Для нескольких серий задайте длительность или число кадров.")
            return
        self._save_settings()
        was_preview = self.mode == "preview"
        if was_preview:
            s = self.stream
            self.stream = None
            self.mode = "idle"
            s.stop(graceful=False)
            time.sleep(0.5)
        self.ensure_camera_free(lambda: self.start_capture(cfg))

    def start_capture(self, cfg):
        self.mode = "capture"
        self.stop_req = False
        self.cap = dict(cfg=cfg, series_i=0, abort=False, written=0, t0=time.time(), files=[], bytes=0)
        self.update_buttons()
        threading.Thread(target=self.capture_thread, daemon=True).start()

    def capture_thread(self):
        cfg = self.cap["cfg"]
        try:
            self._camctl_set(cfg["device"], controls_specs(cfg["controls"]))
            for si in range(cfg["series"]):
                if self.cap["abort"]:
                    break
                self.cap["series_i"] = si
                self.capture_one(cfg, si + 1)
                if self.cap["abort"] or si == cfg["series"] - 1:
                    break
                t_end = time.time() + cfg["pause"]
                while time.time() < t_end and not self.cap["abort"]:
                    self.cap["status"] = f"пауза перед серией {si + 2}: {t_end - time.time():.0f} с"
                    time.sleep(0.2)
        except Exception as e:
            self.logline(f"Ошибка записи: {e!r}")
        finally:
            self.ui(self.capture_finished)

    def make_name(self, cfg, n, t, ext):
        d = dt.datetime.fromtimestamp(t)
        exp = ""
        e = cfg["controls"].get("Exposure")
        if e and not e[1]:
            exp = f"exp{2.0 ** e[0] * 1000:g}ms"
        vals = dict(target=cfg["target"] or "obj", utc=winjupos_time(t), date=d.strftime("%Y-%m-%d"), time=d.strftime("%H%M%S"),
                    fmt=cfg["format"], size=cfg["size"], exp=exp, n=f"{n:02d}")
        try:
            name = cfg["template"].format(**vals)
        except (KeyError, IndexError, ValueError):
            name = "{target}_{utc}".format(**vals)
        if cfg["series"] > 1 and "{n}" not in cfg["template"]:
            name += f"_{n:02d}"
        name = re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "capture"
        base = os.path.join(cfg["outdir"], name)
        p, k = base + ext, 1
        while os.path.exists(p):
            k += 1
            p = f"{base}-{k}{ext}"
        return p

    def capture_one(self, cfg, n):
        fk = cfg["format"]
        kind = FORMATS[fk][1]
        ext = FORMATS[fk][2]
        fps = float(cfg["fps"])
        W, H = map(int, cfg["size"].split("x"))
        x, y, w, h = cfg["crop"] if cfg["crop_on"] else (0, 0, W, H)
        if kind == "copy":
            x, y, w, h = 0, 0, W, H
        t_start = time.time()
        if kind == "seq":
            folder = self.make_name(cfg, n, t_start, "")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "frame_%05d" + ext)
            final_obj = folder
        else:
            path = self.make_name(cfg, n, t_start, ext)
            final_obj = path
        st = dict(written=0, seen=0, t_first=None, t_last=None, done=threading.Event(), rc=None, track=None, err=None)
        self.cap.update(cur=st, cur_path=final_obj, status="запись", t_series=t_start)
        self.logline(f"Серия {n}/{cfg['series']}: {final_obj}")

        if kind == "ser":
            cmd, fb, planes = self.ser_cmd(cfg)
            ser = SerWriter(path, w, h, "rgb" if planes == 3 else "mono", cfg["observer"], "LifeCam " + cfg["size"], cfg["telescope"])
            limit_frames = cfg["frames"] if cfg["frames"] > 0 else None
            dur = cfg["duration"] if cfg["duration"] > 0 else None
            tracker = Tracker(x, y, w, h, W, H) if cfg["track"] and cfg["crop_on"] else None

            def on_frame(b, t):
                if st["done"].is_set():
                    return
                st["seen"] += 1
                img = np.frombuffer(b, np.uint8).reshape((H, W, 3) if planes == 3 else (H, W))
                if st["seen"] <= cfg["skip"]:
                    self.latest = (img, True, (0, 0)); self.latest_seq += 1
                    return
                k = st["seen"] - cfg["skip"] - 1
                if st["t_first"] is None:
                    st["t_first"] = t
                if dur and t - st["t_first"] >= dur - 0.02:
                    st["done"].set(); return
                cx, cy = (tracker.update(img) if tracker else (x, y))
                st["track"] = (cx, cy)
                if k % cfg["every"] == 0:
                    sub = img[cy:cy + h, cx:cx + w]
                    ser.write(np.ascontiguousarray(sub).tobytes(), t)
                    st["written"] += 1
                    st["t_last"] = t
                    self.latest = (sub, False, (cx, cy)); self.latest_seq += 1
                    self.fps_meter.append(t)
                    if limit_frames and st["written"] >= limit_frames:
                        st["done"].set()
            stream = FFStream(cmd, fb, on_frame, self.logline, lambda rc: (st.__setitem__("rc", rc), st["done"].set()))
            self.stream = stream
            stream.start()
            while not st["done"].wait(0.2):
                if self.cap["abort"]:
                    break
            stream.stop(graceful=False)
            time.sleep(0.2)
            nfr = ser.close()
            st["written"] = nfr
            drops = stream.drops
        else:
            cmd, fb = self.file_cmd(cfg, path, x, y, w, h)

            def on_frame(b, t):
                self.latest = (np.frombuffer(b, np.uint8).reshape(h, w, 3), False, (x, y)); self.latest_seq += 1
            stream = FFStream(cmd, fb, on_frame, self.logline, lambda rc: (st.__setitem__("rc", rc), st["done"].set()))
            self.stream = stream
            st["t_first"] = time.time() + cfg["skip"] / fps
            stream.start()
            while not st["done"].wait(0.3):
                try:
                    st["written"] = int(stream.progress.get("frame", "0") or 0)
                except ValueError:
                    pass
                if self.cap["abort"]:
                    stream.stop(graceful=True)
                    break
            st["done"].wait(10)
            try:
                st["written"] = int(stream.progress.get("frame", st["written"]) or st["written"])
            except ValueError:
                pass
            st["t_last"] = time.time()
            drops = stream.drops
            if kind == "seq":
                try:
                    st["written"] = len([f for f in os.listdir(final_obj) if f.endswith(ext)])
                except OSError:
                    pass
        self.stream = None
        t0, t1 = st["t_first"] or t_start, st["t_last"] or time.time()
        mid = (t0 + t1) / 2
        # rename so {utc} is the mid-time (WinJUPOS convention)
        if "{utc}" in cfg["template"] and st["written"] and winjupos_time(mid) != winjupos_time(t_start):
            want = self.make_name(cfg, n, mid, ext if kind != "seq" else "")
            if True:
                try:
                    os.replace(final_obj, want)
                    final_obj = want
                except OSError:
                    pass
        size = dir_size(final_obj)
        self.cap["bytes"] += size
        self.cap["written"] += st["written"]
        self.cap["files"].append(final_obj)
        real_fps = (st["written"] - 1) / (t1 - t0) if st["written"] > 1 and t1 > t0 else 0
        if kind != "ser":
            try:
                real_fps = float(stream.progress.get("fps", real_fps)) * cfg["every"]
            except ValueError:
                pass
        self.write_sidecar(cfg, final_obj, n, t0, t1, mid, st["written"], drops, real_fps, (x, y, w, h), st)
        self.logline(f"✔ {os.path.basename(final_obj)}: {st['written']} кадров, {human_bytes(size)}, "
                     f"{real_fps:.2f} к/с факт., потерь ffmpeg: {drops}" + (f", код {st['rc']}" if st["rc"] not in (0, None, 255) else ""))

    def write_sidecar(self, cfg, path, n, t0, t1, mid, frames, drops, real_fps, rect, st):
        base = path if os.path.isdir(path) else os.path.splitext(path)[0]
        ctl = {k: (("auto" if a else (exposure_text(v) + f" (log2={v})" if k == "Exposure" else v))) for k, (v, a) in cfg["controls"].items()}
        utc = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"
        lines = [
            "AstroCam capture log",
            f"File          : {os.path.basename(path)}",
            f"Target        : {cfg['target']}",
            f"Camera        : {cfg['device']}",
            f"Input         : {cfg['input']} {cfg['size']} @ {cfg['fps']} fps",
            f"Saved region  : x={rect[0]} y={rect[1]} w={rect[2]} h={rect[3]}" + ("  (tracked)" if cfg["track"] and cfg["format"] == "ser" else ""),
            f"Output        : {FORMATS[cfg['format']][0]}, {'mono Y' if cfg['color'] == 'mono' else 'color'}"
            + (", 16-235 stretched" if cfg["fullrange"] else ""),
            f"Series        : {n}/{cfg['series']}",
            f"Start         : {utc(t0)}",
            f"Mid           : {utc(mid)}",
            f"End           : {utc(t1)}",
            f"Duration      : {t1 - t0:.2f} s",
            f"Frames        : {frames} (skip first {cfg['skip']}, every {cfg['every']})",
            f"Real fps      : {real_fps:.3f}",
            f"Dropped (ff)  : {drops}",
            f"Observer      : {cfg['observer']}",
            f"Optics        : {cfg['telescope']}",
            f"Notes         : {cfg['notes']}",
            "Controls      : " + ", ".join(f"{k}={v}" for k, v in ctl.items()),
        ]
        try:
            with open(base + ".txt", "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            self.logline(f"sidecar: {e}")

    def capture_finished(self):
        c = self.cap
        self.mode = "idle"
        self.stream = None
        self.update_buttons()
        if c:
            self.logline(f"Готово: {len(c['files'])} файл(ов), {c['written']} кадров, {human_bytes(c['bytes'])}")
            self.v_capstat.set(f"Готово: {c['written']} кадров, {human_bytes(c['bytes'])} → {c['cfg']['outdir']}")
            self.pb["value"] = 100
        self.stop_req = False

    # ------------------------------------------------------------------ best frames
    def best_frames(self):
        path = filedialog.askopenfilename(initialdir=self.v_outdir.get() or APP_DIR, title="Запись для отбора кадров",
                                          filetypes=[("Видео/SER", "*.ser *.avi *.mkv *.mp4"), ("Все", "*.*")])
        if not path:
            return
        from tkinter import simpledialog
        k = simpledialog.askinteger("AstroCam", "Сколько лучших кадров сохранить?", initialvalue=10, minvalue=1, maxvalue=10000)
        if not k:
            return
        self.logline(f"Оцениваю резкость кадров: {os.path.basename(path)}")
        threading.Thread(target=lambda: self.logline(best_frames(path, k, self.ffmpeg, self.logline)), daemon=True).start()

    # ------------------------------------------------------------------ snapshot
    def snapshot(self):
        if self.latest is None:
            messagebox.showinfo("AstroCam", "Нет кадра — запустите превью.")
            return
        img = self.latest[0]
        cfg = self.collect_cfg()
        os.makedirs(cfg["outdir"], exist_ok=True)
        p = os.path.join(cfg["outdir"], f"snap_{cfg['target'] or 'obj'}_{dt.datetime.now():%Y%m%d_%H%M%S}.png")
        Image.fromarray(img).save(p)
        self.logline(f"Снимок: {p}")

    # ------------------------------------------------------------------ periodic UI
    def ui(self, fn):
        self.uiq.put(fn)

    def tick(self):
        try:
            while not self.uiq.empty():
                self.uiq.get()()
            self._drain_log()
            if self.latest is not None and self.latest_seq != self.shown_seq:
                self.shown_seq = self.latest_seq
                self.render(*self.latest)
            if self.mode == "capture" and self.cap:
                self.update_capture_status()
            elif self.mode == "preview":
                pass
        except Exception as e:
            self.logline(f"UI: {e!r}")
        self.root.after(100, self.tick)

    def update_capture_status(self):
        c = self.cap
        cfg = c["cfg"]
        st = c.get("cur")
        if not st:
            return
        now = time.time()
        t0 = st["t_first"]
        el = (now - t0) if t0 else 0
        fps = self.fps()
        target = None
        if cfg["frames"] > 0:
            target = cfg["frames"]
        if cfg["duration"] > 0:
            nd = cfg["duration"] * fps / cfg["every"]
            target = min(target, nd) if target else nd
        pct = min(100, 100 * st["written"] / target) if target else (el % 10) * 10
        self.pb["value"] = pct
        self.fps_meter = [t for t in self.fps_meter if now - t < 3]
        rf = (len(self.fps_meter) - 1) / (self.fps_meter[-1] - self.fps_meter[0]) if len(self.fps_meter) > 2 else 0
        drops = self.stream.drops if self.stream else 0
        size = dir_size(c.get("cur_path", "")) if int(now * 2) % 2 == 0 else c.get("_lastsize", 0)
        c["_lastsize"] = size
        status = c.get("status", "запись")
        if status.startswith("пауза"):
            self.v_capstat.set(status)
            return
        self.v_capstat.set(f"Серия {c['series_i'] + 1}/{cfg['series']}  кадров {st['written']}" + (f"/{int(target)}" if target else "")
                           + f"  {el:5.1f} с  {rf:4.1f} к/с  {human_bytes(size)}  потерь {drops}")

    def render(self, img, full, origin):
        cw, ch = max(10, self.canvas.winfo_width()), max(10, self.canvas.winfo_height())
        h, w = img.shape[:2]
        s = min(cw / w, ch / h)
        if not full and s > 1:
            s = min(s, 8.0)
        dw, dh = max(1, int(w * s)), max(1, int(h * s))
        pil = Image.fromarray(stretch(img) if self.v_stretch.get() else img)
        pil = pil.resize((dw, dh), Image.NEAREST if s >= 2 else Image.BILINEAR)
        self.photo = ImageTk.PhotoImage(pil)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, image=self.photo, anchor="nw")
        self.disp = (s, ox, oy, origin[0], origin[1]) if full else None
        x, y, rw, rh = self.crop_rect()
        if full:
            if self.v_crop_on.get():
                self.canvas.create_rectangle(ox + x * s, oy + y * s, ox + (x + rw) * s, oy + (y + rh) * s, outline="#0f0", width=2)
                self.canvas.create_text(ox + x * s + 4, oy + y * s - 2, text=f"{rw}×{rh}", anchor="sw", fill="#0f0", font=("Segoe UI", 9))
            sub = img[y:y + rh, x:x + rw] if self.v_crop_on.get() else img
        else:
            sub = img
            self.canvas.create_text(8, 8, anchor="nw", fill="#f44", font=("Segoe UI", 11, "bold"),
                                    text=f"● ЗАПИСЬ  {w}×{h} @ ({origin[0]},{origin[1]})")
        self.update_stats(sub, img if full else None, (x, y, rw, rh) if full and self.v_crop_on.get() else None)

    def update_stats(self, sub, full, rect):
        y = luma(sub)
        if y.size == 0:
            return
        if y.size > 1_500_000:
            y = y[::2, ::2]
        hist = np.bincount(y.ravel(), minlength=256)
        mx = int(y.max())
        sat = 100.0 * hist[250:].sum() / y.size
        mean = float(y.mean())
        f = focus_metric(y)
        if self.mode != "capture":
            self.focus_best = max(self.focus_best * 0.999, f)
        self.fps_meter = [t for t in self.fps_meter if time.time() - t < 3]
        rf = (len(self.fps_meter) - 1) / (self.fps_meter[-1] - self.fps_meter[0]) if len(self.fps_meter) > 2 else 0
        self.v_stats.set(f"макс {mx:3d}   средн {mean:6.1f}   пересвет {sat:5.2f}%\n"
                         f"резкость {f:8.1f}   лучшая {self.focus_best:8.1f}\n"
                         f"{'кроп' if rect else 'кадр'} {sub.shape[1]}×{sub.shape[0]}   {rf:4.1f} к/с")
        # histogram (log)
        hc = self.hist
        hc.delete("all")
        hw, hh = int(hc["width"]), int(hc["height"])
        lg = np.log1p(hist.astype(np.float64))
        lg = lg / (lg.max() or 1)
        pts = []
        for i in range(256):
            pts += [i * hw / 256, hh - lg[i] * (hh - 4)]
        hc.create_line(*pts, fill="#8cf")
        hc.create_line(hw * 235 / 256, 0, hw * 235 / 256, hh, fill="#a60", dash=(2, 2))
        hc.create_text(hw - 3, 3, text=f"max {mx}", anchor="ne", fill="#f88" if mx >= 250 else "#aaa", font=("Consolas", 8))
        # zoom of crop centre at 2x
        zc = self.zoom
        zw, zh = int(zc["width"]), int(zc["height"])
        sh, sw = sub.shape[:2]
        cy, cx = sh // 2, sw // 2
        part = sub[max(0, cy - zh // 4):cy + zh // 4, max(0, cx - zw // 4):cx + zw // 4]
        if part.size:
            pil = Image.fromarray(part).resize((part.shape[1] * 2, part.shape[0] * 2), Image.NEAREST)
            self.zoom_photo = ImageTk.PhotoImage(pil)
            zc.delete("all")
            zc.create_image(zw // 2, zh // 2, image=self.zoom_photo)

    # ------------------------------------------------------------------ presets / settings
    def _load_json(self, p, default):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def preset_names(self):
        names = list(BUILTIN_PRESETS)
        if os.path.isdir(PRESET_DIR):
            names += sorted(os.path.splitext(f)[0] for f in os.listdir(PRESET_DIR) if f.endswith(".json"))
        return names

    def load_preset(self, name):
        if name in BUILTIN_PRESETS:
            cur = self.collect_cfg()
            c = dict(DEFAULTS)
            c.update({k: cur[k] for k in ("device", "outdir", "template", "observer", "telescope", "release_go2rtc")})
            c.update(BUILTIN_PRESETS[name])
        else:
            c = dict(DEFAULTS)
            c.update(self._load_json(os.path.join(PRESET_DIR, name + ".json"), {}))
        c["device"] = c.get("device") if c.get("device") in (self.cb_device["values"] or ()) else self.v_device.get()
        c["outdir"] = c.get("outdir") or self.v_outdir.get()
        self._apply_cfg_to_ui(c)
        if c.get("controls"):
            self.apply_controls(c["controls"])
        self.logline(f"Пресет: {name}")
        if c.get("hint"):
            self.logline("💡 " + c["hint"])

    def save_preset(self):
        win = tk.Toplevel(self.root)
        win.title("Сохранить пресет")
        win.transient(self.root)
        v = tk.StringVar(value=self.v_preset.get() if self.v_preset.get() not in BUILTIN_PRESETS else "")
        ttk.Label(win, text="Имя пресета:").pack(padx=10, pady=(10, 2), anchor="w")
        e = ttk.Entry(win, textvariable=v, width=40)
        e.pack(padx=10)
        e.focus_set()

        def ok():
            name = re.sub(r'[<>:"/\\|?*]+', "_", v.get()).strip()
            if not name:
                return
            os.makedirs(PRESET_DIR, exist_ok=True)
            with open(os.path.join(PRESET_DIR, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(self.collect_cfg(), f, ensure_ascii=False, indent=2)
            self.cb_preset["values"] = self.preset_names()
            self.v_preset.set(name)
            self.logline(f"Пресет сохранён: {name}")
            win.destroy()
        ttk.Button(win, text="Сохранить", command=ok).pack(pady=10)
        e.bind("<Return>", lambda ev: ok())

    def delete_preset(self):
        name = self.v_preset.get()
        p = os.path.join(PRESET_DIR, name + ".json")
        if name in BUILTIN_PRESETS or not os.path.exists(p):
            messagebox.showinfo("AstroCam", "Встроенные пресеты удалить нельзя.")
            return
        if messagebox.askyesno("AstroCam", f"Удалить пресет «{name}»?"):
            os.remove(p)
            self.cb_preset["values"] = self.preset_names()
            self.v_preset.set("")

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.collect_cfg(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.v_outdir.get() or APP_DIR)
        if d:
            self.v_outdir.set(os.path.normpath(d))
            self.update_estimate()

    # ------------------------------------------------------------------ log
    def logline(self, s):
        self.logq.put(f"{dt.datetime.now():%H:%M:%S} {s}")

    def _drain_log(self):
        n = 0
        while not self.logq.empty() and n < 50:
            self.log.insert("end", self.logq.get() + "\n")
            n += 1
        if n:
            self.log.see("end")
            if int(self.log.index("end").split(".")[0]) > 2000:
                self.log.delete("1.0", "500.0")

    def on_close(self):
        if self.mode == "capture" and not messagebox.askyesno("AstroCam", "Идёт запись. Остановить и выйти?"):
            return
        if self.cap:
            self.cap["abort"] = True
        self.stop_all()
        self._save_settings()
        if self.go2rtc_released() and (self.released_by_us or self.v_release.get()):
            if messagebox.askyesno("AstroCam", "Вернуть камеру go2rtc (Frigate снова увидит LifeCam)?"):
                self.go2rtc_action("restore", then=self.root.destroy)
                self.root.after(60000, self.root.destroy)
                return
        self.root.destroy()


# ----------------------------------------------------------------------------- image helpers
def luma(img):
    if img.ndim == 2:
        return img
    return ((img[..., 0].astype(np.uint16) * 77 + img[..., 1].astype(np.uint16) * 150 + img[..., 2].astype(np.uint16) * 29) >> 8).astype(np.uint8)


def focus_metric(y):
    """mean squared Laplacian of the brightest-region-normalised image (higher = sharper)"""
    if y.shape[0] < 3 or y.shape[1] < 3:
        return 0.0
    f = y.astype(np.float32)
    lap = f[1:-1, 1:-1] * 4 - f[:-2, 1:-1] - f[2:, 1:-1] - f[1:-1, :-2] - f[1:-1, 2:]
    return float((lap * lap).mean())


def find_object(y):
    """centroid (x, y) of the brightest blob, or None"""
    step = 2 if y.size > 500_000 else 1
    ys = y[::step, ::step].astype(np.float32)
    mx = ys.max()
    bg = np.median(ys)
    if mx - bg < 10:
        return None
    thr = bg + (mx - bg) * 0.5
    m = ys >= thr
    # keep the region around the maximum only (avoid averaging two separate objects)
    iy, ix = np.unravel_index(np.argmax(ys), ys.shape)
    r = max(8, int(0.15 * min(ys.shape)))
    sl = (slice(max(0, iy - r), iy + r), slice(max(0, ix - r), ix + r))
    w = (ys[sl] - thr).clip(0) * m[sl]
    tot = w.sum()
    if tot <= 0:
        return None
    gy, gx = np.mgrid[sl[0], sl[1]]
    return (float((gx * w).sum() / tot) * step, float((gy * w).sum() / tot) * step)


class Tracker:
    """keeps a fixed-size crop centred on the brightest object, searching around the current crop"""

    def __init__(self, x, y, w, h, W, H):
        self.x, self.y, self.w, self.h, self.W, self.H = x, y, w, h, W, H
        self.cx, self.cy = x + w / 2, y + h / 2

    def update(self, img):
        m = max(self.w, self.h) // 2
        x0, y0 = int(max(0, self.cx - self.w / 2 - m)), int(max(0, self.cy - self.h / 2 - m))
        x1, y1 = int(min(self.W, self.cx + self.w / 2 + m)), int(min(self.H, self.cy + self.h / 2 + m))
        y = luma(img[y0:y1, x0:x1])
        c = find_object(y)
        if c is not None:
            nx, ny = c[0] + x0, c[1] + y0
            # smooth small jitter, follow drift
            self.cx += (nx - self.cx) * 0.3
            self.cy += (ny - self.cy) * 0.3
        x = int(round(self.cx - self.w / 2)) & ~1
        y = int(round(self.cy - self.h / 2)) & ~1
        x = max(0, min(self.W - self.w, x))
        y = max(0, min(self.H - self.h, y))
        return x, y


def iter_frames(path, ffmpeg):
    """yields frames (HxW uint8 or HxWx3 RGB) from a SER file (native) or any video (via ffmpeg)"""
    if path.lower().endswith(".ser"):
        with open(path, "rb") as f:
            hdr = f.read(178)
            _, _, color, _, w, h, depth, n = struct.unpack("<14s7i", hdr[:42])
            if depth > 8:
                raise ValueError("поддерживается только 8-битный SER")
            planes = 3 if color in (100, 101) else 1
            fb = w * h * planes
            for _ in range(n):
                b = f.read(fb)
                if len(b) < fb:
                    break
                img = np.frombuffer(b, np.uint8).reshape((h, w, 3) if planes == 3 else (h, w))
                yield img[..., ::-1] if color == 101 else img
        return
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe") if os.path.isabs(ffmpeg) else "ffprobe"
    pr = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", path], capture_output=True, text=True, creationflags=NOWIN)
    w, h = map(int, pr.stdout.strip().split(",")[:2])
    p = subprocess.Popen([ffmpeg, "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                         stdout=subprocess.PIPE, creationflags=NOWIN)
    fb = w * h * 3
    try:
        while True:
            b = p.stdout.read(fb)
            if len(b) < fb:
                break
            yield np.frombuffer(b, np.uint8).reshape(h, w, 3)
    finally:
        p.kill()


def best_frames(path, k, ffmpeg, log=print):
    """rank frames by brightness-normalised Laplacian energy, save the k sharpest as PNG + scores.csv; returns a summary"""
    import heapq
    try:
        scores, keep = [], []
        for i, img in enumerate(iter_frames(path, ffmpeg)):
            y = luma(img)
            sc = focus_metric(y) / max(1.0, float(y.mean())) ** 2 * 1e4
            scores.append(sc)
            item = (sc, i, img.copy())
            if len(keep) < k:
                heapq.heappush(keep, item)
            elif sc > keep[0][0]:
                heapq.heapreplace(keep, item)
            if i and i % 200 == 0:
                log(f"  …{i} кадров")
        if not scores:
            return "Лучшие кадры: в файле нет кадров"
        out = os.path.splitext(path)[0] + "_best"
        os.makedirs(out, exist_ok=True)
        for rank, (sc, i, img) in enumerate(sorted(keep, key=lambda t: -t[0]), 1):
            Image.fromarray(img).save(os.path.join(out, f"rank{rank:03d}_frame{i + 1:05d}.png"))
        with open(os.path.join(out, "scores.csv"), "w", encoding="utf-8") as f:
            f.write("frame,score\n" + "".join(f"{i + 1},{sc:.3f}\n" for i, sc in enumerate(scores)))
        srt = sorted(scores)
        return (f"✔ Лучшие {len(keep)} из {len(scores)} кадров → {out} "
                f"(резкость: лучший {srt[-1]:.1f}, медиана {srt[len(srt) // 2]:.1f}, худший {srt[0]:.1f})")
    except Exception as e:
        return f"Лучшие кадры: ошибка {e!r}"


def stretch(img):
    """display-only auto levels + gamma, so a dark sky is visible in the preview (data are untouched)"""
    sample = luma(img[::4, ::4]) if img.shape[0] > 64 else luma(img)
    lo = float(np.percentile(sample, 2))
    hi = max(float(np.percentile(sample, 99.95)), float(sample.max()) * 0.6, lo + 12)
    x = np.clip((np.arange(256, dtype=np.float32) - lo) / (hi - lo), 0, 1) ** (1 / 2.2)
    return np.take((x * 255).astype(np.uint8), img)


def planar_prefix(cfg):
    """extractplanes needs planar YUV; packed camera formats are unpacked losslessly first"""
    return "format=yuv422p," if cfg["input"] in ("yuyv422", "uyvy422", "yvyu422") else ""


def controls_specs(controls):
    return [f"{nm}=" + ("auto" if auto else f"{val}:manual") for nm, (val, auto) in controls.items()]


def dir_size(p):
    try:
        if os.path.isdir(p):
            return sum(e.stat().st_size for e in os.scandir(p) if e.is_file())
        return os.path.getsize(p)
    except OSError:
        return 0


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)
    app.cb_preset["values"] = app.preset_names()
    root.mainloop()


if __name__ == "__main__":
    main()

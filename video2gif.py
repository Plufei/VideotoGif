#!/usr/bin/env python3
"""
Batch Video -> GIF Converter
============================

A simple-but-powerful GUI tool that scans a folder (or a hand-picked set of
files), keeps ONLY real video files plus any existing GIFs, converts each
video into a high quality animated GIF, and copies (or moves) existing GIFs
straight through -- all into an output folder of your choosing.

Conversion uses ffmpeg's two-pass palettegen/paletteuse method, run as two
separate streaming passes (palette written to a temp file on disk). This keeps
memory bounded so even long videos convert quickly instead of grinding to a
halt -- unlike the single-pass `split` approach, which must buffer the whole
filtered video in RAM.

Requirements
------------
* Python 3.8+  (tkinter ships with the standard CPython installers)
* ffmpeg and ffprobe on your PATH
      Windows : https://www.gyan.dev/ffmpeg/builds/  (add the bin/ folder to PATH)
      macOS   : brew install ffmpeg
      Linux   : sudo apt install ffmpeg   (or your distro's package)

Run
---
      python3 video2gif.py
"""

import os
import re
import sys
import time
import queue
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# --------------------------------------------------------------------------- #
#  Core (non-GUI) logic  --  kept as plain functions so it is easy to test
# --------------------------------------------------------------------------- #

# Extensions we are willing to *consider* as video. The actual gatekeeping is
# done by ffprobe (a file is only converted if it really contains a video
# stream) but this keeps the folder scan fast and skips obvious non-videos.
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".m4v",
    ".mpg", ".mpeg", ".m2v", ".3gp", ".3g2", ".ogv", ".ts", ".mts",
    ".m2ts", ".vob", ".asf", ".rm", ".rmvb", ".divx", ".f4v", ".mxf",
}

GIF_SUFFIX = ".gif"
# Everything the tool will pick up: convertible videos plus existing gifs.
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {GIF_SUFFIX}

DITHER_OPTIONS = ["sierra2_4a", "floyd_steinberg", "bayer", "none"]


def _no_window_kwargs():
    """On Windows, stop a console window flashing for every ffmpeg call."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def find_tool(name):
    """Return the path to an executable on PATH, or None."""
    return shutil.which(name)


def looks_like_video(path):
    """True if the extension is one of the convertible video types."""
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def is_gif(path):
    """True if the file is already a GIF (these are copied, not converted)."""
    return Path(path).suffix.lower() == GIF_SUFFIX


def looks_like_media(path):
    """True for any file we handle: a convertible video OR an existing gif."""
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def scan_media(folder, recursive=False):
    """Return a sorted list of videos AND gifs inside *folder*."""
    folder = Path(folder)
    it = folder.rglob("*") if recursive else folder.glob("*")
    files = [p for p in it if p.is_file() and looks_like_media(p)]
    return sorted(files, key=lambda p: str(p).lower())


def same_path(a, b):
    """Case-insensitive (on Windows) absolute-path equality test."""
    def norm(p):
        return os.path.normcase(os.path.abspath(str(p)))
    return norm(a) == norm(b)


def probe_video(path, ffprobe):
    """
    Verify *path* really has a video stream and return its duration in seconds.

    Returns (has_video: bool, duration: float|None).
    This is what guarantees we convert ONLY real video files.
    """
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_type:format=duration",
             "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, text=True, **_no_window_kwargs()
        )
    except Exception:
        return False, None

    has_video = "codec_type=video" in out.stdout
    duration = None
    m = re.search(r"duration=([0-9.]+)", out.stdout)
    if m:
        try:
            duration = float(m.group(1))
        except ValueError:
            duration = None
    return has_video, duration


def _scale_fps_chain(fps, width):
    """Shared front of both passes: thin out frames, optionally downscale."""
    chain = [f"fps={fps}"]
    if width and width > 0:
        # -1 keeps the aspect ratio; lanczos gives crisp downscales.
        chain.append(f"scale={width}:-1:flags=lanczos")
    return ",".join(chain)


def build_palettegen_cmd(ffmpeg, src, palette_path, fps, width, max_colors,
                         hwaccel=False):
    """Pass 1 -- stream the video once and write an optimal palette PNG.

    This pass is memory-light: ffmpeg accumulates colour statistics and emits a
    single small image at the end.
    """
    vf = (f"{_scale_fps_chain(fps, width)},"
          f"palettegen=max_colors={max_colors}:stats_mode=full")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-an", "-sn", "-dn",      # ignore audio/subtitle/data: faster decode
           "-threads", "0"]          # let ffmpeg use all cores
    if hwaccel:
        cmd += ["-hwaccel", "auto"]  # must precede the input it applies to
    cmd += [
        "-i", str(src),
        "-vf", vf,
        "-progress", "pipe:1", "-nostats",
        str(palette_path),
    ]
    return cmd


def build_paletteuse_cmd(ffmpeg, src, palette_path, dst,
                         fps, width, dither, loop_forever, hwaccel=False):
    """Pass 2 -- stream the video again, mapping it onto the palette.

    Also memory-light: frames flow straight through to the GIF muxer.
    """
    if dither == "bayer":
        paletteuse = "paletteuse=dither=bayer:bayer_scale=5"
    elif dither == "none":
        paletteuse = "paletteuse=dither=none"
    else:
        paletteuse = f"paletteuse=dither={dither}"

    lavfi = (f"[0:v]{_scale_fps_chain(fps, width)}[x];"
             f"[x][1:v]{paletteuse}")
    loop = "0" if loop_forever else "-1"  # ffmpeg GIF: 0 = infinite, -1 = none
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-an", "-sn", "-dn",
           "-threads", "0"]
    if hwaccel:
        # Applies to the next input (the video) only, not the palette PNG.
        cmd += ["-hwaccel", "auto"]
    cmd += [
        "-i", str(src),
        "-i", str(palette_path),
        "-lavfi", lavfi,
        "-loop", loop,
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]
    return cmd


def parse_progress_seconds(line):
    """Pull a position-in-seconds out of one ffmpeg -progress line, or None."""
    if line.startswith("out_time_us="):
        raw = line.split("=", 1)[1]
        return int(raw) / 1_000_000 if raw.isdigit() else None
    if line.startswith("out_time="):
        m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", line.split("=", 1)[1])
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    return None


def fmt_duration(seconds):
    """Human-friendly m:ss / h:mm:ss for the log."""
    if not seconds or seconds <= 0:
        return "unknown length"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def unique_output_path(src, out_dir, used_names):
    """
    Pick a non-colliding 'name.gif' inside out_dir.

    Two sources called clip.mov and clip.mp4 would both want clip.gif, so we
    disambiguate by folding the original extension (and a counter) into the name.
    """
    src = Path(src)
    candidate = out_dir / f"{src.stem}.gif"
    if candidate.name.lower() not in used_names:
        used_names.add(candidate.name.lower())
        return candidate

    ext = src.suffix.lstrip(".")
    candidate = out_dir / f"{src.stem}_{ext}.gif"
    n = 1
    while candidate.name.lower() in used_names:
        candidate = out_dir / f"{src.stem}_{ext}_{n}.gif"
        n += 1
    used_names.add(candidate.name.lower())
    return candidate


def open_in_file_manager(path):
    """Reveal a folder in the OS file manager (best effort, cross-platform)."""
    path = str(path)
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: E722  (Windows only)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #

class ConverterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Batch Video → GIF Converter")
        self.minsize(640, 600)

        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")

        # Each entry in this list is (Path, tk.BooleanVar) for the checklist.
        self.files = []

        # Worker-thread communication.
        self.msg_queue = queue.Queue()
        self.worker = None
        self.current_proc = None
        self.cancel_flag = threading.Event()

        self._build_ui()
        self._poll_queue()

        if not self.ffmpeg or not self.ffprobe:
            self._warn_missing_ffmpeg()

    # ----- UI construction -------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        self.columnconfigure(0, weight=1)

        # --- Input row ----------------------------------------------------- #
        frm_in = ttk.LabelFrame(self, text="1.  Input")
        frm_in.grid(row=0, column=0, sticky="ew", **pad)
        frm_in.columnconfigure(0, weight=1)

        self.input_var = tk.StringVar()
        ttk.Entry(frm_in, textvariable=self.input_var).grid(
            row=0, column=0, sticky="ew", padx=6, pady=6)
        ttk.Button(frm_in, text="Choose Folder…",
                   command=self.choose_input_folder).grid(row=0, column=1, padx=4)
        ttk.Button(frm_in, text="Choose Files…",
                   command=self.choose_input_files).grid(row=0, column=2, padx=4)

        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_in, text="Include subfolders",
                        variable=self.recursive_var,
                        command=self._rescan_if_folder).grid(
            row=1, column=0, sticky="w", padx=6, pady=(0, 6))

        # --- File checklist ------------------------------------------------ #
        frm_list = ttk.LabelFrame(self, text="2.  Videos to convert")
        frm_list.grid(row=1, column=0, sticky="nsew", **pad)
        frm_list.columnconfigure(0, weight=1)
        frm_list.rowconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        canvas = tk.Canvas(frm_list, height=160, highlightthickness=0)
        scroll = ttk.Scrollbar(frm_list, orient="vertical", command=canvas.yview)
        self.list_inner = ttk.Frame(canvas)
        self.list_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._list_window = canvas.create_window(
            (0, 0), window=self.list_inner, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(self._list_window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        scroll.grid(row=0, column=1, sticky="ns", pady=6)
        self._mousewheel(canvas)

        btns = ttk.Frame(frm_list)
        btns.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))
        ttk.Button(btns, text="Select all",
                   command=lambda: self._set_all(True)).pack(side="left", padx=2)
        ttk.Button(btns, text="Deselect all",
                   command=lambda: self._set_all(False)).pack(side="left", padx=2)
        ttk.Button(btns, text="Clear list",
                   command=self._clear_list).pack(side="left", padx=2)
        self.count_lbl = ttk.Label(btns, text="0 videos")
        self.count_lbl.pack(side="left", padx=12)

        # --- Settings ------------------------------------------------------ #
        frm_set = ttk.LabelFrame(self, text="3.  GIF settings")
        frm_set.grid(row=2, column=0, sticky="ew", **pad)

        self.fps_var = tk.IntVar(value=15)
        self.width_var = tk.IntVar(value=480)
        self.colors_var = tk.IntVar(value=256)
        self.dither_var = tk.StringVar(value=DITHER_OPTIONS[0])
        self.loop_var = tk.BooleanVar(value=True)
        self.hwaccel_var = tk.BooleanVar(value=True)
        self.gif_mode_var = tk.StringVar(value="copy")

        ttk.Label(frm_set, text="FPS").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Spinbox(frm_set, from_=1, to=50, width=6,
                    textvariable=self.fps_var).grid(row=0, column=1, sticky="w")

        ttk.Label(frm_set, text="Width px (0 = original)").grid(
            row=0, column=2, sticky="e", padx=4)
        ttk.Spinbox(frm_set, from_=0, to=4096, increment=20, width=7,
                    textvariable=self.width_var).grid(row=0, column=3, sticky="w")

        ttk.Label(frm_set, text="Max colors").grid(
            row=1, column=0, sticky="e", padx=4, pady=4)
        ttk.Spinbox(frm_set, from_=2, to=256, width=6,
                    textvariable=self.colors_var).grid(row=1, column=1, sticky="w")

        ttk.Label(frm_set, text="Dither").grid(row=1, column=2, sticky="e", padx=4)
        ttk.Combobox(frm_set, values=DITHER_OPTIONS, textvariable=self.dither_var,
                     state="readonly", width=14).grid(row=1, column=3, sticky="w")

        ttk.Checkbutton(frm_set, text="Loop forever",
                        variable=self.loop_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Checkbutton(frm_set, text="Hardware decoding (faster, auto-fallback)",
                        variable=self.hwaccel_var).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=4, pady=4)

        ttk.Separator(frm_set, orient="horizontal").grid(
            row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=6)
        ttk.Label(frm_set, text="Existing .gif files:").grid(
            row=4, column=0, sticky="e", padx=4, pady=(0, 6))
        gif_modes = ttk.Frame(frm_set)
        gif_modes.grid(row=4, column=1, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Radiobutton(gif_modes, text="Copy to output", value="copy",
                        variable=self.gif_mode_var).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(gif_modes, text="Move (delete from input)", value="move",
                        variable=self.gif_mode_var).pack(side="left")

        # --- Output -------------------------------------------------------- #
        frm_out = ttk.LabelFrame(self, text="4.  Output folder")
        frm_out.grid(row=3, column=0, sticky="ew", **pad)
        frm_out.columnconfigure(0, weight=1)
        self.output_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self.output_var).grid(
            row=0, column=0, sticky="ew", padx=6, pady=6)
        ttk.Button(frm_out, text="Choose…",
                   command=self.choose_output_folder).grid(row=0, column=1, padx=4)

        # --- Action + progress -------------------------------------------- #
        frm_go = ttk.Frame(self)
        frm_go.grid(row=4, column=0, sticky="ew", **pad)
        frm_go.columnconfigure(1, weight=1)

        self.convert_btn = ttk.Button(frm_go, text="Convert",
                                      command=self.start_conversion)
        self.convert_btn.grid(row=0, column=0, rowspan=4, padx=4)
        self.cancel_btn = ttk.Button(frm_go, text="Cancel",
                                     command=self.cancel_conversion,
                                     state="disabled")
        self.cancel_btn.grid(row=0, column=2, rowspan=4, padx=4)

        ttk.Label(frm_go, text="Total progress").grid(
            row=0, column=1, sticky="w", padx=8)
        self.progress = ttk.Progressbar(frm_go, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=1, sticky="ew", padx=8)

        ttk.Label(frm_go, text="Current file").grid(
            row=2, column=1, sticky="w", padx=8, pady=(6, 0))
        self.file_progress = ttk.Progressbar(frm_go, mode="determinate",
                                             maximum=100)
        self.file_progress.grid(row=3, column=1, sticky="ew", padx=8)

        # --- Log ----------------------------------------------------------- #
        frm_log = ttk.LabelFrame(self, text="Log")
        frm_log.grid(row=5, column=0, sticky="nsew", **pad)
        frm_log.columnconfigure(0, weight=1)
        frm_log.rowconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        self.log = tk.Text(frm_log, height=8, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(frm_log, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        log_scroll.grid(row=0, column=1, sticky="ns", pady=6)

        self.status = ttk.Label(self, text="Ready.", anchor="w", relief="sunken")
        self.status.grid(row=6, column=0, sticky="ew")

    def _mousewheel(self, canvas):
        def on_wheel(event):
            delta = -1 * (event.delta // 120) if event.delta else (
                1 if event.num == 5 else -1)
            canvas.yview_scroll(delta, "units")
        canvas.bind_all("<MouseWheel>", on_wheel)
        canvas.bind_all("<Button-4>", on_wheel)
        canvas.bind_all("<Button-5>", on_wheel)

    # ----- Input handling -------------------------------------------------- #
    def choose_input_folder(self):
        folder = filedialog.askdirectory(title="Select a folder of videos")
        if not folder:
            return
        self.input_var.set(folder)
        found = scan_media(folder, self.recursive_var.get())
        self._populate(found)
        if not self.output_var.get():
            self.output_var.set(str(Path(folder) / "gifs"))

    def choose_input_files(self):
        paths = filedialog.askopenfilenames(
            title="Select video / gif files",
            filetypes=[("Videos & GIFs",
                        " ".join(f"*{e}" for e in sorted(MEDIA_EXTENSIONS))),
                       ("All files", "*.*")])
        if not paths:
            return
        self.input_var.set("(individual files)")
        self._populate([Path(p) for p in paths])
        if not self.output_var.get() and paths:
            self.output_var.set(str(Path(paths[0]).parent / "gifs"))

    def _rescan_if_folder(self):
        folder = self.input_var.get()
        if folder and folder != "(individual files)" and Path(folder).is_dir():
            self._populate(scan_media(folder, self.recursive_var.get()))

    def _populate(self, paths):
        for child in self.list_inner.winfo_children():
            child.destroy()
        self.files = []
        for p in paths:
            var = tk.BooleanVar(value=True)
            row = ttk.Frame(self.list_inner)
            row.pack(fill="x", anchor="w")
            ttk.Checkbutton(row, variable=var).pack(side="left")
            ttk.Label(row, text=p.name).pack(side="left", anchor="w")
            if is_gif(p):
                ttk.Label(row, text="  (gif — copied as-is)",
                          foreground="#888").pack(side="left", anchor="w")
            self.files.append((p, var))
        self._update_count()

    def _set_all(self, value):
        for _, var in self.files:
            var.set(value)

    def _clear_list(self):
        self._populate([])
        self.input_var.set("")

    def _update_count(self):
        n_gif = sum(1 for p, _ in self.files if is_gif(p))
        n_vid = len(self.files) - n_gif
        parts = [f"{n_vid} video(s)"]
        if n_gif:
            parts.append(f"{n_gif} gif(s)")
        self.count_lbl.config(text=", ".join(parts))

    # ----- Output handling ------------------------------------------------- #
    def choose_output_folder(self):
        folder = filedialog.askdirectory(
            title="Select / create the output folder")
        if folder:
            self.output_var.set(folder)

    # ----- Conversion ------------------------------------------------------ #
    def start_conversion(self):
        if not self.ffmpeg or not self.ffprobe:
            self._warn_missing_ffmpeg()
            return

        selected = [p for p, var in self.files if var.get()]
        if not selected:
            messagebox.showinfo("Nothing to do",
                                "Tick at least one video to convert.")
            return

        out_dir = self.output_var.get().strip()
        if not out_dir:
            messagebox.showinfo("Output folder",
                                "Choose where the GIFs should go.")
            return
        out_dir = Path(out_dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Output folder",
                                 f"Could not create:\n{out_dir}\n\n{e}")
            return

        settings = dict(
            fps=max(1, self.fps_var.get()),
            width=max(0, self.width_var.get()),
            max_colors=min(256, max(2, self.colors_var.get())),
            dither=self.dither_var.get(),
            loop_forever=self.loop_var.get(),
            hwaccel=self.hwaccel_var.get(),
        )

        gif_mode = self.gif_mode_var.get()
        n_gifs = sum(1 for p in selected if is_gif(p))
        if gif_mode == "move" and n_gifs:
            if not messagebox.askyesno(
                    "Confirm move",
                    f"Move mode will DELETE {n_gifs} gif(s) from the input "
                    "folder after copying them to the output.\n\nContinue?"):
                return

        self._set_running(True)
        self.cancel_flag.clear()
        self._log_clear()
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(selected, out_dir, settings, gif_mode),
            daemon=True)
        self.worker.start()

    def cancel_conversion(self):
        self.cancel_flag.set()
        if self.current_proc and self.current_proc.poll() is None:
            try:
                self.current_proc.terminate()
            except Exception:
                pass
        self._post(("status", "Cancelling…"))

    def _run_batch(self, files, out_dir, settings, gif_mode):
        total = len(files)
        used_names = set()
        converted = gifs_copied = gifs_moved = failed = 0

        for i, src in enumerate(files):
            if self.cancel_flag.is_set():
                break

            self._post(("status", f"[{i+1}/{total}] {src.name}"))
            self._post(("file_base", i, total))

            # --- existing GIF: copy or move, never re-encode --------------- #
            if is_gif(src):
                res = self._handle_gif(src, out_dir, used_names, gif_mode)
                if res == "moved":
                    gifs_moved += 1
                elif res in ("copied", "inplace"):
                    gifs_copied += 1
                else:
                    failed += 1
                self._post(("file", 100))
                self._post(("overall", (i + 1) / total * 100))
                continue

            # --- real video: verify then convert --------------------------- #
            has_video, duration = probe_video(src, self.ffprobe)
            if not has_video:
                self._log_event(f"⚠ Skipped (no video stream): {src.name}\n")
                failed += 1
                self._post(("overall", (i + 1) / total * 100))
                continue

            self._post(("file", 0))
            self._log_event(f"▶ Converting {src.name}  "
                            f"({fmt_duration(duration)})…\n")
            dst = unique_output_path(src, out_dir, used_names)
            ok, err = self._convert_video(src, dst, settings, duration, i, total)
            if ok:
                self._log_event(f"✓ {src.name}  →  {dst.name}\n")
                converted += 1
            else:
                if self.cancel_flag.is_set():
                    self._log_event(f"■ Cancelled during {src.name}\n")
                    # Remove the half-written file.
                    try:
                        dst.unlink(missing_ok=True)
                    except Exception:
                        pass
                    break
                self._log_event(f"✗ Failed: {src.name}\n")
                if err:
                    self._log_event(f"   ffmpeg: {err}\n")
                failed += 1
            self._post(("file", 100))
            self._post(("overall", (i + 1) / total * 100))

        self._post(("done", {
            "converted": converted,
            "copied": gifs_copied,
            "moved": gifs_moved,
            "failed": failed,
            "out_dir": str(out_dir),
        }))

    def _handle_gif(self, src, out_dir, used_names, gif_mode):
        """Copy (or move) an existing .gif into the output folder.

        Returns one of: 'copied', 'moved', 'inplace', 'fail'.
        """
        dst = unique_output_path(src, out_dir, used_names)

        # The gif is already sitting in the output folder — never delete it.
        if same_path(src, dst):
            self._log_event(f"• {src.name} already in output — left in place\n")
            return "inplace"

        try:
            shutil.copy2(src, dst)
        except Exception as e:
            self._log_event(f"✗ Could not copy {src.name}: {e}\n")
            return "fail"

        if gif_mode == "move":
            try:
                Path(src).unlink()
                self._log_event(f"⇄ Moved {src.name}  →  {dst.name}\n")
                return "moved"
            except Exception as e:
                self._log_event(
                    f"✓ Copied {src.name}  →  {dst.name} "
                    f"(could not delete original: {e})\n")
                return "copied"

        self._log_event(f"✓ Copied {src.name}  →  {dst.name}\n")
        return "copied"

    # Share of the per-file bar allotted to pass 1 (palette generation).
    _PALETTE_PASS_WEIGHT = 0.4

    def _convert_video(self, src, dst, settings, duration, index, total):
        """Two-pass conversion; try HW decode, fall back to CPU on failure."""
        want_hw = bool(settings.get("hwaccel"))
        ok, err = self._convert_once(
            src, dst, settings, duration, index, total, want_hw)
        if not ok and want_hw and not self.cancel_flag.is_set():
            self._log_event("   hardware decode failed — retrying on CPU…\n")
            self._post(("file", 0))
            ok, err = self._convert_once(
                src, dst, settings, duration, index, total, False)
        return ok, err

    def _convert_once(self, src, dst, settings, duration, index, total, hwaccel):
        """One full two-pass run (low memory). Returns (ok, error_tail)."""
        fd, palette = tempfile.mkstemp(suffix=".png", prefix="v2g_palette_")
        os.close(fd)
        pg = self._PALETTE_PASS_WEIGHT
        try:
            # Pass 1: build palette (streams the file, bounded memory).
            cmd1 = build_palettegen_cmd(
                self.ffmpeg, src, palette,
                settings["fps"], settings["width"], settings["max_colors"],
                hwaccel)
            ok, err = self._run_ffmpeg(
                cmd1, duration,
                lambda f: self._report_progress(index, total, f * pg))
            if not ok:
                return False, err

            # Pass 2: apply palette -> GIF (streams the file, bounded memory).
            cmd2 = build_paletteuse_cmd(
                self.ffmpeg, src, palette, dst,
                settings["fps"], settings["width"],
                settings["dither"], settings["loop_forever"], hwaccel)
            ok, err = self._run_ffmpeg(
                cmd2, duration,
                lambda f: self._report_progress(index, total, pg + f * (1 - pg)))
            return ok, err
        finally:
            try:
                os.remove(palette)
            except Exception:
                pass

    def _report_progress(self, index, total, file_frac):
        """Push both the per-file and overall bars from a 0..1 file fraction."""
        file_frac = max(0.0, min(1.0, file_frac))
        self._post(("file", file_frac * 100))
        self._post(("overall", (index + file_frac) / total * 100))

    # If ffmpeg reports no progress for this long, treat the pass as stalled,
    # kill it, and let the caller recover (e.g. HW decode -> CPU fallback).
    _STALL_TIMEOUT = 120

    def _run_ffmpeg(self, cmd, duration, on_frac):
        """Run one ffmpeg pass, streaming progress. Returns (ok, error_tail).

        Two robustness measures live here:

        1. stderr is written to a temp FILE, never a pipe. A chatty ffmpeg --
           e.g. hardware-decode warnings on an awkward codec like VP9 -- can
           emit enough stderr to fill a pipe buffer; if we only drained that
           pipe *after* reading stdout, ffmpeg would block on the write and the
           whole conversion would deadlock. A file never blocks.

        2. A watchdog reads stdout on a helper thread and kills the pass if it
           stops making progress entirely, so a genuine hang can't freeze the
           app -- it fails the pass and the batch moves on (or falls back).
        """
        errf = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errf,
                text=True, bufsize=1, **_no_window_kwargs())
        except Exception as e:
            errf.close()
            return False, f"could not start ffmpeg: {e}"
        self.current_proc = proc

        # A blocking read on proc.stdout can't be interrupted, so read it on a
        # helper thread and let this thread run the watchdog.
        lines = queue.Queue()

        def _reader():
            try:
                for ln in proc.stdout:
                    lines.put(ln)
            except Exception:
                pass
            lines.put(None)  # EOF sentinel

        threading.Thread(target=_reader, daemon=True).start()

        last_activity = time.monotonic()
        stalled = False
        while True:
            if self.cancel_flag.is_set():
                break
            try:
                ln = lines.get(timeout=2.0)
            except queue.Empty:
                if time.monotonic() - last_activity > self._STALL_TIMEOUT:
                    stalled = True
                    break
                continue
            if ln is None:                 # stdout closed: pass is finishing
                break
            last_activity = time.monotonic()
            ln = ln.strip()
            if ln == "progress=end":
                break
            if duration:
                secs = parse_progress_seconds(ln)
                if secs is not None:
                    on_frac(secs / duration)

        # Stop the process if we bailed out early (stall or user cancel).
        if (stalled or self.cancel_flag.is_set()) and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        self.current_proc = None

        # Read diagnostics from the temp file.
        err = ""
        try:
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace")
        except Exception:
            pass
        finally:
            errf.close()
        tail = "\n".join(err.strip().splitlines()[-4:]) if err else ""

        if stalled:
            note = f"[stalled: no progress for {self._STALL_TIMEOUT}s]"
            return False, (f"{tail}\n{note}").strip()
        if proc.returncode == 0:
            return True, ""
        return False, tail

    # ----- Cross-thread message pump --------------------------------------- #
    def _post(self, msg):
        self.msg_queue.put(msg)

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            self._log_write(msg[1])
        elif kind == "status":
            self.status.config(text=msg[1])
        elif kind == "overall":
            self.progress["value"] = msg[1]
        elif kind == "file":
            self.file_progress["value"] = msg[1]
        elif kind == "file_base":
            index, total = msg[1], msg[2]
            self.progress["value"] = index / total * 100
            self.file_progress["value"] = 0
        elif kind == "done":
            r = msg[1]
            out_dir = r["out_dir"]
            placed = r["converted"] + r["copied"] + r["moved"]
            self.progress["value"] = 100 if not self.cancel_flag.is_set() else \
                self.progress["value"]
            self._set_running(False)

            parts = []
            if r["converted"]:
                parts.append(f"{r['converted']} converted")
            if r["copied"]:
                parts.append(f"{r['copied']} gif(s) copied")
            if r["moved"]:
                parts.append(f"{r['moved']} gif(s) moved")
            if r["failed"]:
                parts.append(f"{r['failed']} skipped/failed")
            body = ", ".join(parts) if parts else "nothing to do"

            summary = (f"Cancelled. {body}." if self.cancel_flag.is_set()
                       else f"Done. {body}.")
            self.status.config(text=summary)
            self._log_write(f"[{time.strftime('%H:%M:%S')}] {summary}\n")
            if placed and not self.cancel_flag.is_set():
                if messagebox.askyesno("Finished",
                                       f"{summary}\n\nOpen the output folder?"):
                    open_in_file_manager(out_dir)

    # ----- small helpers --------------------------------------------------- #
    def _set_running(self, running):
        self.convert_btn.config(state="disabled" if running else "normal")
        self.cancel_btn.config(state="normal" if running else "disabled")

    def _log_clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _log_event(self, text):
        """Worker-thread logging: prefix a timestamp and queue it for the UI."""
        self._post(("log", f"[{time.strftime('%H:%M:%S')}] {text}"))

    def _log_write(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _warn_missing_ffmpeg(self):
        missing = [n for n, p in (("ffmpeg", self.ffmpeg),
                                  ("ffprobe", self.ffprobe)) if not p]
        msg = (f"Could not find {', '.join(missing)} on your PATH.\n\n"
               "Install ffmpeg and make sure its 'bin' folder is on PATH:\n"
               "  Windows : https://www.gyan.dev/ffmpeg/builds/\n"
               "  macOS   : brew install ffmpeg\n"
               "  Linux   : sudo apt install ffmpeg")
        self._log_write(msg + "\n")
        self.status.config(text="ffmpeg not found — install it, then restart.")
        messagebox.showwarning("ffmpeg not found", msg)


def main():
    app = ConverterApp()
    app.mainloop()


if __name__ == "__main__":
    main()

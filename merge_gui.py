"""
FFmpeg 多段视频拼接工具（图形界面 + 独立 AB 滑条对照 + 首尾帧缩略图 + 直接拖入 + 单实例保护）
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from merge_videos import (
    DEFAULT_TRIM_FRAMES,
    VideoInfo,
    calc_trim_seconds,
    default_output_path,
    expand_input_paths,
    extract_first_last_thumbnails,
    extract_frames_rgb,
    find_executable,
    format_fps,
    numeric_sort_key,
    probe_video,
    run_ffmpeg_merge,
)


THUMB_W = 80
THUMB_H = 46
THUMB_GAP = 14
COMP_W = THUMB_W * 2 + THUMB_GAP
COMP_H = THUMB_H + 4

IPC_QUEUE_FILE = Path(tempfile.gettempdir()) / "antigravity_video_merge_ipc.jsonl"
MUTEX_NAME = "Local\\AntigravityVideoMergeGUI_SingleInstance_v1"


def read_clipboard_paths_win() -> List[str]:
    """安全读取 Windows 剪贴板中的文件列表（CF_HDROP）或文本路径。"""
    if sys.platform != "win32":
        return []
    CF_HDROP = 15
    CF_UNICODETEXT = 13

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT

    paths: List[str] = []
    if not user32.OpenClipboard(None):
        return paths
    try:
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            hdrop = user32.GetClipboardData(CF_HDROP)
            if hdrop:
                count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                for i in range(count):
                    length = shell32.DragQueryFileW(hdrop, i, None, 0)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(hdrop, i, buf, length + 1)
                    if buf.value:
                        paths.append(buf.value)
        elif user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            htext = user32.GetClipboardData(CF_UNICODETEXT)
            if htext:
                ptr = kernel32.GlobalLock(htext)
                if ptr:
                    try:
                        text = ctypes.wstring_at(ptr)
                        for line in text.splitlines():
                            cleaned = line.strip().strip('"').strip("'")
                            if cleaned and Path(cleaned).exists():
                                paths.append(cleaned)
                    finally:
                        kernel32.GlobalUnlock(htext)
    finally:
        user32.CloseClipboard()
    return paths


class Win32DropHelper:
    """为 Tkinter 顶层窗口注册原生 Windows 文件拖放 (WM_DROPFILES)。"""

    WM_DROPFILES = 0x0233
    GWLP_WNDPROC = -4

    def __init__(self, root: tk.Tk, on_drop: Callable[[List[str]], None]) -> None:
        self.root = root
        self.on_drop = on_drop
        self.hwnd: Optional[int] = None
        self.old_wndproc: Optional[int] = None
        self._cb = None

        if sys.platform != "win32":
            return

        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32

        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

        self.user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        self.user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        self.user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        self.user32.CallWindowProcW.restype = LRESULT

        self.shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.shell32.DragAcceptFiles.restype = None
        self.shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        self.shell32.DragQueryFileW.restype = wintypes.UINT
        self.shell32.DragFinish.argtypes = [ctypes.c_void_p]
        self.shell32.DragFinish.restype = None

        self.install()

    def install(self) -> None:
        if sys.platform != "win32":
            return
        self.root.update_idletasks()
        frame_hex = self.root.wm_frame()
        hwnd = int(frame_hex, 16) if frame_hex and frame_hex != "0x0" else self.root.winfo_id()
        self.hwnd = hwnd

        # 允许跨权限/常规拖放消息
        try:
            self.user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
            self.user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
            for msg in (self.WM_DROPFILES, 0x0049):
                self.user32.ChangeWindowMessageFilterEx(hwnd, msg, 1, None)
        except Exception:
            pass

        self.shell32.DragAcceptFiles(hwnd, True)

        def wndproc(h: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == self.WM_DROPFILES:
                hdrop = ctypes.c_void_p(wparam)
                dropped: List[str] = []
                try:
                    count = self.shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    for i in range(count):
                        length = self.shell32.DragQueryFileW(hdrop, i, None, 0)
                        buf = ctypes.create_unicode_buffer(length + 1)
                        self.shell32.DragQueryFileW(hdrop, i, buf, length + 1)
                        if buf.value:
                            dropped.append(buf.value)
                finally:
                    self.shell32.DragFinish(hdrop)
                if dropped:
                    self.root.after(10, lambda files=dropped: self.on_drop(files))
                return 0
            return self.user32.CallWindowProcW(self.old_wndproc, h, msg, wparam, lparam)

        self._cb = self.WNDPROC(wndproc)
        self.old_wndproc = self.user32.SetWindowLongPtrW(
            hwnd, self.GWLP_WNDPROC, ctypes.cast(self._cb, ctypes.c_void_p)
        )

    def uninstall(self) -> None:
        if sys.platform == "win32" and self.hwnd and self.old_wndproc:
            try:
                self.user32.SetWindowLongPtrW(self.hwnd, self.GWLP_WNDPROC, self.old_wndproc)
            except Exception:
                pass
            self.old_wndproc = None


def acquire_single_instance_mutex(mutex_name: str = MUTEX_NAME) -> Tuple[Optional[int], bool]:
    """返回 (handle, is_primary)。若已有实例运行则 is_primary=False。"""
    if sys.platform != "win32":
        return None, True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    already_exists = kernel32.GetLastError() == 183
    return handle, not already_exists


def send_paths_to_primary(paths: Sequence[str]) -> None:
    payload = {"paths": [str(p) for p in paths]}
    with open(IPC_QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def make_placeholder_thumbnail() -> ImageTk.PhotoImage:
    img = Image.new("RGB", (COMP_W, COMP_H), (39, 39, 42))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 2), (THUMB_W - 1, 2 + THUMB_H - 1)], outline=(82, 82, 91), fill=(52, 52, 58))
    draw.rectangle([(THUMB_W + THUMB_GAP, 2), (COMP_W - 1, 2 + THUMB_H - 1)], outline=(82, 82, 91), fill=(52, 52, 58))
    mid_x = THUMB_W + THUMB_GAP // 2
    mid_y = COMP_H // 2
    draw.line([(mid_x - 4, mid_y), (mid_x + 4, mid_y)], fill=(161, 161, 170), width=2)
    draw.line([(mid_x + 1, mid_y - 3), (mid_x + 4, mid_y), (mid_x + 1, mid_y + 3)], fill=(161, 161, 170), width=1)
    return ImageTk.PhotoImage(img)


def compose_first_last_thumbnail(first_raw: Optional[bytes], last_raw: Optional[bytes]) -> ImageTk.PhotoImage:
    canvas = Image.new("RGB", (COMP_W, COMP_H), (24, 24, 27))
    if first_raw:
        im1 = Image.frombytes("RGB", (THUMB_W, THUMB_H), first_raw)
        canvas.paste(im1, (0, 2))
    if last_raw:
        im2 = Image.frombytes("RGB", (THUMB_W, THUMB_H), last_raw)
        canvas.paste(im2, (THUMB_W + THUMB_GAP, 2))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, 2), (THUMB_W - 1, 2 + THUMB_H - 1)], outline=(113, 113, 122))
    draw.rectangle([(THUMB_W + THUMB_GAP, 2), (COMP_W - 1, 2 + THUMB_H - 1)], outline=(113, 113, 122))
    mid_x = THUMB_W + THUMB_GAP // 2
    mid_y = COMP_H // 2
    draw.line([(mid_x - 4, mid_y), (mid_x + 4, mid_y)], fill=(250, 204, 21), width=2)
    draw.line([(mid_x + 1, mid_y - 3), (mid_x + 4, mid_y), (mid_x + 1, mid_y + 3)], fill=(250, 204, 21), width=1)
    return ImageTk.PhotoImage(canvas)


class ABCompareWindow(tk.Toplevel):
    """独立 AB 滑条对照预览窗口：比对两段视频的重合部分，支持色度渐变模拟与接缝开关联动。"""

    def __init__(
        self,
        master: tk.Misc,
        info_a: VideoInfo,
        info_b: VideoInfo,
        overlap_frames: int,
        ffmpeg_exe: str,
        initial_blend_enabled: Optional[bool] = None,
        on_toggle_blend: Optional[Callable[[bool], None]] = None,
    ) -> None:
        super().__init__(master)
        self.info_a = info_a
        self.info_b = info_b
        self.overlap_frames = max(0, overlap_frames)
        self.compare_count = max(1, self.overlap_frames)
        self.ffmpeg_exe = ffmpeg_exe
        self.on_toggle_blend = on_toggle_blend

        self.title(f"独立 AB 滑条对照 - {info_a.path.name} vs {info_b.path.name}")
        self.geometry("980x720")
        self.minsize(760, 560)

        self.frames_a: List[Image.Image] = []
        self.frames_b: List[Image.Image] = []
        self.pw = 864
        self.ph = 486
        self.current_idx = 0
        self.split_ratio = 0.5
        self.playing = False
        self._play_after_id: Optional[str] = None
        self._tk_img: Optional[ImageTk.PhotoImage] = None
        self._load_queue: queue.Queue = queue.Queue()

        self.view_mode = tk.StringVar(value="wipe")  # wipe | blend | only_a | only_b
        self.split_var = tk.DoubleVar(value=50.0)
        self.frame_var = tk.IntVar(value=1)
        self.blend_var = tk.BooleanVar(value=bool(initial_blend_enabled))

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_load_frames()

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill=tk.X)

        info_text = (
            f"A（上段尾部）: {self.info_a.path.name} [{self.info_a.width}x{self.info_a.height}, {self.info_a.frame_count}帧]   "
            f"◀——▶   "
            f"B（下段头部）: {self.info_b.path.name} [{self.info_b.width}x{self.info_b.height}, {self.info_b.frame_count}帧]"
        )
        ttk.Label(top, text=info_text, font=("Microsoft YaHei UI", 9, "bold")).pack(anchor=tk.W)

        sub = ttk.Frame(self, padding=(10, 0, 10, 6))
        sub.pack(fill=tk.X)

        ttk.Label(sub, text="预览模式:").pack(side=tk.LEFT)
        for val, lbl in [
            ("wipe", "AB 滑条对照"),
            ("blend", "色度渐变融合模拟"),
            ("only_a", "仅看 A 尾"),
            ("only_b", "仅看 B 头"),
        ]:
            ttk.Radiobutton(
                sub,
                text=lbl,
                value=val,
                variable=self.view_mode,
                command=self._render_current,
            ).pack(side=tk.LEFT, padx=(6, 4))

        if self.on_toggle_blend is not None and self.overlap_frames > 0:
            ttk.Separator(sub, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
            ttk.Checkbutton(
                sub,
                text=f"在此接缝启用 {self.overlap_frames} 帧色度渐变融合",
                variable=self.blend_var,
                command=self._handle_blend_toggle,
            ).pack(side=tk.LEFT)

        canvas_wrap = ttk.Frame(self, padding=(10, 2))
        canvas_wrap.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(canvas_wrap, bg="#18181b", highlightthickness=0, cursor="sb_h_double_arrow")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_drag)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<Configure>", lambda _e: self._render_current())

        self.status_lbl = ttk.Label(self, text="正在从视频提取重合区间帧...", padding=(10, 4))
        self.status_lbl.pack(fill=tk.X)

        split_row = ttk.Frame(self, padding=(10, 2))
        split_row.pack(fill=tk.X)
        ttk.Label(split_row, text="AB 分割滑条 (左A / 右B):", width=22).pack(side=tk.LEFT)
        self.split_scale = ttk.Scale(
            split_row,
            from_=0.0,
            to=100.0,
            variable=self.split_var,
            command=self._on_split_slider,
        )
        self.split_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.split_pct_lbl = ttk.Label(split_row, text="50%", width=6)
        self.split_pct_lbl.pack(side=tk.LEFT)
        ttk.Button(split_row, text="居中 50%", command=self._reset_split).pack(side=tk.LEFT, padx=(4, 0))

        frame_row = ttk.Frame(self, padding=(10, 4, 10, 10))
        frame_row.pack(fill=tk.X)
        ttk.Label(frame_row, text=f"重合帧位置 (共{self.compare_count}帧):", width=22).pack(side=tk.LEFT)
        self.frame_scale = ttk.Scale(
            frame_row,
            from_=1,
            to=max(1, self.compare_count),
            variable=self.frame_var,
            command=self._on_frame_slider,
        )
        self.frame_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        ttk.Button(frame_row, text="◀ 上一帧", command=lambda: self._step_frame(-1)).pack(side=tk.LEFT, padx=2)
        self.play_btn = ttk.Button(frame_row, text="▶ 循环播放", command=self._toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(frame_row, text="下一帧 ▶", command=lambda: self._step_frame(1)).pack(side=tk.LEFT, padx=2)

    def _compute_preview_size(self) -> Tuple[int, int]:
        max_w = 900
        w = min(max_w, max(320, self.info_a.width))
        w = w - (w % 2)
        h = int(round(self.info_a.height * w / max(1, self.info_a.width)))
        h = max(180, h - (h % 2))
        if h > 520:
            h = 520
            w = int(round(self.info_a.width * h / max(1, self.info_a.height)))
            w = max(240, w - (w % 2))
        return w, h

    def _start_load_frames(self) -> None:
        self.pw, self.ph = self._compute_preview_size()

        def worker() -> None:
            try:
                cnt = self.compare_count
                total_a = self.info_a.frame_count
                start_a = max(0, total_a - cnt - 3) if total_a > cnt else 0
                raw_a = extract_frames_rgb(self.info_a.path, start_a, None, self.pw, self.ph, self.ffmpeg_exe)
                raw_b = extract_frames_rgb(self.info_b.path, 0, cnt, self.pw, self.ph, self.ffmpeg_exe)

                if len(raw_a) > cnt:
                    raw_a = raw_a[-cnt:]
                if len(raw_b) > cnt:
                    raw_b = raw_b[:cnt]

                imgs_a = [Image.frombytes("RGB", (self.pw, self.ph), b) for b in raw_a]
                imgs_b = [Image.frombytes("RGB", (self.pw, self.ph), b) for b in raw_b]
                self._load_queue.put(("ok", imgs_a, imgs_b))
            except Exception as exc:
                self._load_queue.put(("err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(40, self._poll_load_queue)

    def _poll_load_queue(self) -> None:
        if not self.winfo_exists():
            return
        try:
            item = self._load_queue.get_nowait()
        except queue.Empty:
            self.after(40, self._poll_load_queue)
            return
        if item[0] == "ok":
            self._on_frames_loaded(item[1], item[2])
        else:
            self.status_lbl.config(text=f"提取失败: {item[1]}")

    def _on_frames_loaded(self, imgs_a: List[Image.Image], imgs_b: List[Image.Image]) -> None:
        if not self.winfo_exists():
            return
        n = min(len(imgs_a), len(imgs_b))
        if n == 0:
            self.status_lbl.config(text="未能提取到有效对比帧，请检查视频文件。")
            return
        self.frames_a = imgs_a[-n:]
        self.frames_b = imgs_b[:n]
        self.compare_count = n
        self.frame_scale.configure(to=n)
        self.current_idx = 0
        self.frame_var.set(1)
        self._render_current()

    def _handle_blend_toggle(self) -> None:
        if self.on_toggle_blend is not None:
            self.on_toggle_blend(self.blend_var.get())
        if self.blend_var.get():
            self.view_mode.set("blend")
            self._render_current()

    def _on_split_slider(self, _val: str) -> None:
        pct = self.split_var.get()
        self.split_ratio = max(0.0, min(1.0, pct / 100.0))
        self.split_pct_lbl.config(text=f"{int(round(pct))}%")
        if self.view_mode.get() != "wipe":
            self.view_mode.set("wipe")
        self._render_current()

    def _reset_split(self) -> None:
        self.split_var.set(50.0)
        self._on_split_slider("50")

    def _on_frame_slider(self, _val: str) -> None:
        if not self.frames_a:
            return
        idx = int(round(self.frame_var.get())) - 1
        idx = max(0, min(len(self.frames_a) - 1, idx))
        if idx != self.current_idx:
            self.current_idx = idx
            self._render_current()

    def _step_frame(self, delta: int) -> None:
        if not self.frames_a:
            return
        self.current_idx = (self.current_idx + delta) % len(self.frames_a)
        self.frame_var.set(self.current_idx + 1)
        self._render_current()

    def _toggle_play(self) -> None:
        if not self.frames_a:
            return
        self.playing = not self.playing
        self.play_btn.config(text="⏸ 暂停" if self.playing else "▶ 循环播放")
        if self.playing:
            self._tick_play()
        elif self._play_after_id:
            self.after_cancel(self._play_after_id)
            self._play_after_id = None

    def _tick_play(self) -> None:
        if not self.playing or not self.winfo_exists():
            return
        self._step_frame(1)
        fps = max(1.0, float(self.info_a.fps))
        delay_ms = max(15, int(round(1000.0 / fps)))
        self._play_after_id = self.after(delay_ms, self._tick_play)

    def _on_canvas_drag(self, event: tk.Event) -> None:
        if not self.frames_a:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        scale = min(cw / self.pw, ch / self.ph)
        disp_w = max(1, int(self.pw * scale))
        offset_x = (cw - disp_w) // 2
        rel = (event.x - offset_x) / disp_w
        pct = max(0.0, min(100.0, rel * 100.0))
        self.split_var.set(pct)
        self._on_split_slider(str(pct))

    def _compose_frame(self, idx: int) -> Image.Image:
        img_a = self.frames_a[idx]
        img_b = self.frames_b[idx]
        mode = self.view_mode.get()
        n = len(self.frames_a)

        if mode == "only_a":
            return img_a
        if mode == "only_b":
            return img_b
        if mode == "blend":
            alpha = (idx + 1) / (n + 1)
            return Image.blend(img_a, img_b, alpha)

        split_x = int(round(self.pw * self.split_ratio))
        out = img_b.copy()
        if split_x > 0:
            crop_a = img_a.crop((0, 0, split_x, self.ph))
            out.paste(crop_a, (0, 0))
        draw = ImageDraw.Draw(out)
        if 0 < split_x < self.pw:
            draw.line([(split_x, 0), (split_x, self.ph)], fill=(255, 215, 0), width=2)
            cy = self.ph // 2
            draw.ellipse([(split_x - 7, cy - 7), (split_x + 7, cy + 7)], fill=(255, 215, 0), outline=(24, 24, 27))
        return out

    def _render_current(self) -> None:
        if not self.frames_a or not self.winfo_exists():
            return
        idx = self.current_idx
        n = len(self.frames_a)
        composed = self._compose_frame(idx)

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        scale = min(cw / self.pw, ch / self.ph)
        disp_w = max(1, int(self.pw * scale))
        disp_h = max(1, int(self.ph * scale))
        resized = composed.resize((disp_w, disp_h), Image.Resampling.BILINEAR)

        self._tk_img = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img, anchor=tk.CENTER)

        a_orig_idx = max(1, self.info_a.frame_count - n + idx + 1)
        b_orig_idx = idx + 1
        alpha = (idx + 1) / (n + 1)
        mode_names = {
            "wipe": f"AB 滑条对照 (可直接在画面左右拖动分割线 | 左A {int(round(self.split_ratio * 100))}% / 右B)",
            "blend": f"色度渐变融合模拟 (当前帧混合权重: A {(1 - alpha) * 100:.1f}% + B {alpha * 100:.1f}%)",
            "only_a": "仅显示 A（上段尾部）",
            "only_b": "仅显示 B（下段头部）",
        }
        self.status_lbl.config(
            text=(
                f"重合第 {idx + 1}/{n} 帧  |  A原第 {a_orig_idx} 帧  vs  B原第 {b_orig_idx} 帧  |  "
                f"{mode_names[self.view_mode.get()]}"
            )
        )

    def _on_close(self) -> None:
        self.playing = False
        if self._play_after_id:
            self.after_cancel(self._play_after_id)
        self.destroy()


class VideoMergeGUI:
    def __init__(self, root: tk.Tk, initial_inputs: Sequence[str], enable_ipc_poll: bool = True) -> None:
        self.root = root
        self.root.title("FFmpeg 多段视频拼接工具（支持直接拖入 & 首尾帧缩略图 & AB 滑条对照）")
        self.root.geometry("1180x740")
        self.root.minsize(980, 600)

        self.ffmpeg_exe = find_executable("ffmpeg")
        self.ffprobe_exe = find_executable("ffprobe")

        self.infos: List[VideoInfo] = []
        self.blend_junctions: List[bool] = []
        self.checked_paths: List[Path] = []
        self.is_merging = False

        self._thumb_cache: Dict[Path, ImageTk.PhotoImage] = {}
        self._placeholder_thumb = make_placeholder_thumbnail()
        self._thumb_queue: queue.Queue = queue.Queue()
        self._thumb_pending: set[Path] = set()

        self.trim_frames_var = tk.IntVar(value=DEFAULT_TRIM_FRAMES)
        self.encoder_var = tk.StringVar(value="auto")
        self.output_var = tk.StringVar(value="")
        self.custom_output = False

        self._build_ui()
        self.root.bind("<Control-v>", lambda _e: self.paste_from_clipboard())
        self.root.bind("<Control-V>", lambda _e: self.paste_from_clipboard())

        self._drop_helper = Win32DropHelper(self.root, self._on_files_dropped)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if initial_inputs:
            self.add_paths(initial_inputs, sort_numeric=True)

        self.root.after(80, self._poll_thumb_queue)
        if enable_ipc_poll:
            self.root.after(300, self._poll_ipc_queue)

    def _build_ui(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        bar.pack(fill=tk.X)

        ttk.Button(bar, text="+ 添加视频", command=self.add_files_dialog).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bar, text="+ 添加文件夹", command=self.add_folder_dialog).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="粘贴文件 (Ctrl+V)", command=self.paste_from_clipboard).pack(side=tk.LEFT, padx=4)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(bar, text="按文件名数字排序 (小→大)", command=self.sort_by_numbers).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="↑ 上移", command=lambda: self.move_selected(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="↓ 下移", command=lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(bar, text="移除选中", command=self.remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="清空", command=self.clear_all).pack(side=tk.LEFT, padx=2)

        tip = ttk.Label(
            self.root,
            text=(
                "直接把视频文件或文件夹【拖进本窗口】即可添加（单个拖入按拖入顺序排列；整批/文件夹拖入自动按数字排序）。"
                "每行左侧显示「首帧 → 尾帧」缩略图；勾选2个视频可开独立 AB 滑条对照窗口，双击行切换接缝是否做色度渐变。"
            ),
            foreground="#4b5563",
            padding=(10, 0, 10, 6),
            wraplength=1100,
            justify=tk.LEFT,
        )
        tip.pack(fill=tk.X)
        tip.bind("<Configure>", lambda e: tip.config(wraplength=max(400, e.width - 20)))

        # 先固定底部区域，再让中间列表自动伸缩，确保小窗口下底部按钮永不被挤出屏幕
        bot = ttk.Frame(self.root, padding=(10, 4, 10, 10))
        bot.pack(side=tk.BOTTOM, fill=tk.X)

        self.summary_lbl = ttk.Label(bot, text="请先拖入或添加至少 2 个视频。", font=("Microsoft YaHei UI", 9))
        self.summary_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.merge_btn = ttk.Button(bot, text="开始合并输出", command=self.start_merge)
        self.merge_btn.pack(side=tk.RIGHT, ipadx=14, ipady=4)

        cfg = ttk.LabelFrame(self.root, text="拼接参数设置", padding=(10, 8))
        cfg.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 6))

        row1 = ttk.Frame(cfg)
        row1.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(row1, text="第2段起重叠/裁剪帧数:").pack(side=tk.LEFT)
        spin = ttk.Spinbox(
            row1,
            from_=0,
            to=500,
            width=7,
            textvariable=self.trim_frames_var,
            command=self._on_trim_changed,
        )
        spin.pack(side=tk.LEFT, padx=(6, 4))
        spin.bind("<KeyRelease>", lambda _e: self._on_trim_changed())
        spin.bind("<FocusOut>", lambda _e: self._on_trim_changed())

        for preset, label in [(22, "22帧(通用)"), (1, "1帧(首尾帧)"), (0, "0帧(直连)")]:
            ttk.Button(row1, text=label, command=lambda v=preset: self._set_trim_preset(v)).pack(side=tk.LEFT, padx=2)

        self.formula_lbl = ttk.Label(row1, text="", foreground="#1d4ed8")
        self.formula_lbl.pack(side=tk.LEFT, padx=(14, 0))

        row2 = ttk.Frame(cfg)
        row2.pack(fill=tk.X)

        ttk.Label(row2, text="编码器:").pack(side=tk.LEFT)
        ttk.Combobox(
            row2,
            textvariable=self.encoder_var,
            values=["auto", "nvenc", "x264"],
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT, padx=(6, 14))

        ttk.Label(row2, text="输出文件:").pack(side=tk.LEFT)
        out_entry = ttk.Entry(row2, textvariable=self.output_var)
        out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        out_entry.bind("<KeyRelease>", lambda _e: setattr(self, "custom_output", True))
        ttk.Button(row2, text="浏览...", command=self.browse_output).pack(side=tk.LEFT)

        mid = ttk.Frame(self.root, padding=(10, 8))
        mid.pack(side=tk.BOTTOM, fill=tk.X)

        ttk.Button(mid, text="打开独立 AB 滑条对照窗口", command=self.open_ab_compare).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(mid, text="切换当前接缝 (硬切 ↔ 色度渐变)", command=self.toggle_selected_junction).pack(side=tk.LEFT, padx=4)
        ttk.Button(mid, text="全部接缝设为色度渐变", command=lambda: self.set_all_junctions(True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(mid, text="全部接缝设为硬切去重", command=lambda: self.set_all_junctions(False)).pack(side=tk.LEFT, padx=4)

        tree_wrap = ttk.Frame(self.root, padding=(10, 0))
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self.root)
        style.configure("Thumb.Treeview", rowheight=COMP_H + 8)

        cols = ("chk", "idx", "name", "spec", "audio", "junction")
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=cols,
            show="tree headings",
            selectmode="browse",
            style="Thumb.Treeview",
        )
        self.tree.heading("#0", text="首帧 → 尾帧 缩略图")
        self.tree.heading("chk", text="AB对照")
        self.tree.heading("idx", text="序号")
        self.tree.heading("name", text="文件名")
        self.tree.heading("spec", text="规格 (分辨率 / 帧率 / 总帧数 / 时长)")
        self.tree.heading("audio", text="音轨")
        self.tree.heading("junction", text="与上一段衔接方式 (双击切换)")

        self.tree.column("#0", width=COMP_W + 22, minwidth=COMP_W + 16, anchor=tk.CENTER, stretch=False)
        self.tree.column("chk", width=62, anchor=tk.CENTER, stretch=False)
        self.tree.column("idx", width=48, anchor=tk.CENTER, stretch=False)
        self.tree.column("name", width=220, anchor=tk.W)
        self.tree.column("spec", width=250, anchor=tk.W)
        self.tree.column("audio", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("junction", width=210, anchor=tk.CENTER)

        ysb = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        self._update_formula_and_summary()

    def _on_files_dropped(self, raw_items: List[str]) -> None:
        # 单个文件拖入保持顺序；一次拖入多个文件或文件夹自动按数字排序
        has_dir = any(Path(p).is_dir() for p in raw_items if Path(p).exists())
        self.add_paths(raw_items, sort_numeric=(has_dir or len(raw_items) > 1))

    def _poll_ipc_queue(self) -> None:
        if not self.root.winfo_exists():
            return
        if IPC_QUEUE_FILE.exists():
            try:
                content = IPC_QUEUE_FILE.read_text(encoding="utf-8")
                IPC_QUEUE_FILE.unlink(missing_ok=True)
                incoming: List[str] = []
                for line in content.splitlines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    incoming.extend(data.get("paths", []))
                if incoming:
                    has_dir = any(Path(p).is_dir() for p in incoming if Path(p).exists())
                    self.add_paths(incoming, sort_numeric=(has_dir or len(incoming) > 1))
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass
        self.root.after(300, self._poll_ipc_queue)

    def _schedule_thumbnail(self, path: Path) -> None:
        rp = path.resolve()
        if rp in self._thumb_cache or rp in self._thumb_pending:
            return
        self._thumb_pending.add(rp)

        def worker() -> None:
            try:
                first_raw, last_raw = extract_first_last_thumbnails(path, THUMB_W, THUMB_H, self.ffmpeg_exe)
                self._thumb_queue.put((rp, first_raw, last_raw))
            except Exception:
                self._thumb_queue.put((rp, None, None))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_thumb_queue(self) -> None:
        if not self.root.winfo_exists():
            return
        updated = False
        while True:
            try:
                rp, first_raw, last_raw = self._thumb_queue.get_nowait()
            except queue.Empty:
                break
            self._thumb_pending.discard(rp)
            if first_raw or last_raw:
                self._thumb_cache[rp] = compose_first_last_thumbnail(first_raw, last_raw)
                updated = True
        if updated:
            self._refresh_tree()
        self.root.after(80, self._poll_thumb_queue)

    def _safe_trim_frames(self) -> int:
        try:
            return max(0, int(self.trim_frames_var.get()))
        except Exception:
            return DEFAULT_TRIM_FRAMES

    def _set_trim_preset(self, val: int) -> None:
        self.trim_frames_var.set(val)
        self._on_trim_changed()

    def _on_trim_changed(self) -> None:
        self._refresh_tree()
        self._update_formula_and_summary()

    def _sync_junctions_len(self) -> None:
        needed = max(0, len(self.infos) - 1)
        if len(self.blend_junctions) < needed:
            self.blend_junctions.extend([False] * (needed - len(self.blend_junctions)))
        elif len(self.blend_junctions) > needed:
            self.blend_junctions = self.blend_junctions[:needed]

    def add_paths(self, raw_items: Sequence[str], sort_numeric: bool = False) -> None:
        new_paths = expand_input_paths(raw_items)
        if sort_numeric:
            new_paths.sort(key=numeric_sort_key)

        existing = {info.path.resolve() for info in self.infos}
        added_paths: List[Path] = []
        for p in new_paths:
            rp = p.resolve()
            if rp in existing:
                continue
            try:
                info = probe_video(p, self.ffprobe_exe)
            except Exception as exc:
                messagebox.showerror("读取视频失败", f"{p.name}\n{exc}", parent=self.root)
                continue
            self.infos.append(info)
            existing.add(rp)
            added_paths.append(p)

        if added_paths:
            self._sync_junctions_len()
            if not self.custom_output and self.infos:
                self.output_var.set(str(default_output_path(self.infos)))
            for p in added_paths:
                self._schedule_thumbnail(p)
            self._refresh_tree()
            self._update_formula_and_summary()

    def add_files_dialog(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择视频文件（可多选，也可逐个添加保持顺序）",
            filetypes=[
                ("视频文件", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.ts *.flv"),
                ("所有文件", "*.*"),
            ],
            parent=self.root,
        )
        if files:
            self.add_paths(files, sort_numeric=False)

    def add_folder_dialog(self) -> None:
        folder = filedialog.askdirectory(title="选择包含分段视频的文件夹", parent=self.root)
        if folder:
            self.add_paths([folder], sort_numeric=True)

    def paste_from_clipboard(self) -> None:
        paths = read_clipboard_paths_win()
        if not paths:
            messagebox.showinfo("提示", "剪贴板里没有检测到有效视频文件或路径。", parent=self.root)
            return
        self.add_paths(paths, sort_numeric=(len(paths) > 1))

    def sort_by_numbers(self) -> None:
        if len(self.infos) < 2:
            return
        self.infos.sort(key=lambda x: numeric_sort_key(x.path))
        self._refresh_tree()
        self._update_formula_and_summary()

    def move_selected(self, delta: int) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.infos)):
            return
        self.infos[idx], self.infos[new_idx] = self.infos[new_idx], self.infos[idx]
        self._refresh_tree(select_idx=new_idx)
        self._update_formula_and_summary()

    def remove_selected(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        removed = self.infos.pop(idx)
        rp = removed.path.resolve()
        self.checked_paths = [p for p in self.checked_paths if p != rp]
        self._thumb_cache.pop(rp, None)
        self._sync_junctions_len()
        self._refresh_tree(select_idx=min(idx, len(self.infos) - 1) if self.infos else None)
        self._update_formula_and_summary()

    def clear_all(self) -> None:
        self.infos.clear()
        self.blend_junctions.clear()
        self.checked_paths.clear()
        self._thumb_cache.clear()
        self.custom_output = False
        self.output_var.set("")
        self._refresh_tree()
        self._update_formula_and_summary()

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _refresh_tree(self, select_idx: Optional[int] = None) -> None:
        prev_sel = select_idx if select_idx is not None else self._selected_index()
        self.tree.delete(*self.tree.get_children())
        trim_frames = self._safe_trim_frames()

        for i, info in enumerate(self.infos):
            rp = info.path.resolve()
            thumb = self._thumb_cache.get(rp, self._placeholder_thumb)
            checked = "☑" if rp in self.checked_paths else "☐"
            spec = f"{info.width}x{info.height} | {format_fps(info.fps)} fps | {info.frame_count}帧 ({info.duration:.2f}s)"
            audio = "有" if info.has_audio else "无"
            if i == 0:
                j_text = "首段 (完整保留)"
            elif trim_frames == 0:
                j_text = "无重叠直接拼接"
            elif self.blend_junctions[i - 1]:
                j_text = f"色度渐变融合 ({trim_frames}帧)"
            else:
                j_text = f"硬切去重 (裁前{trim_frames}帧)"

            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                text="",
                image=thumb,
                values=(checked, i + 1, info.path.name, spec, audio, j_text),
            )

        if prev_sel is not None and 0 <= prev_sel < len(self.infos):
            self.tree.selection_set(str(prev_sel))

    def _update_formula_and_summary(self) -> None:
        trim_frames = self._safe_trim_frames()
        if not self.infos:
            self.formula_lbl.config(text=f"音频裁剪公式: {trim_frames} ÷ 24 fps = {trim_frames / 24:.4f} 秒")
            self.summary_lbl.config(text="请先拖入或添加至少 2 个视频。")
            return

        ref_fps = self.infos[0].fps
        trim_sec = calc_trim_seconds(trim_frames, ref_fps)
        fps_str = format_fps(ref_fps)
        self.formula_lbl.config(text=f"音频裁剪公式(自动): {trim_frames} ÷ {fps_str} fps = {trim_sec:.4f} 秒")

        total_frames = sum(info.frame_count for info in self.infos) - max(0, len(self.infos) - 1) * trim_frames
        total_frames = max(0, total_frames)
        est_sec = total_frames / float(ref_fps)
        blend_cnt = sum(1 for x in self.blend_junctions if x) if trim_frames > 0 else 0
        self.summary_lbl.config(
            text=(
                f"共 {len(self.infos)} 段视频 | 已启用色度渐变接缝: {blend_cnt} 处 | "
                f"预计成片: {total_frames} 帧（约 {est_sec:.2f} 秒 @ {fps_str} fps）"
            )
        )

    def _on_tree_click(self, event: tk.Event) -> None:
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        idx = int(row_id)
        if col == "#1":
            rp = self.infos[idx].path.resolve()
            if rp in self.checked_paths:
                self.checked_paths.remove(rp)
            else:
                if len(self.checked_paths) >= 2:
                    self.checked_paths.pop(0)
                self.checked_paths.append(rp)
            self._refresh_tree(select_idx=idx)

    def _on_tree_double_click(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        idx = int(row_id)
        if idx >= 1:
            self._toggle_junction_at(idx - 1)

    def _toggle_junction_at(self, j_idx: int) -> None:
        if 0 <= j_idx < len(self.blend_junctions):
            self.blend_junctions[j_idx] = not self.blend_junctions[j_idx]
            self._refresh_tree()
            self._update_formula_and_summary()

    def toggle_selected_junction(self) -> None:
        idx = self._selected_index()
        if idx is None or idx == 0:
            messagebox.showinfo("提示", "请先选中第 2 段或之后的某一行，再切换它与上一段的衔接方式。", parent=self.root)
            return
        self._toggle_junction_at(idx - 1)

    def set_all_junctions(self, enabled: bool) -> None:
        self.blend_junctions = [enabled] * max(0, len(self.infos) - 1)
        self._refresh_tree()
        self._update_formula_and_summary()

    def open_ab_compare(self) -> None:
        if len(self.infos) < 2:
            messagebox.showinfo("提示", "至少需要 2 个视频才能做 AB 对照预览。", parent=self.root)
            return

        checked_indices = [i for i, info in enumerate(self.infos) if info.path.resolve() in self.checked_paths]
        if len(checked_indices) == 2:
            idx_a, idx_b = checked_indices[0], checked_indices[1]
        else:
            sel = self._selected_index()
            if sel is None or sel == 0:
                idx_a, idx_b = 0, 1
            else:
                idx_a, idx_b = sel - 1, sel
            self.checked_paths = [self.infos[idx_a].path.resolve(), self.infos[idx_b].path.resolve()]
            self._refresh_tree(select_idx=idx_b)

        info_a = self.infos[idx_a]
        info_b = self.infos[idx_b]
        is_adjacent = idx_b == idx_a + 1
        init_blend = self.blend_junctions[idx_a] if is_adjacent else None

        def on_toggle(val: bool) -> None:
            if is_adjacent and idx_a < len(self.blend_junctions):
                self.blend_junctions[idx_a] = val
                self._refresh_tree()
                self._update_formula_and_summary()

        ABCompareWindow(
            master=self.root,
            info_a=info_a,
            info_b=info_b,
            overlap_frames=self._safe_trim_frames(),
            ffmpeg_exe=self.ffmpeg_exe,
            initial_blend_enabled=init_blend,
            on_toggle_blend=on_toggle if is_adjacent else None,
        )

    def browse_output(self) -> None:
        init_dir = str(self.infos[0].path.parent) if self.infos else os.getcwd()
        chosen = filedialog.asksaveasfilename(
            title="选择合并输出路径",
            initialdir=init_dir,
            initialfile=Path(self.output_var.get()).name if self.output_var.get() else "merged.mp4",
            defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4")],
            parent=self.root,
        )
        if chosen:
            self.custom_output = True
            self.output_var.set(chosen)

    def start_merge(self) -> None:
        if self.is_merging:
            return
        if len(self.infos) < 2:
            messagebox.showwarning("提示", "至少需要 2 个视频才能合并。", parent=self.root)
            return

        out_str = self.output_var.get().strip()
        out_path = Path(out_str) if out_str else default_output_path(self.infos)
        trim_frames = self._safe_trim_frames()
        encoder = self.encoder_var.get()
        infos_snapshot = list(self.infos)
        junctions_snapshot = list(self.blend_junctions)

        self.is_merging = True
        self._merge_queue: queue.Queue = queue.Queue()
        self.merge_btn.config(state=tk.DISABLED, text="正在合并...")
        self.summary_lbl.config(text="正在调用 FFmpeg 流式裁剪合并，请稍候...")

        def worker() -> None:
            try:
                from datetime import datetime

                t0 = datetime.now()
                run_ffmpeg_merge(
                    infos=infos_snapshot,
                    output_path=out_path,
                    trim_frames=trim_frames,
                    target_fps=None,
                    encoder_mode=encoder,
                    ffmpeg_exe=self.ffmpeg_exe,
                    blend_junctions=junctions_snapshot,
                    log_cb=lambda msg: self._merge_queue.put(("log", msg)),
                )
                elapsed = (datetime.now() - t0).total_seconds()
                self._merge_queue.put(("done", out_path, elapsed, None))
            except Exception as exc:
                self._merge_queue.put(("done", out_path, 0.0, exc))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(50, self._poll_merge_queue)

    def _poll_merge_queue(self) -> None:
        if not self.root.winfo_exists():
            return
        finished = False
        while True:
            try:
                item = self._merge_queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == "log":
                self.summary_lbl.config(text=item[1])
            elif item[0] == "done":
                finished = True
                self._on_merge_done(item[1], item[2], item[3])
                break
        if not finished and self.is_merging:
            self.root.after(50, self._poll_merge_queue)

    def _on_merge_done(self, out_path: Path, elapsed: float, err: Optional[Exception]) -> None:
        self.is_merging = False
        self.merge_btn.config(state=tk.NORMAL, text="开始合并输出")
        self._update_formula_and_summary()
        if err is not None:
            messagebox.showerror("合并失败", str(err), parent=self.root)
            return
        if not self.custom_output and self.infos:
            self.output_var.set(str(default_output_path(self.infos)))
        if messagebox.askyesno(
            "合并完成",
            f"合并成功！耗时 {elapsed:.2f} 秒\n\n输出文件：\n{out_path}\n\n是否打开输出文件所在文件夹？",
            parent=self.root,
        ):
            subprocess.Popen(["explorer", "/select,", str(out_path)])

    def _on_close(self) -> None:
        self._drop_helper.uninstall()
        self.root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--check-launch" in args:
        return 0
    mutex_handle, is_primary = acquire_single_instance_mutex()
    if not is_primary:
        send_paths_to_primary(args)
        return 0

    IPC_QUEUE_FILE.unlink(missing_ok=True)
    root = tk.Tk()
    try:
        VideoMergeGUI(root, args, enable_ipc_poll=True)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("启动失败", str(exc))
        root.destroy()
        IPC_QUEUE_FILE.unlink(missing_ok=True)
        if mutex_handle and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(mutex_handle)
        return 1
    try:
        root.mainloop()
    finally:
        IPC_QUEUE_FILE.unlink(missing_ok=True)
        if mutex_handle and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(mutex_handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

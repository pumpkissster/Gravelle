#!/usr/bin/env python3
"""Windows GUI entry point; service, diagnostics and password recovery need no Tk."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import webbrowser

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The frozen server also runs isolated image jobs. Dispatch before loading the
# launcher/runtime so a worker cannot start Tk or acquire the application lock.
if __name__ == "__main__" and sys.argv[1:2] == ["--pdf-worker"]:
    from pdf_reader import worker_main
    raise SystemExit(worker_main(sys.argv[2:]))
if __name__ == "__main__" and sys.argv[1:2] == ["--transcode-worker"]:
    from transcoding import worker_main
    raise SystemExit(worker_main(sys.argv[2:]))
if __name__ == "__main__" and sys.argv[1:2] == ["--image-search-worker"]:
    from image_search import worker_main
    raise SystemExit(worker_main(sys.argv[2:]))

from desktop.core import (LauncherConfig, LauncherError, SingleInstanceLock, check_update,
                          download_update, load_config, load_release_config, network_diagnostics,
                          save_config, security_entry_recovery_warnings, user_directory, verify_installer)
from desktop.runtime import ServerController, reset_security_entry_cli, resource_root, serve

APP_MUTEX = r"Local\GravelleLauncher-6589345A87B047CE942471F892240DF7"


def rounded_button_surface(scale: float, fill: str, border: str, focus: str | None = None):
    """Draw a small antialiased surface for ttk's stretchable image element."""
    from PIL import Image, ImageDraw
    px = lambda value: max(1, round(value * scale))
    radius, edge = px(10), px(1)
    # Keep the rounded ends in the nine-slice border; only straight edges and
    # the center stretch when a translated label or window changes its width.
    slice_width = px(12)
    side, sample = slice_width * 2 + px(2), 4
    image = Image.new("RGBA", (side * sample, side * sample))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, side * sample - 1, side * sample - 1),
                           radius=radius * sample, fill=fill, outline=border, width=edge * sample)
    if focus:
        inset = px(3) * sample
        draw.rounded_rectangle((inset, inset, side * sample - 1 - inset, side * sample - 1 - inset),
                               radius=max(sample, radius * sample - inset), outline=focus,
                               width=px(2) * sample)
    return image.resize((side, side), Image.Resampling.LANCZOS)


def launcher_window_metrics(screen_width: int, screen_height: int, scale: float) -> dict:
    """Keep a usable window inside the desktop, including high-DPI laptops."""
    scale = min(3.0, max(0.9, float(scale)))
    available_width = max(1, screen_width - round(min(48 * scale, screen_width * 0.08)))
    available_height = max(1, screen_height - round(min(88 * scale, screen_height * 0.12)))
    width = min(round(700 * scale), available_width)
    height = min(round(720 * scale), available_height)
    return {"width": width, "height": height, "min_width": min(round(520 * scale), width),
            "min_height": min(round(480 * scale), height), "scale": scale}


def enable_high_dpi() -> str:
    """Set DPI awareness before Tk exists; an installer manifest takes priority."""
    if os.name != "nt":
        return "not_windows"
    import ctypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    modern = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if modern is not None:
        modern.argtypes = [ctypes.c_void_p]
        modern.restype = ctypes.c_bool
        if modern(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return "per_monitor_v2"
        if ctypes.get_last_error() == 5:  # The executable manifest configured it.
            return "already_configured"
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        legacy = shcore.SetProcessDpiAwareness
        legacy.argtypes = [ctypes.c_int]
        legacy.restype = ctypes.c_long
        result = legacy(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        if result == 0:
            return "per_monitor"
        if result in (-2147024891, 2147942405):  # E_ACCESSDENIED
            return "already_configured"
    except (OSError, AttributeError):
        pass
    fallback = getattr(user32, "SetProcessDPIAware", None)
    if fallback is not None and fallback():
        return "system_aware"
    return "unavailable"


class InstallerLaunchError(LauncherError):
    def __init__(self, message: str, lock_recovered: bool):
        super().__init__(message)
        self.lock_recovered = lock_recovered


def launch_verified_installer(path: Path, sha256: str, instance_lock: SingleInstanceLock):
    """Release Inno's AppMutex before starting it; recover the lock on failure."""
    if not verify_installer(path, sha256):
        raise LauncherError("安装程序校验失败，请重新下载。")
    try:
        instance_lock.release()
        return subprocess.Popen([str(path)], shell=False)
    except OSError as error:
        try:
            recovered = instance_lock.acquire()
        except (OSError, LauncherError):
            recovered = False
        raise InstallerLaunchError("无法打开安装程序：" + str(error), recovered) from error


def _restore_child_stdio():
    """PyInstaller --windowed clears Python streams even for inherited pipes."""
    if os.name != "nt":
        return
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    for name, handle_id, mode, flags in (("stdin", -10, "r", os.O_RDONLY),
                                         ("stdout", -11, "w", os.O_WRONLY),
                                         ("stderr", -12, "w", os.O_WRONLY)):
        if getattr(sys, name) is None:
            handle = kernel.GetStdHandle(handle_id & 0xffffffff)
            if not handle or handle == ctypes.c_void_p(-1).value:
                raise LauncherError("启动器未收到服务控制管道。")
            fd = msvcrt.open_osfhandle(handle, flags | os.O_BINARY)
            setattr(sys, name, os.fdopen(fd, mode, encoding="utf-8", buffering=1))


class LauncherWindow:
    def __init__(self, root, config_path: Path, storage: Path, instance_lock: SingleInstanceLock):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.config_path = config_path
        self.storage = storage
        self.instance_lock = instance_lock
        self.controller = ServerController(storage / "data", storage / "logs" / "server.log")
        self.events = queue.Queue()
        self.busy = False
        self.closing = False
        self.release = None
        self.downloaded = None
        self.release_config = None
        from server import VERSION
        self.version = VERSION
        configuration_error = None
        try:
            config = load_config(config_path)
        except LauncherError as error:
            config = LauncherConfig()
            configuration_error = str(error)
        self.library_values = [tk.StringVar(value=path) for path in config.library_paths]
        self.folder_widgets = []
        self.port = tk.StringVar(value=str(config.port))
        self.share_port = tk.StringVar(value=str(config.share_port))
        self.lan = tk.BooleanVar(value=config.lan_enabled)
        self.status = tk.StringVar(value=configuration_error or "选择写真根文件夹，点击启动后在浏览器阅读。")
        self.addresses = tk.StringVar(value=config.local_url)
        self.update_status = tk.StringVar(value="正在检查更新…")
        root.title("Gravelle 启动器")
        try:
            display_scale = float(root.tk.call("tk", "scaling")) / (96 / 72)
        except (tk.TclError, ValueError, TypeError):
            display_scale = 1.0
        metrics = launcher_window_metrics(root.winfo_screenwidth(), root.winfo_screenheight(), display_scale)
        self.ui_scale = metrics["scale"]
        root.geometry(f"{metrics['width']}x{metrics['height']}")
        root.minsize(metrics["min_width"], metrics["min_height"])
        root.configure(background="#f7f2f4")
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        self._build()
        root.after(100, self._poll)
        self._worker("update", self._check_update)

    def _build(self):
        tk = self.tk
        ttk = self.ttk
        px = lambda value: max(1, round(value * self.ui_scale))
        background, white = "#f7f2f4", "#fefdfd"
        foreground, muted, accent = "#2c2b2b", "#786e74", "#ea5098"
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=background)
        style.configure("Card.TFrame", background=white)
        style.configure("TLabel", background=background, foreground=foreground, font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background=white)
        style.configure("Field.TLabel", background=white, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Muted.TLabel", foreground=muted, font=("Microsoft YaHei UI", 9))
        style.configure("CardMuted.TLabel", background=white, foreground=muted, font=("Microsoft YaHei UI", 9))
        style.configure("Address.TLabel", background="#f6edf2", foreground=foreground,
                        font=("Microsoft YaHei UI", 10), padding=(px(14), px(12)))
        style.configure("BrandFallback.TLabel", foreground=accent, font=("Georgia", 28))
        self._configure_button_styles(style)
        style.configure("TCheckbutton", background=white, foreground=foreground,
                        font=("Microsoft YaHei UI", 10), padding=(px(4), px(8)))
        style.map("TCheckbutton", background=[("active", white)], foreground=[("disabled", "#b7aab3")])
        style.configure("TEntry", padding=(px(12), px(10)), fieldbackground="#fcf9fb",
                        foreground=foreground, bordercolor="#dbced5", lightcolor="#fcf9fb",
                        darkcolor="#fcf9fb", insertcolor=accent, font=("Microsoft YaHei UI", 10))
        style.map("TEntry", bordercolor=[("focus", accent), ("disabled", "#eee7ec")],
                  fieldbackground=[("disabled", "#f8f4f7")], foreground=[("disabled", "#9d8e99")])
        style.configure("Brand.Vertical.TScrollbar", background="#e6d5df", troughcolor=background,
                        bordercolor=background, arrowcolor=muted, lightcolor="#e6d5df", darkcolor="#e6d5df",
                        arrowsize=px(12), gripcount=0)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        header = self.header = ttk.Frame(self.root, padding=(px(28), px(20), px(28), px(16)))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        try:
            from PIL import Image, ImageTk
            with Image.open(resource_root() / "desktop" / "assets" / "gravelle-wordmark.png") as image:
                logo_width = px(174)
                logo_height = round(image.height * logo_width / image.width)
                self.wordmark_image = ImageTk.PhotoImage(image.resize((logo_width, logo_height), Image.Resampling.LANCZOS))
            ttk.Label(header, image=self.wordmark_image).grid(row=0, column=0, sticky="w")
        except (ImportError, OSError):
            ttk.Label(header, text="Gravelle", style="BrandFallback.TLabel").grid(row=0, column=0, sticky="w")
        version_actions = ttk.Frame(header)
        version_actions.grid(row=0, column=1, sticky="e", padx=(px(18), 0))
        self.version_label = ttk.Label(version_actions, text=f"v{self.version}", style="Muted.TLabel")
        self.version_label.grid(row=0, column=0, sticky="e")
        self.update_button = ttk.Button(version_actions, text="检查更新", style="Page.Compact.TButton", command=self.update_action)
        self.update_button.grid(row=0, column=1, sticky="e", padx=(px(12), 0))
        self.update_label = ttk.Label(header, textvariable=self.update_status, style="Muted.TLabel",
                                      wraplength=px(600), justify="left")
        self.update_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(8), 0))
        header.bind("<Configure>", lambda event: self.update_label.configure(wraplength=max(px(100), event.width - px(56))))

        body = ttk.Frame(self.root, padding=(px(28), 0, px(16), 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.body_canvas = tk.Canvas(body, background=background, highlightthickness=0, borderwidth=0,
                                     height=px(300), yscrollincrement=px(24))
        self.body_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", style="Brand.Vertical.TScrollbar", command=self.body_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(px(6), 0))
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        self.body_content = ttk.Frame(self.body_canvas)
        body_window = self.body_canvas.create_window(0, 0, anchor="nw", window=self.body_content)
        self.body_content.columnconfigure(0, weight=1)
        self.body_content.bind("<Configure>", lambda event: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")))

        def rounded_card(parent, row):
            canvas = tk.Canvas(parent, background=background, highlightthickness=0, borderwidth=0, height=1)
            canvas.grid(row=row, column=0, sticky="ew", pady=(0, px(16)))
            content = ttk.Frame(canvas, style="Card.TFrame")
            inset = px(22)
            window = canvas.create_window(inset, inset, anchor="nw", window=content)
            def redraw(event=None):
                width = max(1, canvas.winfo_width())
                height = max(1, content.winfo_reqheight() + inset * 2 + px(2))
                canvas.configure(height=height)
                canvas.itemconfigure(window, width=max(1, width - inset * 2))
                canvas.delete("surface")
                radius = px(16)
                right, bottom = width - px(2), height - px(3)
                points = [radius, 1, right-radius, 1, right, 1, right, radius,
                          right, bottom-radius, right, bottom, right-radius, bottom,
                          radius, bottom, 1, bottom, 1, bottom-radius, 1, radius, 1, 1]
                canvas.create_polygon(points, smooth=True, splinesteps=24, fill=white,
                                      outline="#e1d6dd", width=1, tags="surface")
                canvas.tag_lower("surface")
            canvas.bind("<Configure>", redraw)
            content.bind("<Configure>", redraw)
            return content

        configuration = rounded_card(self.body_content, 0)
        configuration.columnconfigure(0, weight=1)
        ttk.Label(configuration, text="写真根文件夹", style="Field.TLabel").grid(row=0, column=0, sticky="w", pady=(0, px(10)))
        self.add_folder_button = ttk.Button(configuration, text="＋ 添加文件夹", style="Compact.TButton", command=self.add_folder)
        self.add_folder_button.grid(row=0, column=1, sticky="e", padx=(px(12), 0), pady=(0, px(10)))
        self.folder_list = ttk.Frame(configuration, style="Card.TFrame")
        self.folder_list.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.folder_list.columnconfigure(0, weight=1)
        self._render_folders()
        network = ttk.Frame(configuration, style="Card.TFrame")
        network.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(22), px(18)))
        ttk.Label(network, text="端口", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.port_entry = ttk.Entry(network, textvariable=self.port, width=8, font=("Microsoft YaHei UI", 10))
        self.port_entry.grid(row=0, column=1, padx=(px(12), px(24)))
        self.lan_check = ttk.Checkbutton(network, text="允许局域网访问", variable=self.lan)
        self.lan_check.grid(row=0, column=2, sticky="w")
        ttk.Label(network, text="分享端口", style="Field.TLabel").grid(row=1, column=0, sticky="w", pady=(px(12), 0))
        self.share_port_entry = ttk.Entry(network, textvariable=self.share_port, width=8, font=("Microsoft YaHei UI", 10))
        self.share_port_entry.grid(row=1, column=1, padx=(px(12), px(24)), pady=(px(12), 0))
        ttk.Label(network, text="在网页网络设置中开启独立分享端口", style="CardMuted.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(px(10), 0))
        self.diagnose_button = ttk.Button(configuration, text="检查目录与网络", command=self.diagnose)
        self.diagnose_button.grid(row=3, column=0, columnspan=2, sticky="ew")

        status_card = rounded_card(self.body_content, 1)
        status_card.columnconfigure(1, weight=1)
        ttk.Label(status_card, text="服务与数据", style="Field.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, px(14)))
        self.status_dot = tk.Canvas(status_card, width=px(10), height=px(10), background=white, highlightthickness=0)
        self.status_dot.create_oval(1, 1, px(8), px(8), fill=accent, outline="")
        self.status_dot.grid(row=1, column=0, sticky="nw", padx=(0, px(10)), pady=(px(6), 0))
        self.status_label = ttk.Label(status_card, textvariable=self.status, style="Card.TLabel", wraplength=px(500), justify="left")
        self.status_label.grid(row=1, column=1, sticky="ew")
        self.address_label = ttk.Label(status_card, textvariable=self.addresses, style="Address.TLabel", wraplength=px(500), justify="left")
        self.address_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(16), px(14)))
        self.storage_label = ttk.Label(status_card, text="账户、封面与阅读记录保存在当前用户的资料目录。",
                                       style="CardMuted.TLabel", wraplength=px(500), justify="left")
        self.storage_label.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.reset_password_button = ttk.Button(status_card, text="重置管理员密码", style="Outline.TButton", command=self.reset_password)
        self.reset_password_button.grid(row=4, column=0, columnspan=2, sticky="w", pady=(px(16), 0))
        self.reset_entry_button = ttk.Button(status_card, text="关闭安全入口", style="Outline.TButton", command=self.reset_security_entry)
        self.reset_entry_button.grid(row=5, column=0, columnspan=2, sticky="w", pady=(px(10), 0))

        def resize_body(event):
            self.body_canvas.itemconfigure(body_window, width=event.width)
            wrap = max(px(100), event.width - px(70))
            self.status_label.configure(wraplength=wrap - px(20))
            self.address_label.configure(wraplength=wrap - px(28))
            self.storage_label.configure(wraplength=wrap)
        self.body_canvas.bind("<Configure>", resize_body)
        def scroll_body(event):
            if self.body_canvas.winfo_rootx() <= event.x_root <= self.body_canvas.winfo_rootx() + self.body_canvas.winfo_width():
                if self.body_canvas.winfo_rooty() <= event.y_root <= self.body_canvas.winfo_rooty() + self.body_canvas.winfo_height():
                    self.body_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self.root.bind_all("<MouseWheel>", scroll_body, add="+")

        footer = ttk.Frame(self.root, style="Card.TFrame", padding=(px(28), px(16), px(28), px(18)))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        tk.Frame(footer, background="#e5d9e0", height=1).grid(row=0, column=0, sticky="ew", pady=(0, px(16)))
        actions = ttk.Frame(footer, style="Card.TFrame")
        actions.grid(row=1, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(actions, text="启动阅读器", style="Accent.TButton", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="ew")
        self.browser_button = ttk.Button(actions, text="打开网页", command=self.open_browser, state="disabled")
        self.browser_button.grid(row=0, column=1, padx=(px(10), 0))
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=(px(10), 0))
        self._configure_button_cursors(self.root)
        try:
            from PIL import Image, ImageTk
            with Image.open(resource_root() / "static" / "gravelle-icon.png") as image:
                self.window_icon = ImageTk.PhotoImage(image.resize((64, 64), Image.Resampling.LANCZOS))
            self.root.iconphoto(True, self.window_icon)
        except (ImportError, OSError):
            pass

    def _configure_button_styles(self, style):
        """Keep ttk button behavior, replacing only Clam's square artwork."""
        from PIL import ImageTk
        px = lambda value: max(1, round(value * self.ui_scale))
        card, page, accent = "#fefdfd", "#f7f2f4", "#ea5098"
        style.configure("TButton", padding=(px(16), px(10)), font=("Microsoft YaHei UI", 10),
                        background=card, foreground="#2c2b2b", relief="flat", borderwidth=0)
        style.configure("Compact.TButton", padding=(px(12), px(8)), font=("Microsoft YaHei UI", 9))
        style.configure("Folder.TButton", padding=(px(12), px(10)))
        style.configure("Remove.TButton", padding=(px(12), px(10)), foreground="#a23868")
        style.configure("Accent.TButton", foreground=card, font=("Microsoft YaHei UI", 10, "bold"))
        secondary = (("#f1e4eb", "#ceb6c3"), ("#ead4e0", "#bb8ba4"), ("#e1c5d5", "#b87596"))
        palettes = {
            "TButton": (card, secondary, accent),
            "Compact.TButton": (card, secondary, accent),
            "Page.Compact.TButton": (page, secondary, accent),
            "Folder.TButton": (card, secondary, accent),
            "Outline.TButton": (card, ((card, "#cdbfc7"), secondary[1], secondary[2]), accent),
            "Remove.TButton": (card, (("#fff1f6", "#d9b7c8"), secondary[1], secondary[2]), accent),
            "Accent.TButton": (card, ((accent, accent), ("#d94389", "#d94389"), ("#c73579", "#c73579")), card),
        }
        self.button_images = {}
        self.button_surface_elements = {}
        for name, (surround, colors, focus) in palettes.items():
            # A ttk window paints its background before drawing the element.
            # Match the parent here, so transparent rounded corners blend into
            # the card or page instead of exposing a rectangular colored tile.
            style.configure(name, background=surround)
            style.map(name, background=[], bordercolor=[], lightcolor=[], darkcolor=[],
                      foreground=[("disabled", "#a59aa1")])
            images = []
            for fill, border in (*colors, ("#f0ebee", "#e4dbe0")):
                images.append(ImageTk.PhotoImage(rounded_button_surface(self.ui_scale, fill, border), master=self.root))
            for fill, border in colors:
                images.append(ImageTk.PhotoImage(rounded_button_surface(self.ui_scale, fill, border, focus), master=self.root))
            element = f"Gravelle{id(self)}.{name}.surface"
            style.element_create(element, "image", images[0],
                                 ("disabled", images[3]), ("pressed", "focus", images[6]),
                                 ("active", "focus", images[5]), ("focus", images[4]),
                                 ("pressed", images[2]), ("active", images[1]),
                                 border=px(12), padding=0, sticky="nsew")
            style.layout(name, [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
            self.button_images[name] = images  # Tk images must outlive their style elements.
            self.button_surface_elements[name] = element

    def _worker(self, kind, action):
        def run():
            try:
                self.events.put((kind, True, action()))
            except Exception as error:
                self.events.put((kind, False, str(error)))
        threading.Thread(target=run, name="launcher-" + kind, daemon=True).start()

    def _configuration(self):
        try:
            port = int(self.port.get().strip())
            share_port = int(self.share_port.get().strip())
        except ValueError as error:
            raise LauncherError("端口需要是 1 至 65535 之间的整数。") from error
        return LauncherConfig(port=port, share_port=share_port, lan_enabled=bool(self.lan.get()),
                              library_paths=tuple(value.get() for value in self.library_values)).validate()

    def _render_folders(self):
        px = lambda value: max(1, round(value * self.ui_scale))
        for widget in self.folder_list.winfo_children():
            widget.destroy()
        self.folder_widgets = []
        for index, value in enumerate(self.library_values):
            row = self.ttk.Frame(self.folder_list, style="Card.TFrame")
            row.grid(row=index, column=0, sticky="ew", pady=(0 if index == 0 else px(10), 0))
            row.columnconfigure(0, weight=1)
            entry = self.ttk.Entry(row, textvariable=value, font=("Microsoft YaHei UI", 10))
            entry.grid(row=0, column=0, sticky="ew")
            browse = self.ttk.Button(row, text="浏览", style="Folder.TButton", command=lambda current=index: self.choose_folder(current))
            browse.grid(row=0, column=1, padx=(px(8), 0))
            remove = self.ttk.Button(row, text="移除", style="Remove.TButton", command=lambda current=index: self.remove_folder(current))
            remove.grid(row=0, column=2, padx=(px(8), 0))
            if len(self.library_values) == 1:
                remove.configure(state="disabled")
            self.folder_widgets.extend((entry, browse, remove))
        self._configure_button_cursors(self.folder_list)

    def _configure_button_cursors(self, parent):
        """Make the enabled/disabled affordance agree with native ttk state."""
        for widget in parent.winfo_children():
            if isinstance(widget, self.ttk.Button):
                widget.configure(cursor="arrow" if widget.instate(["disabled"]) else "hand2")
                widget.bind("<Enter>", lambda event: event.widget.configure(
                    cursor="arrow" if event.widget.instate(["disabled"]) else "hand2"))
                # Windows ttk provides Space activation; make Enter agree with
                # it without adding a second mouse or Space handler.
                widget.bind("<Return>", self._invoke_focused_button)
            self._configure_button_cursors(widget)

    @staticmethod
    def _invoke_focused_button(event):
        if not event.widget.instate(["disabled"]):
            event.widget.invoke()
        return "break"

    def choose_folder(self, index=0):
        if self.busy or self.controller.running:
            return
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.root, title="选择写真根文件夹",
                                           initialdir=self.library_values[index].get(), mustexist=True)
        if selected:
            self.library_values[index].set(selected)

    def add_folder(self):
        if self.busy or self.controller.running:
            return
        if len(self.library_values) >= 20:
            self.status.set("写真根文件夹最多可以添加 20 个。")
            return
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.root, title="添加写真根文件夹",
                                           initialdir=self.library_values[-1].get(), mustexist=True)
        if selected:
            existing = set()
            for value in self.library_values:
                try:
                    if value.get().strip():
                        existing.add(os.path.normcase(str(Path(value.get()).expanduser().resolve())))
                except (OSError, ValueError):
                    # An unfinished typed path must not block the folder
                    # picker; full validation still runs before saving.
                    continue
            if os.path.normcase(str(Path(selected).resolve())) in existing:
                self.status.set("这个写真根文件夹已经在列表中。")
                return
            self.library_values.append(self.tk.StringVar(value=selected))
            self._render_folders()

    def remove_folder(self, index):
        if self.busy or self.controller.running or len(self.library_values) <= 1:
            return
        self.library_values.pop(index)
        self._render_folders()

    def _set_busy(self, value):
        self.busy = value
        running = self.controller.running
        for widget in (*self.folder_widgets, self.port_entry, self.share_port_entry, self.lan_check, self.add_folder_button):
            widget.configure(state="disabled" if value or running else "normal")
        if len(self.library_values) == 1:
            self.folder_widgets[-1].configure(state="disabled")
        self.start_button.configure(state="disabled" if value or running else "normal")
        self.stop_button.configure(state="normal" if running and not value else "disabled")
        self.browser_button.configure(state="normal" if running and not value else "disabled")
        self.diagnose_button.configure(state="disabled" if value else "normal")
        self.reset_password_button.configure(state="disabled" if value else "normal")
        self.reset_entry_button.configure(state="disabled" if value else "normal")
        self._configure_button_cursors(self.root)

    def diagnose(self):
        if self.busy:
            return
        try:
            config = self.controller.config if self.controller.running else self._configuration()
        except LauncherError as error:
            self.status.set(str(error))
            return
        self.status.set("正在检查目录、端口与内网地址…")
        self._set_busy(True)
        self._worker("diagnose", lambda: network_diagnostics(config, self.controller.running, self.controller.data_path))

    def start(self):
        if self.busy or self.controller.running:
            return
        try:
            config = self._configuration()
            save_config(self.config_path, config)
        except (LauncherError, OSError) as error:
            self.status.set(str(error))
            return
        self.status.set("正在启动阅读器…")
        self._set_busy(True)
        self._worker("start", lambda: self.controller.start(config))

    def stop(self):
        if self.busy:
            return
        self.status.set("正在停止阅读器…")
        self._set_busy(True)
        self._worker("stop", self.controller.stop)

    def open_browser(self):
        if self.controller.running:
            try:
                webbrowser.open(self.controller.reader_url)
            except (LauncherError, OSError, RuntimeError) as error:
                self.status.set(str(error))

    def reset_security_entry(self):
        if self.busy or self.closing:
            return
        from tkinter import messagebox
        if not messagebox.askokcancel("关闭安全入口", "忘记安全入口时可在本机关闭。此操作会停止当前启动器管理的阅读器，备份配置并关闭安全入口，随后可重新启动。\n\n如果服务由其他程序启动，请先自行停止它。是否继续？", parent=self.root):
            return
        self.status.set("正在停止阅读器并关闭安全入口…")
        self._set_busy(True)
        self._worker("reset-entry", self.controller.reset_security_entry)

    def reset_password(self):
        if self.busy or self.closing:
            return
        from password_reset import PasswordResetError, validate_confirmation
        tk, ttk = self.tk, self.ttk
        px = lambda value: max(1, round(value * self.ui_scale))
        dialog = tk.Toplevel(self.root)
        dialog.title("重置管理员密码")
        dialog.transient(self.root)
        dialog.configure(background="#ffffff")
        width = min(px(480), max(280, self.root.winfo_screenwidth() - px(48)))
        height = min(px(430), max(260, self.root.winfo_screenheight() - px(80)))
        x = max(0, min(self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2,
                       self.root.winfo_screenwidth() - width))
        y = max(0, min(self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2,
                       self.root.winfo_screenheight() - height))
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text="重置管理员密码", style="Field.TLabel", padding=(px(22), px(18))).grid(
            row=0, column=0, columnspan=2, sticky="ew")
        canvas = tk.Canvas(dialog, background="#ffffff", highlightthickness=0, borderwidth=0)
        canvas.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        body = ttk.Frame(canvas, style="Card.TFrame", padding=(px(22), 0, px(22), px(12)))
        body.columnconfigure(0, weight=1)
        body_window = canvas.create_window(0, 0, anchor="nw", window=body)
        explanation = ttk.Label(body, text="重置前会停止此启动器管理的服务，保存后需要重新登录。外部启动的服务请先手动停止。",
                                style="CardMuted.TLabel", justify="left", wraplength=width - px(60))
        explanation.grid(row=0, column=0, sticky="ew", pady=(0, px(18)))
        first, second, error_text = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(body, text="新密码（8 至 256 个字符）", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        first_entry = ttk.Entry(body, textvariable=first, show="●", font=("Microsoft YaHei UI", 10))
        first_entry.grid(row=2, column=0, sticky="ew", pady=(px(8), px(16)))
        ttk.Label(body, text="再次输入新密码", style="Card.TLabel").grid(row=3, column=0, sticky="w")
        second_entry = ttk.Entry(body, textvariable=second, show="●", font=("Microsoft YaHei UI", 10))
        second_entry.grid(row=4, column=0, sticky="ew", pady=(px(8), px(12)))
        validation = ttk.Label(body, textvariable=error_text, style="Card.TLabel", justify="left", wraplength=width - px(60))
        validation.grid(row=5, column=0, sticky="ew")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        def resize(event):
            canvas.itemconfigure(body_window, width=event.width)
            explanation.configure(wraplength=max(px(100), event.width - px(44)))
            validation.configure(wraplength=max(px(100), event.width - px(44)))
        canvas.bind("<Configure>", resize)
        def scroll(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        dialog.bind("<MouseWheel>", scroll)
        footer = ttk.Frame(dialog, style="Card.TFrame", padding=(px(22), px(14)))
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        footer.columnconfigure(0, weight=1)
        def cancel():
            first.set("")
            second.set("")
            dialog.destroy()
        def save():
            password, confirmation = first.get(), second.get()
            try:
                validate_confirmation(password, confirmation)
            except PasswordResetError as error:
                error_text.set(str(error))
                canvas.yview_moveto(1)
                return
            cancel()
            self.status.set("正在停止服务、备份资料并重置管理员密码…")
            self._set_busy(True)
            self._worker("reset-password", lambda: self.controller.reset_admin_password(password, confirmation))
        ttk.Button(footer, text="取消", command=cancel).grid(row=0, column=1, padx=(0, px(10)))
        ttk.Button(footer, text="保存新密码", style="Accent.TButton", command=save).grid(row=0, column=2)
        self._configure_button_cursors(dialog)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Escape>", lambda event: cancel())
        second_entry.bind("<Return>", lambda event: save())
        dialog.grab_set()
        first_entry.focus_set()
        return dialog

    def _check_update(self):
        self.release_config = load_release_config(resource_root())
        return check_update(self.release_config, self.version)

    def update_action(self):
        from tkinter import messagebox
        if self.closing:
            return
        if self.downloaded:
            if self.busy:
                self.update_status.set("请等待当前操作结束后安装更新。")
                return
            if not messagebox.askyesno("安装更新", "安装前将停止当前阅读器并退出启动器，账户和阅读记录会保留。继续安装吗？", parent=self.root):
                return
            if not verify_installer(Path(self.downloaded["path"]), self.downloaded["sha256"]):
                self.update_status.set("安装程序校验失败，请重新下载。")
                self.downloaded = None
                self.update_button.configure(text="下载更新")
                return
            self.closing = True
            self._set_busy(True)
            self.status.set("正在停止服务并启动安装程序…")
            self._worker("install", self.controller.stop)
        elif self.release and self.release.get("available"):
            self.update_button.configure(state="disabled", cursor="arrow")
            self.update_status.set("正在下载并校验安装程序…")
            self._worker("download", lambda: download_update(self.release_config, self.release, self.storage / "updates"))
        else:
            self.update_button.configure(state="disabled", cursor="arrow")
            self.update_status.set("正在检查更新…")
            self._worker("update", self._check_update)

    def request_exit(self):
        from tkinter import messagebox
        if self.closing:
            return
        if self.busy:
            self.status.set("操作进行中，请稍候再关闭窗口。")
            return
        if self.controller.running and not messagebox.askyesno("退出 Gravelle", "退出启动器会停止阅读器。是否退出？", parent=self.root):
            return
        self.closing = True
        self._set_busy(True)
        self._worker("exit", self.controller.stop)

    def _poll(self):
        try:
            while True:
                kind, success, value = self.events.get_nowait()
                if kind == "update":
                    self.release = value if success else None
                    self.update_status.set(value["message"] if success else str(value))
                    self.update_button.configure(text="下载更新" if success and value.get("available") else "检查更新", state="normal", cursor="hand2")
                elif kind == "download":
                    self.downloaded = value if success else None
                    self.update_status.set(f"版本 {value['version']} 已下载并通过 SHA-256 校验。" if success else str(value))
                    self.update_button.configure(text="安装更新" if success else "重试下载", state="normal", cursor="hand2")
                elif kind in ("exit", "install"):
                    if kind == "install" and not success:
                        self.closing = False
                        self._set_busy(False)
                        self.update_status.set("停止阅读器失败，尚未执行安装程序：" + str(value))
                        continue
                    if kind == "install" and success:
                        try:
                            launch_verified_installer(Path(self.downloaded["path"]), self.downloaded["sha256"], self.instance_lock)
                        except InstallerLaunchError as error:
                            if not error.lock_recovered:
                                from tkinter import messagebox
                                messagebox.showerror("安装更新", str(error) + "\n无法重新获取启动器实例锁，当前窗口将退出。请重新打开启动器。", parent=self.root)
                                self.root.destroy()
                                return
                            self.closing = False
                            self._set_busy(False)
                            self.update_status.set(str(error))
                            continue
                        except (OSError, LauncherError) as error:
                            self.closing = False
                            self._set_busy(False)
                            self.update_status.set("无法打开安装程序：" + str(error))
                            continue
                    self.instance_lock.release()
                    self.root.destroy()
                    return
                else:
                    self._set_busy(False)
                    if not success:
                        self.status.set(str(value))
                    elif kind == "start":
                        self.status.set("阅读器已启动。恢复此窗口可打开网页或停止服务。")
                        self.addresses.set(value)
                        self.open_browser()
                        self.root.iconify()
                        self._worker("diagnose", lambda: network_diagnostics(self.controller.config, True, self.controller.data_path))
                    elif kind == "stop":
                        self.status.set("阅读器已停止。")
                    elif kind == "reset-password":
                        from tkinter import messagebox
                        self.status.set("管理员密码已重置。启动阅读器后，请使用新密码重新登录。")
                        messagebox.showinfo("重置管理员密码", "管理员密码已重置，旧登录已退出。请启动阅读器并使用新密码登录。", parent=self.root)
                    elif kind == "reset-entry":
                        from tkinter import messagebox
                        self.status.set("安全入口已关闭。请重新启动阅读器。")
                        if self.controller.config is not None:
                            self.addresses.set(self.controller.config.local_url)
                        notice = "安全入口已关闭，原配置已备份。请重新启动阅读器。"
                        warnings = security_entry_recovery_warnings(value)
                        if warnings:
                            notice += "\n\n" + "\n".join(warnings)
                        messagebox.showinfo("关闭安全入口", notice, parent=self.root)
                    elif kind == "diagnose":
                        self.status.set(value["message"])
                        self.addresses.set("本机：" + value["local_url"] +
                                           ("\n局域网：" + "\n".join(value["lan_urls"]) if value["lan_urls"] else ""))
        except queue.Empty:
            pass
        if not self.busy and not self.closing and self.controller.process is not None and not self.controller.running:
            self.controller.stop()
            self._set_busy(False)
            self.status.set("阅读器已停止，请查看服务日志后重新启动。")
        self.root.after(100, self._poll)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--pdf-worker"]:
        from pdf_reader import worker_main
        return worker_main(argv[1:])
    if argv[:1] == ["--transcode-worker"]:
        from transcoding import worker_main
        return worker_main(argv[1:])
    if argv[:1] == ["--image-search-worker"]:
        from image_search import worker_main
        return worker_main(argv[1:])
    parser = argparse.ArgumentParser(description="Gravelle Windows launcher")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--serve", action="store_true")
    modes.add_argument("--diagnose", action="store_true")
    modes.add_argument("--reset-admin-password", action="store_true")
    modes.add_argument("--reset-security-entry", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--library", action="append", metavar="PATH", help="写真根文件夹，可重复指定")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--share-port", type=int)
    parser.add_argument("--instance", default="")
    modes.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.reset_security_entry:
        if not arguments.data:
            parser.error("--reset-security-entry requires --data")
        _restore_child_stdio()
        return reset_security_entry_cli(arguments.data)
    if arguments.reset_admin_password:
        if not arguments.data:
            parser.error("--reset-admin-password requires --data")
        _restore_child_stdio()
        from password_reset import main as reset_main
        return reset_main(["--data", str(arguments.data)])
    if arguments.serve:
        _restore_child_stdio()
        if not arguments.library or not arguments.data:
            parser.error("--serve requires --library and --data")
        return serve(LauncherConfig(port=arguments.port, share_port=arguments.share_port, lan_enabled=arguments.host == "0.0.0.0",
                                    library_paths=arguments.library), arguments.data, arguments.instance)
    from server import VERSION
    if arguments.version:
        if sys.stdout is not None:
            print(VERSION)
        return 0
    storage = user_directory()
    config_path = arguments.config or storage / "launcher.json"
    if arguments.diagnose:
        _restore_child_stdio()
        try:
            config = load_config(config_path)
            if arguments.library:
                config = LauncherConfig(port=arguments.port, share_port=arguments.share_port, lan_enabled=arguments.host == "0.0.0.0",
                                        library_paths=arguments.library)
            config.validate()
            result = {"version": VERSION, "data_directory": str(arguments.data or storage / "data"),
                      "configuration": str(config_path), **network_diagnostics(config),
                      "update": check_update(load_release_config(resource_root()), VERSION)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["available"] else 1
        except (LauncherError, OSError) as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 1
    enable_high_dpi()
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    instance_lock = SingleInstanceLock(storage / "launcher.lock", APP_MUTEX)
    if not instance_lock.acquire():
        root.withdraw()
        messagebox.showinfo("Gravelle", "Gravelle 启动器已经打开，请从任务栏恢复窗口。", parent=root)
        root.destroy()
        return 0
    try:
        window = LauncherWindow(root, config_path, storage, instance_lock)
        root.mainloop()
    finally:
        if "window" in locals():
            window.controller.stop()
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

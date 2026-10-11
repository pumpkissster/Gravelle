#!/usr/bin/env python3
"""Windows GUI entry point; --serve and --diagnose also work without Tk."""
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

from desktop.core import (LauncherConfig, LauncherError, SingleInstanceLock, check_update,
                          download_update, load_config, load_release_config, network_diagnostics,
                          save_config, user_directory, verify_installer)
from desktop.runtime import ServerController, resource_root, serve

APP_MUTEX = r"Local\GravelleLauncher-6589345A87B047CE942471F892240DF7"


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
        self.library = tk.StringVar(value=config.library_path)
        self.port = tk.StringVar(value=str(config.port))
        self.lan = tk.BooleanVar(value=config.lan_enabled)
        self.status = tk.StringVar(value=configuration_error or "选择写真文件夹，点击启动后在浏览器阅读。")
        self.addresses = tk.StringVar(value=config.local_url)
        self.update_status = tk.StringVar(value="正在检查更新…")
        root.title("Gravelle 启动器")
        root.geometry("640x600")
        root.minsize(540, 560)
        root.configure(background="#fff8fb")
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        self._build()
        root.after(100, self._poll)
        self._worker("update", self._check_update)

    def _build(self):
        ttk = self.ttk
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#fff8fb")
        style.configure("TLabel", background="#fff8fb", foreground="#39313a", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", foreground="#ea5098", font=("Georgia", 30))
        style.configure("Muted.TLabel", foreground="#786b76", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(12, 9), font=("Microsoft YaHei UI", 10))
        style.configure("Accent.TButton", background="#ea5098", foreground="white")
        style.map("Accent.TButton", background=[("active", "#d73c83"), ("disabled", "#e9b5cc")])
        style.configure("TCheckbutton", background="#fff8fb", font=("Microsoft YaHei UI", 10))
        style.configure("TEntry", padding=9, font=("Microsoft YaHei UI", 10))
        frame = ttk.Frame(self.root, padding=28)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Gravelle", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text=f"Windows 启动器 · {self.version}", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 22))
        ttk.Label(frame, text="写真根文件夹").grid(row=2, column=0, sticky="w", pady=(0, 6))
        folder = ttk.Frame(frame)
        folder.grid(row=3, column=0, sticky="ew")
        folder.columnconfigure(0, weight=1)
        self.folder_entry = ttk.Entry(folder, textvariable=self.library)
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(folder, text="选择文件夹", command=self.choose_folder)
        self.browse_button.grid(row=0, column=1, padx=(8, 0))
        network = ttk.Frame(frame)
        network.grid(row=4, column=0, sticky="ew", pady=(18, 16))
        ttk.Label(network, text="端口").grid(row=0, column=0, sticky="w")
        self.port_entry = ttk.Entry(network, textvariable=self.port, width=9)
        self.port_entry.grid(row=0, column=1, padx=(10, 18))
        self.lan_check = ttk.Checkbutton(network, text="允许局域网访问", variable=self.lan)
        self.lan_check.grid(row=0, column=2, sticky="w")
        self.diagnose_button = ttk.Button(frame, text="检查目录与网络", command=self.diagnose)
        self.diagnose_button.grid(row=5, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.status, wraplength=560).grid(row=6, column=0, sticky="ew", pady=(12, 8))
        ttk.Label(frame, textvariable=self.addresses, wraplength=560, style="Muted.TLabel").grid(row=7, column=0, sticky="ew")
        ttk.Label(frame, text="账户、封面与阅读记录保存在当前用户的资料目录。", style="Muted.TLabel", wraplength=560).grid(row=8, column=0, sticky="w", pady=(12, 18))
        actions = ttk.Frame(frame)
        actions.grid(row=9, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(actions, text="启动阅读器", style="Accent.TButton", command=self.start)
        self.start_button.grid(row=0, column=0, sticky="ew")
        self.browser_button = ttk.Button(actions, text="打开网页", command=self.open_browser, state="disabled")
        self.browser_button.grid(row=0, column=1, padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=(8, 0))
        frame.rowconfigure(10, weight=1)
        updates = ttk.Frame(frame)
        updates.grid(row=11, column=0, sticky="ew", pady=(20, 0))
        updates.columnconfigure(0, weight=1)
        ttk.Label(updates, textvariable=self.update_status, style="Muted.TLabel", wraplength=390).grid(row=0, column=0, sticky="w")
        self.update_button = ttk.Button(updates, text="检查更新", command=self.update_action)
        self.update_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        try:
            from PIL import Image, ImageTk
            self.window_icon = ImageTk.PhotoImage(Image.open(resource_root() / "static" / "gravelle-icon.png").resize((64, 64)))
            self.root.iconphoto(True, self.window_icon)
        except (ImportError, OSError):
            pass

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
        except ValueError as error:
            raise LauncherError("端口需要是 1 至 65535 之间的整数。") from error
        return LauncherConfig(self.library.get().strip(), port, bool(self.lan.get())).validate()

    def choose_folder(self):
        from tkinter import filedialog
        selected = filedialog.askdirectory(parent=self.root, title="选择写真根文件夹", initialdir=self.library.get(), mustexist=True)
        if selected:
            self.library.set(selected)

    def _set_busy(self, value):
        self.busy = value
        running = self.controller.running
        for widget in (self.folder_entry, self.port_entry, self.lan_check, self.browse_button):
            widget.configure(state="disabled" if value or running else "normal")
        self.start_button.configure(state="disabled" if value or running else "normal")
        self.stop_button.configure(state="normal" if running and not value else "disabled")
        self.browser_button.configure(state="normal" if running and not value else "disabled")
        self.diagnose_button.configure(state="disabled" if value else "normal")

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
        self._worker("diagnose", lambda: network_diagnostics(config, self.controller.running))

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
            webbrowser.open(self.controller.config.local_url)

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
            self.update_button.configure(state="disabled")
            self.update_status.set("正在下载并校验安装程序…")
            self._worker("download", lambda: download_update(self.release_config, self.release, self.storage / "updates"))
        else:
            self.update_button.configure(state="disabled")
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
                    self.update_button.configure(text="下载更新" if success and value.get("available") else "检查更新", state="normal")
                elif kind == "download":
                    self.downloaded = value if success else None
                    self.update_status.set(f"版本 {value['version']} 已下载并通过 SHA-256 校验。" if success else str(value))
                    self.update_button.configure(text="安装更新" if success else "重试下载", state="normal")
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
                        self._worker("diagnose", lambda: network_diagnostics(self.controller.config, True))
                    elif kind == "stop":
                        self.status.set("阅读器已停止。")
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
    parser = argparse.ArgumentParser(description="Gravelle Windows launcher")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--library")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--instance", default="")
    parser.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.serve:
        _restore_child_stdio()
        if not arguments.library or not arguments.data:
            parser.error("--serve requires --library and --data")
        return serve(LauncherConfig(arguments.library, arguments.port, arguments.host == "0.0.0.0"), arguments.data, arguments.instance)
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
                config = LauncherConfig(arguments.library, arguments.port, arguments.host == "0.0.0.0")
            config.validate()
            result = {"version": VERSION, "data_directory": str(arguments.data or storage / "data"),
                      "configuration": str(config_path), **network_diagnostics(config),
                      "update": check_update(load_release_config(resource_root()), VERSION)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["available"] else 1
        except (LauncherError, OSError) as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 1
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

"""Private stdio protocol between the Windows launcher and its Python companion."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading

from desktop.core import (LauncherConfig, LauncherError, SingleInstanceLock, check_update,
                          download_update, load_config, load_release_config, network_diagnostics,
                          save_config, security_entry_recovery_warnings, user_directory, verify_installer)
from desktop.runtime import ServerController, resource_root

APP_MUTEX = r"Local\GravelleLauncher-6589345A87B047CE942471F892240DF7"


class WindowsLauncherSession:
    def __init__(self, storage, config_path, version, instance_lock):
        self.storage = storage
        self.config_path = config_path
        self.version = version
        self.instance_lock = instance_lock
        self.controller = ServerController(storage / "data", storage / "logs" / "server.log")
        self.release = None
        self.downloaded = None

    def configuration(self, payload):
        return LauncherConfig(library_paths=payload["library_paths"], port=payload["port"],
                              share_port=payload["share_port"], https_port=payload["https_port"],
                              lan_enabled=payload["lan_enabled"]).validate()

    def current_state(self):
        exited = self.controller.process is not None and not self.controller.running
        if exited:
            self.controller.stop()
        return {"running": self.controller.running, "exited": exited}

    def dispatch(self, operation, payload):
        controller = self.controller
        if operation == "initialize":
            try:
                config = load_config(self.config_path)
                error = ""
            except LauncherError as failure:
                config, error = LauncherConfig(), str(failure)
            return {"config": asdict(config), "error": error, "address": config.local_url,
                    "version": self.version, "storage": str(self.storage)}
        if operation == "choose_folder":
            selected = LauncherConfig(library_paths=[payload["selected"]]).library_path
            for path in payload["library_paths"]:
                try:
                    existing = LauncherConfig(library_paths=[path]).library_path
                except LauncherError:
                    continue
                if Path(selected) == Path(existing):
                    return {"duplicate": True}
            return {"duplicate": False}
        if operation == "status":
            return self.current_state()
        if operation == "start":
            config = self.configuration(payload)
            save_config(self.config_path, config)
            return {"address": controller.start(config)}
        if operation in ("stop", "exit"):
            controller.stop()
            return {}
        if operation == "diagnose":
            config = controller.config if controller.running else self.configuration(payload)
            return network_diagnostics(config, controller.running, controller.data_path)
        if operation == "open_reader":
            if not controller.running:
                raise LauncherError("阅读器尚未启动。")
            return {"address": controller.reader_url}
        if operation == "reset_password":
            controller.reset_admin_password(payload["password"], payload["confirmation"])
            return {}
        if operation == "reset_entry":
            result = controller.reset_security_entry()
            return {"warnings": security_entry_recovery_warnings(result),
                    "address": controller.config.local_url if controller.config else ""}
        if operation == "check_update":
            self.release = check_update(load_release_config(resource_root()), self.version)
            return self.release
        if operation == "download_update":
            if not self.release or not self.release.get("available"):
                raise LauncherError("请先检查更新。")
            self.downloaded = download_update(load_release_config(resource_root()), self.release, self.storage / "updates")
            return {"version": self.downloaded["version"]}
        if operation == "install_update":
            from desktop.launcher import InstallerLaunchError, launch_verified_installer
            if not self.downloaded:
                raise LauncherError("请先下载更新。")
            if not verify_installer(Path(self.downloaded["path"]), self.downloaded["sha256"]):
                self.downloaded = None
                return {"retry_download": True}
            controller.stop()
            try:
                launch_verified_installer(Path(self.downloaded["path"]), self.downloaded["sha256"], self.instance_lock)
            except InstallerLaunchError as error:
                if error.lock_recovered:
                    raise
                return {"exit": True, "error": str(error) + "\n无法重新获取启动器实例锁，请重新打开启动器。"}
            return {"exit": True}
        raise LauncherError("不支持的启动器操作。")


def run(config_path, version):
    sys.stdin.reconfigure(encoding="utf-8")
    storage = user_directory()
    lock = SingleInstanceLock(storage / "launcher.lock", APP_MUTEX)
    output_lock = threading.Lock()

    def respond(value):
        # Keep the pipe ASCII even on systems whose Windows code page varies.
        with output_lock:
            print(json.dumps(value, ensure_ascii=True), flush=True)

    if not lock.acquire():
        respond({"ok": False, "error": "Gravelle 启动器已经打开，请从任务栏恢复窗口。"})
        return 1

    session = WindowsLauncherSession(storage, config_path or storage / "launcher.json", version, lock)

    def process(request):
        command_id = request["id"]
        try:
            result = session.dispatch(request["operation"], request.get("payload") or {})
            respond({"id": command_id, "ok": True, "result": result})
            return result.get("exit") or request["operation"] == "exit"
        except (LauncherError, OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
            respond({"id": command_id, "ok": False, "error": str(error)})
            return False

    try:
        respond({"ok": True})
        for line in sys.stdin:
            request = json.loads(line)
            if request["operation"] in ("check_update", "download_update"):
                threading.Thread(target=process, args=(request,), daemon=True).start()
            elif process(request):
                break
    finally:
        try:
            session.controller.stop()
        finally:
            lock.release()
    return 0

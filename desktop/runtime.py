"""A launcher only controls the server subprocess that it created."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from desktop.core import LauncherConfig, LauncherError, check_port


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def serve(config: LauncherConfig, data_path: Path, instance: str = "") -> int:
    """Private child mode: stdin stop/EOF owns shutdown, never an HTTP route."""
    from server import Library, ReaderServer
    config.validate()
    data_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    library = None
    server = None
    try:
        roots = [Path(path) for path in config.library_paths]
        library = Library(roots[0], data_path, "", 300, roots=roots)
        server = ReaderServer((config.host, config.port), library)
        # With PyInstaller --onedir the static directory is in _internal.
        server.static = resource_root() / "static"
        library.start()
        def watch_parent():
            try:
                for line in sys.stdin:
                    if line.strip() == "stop":
                        break
            finally:
                library.stop_event.set()
                server.shutdown()
        threading.Thread(target=watch_parent, name="launcher-control", daemon=True).start()
        print(json.dumps({"ready": True, "instance": instance, "port": server.server_address[1]}), flush=True)
        server.serve_forever(poll_interval=0.2)
        return 0
    except (OSError, ValueError, RuntimeError, LauncherError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if library is not None:
            library.close()
        if server is not None:
            server.server_close()


class ServerController:
    def __init__(self, data_path: Path, log_path: Path, command_prefix: list[str] | None = None):
        self.data_path = data_path
        self.log_path = log_path
        self.command_prefix = command_prefix
        self.process = None
        self.config = None
        self.log_stream = None
        self.lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def command(self, config: LauncherConfig, instance: str) -> list[str]:
        if self.command_prefix is not None:
            prefix = list(self.command_prefix)
        elif getattr(sys, "frozen", False):
            companion = Path(sys.executable).resolve().with_name("GravelleServer.exe")
            if not companion.is_file():
                raise LauncherError("服务程序 GravelleServer.exe 缺失，请重新安装。")
            prefix = [str(companion)]
        else:
            prefix = [sys.executable, str(Path(__file__).resolve().parent / "launcher.py")]
        libraries = [argument for path in config.library_paths for argument in ("--library", path)]
        return [*prefix, "--serve", *libraries, "--data", str(self.data_path),
                "--host", config.host, "--port", str(config.port), "--instance", instance]

    def start(self, config: LauncherConfig, timeout: float = 20) -> str:
        with self.lock:
            if self.running:
                raise LauncherError("阅读器已经启动。")
            if self.process is not None or self.log_stream is not None:
                self.stop()
            config.validate()
            port = check_port(config)
            if not port["available"]:
                raise LauncherError(port["message"])
            self.data_path.mkdir(parents=True, exist_ok=True)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_stream = self.log_path.open("a", encoding="utf-8")
            instance = secrets.token_hex(16)
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                environment = os.environ.copy()
                environment["PYTHONIOENCODING"] = "utf-8"
                self.process = subprocess.Popen(self.command(config, instance), stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=self.log_stream,
                                                encoding="utf-8", errors="replace", text=True,
                                                creationflags=creationflags, cwd=resource_root(), env=environment)
                self.config = config
                messages = queue.Queue()
                child = self.process
                def read_output():
                    for line in child.stdout:
                        try:
                            messages.put(json.loads(line))
                        except ValueError:
                            pass
                threading.Thread(target=read_output, name="server-ready", daemon=True).start()
                deadline = time.monotonic() + timeout
                ready = False
                while time.monotonic() < deadline:
                    if not self.running:
                        raise LauncherError("阅读器启动失败，请查看服务日志。")
                    try:
                        message = messages.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
                    except queue.Empty:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("error"):
                        raise LauncherError("阅读器启动失败：" + str(message["error"]))
                    if message.get("ready") and message.get("instance") == instance:
                        ready = True
                        break
                if not ready:
                    raise LauncherError("启动超时，请查看服务日志后重试。")
                # Binding success comes from our child's private stdout first;
                # an unrelated web server can never satisfy this handshake.
                # Health is strictly local and must never be sent to a system
                # or environment proxy (including corporate proxy setups).
                local_client = build_opener(ProxyHandler({}))
                while time.monotonic() < deadline:
                    if not self.running:
                        break
                    try:
                        with local_client.open(config.local_url + "api/health", timeout=1) as response:
                            health = json.loads(response.read(16384))
                            if health.get("status") == "ok":
                                return config.local_url
                    except (OSError, ValueError, URLError):
                        time.sleep(0.1)
                raise LauncherError("服务健康检查失败，请查看服务日志。")
            except BaseException:
                self.stop()
                raise

    def stop(self, timeout: float = 15) -> None:
        with self.lock:
            process = self.process
            if process is not None:
                if process.poll() is None:
                    try:
                        process.stdin.write("stop\n")
                        process.stdin.flush()
                    except (OSError, ValueError, AttributeError):
                        pass
                    try:
                        process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        # Only our exact Popen handle is terminated. Never look
                        # up PIDs by port/name or stop an existing reader.
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
                self.process = None
            if self.log_stream is not None:
                self.log_stream.close()
                self.log_stream = None

    def reset_admin_password(self, password: str, confirmation: str) -> dict:
        from password_reset import PasswordResetError, reset_admin_password, validate_confirmation
        with self.lock:
            # Validate before stopping a healthy server. stop() only touches the
            # exact Popen handle owned by this controller, never external PIDs.
            try:
                validate_confirmation(password, confirmation)
                self.stop()
                return reset_admin_password(self.data_path, password, confirmation)
            except PasswordResetError as error:
                raise LauncherError(str(error)) from error

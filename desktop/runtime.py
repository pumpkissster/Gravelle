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

from desktop.core import LauncherConfig, LauncherError, check_port, reader_url, security_entry_recovery_warnings


def _startup_trace(stage: str, *, stream=None, **details) -> None:
    """Opt-in diagnostics contain flags only, never protocol lines or paths."""
    if os.environ.get("GRAVELLE_STARTUP_TRACE") != "1":
        return
    record = {"gravelle_startup_trace": stage}
    for key in ("json", "ready", "instance_matches", "has_error", "alive"):
        if type(details.get(key)) is bool:
            record[key] = details[key]
    for key in ("line_chars", "first_codepoint", "returncode"):
        if type(details.get(key)) is int or key in details and details[key] is None:
            record[key] = details[key]
    elapsed = details.get("elapsed_seconds")
    if type(elapsed) in (int, float):
        record["elapsed_seconds"] = elapsed
    error_type = details.get("error_type")
    if isinstance(error_type, str) and error_type.isascii() and error_type.isidentifier():
        record["error_type"] = error_type[:80]
    try:
        print(json.dumps(record, separators=(",", ":")), file=stream or sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        # Debug output must not affect startup, shutdown or success decisions.
        pass


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def serve(config: LauncherConfig, data_path: Path, instance: str = "") -> int:
    """Private child mode: stdin stop/EOF owns shutdown, never an HTTP route."""
    _startup_trace("service.import_server.begin")
    from server import Library, ReaderServer
    _startup_trace("service.import_server.end")
    config.validate()
    data_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    library = None
    server = None
    try:
        roots = [Path(path) for path in config.library_paths]
        _startup_trace("service.library_init.begin")
        library = Library(roots[0], data_path, "", 300, roots=roots)
        _startup_trace("service.library_init.end")
        _startup_trace("service.bind.begin")
        server = ReaderServer((config.host, config.port), library, share_port=config.share_port, runtime="desktop")
        _startup_trace("service.bind.end")
        # With PyInstaller --onedir the static directory is in _internal.
        server.static = resource_root() / "static"
        _startup_trace("service.library_start.begin")
        library.start()
        _startup_trace("service.library_start.end")
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
        _startup_trace("service.ready_written")
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

    @property
    def reader_url(self) -> str:
        if self.config is None:
            raise LauncherError("阅读器尚未启动。")
        return reader_url(self.config, self.data_path)

    def command(self, config: LauncherConfig, instance: str) -> list[str]:
        if self.command_prefix is not None:
            prefix = list(self.command_prefix)
        elif getattr(sys, "frozen", False):
            if sys.platform == "darwin":
                # macOS retains inherited pipe descriptors in a windowed bundle.
                # Dispatch --serve before loading AppKit or the GUI instance lock.
                prefix = [sys.executable]
            else:
                companion = Path(sys.executable).resolve().with_name("GravelleServer.exe")
                if not companion.is_file():
                    raise LauncherError("服务程序 GravelleServer.exe 缺失，请重新安装。")
                prefix = [str(companion)]
        else:
            entry = "macos_entry.py" if sys.platform == "darwin" else "launcher.py"
            prefix = [sys.executable, str(Path(__file__).resolve().parent / entry)]
        libraries = [argument for path in config.library_paths for argument in ("--library", path)]
        return [*prefix, "--serve", *libraries, "--data", str(self.data_path),
                "--host", config.host, "--port", str(config.port),
                "--share-port", str(config.share_port), "--instance", instance]

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
                _startup_trace("parent.spawn.begin", stream=self.log_stream)
                self.process = subprocess.Popen(self.command(config, instance), stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=self.log_stream,
                                                encoding="utf-8", errors="replace", text=True,
                                                creationflags=creationflags, cwd=resource_root(), env=environment)
                _startup_trace("parent.spawn.end", stream=self.log_stream)
                self.config = config
                messages = queue.Queue()
                child = self.process
                trace_stream = self.log_stream
                def read_output():
                    try:
                        for line in child.stdout:
                            try:
                                message = json.loads(line)
                                mapping = isinstance(message, dict)
                                _startup_trace("parent.stdout.line", stream=trace_stream, json=True,
                                               line_chars=len(line), ready=bool(message.get("ready")) if mapping else False,
                                               instance_matches=message.get("instance") == instance if mapping else False,
                                               has_error=bool(message.get("error")) if mapping else False)
                                messages.put(message)
                            except ValueError:
                                _startup_trace("parent.stdout.line", stream=trace_stream, json=False,
                                               line_chars=len(line), first_codepoint=ord(line[0]) if line else None)
                        _startup_trace("parent.stdout.eof", stream=trace_stream)
                    except BaseException as error:
                        _startup_trace("parent.stdout.error", stream=trace_stream,
                                       error_type=type(error).__name__)
                        raise
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
                        _startup_trace("parent.ready_accepted", stream=self.log_stream)
                        ready = True
                        break
                if not ready:
                    _startup_trace("parent.ready_timeout", stream=self.log_stream)
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
                                _startup_trace("parent.health_ok", stream=self.log_stream)
                                return self.reader_url
                    except (OSError, ValueError, URLError):
                        time.sleep(0.1)
                raise LauncherError("服务健康检查失败，请查看服务日志。")
            except BaseException:
                self.stop()
                raise

    def stop(self, timeout: float = 15) -> None:
        with self.lock:
            process = self.process
            trace_started = time.monotonic()
            _startup_trace("parent.stop.enter", stream=self.log_stream,
                           returncode=getattr(process, "returncode", None))
            try:
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
            finally:
                _startup_trace("parent.stop.exit", stream=self.log_stream,
                               returncode=getattr(process, "returncode", None),
                               elapsed_seconds=round(time.monotonic() - trace_started, 3))
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

    def reset_security_entry(self) -> dict:
        """Offline recovery stops only this controller's child before taking the data lock."""
        from network_settings import reset_security_entry
        with self.lock:
            self.stop()
            try:
                return reset_security_entry(self.data_path)
            except (OSError, ValueError, RuntimeError) as error:
                raise LauncherError(str(error)) from error


def reset_security_entry_cli(data_path: Path) -> int:
    """Explicit local recovery; no HTTP route and no process lookup by port/name."""
    from network_settings import reset_security_entry
    try:
        result = reset_security_entry(data_path)
        payload = {"ok": True, "message": "安全入口已关闭，请重新启动阅读器。 / Security entry disabled. Restart the reader."}
        warnings = security_entry_recovery_warnings(result)
        if warnings:
            payload["warnings"] = warnings
        # This is a JSON protocol also consumed through frozen Windows pipes.
        # Escaping localized text prevents an English ANSI console from turning
        # an already completed recovery into an apparent encoding failure.
        print(json.dumps(payload, ensure_ascii=True))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 1

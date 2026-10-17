#!/usr/bin/env python3
"""macOS app entry; private server/image modes never import AppKit."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def restore_private_stdio() -> None:
    """A windowed frozen bundle still inherits the parent's private pipe FDs."""
    for name, descriptor, mode in (("stdin", 0, "rb"), ("stdout", 1, "wb"), ("stderr", 2, "wb")):
        if getattr(sys, name, None) is None:
            try:
                stream = os.fdopen(os.dup(descriptor), mode, buffering=0)
            except OSError as error:
                raise RuntimeError("The service did not receive its private control pipes.") from error
            setattr(sys, name, io.TextIOWrapper(stream, encoding="utf-8", errors="replace", line_buffering=True))


def startup_trace(stage: str) -> None:
    if os.environ.get("GRAVELLE_STARTUP_TRACE") == "1":
        try:
            print(json.dumps({"gravelle_startup_trace": stage}), file=sys.stderr, flush=True)
        except (OSError, ValueError, AttributeError):
            pass


def serve_private(options) -> int:
    """Trace startup only on request; no paths or private handshake values."""
    restore_private_stdio()
    startup_trace("entry.stdio_ready")
    timer = None
    if os.environ.get("GRAVELLE_STARTUP_TRACE") == "1":
        try:
            import faulthandler
            faulthandler.dump_traceback_later(15, repeat=True, file=sys.stderr)
            timer = faulthandler
        except (ImportError, OSError, RuntimeError, ValueError):
            startup_trace("entry.traceback_unavailable")
    try:
        startup_trace("entry.import_core.begin")
        from desktop.core import LauncherConfig
        startup_trace("entry.import_core.end")
        startup_trace("entry.import_runtime.begin")
        from desktop.runtime import serve
        startup_trace("entry.import_runtime.end")
        startup_trace("entry.serve.begin")
        return serve(LauncherConfig(port=options.port, lan_enabled=options.host == "0.0.0.0",
                                    library_paths=options.library), options.data, options.instance)
    finally:
        if timer is not None:
            try:
                timer.cancel_dump_traceback_later()
            except (OSError, RuntimeError, ValueError):
                pass
        startup_trace("entry.serve.end")


def validate_smoke_paths(storage: Path, config: Path, report: Path) -> tuple[Path, Path, Path]:
    """Native GUI QA requires explicit isolated storage; never uses account data."""
    from desktop.core import user_directory
    supplied = (storage, config, report)
    if any(not value.is_absolute() for value in supplied):
        raise ValueError("GUI smoke paths must be absolute")
    storage, config, report = (value.resolve() for value in supplied)
    real = user_directory().resolve()
    if (storage == real or storage.is_relative_to(real) or real.is_relative_to(storage)
            or config.parent != storage or report.parent != storage
            or config == report or config.name != "launcher.json"
            or report.suffix.lower() != ".json"
            or (storage / "data" / "library.sqlite3").exists()
            or (storage / "library.sqlite3").exists()):
        raise ValueError("GUI smoke requires an isolated empty storage directory and contained config/report")
    return storage, config, report


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Dispatch first, including the reusable --serve-index worker protocol.
    if arguments[:1] == ["--transcode-worker"]:
        restore_private_stdio()
        from transcoding import worker_main
        return worker_main(arguments[1:])
    if arguments[:1] == ["--image-search-worker"]:
        restore_private_stdio()
        from image_search import worker_main
        return worker_main(arguments[1:])

    parser = argparse.ArgumentParser(description="Gravelle macOS launcher")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--serve", action="store_true")
    modes.add_argument("--diagnose", action="store_true")
    modes.add_argument("--version", action="store_true")
    modes.add_argument("--reset-admin-password", action="store_true")
    modes.add_argument("--smoke-launcher", action="store_true")
    parser.add_argument("--library", action="append", metavar="PATH")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--instance", default="")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args(arguments)

    if options.serve:
        if not options.library or not options.data:
            parser.error("--serve requires --library and --data")
        return serve_private(options)
    if options.reset_admin_password:
        if not options.data:
            parser.error("--reset-admin-password requires --data")
        restore_private_stdio()
        from password_reset import main as reset_main
        return reset_main(["--data", str(options.data)])

    if options.smoke_launcher:
        if not options.data or not options.config or not options.report:
            parser.error("--smoke-launcher requires explicit --data, --config and --report")
        if options.library or options.host != "127.0.0.1" or options.port != 8080 or options.instance:
            parser.error("GUI smoke does not accept service arguments")
        try:
            storage, config_path, report_path = validate_smoke_paths(options.data, options.config, options.report)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        if sys.platform != "darwin":
            parser.error("Native GUI smoke requires macOS; no AppKit rendering was performed")
        from server import VERSION
        from desktop.macos_launcher import run_gui
        return run_gui(storage, config_path, VERSION, smoke_report=report_path)
    if options.report:
        parser.error("--report requires --smoke-launcher")

    from server import VERSION
    if options.version:
        restore_private_stdio()
        print(VERSION)
        return 0
    from desktop.core import (LauncherConfig, LauncherError, check_update, load_config,
                              load_release_config, network_diagnostics, user_directory)
    from desktop.runtime import resource_root
    storage = user_directory()
    config_path = options.config or storage / "launcher.json"
    if options.diagnose:
        restore_private_stdio()
        try:
            config = (LauncherConfig(port=options.port, lan_enabled=options.host == "0.0.0.0",
                                     library_paths=options.library) if options.library else load_config(config_path))
            result = {"version": VERSION, "data_directory": str(options.data or storage / "data"),
                      "configuration": str(config_path), **network_diagnostics(config),
                      "update": check_update(load_release_config(resource_root()), VERSION)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["available"] else 1
        except (LauncherError, OSError) as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 1
    if options.library or options.data or options.host != "127.0.0.1" or options.port != 8080 or options.instance:
        parser.error("Service arguments require --serve or --diagnose")
    if sys.platform != "darwin":
        parser.error("The native macOS launcher requires macOS")
    from desktop.macos_launcher import run_gui
    return run_gui(storage, config_path, VERSION)


if __name__ == "__main__":
    raise SystemExit(main())

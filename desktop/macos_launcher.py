"""Native AppKit launcher, with portable state logic and lazy Cocoa imports."""
from __future__ import annotations

import os
import json
from pathlib import Path
import queue
import stat
import threading
import time

from desktop.core import (LauncherConfig, LauncherError, SingleInstanceLock, check_update,
                          download_update, load_config, load_release_config, network_diagnostics,
                          save_config, verify_installer)
from desktop.runtime import ServerController, resource_root

REOPEN_NOTIFICATION = "com.gravelle.launcher.show-window"
BUNDLE_IDENTIFIER = "com.gravelle.launcher"

_TEXT = {
    "title": ("Gravelle 启动器", "Gravelle Launcher"),
    "version": ("版本 {version}", "Version {version}"),
    "check_update": ("检查更新", "Check for Updates"),
    "download_update": ("下载更新", "Download Update"),
    "open_update": ("打开安装包", "Open Installer"),
    "checking": ("正在检查更新…", "Checking for updates…"),
    "downloading": ("正在下载并校验安装包…", "Downloading and verifying the installer…"),
    "downloaded": ("版本 {version} 已下载并通过 SHA-256 校验。", "Version {version} downloaded and SHA-256 verified."),
    "install_hint": ("退出 Gravelle 后，将磁盘映像中的应用拖到“应用程序”文件夹。", "Quit Gravelle, then drag the app from the disk image to Applications."),
    "roots": ("写真根文件夹", "Photo Folders"),
    "add": ("添加文件夹", "Add Folder"),
    "browse": ("选择", "Choose"),
    "remove": ("移除", "Remove"),
    "pick": ("选择写真根文件夹", "Choose Photo Folders"),
    "network": ("访问设置", "Connection"),
    "port": ("服务端口", "Port"),
    "lan": ("允许局域网设备访问", "Allow access from the local network"),
    "diagnose": ("检查连接", "Check Connection"),
    "ready": ("选择写真根文件夹，启动后在浏览器阅读。", "Choose your photo folders, then start reading in your browser."),
    "starting": ("正在启动阅读器…", "Starting the reader…"),
    "running": ("阅读器正在运行。可通过菜单栏恢复启动器。", "The reader is running. Reopen this window from the menu bar."),
    "stopping": ("正在停止阅读器…", "Stopping the reader…"),
    "stopped": ("阅读器已停止。", "The reader is stopped."),
    "crashed": ("阅读器已停止，请查看日志后重新启动。", "The reader stopped. Check the log before restarting."),
    "diagnosing": ("正在检查目录、端口与局域网地址…", "Checking folders, port and local network addresses…"),
    "start": ("启动阅读器", "Start Reader"),
    "stop": ("停止", "Stop"),
    "browser": ("打开网页", "Open Browser"),
    "show": ("显示启动器", "Show Launcher"),
    "quit": ("退出 Gravelle", "Quit Gravelle"),
    "logs": ("服务日志", "Service Log"),
    "data": ("资料目录", "Data Folder"),
    "reset": ("重置管理员密码…", "Reset Admin Password…"),
    "new_password": ("新密码（8 至 256 个字符）", "New password (8–256 characters)"),
    "confirm_password": ("再次输入新密码", "Confirm new password"),
    "reset_hint": ("重置会停止此启动器管理的服务，并退出已有登录。", "Resetting stops the reader managed by this launcher and signs out existing sessions."),
    "resetting": ("正在停止服务并重置管理员密码…", "Stopping the reader and resetting the admin password…"),
    "reset_done": ("管理员密码已重置。请启动阅读器并使用新密码登录。", "Admin password reset. Start the reader and sign in with the new password."),
    "save_password": ("保存新密码", "Save Password"),
    "cancel": ("取消", "Cancel"),
    "working": ("操作进行中，请稍候…", "An operation is in progress. Please wait…"),
    "quitting_after_operation": ("正在完成当前操作，随后退出…", "Finishing the current operation before quitting…"),
    "address": ("访问地址", "Reader Address"),
    "local": ("本机", "This Mac"),
    "lan_address": ("局域网", "Local Network"),
    "update_error": ("暂时无法检查更新，不影响启动阅读器。", "Unable to check for updates. You can still start the reader."),
    "latest": ("当前已是最新版本。", "You're up to date."),
    "update_found": ("发现新版本 {version}。", "Version {version} is available."),
}


def preferred_language(languages) -> str:
    """Use macOS's first preferred app language, including zh-Hans / zh-Hant."""
    first = next((value for value in languages if isinstance(value, str) and value), "en")
    return "zh" if first.lower().replace("_", "-").split("-")[0] == "zh" else "en"


def text(key: str, language="zh", **values) -> str:
    return _TEXT[key][0 if language == "zh" else 1].format(**values)


def window_size(visible_width: float, visible_height: float) -> tuple[float, float]:
    """AppKit uses logical points, so Retina scale must not be applied again."""
    return min(720.0, max(1.0, visible_width - 40)), min(740.0, max(1.0, visible_height - 60))


def normalise_roots(values) -> tuple[str, ...]:
    return LauncherConfig(library_paths=tuple(values)).library_paths


def verified_disk_image(downloaded: dict, directory: Path) -> Path:
    """Recheck trusted downloaded bytes before handing a DMG to macOS."""
    try:
        candidate = Path(downloaded["path"])
        info = candidate.lstat()
        root = directory.resolve(strict=True)
        if (not candidate.is_absolute() or candidate.suffix.lower() != ".dmg"
                or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or candidate.resolve(strict=True).parent != root
                or not verify_installer(candidate, downloaded["sha256"])):
            raise ValueError("Invalid disk image")
    except (OSError, TypeError, ValueError, KeyError) as error:
        raise LauncherError("安装包校验失败，请重新下载。") from error
    return candidate.resolve(strict=True)


class LauncherState:
    """No Cocoa imports: service ownership and config safety are testable anywhere."""

    def __init__(self, storage: Path, config_path: Path, version: str, controller=None):
        self.storage, self.config_path, self.version = Path(storage), Path(config_path), version
        self.controller = controller or ServerController(self.storage / "data", self.storage / "logs" / "server.log")
        self.events = queue.Queue()
        self.worker_completed = None
        self.configuration_error = None
        try:
            config = load_config(self.config_path)
        except LauncherError as error:
            config = LauncherConfig()
            self.configuration_error = str(error)
        self.roots = list(config.library_paths)
        self.port, self.lan_enabled = str(config.port), config.lan_enabled
        self.release_config, self.release, self.downloaded = None, None, None

    def configuration(self) -> LauncherConfig:
        try:
            value = str(self.port).strip()
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Invalid port")
            port = int(value)
        except (ValueError, TypeError) as error:
            raise LauncherError("端口需要是 1 至 65535 之间的整数。") from error
        return LauncherConfig(port=port, lan_enabled=self.lan_enabled, library_paths=self.roots).validate()

    def add_roots(self, paths) -> None:
        proposed = normalise_roots([*self.roots, *paths])
        LauncherConfig(library_paths=proposed).validate()
        self.roots = list(proposed)

    def replace_root(self, index: int, path: str) -> None:
        proposed = list(self.roots)
        proposed[index] = path
        checked = normalise_roots(proposed)
        LauncherConfig(library_paths=checked).validate()
        self.roots = list(checked)

    def remove_root(self, index: int) -> None:
        if len(self.roots) > 1:
            del self.roots[index]

    def start_reader(self, config: LauncherConfig) -> str:
        save_config(self.config_path, config)
        return self.controller.start(config)

    def check_release(self) -> dict:
        self.release_config = load_release_config(resource_root())
        return check_update(self.release_config, self.version)

    def download_release(self) -> dict:
        if not self.release_config or not self.release or not self.release.get("available"):
            raise LauncherError("没有可下载的更新。")
        return download_update(self.release_config, self.release, self.storage / "updates")

    def worker(self, kind: str, action) -> None:
        def execute():
            try:
                result = (kind, True, action())
            except Exception as error:
                result = (kind, False, str(error))
            self.events.put(result)
            completed = self.worker_completed
            if completed is not None:
                completed()
        threading.Thread(target=execute, name="mac-launcher-" + kind, daemon=True).start()


def run_gui(storage: Path, config_path: Path, version: str, *, smoke_report: Path | None = None,
            smoke_scenario: str = 'idle') -> int:
    """Create native controls only in the GUI process, on the AppKit main thread."""
    import AppKit as A
    import Foundation as F
    import Quartz  # Also registers CALayer / CGColor bridge metadata.
    import objc

    # Apple's API returns None without a Quartz GUI session. Do this before
    # constructing NSApplication, which can fail hard without WindowServer.
    if Quartz.CGSessionCopyCurrentDictionary() is None:
        if smoke_report is not None:
            smoke_report.parent.mkdir(parents=True, exist_ok=True)
            smoke_report.write_text(json.dumps({"status": "unsupported", "reason": "No Quartz GUI session is available; native UI was not rendered"}), encoding="utf-8")
            return 77
        raise LauncherError("没有可用的 macOS 图形会话。请在桌面登录后打开 Gravelle。")
    state = LauncherState(storage, config_path, version)
    lock = SingleInstanceLock(Path(storage) / "launcher.lock")
    if not lock.acquire():
        if smoke_report is not None:
            smoke_report.write_text(json.dumps({"status": "failed", "reason": "Isolated GUI smoke instance lock was already held"}), encoding="utf-8")
            return 1
        # LaunchServices reuses a normal .app already. This handles explicitly
        # executing the binary again, without opening another network socket.
        F.NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            REOPEN_NOTIFICATION, None, None, True)
        for running in A.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_IDENTIFIER):
            if running.processIdentifier() != os.getpid():
                running.activateWithOptions_(A.NSApplicationActivateIgnoringOtherApps)
        return 0

    language = preferred_language(F.NSLocale.preferredLanguages())
    tr = lambda key, **kwargs: text(key, language, **kwargs)
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    if not A.NSScreen.screens():
        lock.release()
        if smoke_report is not None:
            smoke_report.write_text(json.dumps({"status": "unsupported", "reason": "No macOS WindowServer screen is available; native UI was not rendered"}), encoding="utf-8")
            return 77
        raise LauncherError("没有可用的 macOS 图形会话。请在桌面登录后打开 Gravelle。")

    def colour(value):
        return A.NSColor.colorWithSRGBRed_green_blue_alpha_(
            int(value[1:3], 16) / 255, int(value[3:5], 16) / 255, int(value[5:7], 16) / 255, 1)

    page, card, ink, muted, accent = map(colour, ("#f7f2f4", "#fefdfd", "#2c2b2b", "#786e74", "#ea5098"))

    class GravelleFlippedView(A.NSView):
        def isFlipped(self):
            return True

    class GravelleMacDelegate(F.NSObject):
        @objc.python_method
        def build(self):
            self.busy, self.updating, self.quitting = False, False, False
            self.exit_started = False
            self.folder_controls = []
            screen = A.NSScreen.mainScreen().visibleFrame().size
            width, height = window_size(screen.width, screen.height)
            style = (A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
                     | A.NSWindowStyleMaskMiniaturizable | A.NSWindowStyleMaskResizable)
            self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                A.NSMakeRect(0, 0, width, height), style, A.NSBackingStoreBuffered, False)
            self.window.setTitle_(tr("title"))
            self.window.setMinSize_(A.NSMakeSize(min(600, width), min(470, height)))
            self.window.setReleasedWhenClosed_(False)
            self.window.setDelegate_(self)
            self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
            self.window.setBackgroundColor_(page)
            self.root = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, width, height))
            self.root.setAutoresizingMask_(A.NSViewWidthSizable | A.NSViewHeightSizable)
            self.window.setContentView_(self.root)
            self.brand = A.NSImageView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 216, 58))
            logo = A.NSImage.alloc().initWithContentsOfFile_(str(resource_root() / "desktop" / "assets" / "gravelle-wordmark.png"))
            if logo is not None:
                self.brand.setImage_(logo)
            self.brand.setImageScaling_(A.NSImageScaleProportionallyUpOrDown)
            self.brand.setAccessibilityLabel_("Gravelle")
            self.root.addSubview_(self.brand)
            self.version = self.label(tr("version", version=version), self.root, size=13, color=muted)
            self.update_button = self.button(tr("check_update"), "update:", self.root)
            self.update_status = self.label(tr("checking"), self.root, size=12, color=muted, wrap=True)
            self.scroll = A.NSScrollView.alloc().initWithFrame_(A.NSMakeRect(0, 100, width, height - 260))
            self.scroll.setHasVerticalScroller_(True)
            self.scroll.setAutohidesScrollers_(True)
            self.scroll.setDrawsBackground_(False)
            self.document = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, width - 56, 400))
            self.scroll.setDocumentView_(self.document)
            self.root.addSubview_(self.scroll)
            self.roots_card = self.panel(self.document)
            self.roots_heading = self.label(tr("roots"), self.roots_card, bold=True)
            self.add_button = self.button(tr("add"), "addFolder:", self.roots_card)
            self.network_card = self.panel(self.document)
            self.network_heading = self.label(tr("network"), self.network_card, bold=True)
            self.port_label = self.label(tr("port"), self.network_card)
            self.port = A.NSTextField.alloc().initWithFrame_(A.NSMakeRect(0, 0, 125, 34))
            self.port.setStringValue_(state.port)
            self.port.setFont_(A.NSFont.systemFontOfSize_(14))
            self.port.setBezelStyle_(A.NSTextFieldRoundedBezel)
            self.port.cell().setUsesSingleLineMode_(True)
            # A rounded AppKit cell has its own bezel/editor metrics. Stretching
            # it to the row height leaves a second-looking outline on focus.
            self.port.sizeToFit()
            self.port_height = self.port.frame().size.height
            self.port.setAccessibilityLabel_(tr("port"))
            self.network_card.addSubview_(self.port)
            self.lan = A.NSButton.checkboxWithTitle_target_action_(tr("lan"), self, "changeLAN:")
            self.lan.setFont_(A.NSFont.systemFontOfSize_(13))
            self.lan.setState_(A.NSControlStateValueOn if state.lan_enabled else A.NSControlStateValueOff)
            self.network_card.addSubview_(self.lan)
            self.diagnose_button = self.button(tr("diagnose"), "diagnose:", self.network_card)
            self.address_label = self.label(tr("address"), self.network_card, size=12, color=muted)
            self.address = self.label(LauncherConfig(port=int(state.port), library_paths=state.roots).local_url,
                                      self.network_card, size=13, wrap=True)
            self.address.setSelectable_(True)
            self.log_button = self.button(tr("logs"), "showLogs:", self.document)
            self.data_button = self.button(tr("data"), "showData:", self.document)
            self.reset_button = self.button(tr("reset"), "resetPassword:", self.document)
            self.status = self.label(state.configuration_error or tr("ready"), self.root, size=13, color=muted, wrap=True)
            self.start_button = self.button(tr("start"), "startReader:", self.root, primary=True)
            self.stop_button = self.button(tr("stop"), "stopReader:", self.root)
            self.browser_button = self.button(tr("browser"), "openBrowser:", self.root)
            self.build_menus()
            self.render_folders()
            self.set_busy(False)
            self.window.center()
            self.window.makeKeyAndOrderFront_(None)
            app.activateIgnoringOtherApps_(True)
            # NSTerminateLater runs AppKit's modal loop. The normal polling
            # timer does not run there, so every completion explicitly wakes
            # the main thread in both the normal and termination modes.
            state.worker_completed = self.wake_for_worker
            self.timer = F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.15, self, "poll:", None, True)
            F.NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_suspensionBehavior_(
                self, "restore:", REOPEN_NOTIFICATION, None, F.NSNotificationSuspensionBehaviorDeliverImmediately)
            if smoke_report is None:
                state.worker("update", state.check_release)
                self.updating = True
                self.update_button.setEnabled_(False)
            else:
                self.update_status.setStringValue_("Native GUI smoke; no update network request.")
                self.smoke_phase = 'views'
                self.smoke_result = None
                self.smoke_start_done = False
                self.smoke_start_error = None
                self.smoke_deadline = time.monotonic() + 55
                self.smoke_timer = F.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.25, self, "finishSmoke:", None, True)

        @objc.python_method
        def label(self, value, parent, size=14, bold=False, color=None, wrap=False):
            field = A.NSTextField.labelWithString_(value)
            field.setFont_(A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size))
            field.setTextColor_(color or ink)
            if wrap:
                field.setLineBreakMode_(A.NSLineBreakByWordWrapping)
                field.setMaximumNumberOfLines_(0)
            parent.addSubview_(field)
            return field

        @objc.python_method
        def panel(self, parent):
            panel = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 1, 1))
            panel.setWantsLayer_(True)
            panel.layer().setBackgroundColor_(card.CGColor())
            panel.layer().setCornerRadius_(16)
            panel.layer().setBorderWidth_(1)
            panel.layer().setBorderColor_(colour("#e9dfe5").CGColor())
            parent.addSubview_(panel)
            return panel

        @objc.python_method
        def button(self, title, action, parent, primary=False):
            button = A.NSButton.buttonWithTitle_target_action_(title, self, action)
            button.setBezelStyle_(A.NSBezelStyleRounded)
            button.setFont_(A.NSFont.boldSystemFontOfSize_(14) if primary else A.NSFont.systemFontOfSize_(13))
            button.setBezelColor_(accent if primary else colour("#efdfE8"))
            button.setContentTintColor_(A.NSColor.whiteColor() if primary else ink)
            button.setAccessibilityLabel_(title)
            if primary:
                button.setKeyEquivalent_("\r")
            parent.addSubview_(button)
            return button

        @objc.python_method
        def menu_item(self, menu, title, action, key=""):
            item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            item.setTarget_(self)
            menu.addItem_(item)
            return item

        @objc.python_method
        def build_menus(self):
            main = A.NSMenu.alloc().initWithTitle_("Main")
            top = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Gravelle", None, "")
            main.addItem_(top)
            submenu = A.NSMenu.alloc().initWithTitle_("Gravelle")
            self.menu_item(submenu, tr("show"), "restore:")
            self.menu_item(submenu, tr("browser"), "openBrowser:")
            submenu.addItem_(A.NSMenuItem.separatorItem())
            self.menu_item(submenu, tr("quit"), "quit:", "q")
            top.setSubmenu_(submenu)
            edit = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("编辑" if language == "zh" else "Edit", None, "")
            edit_menu = A.NSMenu.alloc().initWithTitle_("Edit")
            for titles, selector, key in ((('剪切', 'Cut'), "cut:", "x"), (('复制', 'Copy'), "copy:", "c"),
                                          (('粘贴', 'Paste'), "paste:", "v"), (('全选', 'Select All'), "selectAll:", "a")):
                edit_menu.addItem_(A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(titles[0 if language == "zh" else 1], selector, key))
            edit.setSubmenu_(edit_menu)
            main.addItem_(edit)
            app.setMainMenu_(main)
            self.status_item = A.NSStatusBar.systemStatusBar().statusItemWithLength_(A.NSVariableStatusItemLength)
            icon = A.NSImage.alloc().initWithContentsOfFile_(str(resource_root() / "static" / "gravelle-icon.png"))
            if icon is not None:
                icon.setSize_(A.NSMakeSize(19, 19))
                icon.setTemplate_(True)
                self.status_item.button().setImage_(icon)
            else:
                self.status_item.button().setTitle_("Gravelle")
            self.status_item.button().setToolTip_(tr("title"))
            self.status_menu = A.NSMenu.alloc().initWithTitle_("Gravelle")
            self.menu_item(self.status_menu, tr("show"), "restore:")
            self.menu_item(self.status_menu, tr("browser"), "openBrowser:")
            self.stop_menu = self.menu_item(self.status_menu, tr("stop"), "stopReader:")
            self.status_menu.addItem_(A.NSMenuItem.separatorItem())
            self.menu_item(self.status_menu, tr("quit"), "quit:")
            self.status_item.setMenu_(self.status_menu)

        @objc.python_method
        def render_folders(self):
            for item in self.folder_controls:
                item.removeFromSuperview()
            self.folder_controls = []
            for index, value in enumerate(state.roots):
                field = self.label(value, self.roots_card, size=13)
                field.setSelectable_(True)
                field.setLineBreakMode_(A.NSLineBreakByTruncatingMiddle)
                field.setToolTip_(value)
                choose = self.button(tr("browse"), "chooseFolder:", self.roots_card)
                choose.setTag_(index)
                remove = self.button(tr("remove"), "removeFolder:", self.roots_card)
                remove.setTag_(index)
                self.folder_controls.extend((field, choose, remove))
            self.layout_window()
            self.set_busy(self.busy)

        @objc.python_method
        def frame(self, widget, x, y, width, height):
            widget.setFrame_(A.NSMakeRect(x, y, max(1, width), max(1, height)))

        @objc.python_method
        def layout_window(self):
            width, height = self.root.bounds().size
            self.frame(self.brand, 28, 12, 206, 51)
            self.frame(self.version, 252, 27, 112, 22)
            self.frame(self.update_button, width - 202, 19, 174, 40)
            self.frame(self.update_status, 28, 69, width - 56, 34)
            footer = 143
            self.frame(self.scroll, 28, 107, width - 56, height - footer - 107)
            content_width = max(1, self.scroll.contentSize().width)
            roots_height = 76 + len(state.roots) * 46
            document_height = roots_height + 16 + 236 + 66
            self.document.setFrameSize_(A.NSMakeSize(content_width, document_height))
            self.frame(self.roots_card, 0, 0, content_width, roots_height)
            self.frame(self.roots_heading, 20, 24, content_width - 204, 26)
            self.frame(self.add_button, content_width - 170, 17, 150, 40)
            for index in range(len(state.roots)):
                field, choose, remove = self.folder_controls[index * 3:index * 3 + 3]
                y = 64 + index * 46
                self.frame(field, 20, y + 8, content_width - 204, 24)
                self.frame(choose, content_width - 176, y, 76, 38)
                self.frame(remove, content_width - 96, y, 76, 38)
            y = roots_height + 16
            self.frame(self.network_card, 0, y, content_width, 236)
            self.frame(self.network_heading, 20, 20, content_width - 40, 26)
            self.frame(self.port_label, 20, 65, 108, 24)
            self.frame(self.port, 128, 76 - self.port_height / 2, 116, self.port_height)
            self.frame(self.diagnose_button, content_width - 170, 56, 150, 40)
            self.frame(self.lan, 20, 108, content_width - 40, 28)
            self.frame(self.address_label, 20, 151, content_width - 40, 20)
            self.frame(self.address, 20, 178, content_width - 40, 48)
            self.frame(self.log_button, 0, y + 253, 115, 38)
            self.frame(self.data_button, 123, y + 253, 115, 38)
            self.frame(self.reset_button, content_width - 228, y + 253, 228, 38)
            self.frame(self.status, 30, height - 127, width - 60, 49)
            self.frame(self.start_button, 28, height - 67, width - 316, 43)
            self.frame(self.stop_button, width - 280, height - 67, 100, 43)
            self.frame(self.browser_button, width - 172, height - 67, 144, 43)

        @objc.python_method
        def set_busy(self, busy):
            self.busy = busy
            running = state.controller.running
            editing = not busy and not running
            for field, choose, remove in zip(self.folder_controls[::3], self.folder_controls[1::3], self.folder_controls[2::3]):
                choose.setEnabled_(editing)
                remove.setEnabled_(editing and len(state.roots) > 1)
            for control in (self.port, self.lan, self.add_button):
                control.setEnabled_(editing)
            self.start_button.setEnabled_(editing)
            self.stop_button.setEnabled_(running and not busy)
            self.browser_button.setEnabled_(running and not busy)
            self.diagnose_button.setEnabled_(not busy)
            self.reset_button.setEnabled_(not busy)
            self.stop_menu.setEnabled_(running and not busy)

        @objc.python_method
        def read_configuration(self):
            state.port = str(self.port.stringValue())
            state.lan_enabled = bool(self.lan.state())
            return state.configuration()

        @objc.python_method
        def pick_folders(self, multiple):
            panel = A.NSOpenPanel.openPanel()
            panel.setTitle_(tr("pick"))
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(multiple)
            panel.setCanCreateDirectories_(False)
            panel.setResolvesAliases_(True)
            if panel.runModal() != A.NSModalResponseOK:
                return []
            return [str(url.path()) for url in panel.URLs() if url.isFileURL()]

        def addFolder_(self, sender):
            if self.busy or state.controller.running:
                return
            try:
                selected = self.pick_folders(True)
                if selected:
                    state.add_roots(selected)
                    self.render_folders()
            except (LauncherError, OSError) as error:
                self.status.setStringValue_(str(error))

        def chooseFolder_(self, sender):
            if self.busy or state.controller.running:
                return
            try:
                selected = self.pick_folders(False)
                if selected:
                    state.replace_root(sender.tag(), selected[0])
                    self.render_folders()
            except (LauncherError, OSError) as error:
                self.status.setStringValue_(str(error))

        def removeFolder_(self, sender):
            if not self.busy and not state.controller.running:
                state.remove_root(sender.tag())
                self.render_folders()

        def changeLAN_(self, sender):
            # Saved together with port and roots immediately before starting.
            state.lan_enabled = bool(sender.state())

        def startReader_(self, sender):
            if self.busy or state.controller.running:
                return
            try:
                config = self.read_configuration()
                self.set_busy(True)
                self.status.setStringValue_(tr("starting"))
                state.worker("start", lambda: state.start_reader(config))
            except (LauncherError, OSError) as error:
                self.status.setStringValue_(str(error))

        def stopReader_(self, sender):
            if self.busy or not state.controller.running:
                return
            self.set_busy(True)
            self.status.setStringValue_(tr("stopping"))
            state.worker("stop", state.controller.stop)

        def diagnose_(self, sender):
            if self.busy:
                return
            try:
                config = state.controller.config if state.controller.running else self.read_configuration()
                running = state.controller.running
                self.set_busy(True)
                self.status.setStringValue_(tr("diagnosing"))
                state.worker("diagnose", lambda: network_diagnostics(config, running))
            except (LauncherError, OSError) as error:
                self.status.setStringValue_(str(error))

        def openBrowser_(self, sender):
            if not self.quitting and state.controller.running:
                A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.URLWithString_(state.controller.config.local_url))

        def update_(self, sender):
            if self.updating or self.quitting:
                return
            if state.downloaded:
                try:
                    image = verified_disk_image(state.downloaded, state.storage / "updates")
                    url = F.NSURL.fileURLWithPath_(str(image))
                    workspace = A.NSWorkspace.sharedWorkspace()
                    workspace.activateFileViewerSelectingURLs_([url])
                    if not workspace.openURL_(url):
                        raise LauncherError("无法打开安装包，请在 Finder 中手动打开。")
                    self.update_status.setStringValue_(tr("install_hint"))
                except LauncherError as error:
                    self.update_status.setStringValue_(str(error))
                    state.downloaded = None
                    self.update_button.setTitle_(tr("download_update"))
                return
            self.updating = True
            self.update_button.setEnabled_(False)
            if state.release and state.release.get("available"):
                self.update_status.setStringValue_(tr("downloading"))
                state.worker("download", state.download_release)
            else:
                self.update_status.setStringValue_(tr("checking"))
                state.worker("update", state.check_release)

        def showLogs_(self, sender):
            directory = state.storage / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.fileURLWithPath_(str(directory)))

        def showData_(self, sender):
            directory = state.storage / "data"
            directory.mkdir(parents=True, exist_ok=True)
            A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.fileURLWithPath_(str(directory)))

        def resetPassword_(self, sender):
            if self.busy or self.quitting:
                return
            from password_reset import PasswordResetError, validate_confirmation
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_(tr("reset"))
            alert.setInformativeText_(tr("reset_hint"))
            alert.addButtonWithTitle_(tr("save_password"))
            alert.addButtonWithTitle_(tr("cancel"))
            fields = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 370, 148))
            first_label = self.label(tr("new_password"), fields, size=13)
            self.frame(first_label, 0, 0, 370, 24)
            first = A.NSSecureTextField.alloc().initWithFrame_(A.NSMakeRect(0, 27, 370, 32))
            first.setAccessibilityLabel_(tr("new_password"))
            fields.addSubview_(first)
            second_label = self.label(tr("confirm_password"), fields, size=13)
            self.frame(second_label, 0, 76, 370, 24)
            second = A.NSSecureTextField.alloc().initWithFrame_(A.NSMakeRect(0, 103, 370, 32))
            second.setAccessibilityLabel_(tr("confirm_password"))
            fields.addSubview_(second)
            alert.setAccessoryView_(fields)
            alert.window().setInitialFirstResponder_(first)
            while alert.runModal() == A.NSAlertFirstButtonReturn:
                password, confirmation = str(first.stringValue()), str(second.stringValue())
                try:
                    validate_confirmation(password, confirmation)
                except PasswordResetError as error:
                    alert.setInformativeText_(str(error))
                    continue
                first.setStringValue_("")
                second.setStringValue_("")
                self.set_busy(True)
                self.status.setStringValue_(tr("resetting"))
                state.worker("reset", lambda: state.controller.reset_admin_password(password, confirmation))
                return
            first.setStringValue_("")
            second.setStringValue_("")

        def restore_(self, notification):
            self.window.deminiaturize_(None)
            self.window.makeKeyAndOrderFront_(None)
            app.activateIgnoringOtherApps_(True)

        def quit_(self, sender):
            app.terminate_(None)

        @objc.python_method
        def wake_for_worker(self):
            self.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
                "poll:", None, False, [A.NSModalPanelRunLoopMode, F.NSRunLoopCommonModes])

        @objc.python_method
        def begin_exit(self):
            if self.exit_started:
                return
            self.exit_started = True
            self.status.setStringValue_(tr("stopping"))
            state.worker("exit", state.controller.stop)

        def poll_(self, timer):
            while True:
                try:
                    kind, success, value = state.events.get_nowait()
                except queue.Empty:
                    break
                if kind in ("update", "download"):
                    self.updating = False
                    if self.quitting:
                        continue
                    self.update_button.setEnabled_(not self.quitting)
                    if kind == "update":
                        state.release = value if success else None
                        label = tr("update_error")
                        if success:
                            label = (tr("update_found", version=value["version"]) if value.get("available") else
                                     value.get("message", label) if value.get("platform_available") is False else
                                     tr("latest") if value.get("configured") and not value.get("error") and value.get("version") else
                                     value.get("message", label))
                        self.update_status.setStringValue_(label)
                        self.update_button.setTitle_(tr("download_update") if success and value.get("available") else tr("check_update"))
                    else:
                        state.downloaded = value if success else None
                        self.update_status.setStringValue_(tr("downloaded", version=value["version"]) if success else str(value))
                        self.update_button.setTitle_(tr("open_update") if success else tr("download_update"))
                    continue
                if kind == "exit":
                    if success:
                        app.replyToApplicationShouldTerminate_(True)
                    else:
                        self.quitting = False
                        self.exit_started = False
                        self.restore_(None)
                        self.status.setStringValue_(str(value))
                        self.set_busy(False)
                        self.update_button.setEnabled_(not self.updating)
                        app.replyToApplicationShouldTerminate_(False)
                    continue
                if self.quitting:
                    # A start/reset worker may not have acquired its service
                    # lock when Quit arrives. Wait for its completion before
                    # stopping, so it cannot start an orphan after shutdown.
                    if kind in ("start", "stop", "reset", "diagnose", "smoke-start"):
                        self.begin_exit()
                    continue
                if kind == "background-diagnose":
                    # Automatic checks must not unlock controls belonging to
                    # a later foreground start/stop/reset operation.
                    if success and not self.busy and state.controller.running:
                        addresses = [tr("local") + ": " + value["local_url"]]
                        addresses.extend(tr("lan_address") + ": " + url for url in value["lan_urls"])
                        self.address.setStringValue_("\n".join(addresses))
                    continue
                self.set_busy(False)
                if not success:
                    self.status.setStringValue_(str(value))
                elif kind == "start":
                    self.status.setStringValue_(tr("running"))
                    self.address.setStringValue_(value)
                    self.openBrowser_(None)
                    self.window.orderOut_(None)
                    config = state.controller.config
                    state.worker("background-diagnose", lambda: network_diagnostics(config, True))
                elif kind == "stop":
                    self.status.setStringValue_(tr("stopped"))
                elif kind == "reset":
                    self.status.setStringValue_(tr("reset_done"))
                elif kind == "diagnose":
                    self.status.setStringValue_(value["message"])
                    addresses = [tr("local") + ": " + value["local_url"]]
                    addresses.extend(tr("lan_address") + ": " + url for url in value["lan_urls"])
                    self.address.setStringValue_("\n".join(addresses))
            if not self.busy and not self.quitting and state.controller.process is not None and not state.controller.running:
                state.controller.stop()
                self.set_busy(False)
                self.status.setStringValue_(tr("crashed"))

        def windowDidResize_(self, notification):
            if hasattr(self, "browser_button"):
                self.layout_window()

        def finishSmoke_(self, timer):
            if smoke_report is None or self.smoke_phase == 'terminating':
                return
            if self.smoke_phase == 'views':
                # Exercise actual native controls before using the same
                # terminate event as Dock Quit. No mock or app.stop() pass.
                self.window.setContentSize_(A.NSMakeSize(600, 480))
                self.layout_window()
                port_metrics = self.capture_port_smoke()
                body_height = self.root.bounds().size.height
                footer = self.start_button.frame()
                checks = {"window_visible": bool(self.window.isVisible()),
                          "wordmark_loaded": self.brand.image() is not None,
                          "status_menu_present": self.status_item.menu() is not None,
                          "native_buttons": isinstance(self.start_button, A.NSButton),
                          "port_field": isinstance(self.port, A.NSTextField),
                          "port_uses_native_height": abs(self.port.frame().size.height - self.port.intrinsicContentSize().height) <= 1,
                          "port_single_line": bool(self.port.cell().usesSingleLineMode()),
                          "status_icon_is_template": bool(self.status_item.button().image() is not None
                                                           and self.status_item.button().image().isTemplate()),
                          "small_window_footer_visible": footer.origin.y >= 0 and footer.origin.y + footer.size.height <= body_height,
                          "body_scrolls_independently": self.scroll.documentView() == self.document,
                          "labels_at_least_12pt": min(self.version.font().pointSize(), self.status.font().pointSize(),
                                                      self.update_status.font().pointSize()) >= 12,
                          "service_initially_stopped": not state.controller.running}
                self.smoke_result = {"status": "running", "checks": checks,
                                     "platform": "macos", "native_event_loop": True,
                                     "scenario": smoke_scenario,
                                     "port_metrics": port_metrics,
                                     "termination_requested": False,
                                     "application_will_terminate": False,
                                     "service_pid": None, "service_port": None,
                                     "isolated_storage": str(state.storage)}
                if smoke_scenario != 'idle':
                    import socket
                    media = state.storage / 'media'
                    media.mkdir()
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                        probe.bind(('127.0.0.1', 0))
                        port = probe.getsockname()[1]
                    config = LauncherConfig(port=port, lan_enabled=False, library_paths=[str(media)])
                    self.smoke_result['service_port'] = port
                    self.smoke_phase = 'starting'
                    self.set_busy(True)
                    def start_isolated_service():
                        try:
                            return state.start_reader(config)
                        except Exception as error:
                            self.smoke_start_error = str(error)
                            raise
                        finally:
                            self.smoke_start_done = True
                    state.worker('smoke-start', start_isolated_service)
                    return
            if self.smoke_phase == 'starting':
                process = state.controller.process
                if process is not None:
                    self.smoke_result['service_pid'] = process.pid
                smoke_report.write_text(json.dumps(self.smoke_result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
                if not self.smoke_start_done and time.monotonic() < self.smoke_deadline:
                    return
                self.smoke_result['checks']['service_running_before_quit'] = bool(
                    self.smoke_start_done and self.smoke_start_error is None and state.controller.running)
                if self.smoke_start_error:
                    self.smoke_result['error'] = self.smoke_start_error
                # The service handshake is complete before this flag; no
                # browser is opened and no foreground operation remains.
                if self.smoke_start_done:
                    self.set_busy(False)
                if smoke_scenario == 'hidden':
                    self.window.orderOut_(None)
                    self.smoke_result['checks']['window_hidden_before_quit'] = not self.window.isVisible()
                else:
                    self.smoke_result['checks']['window_visible_before_quit'] = bool(self.window.isVisible())
            self.smoke_phase = 'terminating'
            self.smoke_timer.invalidate()
            self.smoke_result['termination_requested'] = True
            smoke_report.write_text(json.dumps(self.smoke_result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
            app.terminate_(None)

        @objc.python_method
        def capture_port_smoke(self):
            """Capture this isolated input in both states using native AppKit drawing."""
            for name, focus in (("port-focused.png", True), ("port-unfocused.png", False)):
                self.window.makeFirstResponder_(self.port if focus else None)
                if focus:
                    self.port.selectText_(None)
                    editor = self.port.currentEditor()
                    if editor is None or editor.string() != self.port.stringValue():
                        raise LauncherError("The native port editor did not show its current value.")
                self.window.displayIfNeeded()
                rect = A.NSInsetRect(self.port.convertRect_toView_(self.port.bounds(), self.root), -12, -12)
                bitmap = self.root.bitmapImageRepForCachingDisplayInRect_(rect)
                if bitmap is None:
                    raise LauncherError("The native port field could not be captured.")
                self.root.cacheDisplayInRect_toBitmapImageRep_(rect, bitmap)
                png = bitmap.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})
                if png is None or not png.writeToFile_atomically_(str(smoke_report.parent / name), True):
                    raise LauncherError("The native port field capture could not be saved.")
            return {"height": self.port.frame().size.height,
                    "intrinsic_height": self.port.intrinsicContentSize().height,
                    "font_size": self.port.font().pointSize(),
                    "screenshots": ["port-focused.png", "port-unfocused.png"]}

        def windowShouldClose_(self, window):
            window.orderOut_(None)
            return False

        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, visible):
            self.restore_(None)
            return False

        def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
            return False

        def applicationShouldTerminate_(self, sender):
            if self.quitting:
                return A.NSTerminateLater
            waiting = self.busy
            self.quitting = True
            self.set_busy(True)
            self.update_button.setEnabled_(False)
            self.restore_(None)
            if waiting:
                self.status.setStringValue_(tr("quitting_after_operation"))
            else:
                self.begin_exit()
            return A.NSTerminateLater

        def applicationWillTerminate_(self, notification):
            self.timer.invalidate()
            F.NSDistributedNotificationCenter.defaultCenter().removeObserver_(self)
            A.NSStatusBar.systemStatusBar().removeStatusItem_(self.status_item)
            state.controller.stop()
            lock.release()
            if smoke_report is not None and self.smoke_result is not None:
                self.smoke_result['application_will_terminate'] = True
                self.smoke_result['checks']['service_stopped_on_termination'] = (
                    state.controller.process is None and not state.controller.running)
                self.smoke_result['status'] = ('passed' if self.smoke_result['termination_requested']
                                                and all(self.smoke_result['checks'].values()) else 'failed')
                smoke_report.write_text(json.dumps(self.smoke_result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')

    delegate = GravelleMacDelegate.alloc().init()
    app.setDelegate_(delegate)
    try:
        delegate.build()
        app.run()
    finally:
        state.worker_completed = None
        state.controller.stop()
        lock.release()
    if smoke_report is not None:
        try:
            return 0 if json.loads(smoke_report.read_text(encoding="utf-8")).get("status") == "passed" else 1
        except (OSError, ValueError):
            return 1
    return 0

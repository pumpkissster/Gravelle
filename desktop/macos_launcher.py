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
                          save_config, security_entry_recovery_warnings, verify_installer)
from desktop.runtime import ServerController, resource_root

REOPEN_NOTIFICATION = "com.gravelle.launcher.show-window"
BUNDLE_IDENTIFIER = "com.gravelle.launcher"

_TEXT = {
    "title": ("Gravelle 启动器", "Gravelle Launcher"),
    "version": ("版本 {version}", "Version {version}"),
    "reader": ("阅读器", "Reader"),
    "advanced": ("高级设置", "Advanced Settings"),
    "service_status": ("服务状态", "Service Status"),
    "service_stopped": ("未启动", "Stopped"),
    "service_running": ("运行中", "Running"),
    "service_busy": ("处理中", "Working"),
    "service_data": ("服务与数据", "Service & Data"),
    "storage_hint": ("账户、封面与阅读记录保存在当前用户的资料目录。", "Accounts, covers and reading history are stored in your data folder."),
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
    "share_port": ("分享端口", "Share Port"),
    "https_port": ("HTTPS 端口", "HTTPS Port"),
    "share_port_hint": ("在网页网络设置中开启 HTTPS 或独立分享端口", "Enable HTTPS or the separate share port in web Network Settings"),
    "lan": ("允许局域网设备访问", "Allow access from the local network"),
    "diagnose": ("检查目录与网络", "Check Folders & Network"),
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
    "reset_entry": ("关闭安全入口…", "Disable Security Entry…"),
    "reset_entry_hint": ("此操作会停止当前启动器管理的阅读器，备份配置并关闭安全入口。如果服务由其他程序启动，请先自行停止它。", "This stops the reader managed by this launcher, backs up the settings and disables the security entry. Stop any reader started by another app yourself first."),
    "disable_entry": ("关闭安全入口", "Disable Entry"),
    "resetting_entry": ("正在停止阅读器并关闭安全入口…", "Stopping the reader and disabling the security entry…"),
    "reset_entry_done": ("安全入口已关闭。请重新启动阅读器。", "Security entry disabled. Restart the reader."),
    "save_password": ("保存新密码", "Save Password"),
    "cancel": ("取消", "Cancel"),
    "ok": ("确定", "OK"),
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
    return min(860.0, max(1.0, visible_width - 40)), min(660.0, max(1.0, visible_height - 60))


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
        self.port, self.share_port, self.lan_enabled = str(config.port), str(config.share_port), config.lan_enabled
        self.https_port = str(config.https_port)
        self.release_config, self.release, self.downloaded = None, None, None

    def configuration(self) -> LauncherConfig:
        try:
            value = str(self.port).strip()
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Invalid port")
            port = int(value)
            share_value = str(self.share_port).strip()
            if not share_value.isascii() or not share_value.isdecimal():
                raise ValueError("Invalid share port")
            share_port = int(share_value)
            https_value = str(self.https_port).strip()
            if not https_value.isascii() or not https_value.isdecimal():
                raise ValueError("Invalid HTTPS port")
            https_port = int(https_value)
        except (ValueError, TypeError) as error:
            raise LauncherError("端口需要是 1 至 65535 之间的整数。") from error
        return LauncherConfig(port=port, share_port=share_port, https_port=https_port, lan_enabled=self.lan_enabled, library_paths=self.roots).validate()

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

    capture_launcher = smoke_report is not None and smoke_scenario == "idle"
    language = "zh" if capture_launcher else preferred_language(F.NSLocale.preferredLanguages())
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

    page, card, ink, muted, accent = map(colour, ("#faf9fa", "#ffffff", "#29252a", "#6d656d", "#ae3064"))
    line, sidebar, inset, selected, selected_ink = map(
        colour, ("#e9e4e8", "#f8f5f7", "#f7f5f7", "#f7e8ef", "#8c2853"))
    control_border, hover_border, focus_border = map(colour, ("#dbd4da", "#9b9099", "#a32f61"))
    pressed_surface, disabled_ink, accent_hover, accent_pressed, accent_focus = map(
        colour, ("#f2eef1", "#8b828b", "#9c2857", "#86234b", "#671737"))
    control_radius, control_height = 8, 36
    navigation_padding = 11

    def draw_control(bounds, fill, border):
        outer = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, control_radius, control_radius)
        border.setFill()
        outer.fill()
        inner_bounds = A.NSInsetRect(bounds, 1, 1)
        inner = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            inner_bounds, control_radius - 1, control_radius - 1)
        fill.setFill()
        inner.fill()

    class GravelleFlippedView(A.NSView):
        def isFlipped(self):
            return True

    class GravelleLauncherWindow(A.NSWindow):
        def makeFirstResponder_(self, responder):
            accepted = objc.super(GravelleLauncherWindow, self).makeFirstResponder_(responder)
            # While editing, the first responder is AppKit's shared field editor.
            for view in getattr(self, "input_surfaces", ()):
                view.setNeedsDisplay_(True)
            return accepted

    class GravelleActionButton(A.NSButton):
        @objc.python_method
        def set_flat_style(self, primary):
            self.primary = primary
            self.hovered = False
            self.setBordered_(False)
            self.setFocusRingType_(A.NSFocusRingTypeNone)
            options = (A.NSTrackingMouseEnteredAndExited | A.NSTrackingActiveInKeyWindow
                       | A.NSTrackingInVisibleRect)
            area = A.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(self.bounds(), options, self, None)
            self.addTrackingArea_(area)

        def mouseEntered_(self, event):
            self.hovered = True
            self.setNeedsDisplay_(True)

        def mouseExited_(self, event):
            self.hovered = False
            self.setNeedsDisplay_(True)

        def becomeFirstResponder(self):
            accepted = objc.super(GravelleActionButton, self).becomeFirstResponder()
            self.setNeedsDisplay_(True)
            return accepted

        def resignFirstResponder(self):
            accepted = objc.super(GravelleActionButton, self).resignFirstResponder()
            self.setNeedsDisplay_(True)
            return accepted

        def drawRect_(self, rect):
            focused = self.window() is not None and self.window().firstResponder() == self
            pressed = self.cell().isHighlighted()
            hovered = self.hovered and self.window() is not None and self.window().isKeyWindow()
            if not self.isEnabled():
                fill, border, foreground = page, line, disabled_ink
            elif self.primary:
                fill = accent_pressed if pressed else accent_hover if hovered else accent
                border, foreground = (accent_focus if focused else fill), card
            else:
                fill = pressed_surface if pressed else card
                border = focus_border if focused else hover_border if hovered else control_border
                foreground = ink
            bounds = self.bounds()
            draw_control(bounds, fill, border)
            title = F.NSAttributedString.alloc().initWithString_attributes_(
                self.title(), {A.NSFontAttributeName: self.font(), A.NSForegroundColorAttributeName: foreground})
            size = title.size()
            title.drawAtPoint_(A.NSMakePoint((bounds.size.width - size.width) / 2,
                                            (bounds.size.height - size.height) / 2))

    class GravelleNavigationButton(A.NSButton):
        def drawRect_(self, rect):
            # Inset the cell contents without shrinking the selected background or hit target.
            contents = A.NSInsetRect(self.bounds(), navigation_padding, 0)
            self.cell().drawWithFrame_inView_(contents, self)

    class GravelleInputSurface(GravelleFlippedView):
        @objc.python_method
        def configure(self, value, title):
            self.field = A.NSTextField.alloc().initWithFrame_(A.NSMakeRect(0, 0, 1, 1))
            self.field.setStringValue_(value)
            self.field.setFont_(A.NSFont.systemFontOfSize_(14))
            self.field.setTextColor_(ink)
            self.field.setBezeled_(False)
            self.field.setBordered_(False)
            self.field.setDrawsBackground_(False)
            self.field.setFocusRingType_(A.NSFocusRingTypeNone)
            self.field.cell().setUsesSingleLineMode_(True)
            self.field.setAccessibilityLabel_(title)
            self.addSubview_(self.field)

        @objc.python_method
        def layout_field(self):
            width, height = self.bounds().size
            field_height = self.field.intrinsicContentSize().height + 2
            self.field.setFrame_(A.NSMakeRect(10, (height - field_height) // 2, width - 20, field_height))

        @objc.python_method
        def has_focus(self):
            window = self.window()
            if window is None:
                return False
            responder = window.firstResponder()
            editor = self.field.currentEditor()
            return responder == self.field or (editor is not None and responder == editor)

        def drawRect_(self, rect):
            enabled = self.field.isEnabled()
            border = (focus_border if self.has_focus() else control_border) if enabled else line
            draw_control(self.bounds(), page if enabled else inset, border)

        def mouseDown_(self, event):
            if self.field.isEnabled():
                self.window().makeFirstResponder_(self.field)

    class GravelleMacDelegate(F.NSObject):
        @objc.python_method
        def build(self):
            self.busy, self.updating, self.quitting = False, False, False
            self.exit_started = False
            self.folder_controls = []
            self.advanced = False
            self.page_offsets = {False: 0.0, True: 0.0}
            screen = A.NSScreen.mainScreen().visibleFrame().size
            width, height = window_size(screen.width, screen.height)
            style = (A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
                     | A.NSWindowStyleMaskMiniaturizable | A.NSWindowStyleMaskResizable)
            self.window = GravelleLauncherWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                A.NSMakeRect(0, 0, width, height), style, A.NSBackingStoreBuffered, False)
            self.window.setTitle_(tr("title"))
            self.window.setContentMinSize_(A.NSMakeSize(min(610, width), min(420, height)))
            self.window.setReleasedWhenClosed_(False)
            self.window.setDelegate_(self)
            self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
            self.window.setBackgroundColor_(page)
            self.root = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, width, height))
            self.root.setAutoresizingMask_(A.NSViewWidthSizable | A.NSViewHeightSizable)
            self.window.setContentView_(self.root)
            self.sidebar = self.surface(self.root, sidebar)
            self.sidebar_line = self.surface(self.sidebar, line)
            self.brand = A.NSImageView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 140, 38))
            logo = A.NSImage.alloc().initWithContentsOfFile_(str(resource_root() / "desktop" / "assets" / "gravelle-wordmark.png"))
            if logo is not None:
                self.brand.setImage_(logo)
            self.brand.setImageScaling_(A.NSImageScaleProportionallyUpOrDown)
            self.brand.setAccessibilityLabel_("Gravelle")
            self.sidebar.addSubview_(self.brand)
            self.version = self.label(tr("version", version=version), self.sidebar, size=12, color=muted)
            self.home_nav = self.navigation_button(tr("reader"), "showReaderSettings:", "book")
            self.advanced_nav = self.navigation_button(tr("advanced"), "showAdvancedSettings:", "gearshape")
            nav_width = max(button.intrinsicContentSize().width for button in (self.home_nav, self.advanced_nav))
            self.sidebar_width = max(164, int(nav_width + 2 * navigation_padding + 29))
            self.update_button = self.button(tr("check_update"), "update:", self.sidebar)
            self.scroll = A.NSScrollView.alloc().initWithFrame_(A.NSMakeRect(0, 0, width, height))
            self.scroll.setHasVerticalScroller_(True)
            self.scroll.setAutohidesScrollers_(True)
            self.scroll.setDrawsBackground_(False)
            self.document = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, width, 400))
            self.scroll.setDocumentView_(self.document)
            self.root.addSubview_(self.scroll)
            self.home = self.surface(self.document, page)
            self.advanced_page = self.surface(self.document, page)
            self.page_heading = self.label(tr("reader"), self.home, size=24, bold=True)
            self.advanced_heading = self.label(tr("advanced"), self.advanced_page, size=24, bold=True)
            self.service_card = self.panel(self.home)
            self.service_heading = self.label(tr("service_status"), self.service_card, size=16, bold=True)
            self.service_badge = self.surface(self.service_card, selected, radius=11)
            self.service_status = self.label(tr("service_stopped"), self.service_badge, size=12, color=selected_ink)
            self.service_status.setAlignment_(A.NSTextAlignmentCenter)
            self.address_label = self.label(tr("address"), self.service_card, size=12, color=muted)
            self.address_surface = self.surface(self.service_card, inset, radius=control_radius, border=control_border)
            self.address = self.label(
                f"http://127.0.0.1:{state.port}/", self.address_surface, size=13, wrap=True)
            self.address.setSelectable_(True)
            self.roots_card = self.panel(self.home)
            self.roots_heading = self.label(tr("roots"), self.roots_card, bold=True)
            self.add_button = self.button(tr("add"), "addFolder:", self.roots_card)
            self.network_card = self.panel(self.advanced_page)
            self.network_heading = self.label(tr("network"), self.network_card, bold=True)
            self.port_label = self.label(tr("port"), self.network_card)
            self.port_surface = self.port_input(state.port, tr("port"))
            self.port = self.port_surface.field
            self.share_port_label = self.label(tr("share_port"), self.network_card)
            self.share_port_surface = self.port_input(state.share_port, tr("share_port"))
            self.share_port = self.share_port_surface.field
            self.https_port_label = self.label(tr("https_port"), self.network_card)
            self.https_port_surface = self.port_input(state.https_port, tr("https_port"))
            self.https_port = self.https_port_surface.field
            self.window.input_surfaces = (self.port_surface, self.share_port_surface, self.https_port_surface)
            self.share_port_hint = self.label(tr("share_port_hint"), self.network_card, size=12, color=muted, wrap=True)
            self.lan = A.NSButton.checkboxWithTitle_target_action_(tr("lan"), self, "changeLAN:")
            self.lan.setFont_(A.NSFont.systemFontOfSize_(13))
            self.lan.setState_(A.NSControlStateValueOn if state.lan_enabled else A.NSControlStateValueOff)
            self.network_card.addSubview_(self.lan)
            self.diagnose_button = self.button(tr("diagnose"), "diagnose:", self.network_card)
            self.data_card = self.panel(self.advanced_page)
            self.data_heading = self.label(tr("service_data"), self.data_card, size=16, bold=True)
            self.storage_hint = self.label(tr("storage_hint"), self.data_card, size=12, color=muted, wrap=True)
            self.log_button = self.button(tr("logs"), "showLogs:", self.data_card)
            self.data_button = self.button(tr("data"), "showData:", self.data_card)
            self.reset_button = self.button(tr("reset"), "resetPassword:", self.data_card)
            self.reset_entry_button = self.button(tr("reset_entry"), "resetSecurityEntry:", self.data_card)
            self.footer = self.surface(self.root, card)
            self.footer_line = self.surface(self.footer, line)
            self.status = self.label(state.configuration_error or tr("ready"), self.footer, size=13, color=muted, wrap=True)
            self.update_status = self.label(tr("checking"), self.footer, size=12, color=muted, wrap=True)
            self.start_button = self.button(tr("start"), "startReader:", self.footer, primary=True)
            self.stop_button = self.button(tr("stop"), "stopReader:", self.footer)
            self.browser_button = self.button(tr("browser"), "openBrowser:", self.footer)
            self.build_menus()
            self.render_folders()
            self.show_page(False)
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
            field = (A.NSTextField.wrappingLabelWithString_(value) if wrap
                     else A.NSTextField.labelWithString_(value))
            field.setFont_(A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size))
            field.setTextColor_(color or ink)
            if wrap:
                field.setLineBreakMode_(A.NSLineBreakByWordWrapping)
                field.setMaximumNumberOfLines_(0)
            parent.addSubview_(field)
            return field

        @objc.python_method
        def surface(self, parent, background, *, radius=0, border=None):
            panel = GravelleFlippedView.alloc().initWithFrame_(A.NSMakeRect(0, 0, 1, 1))
            panel.setWantsLayer_(True)
            panel.layer().setBackgroundColor_(background.CGColor())
            panel.layer().setCornerRadius_(radius)
            if border is not None:
                panel.layer().setBorderWidth_(1)
                panel.layer().setBorderColor_(border.CGColor())
            parent.addSubview_(panel)
            return panel

        @objc.python_method
        def panel(self, parent):
            return self.surface(parent, card, radius=12, border=line)

        @objc.python_method
        def port_input(self, value, title):
            surface = GravelleInputSurface.alloc().initWithFrame_(A.NSZeroRect)
            surface.configure(value, title)
            self.network_card.addSubview_(surface)
            return surface

        @objc.python_method
        def button(self, title, action, parent, primary=False):
            button = GravelleActionButton.alloc().initWithFrame_(A.NSZeroRect)
            button.setTitle_(title)
            button.setTarget_(self)
            button.setAction_(action)
            button.setButtonType_(A.NSButtonTypeMomentaryPushIn)
            button.setFont_(A.NSFont.systemFontOfSize_weight_(14, A.NSFontWeightSemibold)
                            if primary else A.NSFont.systemFontOfSize_(14))
            button.set_flat_style(primary)
            if primary:
                button.setKeyEquivalent_("\r")
            parent.addSubview_(button)
            return button

        @objc.python_method
        def navigation_button(self, title, action, symbol):
            button = GravelleNavigationButton.alloc().initWithFrame_(A.NSZeroRect)
            button.setTitle_(title)
            button.setTarget_(self)
            button.setAction_(action)
            button.setBordered_(False)
            button.setAlignment_(A.NSTextAlignmentLeft)
            button.setFont_(A.NSFont.boldSystemFontOfSize_(13))
            button.setWantsLayer_(True)
            button.layer().setCornerRadius_(8)
            button.setAccessibilityLabel_(title)
            icon = A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
            if icon is not None:
                button.setImage_(icon)
                button.setImagePosition_(A.NSImageLeft)
            self.sidebar.addSubview_(button)
            return button

        @objc.python_method
        def show_page(self, advanced):
            clip = self.scroll.contentView()
            self.page_offsets[self.advanced] = clip.bounds().origin.y
            self.advanced = advanced
            self.home.setHidden_(advanced)
            self.advanced_page.setHidden_(not advanced)
            for button, active in ((self.home_nav, not advanced), (self.advanced_nav, advanced)):
                button.layer().setBackgroundColor_((selected if active else sidebar).CGColor())
                button.setContentTintColor_(selected_ink if active else muted)
                font = A.NSFont.boldSystemFontOfSize_(13) if active else A.NSFont.systemFontOfSize_(13)
                title = F.NSAttributedString.alloc().initWithString_attributes_(
                    button.accessibilityLabel(), {A.NSFontAttributeName: font,
                                                  A.NSForegroundColorAttributeName: selected_ink if active else ink})
                button.setAttributedTitle_(title)
            self.layout_window()
            max_offset = max(0, self.document.frame().size.height - clip.bounds().size.height)
            clip.scrollToPoint_(A.NSMakePoint(0, min(self.page_offsets[advanced], max_offset)))
            self.scroll.reflectScrolledClipView_(clip)

        def showReaderSettings_(self, sender):
            if self.advanced:
                self.window.makeFirstResponder_(sender)
                self.show_page(False)

        def showAdvancedSettings_(self, sender):
            if not self.advanced:
                self.window.makeFirstResponder_(sender)
                self.show_page(True)

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
            for well, _, choose, remove in self.folder_controls:
                for item in (well, choose, remove):
                    item.removeFromSuperview()
            self.folder_controls = []
            for index, value in enumerate(state.roots):
                well = self.surface(self.roots_card, inset, radius=control_radius, border=control_border)
                field = self.label(value, well, size=13)
                field.setSelectable_(True)
                field.setLineBreakMode_(A.NSLineBreakByTruncatingMiddle)
                field.setToolTip_(value)
                choose = self.button(tr("browse"), "chooseFolder:", self.roots_card)
                choose.setTag_(index)
                remove = self.button(tr("remove"), "removeFolder:", self.roots_card)
                remove.setTag_(index)
                self.folder_controls.append((well, field, choose, remove))
            self.layout_window()
            self.set_busy(self.busy)

        @objc.python_method
        def frame(self, widget, x, y, width, height):
            widget.setFrame_(A.NSMakeRect(x, y, max(1, width), max(1, height)))

        @objc.python_method
        def label_height(self, widget, width):
            bounds = A.NSMakeRect(0, 0, max(1, width), 10000)
            return max(18, int(widget.cell().cellSizeForBounds_(bounds).height + 2))

        @objc.python_method
        def button_width(self, widget, minimum=60):
            return max(minimum, int(widget.intrinsicContentSize().width + 20))

        @objc.python_method
        def layout_window(self):
            width, height = self.root.bounds().size
            rail = min(self.sidebar_width, max(130, int(width * .30)))
            footer_height = 118
            self.frame(self.sidebar, 0, 0, rail, height)
            self.frame(self.sidebar_line, rail - 1, 0, 1, height)
            self.frame(self.brand, 16, 16, rail - 32, 40)
            self.frame(self.version, 17, 60, rail - 32, 18)
            self.frame(self.home_nav, 14, 100, rail - 28, 36)
            self.frame(self.advanced_nav, 14, 142, rail - 28, 36)
            self.frame(self.update_button, 13, height - 49, rail - 26, control_height)
            self.frame(self.scroll, rail, 0, width - rail, height - footer_height)
            content_width = self.scroll.contentSize().width
            card_width = content_width - 48
            self.frame(self.page_heading, 24, 22, card_width, 34)
            self.frame(self.advanced_heading, 24, 22, card_width, 34)
            home_height = self.layout_reader(card_width)
            advanced_height = self.layout_advanced(card_width)
            self.frame(self.home, 0, 0, content_width, home_height)
            self.frame(self.advanced_page, 0, 0, content_width, advanced_height)
            self.document.setFrameSize_(A.NSMakeSize(content_width, advanced_height if self.advanced else home_height))

            footer_width = width - rail
            self.frame(self.footer, rail, height - footer_height, footer_width, footer_height)
            self.frame(self.footer_line, 0, 0, footer_width, 1)
            self.frame(self.status, 24, 10, footer_width - 48, 33)
            self.frame(self.update_status, 24, 45, footer_width - 48, 24)
            x = footer_width - 24
            for button, minimum in ((self.start_button, 124), (self.stop_button, 60), (self.browser_button, 100)):
                size = self.button_width(button, minimum)
                x -= size
                self.frame(button, x, footer_height - 49, size, control_height)
                x -= 8

        @objc.python_method
        def layout_reader(self, width):
            address_height = self.label_height(self.address, width - 56)
            address_box_height = max(control_height, address_height + 12)
            service_height = 78 + address_box_height
            self.frame(self.service_card, 24, 67, width, service_height)
            badge_width = 74
            self.frame(self.service_heading, 16, 16, width - badge_width - 40, 24)
            self.frame(self.service_badge, width - badge_width - 16, 16, badge_width, 23)
            self.frame(self.service_status, 6, 3, badge_width - 12, 18)
            self.frame(self.address_label, 16, 48, width - 32, 18)
            self.frame(self.address_surface, 16, 68, width - 32, address_box_height)
            self.frame(self.address, 12, (address_box_height - address_height) // 2, width - 56, address_height)

            roots_y = 67 + service_height + 12
            roots_height = 68 + len(state.roots) * 43
            self.frame(self.roots_card, 24, roots_y, width, roots_height)
            add_width = self.button_width(self.add_button, 110)
            self.frame(self.roots_heading, 16, 21, width - add_width - 40, 25)
            self.frame(self.add_button, width - add_width - 16, 14, add_width, control_height)
            for index, (well, field, choose, remove) in enumerate(self.folder_controls):
                remove_width = self.button_width(remove, 64)
                choose_width = self.button_width(choose, 64)
                remove_x = width - remove_width - 16
                choose_x = remove_x - choose_width - 6
                y = 59 + index * 43
                well_width = choose_x - 24
                self.frame(well, 16, y, well_width, control_height)
                self.frame(field, 12, (control_height - 20) // 2, well_width - 24, 20)
                self.frame(choose, choose_x, y, choose_width, control_height)
                self.frame(remove, remove_x, y, remove_width, control_height)
            return roots_y + roots_height + 20

        @objc.python_method
        def layout_advanced(self, width):
            self.frame(self.network_heading, 16, 16, width - 32, 24)
            field_width = int((width - 52) / 3)
            for index, (label, surface) in enumerate((
                (self.port_label, self.port_surface),
                (self.share_port_label, self.share_port_surface),
                (self.https_port_label, self.https_port_surface),
            )):
                x = 16 + index * (field_width + 10)
                self.frame(label, x, 52, field_width, 20)
                self.frame(surface, x, 76, field_width, control_height)
                surface.layout_field()
            checkbox_y = 82 + control_height
            self.frame(self.lan, 16, checkbox_y, width - 32, 28)
            hint_height = self.label_height(self.share_port_hint, width - 32)
            self.frame(self.share_port_hint, 16, checkbox_y + 30, width - 32, hint_height)
            button_y = checkbox_y + 38 + hint_height
            self.frame(self.diagnose_button, 16, button_y, self.button_width(self.diagnose_button), control_height)
            network_height = button_y + 50
            self.frame(self.network_card, 24, 67, width, network_height)

            data_y = 67 + network_height + 12
            self.frame(self.data_heading, 16, 16, width - 32, 24)
            description_height = self.label_height(self.storage_hint, width - 32)
            self.frame(self.storage_hint, 16, 47, width - 32, description_height)
            x, y = 16, 55 + description_height
            for button in (self.log_button, self.data_button, self.reset_button, self.reset_entry_button):
                size = self.button_width(button)
                if x > 16 and x + size > width - 16:
                    x, y = 16, y + 42
                self.frame(button, x, y, size, control_height)
                x += size + 8
            data_height = y + 50
            self.frame(self.data_card, 24, data_y, width, data_height)
            return data_y + data_height + 20

        @objc.python_method
        def set_busy(self, busy):
            self.busy = busy
            running = state.controller.running
            editing = not busy and not running
            for _, _, choose, remove in self.folder_controls:
                choose.setEnabled_(editing)
                remove.setEnabled_(editing and len(state.roots) > 1)
            for surface in self.window.input_surfaces:
                surface.field.setEnabled_(editing)
                surface.setNeedsDisplay_(True)
            self.lan.setEnabled_(editing)
            self.add_button.setEnabled_(editing)
            self.start_button.setEnabled_(editing)
            self.stop_button.setEnabled_(running and not busy)
            self.browser_button.setEnabled_(running and not busy)
            self.diagnose_button.setEnabled_(not busy)
            self.reset_button.setEnabled_(not busy)
            self.reset_entry_button.setEnabled_(not busy)
            self.stop_menu.setEnabled_(running and not busy)
            self.service_status.setStringValue_(
                tr("service_busy" if busy else "service_running" if running else "service_stopped"))

        @objc.python_method
        def show_addresses(self, value):
            self.address.setStringValue_(value)
            self.layout_window()

        @objc.python_method
        def read_configuration(self):
            state.port = str(self.port.stringValue())
            state.share_port = str(self.share_port.stringValue())
            state.https_port = str(self.https_port.stringValue())
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
                state.worker("diagnose", lambda: network_diagnostics(config, running, state.controller.data_path))
            except (LauncherError, OSError) as error:
                self.status.setStringValue_(str(error))

        def openBrowser_(self, sender):
            if not self.quitting and state.controller.running:
                try:
                    A.NSWorkspace.sharedWorkspace().openURL_(F.NSURL.URLWithString_(state.controller.reader_url))
                except (LauncherError, OSError, RuntimeError) as error:
                    self.status.setStringValue_(str(error))

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

        def resetSecurityEntry_(self, sender):
            if self.busy or self.quitting:
                return
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_(tr("reset_entry"))
            alert.setInformativeText_(tr("reset_entry_hint"))
            alert.addButtonWithTitle_(tr("disable_entry"))
            alert.addButtonWithTitle_(tr("cancel"))
            if alert.runModal() != A.NSAlertFirstButtonReturn:
                return
            self.set_busy(True)
            self.status.setStringValue_(tr("resetting_entry"))
            state.worker("reset-entry", state.controller.reset_security_entry)

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
                    if kind in ("start", "stop", "reset", "reset-entry", "diagnose", "smoke-start"):
                        self.begin_exit()
                    continue
                if kind == "background-diagnose":
                    # Automatic checks must not unlock controls belonging to
                    # a later foreground start/stop/reset operation.
                    if success and not self.busy and state.controller.running:
                        addresses = [tr("local") + ": " + value["local_url"]]
                        addresses.extend(tr("lan_address") + ": " + url for url in value["lan_urls"])
                        self.show_addresses("\n".join(addresses))
                    continue
                self.set_busy(False)
                if not success:
                    self.status.setStringValue_(str(value))
                elif kind == "start":
                    self.status.setStringValue_(tr("running"))
                    self.show_addresses(value)
                    self.openBrowser_(None)
                    self.window.orderOut_(None)
                    config = state.controller.config
                    state.worker("background-diagnose", lambda: network_diagnostics(config, True, state.controller.data_path))
                elif kind == "stop":
                    self.status.setStringValue_(tr("stopped"))
                elif kind == "reset":
                    self.status.setStringValue_(tr("reset_done"))
                elif kind == "reset-entry":
                    self.status.setStringValue_(tr("reset_entry_done"))
                    if state.controller.config is not None:
                        self.show_addresses(state.controller.config.local_url)
                    warnings = security_entry_recovery_warnings(value, language)
                    if warnings:
                        alert = A.NSAlert.alloc().init()
                        alert.setMessageText_(tr("reset_entry"))
                        alert.setInformativeText_(tr("reset_entry_done") + "\n\n" + "\n".join(warnings))
                        alert.addButtonWithTitle_(tr("ok"))
                        alert.runModal()
                elif kind == "diagnose":
                    self.status.setStringValue_(value["message"])
                    addresses = [tr("local") + ": " + value["local_url"]]
                    addresses.extend(tr("lan_address") + ": " + url for url in value["lan_urls"])
                    self.show_addresses("\n".join(addresses))
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
                if capture_launcher:
                    self.capture_launcher_smoke()
                self.window.setContentSize_(A.NSMakeSize(610, 420))
                divider = self.sidebar.frame().size.width
                home_visible = not self.home.isHidden() and self.advanced_page.isHidden()
                self.advanced_nav.performClick_(None)
                advanced_visible = self.home.isHidden() and not self.advanced_page.isHidden()
                port_metrics = self.capture_port_smoke()
                self.home_nav.performClick_(None)
                body_height = self.root.bounds().size.height
                footer = self.footer.frame()
                start = self.start_button.frame()
                checks = {"window_visible": bool(self.window.isVisible()),
                          "wordmark_loaded": self.brand.image() is not None,
                          "status_menu_present": self.status_item.menu() is not None,
                          "native_buttons": isinstance(self.start_button, A.NSButton),
                          "port_field": isinstance(self.port, A.NSTextField),
                          "action_buttons_are_borderless": not self.start_button.isBordered() and not self.diagnose_button.isBordered(),
                          "ports_and_actions_share_height": all(
                              view.frame().size.height == start.size.height == control_height
                              for view in self.window.input_surfaces),
                          "port_editor_fits_surface": (self.port.frame().size.height >= self.port.intrinsicContentSize().height
                                                       and self.port.frame().size.height < self.port_surface.frame().size.height),
                          "port_uses_a_single_outline": (not self.port.isBezeled() and not self.port.isBordered()
                                                        and self.port.focusRingType() == A.NSFocusRingTypeNone),
                          "port_single_line": bool(self.port.cell().usesSingleLineMode()),
                          "status_icon_is_template": bool(self.status_item.button().image() is not None
                                                           and self.status_item.button().image().isTemplate()),
                          "small_window_footer_visible": (footer.origin.y >= 0
                              and footer.origin.y + footer.size.height <= body_height
                              and start.origin.y >= 0 and start.origin.y + start.size.height <= footer.size.height),
                          "body_scrolls_independently": self.scroll.documentView() == self.document,
                          "navigation_switches_pages": (home_visible and advanced_visible
                              and not self.home.isHidden() and self.advanced_page.isHidden()),
                          "sidebar_and_footer_separate_from_scroll": (self.scroll.frame().origin.x == divider
                              and footer.origin.x == divider and self.scroll.frame().size.height <= footer.origin.y),
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
                if self.port_surface.has_focus() != focus:
                    raise LauncherError("The port outline did not follow keyboard focus.")
                self.window.displayIfNeeded()
                surface = self.port_surface
                rect = A.NSInsetRect(surface.convertRect_toView_(surface.bounds(), self.root), -12, -12)
                self.write_smoke_capture(self.root, rect, name)
            return {"height": self.port_surface.frame().size.height,
                    "editor_height": self.port.frame().size.height,
                    "intrinsic_height": self.port.intrinsicContentSize().height,
                    "font_size": self.port.font().pointSize(),
                    "screenshots": ["port-focused.png", "port-unfocused.png"]}

        @objc.python_method
        def capture_launcher_smoke(self):
            self.window.makeFirstResponder_(None)
            previous = self.update_status.stringValue()
            self.update_status.setStringValue_("")
            for name, advanced in (("launcher-reader.png", False), ("launcher-advanced.png", True)):
                self.show_page(advanced)
                self.window.displayIfNeeded()
                # The window frame includes the native macOS title bar.
                view = self.root.superview() or self.root
                self.write_smoke_capture(view, view.bounds(), name)
            self.show_page(False)
            self.update_status.setStringValue_(previous)

        @objc.python_method
        def write_smoke_capture(self, view, rect, name):
            bitmap = view.bitmapImageRepForCachingDisplayInRect_(rect)
            if bitmap is None:
                raise LauncherError(f"Could not capture {name}.")
            view.cacheDisplayInRect_toBitmapImageRep_(rect, bitmap)
            png = bitmap.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})
            if png is None or not png.writeToFile_atomically_(str(smoke_report.parent / name), True):
                raise LauncherError(f"Could not write {name}.")

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

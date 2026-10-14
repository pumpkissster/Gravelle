"""Exercise the desktop root list without opening a server or user configuration."""
from pathlib import Path
import gc
import sys
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

from desktop.core import LauncherConfig, load_config, save_config
from desktop.launcher import LauncherWindow, rounded_button_surface
from desktop.runtime import ServerController


@unittest.skipUnless(sys.platform == "win32", "Windows launcher controls require the desktop Tk runtime")
class MultipleRootWindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = Path(self.temporary.name)
        self.first = self.storage / "first"
        self.second = self.storage / "写真二"
        self.first.mkdir()
        self.second.mkdir()
        self.config_path = self.storage / "launcher.json"
        save_config(self.config_path, LauncherConfig(str(self.first)))
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self._destroy_tk)
        with patch.object(LauncherWindow, "_worker"), patch.object(self.root, "after"):
            self.window = LauncherWindow(self.root, self.config_path, self.storage, Mock())
        self.root.update_idletasks()

    def _destroy_tk(self):
        # Tcl interpreters and Variables must be finalized by their creating
        # thread. root.destroy alone leaves window/callback/image cycles alive;
        # a later catalogue thread can otherwise trigger their cyclic GC.
        root = self.root
        self.window.controller.stop()
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
        root.destroy()
        self.window = None
        self.root = None
        root = None
        gc.collect()

    def test_add_remove_and_save_multiple_folders(self):
        with patch("tkinter.filedialog.askdirectory", return_value=str(self.second)):
            self.window.add_folder()
        self.assertEqual(len(self.window.library_values), 2)
        save_config(self.config_path, self.window._configuration())
        self.assertEqual(load_config(self.config_path).library_paths, (str(self.first), str(self.second)))
        self.window.remove_folder(0)
        self.assertEqual(self.window._configuration().library_paths, (str(self.second),))
        self.window.remove_folder(0)
        self.assertEqual(len(self.window.library_values), 1)
        self.assertTrue(self.window.folder_widgets[-1].instate(["disabled"]))

    def test_duplicate_picker_selection_does_not_create_second_row(self):
        with patch("tkinter.filedialog.askdirectory", return_value=str(self.first)):
            self.window.add_folder()
        self.assertEqual(len(self.window.library_values), 1)
        self.assertIn("已经在列表", self.window.status.get())

    def test_all_root_controls_are_disabled_during_start_and_running(self):
        with patch("tkinter.filedialog.askdirectory", return_value=str(self.second)):
            self.window.add_folder()
        for running, busy in ((False, True), (True, False)):
            with self.subTest(running=running, busy=busy), \
                    patch.object(ServerController, "running", new_callable=PropertyMock, return_value=running):
                self.window._set_busy(busy)
                for widget in (*self.window.folder_widgets, self.window.add_folder_button):
                    self.assertTrue(widget.instate(["disabled"]))
                with patch("tkinter.filedialog.askdirectory") as choose:
                    self.window.add_folder()
                    self.window.choose_folder(0)
                    self.window.remove_folder(0)
                    choose.assert_not_called()
                self.assertEqual(len(self.window.library_values), 2)
        self.window._set_busy(False)
        for widget in (*self.window.folder_widgets, self.window.add_folder_button):
            self.assertFalse(widget.instate(["disabled"]))

    def test_header_update_action_keeps_check_download_and_install_states(self):
        with patch.object(self.window, "_worker") as worker:
            self.window.update_button.invoke()
            self.assertTrue(self.window.update_button.instate(["disabled"]))
            self.assertEqual(worker.call_args.args[0], "update")
            self.window.events.put(("update", True, {
                "available": True, "message": "发现新版本 0.8.4", "version": "0.8.4"
            }))
            with patch.object(self.root, "after"):
                self.window._poll()
            self.assertEqual(self.window.update_button.cget("text"), "下载更新")
            self.assertFalse(self.window.update_button.instate(["disabled"]))
            self.window.update_button.invoke()
            self.assertTrue(self.window.update_button.instate(["disabled"]))
            self.assertEqual(worker.call_args.args[0], "download")
            self.window.events.put(("download", True, {
                "version": "0.8.4", "path": str(self.storage / "setup.exe"), "sha256": "a" * 64
            }))
            with patch.object(self.root, "after"):
                self.window._poll()
            self.assertEqual(self.window.update_button.cget("text"), "安装更新")
            self.assertFalse(self.window.update_button.instate(["disabled"]))
            self.assertIn("SHA-256", self.window.update_status.get())

    def test_rounded_button_elements_keep_transparent_corners_and_parent_surfaces(self):
        style = self.window.ttk.Style(self.root)
        expected = {"TButton", "Compact.TButton", "Page.Compact.TButton", "Folder.TButton",
                    "Remove.TButton", "Outline.TButton", "Accent.TButton"}
        self.assertEqual(set(self.window.button_images), expected)
        for name, images in self.window.button_images.items():
            with self.subTest(style=name):
                layout = style.layout(name)
                self.assertEqual(layout[0][0], self.window.button_surface_elements[name])
                self.assertNotIn("Button.focus", str(layout))
                self.assertEqual(style.lookup(name, "background"), "#f7f2f4" if name.startswith("Page.") else "#fefdfd")
                self.assertEqual(len(images), 7)  # normal, hover, press, disabled, three focused states
                for image in images:
                    self.assertTrue(self.root.tk.call(str(image), "transparency", "get", 0, 0))
                    self.assertFalse(self.root.tk.call(str(image), "transparency", "get", image.width() // 2, image.height() // 2))
        for scale in (1, 1.5, 2, 3):
            surface = rounded_button_surface(scale, "#ea5098", "#ea5098", "#fefdfd")
            self.assertEqual(surface.getpixel((0, 0))[3], 0)
            self.assertEqual(surface.getpixel((surface.width // 2, surface.height // 2)), (234, 80, 152, 255))
            alpha = surface.getchannel("A")
            self.assertTrue(any(alpha.histogram()[1:255]))

    def test_rounded_buttons_preserve_mouse_keyboard_focus_and_disabled_behavior(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.update()
        button = self.window.update_button
        action = Mock()
        button.configure(command=action)
        button.focus_force()
        self.root.update()
        self.assertTrue(button.instate(["focus"]))
        button.event_generate("<Return>")
        self.root.update()
        self.assertEqual(action.call_count, 1)
        button.event_generate("<KeyPress-space>")
        # ttk's native Space activation briefly retains the pressed state.
        finished = self.window.tk.BooleanVar(value=False)
        self.root.after(150, lambda: finished.set(True))
        self.root.wait_variable(finished)
        self.assertEqual(action.call_count, 2)
        button.event_generate("<Enter>")
        button.event_generate("<ButtonPress-1>", x=button.winfo_width() // 2, y=button.winfo_height() // 2)
        button.event_generate("<ButtonRelease-1>", x=button.winfo_width() // 2, y=button.winfo_height() // 2)
        self.root.update()
        self.assertEqual(action.call_count, 3)
        next_widget = self.root.tk.call("tk_focusNext", str(button))
        button.event_generate("<Tab>")
        self.root.update()
        self.assertEqual(str(self.root.focus_get()), str(next_widget))
        button.configure(state="disabled")
        button.focus_force()
        self.root.update()
        self.window._configure_button_cursors(self.root)
        self.assertEqual(str(button.cget("cursor")), "arrow")
        button.invoke()
        button.event_generate("<Return>")
        button.event_generate("<KeyPress-space>")
        button.event_generate("<ButtonPress-1>", x=1, y=1)
        button.event_generate("<ButtonRelease-1>", x=1, y=1)
        self.root.update()
        self.assertEqual(action.call_count, 3)

    def test_password_dialog_uses_same_rounded_buttons(self):
        dialog = self.window.reset_password()
        self.root.update_idletasks()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = [widget for widget in descendants(dialog) if isinstance(widget, self.window.ttk.Button)]
        self.assertEqual([button.cget("text") for button in buttons], ["取消", "保存新密码"])
        style = self.window.ttk.Style(self.root)
        for button in buttons:
            name = str(button.cget("style")) or "TButton"
            self.assertEqual(style.layout(name)[0][0], self.window.button_surface_elements[name])
        buttons[0].invoke()
        self.assertFalse(dialog.winfo_exists())

    def test_small_windows_and_high_dpi_keep_actions_visible_and_roots_scrollable(self):
        paths = [self.storage / f"root {index}" for index in range(6)]
        for path in paths:
            path.mkdir()
        save_config(self.config_path, LauncherConfig(library_paths=[str(path) for path in paths]))
        # Measure our own transparent test window. No native automation APIs,
        # user settings or external update requests are involved.
        self.root.attributes("-alpha", 0)
        for screen_width, screen_height, scale in ((800, 600, 1), (1280, 720, 1.5), (3840, 2160, 2)):
            with self.subTest(screen=(screen_width, screen_height), scale=scale):
                for widget in self.root.winfo_children():
                    widget.destroy()
                self.root.tk.call("tk", "scaling", scale * 96 / 72)
                with patch.object(LauncherWindow, "_worker"), patch.object(self.root, "after"), \
                        patch.object(self.root, "winfo_screenwidth", return_value=screen_width), \
                        patch.object(self.root, "winfo_screenheight", return_value=screen_height):
                    window = LauncherWindow(self.root, self.config_path, self.storage, Mock())
                width, height = self.root.minsize()
                self.root.geometry(f"{width}x{height}")
                self.root.deiconify()
                self.root.update_idletasks()
                self.assertEqual(len(window.library_values), 6)
                self.assertGreater(window.body_content.winfo_height(), window.body_canvas.winfo_height())
                for button in (window.start_button, window.browser_button, window.stop_button, window.update_button):
                    relative_x = button.winfo_rootx() - self.root.winfo_rootx()
                    relative_y = button.winfo_rooty() - self.root.winfo_rooty()
                    self.assertGreaterEqual(relative_x, 0)
                    self.assertGreaterEqual(relative_y, 0)
                    self.assertLessEqual(relative_x + button.winfo_width(), self.root.winfo_width())
                    self.assertLessEqual(relative_y + button.winfo_height(), self.root.winfo_height())
                footer_y = window.start_button.winfo_rooty()
                header_y = window.header.winfo_rooty()
                update_y = window.update_button.winfo_rooty()
                for text in ("检查更新", "下载更新", "安装更新", "重试下载"):
                    window.update_button.configure(text=text)
                    self.root.update_idletasks()
                    version_right = window.version_label.winfo_rootx() + window.version_label.winfo_width()
                    update_x = window.update_button.winfo_rootx()
                    version_mid_y = window.version_label.winfo_rooty() + window.version_label.winfo_height() / 2
                    update_mid_y = window.update_button.winfo_rooty() + window.update_button.winfo_height() / 2
                    self.assertGreater(update_x, version_right)
                    self.assertLessEqual(abs(version_mid_y - update_mid_y), 1)
                    self.assertLessEqual(update_x + window.update_button.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
                    self.assertEqual(window.update_button.winfo_rooty(), update_y)
                window.body_canvas.yview_moveto(1)
                self.root.update_idletasks()
                self.assertGreater(window.body_canvas.yview()[0], 0)
                self.assertEqual(window.start_button.winfo_rooty(), footer_y)
                self.assertEqual(window.header.winfo_rooty(), header_y)
                self.assertEqual(window.update_button.winfo_rooty(), update_y)
                self.root.withdraw()


if __name__ == "__main__":
    unittest.main()

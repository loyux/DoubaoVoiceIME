"""GTK desktop frontend and Linux clipboard/keyboard integration."""

import json
import os
import shutil
import signal
import subprocess
import sys
import traceback
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from voice_core import (
    APP_RELEASE, CLOUD_ENGINES, HOTWORDS_PATH, LOG_DIR, MIN_HOLD_DEFAULT, MIN_HOLD_MS, Mic,
    list_input_devices, log, resolve_mic_device, save_config,
)
from voice_linux import MAX_RECORDING_SECONDS, Recording, launcher_command, receive_action
from wayland_keyboard import active_window

NAME = "豆包语音输入"
ICON = "audio-input-microphone"
OVERLAY_EDGE_MARGIN = 32
OVERLAY_CSS = b"""
window#doubao-voice-overlay { background-color: transparent; }
#doubao-voice-panel {
    background-color: rgba(25, 28, 35, 0.97);
    border: 1px solid #565e70;
    border-radius: 14px;
    padding: 10px 16px;
    color: #f5f7fb;
}
#doubao-voice-panel label { color: #f5f7fb; }
#doubao-voice-panel .recording-dot { color: #ff687a; }
#doubao-voice-panel .status-message { font-weight: bold; }
#doubao-voice-panel .transcript { color: #cbd3e1; }
#doubao-voice-panel levelbar trough { min-height: 4px; }
#doubao-voice-panel levelbar block.filled { background-color: #ff687a; }
"""


def run_tool(command, env=None):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", timeout=2, check=True, env=env)
    return result.stdout.strip()


class DesktopInput:
    def __init__(self, display):
        self.wayland = "Wayland" in display.__gtype__.name
        self.env = os.environ.copy()
        desktop = self.env.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
        if self.wayland and "hyprland" in desktop and shutil.which("hyprctl"):
            try:
                instances = json.loads(run_tool(["hyprctl", "-j", "instances"]))
                socket_name = Path(self.env.get("WAYLAND_DISPLAY", "wayland-0")).name
                for instance in instances:
                    if instance.get("wl_socket") == socket_name:
                        self.env["HYPRLAND_INSTANCE_SIGNATURE"] = instance["instance"]
                        break
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                log(f"Hyprland instance lookup: {exc}")
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def target(self):
        try:
            if not self.wayland:
                window = run_tool(["xdotool", "getwindowfocus"])
                return ("x11", window) if window and window not in ("0", "1") else None
            return active_window(self.env)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log(f"focused window unavailable: {exc}")
        return None

    def copy(self, text, interactive=False):
        if self.wayland and shutil.which("wl-copy"):
            try:
                # wl-copy backgrounds itself and owns the clipboard after this application exits.
                subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"],
                               input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2, check=True)
                return True
            except (OSError, subprocess.SubprocessError) as exc:
                log(f"wl-copy failed: {exc}")
        if not self.wayland or interactive:
            self.clipboard.set_text(text, -1)
            return True
        return False

    def deliver(self, text, target, terminal_paste):
        if not self.copy(text):
            return "识别完成，可在设置窗口复制结果"
        if target is None or self.target() != target:
            return "已复制到剪贴板"
        if self.wayland:
            helper = Path(__file__).with_name("wayland_keyboard.py")
            command = [sys.executable, str(helper), *target,
                       "terminal" if terminal_paste else "normal"]
        else:
            chord = "ctrl+shift+v" if terminal_paste else "ctrl+v"
            command = ["xdotool", "key", "--clearmodifiers", chord]
        try:
            run_tool(command, env=self.env)
        except subprocess.CalledProcessError as exc:
            if self.wayland and exc.returncode == 3:
                return "已复制到剪贴板"
            log(f"paste shortcut failed: {exc.stderr.strip() or exc}")
            return "自动粘贴失败，已复制到剪贴板"
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"paste shortcut failed: {exc}")
            return "自动粘贴失败，已复制到剪贴板"
        return "已发送粘贴快捷键"


def autostart_path():
    return Path(HOTWORDS_PATH).parent.parent / "autostart/doubao-voice.desktop"


def set_autostart(enabled):
    path = autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    keyfile = GLib.KeyFile()
    group = "Desktop Entry"
    for key, value in (("Type", "Application"), ("Name", NAME), ("Icon", ICON)):
        keyfile.set_string(group, key, value)
    parts = []
    for arg in launcher_command():
        arg = arg.replace("%", "%%")
        for char in ('\\', '"', '`', '$'):
            arg = arg.replace(char, '\\' + char)
        parts.append(f'"{arg}"')
    keyfile.set_string(group, "Exec", " ".join(parts))
    path.parent.mkdir(parents=True, exist_ok=True)
    keyfile.save_to_file(str(path))


class StatusOverlay:
    def __init__(self, wayland):
        self.window = None
        self.layer_shell = None
        if wayland:
            try:
                gi.require_version("GtkLayerShell", "0.1")
                from gi.repository import GtkLayerShell

                if GtkLayerShell.is_supported():
                    self.layer_shell = GtkLayerShell
            except (ImportError, ValueError):
                pass
            if self.layer_shell is None:
                return

        kind = Gtk.WindowType.TOPLEVEL if wayland else Gtk.WindowType.POPUP
        self.window = Gtk.Window(type=kind)
        self.window.set_decorated(False)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_name("doubao-voice-overlay")
        self.input_region = cairo.Region()
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.window.set_visual(visual)
        self.window.connect("realize", self.make_click_through)
        provider = Gtk.CssProvider()
        provider.load_from_data(OVERLAY_CSS)
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if self.layer_shell is not None:
            layer = self.layer_shell
            layer.init_for_window(self.window)
            layer.set_namespace(self.window, "doubao-voice")
            layer.set_layer(self.window, layer.Layer.OVERLAY)
            layer.set_keyboard_mode(self.window, layer.KeyboardMode.NONE)
            layer.set_anchor(self.window, layer.Edge.BOTTOM, True)
            layer.set_margin(self.window, layer.Edge.BOTTOM, OVERLAY_EDGE_MARGIN)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.set_name("doubao-voice-panel")
        box.set_direction(Gtk.TextDirection.LTR)
        box.set_size_request(440, -1)
        header = Gtk.Box(spacing=8)
        self.dot = Gtk.Label(label="●", no_show_all=True)
        self.dot.get_style_context().add_class("recording-dot")
        self.spinner = Gtk.Spinner(no_show_all=True, valign=Gtk.Align.CENTER)
        self.label = Gtk.Label(xalign=0, single_line_mode=True, max_width_chars=32,
                               ellipsize=Pango.EllipsizeMode.END)
        self.label.get_style_context().add_class("status-message")
        self.transcript = Gtk.Label(xalign=0, single_line_mode=True, max_width_chars=48,
                                    ellipsize=Pango.EllipsizeMode.START)
        self.transcript.get_style_context().add_class("transcript")
        self.level = Gtk.LevelBar(no_show_all=True, valign=Gtk.Align.CENTER)
        self.level.set_size_request(44, -1)
        header.pack_start(self.dot, False, False, 0)
        header.pack_start(self.spinner, False, False, 0)
        header.pack_start(self.label, True, True, 0)
        header.pack_start(self.level, False, False, 0)
        box.pack_start(header, False, False, 0)
        box.pack_start(self.transcript, False, False, 0)
        self.window.add(box)
        _, line_height = self.transcript.create_pango_layout("转录").get_pixel_size()
        self.transcript.set_size_request(-1, line_height)

    @staticmethod
    def make_click_through(window):
        window.get_window().set_pass_through(True)

    def show(self, message, text, recording, working):
        if self.window is None:
            return
        self.label.set_text(message)
        preview = " ".join(text[-120:].split())
        self.transcript.set_text(preview)
        self.dot.set_visible(recording)
        self.spinner.set_visible(working)
        self.spinner.props.active = working
        self.level.set_visible(recording)
        self.window.show_all()
        # A Wayland layer can get a new surface when it is shown again.
        self.window.get_window().input_shape_combine_region(self.input_region, 0, 0)
        if self.layer_shell is None:
            display = self.window.get_display()
            _, x, y = display.get_default_seat().get_pointer().get_position()
            area = display.get_monitor_at_point(x, y).get_workarea()
            _, size = self.window.get_preferred_size()
            self.window.move(area.x + (area.width - size.width) // 2,
                             area.y + area.height - size.height - OVERLAY_EDGE_MARGIN)

    def hide(self):
        if self.window is not None:
            self.window.hide()

    def update_level(self, level):
        if self.window is not None:
            self.level.set_value(level)


class Desktop:
    def __init__(self, args, config, server):
        self.args = args
        self.config = config
        self.server = server
        self.input = DesktopInput(Gdk.Display.get_default())
        self.overlay = None if args.no_ui else StatusOverlay(self.input.wayland)
        device = args.device
        if device is None:
            device = resolve_mic_device(config.get("mic_device"))
        self.mic = Mic(device)
        self.recording = None
        self.target = None
        self.live = ""
        self.result = ""
        self.status = "就绪"
        self.capture_timer = 0
        self.hide_timer = 0
        self.closing = False
        self.settings = None
        self.tray = None

    def run(self):
        GLib.io_add_watch(self.server, GLib.PRIORITY_DEFAULT, GLib.IO_IN, self.read_command)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)
        GLib.timeout_add(80, self.update_meter)
        if not self.args.no_tray:
            self.make_tray()
        if self.args.action == "settings" or self.tray is None:
            self.show_settings()
        if self.config.get("mic_mode") == "always":
            self.prewarm_mic()
        Gtk.main()
        return 0

    def read_command(self, _source, _condition):
        action = receive_action(self.server)
        if action == "start":
            hold = self.config.get("min_hold_ms")
            self.start_recording(hold if type(hold) is int and hold in MIN_HOLD_MS
                                 else MIN_HOLD_DEFAULT)
        elif action == "stop":
            self.stop_recording()
        elif action == "toggle":
            self.toggle()
        elif action == "cancel":
            self.stop_recording(cancel=True)
        elif action == "settings":
            self.show_settings()
        elif action == "quit":
            self.quit()
        return GLib.SOURCE_CONTINUE

    def make_tray(self):
        menu = Gtk.Menu()
        for label, action in (("设置…", self.show_settings), ("开始 / 停止录音", self.toggle),
                              ("取消录音", lambda: self.stop_recording(cancel=True)),
                              ("退出", self.quit)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, callback=action: callback())
            menu.append(item)
        menu.show_all()
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as indicator

            self.tray = indicator.Indicator.new("doubao-voice", ICON,
                                                indicator.IndicatorCategory.APPLICATION_STATUS)
            self.tray.set_status(indicator.IndicatorStatus.ACTIVE)
            self.tray.set_menu(menu)
            if self.input.wayland and not self.tray_watcher_available():
                self.tray = None
        except (ImportError, ValueError):
            if not self.input.wayland:
                self.tray = Gtk.StatusIcon.new_from_icon_name(ICON)
                self.tray.set_tooltip_text(NAME)
                self.tray.connect("activate", lambda _icon: self.show_settings())
                self.tray.connect("popup-menu", lambda icon, button, timestamp:
                                  menu.popup(None, None, Gtk.StatusIcon.position_menu, icon,
                                             button, timestamp))

    @staticmethod
    def tray_watcher_available():
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            result = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                   "org.freedesktop.DBus", "NameHasOwner",
                                   GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                                   None, Gio.DBusCallFlags.NONE, 500, None)
            return result.unpack()[0]
        except GLib.Error:
            return False

    def show_settings(self):
        if self.settings is None:
            self.settings = Settings(self)
        self.settings.window.present()

    def prewarm_mic(self):
        try:
            self.mic.open()
        except Exception as exc:
            log(traceback.format_exc())
            self.report(f"麦克风打开失败：{exc}")

    def update_config(self, key, value):
        self.config[key] = value
        saved = dict(self.config)
        if self.args.engine is not None and key != "asr_engine":
            # The command-line engine is scoped to this launch.
            from voice_core import load_config

            saved["asr_engine"] = load_config().get("asr_engine", "doubao_api")
        save_config(saved)
        if self.recording is None and key in ("mic_mode", "mic_device"):
            self.apply_mic_config()

    def apply_mic_config(self):
        self.mic.close()
        if self.args.device is None:
            self.mic.device = resolve_mic_device(self.config.get("mic_device"))
        if self.config.get("mic_mode") == "always":
            self.prewarm_mic()

    def start_recording(self, hold_ms=0):
        if self.recording is not None:
            return
        self.live = ""
        self.target = self.input.target() if not self.args.no_inject else None
        if self.settings is not None and self.settings.window.is_active():
            self.target = None
        job = Recording(self.mic, self.config, self.args.verbose, hold_ms,
                        lambda text: GLib.idle_add(self.live_text, job, text),
                        lambda status: GLib.idle_add(self.job_status, job, status),
                        lambda *result: GLib.idle_add(self.recording_done, *result))
        self.recording = job
        try:
            job.start()
        except Exception as exc:
            job.stop(cancel=True)
            self.recording = None
            log(traceback.format_exc())
            self.report(f"麦克风打开失败：{exc}", dismiss=True)
            return
        self.report("正在录音…")
        self.capture_timer = GLib.timeout_add_seconds(MAX_RECORDING_SECONDS, self.time_limit)

    def toggle(self):
        if self.recording is None:
            self.start_recording()
        else:
            self.stop_recording()

    def time_limit(self):
        self.capture_timer = 0
        self.stop_recording()
        return GLib.SOURCE_REMOVE

    def stop_recording(self, cancel=False):
        job = self.recording
        if job is None or (job.finished.is_set() and not cancel):
            return
        if self.capture_timer:
            GLib.source_remove(self.capture_timer)
            self.capture_timer = 0
        job.stop(cancel)
        self.report("已取消" if job.cancelled.is_set() else "正在转文字…",
                    dismiss=job.cancelled.is_set())

    def recording_done(self, job, text, error):
        if self.closing or self.recording is not job:
            return
        if self.capture_timer:
            GLib.source_remove(self.capture_timer)
            self.capture_timer = 0
        job.stop()
        self.recording = None
        self.live = ""
        if job.cancelled.is_set():
            self.report("已取消", dismiss=True)
        elif error:
            self.report(f"识别失败：{error}", dismiss=True)
        elif not text:
            self.report("未识别到内容", dismiss=True)
        else:
            self.result = text
            status = "识别完成" if self.args.no_inject else self.input.deliver(
                text, self.target, job.config.get("paste_shortcut") == "terminal")
            self.report(status, dismiss=True, show_result=True)
        if job.config.get("mic_mode") != self.config.get("mic_mode"):
            self.apply_mic_config()

    def live_text(self, job, text):
        if self.recording is job and not job.cancelled.is_set():
            self.live = text
            self.report(self.status)

    def job_status(self, job, status):
        if self.recording is job and not job.cancelled.is_set():
            self.report(status)

    def report(self, status, dismiss=False, show_result=False):
        self.status = status
        if self.settings is not None:
            self.settings.refresh_status()
        if self.overlay is None:
            return
        if self.hide_timer:
            GLib.source_remove(self.hide_timer)
            self.hide_timer = 0
        job = self.recording
        active = job is not None and not job.finished.is_set()
        working = job is not None and job.finished.is_set() and not job.cancelled.is_set()
        snippet = self.result if show_result else self.live
        self.overlay.show(status, snippet, active, working)
        if dismiss:
            self.hide_timer = GLib.timeout_add_seconds(4, self.hide_overlay)

    def hide_overlay(self):
        self.hide_timer = 0
        self.overlay.hide()
        return GLib.SOURCE_REMOVE

    def update_meter(self):
        if self.overlay is not None and self.recording is not None:
            self.overlay.update_level(self.mic.level)
        return GLib.SOURCE_CONTINUE

    def quit(self):
        self.closing = True
        if self.recording is not None:
            self.recording.stop(cancel=True)
        self.mic.close()
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE


class Settings:
    def __init__(self, app):
        self.app = app
        self.config = app.config
        self.window = Gtk.Window(title=f"{NAME} · Linux  v{APP_RELEASE}", default_width=530)
        self.window.set_icon_name(ICON)
        self.window.connect("delete-event", self.close)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=18)
        self.window.add(box)
        self.toggles = Gtk.Box(spacing=8)
        box.pack_start(self.toggles, False, False, 0)
        self.controls = Gtk.Grid(column_spacing=14, row_spacing=8)
        options_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                              propagate_natural_height=True)
        options_scroller.add(self.controls)
        box.pack_start(options_scroller, True, True, 0)
        self.row = 0

        self.combo("识别引擎", "asr_engine", [(k, v[0]) for k, v in CLOUD_ENGINES.items()])
        devices = list_input_devices()
        options = [("default", "系统默认")] + [
            (str(d["index"]), f'{d["index"]} · {d["name"]} ({d["hostapi"]})') for d in devices
        ]
        device = app.mic.device
        self.combo("麦克风", "mic_device", options, "default" if device is None else str(device),
                   lambda value: next(({"name": d["name"], "hostapi": d["hostapi"]}
                                       for d in devices if str(d["index"]) == value), None))
        self.combo("最短按住时间", "min_hold_ms", [(str(ms), f"{ms} 毫秒") for ms in MIN_HOLD_MS],
                   str(self.config.get("min_hold_ms", MIN_HOLD_DEFAULT)), int)
        self.combo("标点", "punctuation_mode", [
            ("keep_all", "全部保留"), ("remove_period", "去掉句末句号"),
            ("remove_trailing", "去掉句末标点"), ("space_replace", "空格替代标点"),
        ])
        self.combo("分段", "paragraph_mode", [
            ("none", "不分段"), ("blank", "分段后空行"), ("single", "分段不空行"),
        ])
        self.combo("粘贴快捷键", "paste_shortcut", [
            ("normal", "Ctrl+V"), ("terminal", "Ctrl+Shift+V（终端）"),
        ])
        self.checkbox("麦克风预热", self.config.get("mic_mode") == "always",
                      lambda enabled: app.update_config("mic_mode", "always" if enabled else "press"))
        self.checkbox("识别后用豆包整理文字", self.config.get("smart_text") is True,
                      lambda enabled: app.update_config("smart_text", enabled))
        self.checkbox("登录时启动", autostart_path().exists(), self.autostart_changed)
        if app.args.device is not None:
            self.mic_combo.set_sensitive(False)

        shortcuts = Gtk.Label(label="全局快捷键由桌面配置。下方按钮可直接试录。",
                              xalign=0, wrap=True)
        box.pack_start(shortcuts, False, False, 0)
        self.status = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(self.status, False, False, 0)
        self.output = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow(min_content_height=95)
        scroller.add(self.output)
        box.pack_start(scroller, True, True, 0)
        buttons = Gtk.Box(spacing=6)
        box.pack_start(buttons, False, False, 0)
        self.record = self.button(buttons, "开始录音", app.toggle)
        self.cancel = self.button(buttons, "取消", lambda: app.stop_recording(cancel=True))
        self.copy = self.button(buttons, "复制结果", self.copy_result)
        self.button(buttons, "热词文件", lambda: self.open_path(Path(HOTWORDS_PATH)))
        self.button(buttons, "日志", self.open_logs)
        if app.tray is None:
            self.button(buttons, "退出", app.quit)
        self.window.show_all()
        self.refresh_status()

    def close(self, window, _event):
        if self.app.tray is None:
            self.app.quit()
        else:
            window.hide()
        return True

    def combo(self, label, key, options, selected=None, convert=lambda value: value):
        field = Gtk.ComboBoxText()
        for value, name in options:
            field.append(value, name)
        selected = selected if selected is not None else self.config.get(key)
        if not isinstance(selected, str) or not field.set_active_id(selected):
            field.set_active(0)
        field.set_hexpand(True)
        field.connect("changed", lambda combo: self.app.update_config(key, convert(combo.get_active_id())))
        self.controls.attach(Gtk.Label(label=label, xalign=0), 0, self.row, 1, 1)
        self.controls.attach(field, 1, self.row, 1, 1)
        self.row += 1
        if key == "mic_device":
            self.mic_combo = field

    def checkbox(self, label, selected, callback):
        widget = Gtk.CheckButton(label=label, active=selected, height_request=36)
        self.toggles.pack_start(widget, True, True, 0)
        widget.connect("toggled", lambda button: callback(button.get_active()))

    @staticmethod
    def button(container, label, callback):
        button = Gtk.Button(label=label)
        button.connect("clicked", lambda _button: callback())
        container.pack_start(button, False, False, 0)
        return button

    def refresh_status(self):
        app = self.app
        job = app.recording
        listening = job is not None and not job.finished.is_set()
        self.controls.set_sensitive(job is None)
        self.record.set_label("结束录音" if listening else "开始录音")
        self.record.set_sensitive(job is None or listening)
        self.cancel.set_sensitive(job is not None and not job.cancelled.is_set())
        self.copy.set_sensitive(bool(app.result))
        self.status.set_text(app.status)
        self.output.get_buffer().set_text(app.live if job is not None else app.result)

    def copy_result(self):
        if self.app.input.copy(self.app.result, interactive=True):
            self.status.set_text("已复制到剪贴板")

    def autostart_changed(self, enabled):
        try:
            set_autostart(enabled)
        except (OSError, GLib.Error) as exc:
            self.status.set_text(f"无法设置开机自启：{exc}")

    def open_path(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except GLib.Error as exc:
            self.status.set_text(f"无法打开 {path}：{exc}")

    def open_logs(self):
        path = Path(LOG_DIR)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status.set_text(f"无法创建日志目录：{exc}")
            return
        self.open_path(path)


def run(args, config, server):
    initialized, _ = Gtk.init_check([])
    if not initialized:
        print("无法连接图形会话。可使用 --once --wav 运行命令行识别。")
        return 1
    return Desktop(args, config, server).run()

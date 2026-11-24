import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class OverlayTests(unittest.TestCase):
    def test_overlay_shows_status_above_full_width_transcript(self):
        try:
            from linux_desktop import Gdk, GLib, Gtk, Pango, StatusOverlay
        except ImportError:
            self.skipTest("GTK 3 unavailable")
        if not Gtk.init_check([])[0]:
            self.skipTest("graphical session unavailable")
        wayland = "Wayland" in Gdk.Display.get_default().__gtype__.name
        overlay = StatusOverlay(wayland)
        if overlay.window is None:
            self.skipTest("layer-shell unavailable")
        self.addCleanup(overlay.window.destroy)

        def show(status, text, recording=False, working=False):
            overlay.show(status, text, recording, working)
            GLib.timeout_add(200, Gtk.main_quit)
            Gtk.main()
            self.assertFalse(overlay.label.get_layout().is_ellipsized())
            return overlay.window.get_size()[1]

        def visible_transcript():
            layout = overlay.transcript.get_layout()
            self.assertEqual(layout.get_line_count(), 1)
            encoded = layout.get_text().encode("utf-8")
            return b"".join(
                encoded[run.item.offset:run.item.offset + run.item.length]
                for run in layout.get_line_readonly(0).runs
                if not run.item.analysis.flags & Pango.ANALYSIS_FLAG_IS_ELLIPSIS
            ).decode("utf-8")

        height = show("正在录音…", "", recording=True)
        if wayland:
            layer = overlay.layer_shell
            self.assertTrue(layer.get_anchor(overlay.window, layer.Edge.BOTTOM))
            self.assertFalse(layer.get_anchor(overlay.window, layer.Edge.LEFT))
            self.assertFalse(layer.get_anchor(overlay.window, layer.Edge.RIGHT))
        else:
            display = Gdk.Display.get_default()
            _, x, y = display.get_default_seat().get_pointer().get_position()
            area = display.get_monitor_at_point(x, y).get_workarea()
            width = overlay.window.get_size()[0]
            self.assertEqual(overlay.window.get_position()[0], area.x + (area.width - width) // 2)
        short = "这是实时转录结果"
        self.assertEqual(show("正在录音…", short, recording=True), height)
        self.assertEqual(visible_transcript(), short)
        self.assertLess(overlay.label.get_allocation().y, overlay.transcript.get_allocation().y)
        self.assertGreater(overlay.transcript.get_allocated_width(), overlay.label.get_allocated_width())

        long = "这段文字超过一行可以容纳的宽度。\n" * 20 + "最新转录结果"
        self.assertEqual(show("正在转文字…", long, working=True), height)
        self.assertTrue(visible_transcript().endswith("最新转录结果"))

        final = "这是最终转录结果"
        self.assertEqual(show("已发送粘贴快捷键", final), height)
        self.assertEqual(visible_transcript(), final)

    def test_preferences_can_be_changed_while_recognition_finishes(self):
        try:
            from linux_desktop import GLib, Gtk, Settings
        except ImportError:
            self.skipTest("GTK 3 unavailable")
        if not Gtk.init_check([])[0]:
            self.skipTest("graphical session unavailable")

        app = SimpleNamespace(
            args=SimpleNamespace(device=None), mic=SimpleNamespace(device=None),
            config={"asr_engine": "doubao_api"}, recording=None, tray=True,
            status="正在转文字…", live="", result="", update_config=Mock(), toggle=Mock(),
        )
        with (patch("linux_desktop.list_input_devices", return_value=[]),
              patch("linux_desktop.autostart_path") as path,
              patch("linux_desktop.set_autostart") as set_autostart):
            path.return_value.exists.return_value = False
            settings = Settings(app)
            self.addCleanup(settings.window.destroy)
            settings.window.resize(530, 494)
            GLib.timeout_add(200, Gtk.main_quit)
            Gtk.main()

            app.recording = SimpleNamespace(finished=Mock(), cancelled=Mock())
            app.recording.finished.is_set.return_value = True
            app.recording.cancelled.is_set.return_value = False
            settings.refresh_status()
            toggles = {widget.get_label(): widget for widget in settings.toggles.get_children()}
            self.assertLessEqual(settings.window.get_preferred_size()[0].height, 494)
            self.assertFalse(settings.controls.is_sensitive())
            for label in ("麦克风预热", "登录时启动"):
                self.assertTrue(toggles[label].is_sensitive())
                self.assertGreaterEqual(toggles[label].get_allocated_height(), 36)
                toggles[label].clicked()
            app.update_config.assert_called_once_with("mic_mode", "always")
            set_autostart.assert_called_once_with(True)

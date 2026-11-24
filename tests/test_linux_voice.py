import argparse
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from voice_core import Mic
from voice_linux import Recording, open_control_socket, receive_action, recognize_wav, send_action


class LinuxVoiceTests(unittest.TestCase):
    def test_control_socket_routes_only_known_commands(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": str(time.monotonic_ns())}):
            with open_control_socket() as server:
                with self.assertRaises(OSError):
                    open_control_socket()
                send_action("toggle")
                self.assertEqual(receive_action(server), "toggle")
                send_action("unknown")
                self.assertIsNone(receive_action(server))

    def test_short_tap_does_not_start_cloud_session(self):
        self.assert_capture(30_000, True)

    def test_completed_capture_and_cancelled_capture(self):
        self.assert_capture(0, False)
        self.assert_capture(0, True, explicit_cancel=True)

    def assert_capture(self, hold_ms, cancelled, explicit_cancel=False):
        session, mic = Mock(), Mock()
        session.text = "识别成功"
        engine_started = threading.Event()
        session.start.side_effect = engine_started.set
        done = threading.Event()
        completion = Mock(side_effect=lambda *args: done.set())
        engine = Mock()
        engine.new_session.return_value = session
        with patch("voice_core.create_cloud_engine", return_value=engine):
            job = Recording(mic, {"asr_engine": "doubao_api"}, False, hold_ms,
                            Mock(), Mock(), completion)
            job.start()
            if not hold_ms:
                self.assertTrue(engine_started.wait(2))
            job.stop(cancel=explicit_cancel)
            self.assertTrue(done.wait(2))
        mic.detach.assert_called_once_with(session)
        mic.close.assert_called_once()
        session.close.assert_called_once()
        if cancelled:
            if hold_ms:
                session.start.assert_not_called()
            session.finish.assert_not_called()
            completion.assert_called_once_with(job, "", "")
        else:
            session.finish.assert_called_once()
            completion.assert_called_once_with(job, "识别成功", "")

    def test_audio_prebuffer_is_delivered_before_new_samples(self):
        mic = Mic()
        sink = Mock()
        mic._cb(b"a\0", 1, None, None)
        mic.attach(sink)
        mic._cb(b"b\0", 1, None, None)
        mic.detach(sink)
        mic._cb(b"c\0", 1, None, None)
        self.assertEqual([call.args[0] for call in sink.push.call_args_list], [b"a\0", b"b\0"])

    def test_desktop_default_uses_pipewire_and_preserves_explicit_device(self):
        import sounddevice as sd

        devices = [
            {"name": name, "max_input_channels": 2} for name in ("default", "pulse", "pipewire")
        ]

        def query_devices(index=None, kind=None):
            return devices if index is None and kind is None else devices[index or 0]

        for selection, failure, expected in (
            (None, False, [2]), (None, True, [2, 1]), (0, False, [0])
        ):
            with self.subTest(selection=selection, failure=failure):
                stream = Mock()
                result = [sd.PortAudioError("unavailable"), stream] if failure else [stream]
                mic = Mic(selection)
                with (patch.object(sd, "query_devices", side_effect=query_devices),
                      patch.object(sd, "RawInputStream", side_effect=result) as open_stream,
                      patch("voice_core.log")):
                    mic.open()
                    mic.close()
                    self.assertEqual([args.kwargs["device"] for args in open_stream.call_args_list],
                                     expected)
                stream.close.assert_called_once()

    def test_wav_validation_happens_before_cloud_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-rate.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 80)
            args = argparse.Namespace(wav=str(path), verbose=False)
            engine = Mock()
            with (patch("voice_core.create_cloud_engine", return_value=engine),
                  patch("voice_core.log"), patch("sys.stderr")):
                self.assertEqual(recognize_wav(args, {"asr_engine": "doubao_api"}), 1)
            engine.new_session.assert_not_called()

    def test_system_opus_encodes_pcm(self):
        from api_common import OpusEncoder

        encoder = OpusEncoder()
        try:
            self.assertTrue(encoder.encode(b"\0" * 640))
        finally:
            encoder.close()


if __name__ == "__main__":
    unittest.main()

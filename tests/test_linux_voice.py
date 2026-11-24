import argparse
import base64
import io
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from voice_core import Mic
from voice_linux import (
    LOCAL_ENGINE, Recording, finalize_text, open_control_socket, receive_action, recognize_wav,
    send_action,
)


class LinuxVoiceTests(unittest.TestCase):
    def test_control_socket_routes_only_known_commands(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": str(time.monotonic_ns())}):
            with open_control_socket() as server:
                with self.assertRaises(OSError):
                    open_control_socket()
                send_action("toggle")
                self.assertEqual(receive_action(server), "toggle")
                send_action("start-local")
                self.assertEqual(receive_action(server), "start-local")
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

    def test_local_asr_sends_pcm_as_wav_and_strips_language_header(self):
        from api_llama import transcribe

        pcm = b"\x01\0" * 160
        with patch("api_llama._asr.complete", return_value="language Chinese<asr_text>这是语音。") as infer:
            self.assertEqual(transcribe(pcm, {}), "这是语音。")
        audio = infer.call_args.args[1][0]["content"][0]["input_audio"]
        with wave.open(io.BytesIO(base64.b64decode(audio["data"])), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()),
                             (16000, 1, 2))
            self.assertEqual(wav.readframes(160), pcm)

    def test_local_stream_previews_buffered_and_live_audio_until_release(self):
        from api_llama import LocalASRSession

        on_text = Mock()
        config = {"asr_engine": LOCAL_ENGINE}
        session = LocalASRSession(config, threading.Event(), on_text)
        self.addCleanup(session.close)
        early, live = b"\x01\0" * 160, b"\x02\0" * 160
        with (patch("api_llama.check_local_setup"),
              patch("api_preview.model_available", return_value=True),
              patch("api_preview.StreamingPreview") as preview,
              patch("api_llama.transcribe", return_value="最终转录") as transcribe):
            session.push(early)
            session.start()
            preview.return_value.push.assert_called_once_with(early)
            session.push(live)
            preview.return_value.push.assert_called_with(live)
            emit = preview.call_args.args[0]
            emit("流式预览")
            on_text.assert_called_once_with("流式预览")
            transcribe.assert_not_called()

            session.request_release()
            preview.return_value.stop.assert_called_once_with()
            emit("迟到的预览")
            session.push(b"\x03\0")
            session.finish()
            transcribe.assert_called_once_with(early + live, config)
            self.assertEqual([call.args[0] for call in on_text.call_args_list],
                             ["流式预览", "最终转录"])

    def test_local_transcript_uses_local_editor_and_keeps_original_on_error(self):
        session = Mock(text="这是本地识别结果")
        config = {"asr_engine": LOCAL_ENGINE, "smart_text": True}
        with (patch("voice_core.load_hotwords", return_value=[]),
              patch("voice_core.log"),
              patch("voice_core.api_smart.organize") as cloud,
              patch("api_llama.organize", return_value=("这是本地整理结果。", False)) as local):
            self.assertEqual(finalize_text(session, config), "这是本地整理结果。")
            local.assert_called_once_with(session.text, config)
            local.side_effect = RuntimeError("编辑失败")
            self.assertEqual(finalize_text(session, config), session.text)
            cloud.assert_not_called()


if __name__ == "__main__":
    unittest.main()

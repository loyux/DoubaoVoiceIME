"""Incremental local speech previews through sherpa-onnx Zipformer."""

import functools
import queue
import threading
import time

from api_llama import data_dir

MODEL_FILES = {
    "encoder": "encoder-epoch-99-avg-1.int8.onnx",
    "decoder": "decoder-epoch-99-avg-1.onnx",
    "joiner": "joiner-epoch-99-avg-1.int8.onnx",
    "tokens": "tokens.txt",
}


def model_folder():
    return data_dir() / "models" / "zipformer-zh-en"


def model_paths(folder):
    paths = {key: folder / name for key, name in MODEL_FILES.items()}
    # The full-precision decoder avoids repeat errors seen with the INT8 export.
    if not paths["decoder"].is_file():
        paths["decoder"] = folder / "decoder-epoch-99-avg-1.int8.onnx"
    return paths


def model_available():
    return all(path.is_file() for path in model_paths(model_folder()).values())


@functools.lru_cache(maxsize=1)
def load_recognizer(folder):
    import sherpa_onnx

    files = {key: str(path) for key, path in model_paths(folder).items()}
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        **files, num_threads=2, provider="cpu", sample_rate=16000,
        feature_dim=80, enable_endpoint_detection=False, decoding_method="greedy_search",
    )


class StreamingPreview:
    def __init__(self, on_text):
        self.on_text = on_text
        self.queue = queue.SimpleQueue()
        self.stopped = threading.Event()
        self.started = time.monotonic()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def push(self, pcm):
        if pcm and not self.stopped.is_set() and self.worker.is_alive():
            self.queue.put(pcm)

    def stop(self):
        if not self.stopped.is_set():
            self.stopped.set()
            self.queue.put(None)

    def _run(self):
        from voice_core import log

        try:
            import numpy as np

            recognizer = load_recognizer(model_folder())
            stream = recognizer.create_stream()
            previous = ""
            while not self.stopped.is_set():
                pcm = self.queue.get()
                if pcm is None or self.stopped.is_set():
                    return
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
                stream.accept_waveform(16000, samples / 32768)
                while not self.stopped.is_set() and recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                    text = recognizer.get_result(stream).strip()
                    if text and text != previous and not self.stopped.is_set():
                        if not previous:
                            log(f"local preview first text in {time.monotonic() - self.started:.2f}s")
                        self.on_text(text)
                        previous = text
        except Exception as exc:
            log(f"local preview unavailable: {exc}")

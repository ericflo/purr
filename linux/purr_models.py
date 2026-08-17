"""Local ASR engines and the curated Linux model catalog for Purr."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request


LOG = logging.getLogger("purr")
HF_REVISION = "bf0af9f425fa01809cadec671b3cb672709d13e9"
HF_BASE = f"https://huggingface.co/mudler/parakeet-cpp-gguf/resolve/{HF_REVISION}"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    filename: str
    size: int
    summary: str
    languages: str
    streaming: bool = False
    engine: str = "parakeet"

    @property
    def url(self) -> str:
        return f"{HF_BASE}/{self.filename}" if self.engine == "parakeet" else ""

    @property
    def size_label(self) -> str:
        if not self.size:
            return "Built in"
        return f"{self.size / (1024 * 1024):.0f} MB"


MODELS = (
    ModelSpec(
        "nemotron-streaming-q5",
        "NVIDIA Nemotron 3.5 Streaming",
        "nemotron-3.5-asr-streaming-0.6b-q5_k.gguf",
        784_801_888,
        "Newest and best all-round choice. Native live transcription, punctuation, "
        "capitalization, and prompt-selected multilingual recognition.",
        "40+ language locales",
        True,
    ),
    ModelSpec(
        "eou-streaming-q5",
        "NVIDIA Parakeet Realtime EOU 120M",
        "realtime_eou_120m-v1-q5_k.gguf",
        141_151_648,
        "Very small, very low-latency English streaming model with real "
        "end-of-utterance detection.",
        "English",
        True,
    ),
    ModelSpec(
        "parakeet-v2-q5",
        "NVIDIA Parakeet TDT 0.6B V2",
        "tdt-0.6b-v2-q5_k.gguf",
        705_039_648,
        "High-accuracy English batch dictation. It waits for release before "
        "transcribing, but is an excellent accuracy fallback.",
        "English",
    ),
    ModelSpec(
        "parakeet-v3-q5",
        "NVIDIA Parakeet TDT 0.6B V3",
        "tdt-0.6b-v3-q5_k.gguf",
        741_867_360,
        "High-accuracy multilingual batch dictation across European languages.",
        "25 European languages",
    ),
    ModelSpec(
        "parakeet-fast-q5",
        "NVIDIA Parakeet TDT-CTC 110M",
        "tdt_ctc-110m-q5_k.gguf",
        143_290_496,
        "Small and fast English batch model for minimum memory use.",
        "English",
    ),
    ModelSpec(
        "whisper-turbo-q5",
        "Whisper Large V3 Turbo",
        "ggml-large-v3-turbo-q5_0.bin",
        0,
        "The original broad-language Purr engine and dependable compatibility fallback.",
        "Multilingual",
        engine="whisper",
    ),
)
MODEL_BY_ID = {model.id: model for model in MODELS}
DEFAULT_MODEL_ID = MODELS[0].id


class ModelStore:
    def __init__(self, build: Path):
        self.build = build
        self.models_dir = build / "models"

    def path(self, model: ModelSpec) -> Path:
        return self.models_dir / model.filename

    def installed(self, model: ModelSpec) -> bool:
        path = self.path(model)
        if not path.is_file():
            return False
        return model.size == 0 or path.stat().st_size == model.size

    def download(self, model: ModelSpec, progress=None) -> Path:
        if model.engine != "parakeet":
            raise RuntimeError("The built-in Whisper model is installed by the Linux build")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        destination = self.path(model)
        partial = destination.with_suffix(destination.suffix + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        request = urllib.request.Request(model.url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code == 416 and offset == model.size:
                partial.replace(destination)
                return destination
            raise
        # A server may ignore Range; do not append a full response to a partial file.
        append = offset > 0 and response.status == 206
        if not append:
            offset = 0
        mode = "ab" if append else "wb"
        downloaded = offset
        with response, partial.open(mode) as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(min(downloaded, model.size), model.size)
        actual = partial.stat().st_size
        if actual != model.size:
            raise RuntimeError(
                f"Incomplete model download: expected {model.size:,} bytes, received {actual:,}"
            )
        partial.replace(destination)
        return destination

    def remove(self, model: ModelSpec) -> None:
        if model.engine == "whisper":
            raise RuntimeError("Whisper is Purr's built-in fallback and cannot be removed here")
        self.path(model).unlink(missing_ok=True)
        self.path(model).with_suffix(self.path(model).suffix + ".part").unlink(missing_ok=True)


class WhisperEngine:
    streaming = False

    def __init__(self, config: dict, build: Path, model: Path, state: Path):
        self.config = config
        self.binary_dir = build / "whisper.cpp" / "build" / "bin"
        self.binary = self.binary_dir / "whisper-server"
        self.model = model
        self.state = state
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.log_handle = None
        self.using_gpu = True

    @property
    def backend_label(self) -> str:
        return "Whisper · Radeon/Vulkan" if self.using_gpu else "Whisper · CPU fallback"

    def start(self) -> None:
        if not self.binary.is_file() or not self.model.is_file():
            raise RuntimeError("Whisper is not built; run `make linux-install`")
        last_error = "unknown startup failure"
        for use_gpu in (True, False):
            try:
                self._launch(use_gpu)
                self.using_gpu = use_gpu
                return
            except Exception as error:
                last_error = str(error)
                LOG.exception("Whisper startup failed; trying fallback")
                self.stop()
        raise RuntimeError(f"Could not start Whisper: {last_error}")

    def _launch(self, use_gpu: bool) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(self.binary_dir) + ":" + env.get("LD_LIBRARY_PATH", "")
        command = [
            str(self.binary), "--model", str(self.model), "--host", "127.0.0.1",
            "--port", str(self.port), "--threads", "12", "--language",
            str(self.config.get("language", "en") or "auto"), "--no-timestamps", "--flash-attn",
        ]
        if not use_gpu:
            command.append("--no-gpu")
        self.state.mkdir(parents=True, exist_ok=True)
        self.log_handle = open(self.state / "whisper-server.log", "a", buffering=1)
        self.process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=self.log_handle, stderr=self.log_handle,
            env=env, start_new_session=True,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"whisper-server exited with status {self.process.returncode}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=1
                ) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.2)
        raise RuntimeError("Whisper model load timed out")

    def transcribe(self, path: Path, multipart) -> str:
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Whisper is not running")
        fields = {
            "response_format": "text", "temperature": "0.0", "temperature_inc": "0.2",
            "no_timestamps": "true", "language": str(self.config.get("language", "en") or "auto"),
        }
        prompt = str(self.config.get("initial_prompt", "")).strip()
        if prompt:
            fields["prompt"] = prompt
        body, content_type = multipart(path, fields)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference", data=body,
            headers={"Content-Type": content_type}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read().decode("utf-8", errors="replace").strip()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


class ParakeetStream:
    def __init__(self, engine: "ParakeetEngine", handle: int):
        self.engine = engine
        self.handle = handle

    def _take_json(self, pointer: int) -> dict:
        if not pointer:
            raise RuntimeError(self.engine.last_error())
        try:
            return json.loads(ctypes.string_at(pointer).decode("utf-8"))
        finally:
            self.engine.lib.parakeet_capi_free_string(pointer)

    def feed_s16(self, pcm: bytes) -> dict:
        count = len(pcm) // 2
        source = (ctypes.c_int16 * count).from_buffer_copy(pcm[: count * 2])
        floats = (ctypes.c_float * count)(*(sample / 32768.0 for sample in source))
        return self._take_json(
            self.engine.lib.parakeet_capi_stream_feed_json(self.handle, floats, count)
        )

    def finalize(self) -> dict:
        return self._take_json(self.engine.lib.parakeet_capi_stream_finalize_json(self.handle))

    def close(self) -> None:
        if self.handle:
            self.engine.lib.parakeet_capi_stream_free(self.handle)
            self.handle = 0


class ParakeetEngine:
    def __init__(self, config: dict, build: Path, model: Path, streaming: bool):
        self.config = config
        self.build_dir = build / "parakeet.cpp" / "build"
        self.model = model
        self.streaming = streaming
        self.ctx = 0
        self.lib = None

    @property
    def backend_label(self) -> str:
        mode = "live" if self.streaming else "batch"
        return f"NVIDIA Parakeet · {mode} · Radeon/Vulkan"

    def start(self) -> None:
        library = self.build_dir / "libparakeet.so"
        if not library.is_file() or not self.model.is_file():
            raise RuntimeError("NVIDIA engine/model is not installed; run `make linux-install`")
        self.lib = ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
        self.lib.parakeet_capi_load.argtypes = [ctypes.c_char_p]
        self.lib.parakeet_capi_load.restype = ctypes.c_void_p
        self.lib.parakeet_capi_free.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_last_error.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_last_error.restype = ctypes.c_char_p
        self.lib.parakeet_capi_transcribe_path_lang.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ]
        self.lib.parakeet_capi_transcribe_path_lang.restype = ctypes.c_void_p
        self.lib.parakeet_capi_stream_begin_lang.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.parakeet_capi_stream_begin_lang.restype = ctypes.c_void_p
        self.lib.parakeet_capi_stream_feed_json.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int,
        ]
        self.lib.parakeet_capi_stream_feed_json.restype = ctypes.c_void_p
        self.lib.parakeet_capi_stream_finalize_json.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_stream_finalize_json.restype = ctypes.c_void_p
        self.lib.parakeet_capi_stream_free.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_free_string.argtypes = [ctypes.c_void_p]
        self.ctx = self.lib.parakeet_capi_load(os.fsencode(self.model))
        if not self.ctx:
            raise RuntimeError("parakeet.cpp could not load the selected model")

    def last_error(self) -> str:
        if not self.ctx or not self.lib:
            return "NVIDIA transcription engine error"
        error = self.lib.parakeet_capi_last_error(self.ctx)
        return error.decode("utf-8", errors="replace") if error else "NVIDIA transcription engine error"

    def transcribe(self, path: Path, _multipart=None) -> str:
        language = str(self.config.get("language", "en") or "auto").encode()
        pointer = self.lib.parakeet_capi_transcribe_path_lang(
            self.ctx, os.fsencode(path), 0, language
        )
        if not pointer:
            raise RuntimeError(self.last_error())
        try:
            return ctypes.string_at(pointer).decode("utf-8", errors="replace").strip()
        finally:
            self.lib.parakeet_capi_free_string(pointer)

    def begin_stream(self) -> ParakeetStream:
        if not self.streaming:
            raise RuntimeError("The selected model is not a streaming model")
        language = str(self.config.get("language", "en") or "auto").encode()
        handle = self.lib.parakeet_capi_stream_begin_lang(self.ctx, language)
        if not handle:
            raise RuntimeError(self.last_error())
        return ParakeetStream(self, handle)

    def stop(self) -> None:
        if self.ctx and self.lib:
            self.lib.parakeet_capi_free(self.ctx)
        self.ctx = 0


def create_engine(config: dict, build: Path, state: Path, store: ModelStore):
    model = MODEL_BY_ID.get(config.get("model"), MODEL_BY_ID[DEFAULT_MODEL_ID])
    if not store.installed(model):
        fallback = MODEL_BY_ID["whisper-turbo-q5"]
        if not store.installed(fallback):
            raise RuntimeError(f"Selected model is not downloaded: {model.name}")
        LOG.warning("Selected model %s is missing; using Whisper", model.id)
        model = fallback
    if model.engine == "whisper":
        return WhisperEngine(config, build, store.path(model), state), model
    return ParakeetEngine(config, build, store.path(model), model.streaming), model

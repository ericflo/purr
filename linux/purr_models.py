"""Local live ASR engines and the curated Linux model catalog for Purr."""

from __future__ import annotations

import array
import base64
import ctypes
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request


LOG = logging.getLogger("purr")
# Downloads are pinned to repository revisions and verified against the
# SHA-256 that Hugging Face publishes for each file.
PARAKEET_GGUF = (
    "https://huggingface.co/mudler/parakeet-cpp-gguf/resolve/"
    "bf0af9f425fa01809cadec671b3cb672709d13e9"
)
VOXTRAL_GGUF = (
    "https://huggingface.co/ggml-org/Voxtral-Mini-4B-Realtime-2602-GGUF/resolve/"
    "0b0f7ac97a2bc290053ec2339b70227c59ae6386"
)


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str
    source: str

    @property
    def url(self) -> str:
        return f"{self.source}/{self.name}"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    files: tuple[ModelFile, ...]
    summary: str
    languages: str
    # "parakeet" runs in-process through parakeet.cpp; "llama" through llama-server.
    engine: str = "parakeet"
    # Renew the live stream at pauses; see LiveStream.
    renew_at_pauses: bool = False
    # Whether the model writes punctuation and capitals itself.
    punctuates: bool = True
    # Whether the recognition-language setting steers the model.
    uses_language: bool = False

    @property
    def size(self) -> int:
        return sum(file.size for file in self.files)

    @property
    def size_label(self) -> str:
        megabytes = self.size / (1024 * 1024)
        return f"{megabytes / 1024:.1f} GB" if megabytes >= 1024 else f"{megabytes:.0f} MB"


# Every model types live while you speak; batch models are not offered.
MODELS = (
    ModelSpec(
        "nemotron-streaming-q5",
        "NVIDIA Nemotron 3.5 Streaming",
        (ModelFile(
            "nemotron-3.5-asr-streaming-0.6b-q5_k.gguf", 784_801_888,
            "7bb14605a707f821560952521034002c91653d676272a80dca0cd2aa886d0ab2", PARAKEET_GGUF,
        ),),
        "The default: fast, light, and accurate. Punctuates and capitalizes as you "
        "speak, in the recognition language set below.",
        "40+ language locales",
        renew_at_pauses=True,
        uses_language=True,
    ),
    ModelSpec(
        "voxtral-realtime-q4",
        "Mistral Voxtral Mini 4B Realtime",
        (
            ModelFile(
                "Voxtral-Mini-4B-Realtime-2602-Q4_K_M.gguf", 2_551_244_000,
                "aa7a91fd78a896562642c61950887ec18c999425ab064bfc5ca9e64f8f5b29be", VOXTRAL_GGUF,
            ),
            ModelFile(
                "mmproj-Voxtral-Mini-4B-Realtime-2602-Q8_0.gguf", 1_063_756_032,
                "f28ae3e40f2b610c1c39b5c936ae854b73d0fc487518c6f69b76c22dbcea9ee3", VOXTRAL_GGUF,
            ),
        ),
        "The largest live model, punctuated, about half a second behind your voice, "
        "and it detects the language itself. Needs a capable GPU with about 6 GB "
        "of free memory; runs through llama.cpp.",
        "13 languages",
        engine="llama",
        renew_at_pauses=True,
    ),
    ModelSpec(
        "eou-streaming-q5",
        "NVIDIA Parakeet Realtime EOU 120M",
        (ModelFile(
            "realtime_eou_120m-v1-q5_k.gguf", 141_151_648,
            "e6e83c13a41751d6130cde836690304cbc143347bdf12577e419018df2235398", PARAKEET_GGUF,
        ),),
        "Tiny and very fast, for modest GPUs. Writes lowercase without punctuation; "
        "Purr capitalizes “I”.",
        "English",
        punctuates=False,
    ),
)
MODEL_BY_ID = {model.id: model for model in MODELS}
DEFAULT_MODEL_ID = MODELS[0].id


class ModelStore:
    def __init__(self, build: Path):
        self.build = build
        self.models_dir = build / "models"

    def path(self, model: ModelSpec, index: int = 0) -> Path:
        return self.models_dir / model.files[index].name

    def _complete(self, file: ModelFile) -> bool:
        path = self.models_dir / file.name
        return path.is_file() and path.stat().st_size == file.size

    def installed(self, model: ModelSpec) -> bool:
        return all(self._complete(file) for file in model.files)

    def download(self, model: ModelSpec, progress=None) -> None:
        """Fetch every missing file of a model, reporting progress over all of them."""
        self.models_dir.mkdir(parents=True, exist_ok=True)
        before = 0
        for file in model.files:
            if not self._complete(file):
                report = progress and (
                    lambda done, offset=before: progress(offset + done, model.size)
                )
                self._download_file(file, report)
            before += file.size
            if progress:
                progress(before, model.size)

    def _download_file(self, file: ModelFile, progress=None) -> None:
        destination = self.models_dir / file.name
        partial = destination.with_suffix(destination.suffix + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        request = urllib.request.Request(file.url, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code == 416 and offset == file.size:
                partial.replace(destination)
                return
            raise
        # A server may ignore Range; do not append a full response to a partial file.
        append = offset > 0 and response.status == 206
        if not append:
            offset = 0
        downloaded = offset
        with response, partial.open("ab" if append else "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(min(downloaded, file.size))
        actual = partial.stat().st_size
        if actual != file.size:
            raise RuntimeError(
                f"Incomplete model download: expected {file.size:,} bytes, received {actual:,}"
            )
        digest = hashlib.sha256()
        with partial.open("rb") as downloaded_file:
            while chunk := downloaded_file.read(8 * 1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != file.sha256:
            # Not resumable: the bytes are wrong, not missing.
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"{file.name} failed its checksum; download it again")
        partial.replace(destination)

    def remove(self, model: ModelSpec) -> None:
        for file in model.files:
            path = self.models_dir / file.name
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)


class LiveStream:
    """One dictation's stream, renewed at pauses when the engine needs it.

    Renewal finishes the engine's stream (which closes its sentence) and starts
    a fresh one at a natural pause; the text stays one continuous transcript for
    the caller. Subclasses provide _feed, _finish, and _restart.
    """

    RENEW_AFTER = 8.0  # seconds of stream before a pause may renew it
    RENEW_PAUSE = 1.0  # seconds without new text that make a pause
    # Renew even without a pause after this long (None: only at pauses).
    RENEW_LIMIT: float | None = None
    # A cut needs the audio itself to be quiet too: text lags speech, so a
    # textless second can end just as the next word begins.
    QUIET_RATIO = 0.12  # chunk RMS relative to the recent speech level
    QUIET_CHUNKS = 2
    LOCALE_TAG = re.compile(r"<[a-z]{2}(?:-[a-z]{2})?>", re.IGNORECASE)
    SENTENCE_END = re.compile(r"[.?!…:\n]$")
    # A capitalized ordinary word; "I", "I'm", acronyms, and CamelCase never match.
    CONTINUED_WORD = re.compile(r"^(\s*)([A-Z])([a-z][a-z']*)\b")

    def __init__(self, renew_at_pauses: bool = False):
        self.renew_at_pauses = renew_at_pauses
        self.renewals = 0
        self.emitted = ""  # recent output, to join renewed text cleanly
        self.age = self.quiet = self.level = 0.0
        self.quiet_chunks = 0
        self.heard = self.renewed = False

    def _feed(self, pcm: bytes, count: int) -> dict:
        raise NotImplementedError

    def _finish(self) -> dict:
        raise NotImplementedError

    def _restart(self) -> None:
        raise NotImplementedError

    def feed_s16(self, pcm: bytes) -> dict:
        count = len(pcm) // 2
        result = self._feed(pcm[: count * 2], count)
        if not self.renew_at_pauses or not count:
            return result
        text = result.get("text", "")
        if self.renewed and text.strip():
            text = self._continue(text)
            self.renewed = False
        samples = array.array("h", pcm[: count * 2])
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(sample * sample for sample in samples) / count)
        self.level = max(rms, self.level * 0.995)
        self.quiet_chunks = self.quiet_chunks + 1 if rms < self.level * self.QUIET_RATIO else 0
        self.age += count / 16000
        if text.strip():
            self.quiet, self.heard = 0.0, True
        else:
            self.quiet += count / 16000
        paused = (
            self.age >= self.RENEW_AFTER and self.quiet >= self.RENEW_PAUSE
            and self.quiet_chunks >= self.QUIET_CHUNKS
        )
        if self.heard and (paused or (self.RENEW_LIMIT and self.age >= self.RENEW_LIMIT)):
            text += self._renew()
        self.emitted = (self.emitted + text)[-64:]
        result["text"] = text
        return result

    def _renew(self) -> str:
        closing = self._finish().get("text", "")
        self._restart()
        self.renewals += 1
        self.age = self.quiet = 0.0
        self.heard, self.renewed = False, True
        return closing

    def _continue(self, text: str) -> str:
        """Join a renewed stream's first words onto the transcript so far."""
        if self.emitted and not self.emitted[-1].isspace() and not text[0].isspace():
            text = " " + text
        before = self.LOCALE_TAG.sub("", self.emitted).rstrip()
        if before and not self.SENTENCE_END.search(before):
            # A fresh stream capitalizes its first word even mid-sentence.
            text = self.CONTINUED_WORD.sub(
                lambda m: m[1] + m[2].lower() + m[3], text, count=1
            )
        return text

    def finalize(self) -> dict:
        result = self._finish()
        if self.renewed and result.get("text", "").strip():
            result["text"] = self._continue(result["text"])
        return result

    def close(self) -> None:
        pass


class ParakeetStream(LiveStream):
    """A cache-aware parakeet.cpp stream.

    Nemotron stops punctuating over a long cache-aware stream: on recorded
    speech a single stream fell to about 3 marks per 100 words within minutes,
    while streams renewed at pauses kept 15.
    """

    def __init__(self, engine: "ParakeetEngine", handle: int, renew_at_pauses: bool = False):
        super().__init__(renew_at_pauses)
        self.engine = engine
        self.handle = handle

    def _take_json(self, pointer: int) -> dict:
        if not pointer:
            raise RuntimeError(self.engine.last_error())
        try:
            return json.loads(ctypes.string_at(pointer).decode("utf-8"))
        finally:
            self.engine.lib.parakeet_capi_free_string(pointer)

    def _feed(self, pcm: bytes, count: int) -> dict:
        source = (ctypes.c_int16 * count).from_buffer_copy(pcm)
        floats = (ctypes.c_float * count)(*(sample / 32768.0 for sample in source))
        return self._take_json(
            self.engine.lib.parakeet_capi_stream_feed_json(self.handle, floats, count)
        )

    def _finish(self) -> dict:
        return self._take_json(self.engine.lib.parakeet_capi_stream_finalize_json(self.handle))

    def _restart(self) -> None:
        handle = self.engine.begin_handle()
        self.engine.lib.parakeet_capi_stream_free(self.handle)
        self.handle = handle

    def close(self) -> None:
        if self.handle:
            self.engine.lib.parakeet_capi_stream_free(self.handle)
            self.handle = 0


class ParakeetEngine:
    def __init__(self, config: dict, build: Path, model: Path, renew_at_pauses: bool = False):
        self.config = config
        self.build_dir = build / "parakeet.cpp" / "build"
        self.model = model
        self.renew_at_pauses = renew_at_pauses
        self.ctx = 0
        self.lib = None

    backend_label = "Vulkan · parakeet.cpp"

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

    def begin_stream(self) -> ParakeetStream:
        return ParakeetStream(self, self.begin_handle(), self.renew_at_pauses)

    def begin_handle(self) -> int:
        language = str(self.config.get("language", "en") or "auto").encode()
        handle = self.lib.parakeet_capi_stream_begin_lang(self.ctx, language)
        if not handle:
            raise RuntimeError(self.last_error())
        return handle

    def stop(self) -> None:
        if self.ctx and self.lib:
            self.lib.parakeet_capi_free(self.ctx)
        self.ctx = 0


class WebSocket:
    """Just enough RFC 6455 for llama-server on localhost: JSON text frames,
    masked writes, and ping replies."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, port: int, path: str, timeout: float = 10.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.closed = False
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("The transcription server closed the connection")
            response += chunk
        head, self.buffer = response.split(b"\r\n\r\n", 1)
        if not head.startswith((b"HTTP/1.1 101", b"HTTP/1.0 101")):
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode(errors="replace"))
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest())
        if accept not in head:
            raise ConnectionError("The transcription server sent a bad websocket handshake")
        self.sock.settimeout(None)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        keystream = (mask * (size // 4 + 1))[:size]
        masked = (int.from_bytes(payload, "big") ^ int.from_bytes(keystream, "big")).to_bytes(size, "big")
        self.sock.sendall(bytes(header) + mask + masked)

    def send(self, message: dict) -> None:
        self._send_frame(0x1, json.dumps(message).encode())

    def _read(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                self.closed = True
                raise ConnectionError("The transcription server closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def receive(self, timeout: float) -> dict | None:
        """The next JSON message, or None if none starts within timeout or the server closed."""
        message = b""
        deadline = time.monotonic() + timeout
        while True:
            if not self.buffer and not message:
                wait = max(0.0, deadline - time.monotonic())
                if not select.select([self.sock], [], [], wait)[0]:
                    return None
            # A frame that has started arrives whole from localhost; read all of it.
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else b""
            payload = self._read(size)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 0x9:
                self._send_frame(0xA, payload)
            elif opcode == 0x8:
                self.closed = True
                return None
            elif opcode in (0x0, 0x1):
                message += payload
                if first & 0x80:
                    return json.loads(message)

    def close(self) -> None:
        if not self.closed:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
        self.closed = True
        self.sock.close()


class RealtimeStream(LiveStream):
    """A llama-server /v1/realtime transcription session (OpenAI realtime events).

    Each 80 ms of audio takes one position of the session's context, and a
    session past its context silently stops transcribing (measured: a 1024
    context went quiet about 85 s in, without an error). Long dictation
    therefore moves to a fresh session at a pause after three minutes, and
    after seven even without one, well inside LlamaRealtimeEngine.CONTEXT.
    """

    RENEW_AFTER = 180.0
    RENEW_LIMIT = 420.0
    DRAIN_SECONDS = 60.0  # how long finishing may wait for the model to catch up

    def __init__(self, port: int, renew_at_pauses: bool = True):
        super().__init__(renew_at_pauses)
        self.port = port
        self.ws: WebSocket | None = None
        self._open()

    def _open(self) -> None:
        self.ws = WebSocket(self.port, "/v1/realtime?intent=transcription")
        created = self.ws.receive(10)
        if not created or created.get("type") != "session.created":
            raise RuntimeError("The transcription server did not open a realtime session")
        rate = created.get("session", {}).get("audio", {}).get("input", {}).get("format", {}).get("rate")
        if rate != 16000:
            raise RuntimeError(f"The model expects {rate} Hz audio; Purr records 16000 Hz")

    @staticmethod
    def _text(event: dict) -> str:
        kind = event.get("type")
        if kind == "error":
            raise RuntimeError(event.get("error", {}).get("message") or "Realtime transcription failed")
        return event.get("delta", "") if kind == "conversation.item.input_audio_transcription.delta" else ""

    def _feed(self, pcm: bytes, count: int) -> dict:
        self.ws.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})
        text = ""
        while (event := self.ws.receive(0)) is not None:
            text += self._text(event)
        return {"text": text}

    def _finish(self) -> dict:
        """End the input and wait for the words the model still holds back."""
        self.ws.send({"type": "input_audio_buffer.commit", "final": True})
        text = ""
        deadline = time.monotonic() + self.DRAIN_SECONDS
        while True:
            event = self.ws.receive(max(0.0, deadline - time.monotonic()))
            if event is None:
                if self.ws.closed:
                    return {"text": text}
                raise RuntimeError("The transcription model fell behind and did not finish")
            text += self._text(event)
            if event.get("type") == "conversation.item.input_audio_transcription.completed":
                return {"text": text}

    def _restart(self) -> None:
        self.ws.close()
        self._open()

    def close(self) -> None:
        if self.ws:
            self.ws.close()


class LlamaRealtimeEngine:
    """Voxtral Realtime served by llama-server on an ephemeral localhost port."""

    backend_label = "Vulkan · llama.cpp"
    # About eleven minutes of audio; RealtimeStream renews well before. Each
    # doubling costs about 850 MB of GPU memory.
    CONTEXT = 8192

    def __init__(self, build: Path, model: Path, projector: Path, state: Path):
        self.binary = build / "llama.cpp" / "build" / "bin" / "llama-server"
        self.model, self.projector, self.state = model, projector, state
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.log_handle = None

    @property
    def pid_file(self) -> Path:
        return self.state / "llama-server.pid"

    def reap_stale(self) -> None:
        """Stop a server left running by a Purr that was killed: it holds GBs of GPU memory."""
        try:
            pid = int(self.pid_file.read_text())
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]
        except (OSError, ValueError):
            self.pid_file.unlink(missing_ok=True)
            return
        # Only ever signal our own llama-server, never a process that reused the PID.
        if command == os.fsencode(self.binary):
            LOG.warning("Stopping llama-server %d left running by an earlier Purr", pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.pid_file.unlink(missing_ok=True)

    def start(self) -> None:
        if not self.binary.is_file():
            raise RuntimeError("llama.cpp is not built; run `make linux-install`")
        if not self.model.is_file() or not self.projector.is_file():
            raise RuntimeError("Voxtral is not downloaded")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.state.mkdir(parents=True, exist_ok=True)
        self.reap_stale()
        self.log_handle = open(self.state / "llama-server.log", "a", buffering=1)
        self.process = subprocess.Popen(
            [
                str(self.binary), "--model", str(self.model), "--mmproj", str(self.projector),
                "--host", "127.0.0.1", "--port", str(self.port), "--no-webui",
                "--parallel", "1", "--ctx-size", str(self.CONTEXT),
            ],
            stdin=subprocess.DEVNULL, stdout=self.log_handle, stderr=self.log_handle,
            start_new_session=True,
        )
        self.pid_file.write_text(str(self.process.pid))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.stop()
                raise RuntimeError("llama-server could not load Voxtral; see llama-server.log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("Voxtral took too long to load")

    def begin_stream(self) -> RealtimeStream:
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("The Voxtral server is not running")
        return RealtimeStream(self.port)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process:
            self.pid_file.unlink(missing_ok=True)
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


def create_engine(config: dict, build: Path, state: Path, store: ModelStore):
    # Whatever is selected now, a Voxtral server orphaned by a killed Purr must go.
    LlamaRealtimeEngine(build, Path(), Path(), state).reap_stale()
    model = MODEL_BY_ID.get(config.get("model"), MODEL_BY_ID[DEFAULT_MODEL_ID])
    if not store.installed(model):
        fallback = MODEL_BY_ID[DEFAULT_MODEL_ID]
        if not store.installed(fallback):
            raise RuntimeError(f"Selected model is not downloaded: {model.name}")
        LOG.warning("Selected model %s is missing; using %s", model.id, fallback.id)
        model = fallback
    if model.engine == "llama":
        engine = LlamaRealtimeEngine(build, store.path(model, 0), store.path(model, 1), state)
    else:
        engine = ParakeetEngine(config, build, store.path(model), model.renew_at_pauses)
    return engine, model

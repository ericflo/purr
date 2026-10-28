#!/usr/bin/env python3
"""Small Linux/Wayland implementation of Purr's local dictation."""

from __future__ import annotations

import array
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from purr_models import (
    DEFAULT_MODEL_ID,
    MODEL_BY_ID,
    MODELS,
    ModelStore,
    create_engine,
)


APP_ID = "com.arunbrahma.purr"
ROOT = Path(os.environ.get("PURR_ROOT", Path(__file__).resolve().parent.parent))
BUILD = ROOT / ".build-linux"
WHISPER_BIN = BUILD / "whisper.cpp" / "build" / "bin"
WHISPER_SERVER = WHISPER_BIN / "whisper-server"
MODEL = BUILD / "models" / "ggml-large-v3-turbo-q5_0.bin"
PARAKEET_LIB = BUILD / "parakeet.cpp" / "build" / "libparakeet.so"
NEMOTRON_MODEL = BUILD / "models" / "nemotron-3.5-asr-streaming-0.6b-q5_k.gguf"
ICON = ROOT / "Resources" / "purr_app_logo.png"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "purr"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "purr"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "purr/config.json"
# Peak level below which a recording counts as silence (a muted or dead microphone).
SILENCE_DB = -55.0
# How long Purr listens to unbroken silence before warning that it hears nothing.
SILENCE_WARNING_SECONDS = 3.0


def setup_logging() -> logging.Logger:
    STATE.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("purr")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(STATE / "purr.log", maxBytes=2_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler())
    return logger


LOG = setup_logging()


DEFAULT_CONFIG = {
    "language": "en",
    "model": DEFAULT_MODEL_ID,
    "show_overlay": False,
    "microphone": "default",
    "trim_fillers": True,
    "voice_commands": True,
    "dictionary": {},
    "initial_prompt": "",
    "log_transcripts": False,
}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_FILE.read_text())
        if isinstance(loaded, dict):
            config.update(loaded)
    except FileNotFoundError:
        pass
    except Exception:
        LOG.exception("Ignoring invalid config at %s", CONFIG_FILE)
    # Dictation always types live now; drop the retired clipboard-mode toggle.
    config.pop("live_typing", None)
    return config


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    temporary.replace(CONFIG_FILE)


def list_microphones() -> list[tuple[str, str]]:
    """Return PipeWire/Pulse microphone node names and friendly labels."""
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        sources = json.loads(result.stdout)
        return [
            (str(source["name"]), str(source.get("description") or source["name"]))
            for source in sources
            if not str(source.get("name", "")).endswith(".monitor")
            and source.get("properties", {}).get("device.class") != "monitor"
        ]
    except Exception:
        LOG.exception("Could not enumerate microphones")
        return []


class PostProcessor:
    """Deterministic cleanup matching the useful macOS dictation passes."""

    FILLER = re.compile(
        r"\b(?:u+h+[\s-]*h+u+h+|u+h+m+|u+m+|u+h+|e+r+m+|e+r+|a+h+e+m+|"
        r"a+h+|m+m+[\s-]*h+m*|m+h+m+|h+m+|m+m+m+)\b[,.!?]?\s*",
        re.IGNORECASE,
    )
    COMMANDS = [
        (r"\bnew paragraph\b\.?", "\n\n"),
        (r"\bnew line\b\.?", "\n"),
        (r"\btab key\b\.?", "\t"),
        (r"\bquestion mark\b\.?", "?"),
        (r"\bexclamation (?:point|mark)\b\.?", "!"),
        (r"\b(?:full stop|period)\b\.?", "."),
        (r"\bcomma\b\.?", ","),
        (r"\bsemicolon\b\.?", ";"),
        (r"\bcolon\b\.?", ":"),
        (r"\bellipsis\b\.?", "…"),
        (r"\bem dash\b\.?", " — "),
        (r"\ben dash\b\.?", " – "),
        (r"\bdash\b\.?", " - "),
        (r"\bhyphen\b\.?", "-"),
        (r"\bopen quote\b\.?", ' "'),
        (r"\bclose quote\b\.?", '"'),
        (r"\bopen paren(?:thesis)?\b\.?", " ("),
        (r"\bclose paren(?:thesis)?\b\.?", ")"),
        (r"\bopen bracket\b\.?", " ["),
        (r"\bclose bracket\b\.?", "]"),
        (r"\bopen brace\b\.?", " {"),
        (r"\bclose brace\b\.?", "}"),
        (r"\bampersand\b\.?", "&"),
        (r"\basterisk\b\.?", "*"),
        (r"\bat sign\b\.?", "@"),
        (r"\b(?:forward )?slash\b\.?", "/"),
        (r"\bbackslash\b\.?", "\\"),
        (r"\bhashtag\b\.?", "#"),
        (r"\bpercent sign\b\.?", "%"),
        (r"\bunderscore\b\.?", "_"),
        (r"\bplus sign\b\.?", "+"),
        (r"\bequals sign\b\.?", "="),
    ]

    def __init__(self, config: dict):
        self.config = config

    @staticmethod
    def _scratch(text: str) -> tuple[str, int]:
        """Apply every inline scratch command and count edits before this utterance."""
        command = re.compile(r"\b(?:scratch|delete)\s+that\b[,.!?]*", re.IGNORECASE)
        drop_previous = 0
        while True:
            match = command.search(text)
            if not match:
                break
            before = text[: match.start()].rstrip()
            after = text[match.end() :].lstrip()
            if before:
                before = re.sub(r"\S+\s*$", "", before).rstrip()
            else:
                drop_previous += 1
            text = (before + (" " if before and after else "") + after).strip()
        return text, drop_previous

    def apply(self, raw: str) -> tuple[str, int]:
        text = raw.strip()
        drop_previous = 0
        # Nemotron uses locale prompt tokens internally and can surface one at
        # an utterance boundary. They are metadata, never dictated text.
        text = re.sub(r"<[a-z]{2}(?:-[a-z]{2})?>", " ", text, flags=re.IGNORECASE)
        if self.config.get("trim_fillers", True):
            text = self.FILLER.sub("", text)
        if self.config.get("voice_commands", True):
            text, drop_previous = self._scratch(text)
            for pattern, replacement in self.COMMANDS:
                text = re.sub(pattern, lambda _match, r=replacement: r, text, flags=re.IGNORECASE)
        dictionary = self.config.get("dictionary", {})
        if isinstance(dictionary, dict):
            for source, replacement in dictionary.items():
                if source:
                    text = re.sub(
                        rf"\b{re.escape(str(source))}\b",
                        lambda _match, r=str(replacement): r,
                        text,
                        flags=re.IGNORECASE,
                    )
        # The virtual keyboard is the only insertion path, and control
        # characters have no key or Unicode-compose form.
        if not text.replace("\n", " ").replace("\t", " ").isprintable():
            text = "".join(char for char in text if char.isprintable() or char in "\n\t")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"[ \t]+([,.?!…])", r"\1", text)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        return text.strip(" \t"), drop_previous


def transcript_preview(text: str, limit: int = 280) -> str:
    """Show only the tail so minutes of dictation cannot grow wrapped labels."""
    return text if len(text) <= limit else "…" + text[-limit:].lstrip()


def shortcut_label(binding: str) -> str:
    """Turn a GSettings accelerator such as <Control><Alt>space into Ctrl+Alt+Space."""
    key, modifiers = Gtk.accelerator_parse(binding)
    return Gtk.accelerator_get_label(key, modifiers) if key else binding


def normalize_wav(path: Path) -> tuple[float, float]:
    """Remove DC and boost quiet speech toward -3 dBFS in place."""
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        if params.sampwidth != 2 or params.nchannels != 1:
            raise RuntimeError("PipeWire returned an unexpected audio format")
        pcm = array.array("h", source.readframes(params.nframes))
    if not pcm:
        return -120.0, -120.0
    mean = sum(pcm) / len(pcm)
    centered = [max(-32768, min(32767, round(sample - mean))) for sample in pcm]
    peak = max(abs(sample) for sample in centered)
    rms = math.sqrt(sum(sample * sample for sample in centered) / len(centered))
    peak_db = 20 * math.log10(peak / 32768) if peak else -120.0
    rms_db = 20 * math.log10(rms / 32768) if rms else -120.0
    if -60.0 < peak_db < -3.0:
        gain = (32768 * (10 ** (-3.0 / 20))) / peak
        centered = [max(-32768, min(32767, round(sample * gain))) for sample in centered]
    output = array.array("h", centered)
    with wave.open(str(path), "wb") as target:
        target.setparams(params)
        target.writeframes(output.tobytes())
    return peak_db, rms_db


def multipart_body(path: Path, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----purr-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            b"Content-Type: audio/wav\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class UInputKeyboard:
    """Wayland-safe virtual keyboard for direct live typing."""

    UI_SET_EVBIT = 0x40045564
    UI_SET_KEYBIT = 0x40045565
    UI_DEV_SETUP = 0x405C5503
    UI_DEV_CREATE = 0x5501
    UI_DEV_DESTROY = 0x5502
    EV_SYN = 0
    EV_KEY = 1
    SYN_REPORT = 0
    KEY_LEFTCTRL = 29
    KEY_LEFTSHIFT = 42
    KEY_BACKSPACE = 14
    KEY_ENTER = 28
    KEY_TAB = 15
    KEY_SPACE = 57

    LETTERS = dict(zip("qwertyuiopasdfghjklzxcvbnm", (
        16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
        30, 31, 32, 33, 34, 35, 36, 37, 38,
        44, 45, 46, 47, 48, 49, 50,
    )))
    DIGITS = dict(zip("1234567890", range(2, 12)))
    PUNCTUATION = {
        "-": (12, False), "_": (12, True), "=": (13, False), "+": (13, True),
        "[": (26, False), "{": (26, True), "]": (27, False), "}": (27, True),
        ";": (39, False), ":": (39, True), "'": (40, False), '"': (40, True),
        "`": (41, False), "~": (41, True), "\\": (43, False), "|": (43, True),
        ",": (51, False), "<": (51, True), ".": (52, False), ">": (52, True),
        "/": (53, False), "?": (53, True), "!": (2, True), "@": (3, True),
        "#": (4, True), "$": (5, True), "%": (6, True), "^": (7, True),
        "&": (8, True), "*": (9, True), "(": (10, True), ")": (11, True),
    }

    def __init__(self):
        self.device = open("/dev/uinput", "wb", buffering=0)
        self.lock = threading.Lock()
        fcntl.ioctl(self.device, self.UI_SET_EVBIT, self.EV_KEY)
        keys = {
            self.KEY_LEFTCTRL, self.KEY_LEFTSHIFT, self.KEY_BACKSPACE,
            self.KEY_ENTER, self.KEY_TAB, self.KEY_SPACE,
            *self.LETTERS.values(), *self.DIGITS.values(),
            *(key for key, _shift in self.PUNCTUATION.values()),
        }
        for key in keys:
            fcntl.ioctl(self.device, self.UI_SET_KEYBIT, key)
        setup = struct.pack("HHHH80sI", 0x03, 0x1, 0x1, 1, b"Purr virtual keyboard", 0)
        fcntl.ioctl(self.device, self.UI_DEV_SETUP, setup)
        fcntl.ioctl(self.device, self.UI_DEV_CREATE)
        time.sleep(0.15)

    def _event(self, event_type: int, code: int, value: int) -> None:
        self.device.write(struct.pack("llHHi", 0, 0, event_type, code, value))

    def _key(self, code: int, down: bool) -> None:
        self._event(self.EV_KEY, code, 1 if down else 0)
        self._event(self.EV_SYN, self.SYN_REPORT, 0)

    def _tap(self, code: int, shift: bool = False) -> None:
        if shift:
            self._key(self.KEY_LEFTSHIFT, True)
        self._key(code, True)
        self._key(code, False)
        if shift:
            self._key(self.KEY_LEFTSHIFT, False)

    def backspace(self, count: int) -> None:
        with self.lock:
            for _ in range(max(0, count)):
                self._tap(self.KEY_BACKSPACE)
                time.sleep(0.001)

    def backspace_word(self, count: int = 1) -> None:
        with self.lock:
            for _ in range(max(0, count)):
                self._key(self.KEY_LEFTCTRL, True)
                self._tap(self.KEY_BACKSPACE)
                self._key(self.KEY_LEFTCTRL, False)

    def can_type(self, text: str) -> bool:
        return all(
            char.lower() in self.LETTERS
            or char in self.DIGITS
            or char in self.PUNCTUATION
            or char in " \n\t\r"
            or char.isprintable()
            for char in text
        )

    def _unicode(self, char: str) -> None:
        # GTK/IBus's standard Linux Unicode entry: Ctrl+Shift+U, codepoint,
        # Enter. This keeps multilingual dictation independent of the active
        # keyboard layout and avoids background Wayland clipboard restrictions.
        self._key(self.KEY_LEFTCTRL, True)
        self._key(self.KEY_LEFTSHIFT, True)
        self._tap(self.LETTERS["u"])
        self._key(self.KEY_LEFTSHIFT, False)
        self._key(self.KEY_LEFTCTRL, False)
        for digit in f"{ord(char):x}":
            self._tap(self.DIGITS[digit] if digit.isdigit() else self.LETTERS[digit])
        self._tap(self.KEY_ENTER)

    def type_text(self, text: str) -> bool:
        if not self.can_type(text):
            return False
        with self.lock:
            for char in text:
                lower = char.lower()
                if lower in self.LETTERS:
                    self._tap(self.LETTERS[lower], char.isupper())
                elif char in self.DIGITS:
                    self._tap(self.DIGITS[char])
                elif char in self.PUNCTUATION:
                    self._tap(*self.PUNCTUATION[char])
                elif char == " ":
                    self._tap(self.KEY_SPACE)
                elif char == "\t":
                    self._tap(self.KEY_TAB)
                elif char in "\n\r":
                    self._tap(self.KEY_ENTER)
                else:
                    self._unicode(char)
                time.sleep(0.001)
        return True

    def close(self) -> None:
        if self.device and not self.device.closed:
            try:
                fcntl.ioctl(self.device, self.UI_DEV_DESTROY)
            finally:
                self.device.close()


class GnomeShortcut:
    """A native GNOME custom shortcut that emits one toggle action per press."""

    PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/purr/"
    SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

    def __init__(self, on_binding, on_status):
        self.on_binding = on_binding
        self.on_status = on_status
        self.binding = "<Control><Alt>space"
        self.shortcut: Gio.Settings | None = None

    def start(self) -> None:
        settings = Gio.Settings.new(self.SCHEMA)
        paths = list(settings.get_strv("custom-keybindings"))
        shortcut = Gio.Settings.new_with_path(self.CUSTOM_SCHEMA, self.PATH)
        if self.PATH not in paths:
            shortcut.set_string("name", "Purr Dictation")
            shortcut.set_string("binding", self.binding)
            settings.set_strv("custom-keybindings", [*paths, self.PATH])
        else:
            existing = shortcut.get_string("binding")
            # Migrate F8, which was briefly used while diagnosing the actual
            # focus-stealing bug. Preserve any other user-selected binding.
            if existing in ("", "F8"):
                shortcut.set_string("binding", self.binding)
            else:
                self.binding = existing
        shortcut.set_string("command", f"gapplication action {APP_ID} dictate")
        Gio.Settings.sync()
        # Follow changes made in GNOME Settings while Purr is running. GSettings
        # only emits changed signals for keys that have been read.
        self.shortcut = shortcut
        shortcut.connect("changed::binding", self._binding_changed)
        shortcut.get_string("binding")
        self.on_binding(self.binding)

    def _binding_changed(self, settings, _key) -> None:
        self.binding = settings.get_string("binding")
        self.on_binding(self.binding)

    def configure(self) -> None:
        try:
            subprocess.Popen(
                ["gnome-control-center", "keyboard"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as error:
            self.on_status(f"Could not open GNOME keyboard settings: {error}", True)

    def close(self) -> None:
        pass


class PurrApplication(Gtk.Application):
    def __init__(self, background: bool):
        super().__init__(application_id=APP_ID, flags=0)
        self.background = background
        self.config = load_config()
        self.processor = PostProcessor(self.config)
        self.store = ModelStore(BUILD)
        self.server, self.model = create_engine(self.config, BUILD, STATE, self.store)
        self.keyboard: UInputKeyboard | None = None
        self.shortcuts: GnomeShortcut | None = None
        self.recorder: subprocess.Popen | None = None
        self.recording_path: Path | None = None
        self.stream_thread: threading.Thread | None = None
        self.ready = False
        self.busy = False
        self.downloading = False
        self.engine_error: str | None = None
        self.shutting_down = False
        self.shortcut_description: str | None = None
        self.status_text = "Starting transcription engine…"
        self.status_error = False
        self.stream_typed = ""
        self.stream_raw = ""
        self.stream_external_drops = 0
        self.heard_sound = self.silence_warned = False
        self.shortcut_down = False
        self.shortcut_guard = 0
        self.shortcut_pressed_at = 0.0
        self.window = self.overlay = None
        self.status_label = self.status_icon = self.status_spinner = None
        self.level_meter = self.level_bar = None
        self.replacements_summary = None
        self.practice = self.practice_placeholder = self.overlay_label = None
        self.record_button = self.model_combo = self.model_action = None
        self.microphone_combo = None
        self.microphone_labels: dict[str, str] = {}
        self.model_remove = self.model_description = self.download_progress = None
        self.model_buttons = self.model_state = None
        self.indicator = self.indicator_status = self.indicator_model = None
        self.indicator_dictate = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        self.hold()
        self._install_css()
        self._build_indicator()
        dictate_action = Gio.SimpleAction.new("dictate", None)
        dictate_action.connect("activate", self._native_shortcut)
        self.add_action(dictate_action)
        try:
            self.keyboard = UInputKeyboard()
        except Exception as error:
            LOG.exception("uinput setup failed")
            self._set_status(f"Text injection unavailable: {error}", True)
        try:
            self.shortcuts = GnomeShortcut(self._shortcut_changed, self._set_status)
            self.shortcuts.start()
        except Exception as error:
            LOG.exception("Global shortcut setup failed")
            self._set_status(f"Shortcut setup failed: {error}", True)
        threading.Thread(target=self._start_server, daemon=True).start()

    def do_activate(self) -> None:
        if self.background:
            # Autostart must not construct (and show) the settings window: a
            # mapped Wayland toplevel takes focus away from the dictation target.
            self.background = False
            return
        self._show_window()

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
            .purr-card { background: alpha(@theme_fg_color, 0.055); border-radius: 12px; padding: 16px; }
            .purr-live { background: rgba(24, 24, 28, 0.94); color: white; border-radius: 18px; padding: 18px 24px; }
            .purr-live label { color: white; font-size: 17px; }
            .purr-record { padding: 10px 22px; font-weight: bold; }
            .purr-card-title { font-weight: bold; font-size: 15px; }
            .purr-status { font-weight: bold; }
            .purr-ready { color: @success_color; }
            .purr-listening { color: #e5484d; }
            .purr-error { color: @error_color; }
            .purr-in-use { color: @success_color; font-weight: bold; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _build_indicator(self) -> None:
        self.indicator = AppIndicator.Indicator.new(
            APP_ID, str(ICON), AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        self.indicator_status = Gtk.MenuItem(label=self.status_text)
        self.indicator_status.set_sensitive(False)
        menu.append(self.indicator_status)
        self.indicator_model = Gtk.MenuItem(label=self.model.name)
        self.indicator_model.set_sensitive(False)
        menu.append(self.indicator_model)
        menu.append(Gtk.SeparatorMenuItem())
        show = Gtk.MenuItem(label="Open Purr")
        show.connect("activate", lambda _item: self._show_window())
        menu.append(show)
        configure = Gtk.MenuItem(label="Change Dictation Shortcut…")
        configure.connect("activate", lambda _item: self.shortcuts and self.shortcuts.configure())
        menu.append(configure)
        self.indicator_dictate = Gtk.MenuItem(label="Start Dictation")
        self.indicator_dictate.connect("activate", lambda _item: self._toggle_recording(None))
        menu.append(self.indicator_dictate)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _item: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)

    def _build_window(self) -> None:
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Purr")
        self.window.set_default_size(650, 650)
        self.window.connect("delete-event", self._hide_window)
        header = Gtk.HeaderBar(title="Purr", subtitle="Private, local dictation")
        header.set_show_close_button(True)
        self.window.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_border_width(22)

        status_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        status_card.get_style_context().add_class("purr-card")
        status_row = Gtk.Box(spacing=10)
        self.status_icon = Gtk.Image()
        self.status_spinner = Gtk.Spinner()
        for indicator in (self.status_icon, self.status_spinner):
            indicator.set_no_show_all(True)
            status_row.pack_start(indicator, False, False, 0)
        # The window may be built long after startup (autostart), so begin from
        # the current status instead of a stale placeholder.
        self.status_label = Gtk.Label(label=self.status_text, xalign=0)
        self.status_label.set_line_wrap(True)
        self.status_label.get_style_context().add_class("purr-status")
        status_row.pack_start(self.status_label, True, True, 0)
        # What the microphone hears while listening, so a muted or wrong
        # microphone shows up at once rather than after a minute of talking.
        self.level_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
        for offset in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, Gtk.LEVEL_BAR_OFFSET_FULL):
            self.level_bar.remove_offset_value(offset)
        self.level_bar.set_size_request(110, 8)
        self.level_meter = Gtk.Box(spacing=6, tooltip_text="What the microphone hears")
        microphone = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic", Gtk.IconSize.BUTTON)
        microphone.get_style_context().add_class("dim-label")
        for part in (microphone, self.level_bar):
            self.level_meter.pack_start(part, False, False, 0)
            part.show()  # show_all() skips everything inside a no-show-all widget
        self.level_meter.set_no_show_all(True)
        status_row.pack_end(self.level_meter, False, False, 0)
        status_card.pack_start(status_row, False, False, 0)
        # Dictation started from this window types here. A real text field
        # gives the typed keys a home and keeps focus off the buttons, where a
        # typed space or Enter would press them.
        self.practice = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=10, right_margin=10,
            top_margin=8, bottom_margin=8,
        )
        practice_scroll = Gtk.ScrolledWindow(min_content_height=110)
        practice_scroll.set_shadow_type(Gtk.ShadowType.IN)
        practice_scroll.add(self.practice)
        practice_area = Gtk.Overlay()
        practice_area.add(practice_scroll)
        self.practice_placeholder = Gtk.Label(
            label="Try Purr here: Start Dictation types into this box.\n"
            "In any other app, use the shortcut.",
            xalign=0, yalign=0, margin_start=12, margin_top=9,
        )
        self.practice_placeholder.get_style_context().add_class("dim-label")
        practice_area.add_overlay(self.practice_placeholder)
        practice_area.set_overlay_pass_through(self.practice_placeholder, True)
        self.practice.get_buffer().connect(
            "changed", lambda buffer: self.practice_placeholder.set_visible(not buffer.get_char_count())
        )
        status_card.pack_start(practice_area, True, True, 0)
        buttons = Gtk.Box(spacing=10)
        self.record_button = Gtk.Button(label="Start Dictation", focus_on_click=False)
        self.record_button.get_style_context().add_class("suggested-action")
        self.record_button.get_style_context().add_class("purr-record")
        self.record_button.connect("clicked", self._record_button_clicked)
        buttons.pack_start(self.record_button, True, True, 0)
        shortcut_button = Gtk.Button(label="Shortcut…", focus_on_click=False)
        shortcut_button.connect(
            "clicked", lambda _button: self.shortcuts and self.shortcuts.configure()
        )
        buttons.pack_start(shortcut_button, False, False, 0)
        status_card.pack_start(buttons, False, False, 0)
        outer.pack_start(status_card, True, True, 0)

        model_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        model_card.get_style_context().add_class("purr-card")
        model_title = Gtk.Label(label="Transcription model", xalign=0)
        model_title.get_style_context().add_class("purr-card-title")
        model_card.pack_start(model_title, False, False, 0)
        self.model_combo = Gtk.ComboBoxText()
        self._populate_model_combo(self.model.id)
        self.model_combo.connect("changed", self._model_changed)
        model_card.pack_start(self.model_combo, False, False, 0)
        self.model_description = Gtk.Label(xalign=0)
        self.model_description.set_line_wrap(True)
        self.model_description.get_style_context().add_class("dim-label")
        model_card.pack_start(self.model_description, False, False, 0)
        self.model_state = Gtk.Label(xalign=0)
        self.model_state.get_style_context().add_class("purr-in-use")
        self.model_state.set_no_show_all(True)
        model_card.pack_start(self.model_state, False, False, 0)
        self.model_buttons = Gtk.Box(spacing=8)
        self.model_buttons.set_no_show_all(True)
        self.model_action = Gtk.Button()
        self.model_action.connect("clicked", self._model_action_clicked)
        self.model_buttons.pack_start(self.model_action, True, True, 0)
        self.model_remove = Gtk.Button(label="Remove")
        self.model_remove.connect("clicked", self._remove_model_clicked)
        self.model_buttons.pack_start(self.model_remove, False, False, 0)
        model_card.pack_start(self.model_buttons, False, False, 0)
        self.download_progress = Gtk.ProgressBar(show_text=True)
        self.download_progress.set_no_show_all(True)
        model_card.pack_start(self.download_progress, False, False, 0)
        outer.pack_start(model_card, False, False, 0)

        settings_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings_card.get_style_context().add_class("purr-card")
        settings_title = Gtk.Label(label="Dictation", xalign=0)
        settings_title.get_style_context().add_class("purr-card-title")
        settings_card.pack_start(settings_title, False, False, 0)
        options = Gtk.Grid(column_spacing=14, row_spacing=10)
        language_label = Gtk.Label(label="Recognition language", xalign=0)
        options.attach(language_label, 0, 0, 1, 1)
        language = Gtk.ComboBoxText()
        for code, name in (
            ("en", "English"), ("auto", "Automatic"), ("es", "Spanish"),
            ("de", "German"), ("fr", "French"), ("it", "Italian"),
            ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
        ):
            language.append(code, name)
        language.set_active_id(str(self.config.get("language", "en")))
        language.connect("changed", self._language_changed)
        language.set_hexpand(True)
        options.attach(language, 1, 0, 1, 1)
        microphone_label = Gtk.Label(label="Microphone", xalign=0)
        options.attach(microphone_label, 0, 1, 1, 1)
        self.microphone_combo = Gtk.ComboBoxText()
        for cell in self.microphone_combo.get_cells():
            # Long device names must not widen the whole window.
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.microphone_combo.connect("changed", self._microphone_changed)
        self._populate_microphones()
        self.microphone_combo.set_hexpand(True)
        options.attach(self.microphone_combo, 1, 1, 1, 1)
        options.attach(Gtk.Label(label="Replacements", xalign=0), 0, 2, 1, 1)
        replacements = Gtk.Box(spacing=10)
        self.replacements_summary = Gtk.Label(xalign=0, hexpand=True)
        self.replacements_summary.get_style_context().add_class("dim-label")
        replacements.pack_start(self.replacements_summary, True, True, 0)
        edit = Gtk.Button(label="Edit…")
        edit.connect("clicked", self._edit_replacements)
        replacements.pack_start(edit, False, False, 0)
        options.attach(replacements, 1, 2, 1, 1)
        self._show_replacements_summary()
        commands = Gtk.CheckButton(label='Enable voice commands, including “scratch that”')
        commands.set_active(bool(self.config.get("voice_commands", True)))
        commands.connect("toggled", self._commands_changed)
        options.attach(commands, 0, 3, 2, 1)
        fillers = Gtk.CheckButton(label="Remove filler words such as “um” and “uh”")
        fillers.set_active(bool(self.config.get("trim_fillers", True)))
        fillers.connect("toggled", self._fillers_changed)
        options.attach(fillers, 0, 4, 2, 1)
        settings_card.pack_start(options, False, False, 0)
        outer.pack_start(settings_card, False, False, 0)

        note = Gtk.Label(
            label="Purr starts when you sign in. Closing this window leaves it in the top bar.",
            xalign=0,
        )
        note.get_style_context().add_class("dim-label")
        outer.pack_end(note, False, False, 0)
        self.window.add(outer)
        self.window.show_all()
        self.download_progress.hide()
        self.practice.grab_focus()
        self._show_status_state()
        self._sync_record_controls()
        self._refresh_model_ui()

    def _populate_microphones(self) -> None:
        """(Re)list sources, so devices plugged in since the last look appear."""
        combo = self.microphone_combo
        microphones = list_microphones()
        try:
            current_default = subprocess.run(
                ["pactl", "get-default-source"], capture_output=True, text=True,
                timeout=2, check=True,
            ).stdout.strip()
        except Exception:
            current_default = ""
        default_label = next(
            (label for name, label in microphones if name == current_default), "system choice"
        )
        selected = str(self.config.get("microphone", "default"))
        self.microphone_labels = dict(microphones)
        label = self.microphone_labels.get(selected)
        if label and label != self.config.get("microphone_label"):
            # Keep the remembered name current, so an unplugged choice is shown by name.
            self.config["microphone_label"] = label
            save_config(self.config)
        # Repopulating emits "changed"; it must not rewrite the saved choice.
        combo.handler_block_by_func(self._microphone_changed)
        try:
            combo.remove_all()
            combo.append("default", f"System default — {default_label}")
            for name, label in microphones:
                combo.append(name, label)
            if selected != "default" and selected not in {name for name, _ in microphones}:
                # Keep an unplugged choice visible instead of silently replacing it;
                # recording falls back to the system default until it returns.
                label = str(self.config.get("microphone_label") or "Selected microphone")
                combo.append(selected, f"{label} (not connected)")
            combo.set_active_id(selected)
        finally:
            combo.handler_unblock_by_func(self._microphone_changed)

    def _build_overlay(self) -> None:
        self.overlay = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.overlay.set_decorated(False)
        self.overlay.set_accept_focus(False)
        self.overlay.set_focus_on_map(False)
        self.overlay.set_keep_above(True)
        self.overlay.set_skip_pager_hint(True)
        self.overlay.set_skip_taskbar_hint(True)
        self.overlay.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.overlay.set_default_size(560, 92)
        self.overlay.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        frame.set_border_width(18)
        frame.get_style_context().add_class("purr-live")
        listening = Gtk.Label(label="●  PURR IS LISTENING", xalign=0)
        listening.set_markup("<span foreground='#ff7b72' weight='bold'>●</span>  <b>PURR IS LISTENING</b>")
        frame.pack_start(listening, False, False, 0)
        self.overlay_label = Gtk.Label(label="Speak now…", xalign=0)
        self.overlay_label.set_line_wrap(True)
        self.overlay_label.set_max_width_chars(64)
        frame.pack_start(self.overlay_label, True, True, 0)
        self.overlay.add(frame)

    def _show_overlay(self, text: str = "Speak now…") -> None:
        if not self.config.get("show_overlay", False):
            return
        if not self.overlay:
            self._build_overlay()
        self.overlay_label.set_text(text)
        self.overlay.show_all()

    def _hide_overlay(self) -> bool:
        if self.overlay:
            self.overlay.hide()
        return False

    def _hide_window(self, *_args):
        self.window.hide()
        return True

    def _show_window(self) -> None:
        if not self.window:
            self._build_window()
        else:
            self._populate_microphones()
        self.window.present()

    def _record_button_clicked(self, _button) -> None:
        if not self.recorder and self.practice:
            # Dictation started here types into the practice area.
            self.practice.grab_focus()
            buffer = self.practice.get_buffer()
            buffer.place_cursor(buffer.get_end_iter())
        self._toggle_recording(None)

    def _toggle_recording(self, _button) -> None:
        self.stop_recording() if self.recorder else self.start_recording()

    def _native_shortcut(self, _action, _parameter) -> None:
        now = time.monotonic()
        repeated = now - self.shortcut_pressed_at < 0.8
        self.shortcut_pressed_at = now
        self._hold_typing_for_shortcut()
        # GNOME normally invokes a custom shortcut once per press. If a keyboard
        # does repeat it, keep extending this guard so a held key cannot toggle
        # dictation back off.
        if not repeated:
            # Act on the press at once so the first words are captured; only
            # typing waits for the shortcut's modifiers to be released.
            self._toggle_recording(None)

    def _hold_typing_for_shortcut(self) -> None:
        self.shortcut_down = True
        if self.shortcut_guard:
            GLib.source_remove(self.shortcut_guard)
        self.shortcut_guard = GLib.timeout_add(600, self._shortcut_released)

    def _shortcut_released(self) -> bool:
        self.shortcut_guard = 0
        self.shortcut_down = False
        if self.recorder and self.stream_raw:
            # Type whatever was recognized while the modifiers were still held.
            self._stream_update(self.stream_raw, False, False, False)
        return False

    def _set_status(self, text: str, error: bool = False) -> bool:
        LOG.error(text) if error else LOG.info(text)
        self.status_text, self.status_error = text, error
        if self.status_label:
            self.status_label.set_text(text)
            self._show_status_state()
        if self.indicator_status:
            self.indicator_status.set_label(text[:100])
        if self.indicator:
            icon = "dialog-error-symbolic" if error else "audio-input-microphone-symbolic"
            self.indicator.set_icon_full(icon, text)
        return False

    def _show_status_state(self) -> None:
        """Pair the status text with a glance-able state: ready, listening, working, or error."""
        if not self.status_label:
            return
        working = not self.status_error and not self.recorder and (self.busy or not self.ready)
        self.status_spinner.set_visible(working)
        self.status_spinner.start() if working else self.status_spinner.stop()
        self.status_icon.set_visible(not working)
        if self.status_error:
            state, icon = "purr-error", "dialog-error-symbolic"
        elif self.recorder:
            state, icon = "purr-listening", "media-record-symbolic"
        else:
            state, icon = "purr-ready", "object-select-symbolic"
        self.status_icon.set_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        style = self.status_icon.get_style_context()
        for name in ("purr-error", "purr-listening", "purr-ready"):
            style.remove_class(name)
        style.add_class(state)

    def _flash_status(self, text: str, milliseconds: int = 4000) -> None:
        """Show a passing remark, then return to Ready unless something else happened."""
        self._set_status(text)

        def restore():
            if self.status_text == text and self.ready and not self.busy and not self.recorder:
                self._show_ready()
            return False

        GLib.timeout_add(milliseconds, restore)

    def _listening_status(self) -> str:
        return f"Listening… tap {self._shortcut_text()} again to finish"

    def _sync_record_controls(self) -> None:
        label = "Stop Dictation" if self.recorder else "Start Dictation"
        if self.level_meter:
            # Only a live stream reports levels; batch models record to a file.
            listening = bool(self.recorder) and self.server.streaming
            self.level_meter.set_visible(listening)
            self.level_bar.set_value(0)
        if self.record_button:
            self.record_button.set_label(label)
            self.record_button.set_sensitive(bool(self.recorder) or (self.ready and not self.busy))
            style = self.record_button.get_style_context()
            style.remove_class("destructive-action" if not self.recorder else "suggested-action")
            style.add_class("destructive-action" if self.recorder else "suggested-action")
        if self.indicator_dictate:
            self.indicator_dictate.set_label(label)
        # Switching engines mid-dictation is refused, so reflect that in the UI.
        self._refresh_model_ui()

    def _shortcut_text(self) -> str:
        return self.shortcut_description or "the Purr shortcut"

    def _show_ready(self) -> None:
        # Without the virtual keyboard nothing can be typed, so never claim Ready.
        if not self.keyboard:
            self._set_status("Text injection unavailable — check /dev/uinput access", True)
        else:
            self._set_status(self._ready_status())

    def _ready_status(self) -> str:
        if not self.shortcut_description:
            return "Ready — set a shortcut with Shortcut…"
        return f"Ready — tap {self.shortcut_description} to dictate"

    def _shortcut_changed(self, binding: str) -> None:
        self.shortcut_description = shortcut_label(binding) if binding else None
        if self.ready and not self.busy and not self.recorder:
            self._show_ready()
        elif not self.ready and not self.engine_error:
            self._set_status(f"Loading {self.model.name}…")

    def _unavailable_reason(self) -> str:
        if self.engine_error:
            return f"The transcription engine did not start: {self.engine_error}"
        if not self.ready:
            return "Still loading the transcription model — try again in a moment"
        return "Still finishing the previous dictation — try again in a moment"

    def _notify(self, title: str, body: str) -> None:
        subprocess.Popen(
            ["notify-send", "--app-name=Purr", "--icon=audio-input-microphone", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _start_server(self) -> None:
        try:
            self.server.start()
            GLib.idle_add(self._server_ready)
        except Exception as error:
            LOG.exception("Unable to start ASR")
            GLib.idle_add(self._server_failed, str(error))

    def _server_ready(self) -> bool:
        self.ready = True
        self.busy = False
        self.engine_error = None
        self._show_ready()
        self._sync_record_controls()
        if self.indicator_model:
            self.indicator_model.set_label(f"{self.model.name} ({self.server.backend_label})")
        self._refresh_model_ui()
        return False

    def _server_failed(self, message: str) -> bool:
        self.ready = False
        self.busy = False
        self.engine_error = message
        self._set_status(message, True)
        self._sync_record_controls()
        self._notify("Purr could not start", message)
        self._refresh_model_ui()
        return False

    def _default_source(self) -> str:
        selected = str(self.config.get("microphone", "default"))
        if selected != "default":
            available = {name for name, _label in list_microphones()}
            if selected in available:
                return selected
            LOG.warning("Configured microphone disappeared: %s; using system default", selected)
        result = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True, timeout=2, check=True
        )
        source = result.stdout.strip()
        if not source:
            raise RuntimeError("No default microphone is configured")
        return source

    def start_recording(self) -> None:
        if self.recorder:
            return
        if not self.ready or self.busy:
            # A tap that cannot start must say why; silence looks like a broken shortcut.
            reason = self._unavailable_reason()
            LOG.info("Dictation not started: %s", reason)
            self._notify("Purr isn't ready", reason)
            return
        if not self.keyboard:
            message = "Text injection unavailable — check /dev/uinput access"
            self._set_status(message, True)
            self._notify("Purr cannot type", message)
            return
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            source = self._default_source()
            self.stream_typed = ""
            self.stream_raw = ""
            self.stream_external_drops = 0
            self.heard_sound = self.silence_warned = False
            self._show_overlay()
            if self.server.streaming:
                stream = self.server.begin_stream()
                self.recorder = subprocess.Popen(
                    [
                        "pw-record", "--raw", "--target", source, "--rate", "16000",
                        "--channels", "1", "--format", "s16", "--latency", "20ms", "-",
                    ],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                self.stream_thread = threading.Thread(
                    target=self._consume_stream, args=(self.recorder, stream), daemon=True
                )
                self.stream_thread.start()
            else:
                self.recording_path = RUNTIME / f"utterance-{time.monotonic_ns()}.wav"
                self.recorder = subprocess.Popen(
                    [
                        "pw-record", "--target", source, "--rate", "16000", "--channels", "1",
                        "--format", "s16", "--latency", "20ms", str(self.recording_path),
                    ],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            self._set_status(self._listening_status())
            if self.indicator:
                self.indicator.set_icon_full("media-record-symbolic", "Purr is listening")
            self._sync_record_controls()
        except Exception as error:
            LOG.exception("Could not start microphone")
            self.recorder = None
            self._sync_record_controls()
            self._hide_overlay()
            self._set_status(f"Microphone error: {error}", True)
            self._notify("Purr microphone error", str(error))

    def stop_recording(self) -> None:
        if not self.recorder:
            return
        recorder = self.recorder
        self.recorder = None
        self.busy = True
        self._sync_record_controls()
        self._set_status("Finalizing transcript…" if self.server.streaming else "Transcribing…")
        if self.indicator:
            self.indicator.set_icon_full("emblem-synchronizing-symbolic", "Purr is transcribing")
        try:
            recorder.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        if not self.server.streaming:
            path = self.recording_path
            self.recording_path = None

            def finish_recording():
                try:
                    recorder.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait(timeout=2)
                self._transcribe(path)

            threading.Thread(target=finish_recording, daemon=True).start()

    def _consume_stream(self, recorder, stream) -> None:
        raw_text = ""
        total_samples = 0
        sum_squares = 0
        peak = 0
        error = None
        try:
            while True:
                pcm = os.read(recorder.stdout.fileno(), 6400)
                if not pcm:
                    break
                samples = array.array("h")
                samples.frombytes(pcm[: len(pcm) // 2 * 2])
                if sys.byteorder != "little":
                    samples.byteswap()
                total_samples += len(samples)
                if samples:
                    chunk_peak = max(abs(sample) for sample in samples)
                    peak = max(peak, chunk_peak)
                    sum_squares += sum(sample * sample for sample in samples)
                    GLib.idle_add(self._hear, recorder, chunk_peak, total_samples / 16000)
                result = stream.feed_s16(pcm)
                raw_text += result.get("text", "")
                if result.get("text") or result.get("eou") or result.get("eob"):
                    GLib.idle_add(
                        self._stream_update, raw_text, bool(result.get("eou")),
                        bool(result.get("eob")), False,
                    )
            try:
                recorder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait(timeout=2)
            result = stream.finalize()
            raw_text += result.get("text", "")
        except Exception as caught:
            error = str(caught)
            LOG.exception("Live transcription failed")
        finally:
            stream.close()
        duration = total_samples / 16000
        peak_db = 20 * math.log10(peak / 32768) if peak else -120.0
        rms = math.sqrt(sum_squares / total_samples) if total_samples else 0
        rms_db = 20 * math.log10(rms / 32768) if rms else -120.0
        LOG.info(
            "Stream captured %.2fs, peak %.1f dBFS, RMS %.1f dBFS, raw=%s",
            duration, peak_db, rms_db, self._loggable(raw_text),
        )
        GLib.idle_add(self._stream_finished, raw_text, duration, peak_db, error)

    def _hear(self, recorder, chunk_peak: int, seconds: float) -> bool:
        """Show the level, and warn early when the microphone gives only silence."""
        if recorder is not self.recorder:
            return False  # a late chunk from a dictation that already ended
        level_db = 20 * math.log10(chunk_peak / 32768) if chunk_peak else -120.0
        if self.level_bar:
            self.level_bar.set_value(min(1.0, max(0.0, (level_db + 60) / 60)))
        if level_db >= SILENCE_DB:
            self.heard_sound = True
            if self.silence_warned:
                self.silence_warned = False
                self._set_status(self._listening_status())
        elif not self.heard_sound and not self.silence_warned and seconds >= SILENCE_WARNING_SECONDS:
            self.silence_warned = True
            name = self._recording_microphone_name()
            message = f"Purr hears only silence from {name} — check that it is connected and unmuted"
            self._set_status(message, True)
            self._notify("Purr hears nothing", message)
        return False

    def _recording_microphone_name(self) -> str:
        selected = str(self.config.get("microphone", "default"))
        microphones = dict(list_microphones())
        if selected in microphones:
            return microphones[selected]
        try:
            source = subprocess.run(
                ["pactl", "get-default-source"], capture_output=True, text=True, timeout=2, check=True,
            ).stdout.strip()
        except Exception:
            source = ""
        return microphones.get(source, "the microphone")

    def _loggable(self, text: str) -> str:
        """Dictated words stay out of the log unless log_transcripts is on."""
        if self.config.get("log_transcripts", False):
            return repr(text)
        return f"<{len(text.split())} words>"

    def _stream_update(self, raw: str, eou: bool, eob: bool, final: bool) -> bool:
        self.stream_raw = raw
        text, drop_previous = self.processor.apply(raw)
        preview = transcript_preview(text) or ("End of utterance…" if eou else "Listening…")
        if self.overlay_label:
            self.overlay_label.set_text(preview)
        if not self.shortcut_down:
            self._apply_stream_typing(text, drop_previous)
        return False

    def _apply_stream_typing(self, text: str, drop_previous: int) -> None:
        if not self.keyboard:
            return
        if drop_previous > self.stream_external_drops:
            self.keyboard.backspace_word(drop_previous - self.stream_external_drops)
            self.stream_external_drops = drop_previous
        common = 0
        limit = min(len(self.stream_typed), len(text))
        while common < limit and self.stream_typed[common] == text[common]:
            common += 1
        removed = len(self.stream_typed) - common
        if removed:
            self.keyboard.backspace(removed)
        addition = text[common:]
        if addition:
            # PostProcessor output contains only typeable characters.
            self.keyboard.type_text(addition)
        self.stream_typed = text
        if addition or removed:
            LOG.info(
                "Live injection updated: +%d chars, -%d chars, %d total",
                len(addition), removed, len(text),
            )

    def _stream_finished(
        self, raw: str, duration: float, peak_db: float, error: str | None
    ) -> bool:
        if self.shutting_down:
            return False
        if self.shortcut_down:
            # The stop shortcut's modifiers may still be held; typing the final
            # words now would turn them into Ctrl/Alt chords.
            GLib.timeout_add(50, self._stream_finished, raw, duration, peak_db, error)
            return False
        # stop_recording clears the recorder first, so a recorder still set
        # here ended without being asked: the microphone failed or vanished.
        ended_early = self.recorder is not None
        self.recorder = None
        if ended_early and not error:
            error = (
                "The microphone stopped unexpectedly — dictation ended"
                if self.stream_typed or duration >= 0.20
                else "Could not record from the microphone — check that it is connected"
            )
        if error:
            # Everything already typed was finalized by the engine. Keep it, so a
            # late failure cannot erase minutes of dictation.
            return self._complete("", 0, error)
        if duration < 0.20:
            return self._complete("", 0, "Too short — speak for a moment before stopping")
        if peak_db < SILENCE_DB:
            if self.stream_typed and self.keyboard:
                self.keyboard.backspace(len(self.stream_typed))
            return self._complete("", 0, "No speech detected")
        self._stream_update(raw, False, False, True)
        text, _drop_previous = self.processor.apply(raw)
        LOG.info("Live transcript final=%s", self._loggable(text))
        if text and self.keyboard and not text.endswith("\n"):
            self.keyboard.type_text(" ")
        self._complete("", 0, None)
        GLib.timeout_add(700, self._hide_overlay)
        return False

    def _transcribe(self, path: Path | None) -> None:
        try:
            if not path or not path.exists() or path.stat().st_size <= 44:
                raise RuntimeError("No microphone audio was captured")
            with wave.open(str(path), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            if duration < 0.20:
                GLib.idle_add(self._complete, "", 0, "Too short — speak for a moment before stopping")
                return
            peak_db, rms_db = normalize_wav(path)
            LOG.info("Captured %.2fs, peak %.1f dBFS, RMS %.1f dBFS", duration, peak_db, rms_db)
            if peak_db < SILENCE_DB:
                GLib.idle_add(self._complete, "", 0, "No speech detected")
                return
            raw = self.server.transcribe(path, multipart_body)
            text, drop_previous = self.processor.apply(raw)
            LOG.info("Transcript raw=%s final=%s", self._loggable(raw), self._loggable(text))
            GLib.idle_add(self._complete, text, drop_previous, None)
        except Exception as error:
            LOG.exception("Dictation failed")
            GLib.idle_add(self._complete, "", 0, str(error))
        finally:
            if path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOG.warning("Could not remove temporary recording %s", path)

    def _complete(self, text: str, drop_previous: int, message: str | None) -> bool:
        if self.shortcut_down and (text or drop_previous):
            # Batch results can arrive while the stop shortcut is still held.
            GLib.timeout_add(50, self._complete, text, drop_previous, message)
            return False
        self.busy = False
        self._sync_record_controls()
        if message:
            self._hide_overlay()
            if message in ("No speech detected", "Too short — speak for a moment before stopping"):
                self._flash_status(message)
            else:
                self._set_status(
                    message, message.startswith(("No microphone", "Transcription", "Could not", "The microphone"))
                )
                self._notify("Purr dictation failed", message)
            return False
        if drop_previous and self.keyboard:
            self.keyboard.backspace_word(drop_previous)
        if text:
            insertion = text if text.endswith(("\n", "\t", " ")) else text + " "
            if self.keyboard:
                self.keyboard.type_text(insertion)
        self._show_ready()
        return False

    def _selected_model(self):
        selected = self.model_combo.get_active_id() if self.model_combo else self.model.id
        return MODEL_BY_ID.get(selected, self.model)

    def _populate_model_combo(self, active_id: str) -> None:
        if not self.model_combo:
            return
        self.model_combo.remove_all()
        for model in MODELS:
            installed = "  •  installed" if self.store.installed(model) else f"  •  {model.size_label}"
            live = "  •  live" if model.streaming else ""
            self.model_combo.append(model.id, model.name + live + installed)
        self.model_combo.set_active_id(active_id)

    def _model_changed(self, _combo=None) -> None:
        self._refresh_model_ui()

    def _refresh_model_ui(self) -> bool:
        if not self.model_combo:
            return False
        selected = self._selected_model()
        installed = self.store.installed(selected)
        self.model_description.set_text(
            f"{selected.summary}\n{selected.languages}  ·  {selected.size_label}"
        )
        active = selected.id == self.model.id
        self.model_state.set_visible(active)
        self.model_buttons.set_visible(not active)
        if active:
            self.model_state.set_text(
                f"✓  In use  ·  {self.server.backend_label}" if self.ready else "Loading…"
            )
            return False
        self.model_action.set_label(
            "Use model" if installed else f"Download & use ({selected.size_label})"
        )
        self.model_action.set_sensitive(not self.busy and not self.downloading and not self.recorder)
        self.model_action.show()
        self.model_remove.set_visible(installed and selected.engine != "whisper")
        return False

    def _model_action_clicked(self, _button) -> None:
        selected = self._selected_model()
        if self.store.installed(selected):
            self._activate_model(selected)
            return
        # Downloads run beside dictation; only switching engines needs Purr idle.
        self.downloading = True
        self.model_action.set_sensitive(False)
        self.model_combo.set_sensitive(False)
        self.download_progress.set_fraction(0)
        self.download_progress.set_text(f"Downloading {selected.name}…")
        self.download_progress.show()

        def progress(done, total):
            GLib.idle_add(self._download_progress, selected.name, done, total)

        def download():
            try:
                self.store.download(selected, progress)
                GLib.idle_add(self._download_done, selected, None)
            except Exception as error:
                LOG.exception("Model download failed")
                GLib.idle_add(self._download_done, selected, str(error))

        threading.Thread(target=download, daemon=True).start()

    def _download_progress(self, name: str, done: int, total: int) -> bool:
        self.download_progress.set_fraction(done / total if total else 0)
        self.download_progress.set_text(f"Downloading {name} — {done / (1024*1024):.0f} / {total / (1024*1024):.0f} MB")
        return False

    def _download_done(self, selected, error: str | None) -> bool:
        self.download_progress.hide()
        self.model_combo.set_sensitive(True)
        self.downloading = False
        if error:
            self._set_status(f"Download failed: {error}", True)
            self._refresh_model_ui()
        else:
            self._populate_model_combo(selected.id)
            if self.busy or self.recorder:
                self._set_status(f"Downloaded {selected.name} — choose Use model after dictating")
                self._refresh_model_ui()
            else:
                self._activate_model(selected)
        return False

    def _activate_model(self, selected) -> None:
        if self.busy or self.recorder:
            return
        self.ready = False
        self.busy = True
        self._sync_record_controls()
        self._set_status(f"Loading {selected.name}…")
        self.config["model"] = selected.id
        save_config(self.config)

        def switch():
            try:
                self.server.stop()
                server, actual = create_engine(self.config, BUILD, STATE, self.store)
                server.start()
                self.server, self.model = server, actual
                GLib.idle_add(self._server_ready)
            except Exception as error:
                LOG.exception("Could not switch model")
                GLib.idle_add(self._server_failed, str(error))

        threading.Thread(target=switch, daemon=True).start()

    def _remove_model_clicked(self, _button) -> None:
        selected = self._selected_model()
        try:
            self.store.remove(selected)
            self._flash_status(f"Removed {selected.name}")
        except Exception as error:
            self._set_status(str(error), True)
        self._populate_model_combo(selected.id)
        self._refresh_model_ui()

    def _language_changed(self, combo) -> None:
        if combo.get_active_id():
            self.config["language"] = combo.get_active_id()
            save_config(self.config)

    def _microphone_changed(self, combo) -> None:
        selected = combo.get_active_id()
        if selected:
            self.config["microphone"] = selected
            # Remembered so an unplugged choice can still be shown by name. The
            # "(not connected)" entry itself is not a name worth remembering.
            if selected in self.microphone_labels:
                self.config["microphone_label"] = self.microphone_labels[selected]
            save_config(self.config)

    def _show_replacements_summary(self) -> None:
        if not self.replacements_summary:
            return
        dictionary = self.config.get("dictionary") or {}
        count = len(dictionary) if isinstance(dictionary, dict) else 0
        self.replacements_summary.set_text(
            "None yet — fix names and jargon Purr mishears" if not count
            else "1 replacement" if count == 1 else f"{count} replacements"
        )

    def _edit_replacements(self, _button) -> None:
        dialog = self._build_replacements_dialog()
        dialog.connect("response", lambda dialog, _response: dialog.destroy())
        dialog.show_all()

    def _build_replacements_dialog(self) -> Gtk.Dialog:
        dialog = Gtk.Dialog(
            title="Replacements", transient_for=self.window, modal=True, use_header_bar=True,
        )
        dialog.set_default_size(560, 440)
        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_border_width(16)
        intro = Gtk.Label(
            label="When Purr hears the words on the left, it types the text on the right. "
            "Matching ignores case and whole words only — for example, “type script” → “TypeScript”.",
            xalign=0, wrap=True, max_width_chars=60,
        )
        intro.get_style_context().add_class("dim-label")
        content.pack_start(intro, False, False, 0)
        table = Gtk.Grid(column_spacing=8, row_spacing=8)
        for column, title in enumerate(("When I say", "Type")):
            heading = Gtk.Label(label=title, xalign=0)
            heading.get_style_context().add_class("purr-card-title")
            table.attach(heading, column, 0, 1, 1)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.add(table)
        content.pack_start(scroll, True, True, 0)
        rows = []

        def save(*_args):
            self._save_replacements((spoken.get_text(), typed.get_text()) for spoken, typed, _ in rows)

        def add_row(spoken_text: str = "", typed_text: str = "") -> Gtk.Entry:
            spoken = Gtk.Entry(text=spoken_text, placeholder_text="spoken words", hexpand=True)
            typed = Gtk.Entry(text=typed_text, placeholder_text="text to type", hexpand=True)
            remove = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON)
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_tooltip_text("Remove this replacement")
            row = (spoken, typed, remove)
            line = len(rows) + 1
            for column, widget in enumerate(row):
                table.attach(widget, column, line, 1, 1)
                widget.show()
            for entry in (spoken, typed):
                entry.connect("changed", save)

            def remove_row(_button):
                table.remove_row(table.child_get_property(spoken, "top-attach"))
                rows.remove(row)
                save()

            remove.connect("clicked", remove_row)
            rows.append(row)
            return spoken

        dictionary = self.config.get("dictionary") or {}
        if isinstance(dictionary, dict):
            for spoken_text, typed_text in dictionary.items():
                add_row(str(spoken_text), str(typed_text))
        add = Gtk.Button(label="Add Replacement", halign=Gtk.Align.START)
        add.connect("clicked", lambda _button: add_row().grab_focus())
        content.pack_start(add, False, False, 0)
        # Never open with an existing replacement focused and selected, where
        # the first keystroke would overwrite it.
        dialog.set_focus(add_row() if not rows else add)
        dialog.replacement_rows, dialog.add_replacement = rows, add_row
        return dialog

    def _save_replacements(self, pairs) -> None:
        """Apply the editor's rows at once. A half-filled row is a draft: saving
        it would make Purr delete the spoken words while the user is typing."""
        self.config["dictionary"] = {
            spoken.strip(): typed for spoken, typed in pairs if spoken.strip() and typed.strip()
        }
        save_config(self.config)
        self._show_replacements_summary()

    def _fillers_changed(self, button) -> None:
        self.config["trim_fillers"] = button.get_active()
        save_config(self.config)

    def _commands_changed(self, button) -> None:
        self.config["voice_commands"] = button.get_active()
        save_config(self.config)

    def do_shutdown(self) -> None:
        self.shutting_down = True
        if self.recorder and self.recorder.poll() is None:
            self.recorder.send_signal(signal.SIGINT)
            try:
                self.recorder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.recorder.kill()
        if self.stream_thread and self.stream_thread.is_alive():
            self.stream_thread.join(timeout=5)
        if self.recording_path:
            self.recording_path.unlink(missing_ok=True)
        if self.shortcuts:
            self.shortcuts.close()
        if not self.stream_thread or not self.stream_thread.is_alive():
            self.server.stop()
        if self.keyboard:
            self.keyboard.close()
        Gtk.Application.do_shutdown(self)


def smoke_test() -> int:
    failures = []
    for description, path in (
        ("whisper-server", WHISPER_SERVER),
        ("Whisper model", MODEL),
        ("parakeet.cpp shared library", PARAKEET_LIB),
        ("Nemotron streaming model", NEMOTRON_MODEL),
        ("application icon", ICON),
        ("uinput device", Path("/dev/uinput")),
    ):
        if not path.exists():
            failures.append(f"missing {description}: {path}")
    if NEMOTRON_MODEL.exists() and NEMOTRON_MODEL.stat().st_size != 784_801_888:
        failures.append("Nemotron streaming model is incomplete")
    for command in ("pw-record", "pactl", "notify-send"):
        if not any((Path(folder) / command).exists() for folder in os.environ.get("PATH", "").split(":")):
            failures.append(f"missing command: {command}")
    if Path("/dev/uinput").exists() and not os.access("/dev/uinput", os.R_OK | os.W_OK):
        failures.append("/dev/uinput is not readable/writable by this user")
    try:
        source = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True, timeout=2, check=True
        ).stdout.strip()
        if not source:
            failures.append("no default PipeWire/PulseAudio source")
    except Exception as error:
        failures.append(f"microphone query failed: {error}")
    if WHISPER_SERVER.exists():
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = str(WHISPER_BIN)
        result = subprocess.run(
            [str(WHISPER_SERVER), "--help"], capture_output=True, text=True, env=env, timeout=10
        )
        if result.returncode != 0:
            failures.append("whisper-server does not execute")
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    print("Purr Linux smoke test passed")
    print("  source:", source)
    print("  default model:", NEMOTRON_MODEL)
    print("  engine:", PARAKEET_LIB)
    print("  fallback:", WHISPER_SERVER)
    print("  session:", os.environ.get("XDG_SESSION_TYPE", "unknown"))
    return 0


def main() -> int:
    if "--smoke-test" in sys.argv:
        return smoke_test()
    background = "--background" in sys.argv
    if background:
        sys.argv.remove("--background")
    app = PurrApplication(background)
    try:
        return app.run(sys.argv)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

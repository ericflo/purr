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
from gi.repository import Gdk, Gio, GLib, Gtk

import dbus
from dbus.mainloop.glib import DBusGMainLoop

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
WL_CLIPBOARD_BIN = BUILD / "wl-clipboard" / "usr" / "bin"
WL_COPY = WL_CLIPBOARD_BIN / "wl-copy"
WL_PASTE = WL_CLIPBOARD_BIN / "wl-paste"
ICON = ROOT / "Resources" / "purr_app_logo.png"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "purr"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "purr"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "purr/config.json"


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
    "live_typing": True,
    "show_overlay": False,
    "microphone": "default",
    "trim_fillers": True,
    "voice_commands": True,
    "dictionary": {},
    "initial_prompt": "",
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
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"[ \t]+([,.?!…])", r"\1", text)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        return text.strip(" \t"), drop_previous


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
    """Wayland-safe virtual keyboard for direct live typing and paste fallback."""

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
    KEY_V = 47
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

    def paste(self) -> None:
        with self.lock:
            self._key(self.KEY_LEFTCTRL, True)
            self._tap(self.KEY_V)
            self._key(self.KEY_LEFTCTRL, False)

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


class TextInserter:
    def __init__(self, keyboard: UInputKeyboard):
        self.keyboard = keyboard
        self.clipboard_owner: subprocess.Popen | None = None
        self.generation = 0

    def _stop_owner(self) -> None:
        owner, self.clipboard_owner = self.clipboard_owner, None
        if owner and owner.poll() is None:
            owner.terminate()
            try:
                owner.wait(timeout=1)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait(timeout=1)

    def _claim(self, contents: bytes) -> None:
        self._stop_owner()
        owner = subprocess.Popen(
            [str(WL_COPY), "--foreground", "--type", "text/plain;charset=utf-8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            owner.stdin.write(contents)
            owner.stdin.close()
        except Exception:
            owner.kill()
            owner.wait(timeout=1)
            raise
        self.clipboard_owner = owner

    def insert(self, text: str) -> None:
        encoded = text.encode("utf-8")
        previous = None
        self.generation += 1
        generation = self.generation
        try:
            snapshot = subprocess.run(
                [str(WL_PASTE), "--no-newline"], capture_output=True, timeout=0.5
            )
            previous = snapshot.stdout if snapshot.returncode == 0 else None
        except Exception:
            # An empty or stale Wayland selection can wait indefinitely on
            # some compositors. It is safe to proceed without a snapshot.
            LOG.info("No restorable Wayland clipboard contents were available")
        try:
            self._claim(encoded)
        except Exception:
            LOG.exception("Could not claim the Wayland clipboard")
            return

        attempts = 0

        def send_paste():
            nonlocal attempts
            attempts += 1
            # A background Wayland client cannot assume clipboard ownership is
            # immediate. Verify the offer from a separate client before sending
            # Ctrl+V; otherwise the target can consume the previous clipboard.
            try:
                current = subprocess.run(
                    [str(WL_PASTE), "--no-newline"], capture_output=True, timeout=0.15
                )
                ready = current.returncode == 0 and current.stdout == encoded
            except Exception:
                ready = False
            if not ready and attempts < 8:
                GLib.timeout_add(40, send_paste)
                return False
            if not ready:
                LOG.error("Wayland clipboard ownership did not become active; paste skipped")
                self._stop_owner()
                return False
            self.keyboard.paste()
            GLib.timeout_add(1200, restore)
            return False

        def restore():
            if generation != self.generation:
                return False
            if previous is not None:
                try:
                    self._claim(previous)
                except Exception:
                    LOG.exception("Could not restore the previous Wayland clipboard")
            else:
                self._stop_owner()
            return False

        GLib.timeout_add(25, send_paste)

    def close(self) -> None:
        self.generation += 1
        self._stop_owner()


class GlobalShortcuts:
    def __init__(self, on_press, on_release, on_status):
        self.on_press = on_press
        self.on_release = on_release
        self.on_status = on_status
        self.bus = dbus.SessionBus()
        portal = self.bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
        try:
            registry = dbus.Interface(portal, "org.freedesktop.host.portal.Registry")
            registry.Register(APP_ID, dbus.Dictionary(signature="sv"))
        except dbus.DBusException as error:
            # Older portal versions do not expose Registry; their process-based
            # host-app detection is sufficient.
            if "UnknownInterface" not in error.get_dbus_name():
                LOG.warning("Portal app registration failed: %s", error)
        self.interface = dbus.Interface(portal, "org.freedesktop.portal.GlobalShortcuts")
        self.session_handle = None
        self.interface.connect_to_signal("Activated", self._activated)
        self.interface.connect_to_signal("Deactivated", self._deactivated)
        self.interface.connect_to_signal("ShortcutsChanged", self._changed)

    def _watch(self, handle, callback) -> None:
        request = self.bus.get_object("org.freedesktop.portal.Desktop", handle)
        dbus.Interface(request, "org.freedesktop.portal.Request").connect_to_signal("Response", callback)

    def start(self) -> None:
        token = "purr_" + uuid.uuid4().hex[:12]
        options = {
            "handle_token": dbus.String(token),
            "session_handle_token": dbus.String("purr_" + uuid.uuid4().hex[:12]),
        }
        handle = self.interface.CreateSession(dbus.Dictionary(options, signature="sv"))
        self._watch(handle, self._session_created)

    def _session_created(self, response, results) -> None:
        if response != 0:
            self.on_status("Global shortcut permission was not granted", True)
            return
        self.session_handle = str(results["session_handle"])
        options = {"handle_token": dbus.String("purr_" + uuid.uuid4().hex[:12])}
        handle = self.interface.ListShortcuts(
            dbus.ObjectPath(self.session_handle), dbus.Dictionary(options, signature="sv")
        )
        self._watch(handle, self._listed)

    def _listed(self, response, results) -> None:
        shortcuts = results.get("shortcuts", []) if response == 0 else []
        if shortcuts:
            self._report(shortcuts)
        else:
            self._bind()

    def _bind(self) -> None:
        shortcut = (
            dbus.String("dictate"),
            dbus.Dictionary(
                {
                    "description": dbus.String("Dictate with Purr"),
                    "preferred_trigger": dbus.String("CTRL+ALT+space"),
                },
                signature="sv",
            ),
        )
        options = {"handle_token": dbus.String("purr_" + uuid.uuid4().hex[:12])}
        handle = self.interface.BindShortcuts(
            dbus.ObjectPath(self.session_handle),
            dbus.Array([shortcut], signature="(sa{sv})"),
            "",
            dbus.Dictionary(options, signature="sv"),
        )
        self._watch(handle, self._bound)

    def _bound(self, response, results) -> None:
        if response != 0:
            self.on_status("No global shortcut configured", True)
            return
        self._report(results.get("shortcuts", []))

    def _report(self, shortcuts) -> None:
        for shortcut_id, properties in shortcuts:
            if str(shortcut_id) == "dictate":
                description = str(properties.get("trigger_description", "Configured shortcut"))
                self.on_status(f"Ready — hold {description}", False)

    def _activated(self, session_handle, shortcut_id, _timestamp, _options) -> None:
        if str(session_handle) == self.session_handle and str(shortcut_id) == "dictate":
            self.on_press()

    def _deactivated(self, session_handle, shortcut_id, _timestamp, _options) -> None:
        if str(session_handle) == self.session_handle and str(shortcut_id) == "dictate":
            self.on_release()

    def _changed(self, session_handle, shortcuts) -> None:
        if str(session_handle) == self.session_handle:
            self._report(shortcuts)

    def configure(self) -> None:
        if self.session_handle:
            try:
                self.interface.ConfigureShortcuts(dbus.ObjectPath(self.session_handle), "", {})
            except dbus.DBusException as error:
                # GNOME currently exposes the v2 method in introspection even
                # when its selected backend only implements portal v1.
                if error.get_dbus_name() == "org.freedesktop.DBus.Error.UnknownMethod":
                    self.on_status(
                        "This GNOME version keeps the existing shortcut: "
                        "Press <Control><Alt>space",
                        False,
                    )
                else:
                    self.on_status(f"Could not open shortcut settings: {error}", True)

    def close(self) -> None:
        if not self.session_handle:
            return
        try:
            session = self.bus.get_object("org.freedesktop.portal.Desktop", self.session_handle)
            dbus.Interface(session, "org.freedesktop.portal.Session").Close()
        except dbus.DBusException:
            pass


class GnomeShortcut:
    """A native GNOME custom shortcut that emits one toggle action per press."""

    PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/purr/"
    SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

    def __init__(self, on_status):
        self.on_status = on_status
        self.binding = "<Control><Alt>space"

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
        self.on_status(f"Ready — tap {self.binding}", False)

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
        self.inserter: TextInserter | None = None
        self.shortcuts: GnomeShortcut | None = None
        self.recorder: subprocess.Popen | None = None
        self.recording_path: Path | None = None
        self.stream_thread: threading.Thread | None = None
        self.ready = False
        self.busy = False
        self.shutting_down = False
        self.shortcut_description: str | None = None
        self.last_inserted = ""
        self.stream_typed = ""
        self.stream_raw = ""
        self.stream_external_drops = 0
        self.stream_direct_failed = False
        self.shortcut_down = False
        self.shortcut_pressed_at = 0.0
        self.pending_toggle_stop = False
        self.recording_token = 0
        self.window = self.overlay = None
        self.status_label = self.last_label = self.overlay_label = None
        self.record_button = self.model_combo = self.model_action = None
        self.microphone_combo = None
        self.model_remove = self.model_description = self.download_progress = None
        self.indicator = None

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
            self.inserter = TextInserter(self.keyboard)
        except Exception as error:
            LOG.exception("uinput setup failed")
            self._set_status(f"Text injection unavailable: {error}", True)
        try:
            self.shortcuts = GnomeShortcut(self._shortcut_status)
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
        if not self.window:
            self._build_window()
        self.window.present()

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(b"""
            .purr-card { background: alpha(@theme_fg_color, 0.055); border-radius: 12px; padding: 16px; }
            .purr-live { background: rgba(24, 24, 28, 0.94); color: white; border-radius: 18px; padding: 18px 24px; }
            .purr-live label { color: white; font-size: 17px; }
            .purr-record { padding: 10px 22px; font-weight: bold; }
            .purr-model-title { font-weight: bold; font-size: 15px; }
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
        self.indicator_status = Gtk.MenuItem(label="Starting transcription engine…")
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
        dictate = Gtk.MenuItem(label="Start / Stop Dictation")
        dictate.connect("activate", lambda _item: self._toggle_recording(None))
        menu.append(dictate)
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
        self.status_label = Gtk.Label(label="Starting transcription engine…", xalign=0)
        self.status_label.set_line_wrap(True)
        status_card.pack_start(self.status_label, False, False, 0)
        self.last_label = Gtk.Label(
            label="Your live transcript will appear here while you speak.", xalign=0
        )
        self.last_label.set_line_wrap(True)
        self.last_label.set_selectable(True)
        self.last_label.get_style_context().add_class("dim-label")
        status_card.pack_start(self.last_label, True, True, 0)
        buttons = Gtk.Box(spacing=10)
        self.record_button = Gtk.Button(label="Start Dictation")
        self.record_button.get_style_context().add_class("suggested-action")
        self.record_button.get_style_context().add_class("purr-record")
        self.record_button.set_sensitive(False)
        self.record_button.connect("clicked", self._toggle_recording)
        buttons.pack_start(self.record_button, True, True, 0)
        shortcut_button = Gtk.Button(label="Shortcut…")
        shortcut_button.connect(
            "clicked", lambda _button: self.shortcuts and self.shortcuts.configure()
        )
        buttons.pack_start(shortcut_button, False, False, 0)
        status_card.pack_start(buttons, False, False, 0)
        outer.pack_start(status_card, True, True, 0)

        model_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        model_card.get_style_context().add_class("purr-card")
        model_title = Gtk.Label(label="Transcription model", xalign=0)
        model_title.get_style_context().add_class("purr-model-title")
        model_card.pack_start(model_title, False, False, 0)
        self.model_combo = Gtk.ComboBoxText()
        self._populate_model_combo(self.model.id)
        self.model_combo.connect("changed", self._model_changed)
        model_card.pack_start(self.model_combo, False, False, 0)
        self.model_description = Gtk.Label(xalign=0)
        self.model_description.set_line_wrap(True)
        self.model_description.get_style_context().add_class("dim-label")
        model_card.pack_start(self.model_description, False, False, 0)
        model_buttons = Gtk.Box(spacing=8)
        self.model_action = Gtk.Button()
        self.model_action.connect("clicked", self._model_action_clicked)
        model_buttons.pack_start(self.model_action, True, True, 0)
        self.model_remove = Gtk.Button(label="Remove")
        self.model_remove.connect("clicked", self._remove_model_clicked)
        model_buttons.pack_start(self.model_remove, False, False, 0)
        model_card.pack_start(model_buttons, False, False, 0)
        self.download_progress = Gtk.ProgressBar(show_text=True)
        self.download_progress.set_no_show_all(True)
        model_card.pack_start(self.download_progress, False, False, 0)
        outer.pack_start(model_card, False, False, 0)

        options = Gtk.Grid(column_spacing=14, row_spacing=10)
        options.set_border_width(4)
        live_label = Gtk.Label(label="Type finalized words directly into the focused app", xalign=0)
        live_label.set_tooltip_text(
            "Uses Purr's virtual keyboard, including Linux Unicode compose for multilingual text."
        )
        options.attach(live_label, 0, 0, 1, 1)
        live_switch = Gtk.Switch(active=bool(self.config.get("live_typing", True)))
        # Gtk.Grid gives this column the width requested by the long microphone
        # combo below it. A Switch defaults to filling that allocation, which
        # makes some GTK themes draw a comically wide track and thumb. Keep it
        # at its natural control size and align it with the other inputs.
        live_switch.set_halign(Gtk.Align.END)
        live_switch.set_valign(Gtk.Align.CENTER)
        live_switch.set_hexpand(False)
        live_switch.connect("notify::active", self._live_typing_changed)
        options.attach(live_switch, 1, 0, 1, 1)
        language_label = Gtk.Label(label="Recognition language", xalign=0)
        options.attach(language_label, 0, 1, 1, 1)
        language = Gtk.ComboBoxText()
        for code, name in (
            ("en", "English"), ("auto", "Automatic"), ("es", "Spanish"),
            ("de", "German"), ("fr", "French"), ("it", "Italian"),
            ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
        ):
            language.append(code, name)
        language.set_active_id(str(self.config.get("language", "en")))
        language.connect("changed", self._language_changed)
        options.attach(language, 1, 1, 1, 1)
        microphone_label = Gtk.Label(label="Microphone", xalign=0)
        options.attach(microphone_label, 0, 2, 1, 1)
        self.microphone_combo = Gtk.ComboBoxText()
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
        self.microphone_combo.append("default", f"System default — {default_label}")
        for name, label in microphones:
            self.microphone_combo.append(name, label)
        selected_microphone = str(self.config.get("microphone", "default"))
        if not self.microphone_combo.set_active_id(selected_microphone):
            self.microphone_combo.set_active_id("default")
        self.microphone_combo.connect("changed", self._microphone_changed)
        options.attach(self.microphone_combo, 1, 2, 1, 1)
        commands = Gtk.CheckButton(label='Enable voice commands, including “scratch that”')
        commands.set_active(bool(self.config.get("voice_commands", True)))
        commands.connect("toggled", self._commands_changed)
        options.attach(commands, 0, 3, 2, 1)
        outer.pack_start(options, False, False, 0)

        note = Gtk.Label(
            label="Purr starts when you sign in. Closing this window leaves it in the top bar.",
            xalign=0,
        )
        note.get_style_context().add_class("dim-label")
        outer.pack_end(note, False, False, 0)
        self.window.add(outer)
        self.window.show_all()
        self.download_progress.hide()
        self._refresh_model_ui()

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
        self.window.present()

    def _toggle_recording(self, _button) -> None:
        self.stop_recording() if self.recorder else self.start_recording()

    def _native_shortcut(self, _action, _parameter) -> None:
        now = time.monotonic()
        # GNOME normally invokes a custom shortcut once per press. If a keyboard
        # does repeat it, keep extending this guard so a held key cannot toggle
        # dictation back off.
        if now - self.shortcut_pressed_at < 0.8:
            self.shortcut_pressed_at = now
            return
        self.shortcut_pressed_at = now
        self.shortcut_down = True
        self.pending_toggle_stop = bool(self.recorder)
        # Give GNOME time to finish dispatching the shortcut before live typing
        # begins or the final stream tail is sent through the virtual keyboard.
        GLib.timeout_add(600, self._finish_native_shortcut)

    def _finish_native_shortcut(self) -> bool:
        self.shortcut_down = False
        if self.pending_toggle_stop:
            self.pending_toggle_stop = False
            self.stop_recording()
        else:
            self.start_recording()
        return False

    def _set_status(self, text: str, error: bool = False) -> bool:
        LOG.error(text) if error else LOG.info(text)
        if self.status_label:
            self.status_label.set_text(text)
        if hasattr(self, "indicator_status"):
            self.indicator_status.set_label(text[:100])
        if self.indicator:
            icon = "dialog-error-symbolic" if error else "audio-input-microphone-symbolic"
            self.indicator.set_icon_full(icon, text)
        return False

    def _ready_status(self) -> str:
        shortcut = self.shortcut_description or "the configured shortcut"
        return f"Ready — tap {shortcut}  ·  {self.server.backend_label}"

    def _shortcut_status(self, text: str, error: bool = False) -> bool:
        if error:
            return self._set_status(text, True)
        self.shortcut_description = text.removeprefix("Ready — hold ").removeprefix("Ready — tap ")
        return self._set_status(
            self._ready_status() if self.ready else
            f"Shortcut set to {self.shortcut_description}; loading model…"
        )

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
        self._set_status(self._ready_status())
        if self.record_button:
            self.record_button.set_sensitive(True)
        if hasattr(self, "indicator_model"):
            self.indicator_model.set_label(self.model.name)
        self._refresh_model_ui()
        return False

    def _server_failed(self, message: str) -> bool:
        self.ready = False
        self.busy = False
        self._set_status(message, True)
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
        if not self.ready or self.busy or self.recorder:
            return
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            source = self._default_source()
            self.stream_typed = ""
            self.stream_raw = ""
            self.stream_external_drops = 0
            self.stream_direct_failed = False
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
            self._set_status("Listening live… tap the shortcut again to finish")
            self.indicator.set_icon_full("media-record-symbolic", "Purr is listening")
            if self.record_button:
                self.record_button.set_label("Stop Dictation")
            self.recording_token += 1
            GLib.timeout_add_seconds(180, self._recording_watchdog, self.recording_token)
        except Exception as error:
            LOG.exception("Could not start microphone")
            self.recorder = None
            self._hide_overlay()
            self._set_status(f"Microphone error: {error}", True)
            self._notify("Purr microphone error", str(error))

    def stop_recording(self) -> None:
        if not self.recorder:
            return
        recorder = self.recorder
        self.recorder = None
        self.busy = True
        if self.record_button:
            self.record_button.set_label("Start Dictation")
            self.record_button.set_sensitive(False)
        self._set_status("Finalizing transcript…" if self.server.streaming else "Transcribing…")
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

    def _recording_watchdog(self, token: int) -> bool:
        if token == self.recording_token and self.recorder:
            LOG.error("Recording watchdog finalized a stuck three-minute session")
            self._notify("Purr finalized a stuck recording", "The stop shortcut was not received.")
            self.stop_recording()
        return False

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
                    peak = max(peak, max(abs(sample) for sample in samples))
                    sum_squares += sum(sample * sample for sample in samples)
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
            "Stream captured %.2fs, peak %.1f dBFS, RMS %.1f dBFS, raw=%r",
            duration, peak_db, rms_db, raw_text,
        )
        GLib.idle_add(self._stream_finished, raw_text, duration, peak_db, error)

    def _stream_update(self, raw: str, eou: bool, eob: bool, final: bool) -> bool:
        self.stream_raw = raw
        text, drop_previous = self.processor.apply(raw)
        preview = text or ("End of utterance…" if eou else "Listening…")
        if self.overlay_label:
            self.overlay_label.set_text(preview)
        if self.last_label:
            self.last_label.set_text(text or "Listening…")
        live_safe = (
            not self.shortcut_down
            and not self.pending_toggle_stop
        )
        if live_safe:
            self._apply_stream_typing(text, drop_previous)
        return False

    def _apply_stream_typing(self, text: str, drop_previous: int) -> None:
        direct = bool(self.config.get("live_typing", True)) and self.keyboard
        if direct and not self.stream_direct_failed:
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
            if addition and not self.keyboard.type_text(addition):
                # Roll the partial direct transcript back; final insertion will use
                # the Unicode-safe clipboard handoff.
                if common:
                    self.keyboard.backspace(common)
                self.stream_typed = ""
                self.stream_direct_failed = True
            else:
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
        self.recorder = None
        self.pending_toggle_stop = False
        if self.record_button:
            self.record_button.set_label("Start Dictation")
        if error:
            if self.stream_typed and self.keyboard:
                self.keyboard.backspace(len(self.stream_typed))
            return self._complete("", 0, error)
        if duration < 0.20:
            return self._complete("", 0, "Too short — speak for a moment before stopping")
        if peak_db < -55:
            if self.stream_typed and self.keyboard:
                self.keyboard.backspace(len(self.stream_typed))
            return self._complete("", 0, "No speech detected")
        self._stream_update(raw, False, False, True)
        text, drop_previous = self.processor.apply(raw)
        LOG.info("Live transcript final=%r", text)
        direct_ok = (
            bool(self.config.get("live_typing", True))
            and not self.stream_direct_failed
        )
        if direct_ok and self.keyboard:
            if text:
                self.keyboard.type_text(" ")
                self.last_inserted = text + " "
            self._complete("", 0, None)
        else:
            self._complete(
                text, max(0, drop_previous - self.stream_external_drops), None
            )
        if self.last_label and text:
            self.last_label.set_text(text)
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
            if peak_db < -55:
                GLib.idle_add(self._complete, "", 0, "No speech detected")
                return
            raw = self.server.transcribe(path, multipart_body)
            text, drop_previous = self.processor.apply(raw)
            LOG.info("Transcript raw=%r final=%r", raw, text)
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
        self.busy = False
        if self.record_button:
            self.record_button.set_sensitive(self.ready)
        if message:
            self._hide_overlay()
            self._set_status(message, message.startswith(("No microphone", "Transcription", "Could not")))
            if message not in ("No speech detected", "Too short — speak for a moment before stopping"):
                self._notify("Purr dictation failed", message)
            return False
        if drop_previous and self.keyboard:
            self.keyboard.backspace_word(drop_previous)
        if text:
            insertion = text if text.endswith(("\n", "\t", " ")) else text + " "
            inserted = bool(self.keyboard and self.keyboard.type_text(insertion))
            if not inserted and self.inserter:
                self.inserter.insert(insertion)
                inserted = True
            if inserted:
                self.last_inserted = insertion
            if self.last_label:
                self.last_label.set_text(text)
        self._set_status(self._ready_status())
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
            live = "  •  LIVE" if model.streaming else ""
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
        self.model_action.set_label(
            "In use" if active else "Use model" if installed else f"Download & use ({selected.size_label})"
        )
        self.model_action.set_sensitive(not active and not self.busy)
        self.model_remove.set_sensitive(installed and not active and selected.engine != "whisper")
        return False

    def _model_action_clicked(self, _button) -> None:
        selected = self._selected_model()
        if self.store.installed(selected):
            self._activate_model(selected)
            return
        self.busy = True
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
        self.busy = False
        if error:
            self._set_status(f"Download failed: {error}", True)
            self._refresh_model_ui()
        else:
            self._populate_model_combo(selected.id)
            self._activate_model(selected)
        return False

    def _activate_model(self, selected) -> None:
        if self.busy or self.recorder:
            return
        self.ready = False
        self.busy = True
        if self.record_button:
            self.record_button.set_sensitive(False)
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
            self._set_status(f"Removed {selected.name}")
        except Exception as error:
            self._set_status(str(error), True)
        self._populate_model_combo(selected.id)
        self._refresh_model_ui()

    def _live_typing_changed(self, switch, _property) -> None:
        self.config["live_typing"] = switch.get_active()
        save_config(self.config)

    def _language_changed(self, combo) -> None:
        if combo.get_active_id():
            self.config["language"] = combo.get_active_id()
            save_config(self.config)

    def _microphone_changed(self, combo) -> None:
        if combo.get_active_id():
            self.config["microphone"] = combo.get_active_id()
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
            if self.inserter:
                self.inserter.close()
            self.keyboard.close()
        Gtk.Application.do_shutdown(self)


def smoke_test() -> int:
    failures = []
    for description, path in (
        ("whisper-server", WHISPER_SERVER),
        ("Whisper model", MODEL),
        ("parakeet.cpp shared library", PARAKEET_LIB),
        ("Nemotron streaming model", NEMOTRON_MODEL),
        ("Wayland clipboard writer", WL_COPY),
        ("Wayland clipboard reader", WL_PASTE),
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
    DBusGMainLoop(set_as_default=True)
    app = PurrApplication(background)
    try:
        return app.run(sys.argv)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

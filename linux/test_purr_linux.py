import array
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock
import warnings
import wave

# purr_linux resolves its log and config paths at import time. Point them at a
# sandbox so tests never write the user's real purr.log or config.json.
SANDBOX = tempfile.mkdtemp(prefix="purr-tests-")
os.environ["XDG_STATE_HOME"] = os.path.join(SANDBOX, "state")
os.environ["XDG_CONFIG_HOME"] = os.path.join(SANDBOX, "config")

from gi.repository import GLib

import purr_linux
from purr_linux import (
    PostProcessor,
    PurrApplication,
    UInputKeyboard,
    multipart_body,
    normalize_wav,
    shortcut_label,
    transcript_preview,
)
from purr_models import DEFAULT_MODEL_ID, MODEL_BY_ID, ModelSpec, ModelStore


def tearDownModule():
    shutil.rmtree(SANDBOX, ignore_errors=True)


def run_main_loop(seconds):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    with warnings.catch_warnings():
        # PyGObject's asyncio shim warns about deprecated asyncio policy APIs on
        # Python 3.14 on every iteration; it is unrelated to Purr.
        warnings.simplefilter("ignore", DeprecationWarning)
        while time.monotonic() < deadline:
            if not context.iteration(False):
                time.sleep(0.005)


class ScreenKeyboard:
    """Applies virtual keyboard edits to a string, like the focused text field."""

    def __init__(self):
        self.screen = ""

    def type_text(self, text):
        self.screen += text
        return True

    def backspace(self, count):
        self.screen = self.screen[: max(0, len(self.screen) - count)]

    def backspace_word(self, count=1):
        for _ in range(count):
            self.screen = re.sub(r"\S+\s*$", "", self.screen)


def live_app(keyboard):
    engine = mock.Mock(streaming=True, backend_label="test engine")
    with mock.patch.object(
        purr_linux, "create_engine", return_value=(engine, MODEL_BY_ID[DEFAULT_MODEL_ID])
    ):
        app = PurrApplication(background=False)
    app.keyboard = keyboard
    app.ready = True
    app.notifications = []
    app._notify = lambda title, body: app.notifications.append((title, body))
    return app


class PostProcessorTests(unittest.TestCase):
    def setUp(self):
        self.processor = PostProcessor(
            {"trim_fillers": True, "voice_commands": True, "dictionary": {"type script": "TypeScript"}}
        )

    def test_cleanup_commands_and_dictionary(self):
        text, drop = self.processor.apply("Um, use type script comma new line done period")
        self.assertEqual(text, "use TypeScript,\ndone.")
        self.assertEqual(drop, 0)

    def test_scratch_that(self):
        self.assertEqual(self.processor.apply("Scratch that."), ("", 1))

    def test_inline_scratch_removes_one_word(self):
        self.assertEqual(
            self.processor.apply("This is very wrong scratch that right."),
            ("This is very right.", 0),
        )

    def test_repeated_scratch_and_external_word(self):
        self.assertEqual(
            self.processor.apply("one two scratch that scratch that scratch that"),
            ("", 1),
        )

    def test_nemotron_locale_tokens_are_metadata(self):
        self.assertEqual(
            self.processor.apply("Hello. <en-US> This is Purr. <en-US>"),
            ("Hello. This is Purr.", 0),
        )

    def test_output_is_always_typeable(self):
        text, _ = self.processor.apply("café\0 — 日本語\x1b 🐈 new line done")
        self.assertEqual(text, "café — 日本語 🐈\ndone")
        self.assertTrue(UInputKeyboard.__new__(UInputKeyboard).can_type(text))

    def test_transcript_preview_keeps_only_the_tail(self):
        self.assertEqual(transcript_preview("short"), "short")
        preview = transcript_preview("word " * 2000)
        self.assertLessEqual(len(preview), 281)
        self.assertTrue(preview.startswith("…"))


class AudioTests(unittest.TestCase):
    def test_normalization_boosts_quiet_pcm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quiet.wav"
            samples = array.array("h", [100, -100] * 1600)
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(samples.tobytes())
            peak_db, _ = normalize_wav(path)
            self.assertLess(peak_db, -40)
            with wave.open(str(path), "rb") as result:
                normalized = array.array("h", result.readframes(result.getnframes()))
            self.assertGreater(max(normalized), 20_000)

    def test_multipart_contains_audio_and_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.wav"
            path.write_bytes(b"RIFFaudio")
            body, content_type = multipart_body(path, {"language": "en"})
            self.assertIn(b'form-data; name="language"', body)
            self.assertIn(b"RIFFaudio", body)
            self.assertIn("boundary=", content_type)


class LinuxIntegrationTests(unittest.TestCase):
    def test_background_activation_does_not_build_a_focus_stealing_window(self):
        class BackgroundApp:
            background = True
            window = None
            built = False

            def _build_window(self):
                self.built = True

        app = BackgroundApp()
        PurrApplication.do_activate(app)
        self.assertFalse(app.background)
        self.assertFalse(app.built)

    def test_live_typing_waits_for_shortcut_modifiers_to_be_released(self):
        class Keyboard:
            def __init__(self):
                self.typed = []

            def type_text(self, text):
                self.typed.append(text)
                return True

        app = live_app(Keyboard())
        app.shortcut_down = True

        app._stream_update("hello", False, False, False)
        self.assertEqual(app.keyboard.typed, [])
        self.assertEqual(app.stream_typed, "")

        app.shortcut_down = False
        app._stream_update("hello", False, False, False)
        self.assertEqual(app.keyboard.typed, ["hello"])
        self.assertEqual(app.stream_typed, "hello")

    def test_shortcut_starts_on_press_and_types_once_modifiers_are_up(self):
        app = live_app(ScreenKeyboard())
        starts = []

        def start_recording():
            starts.append(time.monotonic())
            app.recorder = mock.Mock()

        app.start_recording = start_recording
        pressed = time.monotonic()
        app._native_shortcut(None, None)
        self.assertEqual(len(starts), 1)
        self.assertLess(starts[0] - pressed, 0.05)
        # Words recognized while Ctrl+Alt may still be held wait for release.
        app._stream_update("first words", False, False, False)
        self.assertEqual(app.keyboard.screen, "")
        # A repeating key extends the guard instead of toggling dictation off.
        app._native_shortcut(None, None)
        self.assertEqual(len(starts), 1)
        run_main_loop(0.8)
        self.assertFalse(app.shortcut_down)
        self.assertEqual(app.keyboard.screen, "first words")

    def test_stop_shortcut_types_final_words_after_modifiers_are_up(self):
        app = live_app(ScreenKeyboard())
        app.busy = True
        app._hold_typing_for_shortcut()
        app._stream_finished("final words", 4.0, -3.0, None)
        self.assertEqual(app.keyboard.screen, "")
        self.assertTrue(app.busy)
        run_main_loop(0.8)
        self.assertEqual(app.keyboard.screen, "final words ")
        self.assertFalse(app.busy)

    def test_final_new_paragraph_is_not_followed_by_a_space(self):
        app = live_app(ScreenKeyboard())
        app._stream_finished("first line new paragraph", 3.0, -3.0, None)
        self.assertEqual(app.keyboard.screen, "first line\n\n")

    def test_tap_that_cannot_start_explains_why(self):
        app = live_app(ScreenKeyboard())
        app.ready = False
        app.start_recording()
        self.assertIsNone(app.recorder)
        self.assertEqual(app.notifications[-1][0], "Purr isn't ready")
        self.assertIn("loading", app.notifications[-1][1])
        app.ready, app.busy = True, True
        app.start_recording()
        self.assertIn("finishing", app.notifications[-1][1])
        app.ready, app.busy, app.engine_error = False, False, "no Vulkan device"
        app.start_recording()
        self.assertIn("no Vulkan device", app.notifications[-1][1])

    def test_shortcut_is_shown_the_way_people_type_it(self):
        self.assertEqual(shortcut_label("<Control><Alt>space"), "Ctrl+Alt+Space")
        app = live_app(ScreenKeyboard())
        app._shortcut_changed("<Super>d")
        self.assertIn("tap Super+D to dictate", app.status_text)
        app._shortcut_changed("")
        self.assertIn("set a shortcut", app.status_text)

    def test_missing_virtual_keyboard_is_never_reported_as_ready(self):
        app = live_app(None)
        app._shortcut_changed("<Control><Alt>space")
        app._server_ready()
        self.assertIn("uinput", app.status_text)
        self.assertTrue(app.status_error)

    def test_tests_never_touch_the_real_log_or_config(self):
        self.assertTrue(str(purr_linux.STATE).startswith(SANDBOX))
        self.assertTrue(str(purr_linux.CONFIG_FILE).startswith(SANDBOX))

    def test_long_live_stream_types_every_word_in_place(self):
        app = live_app(ScreenKeyboard())
        raw = ""
        # About fifteen minutes of dictation at a normal speaking rate.
        with self.assertLogs("purr", "INFO"):
            for index in range(2200):
                raw += f" word{index}"
                if index % 50 == 49:
                    raw += " comma"
                if index % 400 == 399:
                    raw += " oops scratch that"
                if index % 10 == 9:
                    app._stream_update(raw, False, False, False)
            app._stream_finished(raw, 900.0, -3.0, None)
        expected, _ = app.processor.apply(raw)
        self.assertEqual(app.keyboard.screen, expected + " ")
        self.assertNotIn("oops", app.keyboard.screen)
        self.assertIn("word2199", app.keyboard.screen)

    def test_stream_error_keeps_already_typed_text(self):
        app = live_app(ScreenKeyboard())
        app._stream_update("minutes of finalized dictation", False, False, False)
        app._stream_finished("minutes of finalized dictation", 600.0, -3.0, "engine failed")
        self.assertEqual(app.keyboard.screen, "minutes of finalized dictation")
        self.assertEqual(app.notifications, [("Purr dictation failed", "engine failed")])

    def test_dictation_requires_the_virtual_keyboard(self):
        app = live_app(None)
        app.start_recording()
        app.server.begin_stream.assert_not_called()
        self.assertIsNone(app.recorder)
        self.assertEqual(len(app.notifications), 1)

    def test_retired_live_typing_toggle_is_dropped_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.json"
            config_file.write_text(json.dumps({"live_typing": False, "language": "de"}))
            with mock.patch.object(purr_linux, "CONFIG_FILE", config_file):
                config = purr_linux.load_config()
        self.assertNotIn("live_typing", config)
        self.assertEqual(config["language"], "de")

    def test_live_keyboard_supports_unicode_and_rejects_controls(self):
        keyboard = UInputKeyboard.__new__(UInputKeyboard)
        self.assertTrue(keyboard.can_type("Purr live, scratch that!\n"))
        self.assertTrue(keyboard.can_type("café — 日本語 🐈"))
        self.assertFalse(keyboard.can_type("bad\0control"))

    def test_model_is_installed_only_at_exact_size(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ModelStore(Path(directory))
            model = ModelSpec("test", "Test", "test.gguf", 3, "", "")
            store.models_dir.mkdir(parents=True)
            store.path(model).write_bytes(b"no")
            self.assertFalse(store.installed(model))
            store.path(model).write_bytes(b"yes")
            self.assertTrue(store.installed(model))


if __name__ == "__main__":
    unittest.main()

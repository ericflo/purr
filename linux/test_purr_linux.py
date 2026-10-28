import array
import json
import tempfile
from pathlib import Path
import re
import unittest
from unittest import mock
import wave

import purr_linux
from purr_linux import (
    PostProcessor,
    PurrApplication,
    UInputKeyboard,
    multipart_body,
    normalize_wav,
    transcript_preview,
)
from purr_models import ModelSpec, ModelStore


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
    app = PurrApplication.__new__(PurrApplication)
    app.config = {}
    app.processor = PostProcessor(app.config)
    app.keyboard = keyboard
    app.server = mock.Mock(streaming=True, backend_label="test engine")
    app.shortcut_description = None
    app.overlay = app.overlay_label = app.last_label = None
    app.status_label = app.record_button = app.indicator = None
    app.stream_raw = app.stream_typed = ""
    app.stream_external_drops = 0
    app.pending_toggle_stop = False
    app.shortcut_down = False
    app.shutting_down = False
    app.ready = True
    app.busy = False
    app.recorder = None
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

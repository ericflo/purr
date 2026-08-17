import array
import tempfile
from pathlib import Path
import unittest
import wave

from purr_linux import (
    PostProcessor,
    PurrApplication,
    UInputKeyboard,
    multipart_body,
    normalize_wav,
)
from purr_models import ModelSpec, ModelStore


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
    def test_live_typing_waits_for_shortcut_modifiers_to_be_released(self):
        class Keyboard:
            def __init__(self):
                self.typed = []

            def type_text(self, text):
                self.typed.append(text)
                return True

        app = PurrApplication.__new__(PurrApplication)
        app.config = {"live_typing": True}
        app.processor = PostProcessor(app.config)
        app.keyboard = Keyboard()
        app.overlay_label = app.last_label = None
        app.stream_raw = app.stream_typed = ""
        app.stream_external_drops = 0
        app.stream_direct_failed = False
        app.pending_toggle_stop = False
        app.shortcut_down = True

        app._stream_update("hello", False, False, False)
        self.assertEqual(app.keyboard.typed, [])
        self.assertEqual(app.stream_typed, "")

        app.shortcut_down = False
        app._stream_update("hello", False, False, False)
        self.assertEqual(app.keyboard.typed, ["hello"])
        self.assertEqual(app.stream_typed, "hello")

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

import array
import base64
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import warnings

# purr_linux resolves its log and config paths at import time. Point them at a
# sandbox so tests never write the user's real purr.log or config.json.
SANDBOX = tempfile.mkdtemp(prefix="purr-tests-")
os.environ["XDG_STATE_HOME"] = os.path.join(SANDBOX, "state")
os.environ["XDG_CONFIG_HOME"] = os.path.join(SANDBOX, "config")

from gi.repository import GLib

import purr_linux
from purr_linux import Gtk
from purr_linux import (
    PostProcessor,
    PurrApplication,
    UInputKeyboard,
    shortcut_label,
    transcript_preview,
)
from purr_models import (
    DEFAULT_MODEL_ID,
    MODEL_BY_ID,
    MODELS,
    LlamaRealtimeEngine,
    ModelFile,
    ModelSpec,
    ModelStore,
    ParakeetEngine,
    ParakeetStream,
    RealtimeStream,
    create_engine,
)


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


class UnmappedWindow(Gtk.Window):
    """A real window whose contents are shown but which never maps on the desktop."""

    def show_all(self):
        self.get_child().show_all()


def windowed_app(keyboard=None):
    app = live_app(keyboard or ScreenKeyboard())
    app.server.backend_label = "Vulkan · parakeet.cpp"
    with mock.patch.object(purr_linux.Gtk, "ApplicationWindow", lambda application: UnmappedWindow()), \
            mock.patch.object(purr_linux, "list_microphones", return_value=[]), \
            mock.patch.object(purr_linux.subprocess, "run", side_effect=OSError):
        app._build_window()
    return app


class ScriptedEngine:
    """Stands in for parakeet.cpp: each stream emits scripted text, one entry per feed."""

    def __init__(self, scripts):
        self.scripts = list(scripts)  # per stream: (feed texts, finalize text)
        self.lib = self
        self.handles = 0

    def begin_handle(self):
        self.handles += 1
        feeds, closing = self.scripts.pop(0)
        self.current = (iter(feeds), closing)
        return self.handles

    def parakeet_capi_stream_feed_json(self, _handle, _floats, _count):
        return {"text": next(self.current[0], "")}

    def parakeet_capi_stream_finalize_json(self, _handle):
        return {"text": self.current[1]}

    def parakeet_capi_stream_free(self, _handle):
        pass


def scripted_stream(scripts):
    engine = ScriptedEngine(scripts)
    stream = ParakeetStream(engine, engine.begin_handle(), renew_at_pauses=True)
    stream._take_json = lambda result: result
    return stream


SPEECH = array.array("h", [9000, -9000] * 1600).tobytes()  # 200 ms, loud
SILENCE = bytes(6400)  # 200 ms


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

    def test_lowercase_models_get_a_capital_pronoun_i(self):
        processor = PostProcessor({"language": "en", "voice_commands": False, "trim_fillers": False})
        raw = "okay i think i'm sure, i.e. what i'd say if i'll lose i"
        self.assertEqual(processor.apply(raw)[0], raw)  # models that punctuate are left alone
        processor.capitalize_pronoun = True
        self.assertEqual(
            processor.apply(raw)[0], "okay I think I'm sure, i.e. what I'd say if I'll lose I"
        )
        processor.config["language"] = "it"  # Italian "i" is an article
        self.assertEqual(processor.apply("i gatti")[0], "i gatti")

    def test_only_lowercase_models_ask_for_the_pronoun_fix(self):
        self.assertFalse(MODEL_BY_ID["eou-streaming-q5"].punctuates)
        self.assertTrue(MODEL_BY_ID["nemotron-streaming-q5"].punctuates)
        app = live_app(ScreenKeyboard())
        self.assertFalse(app.processor.capitalize_pronoun)
        app.model = MODEL_BY_ID["eou-streaming-q5"]
        app._server_ready()
        self.assertTrue(app.processor.capitalize_pronoun)

    def test_transcript_preview_keeps_only_the_tail(self):
        self.assertEqual(transcript_preview("short"), "short")
        preview = transcript_preview("word " * 2000)
        self.assertLessEqual(len(preview), 281)
        self.assertTrue(preview.startswith("…"))


class StreamRenewalTests(unittest.TestCase):
    def feed(self, stream, chunks):
        return "".join(stream.feed_s16(chunk)["text"] for chunk in chunks)

    def test_long_streams_renew_at_a_pause_and_read_as_one_transcript(self):
        first = [" So"] + [" word"] * 44  # 9 s of speech
        stream = scripted_stream([(first, "."), ([" Next", " part"], ""), ([], "")])
        text = self.feed(stream, [SPEECH] * 45 + [SILENCE] * 5)
        self.assertEqual(stream.renewals, 1)
        self.assertTrue(text.endswith("word."))
        text += self.feed(stream, [SPEECH] * 2) + stream.finalize()["text"]
        # A closed sentence keeps the fresh stream's capital.
        self.assertTrue(text.endswith("word. Next part"))

    def test_mid_sentence_renewal_continues_in_lowercase_without_fusing_words(self):
        stream = scripted_stream([([" my"] + [" account"] * 44 + [" has"], ""), (["Access", " to"], ""), ([], "")])
        text = self.feed(stream, [SPEECH] * 46 + [SILENCE] * 5 + [SPEECH] * 2)
        self.assertEqual(stream.renewals, 1)
        self.assertTrue(text.endswith("account has access to"), text[-40:])

    def test_names_and_i_keep_their_capitals_after_renewal(self):
        for word in ("I", "I'm", "OpenAI", "NASA"):
            stream = scripted_stream([([" so"] * 45, ""), ([" " + word], ""), ([], "")])
            text = self.feed(stream, [SPEECH] * 45 + [SILENCE] * 5 + [SPEECH])
            self.assertTrue(text.endswith(" " + word), (word, text[-20:]))

    def test_no_renewal_during_speech_or_short_streams(self):
        # Text pauses while the audio stays loud: text lags speech, so no cut.
        stream = scripted_stream([([" word"] * 45, ""), ([], "")])
        self.feed(stream, [SPEECH] * 60)
        self.assertEqual(stream.renewals, 0)
        # A quiet pause in the first seconds keeps the stream.
        stream = scripted_stream([([" word"] * 10, ""), ([], "")])
        self.feed(stream, [SPEECH] * 10 + [SILENCE] * 10)
        self.assertEqual(stream.renewals, 0)

    def test_only_models_that_ask_for_it_renew(self):
        self.assertTrue(MODEL_BY_ID["nemotron-streaming-q5"].renew_at_pauses)
        self.assertFalse(MODEL_BY_ID["eou-streaming-q5"].renew_at_pauses)


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

    def test_passing_remarks_return_to_ready(self):
        app = live_app(ScreenKeyboard())
        app._shortcut_changed("<Control><Alt>space")
        app._flash_status("No speech detected", 50)
        self.assertEqual(app.status_text, "No speech detected")
        run_main_loop(0.2)
        self.assertTrue(app.status_text.startswith("Ready — tap Ctrl+Alt+Space"))

    def test_passing_remark_never_hides_a_newer_status(self):
        app = live_app(ScreenKeyboard())
        app._flash_status("Removed a model", 50)
        app._set_status("Listening…")
        run_main_loop(0.2)
        self.assertEqual(app.status_text, "Listening…")

    def test_filler_toggle_applies_to_the_next_words(self):
        app = live_app(ScreenKeyboard())
        self.assertEqual(app.processor.apply("um hello")[0], "hello")
        app._fillers_changed(mock.Mock(get_active=lambda: False))
        self.assertEqual(app.processor.apply("um hello")[0], "um hello")
        self.assertFalse(json.loads(purr_linux.CONFIG_FILE.read_text())["trim_fillers"])

    def test_unplugged_microphone_stays_selected_by_name(self):
        app = live_app(ScreenKeyboard())
        app.microphone_combo = Gtk.ComboBoxText()
        app.microphone_combo.connect("changed", app._microphone_changed)
        yeti = ("alsa_input.usb-yeti", "Yeti Stereo Microphone")

        def populate(microphones):
            with mock.patch.object(purr_linux, "list_microphones", return_value=microphones), \
                    mock.patch.object(purr_linux.subprocess, "run", side_effect=OSError):
                app._populate_microphones()

        populate([yeti])
        app.microphone_combo.set_active_id(yeti[0])
        populate([])
        self.assertEqual(app.microphone_combo.get_active_id(), yeti[0])
        self.assertEqual(
            app.microphone_combo.get_active_text(), "Yeti Stereo Microphone (not connected)"
        )
        # Re-choosing the unplugged entry must not remember the suffix as its name.
        app.microphone_combo.set_active_id("default")
        app.microphone_combo.set_active_id(yeti[0])
        populate([])
        self.assertEqual(
            app.microphone_combo.get_active_text(), "Yeti Stereo Microphone (not connected)"
        )
        saved = json.loads(purr_linux.CONFIG_FILE.read_text())
        self.assertEqual(saved["microphone"], yeti[0])
        self.assertEqual(saved["microphone_label"], "Yeti Stereo Microphone")

    def test_active_model_shows_one_clear_state_not_disabled_buttons(self):
        app = windowed_app()
        self.assertTrue(app.model_state.get_visible())
        self.assertIn("In use", app.model_state.get_text())
        self.assertIn("Vulkan · parakeet.cpp", app.model_state.get_text())
        self.assertFalse(app.model_buttons.get_visible())
        with mock.patch.object(app.store, "installed", return_value=True):
            app.model_combo.set_active_id("eou-streaming-q5")
        self.assertFalse(app.model_state.get_visible())
        self.assertTrue(app.model_buttons.get_visible())
        self.assertEqual(app.model_action.get_label(), "Use model")
        self.assertTrue(app.model_remove.get_visible())
        with mock.patch.object(app.store, "installed", return_value=False):
            app.model_combo.set_active_id("voxtral-realtime-q4")
        self.assertEqual(app.model_action.get_label(), "Download & use (3.4 GB)")
        # Nothing to remove before it is downloaded.
        self.assertFalse(app.model_remove.get_visible())

    def test_language_setting_is_greyed_out_for_models_that_ignore_it(self):
        app = windowed_app()
        self.assertTrue(app.language_combo.get_sensitive())
        for model_id, reason in (
            ("eou-streaming-q5", "is English only"),
            ("voxtral-realtime-q4", "detects the language itself"),
        ):
            app.model = MODEL_BY_ID[model_id]
            app._refresh_model_ui()
            self.assertFalse(app.language_combo.get_sensitive())
            self.assertIn(reason, app.language_combo.get_tooltip_text())

    def test_window_scrolls_only_on_screens_too_short_for_it(self):
        def page_policy(screen_height):
            monitor = mock.Mock()
            monitor.get_workarea.return_value = mock.Mock(height=screen_height)
            display = mock.Mock(get_primary_monitor=lambda: monitor)
            with mock.patch.object(purr_linux.Gdk.Display, "get_default", return_value=display):
                app = windowed_app()
            return app.window.get_child().get_policy()[1]

        self.assertEqual(page_policy(1080), Gtk.PolicyType.NEVER)
        self.assertEqual(page_policy(600), Gtk.PolicyType.AUTOMATIC)

    def test_window_gives_back_the_height_of_a_hidden_description(self):
        app = windowed_app()
        app.window.get_child().set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        app.window.resize = mock.Mock()
        app.model_combo.set_active_id("eou-streaming-q5")
        self.assertTrue(app.model_description.get_visible())
        app.window.resize.assert_not_called()
        app.model_combo.set_active_id(app.model.id)
        self.assertFalse(app.model_description.get_visible())
        app.window.resize.assert_called_once()
        # A scrolling page keeps the size the screen allows.
        app.window.get_child().set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        app.model_combo.set_active_id("eou-streaming-q5")
        app.model_combo.set_active_id(app.model.id)
        app.window.resize.assert_called_once()

    def test_status_shows_a_matching_state_indicator(self):
        app = windowed_app()
        app._show_ready()
        self.assertTrue(app.status_icon.get_style_context().has_class("purr-ready"))
        self.assertEqual(app.status_text, "Ready — set a shortcut with Shortcut…")
        app.recorder = mock.Mock()
        app._sync_record_controls()
        app._set_status("Listening…")
        self.assertTrue(app.status_icon.get_style_context().has_class("purr-listening"))
        # While listening the main button is a red Stop, not the calm blue Start.
        button = app.record_button.get_style_context()
        self.assertTrue(button.has_class("destructive-action"))
        self.assertFalse(button.has_class("suggested-action"))
        app.recorder, app.busy = None, True
        app._set_status("Finalizing transcript…")
        self.assertTrue(app.status_spinner.get_visible())
        self.assertFalse(app.status_icon.get_visible())
        app.busy = False
        app._set_status("Download failed", True)
        self.assertTrue(app.status_icon.get_style_context().has_class("purr-error"))
        self.assertFalse(app.status_spinner.get_visible())

    def test_practice_placeholder_hides_once_words_arrive(self):
        app = windowed_app()
        self.assertTrue(app.practice_placeholder.get_visible())
        app.practice.get_buffer().set_text("Hello")
        self.assertFalse(app.practice_placeholder.get_visible())
        app.practice.get_buffer().set_text("")
        self.assertTrue(app.practice_placeholder.get_visible())

    def test_microphone_that_stops_on_its_own_is_reported(self):
        app = live_app(ScreenKeyboard())
        app.recorder = mock.Mock()  # still set: Purr never asked it to stop
        app._stream_update("words so far", False, False, False)
        app._stream_finished("words so far", 12.0, -6.0, None)
        self.assertIsNone(app.recorder)
        self.assertEqual(app.keyboard.screen, "words so far")
        self.assertTrue(app.status_error)
        self.assertIn("stopped unexpectedly", app.notifications[-1][1])

    def test_microphone_that_never_starts_is_not_called_too_short(self):
        app = live_app(ScreenKeyboard())
        app.recorder = mock.Mock()
        app._stream_finished("", 0.0, -120.0, None)
        self.assertTrue(app.status_error)
        self.assertIn("Could not record", app.status_text)

    def test_dictated_words_stay_out_of_the_log_by_default(self):
        app = live_app(ScreenKeyboard())
        with self.assertLogs("purr", "INFO") as logs:
            app._stream_finished("my bank pin is four four", 3.0, -3.0, None)
        self.assertNotIn("bank", "\n".join(logs.output))
        self.assertIn("<6 words>", "\n".join(logs.output))
        app.config["log_transcripts"] = True
        with self.assertLogs("purr", "INFO") as logs:
            app._stream_finished("diagnose this", 3.0, -3.0, None)
        self.assertIn("diagnose this", "\n".join(logs.output))

    def test_silent_microphone_is_flagged_within_seconds_and_clears_on_sound(self):
        app = windowed_app()
        app.config["microphone"] = "usb.yeti"
        app.recorder = recorder = mock.Mock()
        app._sync_record_controls()
        self.assertTrue(app.level_meter.get_visible())
        self.assertTrue(all(part.get_visible() for part in app.level_meter.get_children()))
        with mock.patch.object(purr_linux, "list_microphones", return_value=[("usb.yeti", "Yeti")]):
            app._hear(recorder, 0, 1.0)
            self.assertEqual(app.notifications, [])
            app._hear(recorder, 0, purr_linux.SILENCE_WARNING_SECONDS)
            app._hear(recorder, 0, purr_linux.SILENCE_WARNING_SECONDS + 0.2)
        self.assertTrue(app.status_error)
        self.assertIn("silence from Yeti", app.status_text)
        self.assertEqual(len(app.notifications), 1)
        app._hear(recorder, 8000, 3.6)
        self.assertFalse(app.status_error)
        self.assertTrue(app.status_text.startswith("Listening"))
        self.assertGreater(app.level_bar.get_value(), 0.5)
        # Pausing after speech is normal and never warns.
        app._hear(recorder, 0, 30.0)
        self.assertFalse(app.status_error)
        # Chunks from a dictation that already ended change nothing.
        app._hear(mock.Mock(), 0, 99.0)
        self.assertEqual(len(app.notifications), 1)
        app.recorder = None
        app._sync_record_controls()
        self.assertFalse(app.level_meter.get_visible())

    def test_empty_replacements_editor_opens_ready_to_type(self):
        app = windowed_app()
        app.config["dictionary"] = {}
        app._show_replacements_summary()
        self.assertIn("None yet", app.replacements_summary.get_text())
        dialog = app._build_replacements_dialog()
        self.assertEqual(len(dialog.replacement_rows), 1)
        self.assertIs(dialog.get_focus(), dialog.replacement_rows[0][0])
        self.assertEqual(app.config["dictionary"], {})
        dialog.destroy()

    def test_replacements_are_edited_in_the_window_and_apply_at_once(self):
        app = windowed_app()
        app.config["dictionary"] = {"purr": "Purr"}
        app._show_replacements_summary()
        self.assertEqual(app.replacements_summary.get_text(), "1 replacement")
        dialog = app._build_replacements_dialog()
        rows = dialog.replacement_rows
        self.assertEqual([(s.get_text(), t.get_text()) for s, t, _ in rows], [("purr", "Purr")])
        self.assertIsInstance(dialog.get_focus(), Gtk.Button)  # not the existing entry
        dialog.add_replacement().set_text(" type script ")
        # Half-filled: Purr must not start deleting "type script" yet.
        self.assertEqual(app.processor.apply("use type script")[0], "use type script")
        rows[-1][1].set_text("TypeScript")
        self.assertEqual(app.processor.apply("use type script")[0], "use TypeScript")
        self.assertEqual(app.replacements_summary.get_text(), "2 replacements")
        saved = json.loads(purr_linux.CONFIG_FILE.read_text())["dictionary"]
        self.assertEqual(saved, {"purr": "Purr", "type script": "TypeScript"})
        rows[0][2].clicked()  # the first row's delete button
        self.assertEqual(app.config["dictionary"], {"type script": "TypeScript"})
        self.assertEqual(len(rows), 1)
        # Rows below a removed one move up, keeping the table compact.
        self.assertEqual(rows[0][0].get_parent().child_get_property(rows[0][0], "top-attach"), 1)
        dialog.destroy()

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

    def test_retired_settings_are_dropped_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.json"
            config_file.write_text(json.dumps(
                {"live_typing": False, "initial_prompt": "Whisper only", "language": "de"}
            ))
            with mock.patch.object(purr_linux, "CONFIG_FILE", config_file):
                config = purr_linux.load_config()
        self.assertNotIn("live_typing", config)
        self.assertNotIn("initial_prompt", config)
        self.assertEqual(config["language"], "de")

    def test_live_keyboard_supports_unicode_and_rejects_controls(self):
        keyboard = UInputKeyboard.__new__(UInputKeyboard)
        self.assertTrue(keyboard.can_type("Purr live, scratch that!\n"))
        self.assertTrue(keyboard.can_type("café — 日本語 🐈"))
        self.assertFalse(keyboard.can_type("bad\0control"))

    def test_model_is_installed_only_with_every_file_at_exact_size(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ModelStore(Path(directory))
            model = ModelSpec(
                "test", "Test", (ModelFile("a.gguf", 3, "", ""), ModelFile("b.gguf", 2, "", "")), "", ""
            )
            store.models_dir.mkdir(parents=True)
            store.path(model, 0).write_bytes(b"yes")
            self.assertFalse(store.installed(model))
            store.path(model, 1).write_bytes(b"n")
            self.assertFalse(store.installed(model))
            store.path(model, 1).write_bytes(b"ok")
            self.assertTrue(store.installed(model))
            store.remove(model)
            self.assertEqual(list(store.models_dir.iterdir()), [])


class ModelCatalogTests(unittest.TestCase):
    def test_every_model_types_live_from_a_pinned_download(self):
        self.assertEqual(DEFAULT_MODEL_ID, "nemotron-streaming-q5")
        for model in MODELS:
            self.assertIn(model.engine, ("parakeet", "llama"))
            for file in model.files:
                # A revision hash, never a moving branch such as "main".
                self.assertRegex(file.url, r"/resolve/[0-9a-f]{40}/")
                self.assertGreater(file.size, 0)
                self.assertRegex(file.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(len(MODEL_BY_ID["voxtral-realtime-q4"].files), 2)

    def test_download_fetches_every_file_and_reports_progress_across_them(self):
        with tempfile.TemporaryDirectory() as directory:
            served = Path(directory) / "served"
            served.mkdir()
            (served / "model.gguf").write_bytes(b"m" * 3000)
            (served / "projector.gguf").write_bytes(b"p" * 1000)
            digest = lambda name: hashlib.sha256((served / name).read_bytes()).hexdigest()
            handler = functools.partial(QuietHandler, directory=str(served))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            source = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                model = ModelSpec(
                    "two", "Two",
                    (
                        ModelFile("model.gguf", 3000, digest("model.gguf"), source),
                        ModelFile("projector.gguf", 1000, digest("projector.gguf"), source),
                    ),
                    "", "",
                )
                store = ModelStore(Path(directory) / "build")
                store.models_dir.mkdir(parents=True)
                # A stale partial file from an interrupted download is replaced.
                (store.models_dir / "model.gguf.part").write_bytes(b"stale")
                reports = []
                store.download(model, lambda done, total: reports.append((done, total)))
                self.assertTrue(store.installed(model))
                self.assertEqual(reports[-1], (4000, 4000))
                self.assertEqual([done for done, _ in reports], sorted(done for done, _ in reports))
                # Right size, wrong bytes: refused, and nothing half-trusted is left behind.
                (served / "tampered.gguf").write_bytes(b"x" * 1000)
                tampered = ModelSpec(
                    "bad", "Bad", (ModelFile("tampered.gguf", 1000, digest("projector.gguf"), source),), "", ""
                )
                with self.assertRaisesRegex(RuntimeError, "checksum"):
                    store.download(tampered)
                self.assertFalse(store.installed(tampered))
                self.assertFalse((store.models_dir / "tampered.gguf.part").exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_a_missing_model_falls_back_to_the_default(self):
        store = mock.Mock(installed=lambda model: model.id == DEFAULT_MODEL_ID)
        store.path = lambda model, index=0: Path(model.files[index].name)
        engine, model = create_engine({"model": "voxtral-realtime-q4"}, Path("/b"), Path("/s"), store)
        self.assertEqual(model.id, DEFAULT_MODEL_ID)
        self.assertIsInstance(engine, ParakeetEngine)
        store.installed = lambda model: True
        engine, model = create_engine({"model": "voxtral-realtime-q4"}, Path("/b"), Path("/s"), store)
        self.assertIsInstance(engine, LlamaRealtimeEngine)
        self.assertEqual(engine.projector.name, "mmproj-Voxtral-Mini-4B-Realtime-2602-Q8_0.gguf")
        store.installed = lambda model: False
        with self.assertRaises(RuntimeError):
            create_engine({"model": "voxtral-realtime-q4"}, Path("/b"), Path("/s"), store)


class StaleServerTests(unittest.TestCase):
    def test_an_orphaned_server_is_stopped_but_nothing_else_is(self):
        with tempfile.TemporaryDirectory() as directory:
            build, state = Path(directory) / "build", Path(directory) / "state"
            binary = build / "llama.cpp" / "build" / "bin" / "llama-server"
            binary.parent.mkdir(parents=True)
            shutil.copy(shutil.which("sleep"), binary)  # an executable that keeps its own argv[0]
            state.mkdir()
            engine = LlamaRealtimeEngine(build, Path(), Path(), state)
            # A PID now belonging to some other program is left alone.
            bystander = purr_linux.subprocess.Popen(["sleep", "30"])
            engine.pid_file.write_text(str(bystander.pid))
            engine.reap_stale()
            self.assertIsNone(bystander.poll())
            self.assertFalse(engine.pid_file.exists())
            bystander.kill()
            bystander.wait()
            # Our own orphaned llama-server is stopped.
            orphan = purr_linux.subprocess.Popen([str(binary), "30"])
            time.sleep(0.2)
            engine.pid_file.write_text(str(orphan.pid))
            with self.assertLogs("purr", "WARNING"):
                create_engine(
                    {}, build, state, mock.Mock(installed=lambda model: True, path=lambda m, i=0: Path("x"))
                )
            self.assertIsNotNone(orphan.wait(timeout=5))
            self.assertFalse(engine.pid_file.exists())


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class FakeRealtimeServer(threading.Thread):
    """A local /v1/realtime endpoint: each audio chunk gets the next scripted delta,
    and a final commit gets the session's closing text and a completed event."""

    def __init__(self, sessions, rate=16000):
        super().__init__(daemon=True)
        self.sessions = list(sessions)  # per session: (deltas, closing text)
        self.rate = rate
        self.opened = 0
        self.audio_bytes = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.start()

    def run(self):
        while self.sessions:
            connection, _ = self.listener.accept()
            deltas, closing = self.sessions.pop(0)
            self.opened += 1
            with connection:
                self.serve(connection, iter(deltas), closing)

    @staticmethod
    def read_exact(connection, count):
        data = b""
        while len(data) < count:
            chunk = connection.recv(count - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def receive(self, connection):
        first, second = self.read_exact(connection, 2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self.read_exact(connection, 2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self.read_exact(connection, 8))[0]
        assert second & 0x80, "client frames must be masked"
        mask = self.read_exact(connection, 4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self.read_exact(connection, size)))
        return None if first & 0x0F == 0x8 else json.loads(payload)

    @staticmethod
    def send(connection, message):
        payload = json.dumps(message).encode()
        size = len(payload)
        header = bytes([0x81, size]) if size < 126 else bytes([0x81, 126]) + struct.pack("!H", size)
        connection.sendall(header + payload)

    def serve(self, connection, deltas, closing):
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        assert b"GET /v1/realtime?intent=transcription " in request
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", request)[1]
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        connection.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        self.send(connection, {
            "type": "session.created", "session": {"audio": {"input": {"format": {"rate": self.rate}}}},
        })
        while True:
            try:
                event = self.receive(connection)
            except ConnectionError:
                return
            if event is None:
                return
            if event["type"] == "input_audio_buffer.append":
                self.audio_bytes += len(base64.b64decode(event["audio"]))
                delta = next(deltas, "")
                if delta == "ERROR":
                    self.send(connection, {"type": "error", "error": {"message": "model failed"}})
                elif delta:
                    self.send(connection, {
                        "type": "conversation.item.input_audio_transcription.delta", "delta": delta,
                    })
            elif event["type"] == "input_audio_buffer.commit":
                if closing:
                    self.send(connection, {
                        "type": "conversation.item.input_audio_transcription.delta", "delta": closing,
                    })
                self.send(connection, {"type": "conversation.item.input_audio_transcription.completed"})


class RealtimeStreamTests(unittest.TestCase):
    def feed(self, stream, chunks):
        text = ""
        for chunk in chunks:
            text += stream.feed_s16(chunk)["text"]
            time.sleep(0.002)  # let the local server answer, as a real one does between chunks
        return text

    def test_audio_streams_up_and_words_come_back(self):
        server = FakeRealtimeServer([([" Hello", " there,", " friend"], ".")])
        stream = RealtimeStream(server.port)
        text = self.feed(stream, [SPEECH] * 3) + stream.finalize()["text"]
        stream.close()
        self.assertEqual(text, " Hello there, friend.")
        # Every 200 ms chunk arrived intact through masked 16-bit-length frames.
        self.assertEqual(server.audio_bytes, 3 * len(SPEECH))

    def test_long_dictation_moves_to_a_fresh_session_at_a_pause(self):
        server = FakeRealtimeServer([([" my"] + [" account"] * 44 + [" has"], ""), (["Access", " to"], ""), ([], "")])
        stream = RealtimeStream(server.port)
        stream.RENEW_AFTER = 8.0
        text = self.feed(stream, [SPEECH] * 46 + [SILENCE] * 8 + [SPEECH] * 3)
        text += stream.finalize()["text"]
        stream.close()
        self.assertEqual(stream.renewals, 1)
        self.assertEqual(server.opened, 2)
        self.assertTrue(text.endswith("account has access to"), text[-40:])

    def test_a_session_renews_even_without_a_pause_before_its_context_fills(self):
        server = FakeRealtimeServer([([" word"] * 30, ""), ([" more"], ""), ([], "")])
        stream = RealtimeStream(server.port)
        stream.RENEW_LIMIT = 5.0
        self.feed(stream, [SPEECH] * 30)
        stream.close()
        self.assertEqual(stream.renewals, 1)

    def test_errors_from_the_model_are_raised(self):
        server = FakeRealtimeServer([([" fine", "ERROR"], "")])
        stream = RealtimeStream(server.port)
        with self.assertRaisesRegex(RuntimeError, "model failed"):
            self.feed(stream, [SPEECH] * 4)
        stream.close()

    def test_a_model_wanting_another_sample_rate_is_refused(self):
        server = FakeRealtimeServer([([], "")], rate=24000)
        with self.assertRaisesRegex(RuntimeError, "24000 Hz"):
            RealtimeStream(server.port)


if __name__ == "__main__":
    unittest.main()

# Purr on Linux

The Linux port provides private, local dictation as a GTK 3 application with a
top-bar indicator. It records through PipeWire, transcribes live and locally
with `parakeet.cpp` or `llama.cpp`, and types results into the focused
application through a `/dev/uinput` virtual keyboard.

## Support status

The current supported and tested combination is:

- x86_64 Arch Linux
- GNOME on Wayland
- a working Vulkan 1.3 driver
- PipeWire with PulseAudio compatibility

The code is intentionally small and may work elsewhere, but the documented
prerequisites use `pacman`, and the global shortcut integration uses GNOME
GSettings. X11, other desktop environments,
other distributions, and ARM Linux are not currently tested.

Linux currently includes dictation, live typing, model selection, microphone
selection, filler trimming, a custom replacement dictionary, spoken
punctuation, and “scratch that.” The macOS meeting, summarization, and voice-edit
modes have not been ported.

## Prerequisites

Install the build and desktop dependencies:

```bash
sudo pacman -S --needed \
  base-devel git cmake ninja curl libarchive \
  python-gobject gtk3 libayatana-appindicator \
  gnome-shell-extension-appindicator \
  pipewire-audio pipewire-pulse libpulse libnotify \
  vulkan-headers spirv-headers shaderc vulkan-icd-loader vulkan-tools
```

Install one Vulkan implementation for the active GPU:

```bash
# AMD
sudo pacman -S --needed vulkan-radeon

# Intel
sudo pacman -S --needed vulkan-intel

# NVIDIA proprietary driver
sudo pacman -S --needed nvidia-utils
```

If the Purr icon does not appear in GNOME's top bar, enable the installed shell
extension:

```bash
gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

Confirm that Vulkan can see the GPU:

```bash
vulkaninfo --summary
```

Purr also needs read/write access to `/dev/uinput` to create its virtual
keyboard. On a normal systemd-logind desktop session, the active local user gets
this through the device's `uaccess` ACL. Check it without using `sudo`:

```bash
test -r /dev/uinput && test -w /dev/uinput && echo "uinput is ready"
```

If that prints nothing, fix the distribution's udev/logind configuration for
the `uinput` device before installing. Avoid running Purr as root. Adding a user
to the broad `input` group also grants access to physical input devices and is
not recommended as the first fix.

## Build and install

```bash
git clone https://github.com/ericflo/purr.git
cd purr
make linux-install
purr
```

`make linux-install` performs these steps:

1. Clones pinned `parakeet.cpp` and `llama.cpp` release tags into
   `.build-linux/` and builds both with Vulkan (only `llama-server` from
   `llama.cpp`). Changing a pinned tag rebuilds that engine from a clean clone.
2. Downloads the default Nemotron streaming model and verifies its SHA-256.
3. Runs the Python unit tests and a runtime smoke test.
4. Adds a `~/.local/bin/purr` symlink, desktop entry, GNOME sign-in autostart
   entry, and the GNOME custom shortcut.

The launcher points back into the checkout, so keep the repository in the same
location after installation. The default model is a 748 MiB download; the
complete build directory is currently about 1.9 GiB and can vary by toolchain.

Run checks again at any time with:

```bash
make linux-check
```

## Using Purr

Launch Purr from GNOME's app grid or run `purr`. It remains in the top bar when
the settings window is closed and starts automatically on future sign-ins.

1. Focus a normal text field.
2. Tap <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> and start speaking right
   away: the microphone opens the moment the shortcut fires.
3. Finalized words are typed into the focused app as they are decoded, starting
   once the shortcut keys are released. There is no time limit; pauses do not
   end dictation. Nemotron punctuates and capitalizes as you go; Purr renews
   its stream at natural pauses so punctuation does not fade during long
   dictation.
4. Tap the shortcut again to finish. Recording stops on the tap, and the last
   words are typed as soon as the keys are up.

If Purr cannot start yet (the model is still loading, the previous dictation is
still finishing, or the engine failed), the tap shows a notification explaining
why instead of doing nothing. If the microphone stops or disappears during
dictation, Purr keeps the words already typed and says that dictation ended.
The top-bar menu leads with **Start Dictation**, which switches to **Stop
Dictation** while Purr is listening.

If the microphone delivers only silence for the first three seconds (muted,
switched off, or the wrong device), Purr warns right away with a notification
instead of waiting until you stop. The warning clears as soon as it hears
sound, and dictation keeps running either way.

The Purr window has a practice box: **Start Dictation** in the window types
into it, so you can try dictation, voice commands, and microphones without
touching another app. While listening, a level meter beside the status shows
what the microphone hears. On screens too short for the whole window, such as
1366×768 laptops, the window scrolls instead of cutting off its settings.

Click **Shortcut…** in Purr to open GNOME's keyboard settings. Purr preserves a
shortcut changed there and shows the new one immediately. All text is typed
through Purr's virtual keyboard; Purr never reads or writes the clipboard.
Optional model downloads continue in the background while you dictate.

The microphone list refreshes each time the window opens, so newly connected
devices appear without restarting Purr. A chosen microphone that is unplugged
stays selected (marked “not connected”) and Purr records from the system
default until it returns.

Spoken commands include “new paragraph,” “new line,” punctuation names, and
“scratch that.” The last command removes the preceding dictated word, including
one already typed by the current live stream.

**Replacements → Edit…** fixes words Purr mishears or spells the wrong way: when
Purr hears the words on the left, it types the text on the right (for example,
“type script” → “TypeScript”). Matching ignores case and applies to whole words.
Changes apply to the next words you dictate, with no restart.

## Models

Every model types live while you speak; models that only transcribe after you
stop are not offered. The settings window downloads, switches, and removes
them, and verifies each download against its published SHA-256.

| Model | Download | Languages | Output | GPU memory | Speed on a Radeon 8060S |
| --- | ---: | --- | --- | ---: | --- |
| NVIDIA Nemotron 3.5 Streaming Q5 (default) | 748 MiB | 40+ locales, chosen in the language menu | Punctuated and capitalized | about 0.8 GiB | about 11× real time |
| Mistral Voxtral Mini 4B Realtime Q4_K_M | 3.4 GiB | 13, detected automatically | Punctuated and capitalized, about 0.5 s behind your voice | about 5.6 GiB | about 3× real time |
| NVIDIA Parakeet Realtime EOU 120M Q5 | 135 MiB | English | Lowercase without punctuation | about 0.2 GiB | about 13× real time |

- **Nemotron** is the best balance and the default. Purr renews its stream at
  natural pauses so punctuation does not fade during long dictation.
- **Voxtral** is the largest live model. It handles Arabic, Chinese, Dutch,
  English, French, German, Hindi, Italian, Japanese, Korean, Portuguese,
  Russian, and Spanish without a language setting, so the language menu is
  greyed out while it is in use. It needs a GPU fast enough to stay ahead of
  your voice; on a slower GPU the words fall behind and the end of a dictation
  takes longer to arrive. Purr moves long dictation to a fresh Voxtral session
  at a pause every few minutes to keep its memory bounded.
- **Realtime EOU** is the lightest, for modest GPUs. It writes lowercase text
  without punctuation; Purr capitalizes the pronoun “I” for it when the
  recognition language is English.

Nemotron and Realtime EOU run in-process through
[`parakeet.cpp`](https://github.com/mudler/parakeet.cpp) with GGUF conversions
from [`mudler/parakeet-cpp-gguf`](https://huggingface.co/mudler/parakeet-cpp-gguf).
Voxtral runs in a local `llama-server` from
[`llama.cpp`](https://github.com/ggml-org/llama.cpp), which Purr starts on a
`127.0.0.1` port and stops when you switch models, using the
[`ggml-org/Voxtral-Mini-4B-Realtime-2602-GGUF`](https://huggingface.co/ggml-org/Voxtral-Mini-4B-Realtime-2602-GGUF)
conversion. Each model keeps the terms of its original model card.

Earlier versions also offered Parakeet TDT V2, TDT V3, TDT-CTC 110M, and
Whisper Large V3 Turbo, which transcribe only after you stop. A configuration
that still selects one of them starts Nemotron instead. Their files in
`.build-linux/models/` and the old `.build-linux/whisper.cpp/` build are no
longer used and can be deleted.

The language menu exposes English, automatic detection, Spanish, German,
French, Italian, Portuguese, Dutch, and Polish for Nemotron.

## Local data and network access

Purr has no account, analytics, crash reporter, cloud transcription, or remote
service. The build needs network access for source code, Arch packages, and
model weights. Downloading another model from the settings window contacts
Hugging Face. Inference itself stays on the machine; while Voxtral is in use,
its `llama-server` listens only on an ephemeral `127.0.0.1` port. Recorded audio
streams straight from PipeWire to the engine and is never written to disk.

Linux files are stored in these locations:

| Path | Contents |
| --- | --- |
| `<checkout>/.build-linux/` | Compiled engines and downloaded models |
| `~/.config/purr/config.json` | Model, language, microphone, and dictation preferences |
| `~/.local/state/purr/purr.log` | Rotating application log |
| `~/.local/state/purr/llama-server.log` | Voxtral engine log |
| `~/.local/share/applications/com.arunbrahma.purr.desktop` | Per-user launcher |
| `~/.config/autostart/com.arunbrahma.purr.desktop` | Per-user sign-in autostart entry |

The application log records timings, audio levels, and word counts, not the
words you dictate. To diagnose recognition problems, set `log_transcripts` to
`true` in the configuration; the log then contains dictated text in plaintext,
so turn it off again and delete the log afterwards. Logs written by Purr
versions before this setting existed may still contain dictated text.

## Configuration

The GUI writes `~/.config/purr/config.json`. Supported keys are:

- `model`: a model ID from `linux/purr_models.py`
- `language`: recognition language code or `auto`
- `microphone`: PipeWire/Pulse source name or `default`
- `microphone_label`: friendly name of that source, shown while it is unplugged
- `trim_fillers`: remove common fillers such as “um” and “uh”
- `voice_commands`: enable spoken punctuation and “scratch that”
- `dictionary`: JSON object mapping spoken forms to replacements (edited by
  **Replacements → Edit…**)
- `log_transcripts`: include dictated text in `purr.log` (default `false`)
- `show_overlay`: show a non-focusable listening overlay

Quit Purr before editing this file by hand so the GUI cannot overwrite the
change.

## Updating and uninstalling

Linux does not have a binary release or automatic updater yet. Quit Purr from
the top-bar menu, then update and rebuild in the checkout:

```bash
git pull --ff-only
make linux-install
purr
```

To uninstall, quit Purr, remove the three installed per-user files listed
above, and remove the “Purr Dictation” custom shortcut in GNOME Settings. The
checkout can then be deleted to remove compiled engines and model weights.
Delete `~/.config/purr/` and `~/.local/state/purr/` as well if preferences and
logs should not be retained.

## Troubleshooting

- Run `make linux-check`; it reports missing runtime binaries, model files,
  microphone setup, and `/dev/uinput` access.
- Inspect `~/.local/state/purr/purr.log` for application errors and
  `llama-server.log` for Voxtral startup failures.
- If no microphone appears, check `pactl get-default-source` and verify that
  PipeWire PulseAudio compatibility is running.
- If the shortcut does nothing, open GNOME Settings → Keyboard → Custom
  Shortcuts and confirm that “Purr Dictation” invokes
  `gapplication action com.arunbrahma.purr dictate`.
- If direct typing fails, recheck `/dev/uinput` access. Purr types only through
  its virtual keyboard, so dictation will not start without it.
- If Voxtral's words fall further and further behind your voice, the GPU is
  not fast enough for it; switch back to Nemotron.

Please include the Linux distribution, desktop, session type, GPU, selected
model, and output from `make linux-check` in a bug report. Review logs first and
redact dictated text before posting them publicly.

# Purr on Linux

The Linux port provides private, local dictation as a GTK 3 application with a
top-bar indicator. It records through PipeWire, transcribes locally with
`parakeet.cpp` or `whisper.cpp`, and types results into the focused application
through a `/dev/uinput` virtual keyboard.

## Support status

The current supported and tested combination is:

- x86_64 Arch Linux
- GNOME on Wayland
- a working Vulkan 1.3 driver
- PipeWire with PulseAudio compatibility

The code is intentionally small and may work elsewhere, but the installer is
Arch-specific (`pacman` is used to obtain `wl-clipboard`), and the global
shortcut integration uses GNOME GSettings. X11, other desktop environments,
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
  python-gobject python-dbus gtk3 libayatana-appindicator \
  gnome-shell-extension-appindicator \
  pipewire-audio pipewire-pulse libpulse libnotify \
  vulkan-headers shaderc vulkan-icd-loader vulkan-tools
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

1. Clones pinned `parakeet.cpp` and `whisper.cpp` release tags into
   `.build-linux/` and builds both with Vulkan.
2. Downloads the default Nemotron streaming model and the Whisper fallback.
3. Extracts a private copy of Arch's `wl-clipboard` package into the build
   directory; it does not install that package system-wide.
4. Runs the Python unit tests and a runtime smoke test.
5. Adds a `~/.local/bin/purr` symlink, desktop entry, GNOME sign-in autostart
   entry, and the GNOME custom shortcut.

The launcher points back into the checkout, so keep the repository in the same
location after installation. The initial model downloads total about 1.3 GiB;
the complete build directory is currently about 2.3 GiB and can vary by toolchain.

Run checks again at any time with:

```bash
make linux-check
```

## Using Purr

Launch Purr from GNOME's app grid or run `purr`. It remains in the top bar when
the settings window is closed and starts automatically on future sign-ins.

1. Focus a normal text field.
2. Tap <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> to begin.
3. Speak. With live typing enabled, finalized words appear as they are decoded.
4. Tap the shortcut again to finish the stream.

Click **Shortcut…** in Purr to open GNOME's keyboard settings. Purr preserves a
shortcut changed there. The **Type finalized words directly into the focused
app** switch disables incremental typing; the final result is inserted when the
utterance ends instead.

Spoken commands include “new paragraph,” “new line,” punctuation names, and
“scratch that.” The last command removes the preceding dictated word, including
one already typed by the current live stream.

## Models

The settings window can download, activate, and remove the optional Parakeet
models. Whisper is installed as the built-in compatibility fallback and is not
removable from the window.

| Model | Approx. size | Mode | Languages |
| --- | ---: | --- | --- |
| NVIDIA Nemotron 3.5 Streaming Q5 | 748 MiB | Live, default | 40+ prompted locales |
| NVIDIA Parakeet Realtime EOU 120M Q5 | 135 MiB | Live | English |
| NVIDIA Parakeet TDT 0.6B V2 Q5 | 672 MiB | Batch | English |
| NVIDIA Parakeet TDT 0.6B V3 Q5 | 708 MiB | Batch | 25 European languages |
| NVIDIA Parakeet TDT-CTC 110M Q5 | 137 MiB | Batch | English |
| Whisper Large V3 Turbo Q5 | 548 MiB | Batch fallback | Multilingual |

Nemotron and Parakeet run through
[`parakeet.cpp`](https://github.com/mudler/parakeet.cpp). The Q5 GGUF conversions
come from [`mudler/parakeet-cpp-gguf`](https://huggingface.co/mudler/parakeet-cpp-gguf)
and retain the terms of their original NVIDIA model cards. Whisper runs through
[`whisper.cpp`](https://github.com/ggml-org/whisper.cpp).

The language menu currently exposes English, automatic detection, Spanish,
German, French, Italian, Portuguese, Dutch, and Polish. `initial_prompt` and
dictionary replacements can also be set directly in the JSON configuration.

## Local data and network access

Purr has no account, analytics, crash reporter, cloud transcription, or remote
service. The build needs network access for source code, Arch packages, and
model weights. Downloading another model from the settings window contacts
Hugging Face. Inference itself stays on the machine; Whisper's HTTP server binds
only to an ephemeral `127.0.0.1` port.

Linux files are stored in these locations:

| Path | Contents |
| --- | --- |
| `<checkout>/.build-linux/` | Compiled engines, downloaded models, and private `wl-clipboard` binaries |
| `~/.config/purr/config.json` | Model, language, microphone, and dictation preferences |
| `~/.local/state/purr/purr.log` | Rotating application log |
| `~/.local/state/purr/whisper-server.log` | Whisper engine log |
| `$XDG_RUNTIME_DIR/purr/` | Temporary batch audio, removed after transcription |
| `~/.local/share/applications/com.arunbrahma.purr.desktop` | Per-user launcher |
| `~/.config/autostart/com.arunbrahma.purr.desktop` | Per-user sign-in autostart entry |

The application log includes recognized text for diagnostics. It never leaves
the machine, but it is plaintext and may contain sensitive dictated content.
Remove or protect that log as you would a transcript.

## Configuration

The GUI writes `~/.config/purr/config.json`. Supported keys are:

- `model`: a model ID from `linux/purr_models.py`
- `language`: recognition language code or `auto`
- `microphone`: PipeWire/Pulse source name or `default`
- `live_typing`: type stable partial results while streaming
- `trim_fillers`: remove common fillers such as “um” and “uh”
- `voice_commands`: enable spoken punctuation and “scratch that”
- `dictionary`: JSON object mapping spoken forms to replacements
- `initial_prompt`: optional Whisper prompt
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
  `whisper-server.log` for Whisper startup failures.
- If no microphone appears, check `pactl get-default-source` and verify that
  PipeWire PulseAudio compatibility is running.
- If the shortcut does nothing, open GNOME Settings → Keyboard → Custom
  Shortcuts and confirm that “Purr Dictation” invokes
  `gapplication action com.arunbrahma.purr dictate`.
- If direct typing fails, recheck `/dev/uinput` access. The final insertion can
  fall back to a short-lived Wayland clipboard handoff, but live typing needs
  the virtual keyboard.
- If a batch model feels unresponsive, remember that it emits text only after
  the stop shortcut. Use Nemotron or Realtime EOU for live output.

Please include the Linux distribution, desktop, session type, GPU, selected
model, and output from `make linux-check` in a bug report. Review logs first and
redact dictated text before posting them publicly.

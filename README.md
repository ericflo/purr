<div align="center">

<img src="Resources/purr_app_logo.png" alt="Purr" width="180" height="180">

# Purr

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![macOS 14+](https://img.shields.io/badge/macOS-14%2B-black?logo=apple)](#)
[![Linux: GNOME Wayland](https://img.shields.io/badge/Linux-GNOME%20Wayland-4a86cf?logo=linux)](LINUX.md)
[![Apple Silicon](https://img.shields.io/badge/Apple%20Silicon-arm64-orange)](#)
[![Swift 5.9+](https://img.shields.io/badge/Swift-5.9%2B-orange?logo=swift)](#)
[![Local ASR](https://img.shields.io/badge/ASR-Parakeet%20%2B%20Whisper-purple)](#)

### Press a shortcut. Speak. Your words type into any text field - 100% local, on-device.

</div>

Local voice-to-text dictation for macOS Apple Silicon and GNOME/Wayland Linux.
A free, MIT-licensed alternative to paid dictation apps, with no telemetry and
no cloud transcription.

> [!NOTE]
> This is a Linux-focused fork of [Arun Brahma's original Purr](https://github.com/iamarunbrahma/purr).
> The original macOS application, name, artwork, and most of the project are
> Arun's work. This fork adds the native Linux implementation while preserving
> the original MIT license and copyright notice.

## macOS screenshots

<table>
  <tr>
    <td align="center" width="50%">
      <img src="Resources/screenshots/onboarding.png" alt="Onboarding and permissions"><br>
      <sub>Onboarding &amp; permissions</sub>
    </td>
    <td align="center" width="50%">
      <img src="Resources/screenshots/general-settings.png" alt="General settings"><br>
      <sub>General settings</sub>
    </td>
  </tr>
  <tr>
    <td align="center">
      <img src="Resources/screenshots/features-settings.png" alt="Features settings"><br>
      <sub>Features settings</sub>
    </td>
    <td align="center">
      <img src="Resources/screenshots/customization-settings.png" alt="Customization settings"><br>
      <sub>Customization settings</sub>
    </td>
  </tr>
</table>

## Quick start

### Linux (GNOME Wayland)

The Linux port is currently source-built and tested on x86_64 Arch Linux with
GNOME Wayland and Vulkan. Install the dependencies and the Vulkan driver for
your GPU first; the complete package list and troubleshooting notes are in
[LINUX.md](LINUX.md).

```bash
git clone https://github.com/ericflo/purr.git
cd purr
make linux-install
purr
```

The first build downloads and compiles pinned versions of `parakeet.cpp` and
`whisper.cpp`, then downloads about 1.3 GiB of default model weights. It also
installs a per-user desktop launcher, sign-in autostart entry, and the native
GNOME <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> shortcut. Tap the shortcut
to start dictation and tap it again to stop.

### macOS (Apple Silicon, upstream)

The signed macOS release and its updater are maintained by the original
project. For that version, build or download upstream Purr:

```bash
git clone https://github.com/iamarunbrahma/purr.git
cd purr
make app
mv dist/Purr.app /Applications/
open /Applications/Purr.app
```

First launch:

1. Walks you through Microphone, Accessibility, and Input Monitoring permissions.
2. Click into any text field, hold **Right Option**, speak, release.
3. First launch downloads the ~450 MB Parakeet model in the background; once it finishes, dictation is near-instant.

## Hotkeys

On Linux, tap <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> to start or stop
dictation. The settings window links to GNOME's keyboard shortcut settings.

The macOS app provides these additional modes:

| Action      | Default | Behavior                                       |
| ----------- | ------- | ---------------------------------------------- |
| Dictation   | hold ⌥  | Hold to talk, release to paste/type.           |
| Meeting     | ⌃⌥ M    | Tap to start/stop. Captures mic + system audio. |
| Voice edit  | ⌃⌥ E    | Select text, hold, speak the edit, release.    |

While a meeting is recording, the dictation hotkey is suspended so a reflexive press won't spray a transcript over your notes. Each hotkey can be switched between a few built-in presets in Settings.

**In-speech voice commands:** "new paragraph", "comma", "period", "question mark", "exclamation mark", "scratch that".

**Voice-edit phrases:** "change X to Y", "replace X with Y", "delete X", "add Y at the end", "prepend Y", "capitalize", "lowercase", "uppercase". Anything else replaces the selection wholesale.

## Features

- **Two transcription engines**: Parakeet TDT v2 (default) is English-only, on-device, and the fastest option on Apple Silicon. Switch to WhisperKit (Whisper Tiny EN through Large V3 Turbo) when you need another language.
- **Native Linux live transcription**: Nemotron 3.5 Streaming is the default on Linux, with cache-aware partial results, punctuation, capitalization, prompt-selected multilingual recognition, and a Whisper Large V3 Turbo fallback.
- **Wayland-safe text injection**: Finalized words are typed through a local virtual keyboard. Unicode compose handles accents, symbols, and non-Latin text without relying on background clipboard access.
- **Smart Typing**: With Parakeet, words appear live in the focused app as you speak instead of only landing on key release. Each phrase stays separately undoable and coexists cleanly with autocorrect and IMEs.
- **Meeting mode**: Captures your mic and your Mac's system audio together, so everyone on a call is transcribed. Echo cancellation and offline speaker diarization (FluidAudio) label each voice, and transcripts save as Markdown to a folder you choose.
- **Meeting summaries**: Each meeting can save a sidecar `.summary.md` with a TL;DR, decisions, action items, and notes. Uses Apple's on-device model on macOS 26+, or Gemma 3 4B locally on older systems.
- **Voice editing**: Select text, hold the voice-edit hotkey, and speak the change. A parser handles "change X to Y", "delete X", "capitalize", and more; anything else replaces the selection wholesale.
- **In-speech voice commands**: Punctuate, break paragraphs, and undo hands-free by saying "comma", "period", "new paragraph", or "scratch that" as you dictate. Add your own in Settings.
- **Custom dictionary**: Teach Purr the proper nouns and acronyms it keeps mishearing - map "fluid audio" to "FluidAudio" or "ts" to "TypeScript".
- **Filler trimming**: "um", "uh", and "er" are stripped automatically; add your own filler words in Settings.
- **Flexible hotkeys**: Pick each feature's trigger from a few built-in presets - a bare modifier, a modifier combo, or a function key.
- **Native and private**: Swift/SwiftUI on macOS and Python/GTK on Linux (no Electron or Tauri), with inference running on-device and zero telemetry.

## Compatibility

| Platform | Status |
| --- | --- |
| Arch Linux x86_64, GNOME Wayland, Vulkan | Supported by this fork as a source build |
| Other Linux distributions or desktops | Not packaged or tested yet |
| macOS 14+ on Apple Silicon (M1 or newer) | Supported by the original app |
| macOS on Intel or macOS 13 and earlier | Not supported; the macOS engine requires CoreML/ANE and Sonoma+ APIs |
| Windows, iOS, and iPadOS | Not supported |

On Linux, `make linux-install` builds pinned `parakeet.cpp` and `whisper.cpp`
engines with Vulkan, downloads NVIDIA Nemotron 3.5 Streaming Q5 plus Whisper
Large V3 Turbo Q5 as a fallback, installs the user launcher and autostart entry,
and runs the test and smoke-test suite.

Nemotron is the default: after the first tap, cache-aware transcription is typed
directly into the focused application through Purr's virtual keyboard. Linux
Unicode compose is used for accents, symbols, and non-Latin text, avoiding
background Wayland clipboard restrictions. Say “scratch that” to remove the
preceding dictated word immediately. The second tap finalizes the stream. The
GUI can download, select, and remove these additional local models:

- NVIDIA Parakeet Realtime EOU 120M (English live streaming)
- NVIDIA Parakeet TDT 0.6B V2 (English batch accuracy)
- NVIDIA Parakeet TDT 0.6B V3 (25-language batch accuracy)
- NVIDIA Parakeet TDT-CTC 110M (small and fast)
- Whisper Large V3 Turbo (broad-language compatibility fallback)

Purr starts automatically on sign-in and keeps running in the top bar when its
window is closed. See [the Linux guide](LINUX.md) for prerequisites, model
sizes, settings, local files, update/uninstall steps, architecture, and common
failure modes.

## FAQs

<details>
<summary><strong>Does my audio leave my computer?</strong></summary>

No. Recording, transcription, and post-processing happen on-device. macOS uses
the Apple Neural Engine; Linux uses local Vulkan inference (or Whisper's CPU
fallback). There is no telemetry and no cloud transcription. Network access is
used to fetch source dependencies, models, and macOS updates.

</details>

<details>
<summary><strong>Which mode is lowest-latency?</strong></summary>

Use Parakeet with live typing enabled, so finalized words appear as you speak.
On Linux, the default Nemotron model and optional Realtime EOU model stream;
the batch Parakeet models and Whisper wait until the utterance finishes.

</details>

<details>
<summary><strong>What languages can I dictate in?</strong></summary>

On macOS, the default Parakeet engine is English-only; WhisperKit supports
roughly 100 languages. On Linux, Nemotron supports 40+ prompted locales, the
Parakeet V3 option supports 25 European languages, and Whisper provides the
broadest compatibility. Select the language and model in the window.

</details>

<details>
<summary><strong>Does meeting mode record the other people on my call?</strong></summary>

On macOS, yes: it captures your microphone and system audio together, still
entirely on-device. System-audio capture needs macOS 14.2+. Meeting mode has not
been ported to Linux.

</details>

<details>
<summary><strong>The mic icon isn't visible in my menu bar.</strong></summary>

On notched MacBooks the icon can hide behind the notch when the menu bar is
crowded. On GNOME, make sure the AppIndicator extension is enabled and
`libayatana-appindicator` is installed; [LINUX.md](LINUX.md) includes both
packages.

</details>

<details>
<summary><strong>How big is the model download?</strong></summary>

On macOS, the default Parakeet model is about 450 MB. The Linux build downloads
about 1.3 GiB for Nemotron and the Whisper fallback and currently occupies
about 2.3 GiB including compiled engines; optional Linux models are 135–708 MB
each.

</details>

<details>
<summary><strong>Does it work without internet?</strong></summary>

Yes, after the first model download. Everything runs on-device.

</details>

<details>
<summary><strong>Can I change the hotkey?</strong></summary>

On macOS, pick from the built-in presets in Settings. On Linux, click
**Shortcut…** to open GNOME keyboard settings; the installed default is
<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd>.

</details>

<details>
<summary><strong>Can I use it in any app?</strong></summary>

Dictation works in standard text fields. On Linux, direct typing works in GTK,
Chromium/Electron, and other applications that accept normal keyboard input;
Unicode compose support varies by toolkit. Voice editing is currently macOS-only.

</details>

<details>
<summary><strong>How do I update Purr?</strong></summary>

On Linux, quit Purr and run `git pull --ff-only && make linux-install` in the
checkout. On macOS, open **About Purr > Check for Updates**; the signed upstream
release downloads, verifies its checksum, and relaunches in place.

</details>

<details>
<summary><strong>Why was I asked to grant permissions again after an update?</strong></summary>

Either the bundle ID changed (e.g. you swapped in a different fork) or you revoked a permission in System Settings. Re-grant from the menu bar > Onboarding Setup.

</details>

<details>
<summary><strong>How do I delete the models or fully uninstall Purr?</strong></summary>

For Linux, follow [the update and uninstall guide](LINUX.md#updating-and-uninstalling).
The steps below apply to macOS.

To reclaim disk space without uninstalling, open **Settings > Customization > Models** and click **Delete Models**. It removes every downloaded model (Parakeet, Whisper, the diarizer, and Gemma) while keeping your meeting transcripts and preferences; the models re-download automatically the next time a feature needs them.

To uninstall completely, quit Purr, drag `Purr.app` to the Trash, then remove its data. Everything Purr writes - all model weights and your meeting transcripts - lives under one folder:

```bash
rm -rf ~/Library/Application\ Support/Purr   # models + meeting transcripts
defaults delete com.arunbrahma.purr          # preferences
```

macOS can't run cleanup code when an app is dragged to the Trash, so those are the only leftovers. Because every model now lives under that single folder, app-cleaner utilities that search for "Purr" find all of it too.

</details>

## Project lineage and license

This fork is based on [Purr by Arun Brahma](https://github.com/iamarunbrahma/purr).
The Linux port lives in `linux/` plus the Linux targets in the `Makefile`; the
commit history records the fork's changes. Upstream sponsorship remains linked
through [`.github/FUNDING.yml`](.github/FUNDING.yml).

Purr itself is MIT-licensed. Copyright (c) 2026 Arun Brahma. See [LICENSE](LICENSE).

### Third-party licenses

Purr builds on open-source work in two categories, handled differently.

**Model weights** download on first use and are never redistributed by Purr; each carries its own terms:

- **Parakeet TDT v2** (NVIDIA, run via FluidAudio) - CC-BY-4.0. Powers the default English dictation, voice editing, and meeting transcription.
- **Whisper** (OpenAI, via WhisperKit) - MIT. Optional engine for other languages.
- **Gemma 3 4B Instruct** (Google) - [Gemma Terms of Use](https://ai.google.dev/gemma/terms). Used for meeting summaries. Settings surfaces this notice before the download begins; by downloading the model you accept those terms, including the [Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy).

**Bundled code** is compiled or linked into the app:

- **WhisperKit** (Argmax) - MIT.
- **FluidAudio** (FluidInference) - Apache-2.0. Runs Parakeet transcription and the meeting-mode speaker diarization model.
- **llama.cpp** (ggml-org) - MIT, embedded as an Apple XCFramework; runs the Gemma summaries.
- **SpeexDSP** (Xiph.Org) - BSD-3-Clause, vendored from source for meeting echo cancellation.
- **parakeet.cpp** (LocalAI contributors) - MIT, compiled from a pinned tag for Linux ASR.
- **whisper.cpp** (ggml contributors) - MIT, compiled from a pinned tag for the Linux Whisper fallback.
- **NVIDIA Linux model weights** - downloaded on demand and governed by each original model card's terms; the GGUF conversions come from [`mudler/parakeet-cpp-gguf`](https://huggingface.co/mudler/parakeet-cpp-gguf).

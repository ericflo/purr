# Security Policy

Purr handles microphone audio and can inject text into other applications on
both supported platforms. Please report anything security-sensitive you find.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

For the Linux port in this fork, use:

- [Private vulnerability reporting for `ericflo/purr`](https://github.com/ericflo/purr/security/advisories/new)

For the original macOS application or its signed release channel, report to
the [upstream Purr advisory page](https://github.com/iamarunbrahma/purr/security/advisories/new)
or email **contact@arunbrahma.com** with `[purr-security]` in the subject.

Please include what you need to make the problem reproducible: the Purr version
(or commit), operating system and desktop, CPU/GPU, selected model, what you did,
what happened, and what you expected. A proof of concept helps but is not
required.

### What to expect

The project is maintained by volunteers and does not promise a response-time
service level. Maintainers will use the private advisory to acknowledge the
report, coordinate a fix and disclosure timeline, and explain if a report will
not be fixed. If you receive no acknowledgement, follow up through the same
private channel.

## Disclosure

Please use coordinated disclosure. Maintainers will work with you on a timeline
and credit you in the release notes and advisory unless you prefer anonymity.
If a fix takes longer than 90 days, you are free to disclose.

## Safe harbour

If you act in good faith to find and report a vulnerability, the project
maintainers will not pursue or support action against you. Test only against
your own machine and data, do not access other people's audio or transcripts,
do not degrade services, and allow a reasonable opportunity to fix the issue
before public disclosure.

## Supported versions

Purr is pre-1.0 and ships from a single release line. Only the latest release receives
security fixes.

| Version | Supported |
| --- | --- |
| Linux `main` branch | Yes; source build only |
| Latest upstream macOS release | Yes, by the upstream project |
| Older revisions/releases | No; please update |

Linux users update with `git pull --ff-only && make linux-install`. macOS users
update from **About Purr > Check for Updates**.

## Threat model

Purr's permission set is unusual. You should know what you are trusting.

### What Purr can do on your Mac

To type into arbitrary applications, Purr holds:

- **Microphone** - records your voice
- **Accessibility** - reads and writes text in other applications' fields
- **Input Monitoring** with a global `CGEventTap` - observes keystrokes system-wide to
  detect its hotkeys
- **System audio capture** (meeting mode, macOS 14.2+) - records everything your Mac plays,
  including other participants on a call

**App Sandbox is intentionally disabled.** `CGEventTap` and paste injection do not work
reliably from a sandboxed process across arbitrary host applications. This is documented in
`Resources/Purr.entitlements`. The tradeoff is deliberate.

Taken together this is, mechanically, the capability set of a keylogger with a microphone.
Purr does not use it that way, and the source is here so you can confirm that. But the
correct assumption is that a compromised Purr build would be an excellent surveillance
tool, which is why the integrity of the release channel matters more than almost anything
else in this project.

### What Purr can do on Linux

The Linux port has a smaller but still sensitive permission set:

- **Microphone** - records the selected PipeWire/PulseAudio source while dictation is active
- **Virtual keyboard** - opens `/dev/uinput` and creates “Purr virtual keyboard,” which can
  type and erase text in the focused application
- **GNOME settings** - installs and updates a custom global shortcut that invokes Purr's
  application action

Purr's Linux shortcut does not monitor raw keyboard input: GNOME recognizes the configured
shortcut and invokes a D-Bus application action. Temporary batch audio is deleted after
transcription. Diagnostic logs record word counts rather than dictated text
unless `log_transcripts` is turned on. See
[LINUX.md](LINUX.md#local-data-and-network-access) for exact paths.

### What Purr does not do

- No telemetry, analytics, crash reporting, or usage tracking of any kind
- No account, no login, no remote transcription server
- No network traffic at all once models are downloaded, other than an explicit update check

The macOS application makes these outbound connections:

| Purpose | Host |
| --- | --- |
| Update check | `api.github.com` |
| Update download | `github.com`, redirecting to `objects.githubusercontent.com` |
| Gemma 3 4B summary model | `huggingface.co` |
| Parakeet / Whisper / diarizer weights | fetched by FluidAudio and WhisperKit |

The updater refuses any download URL whose host is not exactly `github.com` over HTTPS,
even if the GitHub API response names one.

The Linux build additionally clones pinned engine tags from GitHub, downloads models from
Hugging Face. Optional model
downloads in the Linux GUI contact Hugging Face. The local Whisper HTTP process binds only
to `127.0.0.1` on an ephemeral port.

Audio is never uploaded. Transcription runs locally on the Apple Neural Engine on macOS and
through local Vulkan/CPU inference on Linux.

### Where your data sits

By default everything Purr writes lives under one directory:

```
~/Library/Application Support/Purr/models/          # downloaded model weights
~/Library/Application Support/Purr/Purr Meetings/   # meeting transcripts
```

You can choose a different meetings folder during setup or in Settings, in which case
transcripts go there instead. If that folder later becomes unwritable, Purr falls back to
the default location rather than losing the transcript.

Meeting transcripts are **plaintext Markdown, not encrypted at rest** - one
`Meeting <timestamp>.md` per recording, plus an optional `.summary.md` sidecar. They may
contain the speech of other people on your calls who never installed Purr. Treat that
folder with the same care as the recordings themselves. Encryption at rest is on the
roadmap.

Preferences live in `defaults` under `com.arunbrahma.purr`.

On Linux, models and engines live under the checkout's `.build-linux/` directory,
preferences live at `~/.config/purr/config.json`, and plaintext diagnostic logs live under
`~/.local/state/purr/`. Temporary batch audio uses `$XDG_RUNTIME_DIR/purr/` and is removed
after transcription. The Linux port does not save transcript documents.

### Release integrity

The following applies to upstream macOS releases. This fork currently publishes no Linux
binaries: Linux users build the checked-out source locally with `make linux-install`.

Releases are Developer ID signed, built with the Hardened Runtime, notarized by Apple, and
stapled. The `.app`, the embedded `llama.framework`, and the `.dmg` are each signed, and the
DMG is notarized and stapled before its hash is computed.

Verify a download yourself before installing:

```bash
shasum -a 256 -c Purr.dmg.sha256          # integrity against the published sidecar
codesign --verify --deep --strict --verbose=2 /Applications/Purr.app
spctl -a -t exec -vv /Applications/Purr.app   # should report: accepted, Notarized Developer ID

# Confirm it is signed by this project and not merely by *someone* with a Developer ID:
codesign -dv --verbose=4 /Applications/Purr.app 2>&1 | grep TeamIdentifier
# expected: TeamIdentifier=5JCFRMC367
```

The in-app updater refuses to install a release that has no `.sha256` sidecar, checks the
download host, and verifies the codesign seal before swapping the bundle.

### Known limitations

Known weaknesses include:

- **The checksum sidecar shares an origin with the artifact.** `Purr.dmg.sha256` is published
  in the same GitHub release as `Purr.dmg`. That defends against corruption and interception
  in transit; it does **not** defend against a compromised GitHub account or token, since an
  attacker able to replace one file could replace both. An independent signature verified
  against an offline key is planned.
- **The install helper does not pin the signing identity.** Before swapping the bundle the
  helper runs `codesign --verify --deep --strict` on the app inside the downloaded DMG. That
  proves the bundle carries an intact, valid signature; it does **not** prove the signature
  is *Purr's*. Any bundle signed by any valid Developer ID would pass. Pinning the check to
  Team ID `5JCFRMC367` with `codesign -R` is the fix, and it is not yet in place.
- **Releases are built by hand on the maintainer's machine.** There is no CI, so there is no
  build provenance or attestation. See [RELEASING.md](RELEASING.md).
- **`Package.resolved` is not committed**, and Swift dependencies use `from:` version ranges,
  so builds are not byte-for-byte reproducible across machines or time.
- **Vendored SpeexDSP has not been audited or fuzzed.** `Sources/CEcho` contains SpeexDSP
  compiled from source with warnings suppressed (`-w`) and a hand-written `config.h`. It
  parses untrusted audio in meeting mode.
- **Model weights are third-party.** Purr verifies the Gemma GGUF against a pinned SHA-256
  and pins the llama.cpp XCFramework by checksum, but weights fetched by WhisperKit and
  FluidAudio are subject to those projects' own integrity handling.
- **Linux downloads are not cryptographically verified.** Engine source is selected by
  release tag rather than immutable commit, the Whisper download helper controls its model
  URL, and Linux GGUF files are currently checked only by expected byte length. A mutable
  tag or compromised download origin is therefore in the Linux build's trust boundary.
- **The Linux port has no packaged release or update channel.** Users run build code and
  newly pulled source directly from their checkout. Review changes before rebuilding.

These are priority areas for security review. Please report anything worse privately.

## Scope

**In scope:** the Purr applications and this repository - the updater, Linux build/install
path, model download and verification, permission handling, text injection, the vendored C
in `Sources/CEcho`, transcript/log storage, and the signing and release pipeline.

**Out of scope:** vulnerabilities solely in upstream dependencies (report those to
WhisperKit, FluidAudio, llama.cpp, SpeexDSP, parakeet.cpp, or whisper.cpp directly, with a
heads-up here if Purr is affected), the operating system itself, and the fact that Purr
requires the documented permissions. Reports consisting only of automated scanner output
with no demonstrated impact may be closed.

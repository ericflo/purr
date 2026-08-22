# Contributing

Thanks for helping improve Purr. This repository is a Linux-focused fork of
[the original macOS project](https://github.com/iamarunbrahma/purr). Changes
specific to the Linux port belong here; fixes to unchanged macOS behavior may
also be useful upstream.

Before opening a pull request:

- Keep transcription local and do not add telemetry or cloud audio processing.
- Avoid committing model weights, compiled output, logs, recordings, or credentials.
- Follow the existing platform-native approach: Swift/SwiftUI on macOS and
  Python/GTK for the Linux integration.
- Update `README.md`, `LINUX.md`, or `SECURITY.md` when behavior, permissions,
  network access, dependencies, or supported platforms change.

Run the checks relevant to the change:

```bash
# Linux unit and runtime smoke tests (requires a completed Linux build)
make linux-check

# Python syntax without starting the application
python3 -m py_compile linux/purr_linux.py linux/purr_models.py

# macOS release build, on Apple Silicon with Xcode
swift build -c release --arch arm64
```

Linux bug reports should include the distribution, desktop, Wayland/X11 session,
GPU and Vulkan driver, selected model, and sanitized `make linux-check` output.
Logs can contain dictated text, so redact them before posting publicly.

Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).

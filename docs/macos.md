# macOS (Apple Silicon MacBook) port

Tested target: Apple Silicon MacBook (M1/M2/M3/M4), macOS 14+, `aarch64-apple-darwin`.
Intel Macs should also build (`x86_64-apple-darwin`) but are untested.

This checkout already contains the port changes (branch/diff below). Upstream
`main` is Windows-only: Vulkan + XInput + `.exe`/`.dll` helpers + Win64-only
asset tools. This document explains what changed and how to run it.

## What was blocking macOS

| # | Windows-only assumption | macOS fix in this checkout |
|---|---|---|
| 1 | `app.rs` forces `Backends::VULKAN`. Macs have no native Vulkan. | `Backends::METAL` on `target_os = "macos"`, Vulkan elsewhere. |
| 2 | `input/platform.rs` only implements XInput; every other OS returns `UnsupportedPlatform`, so no controller is ever `Ready`. `GilrsPlugin` is also disabled unconditionally. | New `desktop` backend polls `gilrs 0.11.2` (same version Bevy 0.18.1 uses) and maps it to the XInput ABI the TU3 converter expects. `GilrsPlugin` stays disabled on Windows, enabled on macOS. |
| 3 | `setup.rs` / `updater.rs` / `custom_models.rs` hard-code `support/skate3setup.exe`, `support/skate3update.exe`. `multiplayer/transport.rs` hard-codes `steam-relay/skate-steam-relay.exe` + `steam_api64.dll`. | New `platform_bins.rs` returns extension-less names on Unix and `libsteam_api.dylib` on macOS. Direct (non-Steam) multiplayer works without the relay. |
| 4 | Dev `wgpu` dependency enables only the `vulkan` backend feature; headless shader probe hard-codes `VULKAN`. | Dev `wgpu` enables `vulkan` + `metal`; probe selects per-OS. |
| 5 | `tools/asset_pipeline/fast_refpack.py` only loads `refpack.dll`. | Loads `.dylib`/`.so` on Unix, `.dll` on Windows. Build with `rustc ... --crate-type cdylib` → `target/native/librefpack.dylib` (done by `build-macos.sh`). |
| 6 | `tools/asset_pipeline/install.py` `dependency()` only finds `name.exe`; XISO URL is Win64-only. | Finds extension-less binaries too, `chmod +x` on Unix. ISO path still defaults to the Win64 ZIP — **prefer an extracted folder** (select `default.xex`) to skip ISO extraction entirely. |
| 7 | Only `BUILD.bat` / `PLAY.bat` / `*.ps1`, CI only `windows-2025`. | New `scripts/build-macos.sh`, `scripts/launch-macos.sh`. |

Pure-logic crates (`skate-core`, `skate-data`, `skate-net`, `skate-vehicles`,
`skate-mods`) needed no changes — `cargo check` passes on ARM64 as-is.

## Prerequisites

1. Xcode Command Line Tools: `xcode-select --install`
2. Rust (stable, ARM64): `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
3. Python 3.13+ for asset conversion (system 3.9 is too old for
   `tools/requirements-setup.txt`): `brew install python@3.13` or python.org.
4. An Xbox 360 controller-compatible pad (Xbox, PS5, 8BitDo, etc.). macOS
   exposes them via HID; `gilrs` reads them without drivers. Keyboard-only
   play is **not** supported — the engine consumes raw pad packets.
5. Your own Skate 3 Xbox 360 disc source: either an `.iso` or (recommended on
   Mac) an **already-extracted folder containing `default.xex` + `data/`**.
   Game assets are never downloaded and never included.

## Build

```bash
./scripts/build-macos.sh
# release (slower, faster game):
./scripts/build-macos.sh --release
```

First run refreshes `Cargo.lock` (new `gilrs` edge for non-Windows targets).
Expect the first `cargo build` to take 10–20 minutes (Bevy 0.18.1); later
builds are incremental. Output: `bin/skate3rust`.

## Assets (one time)

Converted assets live beside the checkout in `assets/` for development:

```bash
# extracted-folder path (recommended, skips extract-xiso):
python3 tools/prepare_assets.py \
  --game-root /path/to/extracted/SKATE3 \
  --output "$PWD/assets" \
  --game-exe "$PWD/bin/skate3rust"
```

`--game-root` must contain `default.xex`. Full conversion (10 maps + skater)
takes several minutes and several GB. Afterwards:

```bash
./scripts/launch-macos.sh
# or a specific map:
./scripts/launch-macos.sh maps/University.skate
```

In-game: Esc opens graphics/difficulty/map settings. Maps switch without
restarting.

## Known macOS gaps (honest list)

- **Steam lobbies**: `skate-steam-relay` compiles on macOS in principle
  (`steamworks 0.13.1` ships a macOS SDK), but the Steam client + overlay on
  Apple Silicon is Intel-only and untested here. **Direct UDP multiplayer**
  (`--net-host` / `--net-local`, as in `launch-multiplayer-test.ps1`) is the
  supported path on Mac.
- **ISO extraction**: `extract-xiso` Win64 ZIP is still the pinned URL. Use an
  extracted folder for now; a macOS `extract-xiso` URL + SHA needs pinning and
  testing before ISO-direct setup works on Mac.
- **FBX2glTF importer** (Custom Models / Mixamo): the packaged importer is the
  Windows `FBX2glTF.exe`. Character import on Mac needs a macOS FBX2glTF binary
  wired through `Prepare-CharacterImporter`. Core game + maps do not need it.
- **Setup GUI**: `tools/setup.py` uses Tkinter (`iconbitmap`, file dialogs).
  It runs on macOS with python.org Tk, but the *packaged* `skate3setup` helper
  (PyInstaller bundle) has no macOS build yet — hence `--assets` dev flow
  above instead of first-launch setup windows.
- **Performance**: the vendored `bevy_pbr`/`bevy_core_pipeline` patches were
  tuned against Vulkan validation layers. They are backend-agnostic bind-group
  caches, but frame-time numbers on Metal need re-measuring
  (`docs/cpu-followup-optimizations.md`, `--trace`).
- ** CI**: `.github/workflows/release.yml` is still `windows-2025` only. A
  `macos-15` (ARM64) job running `cargo check/test + build-macos.sh` is the
  next step before publishing a `skate3rust-macos-aarch64.zip`.

## Files changed

- `crates/skate-game/src/app.rs` — Metal backend, conditional GilrsPlugin
- `crates/skate-game/src/input/platform.rs` — gilrs desktop transport
- `crates/skate-game/Cargo.toml` — `target.'cfg(not(windows))'.dependencies.gilrs`, wgpu metal feature
- `crates/skate-game/src/platform_bins.rs` *(new)* — portable helper names
- `crates/skate-game/src/{updater,setup,custom_models}.rs`, `multiplayer/transport.rs` — use it
- `crates/skate-game/src/{input,main,retail_shader_tests}.rs` — platform-neutral log/probe
- `tools/asset_pipeline/{fast_refpack,install}.py` — dylib + extension-less tools
- `scripts/{build-macos,launch-macos}.sh` *(new)*

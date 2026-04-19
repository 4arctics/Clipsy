# Clipsy

Clip gameplay, efficiently and properly. A Linux clipping engine for Hyprland backed by [GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/about/).

- Instant replay buffer — press a key, save the last N seconds
- Full recording mode with start/stop toggle
- Desktop audio + microphone capture with optional voice gate
- GUI settings panel with Hyprland hotkey installer
- Compositor-native save notification (shows above fullscreen via `hyprctl notify`)

---

## Requirements

- Arch Linux (or any distro with the AUR)
- Hyprland
- Python 3.11+
- [`gpu-screen-recorder`](https://aur.archlinux.org/packages/gpu-screen-recorder) — AUR
- `ffmpeg` + `ffprobe` — for voice gate postprocessing (optional)
- `hyprctl` — ships with Hyprland

**Optional (for the GUI):**

```sh
sudo pacman -S pyside6
```

---

## Installation

### 1. Clone the repo

```sh
git clone https://github.com/4arctics/Clipsy.git ~/clipping
cd ~/clipping
```

### 2. Install gpu-screen-recorder

```sh
yay -S gpu-screen-recorder
```

### 3. Install the `clipsy` command

```sh
pip install --user -e .
```

Then make sure `~/.local/bin` is in your `$PATH` — add this to your `~/.bashrc` or `~/.zshrc` if `clipsy` says "not found":

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Then reload your shell: `source ~/.bashrc` (or open a new terminal).

Or skip the install entirely and run directly:

```sh
python -m clipsy <command>
```

### 4. Generate a config

```sh
clipsy init
```

Writes `~/.config/clipsy/config.toml` with sensible defaults (30s buffer, 60 FPS, h264, saves to `~/Videos/Clips`).

### 5. Start the daemon

```sh
clipsy daemon
```

Or set it up as a systemd user service so it starts on login:

```sh
mkdir -p ~/.config/systemd/user
cp ~/clipping/systemd/user/clipsy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now clipsy.service
```

### 6. Set up Hyprland hotkeys

**Option A — automatic:**

```sh
clipsy install-hyprland
```

Writes a managed block into `~/.config/hypr/hyprland.conf` and runs `hyprctl reload`. Same as clicking **Install hotkeys to Hyprland** in the GUI.

**Option B — manual:** add these to your `hyprland.conf`:

```ini
bind = SUPER, F9,  exec, clipsy clip
bind = SUPER, F10, exec, clipsy record-toggle
bind = SUPER, F11, exec, clipsy reload
```

If you did not install the package:

```ini
bind = SUPER, F9,  exec, cd ~/clipping && python -m clipsy clip
bind = SUPER, F10, exec, cd ~/clipping && python -m clipsy record-toggle
bind = SUPER, F11, exec, cd ~/clipping && python -m clipsy reload
```

---

## Usage

| Command | What it does |
|---|---|
| `clipsy clip` | Save the last N seconds from the replay buffer |
| `clipsy record-toggle` | Start or stop a full recording |
| `clipsy status` | Show daemon and buffer status |
| `clipsy reload` | Reload config without restarting the daemon |
| `clipsy gui` | Open the settings UI |
| `clipsy list-devices` | Print available capture sources and audio devices |
| `clipsy quit` | Stop the daemon |

When you clip, a **✔ Saved the last N seconds.** popup appears in the top-right corner for 5 seconds. It is rendered by the Hyprland compositor via `hyprctl notify`, so it shows above fullscreen windows with no extra configuration.

---

## Configuration

Edit `~/.config/clipsy/config.toml` directly, or use `clipsy gui`.

```toml
[clip]
buffer_seconds = 30      # how far back a clip reaches
fps            = 60
codec          = "h264"
save_path      = "~/Videos/Clips"
capture        = "screen" # or a monitor name like HDMI-A-1, or "portal"

[audio]
desktop_enabled = true
mic_mode        = "off"  # off | always | voice
```

Run `clipsy reload` after saving to apply changes while the daemon is running.

### Microphone voice gate

Set `mic_mode = "voice"` to gate the mic track through FFmpeg's `agate` filter after each clip saves. The mic is recorded on a separate audio track so the desktop audio is unaffected.

---

## Rofi launcher entry

```sh
bash ~/clipping/scripts/install-rofi-entry.sh
```

Installs a `.desktop` file so Clipsy appears in rofi/your app launcher.

---

## Recording mode

`SUPER+F10` toggles a full recording. By default the replay buffer pauses while recording is active so the two do not fight over the capture device. Set `record.pause_replay_buffer = false` to run both simultaneously.

---

## Tips

- Use `mkv` container for long recordings — safer if your PC crashes mid-record.
- Use `h264` codec for easy Discord/browser sharing.
- Use `capture = "portal"` if direct monitor capture misbehaves.
- Clips save to `~/Videos/Clips`, recordings to `~/Videos/Recordings`.

---

## License

MIT

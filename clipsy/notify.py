from __future__ import annotations

import shutil
import subprocess


def notify(title: str, body: str, enabled: bool = True) -> None:
    if not enabled or shutil.which("notify-send") is None:
        return
    try:
        subprocess.run(
            ["notify-send", "-t", "1800", "-u", "low", title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def notify_clip(seconds: int, enabled: bool = True) -> None:
    """Show 'Saved the last N seconds.' via hyprctl notify (compositor-level, fullscreen-safe).
    Falls back to notify-send if hyprctl is unavailable."""
    if not enabled:
        return
    if shutil.which("hyprctl") is not None:
        text = f"fontsize:16 ✔  Saved the last {seconds} seconds."
        try:
            subprocess.run(
                ["hyprctl", "notify", "5", "5000", "rgb(a6e3a1)", text],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    notify("Clipper", f"Saved the last {seconds} seconds.", enabled)

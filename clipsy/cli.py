from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import default_config, default_config_path, effective_socket_path, load_config, save_config
from .daemon import run_daemon
from .ipc import IpcError, send_command
from .postprocess import postprocess_saved_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipsy")
    parser.add_argument("--config", type=Path, default=default_config_path())
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a default config")
    init.add_argument("--force", action="store_true")

    sub.add_parser("daemon", help="run the clipping daemon")
    sub.add_parser("gui", help="open the native settings window")
    sub.add_parser("clip", help="save the replay buffer")
    sub.add_parser("record-toggle", help="start or stop a full recording")
    sub.add_parser("record-start", help="start a full recording")
    sub.add_parser("record-stop", help="stop a full recording")
    sub.add_parser("reload", help="reload config in the daemon")
    sub.add_parser("status", help="show daemon status")
    sub.add_parser("quit", help="stop the daemon")
    sub.add_parser("hyprland-binds", help="print Hyprland bind examples")
    sub.add_parser("install-hyprland", help="write Clipsy hotkeys into hyprland.conf")
    sub.add_parser("list-devices", help="print capture and audio devices from gpu-screen-recorder")

    post = sub.add_parser("postprocess", help="internal post-save hook")
    post.add_argument("path")
    post.add_argument("event_type", nargs="?", default="regular")

    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()

    if args.command == "init":
        if config_path.exists() and not args.force:
            print(f"{config_path} already exists; use --force to replace it")
            return 1
        saved = save_config(default_config(), config_path)
        print(f"Wrote {saved}")
        print(f"Socket: {effective_socket_path(load_config(saved))}")
        return 0

    if args.command == "daemon":
        run_daemon(config_path)
        return 0

    if args.command == "gui":
        try:
            from .gui import run_gui
        except ImportError as exc:
            print(
                f"The settings UI is unavailable: {exc}\n"
                "You can still edit ~/.config/clipsy/config.toml directly.",
                file=sys.stderr,
            )
            return 1

        run_gui(config_path)
        return 0

    if args.command == "hyprland-binds":
        print(_hyprland_binds(load_config(config_path)))
        return 0

    if args.command == "install-hyprland":
        from .gui import _install_hyprland_binds

        response = _install_hyprland_binds(load_config(config_path))
        print(json.dumps(response, indent=2))
        return 0 if response.get("ok") else 1

    if args.command == "list-devices":
        return _list_devices()

    if args.command == "postprocess":
        config = load_config(config_path)
        result = postprocess_saved_file(args.path, args.event_type, config)
        print(json.dumps(result))
        return 0 if result.get("ok") else 1

    action_map = {
        "clip": "clip",
        "record-toggle": "record_toggle",
        "record-start": "record_start",
        "record-stop": "record_stop",
        "reload": "reload_config",
        "status": "status",
        "quit": "quit",
    }
    try:
        response = send_command(action_map[args.command], socket_path=effective_socket_path(load_config(config_path)))
    except IpcError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2))
    return 0 if response.get("ok") else 1


def _hyprland_binds(config) -> str:
    from .gui import _hyprland_block

    return _hyprland_block(config)


def _list_devices() -> int:
    commands = [
        ("Capture options", ["gpu-screen-recorder", "--list-capture-options"]),
        ("Monitors", ["gpu-screen-recorder", "--list-monitors"]),
        ("Audio devices", ["gpu-screen-recorder", "--list-audio-devices"]),
    ]
    for title, command in commands:
        print(f"\n{title}")
        print("=" * len(title))
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0:
            print(result.stdout.strip() or "(none)")
        else:
            print(result.stderr.strip() or "command failed")
    return 0

from __future__ import annotations

import unittest

from clipsy.config import default_config
from clipsy.gui import (
    APP_HTML,
    HYPRLAND_BEGIN,
    HYPRLAND_END,
    _asset_path,
    _config_from_dict,
    _config_to_dict,
    _hyprland_block,
    _read_asset,
    _replace_managed_block,
    _split_hotkey,
)


class GuiTests(unittest.TestCase):
    def test_html_contains_control_app(self) -> None:
        self.assertIn("Clipsy Control", APP_HTML)
        self.assertIn("/api/config", APP_HTML)
        self.assertIn("data-tab=\"clips\"", APP_HTML)
        self.assertIn("/assets/clipsy.svg", APP_HTML)
        self.assertIn('<option value="240">240 FPS</option>', APP_HTML)

    def test_logo_asset_exists(self) -> None:
        self.assertTrue(_asset_path("clipsy.svg").exists())
        self.assertTrue(_read_asset("clipsy.svg").startswith(b"<svg"))

    def test_config_dict_round_trip_updates_values(self) -> None:
        config = default_config()
        raw = _config_to_dict(config)
        raw["clip"]["buffer_seconds"] = 90
        raw["record"]["container"] = "mkv"
        raw["audio"]["mic_mode"] = "voice"
        updated = _config_from_dict(raw, config)
        self.assertEqual(updated.clip.buffer_seconds, 90)
        self.assertEqual(updated.record.container, "mkv")
        self.assertEqual(updated.audio.mic_mode, "voice")

    def test_hyprland_block_replaces_managed_section(self) -> None:
        original = "bind = SUPER, RETURN, exec, kitty\n\nold"
        block = f"{HYPRLAND_BEGIN}\nbind = SUPER, F9, exec, clipsy clip\n{HYPRLAND_END}"
        first = _replace_managed_block(original, block)
        second = _replace_managed_block(first, block)
        self.assertEqual(first, second)
        self.assertIn("bind = SUPER, RETURN, exec, kitty", second)
        self.assertIn("clipsy clip", second)

    def test_hyprland_block_uses_config_hotkeys(self) -> None:
        config = default_config()
        config.hotkeys.clip = "SUPER SHIFT,F8"
        block = _hyprland_block(config)
        self.assertIn("bind = SUPER SHIFT, F8, exec,", block)

    def test_split_hotkey_accepts_plus_format(self) -> None:
        self.assertEqual(_split_hotkey("SUPER+F9"), ("SUPER", "F9"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clipsy.config import default_config, load_config, save_config, validate_config


class ConfigTests(unittest.TestCase):
    def test_round_trip_default_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            original = default_config()
            original.clip.buffer_seconds = 45
            original.audio.mic_mode = "voice"
            save_config(original, path)
            loaded = load_config(path)
            self.assertEqual(loaded.clip.buffer_seconds, 45)
            self.assertEqual(loaded.audio.mic_mode, "voice")
            self.assertTrue(loaded.audio.separate_tracks)

    def test_invalid_values_are_clamped(self) -> None:
        config = default_config()
        config.clip.buffer_seconds = 1
        config.clip.fps = 0
        config.audio.mic_mode = "sometimes"
        validated = validate_config(config)
        self.assertEqual(validated.clip.buffer_seconds, 2)
        self.assertEqual(validated.clip.fps, 60)
        self.assertEqual(validated.audio.mic_mode, "off")


if __name__ == "__main__":
    unittest.main()

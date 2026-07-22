# tests/test_game_input.py
import os, sys, unittest
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot


class TestGameInputKeyUpGuaranteed(unittest.TestCase):
    def test_keyup_always_called_after_successful_keydown(self):
        """keyUp must be called even if time.sleep raises after keyDown."""
        fake_pydirect = MagicMock()

        with patch("twitch_bot.HAS_PYDIRECTINPUT", True), \
             patch("twitch_bot.pydirectinput", fake_pydirect, create=True), \
             patch("twitch_bot.HAS_PYNPUT", False), \
             patch("time.sleep", side_effect=RuntimeError("interrupted")):
            ctrl = object.__new__(twitch_bot.GameInputController)
            ctrl.enabled_var = twitch_bot._BoolGetter(True)
            try:
                ctrl._press("w", 0.1)
            except Exception:
                pass

        fake_pydirect.keyDown.assert_called_once_with("w")
        fake_pydirect.keyUp.assert_called_once_with("w")

    def test_pynput_not_called_after_pydirectinput_keydown_succeeds(self):
        """If pydirectinput keyDown succeeds, pynput must not also press the key."""
        fake_pydirect = MagicMock()
        fake_kb = MagicMock()

        with patch("twitch_bot.HAS_PYDIRECTINPUT", True), \
             patch("twitch_bot.pydirectinput", fake_pydirect, create=True), \
             patch("twitch_bot.HAS_PYNPUT", True), \
             patch("twitch_bot._pynput_kb", fake_kb), \
             patch("time.sleep"):
            ctrl = object.__new__(twitch_bot.GameInputController)
            ctrl.enabled_var = twitch_bot._BoolGetter(True)
            ctrl._press("w", 0.1)

        fake_pydirect.keyDown.assert_called_once_with("w")
        fake_pydirect.keyUp.assert_called_once_with("w")
        fake_kb.press.assert_not_called()

    def test_pynput_used_when_pydirectinput_keydown_fails(self):
        """If pydirectinput keyDown raises, fall through to pynput."""
        fake_pydirect = MagicMock()
        fake_pydirect.keyDown.side_effect = RuntimeError("device lost")
        fake_kb = MagicMock()

        with patch("twitch_bot.HAS_PYDIRECTINPUT", True), \
             patch("twitch_bot.pydirectinput", fake_pydirect, create=True), \
             patch("twitch_bot.HAS_PYNPUT", True), \
             patch("twitch_bot._pynput_kb", fake_kb), \
             patch("time.sleep"):
            ctrl = object.__new__(twitch_bot.GameInputController)
            ctrl.enabled_var = twitch_bot._BoolGetter(True)
            ctrl._press("w", 0.1)

        fake_kb.press.assert_called_once_with("w")
        fake_pydirect.keyUp.assert_not_called()


if __name__ == "__main__":
    unittest.main()

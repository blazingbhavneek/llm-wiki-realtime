"""The orb gate and the spoken commands."""

from __future__ import annotations

import unittest

from app.core.attention import DORMANT, OPEN, Attention


# ---------------------------------------------------------------------------
# attention
# ---------------------------------------------------------------------------


class AttentionTests(unittest.TestCase):
    def test_orb_off_ignores_speech(self):
        attention = Attention()
        turn = attention.accept("隣の人との会話", now=1)
        self.assertFalse(turn.accepted)
        self.assertEqual(attention.state, DORMANT)

    def test_orb_on_accepts_speech_verbatim(self):
        attention = Attention()
        attention.set_button_held(True)
        attention.open("button", now=0)
        turn = attention.accept("mpf_mfs_open って何？", now=2)
        self.assertTrue(turn.accepted)
        # nothing is stripped now that there is no wake word to remove
        self.assertEqual(turn.text, "mpf_mfs_open って何？")
        self.assertTrue(attention.accept("引数も教えて", now=3).accepted)

    def test_orb_stays_open_until_pressed_again(self):
        attention = Attention()
        attention.set_button_held(True)
        attention.open("button", now=0)
        # no idle timeout: a long silence must not close it
        self.assertTrue(attention.accept("まだ聞いてる？", now=10_000).accepted)
        self.assertEqual(attention.state, OPEN)

        attention.set_button_held(False)
        attention.close()
        self.assertEqual(attention.state, DORMANT)
        self.assertFalse(attention.accept("ただの雑談", now=10_001).accepted)

    def test_typed_text_is_a_turn_even_when_dormant(self):
        attention = Attention()
        turn = attention.accept("mpf_buf は？", from_text_input=True, now=1)
        self.assertTrue(turn.accepted)

    def test_commands(self):
        classify = Attention.classify
        self.assertEqual(classify("もういい"), "stop")
        self.assertEqual(classify("やめて"), "stop")
        self.assertEqual(classify("もう一回言って"), "repeat")
        self.assertEqual(classify("続けて"), "continue")
        self.assertEqual(classify("ありがとう"), "close")
        self.assertEqual(classify("mpf_buf は？"), "none")


if __name__ == "__main__":
    unittest.main()

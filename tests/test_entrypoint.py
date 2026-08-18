"""The dispatch-metadata -> TTS_VOICE override, isolated from a real job."""

from __future__ import annotations

import unittest

from app.runtime.entrypoint import _requested_voice


class RequestedVoiceTests(unittest.TestCase):
    def test_empty_metadata_has_no_voice(self):
        self.assertIsNone(_requested_voice(""))
        self.assertIsNone(_requested_voice("{}"))

    def test_reads_the_voice_field(self):
        self.assertEqual(_requested_voice('{"voice": "Ono_Anna"}'), "Ono_Anna")

    def test_malformed_or_unexpected_metadata_is_ignored_not_raised(self):
        self.assertIsNone(_requested_voice("not json"))
        self.assertIsNone(_requested_voice("[1, 2, 3]"))
        self.assertIsNone(_requested_voice('{"voice": ""}'))
        self.assertIsNone(_requested_voice('{"voice": null}'))


if __name__ == "__main__":
    unittest.main()

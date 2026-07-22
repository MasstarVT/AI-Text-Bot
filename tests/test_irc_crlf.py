# tests/test_irc_crlf.py
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot

class TestIRCCRLFInjection(unittest.TestCase):
    def _make_client(self):
        c = object.__new__(twitch_bot.TwitchIRCClient)
        sent = []
        fake_sock = mock.MagicMock()
        fake_sock.sendall.side_effect = lambda data: sent.append(data)
        c._sock = fake_sock
        c._log  = lambda msg: None
        return c, sent

    def test_newline_in_text_is_stripped(self):
        c, sent = self._make_client()
        c.say("mychannel", "Hello\r\nJOIN #evil")
        self.assertEqual(len(sent), 1)
        payload = sent[0].decode()
        # Strip the trailing \r\n that say() adds — only that one should remain
        body = payload.rstrip("\r\n")
        self.assertNotIn("\r", body)
        self.assertNotIn("\n", body)
        # "JOIN" may appear as plain text in the body — that's fine.
        # What must NOT happen is a second IRC command being sent.
        self.assertEqual(len(sent), 1, "CRLF injection produced a second send()")

    def test_bare_newline_stripped(self):
        c, sent = self._make_client()
        c.say("mychannel", "line1\nline2")
        self.assertEqual(len(sent), 1)
        self.assertNotIn(b"\nline2", sent[0])

    def test_normal_message_unchanged(self):
        c, sent = self._make_client()
        c.say("mychannel", "Hello world!")
        self.assertEqual(len(sent), 1)
        self.assertIn(b"Hello world!", sent[0])

if __name__ == "__main__":
    unittest.main()

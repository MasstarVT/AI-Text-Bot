"""Unit tests for TTSEngine persistent Piper process."""
import io
import json
import struct
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call


def make_wav_frame(text="hello"):
    """Build a minimal fake WAV frame (RIFF header + 4 bytes of silence)."""
    data = b'\x00\x00\x00\x00'
    chunk_size = 36 + len(data)  # 36 = WAV fmt chunk + data chunk header
    header = b'RIFF' + struct.pack('<I', chunk_size) + b'WAVE'
    fmt    = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 22050, 44100, 2, 16)
    dchunk = b'data' + struct.pack('<I', len(data)) + data
    return header + fmt + dchunk


class FakePiperProc:
    """Fake subprocess.Popen for Piper — reads JSON lines, writes WAV frames."""
    def __init__(self, model_path="", wav_frames=None):
        self.returncode = None
        self._wav_frames = wav_frames or [make_wav_frame()]
        self._frame_idx = 0
        self._stdin_lines = []
        self._out_buf = io.BytesIO()
        self.stdin  = MagicMock()
        self.stderr = io.BytesIO(b'')
        # Build stdout content: one WAV per line written
        self.stdout = MagicMock()
        self._setup_stdout()

    def _setup_stdout(self):
        frames = b''.join(self._wav_frames)
        self.stdout.read = self._make_reader(io.BytesIO(frames))

    def _make_reader(self, buf):
        def read(n):
            return buf.read(n)
        return read

    def kill(self): self.returncode = -9
    def poll(self): return self.returncode
    def wait(self, timeout=None): return self.returncode


class TestTTSEnginePersistentPiper(unittest.TestCase):
    def test_speak_calls_on_audio(self):
        """speak() causes on_audio to be called with a base64 WAV string."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "/voices/test.onnx", "config_path": ""}
        received = []
        done = threading.Event()

        frame = make_wav_frame()

        def on_audio(b64):
            received.append(b64)
            done.set()

        fake_proc = FakePiperProc(wav_frames=[frame])
        with patch("subprocess.Popen", return_value=fake_proc):
            engine = twitch_bot.TTSEngine(
                get_config=lambda: cfg,
                log=lambda msg: None,
                on_audio=on_audio,
            )
            engine.speak("hello world")
            done.wait(timeout=3)

        self.assertEqual(len(received), 1)
        import base64
        self.assertEqual(base64.b64decode(received[0]), frame)

    def test_panic_discards_next_frame(self):
        """panic() causes the reader to discard the next WAV frame."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "/voices/test.onnx", "config_path": ""}
        received = []

        # Gate that prevents the reader from reading any data until _stop_event
        # is set — this makes the test deterministic across Python GIL variants.
        gate = threading.Event()
        frames_bytes = make_wav_frame() + make_wav_frame()
        buf = io.BytesIO(frames_bytes)

        def gated_read(n):
            gate.wait()
            return buf.read(n)

        fake_proc = FakePiperProc(wav_frames=[make_wav_frame(), make_wav_frame()])
        fake_proc.stdout.read = gated_read

        with patch("subprocess.Popen", return_value=fake_proc):
            engine = twitch_bot.TTSEngine(
                get_config=lambda: cfg,
                log=lambda msg: None,
                on_audio=lambda b64: received.append(b64),
            )
            # Panic before any audio is dispatched — gate is still closed so the
            # reader has not yet seen any bytes.
            engine._stop_event.set()
            # Open the gate: reader reads frame 1, sees _stop_event set, clears it.
            gate.set()
            time.sleep(0.2)
            self.assertFalse(engine._stop_event.is_set(), "panic event should be cleared after frame discard")

    def test_no_on_audio_when_model_missing(self):
        """speak() with no model configured logs and does not call on_audio."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        cfg = {"piper_exe": "piper", "model_path": "", "config_path": ""}
        logs = []
        received = []

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = FakePiperProc(wav_frames=[])
            engine = twitch_bot.TTSEngine(
                get_config=lambda: cfg,
                log=lambda msg: logs.append(msg),
                on_audio=lambda b64: received.append(b64),
            )
            engine.speak("test")
            time.sleep(0.3)

        self.assertEqual(len(received), 0)
        self.assertTrue(any("No voice model" in l for l in logs))


class TestVoicesEndpoint(unittest.TestCase):
    def test_voices_lists_onnx_files(self):
        """_scan_voices_dir returns .onnx filenames (without extension), sorted, no other files."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        with patch("os.listdir", return_value=["voice1.onnx", "voice1.onnx.json", "voice2.onnx", "readme.txt"]):
            result = twitch_bot._scan_voices_dir("/fake/voices")

        self.assertIn("voice1", result)
        self.assertIn("voice2", result)
        self.assertNotIn("voice1.onnx.json", result)
        self.assertNotIn("readme.txt", result)
        self.assertEqual(result, sorted(result))

    def test_voices_missing_dir(self):
        """_scan_voices_dir returns empty list when Voices/ directory doesn't exist."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot

        with patch("os.listdir", side_effect=FileNotFoundError):
            result = twitch_bot._scan_voices_dir("/fake/missing")

        self.assertEqual(result, [])


class TestWorkerSurvivesException(unittest.TestCase):
    def test_worker_continues_after_synthesize_exception(self):
        """_worker must log and continue — not die — if _synthesize raises."""
        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import twitch_bot, threading, queue
        from unittest.mock import MagicMock

        tts = object.__new__(twitch_bot.TTSEngine)
        tts._q = queue.Queue()
        tts.log = MagicMock()
        tts.get_config = lambda: {}

        call_count = [0]
        def bad_synthesize(text):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated error")
            # second call succeeds silently

        tts._synthesize = bad_synthesize
        # _stop_event may be needed — set it as a cleared Event
        tts._stop_event = threading.Event()

        t = threading.Thread(target=tts._worker, daemon=True)
        t.start()

        tts._q.put("first")   # will raise
        tts._q.put("second")  # must still be processed
        tts._q.put(None)      # sentinel to stop

        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "_worker thread must exit cleanly after sentinel")
        self.assertEqual(call_count[0], 2, "_worker must process both items despite exception on first")
        tts.log.assert_called()  # must have logged the error

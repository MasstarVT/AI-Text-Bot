import asyncio, os, sys, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import twitch_bot

class TestEventSubDisconnectJoinsThread(unittest.TestCase):
    def test_disconnect_blocks_until_thread_exits(self):
        """disconnect() must join the thread so the old session is dead before it returns."""
        client = object.__new__(twitch_bot.EventSubClient)
        client._stop  = threading.Event()
        client._loop  = None
        client._log   = lambda m: None

        def slow_session():
            loop = asyncio.new_event_loop()
            client._loop = loop
            async def run():
                while not client._stop.is_set():
                    await asyncio.sleep(0.01)
            try:
                loop.run_until_complete(run())
            finally:
                loop.close()

        client._thread = threading.Thread(target=slow_session, daemon=True)
        client._thread.start()

        # Give the thread time to start and set _loop
        time.sleep(0.05)

        client.disconnect()

        self.assertFalse(client._thread.is_alive(),
            "EventSubClient thread must be dead after disconnect() returns")

if __name__ == "__main__":
    unittest.main()

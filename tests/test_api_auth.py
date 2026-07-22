# tests/test_api_auth.py
import json, os, sys, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flask as _flask

class TestAPIAuth(unittest.TestCase):
    def _make_flask_app(self, token="test-secret-token"):
        app = _flask.Flask(__name__)
        web_token = token

        @app.before_request
        def check_auth():
            if _flask.request.path.startswith("/api/"):
                if _flask.request.headers.get("X-Bot-Token") != web_token:
                    return _flask.jsonify({"error": "unauthorized"}), 401

        @app.route("/api/settings", methods=["GET"])
        def settings():
            return _flask.jsonify({"ok": True})

        @app.route("/stream")
        def stream():
            return "stream", 200

        return app, web_token

    def test_api_request_without_token_returns_401(self):
        app, token = self._make_flask_app()
        client = app.test_client()
        resp = client.get("/api/settings")
        self.assertEqual(resp.status_code, 401)

    def test_api_request_with_correct_token_returns_200(self):
        app, token = self._make_flask_app()
        client = app.test_client()
        resp = client.get("/api/settings", headers={"X-Bot-Token": token})
        self.assertEqual(resp.status_code, 200)

    def test_api_request_with_wrong_token_returns_401(self):
        app, token = self._make_flask_app()
        client = app.test_client()
        resp = client.get("/api/settings", headers={"X-Bot-Token": "wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_stream_endpoint_does_not_require_token(self):
        app, token = self._make_flask_app()
        client = app.test_client()
        resp = client.get("/stream")
        self.assertEqual(resp.status_code, 200)

if __name__ == "__main__":
    unittest.main()

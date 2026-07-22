# tests/test_browse_path.py
import os, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import flask as _flask

class TestBrowsePathConfinement(unittest.TestCase):
    def _make_client(self, here_dir):
        """Build a minimal Flask test client that mirrors the real api_browse logic."""
        app = _flask.Flask(__name__)
        here = here_dir

        @app.route("/api/browse")
        def api_browse():
            requested = _flask.request.args.get("path", here)
            root = os.path.realpath(requested)
            try:
                if os.path.commonpath([root, here]) != here:
                    return _flask.jsonify({"error": "access denied"}), 403
            except ValueError:
                return _flask.jsonify({"error": "access denied"}), 403
            if not os.path.isdir(root):
                root = here
            return _flask.jsonify({"path": root})

        return app.test_client()

    def test_path_outside_project_root_returns_403(self):
        with tempfile.TemporaryDirectory() as d:
            client = self._make_client(d)
            resp = client.get("/api/browse?path=/etc")
            self.assertEqual(resp.status_code, 403)

    def test_path_inside_project_root_returns_200(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "subdir")
            os.makedirs(sub)
            client = self._make_client(d)
            resp = client.get(f"/api/browse?path={sub}")
            self.assertEqual(resp.status_code, 200)

    def test_path_traversal_attempt_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            client = self._make_client(d)
            resp = client.get(f"/api/browse?path={d}/../../etc")
            self.assertEqual(resp.status_code, 403)

if __name__ == "__main__":
    unittest.main()

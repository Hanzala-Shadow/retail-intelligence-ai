import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.remote_reranker import RemoteRerankerClient


TOKEN = "t" * 64


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(body)
        response = {
            "schema_version": 1,
            "role": request["role"],
            "model_id": request["model_id"],
            "revision": request["revision"],
            "scores": [float(index) for index, _ in enumerate(request["pairs"])],
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        return


class RemoteRerankerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def client(self, token=TOKEN):
        host, port = self.server.server_address
        return RemoteRerankerClient(f"http://{host}:{port}", token, 5)

    def test_scores_and_validates_model_identity(self):
        scores = self.client().score(
            role="anchor",
            model_id="anchor-model",
            revision="revision",
            max_length=512,
            batch_size=32,
            pairs=[("q", "p1"), ("q", "p2")],
        )
        self.assertEqual(scores, [0.0, 1.0])

    def test_rejects_short_token_and_empty_pairs(self):
        with self.assertRaisesRegex(ValueError, "32 characters"):
            self.client("short")
        with self.assertRaisesRegex(ValueError, "1..1000"):
            self.client().score(
                role="anchor",
                model_id="m",
                revision="r",
                max_length=512,
                batch_size=32,
                pairs=[],
            )

    def test_rejects_plain_http_public_endpoint(self):
        with self.assertRaisesRegex(ValueError, "private IP"):
            RemoteRerankerClient("http://8.8.8.8:9000", TOKEN, 5)


if __name__ == "__main__":
    unittest.main()

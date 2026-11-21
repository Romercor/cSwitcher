"""Asking Anthropic which organization a login's token belongs to, against a local
stand-in server: answers, refused redirects, the hard deadline, and safe failure."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from cswitch.claude import server
from cswitch.claude.login import Login


def login(expires_in_ms: int = 60_000) -> Login:
    oauth = {"accessToken": "at-1", "refreshToken": "rt-1", "expiresAt": int(time.time() * 1000) + expires_in_ms}
    return Login({"claudeAiOauth": oauth}, {})


class FakeAnthropic(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def do_GET(self):
        FakeAnthropic.seen.append((self.path, self.headers.get("Authorization")))
        status, headers, body, delay = FakeAnthropic.routes[self.path]
        time.sleep(delay)
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def log_message(self, *args):
        pass


class OrganizationOfTest(unittest.TestCase):
    def setUp(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        FakeAnthropic.routes, FakeAnthropic.seen = {}, []
        patcher = mock.patch.object(server, "PROFILE_URL", f"{self.base}/profile")
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, path: str, body: object = b"", status: int = 200, headers: dict | None = None,
              delay: float = 0) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        FakeAnthropic.routes[path] = (status, headers or {}, raw, delay)

    def test_returns_the_organization_and_sends_the_token(self):
        self.route("/profile", {"organization": {"uuid": "org-1"}})
        self.assertEqual(server.organization_of(login()), "org-1")
        self.assertEqual(FakeAnthropic.seen, [("/profile", "Bearer at-1")])

    def test_expired_access_token_is_never_sent(self):
        self.route("/profile", {"organization": {"uuid": "org-1"}})
        self.assertIsNone(server.organization_of(login(-1)))
        self.assertEqual(FakeAnthropic.seen, [])

    def test_redirects_are_refused_and_the_token_never_follows(self):
        self.route("/profile", status=302, headers={"Location": f"{self.base}/elsewhere"})
        self.route("/elsewhere", {"organization": {"uuid": "org-evil"}})
        self.assertIsNone(server.organization_of(login()))
        self.assertNotIn("/elsewhere", [path for path, _ in FakeAnthropic.seen])

    def test_hard_deadline(self):
        self.route("/profile", {"organization": {"uuid": "org-1"}}, delay=3)
        with mock.patch.object(server, "DEADLINE_SECONDS", 0.3):
            started = time.monotonic()
            self.assertIsNone(server.organization_of(login()))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_errors_and_unexpected_answers_mean_unknown(self):
        cases = [(401, {"error": "no"}), (200, b"not json"), (200, {}), (200, {"organization": None}),
                 (200, {"organization": {"uuid": 5}}), (200, [1])]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                self.route("/profile", body, status=status)
                self.assertIsNone(server.organization_of(login()))


if __name__ == "__main__":
    unittest.main()

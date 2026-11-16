
"""checker.py 并发和队列限制专项测试。

使用本机 HTTP 模拟服务，不访问公网，不调用 Globalping。
"""

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checker import check_candidates


VALID_TS = b"".join(b"\x47" + bytes(187) for _ in range(6))


class State:
    lock = threading.Lock()
    active = 0
    maximum = 0
    routes = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        pass

    def do_GET(self):
        with State.lock:
            State.active += 1
            State.maximum = max(State.maximum, State.active)
            delay = State.routes.get(self.path, 0)

        try:
            if delay:
                time.sleep(delay)

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(VALID_TS)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(VALID_TS)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with State.lock:
                State.active -= 1

    def setup(self):
        super().setup()
        self.close_connection = True


class CheckerConcurrencyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            Handler,
        )
        cls.server.daemon_threads = True

        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()

        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        with State.lock:
            State.active = 0
            State.maximum = 0
            State.routes = {}

    def settings(self, **overrides):
        config = {
            "local_concurrency": 4,
            "per_host_concurrency": 4,
            "queue_limit": 10,
            "max_queue_seconds": 5,
            "candidate_active_budget_seconds": 4,
            "max_hls_manifest_bytes": 1048576,
            "max_ts_read_bytes": 65536,
            "max_redirects": 5,
        }
        config.update(overrides)
        return config

    def candidate(self, path, host="127.0.0.1"):
        return {
            "normalized_url": (
                f"http://{host}:{self.port}{path}"
            )
        }

    def run_candidates(self, candidates, **overrides):
        return check_candidates(
            candidates,
            self.settings(**overrides),
        )

    def maximum_active(self):
        with State.lock:
            return State.maximum

    def test_global_concurrency_never_exceeds_limit(self):
        """不同主机的请求总并发不得超过 local_concurrency。"""
        with State.lock:
            State.routes = {
                f"/global-{index}.ts": 0.35
                for index in range(8)
            }

        candidates = []
        for index in range(8):
            host = (
                "127.0.0.1"
                if index % 2 == 0
                else "localhost"
            )
            candidates.append(
                self.candidate(
                    f"/global-{index}.ts",
                    host=host,
                )
            )

        results = self.run_candidates(
            candidates,
            local_concurrency=2,
            per_host_concurrency=2,
            queue_limit=10,
        )

        self.assertEqual(len(results), 8)
        self.assertLessEqual(self.maximum_active(), 2)
        self.assertGreaterEqual(self.maximum_active(), 2)

        for result in results:
            self.assertEqual(
                result["checks"]["ipv4"]["status"],
                "PASS",
            )

    def test_per_host_concurrency_never_exceeds_limit(self):
        """同一主机的请求并发不得超过 per_host_concurrency。"""
        with State.lock:
            State.routes = {
                f"/host-{index}.ts": 0.3
                for index in range(5)
            }

        candidates = [
            self.candidate(f"/host-{index}.ts")
            for index in range(5)
        ]

        results = self.run_candidates(
            candidates,
            local_concurrency=4,
            per_host_concurrency=1,
            queue_limit=10,
        )

        self.assertEqual(len(results), 5)
        self.assertEqual(self.maximum_active(), 1)

        for result in results:
            self.assertEqual(
                result["checks"]["ipv4"]["status"],
                "PASS",
            )

    def test_queue_limit_splits_candidates_into_batches(self):
        """queue_limit=1 时，每批只能提交一个候选检测任务。"""
        with State.lock:
            State.routes = {
                f"/batch-{index}.ts": 0.2
                for index in range(4)
            }

        candidates = [
            self.candidate(f"/batch-{index}.ts")
            for index in range(4)
        ]

        results = self.run_candidates(
            candidates,
            local_concurrency=4,
            per_host_concurrency=4,
            queue_limit=1,
        )

        self.assertEqual(len(results), 4)
        self.assertEqual(self.maximum_active(), 1)

        for result in results:
            self.assertEqual(
                result["checks"]["ipv4"]["status"],
                "PASS",
            )

    def test_waiting_for_host_slot_times_out_as_incomplete(self):
        """等待同一主机槽位超过 max_queue_seconds 必须返回 INCOMPLETE。"""
        with State.lock:
            State.routes = {
                "/occupy-slot.ts": 1.5,
                "/waiting-slot.ts": 0,
            }

        candidates = [
            self.candidate("/occupy-slot.ts"),
            self.candidate("/waiting-slot.ts"),
        ]

        started = time.monotonic()

        results = self.run_candidates(
            candidates,
            local_concurrency=2,
            per_host_concurrency=1,
            queue_limit=2,
            max_queue_seconds=1,
            candidate_active_budget_seconds=4,
        )

        elapsed = time.monotonic() - started

        self.assertEqual(len(results), 2)

        self.assertEqual(
            results[0]["checks"]["ipv4"]["status"],
            "PASS",
        )

        self.assertEqual(
            results[1]["checks"]["ipv4"]["status"],
            "INCOMPLETE",
        )

        self.assertEqual(
            results[1]["checks"]["http"]["status"],
            "INCOMPLETE",
        )

        self.assertLess(elapsed, 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

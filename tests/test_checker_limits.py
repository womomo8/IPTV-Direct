
"""checker.py 并发和队列限制专项测试。

使用本机 HTTP 模拟服务，不访问公网，不调用 Globalping。
"""

import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checker import Detector, check_candidates


VALID_TS = b"".join(b"\x47" + bytes(187) for _ in range(6))


class State:
    lock = threading.Lock()
    active = 0
    maximum = 0
    routes = {}
    started_event = None
    secondary_started_event = None
    release_event = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        pass

    def do_GET(self):
        with State.lock:
            State.active += 1
            State.maximum = max(State.maximum, State.active)
            delay = State.routes.get(self.path, 0)
            started_event = State.started_event
            secondary_started_event = State.secondary_started_event
            release_event = State.release_event

        try:
            if self.path == "/occupy-slot.ts":
                if started_event is not None:
                    started_event.set()
                if release_event is not None:
                    release_event.wait(timeout=5)
            elif self.path == "/other-host.ts":
                if secondary_started_event is not None:
                    secondary_started_event.set()
            elif delay:
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
            State.started_event = None
            State.secondary_started_event = None
            State.release_event = None

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
        """直接统计候选检测任务并发，不依赖 HTTP 服务端线程清理时间。"""
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

        counter_lock = threading.Lock()
        counter = {"active": 0, "maximum": 0}
        original_check_one = Detector.check_one

        def tracked_check_one(detector, candidate, submitted_at):
            with counter_lock:
                counter["active"] += 1
                counter["maximum"] = max(
                    counter["maximum"],
                    counter["active"],
                )

            try:
                return original_check_one(
                    detector,
                    candidate,
                    submitted_at,
                )
            finally:
                with counter_lock:
                    counter["active"] -= 1

        with patch.object(Detector, "check_one", tracked_check_one):
            results = self.run_candidates(
                candidates,
                local_concurrency=2,
                per_host_concurrency=2,
                queue_limit=10,
            )

        self.assertEqual(len(results), 8)
        self.assertEqual(counter["maximum"], 2)
        self.assertEqual(counter["active"], 0)

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

    def test_executor_queue_wait_does_not_consume_candidate_queue_budget(self):
        """线程池排队时间不应提前消耗候选的主机槽位等待预算。"""
        with State.lock:
            State.routes = {
                "/worker-first.ts": 1.2,
                "/worker-queued.ts": 0,
            }

        candidates = [
            self.candidate("/worker-first.ts"),
            self.candidate("/worker-queued.ts"),
        ]

        results = self.run_candidates(
            candidates,
            local_concurrency=1,
            per_host_concurrency=2,
            queue_limit=2,
            max_queue_seconds=1,
            candidate_active_budget_seconds=4,
        )

        self.assertEqual(len(results), 2)

        for result in results:
            self.assertEqual(
                result["checks"]["ipv4"]["status"],
                "PASS",
                msg=f"候选未能完成 IPv4 检测：{result}",
            )

        self.assertEqual(
            results[1]["checks"]["http"]["status"],
            "PASS",
            msg=f"线程池排队后的候选被错误判定：{results[1]}",
        )

    def test_unicode_request_path_and_query_are_encoded(self):
        """请求路径和查询参数中的中文必须转换为 UTF-8 百分号编码。"""
        encoded_path = (
            "/%E7%9B%B4%E6%92%AD.ts"
            "?token=%E4%B8%AD%E6%96%87"
        )

        with State.lock:
            State.routes = {
                encoded_path: 0,
            }

        candidate = self.candidate(
            "/直播.ts?token=中文"
        )

        results = self.run_candidates([candidate])

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["checks"]["ipv4"]["status"],
            "PASS",
            msg=f"中文 URL 请求失败：{results[0]}",
        )
        self.assertEqual(
            results[0]["checks"]["http"]["status"],
            "PASS",
            msg=f"中文 URL 的 HTTP 检测失败：{results[0]}",
        )
    
    def test_waiting_for_host_slot_times_out_as_incomplete(self):
        """用事件锁定第一个请求，再验证第二个请求等待槽位超时。"""
        started_event = threading.Event()
        release_event = threading.Event()

        with State.lock:
            State.routes = {
                "/occupy-slot.ts": 0,
                "/waiting-slot.ts": 0,
            }
            State.started_event = started_event
            State.release_event = release_event

        config = self.settings(
            local_concurrency=2,
            per_host_concurrency=1,
            queue_limit=2,
            max_queue_seconds=1,
            candidate_active_budget_seconds=8,
        )

        detector = Detector(config)

        first_candidate = self.candidate("/occupy-slot.ts")
        second_candidate = self.candidate("/waiting-slot.ts")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                detector.check_one,
                first_candidate,
                time.monotonic(),
            )

            second_future = None

            try:
                # 只有服务器收到第一个指定请求，事件才会触发。
                self.assertTrue(
                    started_event.wait(timeout=3),
                    "第一个指定请求未能到达测试服务器",
                )

                # 此时第一个请求仍被 release_event 暂停，
                # 因此它必然仍占用同一主机的并发槽位。
                second_future = pool.submit(
                    detector.check_one,
                    second_candidate,
                    time.monotonic(),
                )

                second_result = second_future.result(timeout=4)

            finally:
                # 无论断言是否成功，都释放第一个请求，
                # 避免测试失败时留下阻塞中的服务线程。
                release_event.set()

            first_result = first_future.result(timeout=6)

        self.assertEqual(
            first_result["checks"]["ipv4"]["status"],
            "PASS",
            msg=f"第一个请求结果异常：{first_result}",
        )

        self.assertEqual(
            first_result["checks"]["ts"]["status"],
            "PASS",
            msg=f"第一个请求 TS 检测异常：{first_result}",
        )

        self.assertEqual(
            second_result["checks"]["ipv4"]["status"],
            "INCOMPLETE",
            msg=f"第二个请求 IPv4 状态异常：{second_result}",
        )

        self.assertEqual(
            second_result["checks"]["http"]["status"],
            "INCOMPLETE",
            msg=f"第二个请求 HTTP 状态异常：{second_result}",
        )

    def test_same_host_waiters_do_not_block_other_hosts_and_keep_input_order(self):
        """同主机等待任务不能占满工作线程，且结果保持输入顺序。"""
        started_event = threading.Event()
        secondary_started_event = threading.Event()
        release_event = threading.Event()

        with State.lock:
            State.routes = {
                "/other-host.ts": 0,
            }
            State.started_event = started_event
            State.secondary_started_event = secondary_started_event
            State.release_event = release_event

        candidates = [
            self.candidate("/occupy-slot.ts"),
            self.candidate("/queued-same-host.ts"),
            self.candidate("/other-host.ts", host="localhost"),
        ]

        with ThreadPoolExecutor(max_workers=1) as runner:
            future = runner.submit(
                self.run_candidates,
                candidates,
                local_concurrency=2,
                per_host_concurrency=1,
                queue_limit=3,
                max_queue_seconds=3,
                candidate_active_budget_seconds=4,
            )

            try:
                self.assertTrue(
                    started_event.wait(timeout=2),
                    "第一个主机请求没有启动",
                )

                self.assertTrue(
                    secondary_started_event.wait(timeout=1),
                    "其他主机的候选被同主机等待任务阻塞",
                )
            finally:
                release_event.set()

            results = future.result(timeout=8)

        self.assertEqual(len(results), 3)

        self.assertEqual(
            [
                item["normalized_url"]
                for item in results
            ],
            [
                item["normalized_url"]
                for item in candidates
            ],
            "调度顺序变化后，结果必须仍保持原始输入顺序",
        )

        for result in results:
            self.assertEqual(
                result["checks"]["http"]["status"],
                "PASS",
                msg=f"HTTP 检测未通过：{result}",
            )
            self.assertEqual(
                result["checks"]["ts"]["status"],
                "PASS",
                msg=f"TS 检测未通过：{result}",
            )

if __name__ == "__main__":
    unittest.main(verbosity=2)

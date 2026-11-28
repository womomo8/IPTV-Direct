
"""IPTV-Direct checker.py 专项测试。

只使用本机 HTTP 模拟服务和连接层模拟，不访问公网直播源，
不调用 Globalping。
运行方式：
    python -m unittest discover -s tests -p "test_checker.py" -v
"""

import socket
import ssl
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from checker import (
    Detector,
    IPv4HTTPSConnection,
    check_candidates,
)


VALID_TS = b"".join(b"\x47" + bytes(187) for _ in range(6))
INVALID_TS = bytes(len(VALID_TS))

MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXT-X-MEDIA-SEQUENCE:1\n"
    "#EXTINF:10,\n"
    "seg1.ts\n"
    "#EXTINF:10,\n"
    "seg2.ts\n"
    "#EXT-X-ENDLIST\n"
).encode("utf-8")

MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
    "variant/index.m3u8\n"
).encode("utf-8")

BUDGET_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXTINF:10,\n"
    "budget-seg1.ts\n"
    "#EXTINF:10,\n"
    "budget-seg2.ts\n"
    "#EXT-X-ENDLIST\n"
).encode("utf-8")

ENCRYPTED_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
    "#EXTINF:10,\n"
    "seg1.ts\n"
    "#EXTINF:10,\n"
    "seg2.ts\n"
    "#EXT-X-ENDLIST\n"
).encode("utf-8")

FMP4_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:7\n"
    "#EXT-X-TARGETDURATION:10\n"
    '#EXT-X-MAP:URI="init.mp4"\n'
    "#EXTINF:10,\n"
    "seg1.m4s\n"
    "#EXTINF:10,\n"
    "seg2.m4s\n"
    "#EXT-X-ENDLIST\n"
).encode("utf-8")

SUBSCRIPTION_M3U = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-name="CCTV-5",CCTV-5\n'
    "http://example.invalid/live.ts\n"
).encode("utf-8")

# 超过首次64 KB读取范围，但小于1 MB。
LARGE_MANIFEST = (
    MEDIA_PLAYLIST
    + b"\n#"
    + b"x" * (70 * 1024)
    + b"\n"
)

# 超过正式1 MB清单上限。
OVERSIZED_MANIFEST = (
    MEDIA_PLAYLIST
    + b"\n#"
    + b"x" * (1048576 + 1024)
    + b"\n"
)


class FakeSocket:
    """用于验证 HTTPS 连接层行为，不建立真实外部连接。"""

    def __init__(self):
        self.timeout = None
        self.connected_address = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        self.connected_address = address

    def close(self):
        self.closed = True

    def shutdown(self, how):
        pass


class FakeTLSContext:
    """记录 TLS 包装时传入的 SNI 主机名。"""

    def __init__(self):
        self.server_hostname = None

    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostname = server_hostname
        return sock


class MockHandler(BaseHTTPRequestHandler):
    """为 checker.py 提供可控的本机 HTTP 响应。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        pass

    def do_GET(self):
        self.server.range_headers.append(
            (self.path, self.headers.get("Range"))
        )
        spec = self.server.routes.get(self.path)

        if spec is None:
            self.send_error(404)
            return

        status = spec.get("status", 200)
        body = spec.get("body", b"")
        headers = dict(spec.get("headers", {}))
        delay = spec.get("delay", 0)
        range_mode = spec.get("range_mode", "auto")

        if delay:
            time.sleep(delay)

        if status in (301, 302, 303, 307, 308):
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        response_body = body

        if status == 206 and range_mode == "auto":
            range_header = self.headers.get("Range", "")
            start, end = 0, len(body) - 1

            if range_header.startswith("bytes="):
                try:
                    requested = range_header[6:].split("-", 1)
                    start = int(requested[0])
                    end = int(requested[1])
                except (ValueError, IndexError):
                    self.send_error(400)
                    return

            if not body or start >= len(body):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            end = min(end, len(body) - 1)
            response_body = body[start:end + 1]
            headers["Content-Range"] = (
                f"bytes {start}-{end}/{len(body)}"
            )

        self.send_response(status)

        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/octet-stream"

        if "Content-Length" not in headers:
            headers["Content-Length"] = str(len(response_body))

        for name, value in headers.items():
            self.send_header(name, str(value))

        self.end_headers()

        try:
            self.wfile.write(response_body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class CheckerLocalTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            MockHandler,
        )
        cls.server.daemon_threads = True
        cls.server.routes = {}
        cls.server.range_headers = []

        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.server_thread.start()

        cls.base_url = (
            f"http://127.0.0.1:{cls.server.server_port}"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def setUp(self):
        self.server.range_headers = []
        self.server.routes = {
            "/direct.ts": {
                "status": 206,
                "body": VALID_TS,
            },
            "/direct-200.ts": {
                "status": 200,
                "body": VALID_TS,
            },
            "/large-direct.ts": {
                "status": 206,
                "body": VALID_TS * 100,
            },
            "/empty.ts": {
                "status": 200,
                "body": b"",
            },
            "/invalid-length.ts": {
                "status": 200,
                "body": VALID_TS,
                "headers": {"Content-Length": "not-a-number"},
            },
            "/media.m3u8": {
                "status": 200,
                "body": MEDIA_PLAYLIST,
            },
            "/master.m3u8": {
                "status": 200,
                "body": MASTER_PLAYLIST,
            },
            "/variant/index.m3u8": {
                "status": 200,
                "body": MEDIA_PLAYLIST,
            },
            "/variant/seg1.ts": {
                "status": 200,
                "body": VALID_TS,
            },
            "/variant/seg2.ts": {
                "status": 200,
                "body": VALID_TS,
            },
            "/seg1.ts": {
                "status": 200,
                "body": VALID_TS,
            },
            "/seg2.ts": {
                "status": 200,
                "body": VALID_TS,
            },
            "/bad-hls.m3u8": {
                "status": 200,
                "body": MEDIA_PLAYLIST,
            },
            "/bad-seg1.ts": {
                "status": 200,
                "body": INVALID_TS,
            },
            "/bad-seg2.ts": {
                "status": 200,
                "body": INVALID_TS,
            },
            "/encrypted.m3u8": {
                "status": 200,
                "body": ENCRYPTED_PLAYLIST,
            },
            "/fmp4.m3u8": {
                "status": 200,
                "body": FMP4_PLAYLIST,
            },
            "/subscription.m3u": {
                "status": 200,
                "body": SUBSCRIPTION_M3U,
            },
            "/unknown.bin": {
                "status": 200,
                "body": b"this is not a media stream",
            },
            "/redirect": {
                "status": 302,
                "headers": {"Location": "/direct.ts"},
            },
            "/redirect-other-host": {
                "status": 302,
                "headers": {
                    "Location": (
                        f"http://localhost:{self.server.server_port}"
                        "/direct.ts"
                    )
                },
            },
            "/loop": {
                "status": 302,
                "headers": {"Location": "/loop"},
            },
            "/unavailable": {
                "status": 503,
                "body": b"service unavailable",
            },
            "/bad-range": {
                "status": 206,
                "body": VALID_TS,
                "range_mode": "invalid",
                "headers": {
                    "Content-Range": "bytes 1-1128/1129",
                    "Content-Length": str(len(VALID_TS)),
                },
            },
            "/slow.ts": {
                "status": 200,
                "body": VALID_TS,
                "delay": 1.5,
            },
            "/budget.m3u8": {
                "status": 200,
                "body": BUDGET_PLAYLIST,
                "delay": 0.65,
            },
            "/budget-seg1.ts": {
                "status": 200,
                "body": VALID_TS,
                "delay": 0.65,
            },
            "/budget-seg2.ts": {
                "status": 200,
                "body": VALID_TS,
            },
            "/large-manifest.m3u8": {
                "status": 206,
                "body": LARGE_MANIFEST,
            },
            "/oversized-manifest.m3u8": {
                "status": 206,
                "body": OVERSIZED_MANIFEST,
            },
        }

    def settings(self, **overrides):
        """返回用于隔离边界测试的独立参数，不代表正式 settings.json。"""
        config = {
            "local_concurrency": 4,
            "per_host_concurrency": 2,
            "queue_limit": 10,
            "max_queue_seconds": 5,
            "candidate_active_budget_seconds": 3,
            "max_hls_manifest_bytes": 1048576,
            "max_ts_read_bytes": 65536,
            "max_redirects": 5,
        }
        config.update(overrides)
        return config

    def candidate(self, path):
        return {
            "normalized_url": self.base_url + path,
        }

    def check(self, path, **settings_overrides):
        results = check_candidates(
            [self.candidate(path)],
            self.settings(**settings_overrides),
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def assert_status(self, result, name, expected):
        actual = result["checks"][name]["status"]
        self.assertEqual(
            actual,
            expected,
            msg=(
                f"{name} 状态不符合预期。"
                f"实际：{actual}；"
                f"原因：{result['checks'][name]['reason']}"
            ),
        )

    def test_direct_ts_http_206_passes(self):
        result = self.check("/direct.ts")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assert_status(result, "hls", "N/A")

    def test_direct_ts_http_200_ignoring_range_passes(self):
        result = self.check("/direct-200.ts")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assert_status(result, "hls", "N/A")

    def test_hls_media_playlist_and_segments_pass(self):
        result = self.check("/media.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")

    def test_hls_master_playlist_resolves_relative_urls(self):
        result = self.check("/master.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")

    def test_hls_uses_second_variant_when_first_fails(self):
        self.server.routes["/master.m3u8"]["body"] = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
            "missing/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2400000\n"
            "variant/index.m3u8\n"
        ).encode("utf-8")

        result = self.check("/master.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")   

     def test_hls_uses_second_variant_when_first_playlist_invalid(self):
        self.server.routes["/master.m3u8"]["body"] = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
            "bad-variant/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2400000\n"
            "variant/index.m3u8\n"
        ).encode("utf-8")

        self.server.routes["/bad-variant/index.m3u8"] = {
            "status": 200,
            "body": b"\xff",
        }

        result = self.check("/master.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")   
    
    def test_hls_invalid_ts_segments_do_not_pass(self):
        self.server.routes["/bad-hls.m3u8"]["body"] = (
            MEDIA_PLAYLIST.replace(
                b"seg1.ts", b"bad-seg1.ts"
            ).replace(
                b"seg2.ts", b"bad-seg2.ts"
            )
        )

        result = self.check("/bad-hls.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "FAIL")

    def test_hls_all_variants_fail(self):
        self.server.routes["/master.m3u8"]["body"] = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
            "missing/first.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2400000\n"
            "missing/second.m3u8\n"
        ).encode("utf-8")

        result = self.check("/master.m3u8")

        self.assert_status(result, "hls", "FAIL")

    def test_hls_variant_timeout_is_incomplete(self):
        self.server.routes["/master.m3u8"]["body"] = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
            "slow/index.m3u8\n"
        ).encode("utf-8")

        self.server.routes["/slow/index.m3u8"] = {
            "status": 200,
            "body": MEDIA_PLAYLIST,
            "delay": 1.0,
        }

        result = self.check(
            "/master.m3u8",
            candidate_active_budget_seconds=0.5,
        )

        self.assert_status(result, "hls", "INCOMPLETE")
    
    def test_hls_uses_one_total_candidate_budget(self):
        started = time.monotonic()

        result = self.check(
            "/budget.m3u8",
            candidate_active_budget_seconds=1,
        )

        elapsed = time.monotonic() - started

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "INCOMPLETE")
        self.assert_status(result, "ts", "INCOMPLETE")
        self.assertLess(elapsed, 2.5)

    def test_hls_manifest_over_64kb_but_under_1mb_passes(self):
        """超过首次读取范围的完整清单应继续读取，而不是误判超限。"""
        self.assertGreater(len(LARGE_MANIFEST), 65536)
        self.assertLessEqual(len(LARGE_MANIFEST), 1048576)

        result = self.check("/large-manifest.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")

    def test_hls_manifest_over_1mb_is_rejected(self):
        """超过1 MB的清单必须拒绝，且不能因截断内容而误判通过。"""
        self.assertGreater(len(OVERSIZED_MANIFEST), 1048576)

        result = self.check("/oversized-manifest.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_encrypted_hls_is_rejected_without_ts_failure(self):
        result = self.check("/encrypted.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_fmp4_hls_is_rejected_without_ts_failure(self):
        result = self.check("/fmp4.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_channel_subscription_m3u_is_not_hls(self):
        result = self.check("/subscription.m3u")

        self.assertEqual(result["media_type"], "UNKNOWN")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_unknown_content_is_rejected(self):
        result = self.check("/unknown.bin")

        self.assertEqual(result["media_type"], "UNKNOWN")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "FAIL")

    def test_redirect_to_direct_ts_uses_ipv4(self):
        result = self.check("/redirect")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assert_status(result, "hls", "N/A")

    def test_cross_host_redirect_uses_ipv4_dns_on_each_hop(self):
        """跨主机重定向的初始请求和后续请求都必须强制 IPv4。"""
        original_getaddrinfo = socket.getaddrinfo
        calls = []

        def recording_getaddrinfo(
            host,
            port,
            family=0,
            type=0,
            proto=0,
            flags=0,
        ):
            calls.append((host, family))
            return original_getaddrinfo(
                host, port, family, type, proto, flags
            )

        with patch(
            "checker.socket.getaddrinfo",
            side_effect=recording_getaddrinfo,
        ):
            result = self.check("/redirect-other-host")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")

        self.assertGreaterEqual(
            len(calls),
            2,
            msg=f"预期至少发生两次 DNS 解析，实际记录：{calls}",
        )
        self.assertTrue(
            all(family == socket.AF_INET for _, family in calls),
            msg=f"发现非 IPv4 DNS 解析：{calls}",
        )

    def test_https_connection_uses_ipv4_socket_and_sni(self):
        """HTTPS 连接必须使用 AF_INET，并将域名传给 TLS SNI。"""
        fake_socket = FakeSocket()
        fake_context = FakeTLSContext()

        resolver = Mock()
        resolver.resolve.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.0.2.10", 443),
            )
        ]

        with patch(
            "checker.socket.socket",
            return_value=fake_socket,
        ) as socket_factory:
            connection = IPv4HTTPSConnection(
                "media.example",
                443,
                deadline=time.monotonic() + 3,
                resolver=resolver,
                context=fake_context,
                timeout=3,
            )
            connection.connect()

        self.assertTrue(connection.ipv4_connected)
        self.assertEqual(
            socket_factory.call_args.args[0],
            socket.AF_INET,
        )
        self.assertEqual(
            fake_context.server_hostname,
            "media.example",
        )
        self.assertEqual(
            fake_socket.connected_address,
            ("192.0.2.10", 443),
        )

        connection.close()

    def test_https_default_context_verifies_certificate_and_hostname(self):
        """未显式传入 TLS 上下文时，必须启用证书和主机名验证。"""
        resolver = Mock()

        connection = IPv4HTTPSConnection(
            "media.example",
            443,
            deadline=time.monotonic() + 3,
            resolver=resolver,
            timeout=3,
        )

        self.assertEqual(
            connection.context.verify_mode,
            ssl.CERT_REQUIRED,
        )
        self.assertTrue(connection.context.check_hostname)

    def test_redirect_loop_is_rejected(self):
        result = self.check("/loop")

        self.assert_status(result, "http", "FAIL")

    def test_http_503_is_not_pass(self):
        result = self.check("/unavailable")

        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "INCOMPLETE")
        self.assert_status(result, "hls", "INCOMPLETE")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_invalid_content_range_is_rejected(self):
        result = self.check("/bad-range")

        self.assert_status(result, "http", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_abort_only_shutdowns_socket(self):
        """超时线程只中断 socket，不与请求线程争抢连接关闭权。"""
        conn = Mock()
        sock = Mock()
        conn.sock = sock

        Detector._abort(conn)

        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        sock.close.assert_not_called()
        conn.close.assert_not_called()
    
    def test_active_detection_budget_timeout(self):
        result = self.check(
            "/slow.ts",
            candidate_active_budget_seconds=1,
        )

        self.assert_status(result, "http", "INCOMPLETE")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_max_redirect_limit_is_respected(self):
        result = self.check(
            "/loop",
            max_redirects=2,
        )

        self.assert_status(result, "http", "FAIL")

    def test_connection_refused_is_reported_as_ipv4_and_http_failure(self):
        """真实 IPv4 连接被拒绝时，不得仅因 DNS 成功而标记通过。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        result = check_candidates(
            [{
                "normalized_url": (
                    f"http://127.0.0.1:{port}/direct.ts"
                )
            }],
            self.settings(),
        )[0]

        self.assert_status(result, "ipv4", "FAIL")
        self.assert_status(result, "http", "FAIL")
        self.assertIn(
            "请求失败",
            result["checks"]["http"]["reason"],
        )

    def test_empty_http_body_is_rejected(self):
        """HTTP 200 但没有实际正文，不能标记 HTTP 通过。"""
        result = self.check("/empty.ts")

        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_invalid_content_length_is_rejected(self):
        """非法 Content-Length 必须作为无效 HTTP 响应处理。"""
        result = self.check("/invalid-length.ts")

        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "FAIL")
        self.assertIn(
            "Content-Length 无效",
            result["checks"]["http"]["reason"],
        )

    def test_direct_ts_range_read_is_capped_at_64kb(self):
        """直接 TS 检测必须把 Range 请求限制在 64 KB。"""
        result = self.check("/large-direct.ts")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assertEqual(
            self.server.range_headers[-1],
            ("/large-direct.ts", "bytes=0-65535"),
        )

    def test_progress_callback_tracks_completion_and_preserves_order(self):
        candidates = [
            {"id": "first", "normalized_url": "first"},
            {"id": "second", "normalized_url": "second"},
            {"id": "third", "normalized_url": "third"},
        ]
        completion_order = []
        progress = []

        def fake_check(item, submitted_at):
            if item["id"] == "first":
                time.sleep(0.1)
            completion_order.append(item["id"])
            return dict(item)

        with patch.object(
            Detector,
            "check_one",
            side_effect=fake_check,
        ):
            results = check_candidates(
                candidates,
                self.settings(
                    local_concurrency=3,
                    queue_limit=3,
                ),
                progress_callback=lambda completed, total: (
                    progress.append((completed, total))
                ),
            )

        self.assertNotEqual(completion_order[0], "first")
        self.assertEqual(
            progress,
            [(1, 3), (2, 3), (3, 3)],
        )
        self.assertEqual(
            [item["id"] for item in results],
            ["first", "second", "third"],
        )

    def test_progress_callback_exception_does_not_interrupt_detection(self):
        candidate = {
            "id": "first",
            "normalized_url": "first",
        }

        def failing_callback(completed, total):
            raise RuntimeError("模拟日志回调异常")

        with patch.object(
            Detector,
            "check_one",
            return_value=dict(candidate),
        ):
            results = check_candidates(
                [candidate],
                self.settings(),
                progress_callback=failing_callback,
            )

        self.assertEqual(results, [candidate])
    
    def test_ts_requires_five_sync_bytes_at_188_byte_intervals(self):
        """五个同步位置必须连续满足，四个同步位置不能通过。"""
        valid = bytearray(188 * 5)
        for index in range(5):
            valid[index * 188] = 0x47

        invalid = bytearray(valid)
        invalid[4 * 188] = 0x00

        offset_valid = bytearray(188 * 5)
        for index in range(5):
            offset_valid[187 + index * 188] = 0x47

        self.assertTrue(Detector._ts_ok(bytes(valid)))
        self.assertFalse(Detector._ts_ok(bytes(invalid)))
        self.assertTrue(Detector._ts_ok(bytes(offset_valid)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

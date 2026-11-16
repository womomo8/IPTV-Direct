
"""IPTV-Direct checker.py 专项测试。

只使用本机 HTTP 模拟服务，不访问公网直播源，不调用 Globalping。
运行方式：
    python -m unittest discover -s tests -p "test_checker.py" -v
"""

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from checker import check_candidates


# 6 个 188 字节 TS 包，包含连续 5 个 0x47 同步位置。
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


class MockHandler(BaseHTTPRequestHandler):
    """为 checker.py 提供可控的本机 HTTP 响应。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        pass

    def do_GET(self):
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
        self.server.routes = {
            "/direct.ts": {
                "status": 206,
                "body": VALID_TS,
            },
            "/direct-200.ts": {
                "status": 200,
                "body": VALID_TS,
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
        }

    def settings(self, **overrides):
        """返回与 settings.json detection 区段一致的测试配置。"""
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
        """直接 TS：合法 HTTP 206、IPv4 和 TS 检测通过，HLS 为 N/A。"""
        result = self.check("/direct.ts")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assert_status(result, "hls", "N/A")

    def test_direct_ts_http_200_ignoring_range_passes(self):
        """服务器忽略 Range 并返回合法 HTTP 200 时，允许检测。"""
        result = self.check("/direct-200.ts")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assert_status(result, "hls", "N/A")

    def test_hls_media_playlist_and_segments_pass(self):
        """HLS 媒体清单必须解析并实际检查多个 TS 分片。"""
        result = self.check("/media.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")

    def test_hls_master_playlist_resolves_relative_urls(self):
        """HLS 主清单应能解析媒体清单和相对分片地址。"""
        result = self.check("/master.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "hls", "PASS")
        self.assert_status(result, "ts", "PASS")

    def test_hls_invalid_ts_segments_do_not_pass(self):
        """HLS 分片不符合 TS 同步结构时，不得通过。"""
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

    def test_encrypted_hls_is_rejected_without_ts_failure(self):
        """加密 HLS 应识别类型，但不能把未检测的 TS 标记为失败。"""
        result = self.check("/encrypted.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_fmp4_hls_is_rejected_without_ts_failure(self):
        """fMP4 HLS 应识别类型，TS 未实际检测时保持 INCOMPLETE。"""
        result = self.check("/fmp4.m3u8")

        self.assertEqual(result["media_type"], "HLS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_channel_subscription_m3u_is_not_hls(self):
        """频道订阅 M3U 不能仅凭 EXTM3U 被识别为 HLS。"""
        result = self.check("/subscription.m3u")

        self.assertEqual(result["media_type"], "UNKNOWN")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        # 未知频道订阅不是 TS；没有执行 TS 检测，不能伪称 TS 检测失败。
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_unknown_content_is_rejected(self):
        """无法识别的媒体内容不得获得通过状态。"""
        result = self.check("/unknown.bin")

        self.assertEqual(result["media_type"], "UNKNOWN")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "hls", "FAIL")
        self.assert_status(result, "ts", "FAIL")

    def test_redirect_to_direct_ts_uses_ipv4(self):
        """重定向后的最终目标也必须成功建立 IPv4 连接。"""
        result = self.check("/redirect")

        self.assertEqual(result["media_type"], "DIRECT_TS")
        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "PASS")
        self.assert_status(result, "ts", "PASS")
        self.assert_status(result, "hls", "N/A")

    def test_redirect_loop_is_rejected(self):
        """重定向循环不能无限执行。"""
        result = self.check("/loop")

        self.assert_status(result, "http", "FAIL")

    def test_http_503_is_not_pass(self):
        """HTTP 503 必须记录为未完成，不能因为连通而判定 HTTP 通过。"""
        result = self.check("/unavailable")

        self.assert_status(result, "ipv4", "PASS")
        self.assert_status(result, "http", "INCOMPLETE")
        self.assert_status(result, "hls", "INCOMPLETE")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_invalid_content_range_is_rejected(self):
        """HTTP 206 的 Content-Range 不合法时必须拒绝。"""
        result = self.check("/bad-range")

        self.assert_status(result, "http", "FAIL")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_active_detection_budget_timeout(self):
        """超过单候选主动检测时间预算时，不得判定通过。"""
        result = self.check(
            "/slow.ts",
            candidate_active_budget_seconds=1,
        )

        self.assert_status(result, "http", "INCOMPLETE")
        self.assert_status(result, "ts", "INCOMPLETE")

    def test_max_redirect_limit_is_respected(self):
        """超过配置的最大重定向次数时必须停止。"""
        result = self.check(
            "/loop",
            max_redirects=2,
        )

        self.assert_status(result, "http", "FAIL")


if __name__ == "__main__":
    unittest.main(verbosity=2)

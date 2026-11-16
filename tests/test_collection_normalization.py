
import copy
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import collector
import normalizer


TEST_CHANNEL = "IPTV-Direct 模拟测试频道"


class LocalHTTPServer:
    """为采集测试提供本机模拟 HTTP 服务。"""

    def __init__(self):
        def response(body, content_type="text/plain; charset=utf-8",
                     status=200, headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            return status, content_type, data, headers or {}

        playlist = (
            "#EXTM3U\n"
            f'#EXTINF:-1 tvg-name="{TEST_CHANNEL}",{TEST_CHANNEL}\n'
            "http://example.invalid/live/test.ts\n"
        )
        txt = f"{TEST_CHANNEL},http://example.invalid/live/txt.ts\n"
        hls = (
            "#EXTM3U\n#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n"
            "segment.ts\n#EXT-X-ENDLIST\n"
        )

        self.routes = {
            "/bad.m3u": response("服务暂时不可用", status=503),
            "/playlist.m3u": response(playlist, "audio/x-mpegurl"),
            "/channels.txt": response(txt),
            "/redirect.txt": response(
                b"", status=302, headers={"Location": "/channels.txt"}
            ),
            "/partial.txt": response(
                txt, status=206,
                headers={"Content-Range": "bytes 0-71/72"}
            ),
            "/live.m3u8": response(hls, "audio/x-mpegurl"),
            "/child.m3u": response(
                '<html><a href="sub/hidden.m3u">hidden</a></html>',
                "text/html; charset=utf-8"
            ),
            "/large.txt": response("X" * 128),
            "/oversized-hls.m3u8": response(
                "#EXTM3U\n#EXT-X-VERSION:3\n" + ("X" * 96),
                "audio/x-mpegurl"
            ),
        }

        links = [
            "sub/one.m3u",
            "sub/one.m3u#duplicate",
            "sub/two.txt",
            "child.m3u",
        ]
        links.extend(
            f"sub/extra{number:02d}.m3u"
            for number in range(38)
        )
        links.extend(("child.html", "ftp://example.invalid/x.m3u"))

        self.routes["/index.html"] = response(
            "<html>" + "".join(
                f'<a href="{link}">{link}</a>' for link in links
            ) + "</html>",
            "text/html; charset=utf-8"
        )

        owner = self

        class Handler(BaseHTTPRequestHandler):
            request_counts = {}

            def do_GET(self):
                path = urlsplit(self.path).path
                self.request_counts[path] = (
                    self.request_counts.get(path, 0) + 1
                )
                item = owner.routes.get(path)

                if item is None and path.startswith("/sub/"):
                    item = response(playlist, "audio/x-mpegurl")

                if item is None:
                    item = response("Not found", status=404)

                status, content_type, body, headers = item
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))

                for name, value in headers.items():
                    self.send_header(name, value)

                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *args):
                return

        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    def start(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CollectorTests(unittest.TestCase):

    def setUp(self):
        self.http = LocalHTTPServer()
        self.base = self.http.start()

    def tearDown(self):
        self.http.close()

    def test_download_m3u_and_mime(self):
        url = f"{self.base}/playlist.m3u"
        result = collector.download_url(url, 10, 1048576, 5, 1048576)

        self.assertEqual(result["content_type"], "audio/x-mpegurl")
        self.assertIn(b"#EXTM3U", result["content"])

        parsed = normalizer.normalize_source_content(
            content=result["content"],
            source={"url": url, "final_url": result["final_url"]},
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=result["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        self.assertTrue(parsed["recognized"])
        self.assertEqual(parsed["format"], "m3u")
        self.assertEqual(len(parsed["candidates"]), 1)
        self.assertEqual(parsed["candidates"][0]["channel"], TEST_CHANNEL)

    def test_download_txt_and_parse(self):
        url = f"{self.base}/channels.txt"
        result = collector.download_url(url, 10, 1048576, 5, 1048576)

        parsed = normalizer.normalize_source_content(
            content=result["content"],
            source={"url": url, "final_url": result["final_url"]},
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=result["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        self.assertTrue(parsed["recognized"])
        self.assertEqual(parsed["format"], "txt")
        self.assertEqual(len(parsed["candidates"]), 1)

    def test_hls_manifest_is_recognized_without_channel_candidate(self):
        url = f"{self.base}/live.m3u8"
        result = collector.download_url(url, 10, 1048576, 5, 1048576)

        parsed = normalizer.normalize_source_content(
            content=result["content"],
            source={"url": url, "final_url": result["final_url"]},
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=result["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        self.assertTrue(parsed["recognized"])
        self.assertEqual(parsed["format"], "hls")
        self.assertEqual(parsed["candidates"], [])

    def test_manual_sources_first_and_failed_source_isolated(self):
        manual = [
            {
                "name": "失败人工来源",
                "url": f"{self.base}/bad.m3u",
                "source_type": "manual",
                "priority": 0,
                "order": 0,
            },
            {
                "name": "人工 M3U",
                "url": f"{self.base}/playlist.m3u",
                "source_type": "manual",
                "priority": 0,
                "order": 1,
            },
            {
                "name": "重定向 TXT",
                "url": f"{self.base}/redirect.txt",
                "source_type": "manual",
                "priority": 0,
                "order": 2,
            },
            {
                "name": "HTTP 206 TXT",
                "url": f"{self.base}/partial.txt",
                "source_type": "manual",
                "priority": 0,
                "order": 3,
            },
        ]
        automatic = [
            {
                "name": "自动 playlist",
                "url": f"{self.base}/live.m3u8",
                "source_type": "automatic",
                "mode": "playlist",
                "priority": 1,
                "order": 4,
            },
            {
                "name": "自动 index",
                "url": f"{self.base}/index.html",
                "source_type": "automatic",
                "mode": "index",
                "priority": 1,
                "order": 5,
            },
        ]
        settings = {
            "detection": {
                "source_download_timeout_seconds": 10,
                "max_source_download_bytes": 1048576,
                "max_redirects": 5,
                "max_hls_manifest_bytes": 1048576,
            },
            "collection": {"max_index_links_per_page": 40},
        }

        items = list(
            collector.iter_source_contents(manual, automatic, settings)
        )

        self.assertEqual(len(items), 45)
        self.assertEqual(
            [item["source"]["url"] for item in items[:4]],
            [source["url"] for source in manual],
        )
        self.assertIsNone(items[0]["download"])
        self.assertTrue(items[0]["errors"])
        self.assertIsNotNone(items[1]["download"])
        self.assertFalse(items[1]["errors"])
        self.assertEqual(
            items[2]["download"]["final_url"],
            f"{self.base}/channels.txt",
        )
        self.assertEqual(items[3]["download"]["http_status"], 206)
        self.assertEqual(items[4]["source"]["url"], f"{self.base}/live.m3u8")
        self.assertTrue(all(
            item["source"]["source_type"] == "automatic"
            for item in items[5:]
        ))

        redirected = normalizer.normalize_source_content(
            content=items[2]["download"]["content"],
            source={
                **items[2]["source"],
                "final_url": items[2]["download"]["final_url"],
            },
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=items[2]["download"]["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )
        self.assertTrue(redirected["recognized"])
        self.assertEqual(redirected["format"], "txt")
        self.assertEqual(len(redirected["candidates"]), 1)

    def test_index_links_relative_dedup_filter_limit_and_no_recursion(self):

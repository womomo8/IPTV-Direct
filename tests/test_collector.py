import io
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import collector


def make_settings():
    return {
        "detection": {
            "source_download_timeout_seconds": 5,
            "max_source_download_bytes": 1024,
            "max_redirects": 3,
            "max_hls_manifest_bytes": 512,
        },
        "collection": {
            "max_index_links_per_page": 3,
            "max_valid_sources": 40,
        },
    }


class FakeResponse:
    def __init__(self, content=b"sample playlist", status=200,
                 final_url="https://example.test/final.m3u",
                 content_type="text/plain"):
        self.stream = io.BytesIO(content)
        self.status = status
        self.final_url = final_url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size=-1): return self.stream.read(size)
    def getcode(self): return self.status
    def geturl(self): return self.final_url


class PrepareSourcesTests(unittest.TestCase):
    def test_manual_url_and_inline_m3u(self):
        manual = "\n".join([
            "# A comment", "https://manual.test/list.m3u", "#EXTM3U",
            "#EXTINF:-1,测试频道", "https://media.test/live.m3u8", "#END-M3U",
        ])
        config = {"sources": [{"id": "auto-1", "name": "自动来源",
            "url": "https://auto.test/list.m3u", "enabled": True, "mode": "playlist"}]}
        result = collector.prepare_sources(manual, config)
        self.assertEqual(len(result["manual_sources"]), 2)
        self.assertEqual(result["manual_sources"][0]["mode"], "manual")
        self.assertEqual(result["manual_sources"][1]["mode"], "inline")
        self.assertIn("#EXTINF:-1,测试频道", result["manual_sources"][1]["inline_content"])
        self.assertEqual(len(result["automatic_sources"]), 1)

    def test_inline_m3u_without_end_marker(self):
        result = collector.prepare_sources(
            "#EXTM3U\n#EXTINF:-1,频道\nhttps://media.test/a.m3u8", {"sources": []})
        self.assertEqual(result["manual_sources"], [])
        self.assertTrue(any("缺少 #END-M3U" in x for x in result["errors"]))

    def test_invalid_manual_url_is_skipped(self):
        result = collector.prepare_sources(
            "ftp://invalid.test/list\nhttps://valid.test/list.m3u", {"sources": []})
        self.assertEqual(len(result["manual_sources"]), 1)
        self.assertEqual(result["manual_sources"][0]["url"], "https://valid.test/list.m3u")

    def test_disabled_source_is_counted(self):
        result = collector.prepare_sources("", {"sources": [{
            "id": "off", "name": "停用来源", "url": "https://example.test/list.m3u",
            "enabled": False, "mode": "playlist"}]})
        self.assertEqual(result["disabled_sources"], 1)
        self.assertEqual(result["automatic_sources"], [])

    def test_invalid_top_level_config_raises(self):
        with self.assertRaises(collector.SourceConfigError):
            collector.prepare_sources("", [])


class UrlAndIndexTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertFalse(collector._valid_http_url("ftp://example.test/a"))
        self.assertFalse(collector._valid_http_url("https://user:pass@example.test/a"))
        self.assertFalse(collector._valid_http_url("https://example.test:99999/a"))
        self.assertTrue(collector._valid_http_url("https://example.test/a"))

    def test_source_key_ignores_fragment_and_default_port(self):
        self.assertEqual(collector._source_key("HTTPS://Example.test:443/a.m3u#one"),
                         collector._source_key("https://example.test/a.m3u#two"))

    def test_index_extracts_relative_links(self):
        html = b'<a href="../lists/cctv.m3u">one</a><a href="/lists/sports.m3u8">two</a><a href="/readme.html">ignore</a><a href="https://other.test/channels.txt">three</a>'
        self.assertEqual(collector.extract_playlist_links(
            html, "https://index.test/pages/index.html", 10), [
                "https://index.test/lists/cctv.m3u", "https://index.test/lists/sports.m3u8",
                "https://other.test/channels.txt"])

    def test_index_deduplicates_fragment_variants(self):
        html = b'<a href="/list.m3u#first">one</a><a href="/list.m3u#second">two</a>'
        self.assertEqual(collector.extract_playlist_links(html, "https://index.test/", 10),
                         ["https://index.test/list.m3u#first"])

    def test_index_obeys_link_limit(self):
        html = b"".join(f'<a href="/{i}.m3u">x</a>'.encode() for i in range(5))
        self.assertEqual(len(collector.extract_playlist_links(html, "https://index.test/", 2)), 2)

    def test_invalid_index_limit_raises(self):
        with self.assertRaises(ValueError):
            collector.extract_playlist_links(b"", "https://index.test/", 0)


class DownloadTests(unittest.TestCase):
    def test_download_returns_content_and_final_url(self):
        body = b"#EXTM3U\n#EXTINF:-1,Test\nhttps://media.test/live\n"
        response = FakeResponse(body, final_url="https://cdn.test/list.m3u",
                                content_type="application/vnd.apple.mpegurl")
        with patch("collector.build_opener") as mocked:
            mocked.return_value.open.return_value = response
            result = collector.download_url("https://origin.test/list.m3u", 5, 1024, 3, 512)
        self.assertEqual(result["content"], body)
        self.assertEqual(result["final_url"], "https://cdn.test/list.m3u")

    def test_download_rejects_unsolicited_206(self):
        response = FakeResponse(b"#EXTM3U\n#EXTINF:-1,Partial\nhttps://media.test/live\n", status=206)
        with patch("collector.build_opener") as mocked:
            mocked.return_value.open.return_value = response
            with self.assertRaisesRegex(collector.SourceDownloadError, "要求 HTTP 200"):
                collector.download_url("https://example.test/list.m3u", 5, 1024, 3, 512)

    def test_invalid_url_is_rejected_before_network(self):
        with patch("collector.build_opener") as mocked:
            with self.assertRaises(collector.SourceDownloadError):
                collector.download_url("file:///etc/passwd", 5, 1024, 3, 512)
        mocked.assert_not_called()

    def test_invalid_limits_are_rejected(self):
        with patch("collector.build_opener") as mocked:
            with self.assertRaises(collector.SourceDownloadError):
                collector.download_url("https://example.test/a", 0, 1024, 3, 512)
        mocked.assert_not_called()

    def test_source_size_limit(self):
        response = FakeResponse(b"x" * 20)
        with patch("collector.build_opener") as mocked:
            mocked.return_value.open.return_value = response
            with self.assertRaises(collector.SourceDownloadError):
                collector.download_url("https://example.test/a", 5, 10, 3, 8)

    def test_hls_manifest_size_limit(self):
        response = FakeResponse(b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n" + b"x" * 100)
        with patch("collector.build_opener") as mocked:
            mocked.return_value.open.return_value = response
            with self.assertRaises(collector.SourceDownloadError):
                collector.download_url("https://example.test/live.m3u8", 5, 1024, 3, 32)

    def test_redirect_rejects_non_http_target(self):
        handler, request = collector._LimitedRedirectHandler(3), Request("https://example.test/start")
        with self.assertRaises(HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "file:///etc/passwd")

    def test_redirect_count_limit(self):
        handler, request = collector._LimitedRedirectHandler(0), Request("https://example.test/start")
        with self.assertRaises(HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://other.test/end")


class StructureRecognitionTests(unittest.TestCase):
    def test_txt_url_is_valid(self):
        result = collector.inspect_source_structure({"content": b"https://media.test/live.m3u8\n",
                                                     "url": "https://source.test/list.txt"})
        self.assertTrue(result["structure_valid"])
        self.assertEqual(result["structure_format"], "txt")
        self.assertEqual(result["structure_entry_count"], 1)

    def test_txt_channel_url_row_is_valid(self):
        result = collector.inspect_source_structure({"content": "某频道,https://media.test/live\n"})
        self.assertTrue(result["structure_valid"])

    def test_m3u_requires_extinf_and_url(self):
        invalid = collector.inspect_source_structure({"content": "#EXTM3U\n#EXTINF:-1,频道\n".encode()})
        valid = collector.inspect_source_structure({"content": "#EXTM3U\n#EXTINF:-1,频道\nhttps://media.test/live\n".encode()})
        self.assertFalse(invalid["structure_valid"])
        self.assertTrue(valid["structure_valid"])
        self.assertEqual(valid["structure_format"], "m3u")

    def test_hls_requires_uri(self):
        invalid = collector.inspect_source_structure({"content": b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n",
                                                      "url": "https://source.test/live.m3u8"})
        valid = collector.inspect_source_structure({"content": b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nsegment001.ts\n",
                                                    "url": "https://source.test/live.m3u8"})
        self.assertFalse(invalid["structure_valid"])
        self.assertTrue(valid["structure_valid"])
        self.assertEqual(valid["structure_format"], "hls")


class IterSourceContentsTests(unittest.TestCase):
    def test_inline_manual_precedes_auto_and_structure_is_reported(self):
        manual = [{"url": "manual.txt#inline-m3u-1", "name": "内嵌来源", "mode": "inline",
                   "inline_content": "#EXTM3U\n#EXTINF:-1,内嵌\nhttps://media.test/a\n"},
                  {"url": "https://manual.test/list.m3u", "name": "人工 URL", "mode": "manual"}]
        automatic = [{"url": "https://auto.test/list.m3u", "name": "自动 URL", "mode": "playlist"}]
        def fake_download(url, *args):
            return {"url": url, "final_url": url,
                    "content": b"#EXTM3U\n#EXTINF:-1,Channel\nhttps://media.test/live\n",
                    "content_type": "text/plain", "http_status": 200, "truncated_media_probe": False}
        with patch("collector.download_url", side_effect=fake_download) as mocked:
            results = list(collector.iter_source_contents(manual, automatic, make_settings()))
        self.assertEqual([x["source"]["name"] for x in results], ["内嵌来源", "人工 URL", "自动 URL"])
        self.assertEqual(results[0]["download"]["http_status"], "LOCAL")
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(all(x["structure_valid"] for x in results))

    def test_failed_source_does_not_stop_later_sources(self):
        sources = [{"url": "https://bad.test/a.m3u", "name": "坏来源", "mode": "manual"},
                   {"url": "https://good.test/a.m3u", "name": "好来源", "mode": "manual"}]
        def fake_download(url, *args):
            if "bad.test" in url:
                raise collector.SourceDownloadError("模拟失败")
            return {"url": url, "final_url": url, "content": b"https://media.test/live\n",
                    "content_type": "text/plain", "http_status": 200, "truncated_media_probe": False}
        with patch("collector.download_url", side_effect=fake_download):
            results = list(collector.iter_source_contents(sources, [], make_settings()))
        self.assertIsNone(results[0]["download"])
        self.assertIn("模拟失败", results[0]["errors"][0])
        self.assertIsNotNone(results[1]["download"])
        self.assertEqual(results[1]["valid_source_count"], 1)

    def test_invalid_structure_does_not_consume_valid_source_limit(self):
        settings = make_settings()
        settings["collection"]["max_valid_sources"] = 1
        sources = [{"url": "https://bad.test/list", "name": "无效", "mode": "manual"},
                   {"url": "https://good.test/list", "name": "有效", "mode": "manual"},
                   {"url": "https://later.test/list", "name": "后续", "mode": "manual"}]
        def fake_download(url, *args):
            body = b"not a playlist" if "bad.test" in url else b"https://media.test/live\n"
            return {"url": url, "final_url": url, "content": body, "content_type": "text/plain",
                    "http_status": 200, "truncated_media_probe": False}
        with patch("collector.download_url", side_effect=fake_download) as mocked:
            results = list(collector.iter_source_contents(sources, [], settings))
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]["structure_valid"])
        self.assertTrue(results[1]["structure_valid"])
        self.assertEqual(mocked.call_count, 2)

    def test_index_expands_all_links_below_limit(self):
        index = {"url": "https://index.test/page.html", "name": "索引", "mode": "index"}
        html = b'<a href="/a.m3u">A</a><a href="/b.m3u8">B</a>'
        def fake_download(url, *args):
            body = html if url == index["url"] else b"#EXTM3U\n#EXTINF:-1,X\nhttps://media.test/live\n"
            return {"url": url, "final_url": url, "content": body,
                    "content_type": "text/html", "http_status": 200,
                    "truncated_media_probe": False}
        with patch("collector.download_url", side_effect=fake_download) as mocked:
            results = list(collector.iter_source_contents([], [index], make_settings()))
        self.assertEqual(len(results), 2)
        self.assertEqual([x["source"]["url"] for x in results], [
            "https://index.test/a.m3u", "https://index.test/b.m3u8"])
        self.assertEqual(mocked.call_count, 3)

    def test_index_stops_expanding_at_valid_source_limit(self):
        settings = make_settings()
        settings["collection"]["max_valid_sources"] = 1
        index = {"url": "https://index.test/page.html", "name": "索引", "mode": "index"}
        html = b'<a href="/a.m3u">A</a><a href="/b.m3u">B</a>'
        def fake_download(url, *args):
            body = html if url == index["url"] else b"#EXTM3U\n#EXTINF:-1,X\nhttps://media.test/live\n"
            return {"url": url, "final_url": url, "content": body, "content_type": "text/html",
                    "http_status": 200, "truncated_media_probe": False}
        with patch("collector.download_url", side_effect=fake_download) as mocked:
            results = list(collector.iter_source_contents([], [index], settings))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["structure_valid"])
        self.assertEqual(mocked.call_count, 2)

    def test_duplicate_sources_are_downloaded_once(self):
        manual = [{"url": "https://same.test/list.m3u#fragment", "name": "人工", "mode": "manual"}]
        automatic = [{"url": "https://same.test/list.m3u", "name": "自动", "mode": "playlist"}]
        def fake_download(url, *args):
            return {"url": url, "final_url": url, "content": b"https://media.test/live\n",
                    "content_type": "text/plain", "http_status": 200, "truncated_media_probe": False}
        with patch("collector.download_url", side_effect=fake_download) as mocked:
            results = list(collector.iter_source_contents(manual, automatic, make_settings()))
        self.assertEqual(len(results), 1)
        self.assertEqual(mocked.call_count, 1)


if __name__ == "__main__":
    unittest.main()

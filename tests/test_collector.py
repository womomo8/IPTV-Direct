import unittest
from unittest.mock import patch

import collector


class CollectorTests(unittest.TestCase):
    def test_prepare_manual_and_automatic_sources(self):
        manual = (
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV-1\n"
            "https://a.example/live.m3u8\n"
            "#END-M3U\n"
            "https://b.example/list.txt\n"
        )
        config = {
            "sources": [{
                "id": "source-a",
                "name": "自动来源",
                "url": "https://c.example/index",
                "enabled": True,
                "mode": "index",
            }]
        }

        result = collector.prepare_sources(manual, config)

        self.assertEqual(len(result["manual_sources"]), 2)
        self.assertEqual(len(result["automatic_sources"]), 1)
        self.assertEqual(result["manual_sources"][0]["mode"], "inline")
        self.assertEqual(result["automatic_sources"][0]["mode"], "index")

    def test_m3u_without_extinf_is_valid(self):
        result = collector.inspect_source_structure({
            "url": "https://a.example/list.m3u",
            "content": (
                "#EXTM3U\n"
                "https://a.example/live.m3u8\n"
            ),
        })

        self.assertTrue(result["structure_valid"])
        self.assertEqual(result["structure_format"], "m3u")
        self.assertEqual(result["structure_entry_count"], 1)

    def test_m3u_with_extinf_is_valid(self):
        result = collector.inspect_source_structure({
            "url": "https://a.example/list.m3u",
            "content": (
                "#EXTM3U\n"
                "#EXTINF:-1,CCTV-1\n"
                "https://a.example/live.m3u8\n"
            ),
        })

        self.assertTrue(result["structure_valid"])
        self.assertEqual(result["structure_entry_count"], 1)

    def test_txt_source_is_valid(self):
        result = collector.inspect_source_structure({
            "url": "https://a.example/list.txt",
            "content": "CCTV-1,https://a.example/live.m3u8\n",
        })

        self.assertTrue(result["structure_valid"])
        self.assertEqual(result["structure_format"], "txt")

    def test_index_links_are_deduplicated_and_limited(self):
        html = (
            '<a href="/one.m3u">one</a>'
            '<a href="/one.m3u#part">duplicate</a>'
            '<a href="/two.txt">two</a>'
        ).encode()

        result = collector.extract_playlist_links(
            html, "https://a.example/index", 1
        )

        self.assertEqual(result, ["https://a.example/one.m3u"])

    def test_manual_source_limit_stops_automatic_sources(self):
        manual = [{
            "url": "https://a.example/list.m3u",
            "mode": "manual",
        }]
        automatic = [{
            "url": "https://b.example/list.m3u",
            "mode": "playlist",
        }]
        settings = {
            "detection": {
                "source_download_timeout_seconds": 10,
                "max_source_download_bytes": 10000,
                "max_hls_manifest_bytes": 1000,
                "max_redirects": 2,
            },
            "collection": {
                "max_valid_sources": 1,
                "max_index_links_per_page": 10,
            },
        }
        downloaded = {
            "url": "https://a.example/list.m3u",
            "final_url": "https://a.example/list.m3u",
            "content": b"#EXTM3U\nhttps://a.example/live.m3u8\n",
            "content_type": "application/vnd.apple.mpegurl",
            "http_status": 200,
            "truncated_media_probe": False,
        }

        with patch("collector.download_url", return_value=downloaded) as mock:
            results = list(collector.iter_source_contents(
                manual, automatic, settings
            ))

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["structure_valid"])
        mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

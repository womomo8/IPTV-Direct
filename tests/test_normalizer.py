
"""normalizer.py：解析、频道匹配、URL 归一和去重专项测试。"""

import tempfile
import unittest
from pathlib import Path

from normalizer import (
    ChannelConfigError,
    _channel_match,
    load_channels,
    load_channel_categories,
    normalize_source_content,
    normalize_url,
    parse_source_content,
)


CHANNELS = ["CCTV-1", "CCTV-5", "湖南卫视"]

SOURCE = {
    "id": "test-001",
    "name": "模拟测试来源",
    "url": "https://example.test/list.m3u",
    "final_url": "https://example.test/list.m3u",
    "source_type": "manual",
    "priority": 0,
    "order": 3,
}


class ChannelConfigTests(unittest.TestCase):

    def test_load_channels_ignores_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.txt"
            path.write_text(
                "# 央视\nCCTV-1\nCCTV-5\nCCTV-1\n",
                encoding="utf-8",
            )

            self.assertEqual(
                load_channels(path),
                ["CCTV-1", "CCTV-5"],
            )

    def test_load_channels_rejects_empty_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.txt"
            path.write_text("# 频道配置\n\n", encoding="utf-8")

            with self.assertRaises(ChannelConfigError):
                load_channels(path)

    def test_load_channel_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.txt"
            path.write_text(
                "# 央视频道\nCCTV-1\nCCTV-5\n"
                "# 卫视频道\n湖南卫视\n",
                encoding="utf-8",
            )

            self.assertEqual(
                load_channel_categories(path),
                {
                    "CCTV-1": "央视频道",
                    "CCTV-5": "央视频道",
                    "湖南卫视": "卫视频道",
                },
            )


class URLNormalizationTests(unittest.TestCase):

    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            normalize_url("HTTPS://EXAMPLE.COM/live"),
            "https://example.com/live",
        )

    def test_removes_default_port(self):
        self.assertEqual(
            normalize_url("http://example.com:80/live"),
            "http://example.com/live",
        )
        self.assertEqual(
            normalize_url("https://example.com:443/live"),
            "https://example.com/live",
        )

    def test_preserves_non_default_port(self):
        self.assertEqual(
            normalize_url("https://example.com:8443/live"),
            "https://example.com:8443/live",
        )

    def test_preserves_query_parameters(self):
        self.assertEqual(
            normalize_url(
                "https://example.com/live?token=abc&id=1"
            ),
            "https://example.com/live?token=abc&id=1",
        )

    def test_removes_fragment(self):
        self.assertEqual(
            normalize_url("https://example.com/live#section"),
            "https://example.com/live",
        )

    def test_adds_root_path(self):
        self.assertEqual(
            normalize_url("https://example.com"),
            "https://example.com/",
        )

    def test_rejects_invalid_urls(self):
        invalid_urls = [
            "",
            "not-a-url",
            "ftp://example.com/live",
            "https://user:pass@example.com/live",
            "https://example.com:bad/live",
            "https://example.com:99999/live",
            "https://example.com/live path",
        ]

        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    normalize_url(url)


class ChannelMatchingTests(unittest.TestCase):

    def test_exact_match_is_case_insensitive(self):
        self.assertEqual(
            _channel_match(["cctv-1"], CHANNELS, None),
            "CCTV-1",
        )

    def test_explicit_alias_matches_target_channel(self):
        self.assertEqual(
            _channel_match(
                ["CCTV5"],
                CHANNELS,
                {"CCTV5": "CCTV-5"},
            ),
            "CCTV-5",
        )

    def test_unknown_alias_does_not_match(self):
        self.assertIsNone(
            _channel_match(
                ["CCTV5"],
                CHANNELS,
                {},
            )
        )

    def test_fuzzy_match_is_not_allowed(self):
        self.assertIsNone(
            _channel_match(
                ["CCTV-1高清频道"],
                CHANNELS,
                None,
            )
        )

    def test_invalid_alias_configuration_is_rejected(self):
        with self.assertRaises(TypeError):
            _channel_match(["CCTV-1"], CHANNELS, [])


class SourceParsingTests(unittest.TestCase):

    def test_parse_txt_channel_url_format(self):
        result = parse_source_content(
            "CCTV-1,https://example.test/live.ts\n",
            SOURCE,
            CHANNELS,
        )

        self.assertTrue(result["recognized"])
        self.assertEqual(result["format"], "txt")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(
            result["candidates"][0]["channel"],
            "CCTV-1",
        )
        self.assertEqual(
            result["candidates"][0]["raw_url"],
            "https://example.test/live.ts",
        )

    def test_parse_m3u_and_extract_channel_attributes(self):
        content = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="cctv1" '
            'tvg-name="CCTV-1" group-title="央视",中央一套\n'
            "https://example.test/live.m3u8\n"
        )

        result = parse_source_content(
            content,
            SOURCE,
            CHANNELS,
        )

        self.assertEqual(result["format"], "m3u")
        self.assertEqual(len(result["candidates"]), 1)

        candidate = result["candidates"][0]
        self.assertEqual(candidate["channel"], "CCTV-1")
        self.assertEqual(candidate["tvg_id"], "cctv1")
        self.assertEqual(candidate["tvg_name"], "CCTV-1")
        self.assertEqual(candidate["category"], "央视")

    def test_parse_m3u_relative_url(self):
        content = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="CCTV-1",CCTV-1\n'
            "../live/stream.ts\n"
        )

        result = parse_source_content(
            content,
            SOURCE,
            CHANNELS,
        )

        self.assertEqual(
            result["candidates"][0]["raw_url"],
            "https://example.test/live/stream.ts",
        )

    def test_m3u_alias_maps_to_canonical_channel(self):
        content = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="CCTV5",CCTV5\n'
            "https://example.test/live.ts\n"
        )

        result = parse_source_content(
            content,
            SOURCE,
            CHANNELS,
            aliases={"CCTV5": "CCTV-5"},
        )

        self.assertEqual(
            result["candidates"][0]["channel"],
            "CCTV-5",
        )

    def test_unknown_channel_is_not_forced_to_match(self):
        content = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="未知频道",未知频道\n'
            "https://example.test/live.ts\n"
        )

        result = parse_source_content(
            content,
            SOURCE,
            CHANNELS,
        )

        self.assertIsNone(result["candidates"][0]["channel"])
        self.assertTrue(result["errors"])

    def test_channel_subscription_is_not_hls_media_playlist(self):
        content = (
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-name="CCTV-1",CCTV-1\n'
            "https://example.test/live.m3u8\n"
        )

        result = parse_source_content(
            content,
            SOURCE,
            CHANNELS,
            content_type="application/vnd.apple.mpegurl",
        )

        self.assertEqual(result["format"], "m3u")
        self.assertEqual(
            result["candidates"][0]["channel"],
            "CCTV-1",
        )

    def test_hls_media_playlist_is_not_channel_subscription(self):
        content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10,\n"
            "segment001.ts\n"
            "#EXT-X-ENDLIST\n"
        )

        result = parse_source_content(
            content,
            SOURCE,
            CHANNELS,
        )

        self.assertEqual(result["format"], "hls")
        self.assertIsNone(result["candidates"][0]["channel"])

    def test_empty_content_is_not_recognized(self):
        result = parse_source_content(
            "\n  \n",
            SOURCE,
            CHANNELS,
        )

        self.assertFalse(result["recognized"])
        self.assertEqual(result["format"], "unknown")

    def test_invalid_candidate_limit_is_rejected(self):
        for limit in (0, -1, True, "10"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    parse_source_content(
                        "CCTV-1,https://example.test/live.ts",
                        SOURCE,
                        CHANNELS,
                        max_candidates=limit,
                    )


class SourceNormalizationTests(unittest.TestCase):

    def normalize(self, content, seen_urls=None, **kwargs):
        return normalize_source_content(
            content=content,
            source=SOURCE,
            channels=CHANNELS,
            max_candidates_per_source=100,
            seen_urls=seen_urls if seen_urls is not None else set(),
            **kwargs,
        )

    def test_normalizes_and_preserves_source_information(self):
        result = self.normalize(
            "CCTV-1,HTTPS://EXAMPLE.TEST:443/live.ts\n"
        )

        self.assertEqual(len(result["candidates"]), 1)

        candidate = result["candidates"][0]
        self.assertEqual(
            candidate["normalized_url"],
            "https://example.test/live.ts",
        )
        self.assertEqual(candidate["source_type"], "manual")
        self.assertEqual(candidate["priority"], 0)
        self.assertEqual(candidate["source_order"], 3)

    def test_global_duplicate_url_is_removed(self):
        seen = set()

        first = self.normalize(
            "CCTV-1,https://example.test/live.ts\n",
            seen_urls=seen,
        )
        second = self.normalize(
            "CCTV-5,https://example.test/live.ts\n",
            seen_urls=seen,
        )

        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(len(second["candidates"]), 0)
        self.assertEqual(second["duplicate_candidates"], 1)

    def test_query_parameters_are_part_of_duplicate_identity(self):
        seen = set()

        first = self.normalize(
            "CCTV-1,https://example.test/live?token=one\n",
            seen_urls=seen,
        )
        second = self.normalize(
            "CCTV-1,https://example.test/live?token=two\n",
            seen_urls=seen,
        )

        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(len(second["candidates"]), 1)

    def test_same_url_with_fragment_is_duplicate(self):
        seen = set()

        self.normalize(
            "CCTV-1,https://example.test/live.ts#one\n",
            seen_urls=seen,
        )
        second = self.normalize(
            "CCTV-1,https://example.test/live.ts#two\n",
            seen_urls=seen,
        )

        self.assertEqual(second["duplicate_candidates"], 1)
        self.assertEqual(second["candidates"], [])

    def test_channel_category_comes_from_channels_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.txt"
            path.write_text(
                "# 央视频道\nCCTV-1\nCCTV-5\n",
                encoding="utf-8",
            )

            result = self.normalize(
                "CCTV-1,https://example.test/live.ts\n",
                channels_path=str(path),
            )

        self.assertEqual(
            result["candidates"][0]["category"],
            "央视频道",
        )

    def test_single_source_candidate_limit(self):
        content = (
            "CCTV-1,https://example.test/one.ts\n"
            "CCTV-5,https://example.test/two.ts\n"
            "湖南卫视,https://example.test/three.ts\n"
        )

        result = normalize_source_content(
            content=content,
            source=SOURCE,
            channels=CHANNELS,
            max_candidates_per_source=1,
            seen_urls=set(),
        )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["extracted_candidates"], 1)
        self.assertTrue(result["limit_reached"])

    def test_remaining_candidate_limit(self):
        content = (
            "CCTV-1,https://example.test/one.ts\n"
            "CCTV-5,https://example.test/two.ts\n"
        )

        result = normalize_source_content(
            content=content,
            source=SOURCE,
            channels=CHANNELS,
            max_candidates_per_source=100,
            max_candidates_remaining=1,
            seen_urls=set(),
        )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["extracted_candidates"], 1)

    def test_seen_urls_must_be_shared_set(self):
        with self.assertRaises(TypeError):
            self.normalize(
                "CCTV-1,https://example.test/live.ts\n",
                seen_urls=[],
            )

    def test_invalid_candidate_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_source_content(
                content="CCTV-1,https://example.test/live.ts\n",
                source=SOURCE,
                channels=CHANNELS,
                max_candidates_per_source=0,
                seen_urls=set(),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

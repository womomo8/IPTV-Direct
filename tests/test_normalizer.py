
"""normalizer.py 候选额度与 URL 去重测试。"""

import unittest

from normalizer import normalize_source_content


CHANNEL = "CCTV-1"

SOURCE = {
    "url": "https://source.example/list.m3u",
    "final_url": "https://source.example/list.m3u",
    "source_type": "automatic",
    "priority": 1,
    "order": 0,
}


def make_m3u(entries):
    lines = ["#EXTM3U"]

    for channel, url in entries:
        lines.append(
            f'#EXTINF:-1 tvg-name="{channel}",{channel}'
        )
        lines.append(url)

    return "\n".join(lines)


def normalize(entries, seen_urls, source_limit=100, total_remaining=None):
    return normalize_source_content(
        content=make_m3u(entries),
        source=SOURCE,
        channels=[CHANNEL],
        max_candidates_per_source=source_limit,
        seen_urls=seen_urls,
        max_candidates_remaining=total_remaining,
    )


class NormalizerCandidateLimitTests(unittest.TestCase):

    def test_invalid_and_duplicate_urls_do_not_use_source_quota(self):
        seen_urls = set()

        entries = [
            (CHANNEL, "http://example.com:bad/invalid"),
            (CHANNEL, "http://example.com:80/a"),
            (CHANNEL, "HTTP://EXAMPLE.COM/a"),
            (CHANNEL, "https://example.com/b"),
            (CHANNEL, "https://example.com/c"),
        ]

        result = normalize(
            entries,
            seen_urls,
            source_limit=2,
        )

        self.assertEqual(result["extracted_candidates"], 2)
        self.assertEqual(result["retained_candidates"], 2)
        self.assertEqual(result["invalid_candidates"], 1)
        self.assertEqual(result["duplicate_candidates"], 1)
        self.assertEqual(result["matched_target_candidates"], 4)

        self.assertEqual(
            [item["normalized_url"] for item in result["candidates"]],
            [
                "http://example.com/a",
                "https://example.com/b",
            ],
        )

        self.assertTrue(result["limit_reached"])
        self.assertEqual(
            seen_urls,
            {
                "http://example.com/a",
                "https://example.com/b",
            },
        )

    def test_non_target_channel_does_not_use_quota(self):
        seen_urls = set()

        entries = [
            ("不是目标频道", "https://example.com/other"),
            (CHANNEL, "https://example.com/a"),
            (CHANNEL, "https://example.com/b"),
        ]

        result = normalize(
            entries,
            seen_urls,
            source_limit=1,
        )

        self.assertEqual(result["extracted_candidates"], 1)
        self.assertEqual(result["retained_candidates"], 1)
        self.assertEqual(result["matched_target_candidates"], 2)
        self.assertTrue(result["limit_reached"])

        self.assertEqual(
            [item["normalized_url"] for item in result["candidates"]],
            ["https://example.com/a"],
        )

        self.assertNotIn("https://example.com/other", seen_urls)
        self.assertNotIn("https://example.com/b", seen_urls)

    def test_global_remaining_quota_ignores_invalid_and_duplicate_urls(self):
        seen_urls = set()

        entries = [
            (CHANNEL, "https://example.com/a"),
            (CHANNEL, "https://example.com/a"),
            (CHANNEL, "http://example.com:bad/invalid"),
            (CHANNEL, "https://example.com/b"),
        ]

        result = normalize(
            entries,
            seen_urls,
            source_limit=100,
            total_remaining=1,
        )

        self.assertEqual(result["extracted_candidates"], 1)
        self.assertEqual(result["retained_candidates"], 1)
        self.assertEqual(result["duplicate_candidates"], 1)
        self.assertEqual(result["invalid_candidates"], 1)

        self.assertFalse(result["limit_reached"])
        self.assertTrue(result["total_limit_reached"])

        self.assertEqual(
            seen_urls,
            {"https://example.com/a"},
        )

    def test_source_and_global_limits_can_be_reached_together(self):
        seen_urls = set()

        entries = [
            (CHANNEL, "https://example.com/a"),
            (CHANNEL, "https://example.com/b"),
        ]

        result = normalize(
            entries,
            seen_urls,
            source_limit=1,
            total_remaining=1,
        )

        self.assertEqual(result["extracted_candidates"], 1)
        self.assertTrue(result["limit_reached"])
        self.assertTrue(result["total_limit_reached"])

        self.assertEqual(
            seen_urls,
            {"https://example.com/a"},
        )

    def test_exact_source_limit_does_not_report_overflow(self):
        seen_urls = set()

        entries = [
            (CHANNEL, f"https://example.com/{index}")
            for index in range(100)
        ]

        result = normalize(
            entries,
            seen_urls,
            source_limit=100,
        )

        self.assertEqual(result["extracted_candidates"], 100)
        self.assertEqual(result["retained_candidates"], 100)
        self.assertFalse(result["limit_reached"])
        self.assertEqual(len(seen_urls), 100)

    def test_next_unique_url_after_source_limit_is_not_retained(self):
        seen_urls = set()

        entries = [
            (CHANNEL, f"https://example.com/{index}")
            for index in range(101)
        ]

        result = normalize(
            entries,
            seen_urls,
            source_limit=100,
        )

        self.assertEqual(result["extracted_candidates"], 100)
        self.assertEqual(result["retained_candidates"], 100)
        self.assertTrue(result["limit_reached"])

        self.assertNotIn(
            "https://example.com/100",
            seen_urls,
        )

    def test_global_quota_accumulates_across_sources(self):
        seen_urls = set()

        first = normalize(
            [
                (CHANNEL, "https://example.com/a"),
                (CHANNEL, "https://example.com/b"),
            ],
            seen_urls,
            source_limit=100,
            total_remaining=2,
        )

        second = normalize(
            [
                (CHANNEL, "https://example.com/a"),
                (CHANNEL, "https://example.com/c"),
                (CHANNEL, "https://example.com/d"),
            ],
            seen_urls,
            source_limit=100,
            total_remaining=1,
        )

        self.assertEqual(first["extracted_candidates"], 2)
        self.assertEqual(second["extracted_candidates"], 1)
        self.assertEqual(second["duplicate_candidates"], 1)
        self.assertTrue(second["total_limit_reached"])

        self.assertEqual(
            seen_urls,
            {
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            },
        )


if __name__ == "__main__":
    unittest.main()

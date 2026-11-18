"""normalizer.py 候选额度、来源身份和来源统计测试。"""

import unittest

from normalizer import normalize_source_content


CHANNEL = "CCTV-1"

SOURCE = {
    "url": "https://source.example/list.m3u",
    "final_url": "https://source.example/list.m3u",
    "name": "测试来源",
    "id": "source-1",
    "source_type": "automatic",
    "priority": 1,
    "order": 0,
    "mode": "playlist",
}


def make_m3u(entries):
    lines = ["#EXTM3U"]
    for channel, url in entries:
        lines.append(f'#EXTINF:-1 tvg-name="{channel}",{channel}')
        lines.append(url)
    return "\n".join(lines)


def normalize(entries, seen_urls, source_limit=100, total_remaining=None,
              source=SOURCE, source_history_by_url=None, aliases=None):
    return normalize_source_content(
        content=make_m3u(entries),
        source=source,
        channels=[CHANNEL],
        max_candidates_per_source=source_limit,
        seen_urls=seen_urls,
        max_candidates_remaining=total_remaining,
        source_history_by_url=source_history_by_url,
        aliases=aliases,
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
        result = normalize(entries, seen_urls, source_limit=2)
        self.assertEqual(result["extracted_candidates"], 2)
        self.assertEqual(result["retained_candidates"], 2)
        self.assertEqual(result["invalid_candidates"], 1)
        self.assertEqual(result["duplicate_candidates"], 1)
        self.assertEqual(result["matched_target_candidates"], 4)
        self.assertEqual(
            [item["normalized_url"] for item in result["candidates"]],
            ["http://example.com/a", "https://example.com/b"],
        )
        self.assertTrue(result["limit_reached"])
        self.assertEqual(seen_urls, {"http://example.com/a", "https://example.com/b"})

    def test_non_target_channel_does_not_use_quota(self):
        seen_urls = set()
        entries = [
            ("不是目标频道", "https://example.com/other"),
            (CHANNEL, "https://example.com/a"),
            (CHANNEL, "https://example.com/b"),
        ]
        result = normalize(entries, seen_urls, source_limit=1)
        self.assertEqual(result["extracted_candidates"], 1)
        self.assertEqual(result["retained_candidates"], 1)
        self.assertEqual(result["matched_target_candidates"], 2)
        self.assertTrue(result["limit_reached"])
        self.assertEqual([item["normalized_url"] for item in result["candidates"]], ["https://example.com/a"])
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
        result = normalize(entries, seen_urls, source_limit=100, total_remaining=1)
        self.assertEqual(result["extracted_candidates"], 1)
        self.assertEqual(result["retained_candidates"], 1)
        self.assertEqual(result["duplicate_candidates"], 1)
        self.assertEqual(result["invalid_candidates"], 1)
        self.assertFalse(result["limit_reached"])
        self.assertTrue(result["total_limit_reached"])
        self.assertEqual(seen_urls, {"https://example.com/a"})

    def test_source_and_global_limits_can_be_reached_together(self):
        seen_urls = set()
        result = normalize(
            [(CHANNEL, "https://example.com/a"), (CHANNEL, "https://example.com/b")],
            seen_urls, source_limit=1, total_remaining=1,
        )
        self.assertEqual(result["extracted_candidates"], 1)
        self.assertTrue(result["limit_reached"])
        self.assertTrue(result["total_limit_reached"])
        self.assertEqual(seen_urls, {"https://example.com/a"})

    def test_exact_source_limit_does_not_report_overflow(self):
        seen_urls = set()
        entries = [(CHANNEL, f"https://example.com/{index}") for index in range(100)]
        result = normalize(entries, seen_urls, source_limit=100)
        self.assertEqual(result["extracted_candidates"], 100)
        self.assertEqual(result["retained_candidates"], 100)
        self.assertFalse(result["limit_reached"])
        self.assertEqual(len(seen_urls), 100)

    def test_next_unique_url_after_source_limit_is_not_retained(self):
        seen_urls = set()
        entries = [(CHANNEL, f"https://example.com/{index}") for index in range(101)]
        result = normalize(entries, seen_urls, source_limit=100)
        self.assertEqual(result["extracted_candidates"], 100)
        self.assertEqual(result["retained_candidates"], 100)
        self.assertTrue(result["limit_reached"])
        self.assertNotIn("https://example.com/100", seen_urls)

    def test_global_quota_accumulates_across_sources(self):
        seen_urls = set()
        first = normalize(
            [(CHANNEL, "https://example.com/a"), (CHANNEL, "https://example.com/b")],
            seen_urls, source_limit=100, total_remaining=2,
        )
        second = normalize(
            [(CHANNEL, "https://example.com/a"), (CHANNEL, "https://example.com/c"),
             (CHANNEL, "https://example.com/d")],
            seen_urls, source_limit=100, total_remaining=1,
        )
        self.assertEqual(first["extracted_candidates"], 2)
        self.assertEqual(second["extracted_candidates"], 1)
        self.assertEqual(second["duplicate_candidates"], 1)
        self.assertTrue(second["total_limit_reached"])
        self.assertEqual(seen_urls, {
            "https://example.com/a", "https://example.com/b", "https://example.com/c",
        })


class NormalizerSourceIdentityTests(unittest.TestCase):
    def test_candidate_contains_complete_primary_source_identity(self):
        source = {**SOURCE, "source_type": "manual", "name": "人工源", "id": None}
        result = normalize([(CHANNEL, "https://media.example/live")], set(), source=source)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["source_id"], None)
        self.assertEqual(candidate["source_name"], "人工源")
        self.assertTrue(candidate["is_manual"])
        self.assertEqual(candidate["primary_source"]["source_url"], source["url"])
        self.assertEqual(candidate["primary_source"]["source_final_url"], source["final_url"])
        self.assertEqual(candidate["source_history"], [candidate["primary_source"]])
        self.assertIsNone(candidate["media_type"])

    def test_duplicate_url_adds_source_history_without_changing_primary(self):
        seen_urls = set()
        source_history_by_url = {}
        first_source = {**SOURCE, "name": "人工优先源", "source_type": "manual", "id": None, "order": 0}
        second_source = {**SOURCE, "url": "https://second.example/list.m3u", "name": "自动重复源",
                         "source_type": "automatic", "id": "source-2", "order": 1}
        first = normalize(
            [(CHANNEL, "https://media.example/live")], seen_urls,
            source=first_source, source_history_by_url=source_history_by_url,
        )
        second = normalize(
            [(CHANNEL, "https://media.example/live")], seen_urls,
            source=second_source, source_history_by_url=source_history_by_url,
        )
        candidate = first["candidates"][0]
        self.assertEqual(candidate["primary_source"]["source_name"], "人工优先源")
        self.assertEqual([x["source_name"] for x in candidate["source_history"]], ["人工优先源", "自动重复源"])
        self.assertEqual(second["duplicate_candidates"], 1)
        self.assertEqual(second["duplicate_source_records"][0]["normalized_url"], "https://media.example/live")
        self.assertEqual(len(second["candidates"]), 0)

    def test_channel_valid_source_count_is_per_source_and_does_not_require_new_url(self):
        seen_urls = {"https://media.example/live"}
        duplicate_only = normalize([(CHANNEL, "https://media.example/live")], seen_urls)
        nonmatching = normalize([("其他频道", "https://other.example/live")], seen_urls)
        self.assertEqual(duplicate_only["channel_valid_source_count"], 1)
        self.assertTrue(duplicate_only["channel_valid_source"])
        self.assertEqual(nonmatching["channel_valid_source_count"], 0)
        self.assertFalse(nonmatching["channel_valid_source"])

    def test_channel_valid_source_counts_exact_match_even_if_url_is_invalid(self):
        result = normalize([(CHANNEL, "http://example.com:bad/invalid")], set())
        self.assertEqual(result["matched_target_candidates"], 0)
        self.assertEqual(result["matched_channel_candidates"], 1)
        self.assertEqual(result["channel_valid_source_count"], 1)

    def test_alias_matching_remains_explicit_and_exact(self):
        result = normalize(
            [("CCTV1", "https://media.example/live")], set(), aliases={"CCTV1": CHANNEL},
        )
        self.assertEqual(result["candidates"][0]["channel"], CHANNEL)
        no_match = normalize([("CCTV-1高清版", "https://media.example/other")], set())
        self.assertEqual(no_match["matched_target_candidates"], 0)


if __name__ == "__main__":
    unittest.main()

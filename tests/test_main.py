import unittest
import main

class PublisherHandoffVerificationTests(unittest.TestCase):
    def _candidate(self, channel, url, source_id):
        return {
            "channel": channel,
            "normalized_url": url,
            "primary_source": {
                "source_id": source_id,
            },
        }

    def test_complete_queue_and_excluded_are_accepted(self):
        queue_candidate = self._candidate(
            "CCTV-1",
            "https://a.example/live.m3u8",
            "source-a",
        )
        excluded_candidate = self._candidate(
            "CCTV-2",
            "https://b.example/live.m3u8",
            "source-b",
        )

        result = {
            "queue": [queue_candidate],
            "excluded": [{
                "candidate": excluded_candidate,
                "identity": (
                    "CCTV-2",
                    "https://b.example/live.m3u8",
                ),
                "reason": "本地检测未通过",
            }],
            "ranking": [{
                "identity": (
                    "CCTV-1",
                    "https://a.example/live.m3u8",
                ),
            }],
        }

        returned = main._verify_publisher_result(
            [queue_candidate, excluded_candidate],
            result,
        )

        self.assertEqual(returned, [queue_candidate])

    def test_missing_candidate_is_rejected(self):
        queue_candidate = self._candidate(
            "CCTV-1",
            "https://a.example/live.m3u8",
            "source-a",
        )
        missing_candidate = self._candidate(
            "CCTV-2",
            "https://b.example/live.m3u8",
            "source-b",
        )

        result = {
            "queue": [queue_candidate],
            "excluded": [],
            "ranking": [{
                "identity": (
                    "CCTV-1",
                    "https://a.example/live.m3u8",
                ),
            }],
        }

        with self.assertRaises(main.PipelineError):
            main._verify_publisher_result(
                [queue_candidate, missing_candidate],
                result,
            )

    def test_candidate_in_both_queue_and_excluded_is_rejected(self):
        queue_candidate = self._candidate(
            "CCTV-1",
            "https://a.example/live.m3u8",
            "source-a",
        )
        other_candidate = self._candidate(
            "CCTV-2",
            "https://b.example/live.m3u8",
            "source-b",
        )

        result = {
            "queue": [queue_candidate],
            "excluded": [{
                "candidate": queue_candidate,
                "identity": (
                    "CCTV-1",
                    "https://a.example/live.m3u8",
                ),
                "reason": "重复交接测试",
            }],
            "ranking": [{
                "identity": (
                    "CCTV-1",
                    "https://a.example/live.m3u8",
                ),
            }],
        }

        with self.assertRaises(main.PipelineError):
            main._verify_publisher_result(
                [queue_candidate, other_candidate],
                result,
            )

    def test_excluded_identity_mismatch_is_rejected(self):
        queue_candidate = self._candidate(
            "CCTV-1",
            "https://a.example/live.m3u8",
            "source-a",
        )
        excluded_candidate = self._candidate(
            "CCTV-2",
            "https://b.example/live.m3u8",
            "source-b",
        )

        result = {
            "queue": [queue_candidate],
            "excluded": [{
                "candidate": excluded_candidate,
                "identity": (
                    "CCTV-3",
                    "https://b.example/live.m3u8",
                ),
                "reason": "身份不匹配测试",
            }],
            "ranking": [{
                "identity": (
                    "CCTV-1",
                    "https://a.example/live.m3u8",
                ),
            }],
        }

        with self.assertRaises(main.PipelineError):
            main._verify_publisher_result(
                [queue_candidate, excluded_candidate],
                result,
            )

    def test_unknown_excluded_candidate_is_rejected(self):
        queue_candidate = self._candidate(
            "CCTV-1",
            "https://a.example/live.m3u8",
            "source-a",
        )
        checked_candidate = self._candidate(
            "CCTV-2",
            "https://b.example/live.m3u8",
            "source-b",
        )
        unknown_candidate = self._candidate(
            "CCTV-3",
            "https://c.example/live.m3u8",
            "source-c",
        )

        result = {
            "queue": [queue_candidate],
            "excluded": [{
                "candidate": unknown_candidate,
                "identity": (
                    "CCTV-3",
                    "https://c.example/live.m3u8",
                ),
                "reason": "未知候选测试",
            }],
            "ranking": [{
                "identity": (
                    "CCTV-1",
                    "https://a.example/live.m3u8",
                ),
            }],
        }

        with self.assertRaises(main.PipelineError):
            main._verify_publisher_result(
                [queue_candidate, checked_candidate],
                result,
            )

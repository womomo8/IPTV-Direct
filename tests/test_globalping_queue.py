
import tempfile
import unittest
from unittest.mock import patch

import globalping


class GlobalpingQueueExecutionTests(unittest.TestCase):
    """验证 Globalping 按原队列顺序候补及频道名额动态停止。"""

    @staticmethod
    def _candidate(channel, url):
        return {
            "channel": channel,
            "media_type": "DIRECT_TS",
            "normalized_url": url,
            "checks": {
                "ipv4": {"status": globalping.PASS},
                "http": {"status": globalping.PASS},
                "hls": {"status": globalping.NA},
                "ts": {"status": globalping.PASS},
            },
        }

    @staticmethod
    def _remote_result(state):
        return {
            "overall": state,
            "checks": {
                "ipv4": globalping.NA,
                "http": state,
                "hls": globalping.NA,
            },
        }

    @staticmethod
    def _common_patches():
        return (
            patch.object(
                globalping,
                "_read_limits",
                return_value={
                    "remaining": 10,
                    "rate_remaining": 10,
                    "credits_remaining": 0,
                },
            ),
            patch.object(
                globalping,
                "_probe_locations",
                return_value=([{"id": "probe-1"}], 1),
            ),
            patch.object(
                globalping,
                "_publish",
                return_value={"status": "published", "reason": ""},
            ),
        )

    def test_remote_failure_continues_to_next_candidate_in_original_order(self):
        first = self._candidate(
            "CCTV-1", "https://a.example/first.ts"
        )
        second = self._candidate(
            "CCTV-1", "https://a.example/second.ts"
        )
        third = self._candidate(
            "CCTV-2", "https://b.example/third.ts"
        )

        states = {
            first["normalized_url"]: globalping.FAIL,
            second["normalized_url"]: globalping.PASS,
            third["normalized_url"]: globalping.PASS,
        }
        calls = []

        def fake_candidate_remote(
            candidate, locations, probe_limit, boundary, budget, stats
        ):
            url = candidate["normalized_url"]
            calls.append(url)
            stats["attempted_candidates"] += 1
            return self._remote_result(states[url])

        read_limits, probe_locations, publish = self._common_patches()

        with tempfile.TemporaryDirectory() as directory:
            with read_limits, probe_locations, publish, patch.object(
                globalping,
                "_candidate_remote",
                side_effect=fake_candidate_remote,
            ):
                result = globalping.run_globalping(
                    [first, second, third],
                    {
                        "globalping": {"max_probes": 3},
                        "selection": {"max_routes_per_channel": 2},
                    },
                    ["CCTV-1", "CCTV-2"],
                    output_dir=directory,
                )

        expected_order = [
            first["normalized_url"],
            second["normalized_url"],
            third["normalized_url"],
        ]
        self.assertEqual(calls, expected_order)
        self.assertEqual(
            [item["admission"] for item in result["results"]],
            ["REJECTED_REMOTE", "ADMITTED", "ADMITTED"],
        )
        self.assertEqual(
            result["accepted"],
            [
                {
                    "channel": "CCTV-1",
                    "normalized_url": second["normalized_url"],
                },
                {
                    "channel": "CCTV-2",
                    "normalized_url": third["normalized_url"],
                },
            ],
        )
        self.assertEqual(result["stats"]["remote_fail_candidates"], 1)
        self.assertEqual(result["stats"]["admitted_candidates"], 2)

    def test_channel_cap_skips_measurement_but_other_channels_continue(self):
        first = self._candidate(
            "CCTV-1", "https://a.example/first.ts"
        )
        second = self._candidate(
            "CCTV-1", "https://a.example/second.ts"
        )
        third = self._candidate(
            "CCTV-1", "https://a.example/third.ts"
        )
        other = self._candidate(
            "CCTV-2", "https://b.example/first.ts"
        )

        calls = []

        def fake_candidate_remote(
            candidate, locations, probe_limit, boundary, budget, stats
        ):
            url = candidate["normalized_url"]
            calls.append(url)
            stats["attempted_candidates"] += 1
            return self._remote_result(globalping.PASS)

        read_limits, probe_locations, publish = self._common_patches()

        with tempfile.TemporaryDirectory() as directory:
            with read_limits, probe_locations, publish, patch.object(
                globalping,
                "_candidate_remote",
                side_effect=fake_candidate_remote,
            ):
                result = globalping.run_globalping(
                    [first, second, third, other],
                    {
                        "globalping": {"max_probes": 3},
                        "selection": {"max_routes_per_channel": 2},
                    },
                    ["CCTV-1", "CCTV-2"],
                    output_dir=directory,
                )

        expected_measurements = [
            first["normalized_url"],
            second["normalized_url"],
            other["normalized_url"],
        ]
        self.assertEqual(calls, expected_measurements)

        self.assertEqual(
            [item["admission"] for item in result["results"]],
            [
                "ADMITTED",
                "ADMITTED",
                "SKIPPED_CHANNEL_FULL",
                "ADMITTED",
            ],
        )
        self.assertEqual(result["stats"]["channel_full_skipped"], 1)
        self.assertEqual(result["stats"]["admitted_candidates"], 3)
        self.assertEqual(
            [item["normalized_url"] for item in result["accepted"]],
            expected_measurements,
        )


if __name__ == "__main__":
    unittest.main()

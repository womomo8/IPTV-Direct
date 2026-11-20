
import unittest

import globalping


class RemoteAdmissionTests(unittest.TestCase):
    def test_hls_rejects_incomplete_in_any_applicable_check(self):
        candidate = {"media_type": "HLS"}
        checks = {
            "ipv4": globalping.PASS,
            "http": globalping.PASS,
            "hls": globalping.PASS,
        }

        for key in ("ipv4", "http", "hls"):
            with self.subTest(check=key):
                remote_checks = dict(checks)
                remote_checks[key] = globalping.INCOMPLETE
                accepted, reason = globalping._remote_admission(
                    candidate,
                    {
                        "checks": remote_checks,
                        "overall": globalping.INCOMPLETE,
                    },
                )
                self.assertFalse(accepted)
                self.assertIn("未完成", reason)

    def test_direct_ts_rejects_incomplete_http_check(self):
        accepted, reason = globalping._remote_admission(
            {"media_type": "DIRECT_TS"},
            {
                "checks": {
                    "ipv4": globalping.NA,
                    "http": globalping.INCOMPLETE,
                    "hls": globalping.NA,
                },
                "overall": globalping.INCOMPLETE,
            },
        )
        self.assertFalse(accepted)
        self.assertIn("未完成", reason)

    def test_all_applicable_checks_pass_allows_admission(self):
        cases = [
            (
                {"media_type": "HLS"},
                {
                    "ipv4": globalping.PASS,
                    "http": globalping.PASS,
                    "hls": globalping.PASS,
                },
            ),
            (
                {"media_type": "DIRECT_TS"},
                {
                    "ipv4": globalping.NA,
                    "http": globalping.PASS,
                    "hls": globalping.NA,
                },
            ),
        ]

        for candidate, checks in cases:
            with self.subTest(media_type=candidate["media_type"]):
                accepted, reason = globalping._remote_admission(
                    candidate,
                    {"checks": checks, "overall": globalping.PASS},
                )
                self.assertTrue(accepted, reason)

    def test_overall_status_mismatch_rejects_admission(self):
        accepted, reason = globalping._remote_admission(
            {"media_type": "DIRECT_TS"},
            {
                "checks": {
                    "ipv4": globalping.NA,
                    "http": globalping.PASS,
                    "hls": globalping.NA,
                },
                "overall": globalping.INCOMPLETE,
            },
        )
        self.assertFalse(accepted)
        self.assertIn("总体状态", reason)


if __name__ == "__main__":
    unittest.main()

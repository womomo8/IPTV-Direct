import unittest

import main


class GlobalpingResultHandoffTests(unittest.TestCase):
    def setUp(self):
        self.queue = [
            {
                "channel": "CCTV1",
                "normalized_url": "https://one.example/live.m3u8",
                "primary_source": {"source_id": "source-one"},
            },
            {
                "channel": "CCTV2",
                "normalized_url": "https://two.example/live.ts",
                "primary_source": {"source_id": "source-two"},
            },
        ]
        self.identities = [
            ("CCTV1", "https://one.example/live.m3u8"),
            ("CCTV2", "https://two.example/live.ts"),
        ]

    def make_result(self, admissions=("ADMITTED", "REJECTED_REMOTE")):
        results = [
            {"identity": identity, "admission": admission}
            for identity, admission in zip(self.identities, admissions)
        ]
        accepted = [
            {"channel": identity[0], "normalized_url": identity[1]}
            for identity, admission in zip(self.identities, admissions)
            if admission == "ADMITTED"
        ]
        return {
            "input_queue_identities": list(self.identities),
            "stats": {},
            "results": results,
            "accepted": accepted,
            "publication": {"status": "published"},
        }

    def assert_invalid(self, result):
        with self.assertRaises(main.PipelineError):
            main._verify_globalping_result(self.queue, result)

    def test_valid_results_preserve_queue_order_and_accepted(self):
        result = self.make_result()
        stats, records, publication = main._verify_globalping_result(
            self.queue, result
        )
        self.assertEqual(stats, {})
        self.assertEqual(
            [tuple(item["identity"]) for item in records],
            self.identities,
        )
        self.assertEqual(publication["status"], "published")

    def test_dry_run_not_run_records_are_valid(self):
        result = self.make_result(("NOT_RUN", "NOT_RUN"))
        result["accepted"] = []
        result["publication"] = {"status": "not_run"}
        _, records, publication = main._verify_globalping_result(
            self.queue, result
        )
        self.assertEqual(len(records), len(self.queue))
        self.assertEqual(publication["status"], "not_run")

    def test_preflight_blocked_empty_result_is_valid(self):
        for flag in ("preflight_failed", "no_probes"):
            with self.subTest(flag=flag):
                result = {
                    "input_queue_identities": list(self.identities),
                    "stats": {flag: 1},
                    "results": [],
                    "accepted": [],
                    "publication": {"status": "blocked"},
                }
                stats, records, publication = main._verify_globalping_result(
                    self.queue, result
                )
                self.assertEqual(stats[flag], 1)
                self.assertEqual(records, [])
                self.assertEqual(publication["status"], "blocked")

    def test_missing_or_changed_input_queue_is_rejected(self):
        missing = self.make_result()
        del missing["input_queue_identities"]
        self.assert_invalid(missing)

        changed = self.make_result()
        changed["input_queue_identities"].reverse()
        self.assert_invalid(changed)

    def test_missing_short_or_long_results_are_rejected(self):
        missing = self.make_result()
        del missing["results"]
        self.assert_invalid(missing)

        short = self.make_result()
        short["results"].pop()
        self.assert_invalid(short)

        long = self.make_result()
        long["results"].append({
            "identity": self.identities[-1],
            "admission": "REJECTED_REMOTE",
        })
        self.assert_invalid(long)

    def test_result_identity_order_and_record_shape_are_validated(self):
        reversed_results = self.make_result()
        reversed_results["results"].reverse()
        self.assert_invalid(reversed_results)

        malformed = self.make_result()
        malformed["results"][0] = "not-a-record"
        self.assert_invalid(malformed)

    def test_admission_must_be_a_known_string(self):
        for invalid_admission in (None, [], {}, "UNKNOWN"):
            with self.subTest(admission=invalid_admission):
                result = self.make_result()
                result["results"][0]["admission"] = invalid_admission
                self.assert_invalid(result)

    def test_accepted_must_match_admitted_records_exactly(self):
        missing_accepted = self.make_result()
        missing_accepted["accepted"] = []
        self.assert_invalid(missing_accepted)

        extra_accepted = self.make_result(
            ("REJECTED_REMOTE", "REJECTED_REMOTE")
        )
        extra_accepted["accepted"] = [{
            "channel": self.identities[0][0],
            "normalized_url": self.identities[0][1],
        }]
        self.assert_invalid(extra_accepted)

    def test_conflicting_blocked_flags_or_nonempty_blocked_results_are_rejected(self):
        result = {
            "input_queue_identities": list(self.identities),
            "stats": {"preflight_failed": 1, "no_probes": 1},
            "results": [],
            "accepted": [],
            "publication": {"status": "blocked"},
        }
        self.assert_invalid(result)

        result["stats"] = {"preflight_failed": 1}
        result["results"] = [{
            "identity": self.identities[0],
            "admission": "NOT_RUN",
        }]
        self.assert_invalid(result)


if __name__ == "__main__":
    unittest.main()

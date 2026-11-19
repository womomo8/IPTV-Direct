import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import globalping


class GlobalpingAggregationTests(unittest.TestCase):
    def test_http_any_valid_success_wins(self):
        measurement = {"results": [
            {"probe": {"city": "A"}, "result": {"status": "finished", "statusCode": 200, "rawBody": "ok"}},
            {"probe": {"city": "B"}, "result": {"status": "finished", "statusCode": 503}},
        ]}
        state, raw, success = globalping._http_aggregate(measurement)
        self.assertEqual(state, globalping.PASS)
        self.assertEqual(len(success), 1)
        self.assertEqual(len(raw), 2)

    def test_partial_206_http_response_is_incomplete(self):
        measurement = {"results": [
            {"probe": {"city": "A"}, "result": {
                "status": "finished", "statusCode": 206,
                "headers": {"content-range": "bytes 0-1023/5000", "content-length": "1024"},
                "rawBody": "x" * 1024,
            }},
        ]}
        state, _, success = globalping._http_aggregate(measurement)
        self.assertEqual(state, globalping.INCOMPLETE)
        self.assertEqual(success, [])

    def test_http_probe_failure_without_target_response_is_incomplete(self):
        measurement = {"results": [
            {"probe": {"city": "A"}, "result": {"status": "offline", "rawOutput": "probe offline"}},
        ]}
        state, _, success = globalping._http_aggregate(measurement)
        self.assertEqual(state, globalping.INCOMPLETE)
        self.assertEqual(success, [])

    def test_partial_206_manifest_is_not_success(self):
        record = {"status_code": 206, "body": "#EXTM3U\n#EXT-X-TARGETDURATION:6\n",
                  "headers": {"content-range": "bytes 0-30/200", "content-length": "30"}}
        complete, reason = globalping._hls_content_complete(record)
        self.assertFalse(complete)
        self.assertTrue(reason)

    def test_complete_206_manifest_requires_full_range_and_length(self):
        body = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg.ts\n"
        size = len(body.encode())
        record = {"status_code": 206, "body": body,
                  "headers": {"content-range": f"bytes 0-{size-1}/{size}", "content-length": str(size)}}
        complete, _ = globalping._hls_content_complete(record)
        self.assertTrue(complete)

    def test_master_playlist_child_is_resolved(self):
        body = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow/index.m3u8\n"
        record = {"status_code": 200, "body": body, "headers": {"content-length": str(len(body))}}
        self.assertEqual(globalping._master_child_url([record], "https://host.example/master.m3u8"),
                         "https://host.example/low/index.m3u8")


class GlobalpingDryRunTests(unittest.TestCase):
    def test_dry_run_never_calls_api_or_publishes(self):
        candidate = {"channel": "CCTV-1", "normalized_url": "https://example.com/live.m3u8"}
        with patch.object(globalping, "_api_request", side_effect=AssertionError("API called")), \
             patch.object(globalping, "_publish", side_effect=AssertionError("publish called")):
            result = globalping.run_globalping(
                [candidate], {"globalping": {"max_probes": 3}}, ["CCTV-1"], dry_run=True
            )
        self.assertEqual(result["publication"]["status"], "not_run")
        self.assertEqual(result["input_queue_identities"], [("CCTV-1", "https://example.com/live.m3u8")])


class PublicationValidationTests(unittest.TestCase):
    def test_empty_candidate_set_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            result = globalping._publish([], Path(directory), [], {}, [])
            self.assertEqual(result["status"], "blocked")
            self.assertFalse((Path(directory) / "iptv.m3u").exists())

    def test_channel_cap_is_enforced_in_output_validation(self):
        accepted = [
            {"channel": "CCTV-1", "normalized_url": "https://a.example/1"},
            {"channel": "CCTV-1", "normalized_url": "https://a.example/2"},
            {"channel": "CCTV-1", "normalized_url": "https://a.example/3"},
        ]
        m3u = "#EXTM3U\n" + "".join(f"#EXTINF:-1,{x['channel']}\n{x['normalized_url']}\n" for x in accepted)
        txt = "".join(f"{x['channel']},{x['normalized_url']}\n" for x in accepted)
        valid, reason = globalping._validate_new(m3u, txt, accepted, ["CCTV-1"])
        self.assertFalse(valid)
        self.assertIn("最多两条", reason)


class DirectTSAndQuotaTests(unittest.TestCase):
    def test_direct_ts_uses_http_only_and_consumes_actual_probe_quota(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "DIRECT_TS",
            "normalized_url": "https://example.com/live.ts",
        }
        budget = {"remaining": 4, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            return_value=("measurement-1", 3),
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [], [{"probe": {"city": "A"}}]),
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.args[0], "http")
        self.assertEqual(create.call_args.args[3], 3)
        self.assertEqual(budget["remaining"], 1)
        self.assertEqual(result["checks"]["ipv4"], globalping.NA)
        self.assertEqual(result["checks"]["http"], globalping.PASS)
        self.assertEqual(result["checks"]["hls"], globalping.NA)
        self.assertEqual(result["overall"], globalping.PASS)
        self.assertEqual(stats["actual_measurements"], 1)
        self.assertEqual(stats["actual_probe_slots"], 3)

    def test_probe_request_is_limited_by_remaining_test_quota(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "DIRECT_TS",
            "normalized_url": "https://example.com/live.ts",
        }
        budget = {"remaining": 2, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            return_value=("measurement-1", 2),
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [], [{"probe": {"city": "A"}}]),
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_args.args[3], 2)
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual(result["overall"], globalping.PASS)
        self.assertEqual(stats["actual_measurements"], 1)
        self.assertEqual(stats["actual_probe_slots"], 2)

    def test_hls_second_stage_is_blocked_without_full_probe_quota(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "HLS",
            "normalized_url": "https://example.com/master.m3u8",
        }
        budget = {"remaining": 8, "stop_new": 0}
        stats = globalping.Counter()
        probe = {"probe": {"city": "A", "country": "CN", "asn": 9808}}

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=[
                ("ipv4-measurement", 3),
                ("http-measurement", 3),
            ],
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_ping_aggregate",
            return_value=(globalping.PASS, [probe]),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [probe], [probe]),
        ), patch.object(
            globalping,
            "_master_child_url",
            return_value="https://example.com/child.m3u8",
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 2)
        self.assertEqual(budget["remaining"], 2)
        self.assertEqual(result["checks"]["hls"], globalping.INCOMPLETE)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertEqual(stats["actual_measurements"], 2)
        self.assertEqual(stats["actual_probe_slots"], 6)

    def test_hls_second_stage_uncertain_creation_blocks_admission(self):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "HLS",
            "normalized_url": "https://example.com/master.m3u8",
        }
        budget = {"remaining": 9, "stop_new": 0}
        stats = globalping.Counter()
        probe = {"probe": {"city": "A", "country": "CN", "asn": 9808}}

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=[
                ("ipv4-measurement", 3),
                ("http-measurement", 3),
                globalping.GlobalpingError(
                    "connection lost", uncertain=True
                ),
            ],
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_ping_aggregate",
            return_value=(globalping.PASS, [probe]),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.PASS, [probe], [probe]),
        ), patch.object(
            globalping,
            "_master_child_url",
            return_value="https://example.com/child.m3u8",
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 3)
        self.assertTrue(result["creation_uncertain"])
        self.assertEqual(result["checks"]["hls"], globalping.INCOMPLETE)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertEqual(stats["creation_uncertain_candidates"], 1)
        self.assertEqual(stats["actual_measurements"], 2)
        self.assertEqual(stats["actual_probe_slots"], 6)
        self.assertEqual(budget["remaining"], 0)

    def _run_hls_child_probe_case(self, child_probe):
        candidate = {
            "channel": "CCTV-1",
            "media_type": "HLS",
            "normalized_url": "https://example.com/master.m3u8",
        }
        parent_probe = {
            "probe": {
                "city": "A",
                "country": "CN",
                "asn": 9808,
                "network": "China Mobile",
            }
        }
        body = "#EXTM3U\n#EXTINF:6,\nsegment.ts\n"

        def make_record(probe):
            return {
                "probe": probe["probe"],
                "status": "finished",
                "status_code": 200,
                "headers": {"content-length": str(len(body))},
                "body": body,
                "success_evidence": True,
            }

        parent_record = make_record(parent_probe)
        child_record = make_record(child_probe)
        budget = {"remaining": 9, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=[
                ("ipv4-measurement", 3),
                ("http-measurement", 3),
                ("child-measurement", 3),
            ],
        ) as create, patch.object(
            globalping,
            "_poll",
            return_value=({"status": "finished"}, None),
        ), patch.object(
            globalping,
            "_ping_aggregate",
            return_value=(globalping.PASS, [parent_probe]),
        ), patch.object(
            globalping,
            "_http_aggregate",
            side_effect=[
                (globalping.PASS, [parent_record], [parent_record]),
                (globalping.PASS, [child_record], [child_record]),
            ],
        ), patch.object(
            globalping,
            "_master_child_url",
            return_value="https://example.com/child.m3u8",
        ):
            result = globalping._candidate_remote(
                candidate, [], 3, 8, budget, stats
            )

        return result, budget, stats, create

    def test_hls_second_stage_reuses_same_probe_set(self):
        child_probe = {
            "probe": {
                "city": "A",
                "country": "CN",
                "asn": 9808,
                "network": "China Mobile",
            }
        }

        result, budget, stats, create = self._run_hls_child_probe_case(
            child_probe
        )

        self.assertEqual(create.call_count, 3)
        self.assertEqual(
            create.call_args_list[2].kwargs["previous_measurement"],
            "http-measurement",
        )
        self.assertEqual(create.call_args_list[2].args[1],
                         "https://example.com/child.m3u8")
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual(result["checks"]["hls"], globalping.PASS)
        self.assertEqual(result["overall"], globalping.PASS)
        self.assertEqual(stats["actual_measurements"], 3)
        self.assertEqual(stats["actual_probe_slots"], 9)

    def test_hls_second_stage_probe_mismatch_stays_incomplete(self):
        child_probe = {
            "probe": {
                "city": "B",
                "country": "CN",
                "asn": 9808,
                "network": "China Mobile",
            }
        }

        result, budget, stats, create = self._run_hls_child_probe_case(
            child_probe
        )

        self.assertEqual(create.call_count, 3)
        self.assertEqual(
            create.call_args_list[2].kwargs["previous_measurement"],
            "http-measurement",
        )
        self.assertEqual(budget["remaining"], 0)
        self.assertEqual(result["checks"]["hls"], globalping.INCOMPLETE)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertTrue(any(
            "相同探针" in reason
            for reason in result.get("reasons", [])
        ))
        self.assertEqual(stats["actual_measurements"], 3)
        self.assertEqual(stats["actual_probe_slots"], 9)

    def test_hls_child_measurement_uses_previous_measurement_id(self):
        with patch.object(
            globalping,
            "_api_request",
            return_value=(202, {"id": "child-measurement", "probesCount": 3}, {}),
        ) as request:
            measurement_id, probes_count = globalping._create_measurement(
                "http",
                "https://example.com/child.m3u8",
                [],
                3,
                previous_measurement="http-measurement",
            )

        payload = request.call_args.args[2]
        self.assertEqual(measurement_id, "child-measurement")
        self.assertEqual(probes_count, 3)
        self.assertEqual(payload["locations"], "http-measurement")
        self.assertNotIn("limit", payload)

class BudgetBoundaryAndExceptionTests(unittest.TestCase):
    def _candidate(self):
        return {
            "channel": "CCTV-1",
            "media_type": "DIRECT_TS",
            "normalized_url": "https://example.com/live.ts",
        }

    def test_no_probes_refunds_reserved_quota(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError(
                "no probes", no_probes=True
            ),
        ) as create:
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(budget["remaining"], 3)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)

    def test_http_429_refunds_quota_and_stops_new_measurements(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError(
                "rate limited", status=429
            ),
        ) as create:
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(budget["remaining"], 3)
        self.assertEqual(budget["stop_new"], 1)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)

    def test_uncertain_creation_keeps_reserved_quota_separate(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError(
                "connection lost", uncertain=True
            ),
        ) as create:
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(budget["remaining"], 0)
        self.assertTrue(result["creation_uncertain"])
        self.assertEqual(stats["creation_uncertain_candidates"], 1)
        self.assertEqual(stats["quota_blocked_candidates"], 0)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)

    def test_known_creation_failure_refunds_quota(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            side_effect=globalping.GlobalpingError("creation failed"),
        ):
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(budget["remaining"], 3)
        self.assertEqual(stats["actual_measurements"], 0)
        self.assertEqual(stats["actual_probe_slots"], 0)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)

    def test_poll_boundary_keeps_created_measurement_in_statistics(self):
        budget = {"remaining": 3, "stop_new": 0}
        stats = globalping.Counter()

        with patch.object(
            globalping,
            "_create_measurement",
            return_value=("measurement-1", 2),
        ), patch.object(
            globalping,
            "_poll",
            return_value=({"status": "in-progress"}, "轮询边界耗尽"),
        ), patch.object(
            globalping,
            "_http_aggregate",
            return_value=(globalping.INCOMPLETE, [], []),
        ):
            result = globalping._candidate_remote(
                self._candidate(), [], 3, 8, budget, stats
            )

        self.assertEqual(stats["actual_measurements"], 1)
        self.assertEqual(stats["actual_probe_slots"], 2)
        self.assertEqual(stats["incomplete_measurements"], 1)
        self.assertEqual(stats["completed_measurements"], 0)
        self.assertEqual(len(result["measurements"]), 1)
        self.assertEqual(result["overall"], globalping.INCOMPLETE)
        self.assertTrue(any(
            "轮询边界耗尽" in reason
            for reason in result.get("reasons", [])
        ))

    def test_retry_after_does_not_extend_poll_boundary(self):
        error = globalping.urllib.error.HTTPError(
            "https://api.globalping.io/v1/measurements/test",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            None,
        )

        with patch.object(
            globalping.urllib.request,
            "urlopen",
            side_effect=error,
        ) as request, patch.object(
            globalping.time,
            "monotonic",
            return_value=0.1,
        ), patch.object(
            globalping.time,
            "sleep",
        ) as sleep:
            measurement, reason = globalping._poll("measurement-test", 1)

        self.assertIsNone(measurement)
        self.assertEqual(reason, "轮询边界耗尽")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

class PublicationUnknownRecoveryTests(unittest.TestCase):
    def _prepare_unknown_state(self, output_dir):
        state_dir = output_dir / ".iptv-publish-state"
        state_dir.mkdir(parents=True)
        marker = state_dir / "publication-unknown.json"
        marker.write_text('{"status": "unknown"}', encoding="utf-8")
        return state_dir, marker

    def _prepare_known_good(self, state_dir):
        known = state_dir / "known-good"
        known.mkdir(parents=True)

        m3u = (
            "#EXTM3U\n"
            "#EXTINF:-1,CCTV-1\n"
            "https://good.example/live.m3u8\n"
        )
        txt = "CCTV-1,https://good.example/live.m3u8\n"

        (known / "iptv.m3u").write_text(m3u, encoding="utf-8")
        (known / "iptv.txt").write_text(txt, encoding="utf-8")
        return m3u, txt

    def test_no_unknown_marker_needs_no_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            recovered, reason = globalping._recover_unknown(Path(directory))

        self.assertTrue(recovered)
        self.assertIn("无状态未知标记", reason)

    def test_missing_known_good_backup_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, marker = self._prepare_unknown_state(Path(directory))

            recovered, reason = globalping._recover_unknown(Path(directory))

            self.assertFalse(recovered)
            self.assertTrue(marker.exists())
            self.assertIn("人工确认", reason)

    def test_invalid_known_good_backup_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            state_dir, marker = self._prepare_unknown_state(output_dir)
            known = state_dir / "known-good"
            known.mkdir()
            (known / "iptv.m3u").write_text("invalid", encoding="utf-8")
            (known / "iptv.txt").write_text(
                "CCTV-1,https://good.example/live.m3u8\n",
                encoding="utf-8",
            )

            recovered, reason = globalping._recover_unknown(output_dir)

            self.assertFalse(recovered)
            self.assertTrue(marker.exists())
            self.assertIn("无法解析", reason)
            self.assertFalse((output_dir / "iptv.m3u").exists())

    def test_known_good_backup_is_restored_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            state_dir, marker = self._prepare_unknown_state(output_dir)
            expected_m3u, expected_txt = self._prepare_known_good(state_dir)

            recovered, reason = globalping._recover_unknown(output_dir)

            self.assertTrue(recovered)
            self.assertFalse(marker.exists())
            self.assertEqual(
                (output_dir / "iptv.m3u").read_text(encoding="utf-8"),
                expected_m3u,
            )
            self.assertEqual(
                (output_dir / "iptv.txt").read_text(encoding="utf-8"),
                expected_txt,
            )
            self.assertIn("恢复并核验", reason)

    def test_restore_failure_keeps_unknown_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            state_dir, marker = self._prepare_unknown_state(output_dir)
            self._prepare_known_good(state_dir)

            with patch.object(
                globalping.shutil,
                "copy2",
                side_effect=OSError("模拟恢复失败"),
            ):
                recovered, reason = globalping._recover_unknown(output_dir)

            self.assertFalse(recovered)
            self.assertTrue(marker.exists())
            self.assertIn("自动恢复失败", reason)

    def test_publish_is_blocked_when_unknown_state_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            state_dir, marker = self._prepare_unknown_state(output_dir)

            old_m3u = "#EXTM3U\n#EXTINF:-1,OLD\nhttps://old.example/live.m3u8\n"
            old_txt = "OLD,https://old.example/live.m3u8\n"
            (output_dir / "iptv.m3u").write_text(old_m3u, encoding="utf-8")
            (output_dir / "iptv.txt").write_text(old_txt, encoding="utf-8")

            accepted = [{
                "channel": "CCTV-1",
                "normalized_url": "https://new.example/live.m3u8",
            }]

            result = globalping._publish(
                accepted, output_dir, ["CCTV-1"], {}, []
            )

            self.assertEqual(result["status"], "blocked")
            self.assertTrue(marker.exists())
            self.assertEqual(
                (output_dir / "iptv.m3u").read_text(encoding="utf-8"),
                old_m3u,
            )
            self.assertEqual(
                (output_dir / "iptv.txt").read_text(encoding="utf-8"),
                old_txt,
            )

    def test_publish_can_continue_after_successful_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            state_dir, marker = self._prepare_unknown_state(output_dir)
            expected_m3u, expected_txt = self._prepare_known_good(state_dir)

            accepted = [{
                "channel": "CCTV-1",
                "normalized_url": "https://good.example/live.m3u8",
            }]

            result = globalping._publish(accepted, output_dir, [], {}, [])

            self.assertIn(result["status"], {"unchanged", "published"})
            self.assertFalse(marker.exists())
            self.assertEqual(
                (output_dir / "iptv.m3u").read_text(encoding="utf-8"),
                expected_m3u,
            )
            self.assertEqual(
                (output_dir / "iptv.txt").read_text(encoding="utf-8"),
                expected_txt,
            )

class PublicationProtectionAndRollbackTests(unittest.TestCase):
    def _items(self, channel_count, routes_per_channel=1, prefix="old"):
        items = []
        for channel_index in range(channel_count):
            channel = f"CH-{channel_index + 1}"
            for route_index in range(routes_per_channel):
                items.append({
                    "channel": channel,
                    "normalized_url": (
                        f"https://{prefix}.example/"
                        f"{channel}-{route_index + 1}.m3u8"
                    ),
                })
        return items

    def _write_old(self, output_dir, items):
        m3u_lines = ["#EXTM3U"]
        txt_lines = []

        for item in items:
            channel = item["channel"]
            url = item["normalized_url"]
            m3u_lines.extend([
                f"#EXTINF:-1,{channel}",
                url,
            ])
            txt_lines.append(f"{channel},{url}")

        m3u_path = output_dir / "iptv.m3u"
        txt_path = output_dir / "iptv.txt"

        m3u_path.write_text(
            "\n".join(m3u_lines) + "\n",
            encoding="utf-8",
        )
        txt_path.write_text(
            "\n".join(txt_lines) + "\n",
            encoding="utf-8",
        )

        return (
            m3u_path.read_text(encoding="utf-8"),
            txt_path.read_text(encoding="utf-8"),
        )

    def _publish(self, output_dir, accepted):
        channels = list(dict.fromkeys(
            item["channel"] for item in accepted
        ))
        category_by_channel = {
            channel: "全部" for channel in channels
        }
        return globalping._publish(
            accepted,
            output_dir,
            channels,
            category_by_channel,
            ["全部"],
        )

    def test_both_old_files_missing_are_first_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)

            valid, channels, routes, reason = globalping._parse_existing(
                output_dir / "iptv.m3u",
                output_dir / "iptv.txt",
            )

            self.assertTrue(valid)
            self.assertEqual(channels, 0)
            self.assertEqual(routes, 0)
            self.assertIn("首次发布", reason)

    def test_both_old_files_empty_are_first_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            m3u_path = output_dir / "iptv.m3u"
            txt_path = output_dir / "iptv.txt"
            m3u_path.touch()
            txt_path.touch()

            valid, channels, routes, reason = globalping._parse_existing(
                m3u_path,
                txt_path,
            )

            self.assertTrue(valid)
            self.assertEqual(channels, 0)
            self.assertEqual(routes, 0)
            self.assertIn("首次发布", reason)

    def test_one_missing_or_empty_old_file_blocks_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_items = self._items(1)
            self._write_old(output_dir, old_items)

            (output_dir / "iptv.txt").unlink()

            valid, _, _, reason = globalping._parse_existing(
                output_dir / "iptv.m3u",
                output_dir / "iptv.txt",
            )

            self.assertFalse(valid)
            self.assertIn("缺失/为空", reason)

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_items = self._items(1)
            self._write_old(output_dir, old_items)

            (output_dir / "iptv.txt").write_text(
                "",
                encoding="utf-8",
            )

            valid, _, _, reason = globalping._parse_existing(
                output_dir / "iptv.m3u",
                output_dir / "iptv.txt",
            )

            self.assertFalse(valid)
            self.assertIn("缺失/为空", reason)

    def test_invalid_or_inconsistent_old_subscriptions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(output_dir, self._items(1))

            (output_dir / "iptv.m3u").write_text(
                "INVALID M3U\n",
                encoding="utf-8",
            )

            valid, _, _, reason = globalping._parse_existing(
                output_dir / "iptv.m3u",
                output_dir / "iptv.txt",
            )

            self.assertFalse(valid)
            self.assertIn("M3U", reason)

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(output_dir, self._items(1))

            (output_dir / "iptv.txt").write_text(
                "OTHER,https://other.example/live.m3u8\n",
                encoding="utf-8",
            )

            valid, _, _, reason = globalping._parse_existing(
                output_dir / "iptv.m3u",
                output_dir / "iptv.txt",
            )

            self.assertFalse(valid)
            self.assertIn("不一致", reason)

    def test_channel_drop_over_50_percent_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(output_dir, self._items(3))

            accepted = self._items(
                1,
                routes_per_channel=2,
                prefix="new",
            )

            result = self._publish(output_dir, accepted)

            self.assertEqual(result["status"], "blocked")
            self.assertIn("频道数量下降超过 50%", result["reason"])

    def test_channel_drop_exactly_50_percent_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(output_dir, self._items(2))

            accepted = self._items(1, prefix="new")

            result = self._publish(output_dir, accepted)

            self.assertEqual(result["status"], "published")

    def test_route_drop_over_50_percent_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(
                output_dir,
                self._items(2, routes_per_channel=2),
            )

            accepted = self._items(1, prefix="new")

            result = self._publish(output_dir, accepted)

            self.assertEqual(result["status"], "blocked")
            self.assertIn("线路数量下降超过 50%", result["reason"])

    def test_route_drop_exactly_50_percent_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(
                output_dir,
                self._items(2, routes_per_channel=2),
            )

            accepted = self._items(
                2,
                routes_per_channel=1,
                prefix="new",
            )

            result = self._publish(output_dir, accepted)

            self.assertEqual(result["status"], "published")

    def test_consecutive_downgrade_uses_previous_published_version(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(output_dir, self._items(8))

            first = self._publish(
                output_dir,
                self._items(6, prefix="first"),
            )
            self.assertEqual(first["status"], "published")

            second = self._publish(
                output_dir,
                self._items(3, prefix="second"),
            )
            self.assertEqual(second["status"], "published")

            third = self._publish(
                output_dir,
                self._items(1, prefix="third"),
            )
            self.assertEqual(third["status"], "blocked")
            self.assertIn("50%", third["reason"])

    def test_corrupt_backup_is_rejected_before_replacing_old_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_m3u, old_txt = self._write_old(
                output_dir,
                self._items(1),
            )
            real_copy2 = globalping.shutil.copy2

            def corrupt_backup(source, destination):
                result = real_copy2(source, destination)
                destination = Path(destination)

                if (
                    destination.parent.name.startswith("backup-")
                    and destination.name == "iptv.m3u"
                ):
                    destination.write_text(
                        "BROKEN BACKUP\n",
                        encoding="utf-8",
                    )

                return result

            with patch.object(
                globalping.shutil,
                "copy2",
                side_effect=corrupt_backup,
            ):
                result = self._publish(
                    output_dir,
                    self._items(1, prefix="new"),
                )

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(
                (output_dir / "iptv.m3u").read_text(encoding="utf-8"),
                old_m3u,
            )
            self.assertEqual(
                (output_dir / "iptv.txt").read_text(encoding="utf-8"),
                old_txt,
            )

    def test_failed_second_replace_restores_and_verifies_old_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_m3u, old_txt = self._write_old(
                output_dir,
                self._items(1),
            )
            real_replace = globalping.os.replace
            calls = []

            def fail_second_replace(source, destination):
                calls.append((source, destination))
                if len(calls) == 1:
                    return real_replace(source, destination)
                raise OSError("模拟第二份正式文件替换失败")

            with patch.object(
                globalping.os,
                "replace",
                side_effect=fail_second_replace,
            ):
                result = self._publish(
                    output_dir,
                    self._items(1, prefix="new"),
                )

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                (output_dir / "iptv.m3u").read_text(encoding="utf-8"),
                old_m3u,
            )
            self.assertEqual(
                (output_dir / "iptv.txt").read_text(encoding="utf-8"),
                old_txt,
            )

    def test_failed_rollback_keeps_unknown_marker_and_scene_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._write_old(output_dir, self._items(1))
            real_replace = globalping.os.replace
            calls = []

            def corrupt_backup_and_fail(source, destination):
                calls.append((source, destination))

                if len(calls) == 1:
                    return real_replace(source, destination)

                backup_files = list(
                    (
                        output_dir / ".iptv-publish-state"
                    ).glob("backup-*/iptv.m3u")
                )
                self.assertTrue(backup_files)

                backup_files[0].write_text(
                    "BROKEN BACKUP\n",
                    encoding="utf-8",
                )
                raise OSError("模拟第二份正式文件替换失败")

            with patch.object(
                globalping.os,
                "replace",
                side_effect=corrupt_backup_and_fail,
            ):
                result = self._publish(
                    output_dir,
                    self._items(1, prefix="new"),
                )

            state_dir = output_dir / ".iptv-publish-state"
            marker = state_dir / "publication-unknown.json"

            self.assertEqual(result["status"], "unknown")
            self.assertTrue(marker.exists())
            self.assertTrue(list(state_dir.glob("backup-*")))
            self.assertTrue((output_dir / ".iptv.txt.tmp").exists())

            marker_data = globalping.json.loads(
                marker.read_text(encoding="utf-8")
            )
            self.assertEqual(marker_data["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

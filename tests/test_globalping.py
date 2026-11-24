"""IPTV-Direct globalping.py 专项测试。

仅使用模拟 API 响应和本地数据，不访问 Globalping 公网 API，
不创建真实测量，不消耗额度，不修改正式订阅。

运行方式：
    python -m unittest discover -s tests -p "test_globalping.py" -v
"""

import json
import urllib.error
import unittest
from unittest.mock import Mock, patch

import globalping as gp


PROBE = {
    "country": "CN",
    "state": "Fujian",
    "city": "Xiamen",
    "asn": 9808,
    "network": "China Mobile",
    "latitude": 24.5,
    "longitude": 118.1,
    "tags": ["eyeball-network"],
    "resolvers": ["1.1.1.1"],
}

MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:10\n"
    "#EXT-X-MEDIA-SEQUENCE:1\n"
    "#EXTINF:10,\n"
    "segment.ts\n"
    "#EXT-X-ENDLIST\n"
).encode()

MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=1200000\n"
    "variant/index.m3u8\n"
).encode()

def result_item(result, probe=None):
    return {"probe": dict(probe or PROBE), "result": dict(result)}


def finished_http(body, status_code=200, headers=None, **extra):
    value = {
        "status": "finished",
        "resolvedAddress": "1.2.3.4",
        "statusCode": status_code,
        "headers": headers or {},
        "rawBody": body.decode("utf-8") if isinstance(body, bytes) else body,
        "truncated": False,
    }
    value.update(extra)
    return value


def measurement(*items):
    return {"status": "finished", "results": list(items)}


class ApiRequestTests(unittest.TestCase):
    def test_json_array_response_is_supported(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'[{"id":"p1"}]'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(gp.urllib.request, "urlopen", return_value=response):
            status, data, headers = gp._api_request("https://example.test/probes")
        self.assertEqual(status, 200)
        self.assertEqual(data, [{"id": "p1"}])

    def test_304_is_normal_response(self):
        error = urllib.error.HTTPError(
            "https://example.test/measurements/id", 304, "Not Modified",
            {"ETag": '"abc"'}, None,
        )
        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            status, data, headers = gp._api_request("https://example.test/measurements/id")
        self.assertEqual(status, 304)
        self.assertIsNone(data)
        self.assertEqual(headers.get("etag"), '"abc"')

    def test_429_is_marked_rate_limited(self):
        error = urllib.error.HTTPError(
            "https://example.test/measurements", 429, "Too Many Requests",
            {"Retry-After": "10"}, None,
        )
        with patch.object(gp.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(gp.GlobalpingError) as caught:
                gp._api_request("https://example.test/measurements", method="POST", payload={})
        self.assertTrue(caught.exception.rate_limited)
        self.assertIn("429", str(caught.exception))

    def test_read_limits_parses_live_quota(self):
        payload = {"rateLimit": {"measurements": {"create": {
            "limit": 100, "remaining": 7, "reset": 12345,
        }}}}
        with patch.object(gp, "_api_request", return_value=(200, payload, {})):
            self.assertEqual(gp.read_limits(), {"limit": 100, "remaining": 7, "reset": 12345})

    def test_read_limits_rejects_unknown_shape(self):
        with patch.object(gp, "_api_request", return_value=(200, [], {})):
            with self.assertRaises(gp.GlobalpingError):
                gp.read_limits()


class ProbeSelectionTests(unittest.TestCase):
    def test_selects_mainland_eyeball_probes_mobile_first_and_max_three(self):
        probes = [
            {"tags": ["eyeball-network"], "location": {"country": "CN", "asn": 9808, "network": "China Mobile"}},
            {"tags": ["eyeball-network"], "location": {"country": "CN", "asn": 4134, "network": "China Telecom"}},
            {"tags": ["eyeball-network"], "location": {"country": "CN", "asn": 4837, "network": "China Unicom"}},
            {"tags": ["eyeball-network"], "location": {"country": "US", "asn": 9808, "network": "China Mobile"}},
            {"tags": ["hosting"], "location": {"country": "CN", "asn": 9808, "network": "China Mobile"}},
        ]
        with patch.object(gp, "_api_request", return_value=(200, probes, {})):
            locations = gp.choose_probes(3)
        self.assertEqual(sum(item["limit"] for item in locations), 3)
        self.assertLessEqual(sum(item["limit"] for item in locations), 3)
        self.assertEqual(locations[0]["asn"], 9808)
        self.assertTrue(all(item["country"] == "CN" for item in locations))
        self.assertTrue(all(item["tags"] == ["eyeball-network"] for item in locations))

    def test_excludes_asns_already_used_by_same_candidate(self):
        probes = [
            {
                "tags": ["eyeball-network"],
                "location": {
                    "country": "CN",
                    "asn": 9808,
                    "network": "China Mobile",
                },
            },
            {
                "tags": ["eyeball-network"],
                "location": {
                    "country": "CN",
                    "asn": 4134,
                    "network": "China Telecom",
                },
            },
        ]

        with patch.object(
            gp, "_api_request", return_value=(200, probes, {})
        ):
            locations = gp.choose_probes(
                1,
                excluded_asns={9808},
            )

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0]["asn"], 4134)
    
    def test_rejects_invalid_probe_limit(self):
        for value in (0, -1, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    gp.choose_probes(value)

    def test_rejects_non_array_probe_response(self):
        with patch.object(gp, "_api_request", return_value=(200, {"data": []}, {})):
            with self.assertRaises(gp.GlobalpingError):
                gp.choose_probes(3)

    def test_no_eligible_probe_is_incomplete(self):
        with patch.object(gp, "_api_request", return_value=(200, [
            {"tags": ["eyeball-network"], "location": {"country": "US", "asn": 9808}}
        ], {})):
            with self.assertRaises(gp.GlobalpingError):
                gp.choose_probes(3)

class ProbeResultValidationTests(unittest.TestCase):
    def setUp(self):
        self.locations = [
            {
                "country": "CN",
                "asn": 9808,
                "tags": ["eyeball-network"],
                "limit": 1,
            }
        ]
        self.result = finished_http("", 200)

    def test_ipv4_requires_finished_measurement(self):
        self.assertTrue(
            gp._ipv4_ok(finished_http("", 200))
        )
        self.assertFalse(
            gp._ipv4_ok({
                "status": "failed",
                "resolvedAddress": "1.2.3.4",
            })
        )
        self.assertFalse(
            gp._ipv4_ok({
                "status": "offline",
                "resolvedAddress": "1.2.3.4",
            })
        )

    def test_ipv4_aggregation_rejects_unfinished_measurement(self):
        result = gp._aggregate_ipv4({
            ("probe",): result_item({
                "status": "failed",
                "resolvedAddress": "1.2.3.4",
            })
        })
        self.assertEqual(result["status"], gp.INCOMPLETE)
    
    def test_accepts_exact_requested_probe(self):
        result = gp._result_map(
            measurement(result_item(self.result)),
            self.locations,
        )
        self.assertEqual(len(result), 1)

    def test_rejects_missing_or_extra_results(self):
        invalid_results = [
            measurement(),
            measurement(
                result_item(self.result),
                result_item(self.result, {
                    **PROBE,
                    "asn": 4134,
                    "network": "China Telecom",
                }),
            ),
        ]

        for value in invalid_results:
            with self.subTest(value=value):
                with self.assertRaises(gp.GlobalpingError):
                    gp._result_map(value, self.locations)

    def test_rejects_wrong_country(self):
        probe = {**PROBE, "country": "US"}
        with self.assertRaises(gp.GlobalpingError):
            gp._result_map(
                measurement(result_item(self.result, probe)),
                self.locations,
            )

    def test_rejects_wrong_asn(self):
        probe = {**PROBE, "asn": 4134}
        with self.assertRaises(gp.GlobalpingError):
            gp._result_map(
                measurement(result_item(self.result, probe)),
                self.locations,
            )

    def test_rejects_missing_eyeball_network_tag(self):
        probe = {**PROBE, "tags": ["hosting"]}
        with self.assertRaises(gp.GlobalpingError):
            gp._result_map(
                measurement(result_item(self.result, probe)),
                self.locations,
            )

    def test_rejects_different_probe_for_hls_child(self):
        wrong_probe = {
            **PROBE,
            "asn": 4134,
            "network": "China Telecom",
        }
        expected_key = gp._probe_key(PROBE)

        with self.assertRaises(gp.GlobalpingError):
            gp._result_map(
                measurement(result_item(self.result, wrong_probe)),
                expected_probe_key=expected_key,
            )

class UrlAndBodyTests(unittest.TestCase):
    def test_split_url_preserves_path_query_and_port(self):
        parts = gp._split_url("https://example.test:8443/live/index.m3u8?token=x")
        self.assertEqual(parts["target"], "example.test")
        self.assertEqual(parts["protocol"], "HTTPS")
        self.assertEqual(parts["port"], 8443)
        self.assertEqual(parts["path"], "/live/index.m3u8")
        self.assertEqual(parts["query"], "token=x")

    def test_ipv4_literal_measurement_omits_ip_version(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = [{"country": "CN", "asn": 9808, "limit": 1}]

        with patch.object(gp, "_remaining_budget", return_value=10), \
             patch.object(gp, "_api_request", return_value=(202, {"id": "m1", "probesCount": 1}, {})) as request:
            measurement_id = gp._create_measurement(
                "http://1.2.3.4/live.ts",
                locations,
                budget,
            )

        self.assertEqual(measurement_id, "m1")
        payload = request.call_args.kwargs["payload"]
        self.assertNotIn("ipVersion", payload["measurementOptions"])

    def test_domain_measurement_keeps_ipv4_version(self):
        budget = {"remaining": 10, "probe_count": 1}
        locations = [{"country": "CN", "asn": 9808, "limit": 1}]

        with patch.object(gp, "_remaining_budget", return_value=10), \
             patch.object(gp, "_api_request", return_value=(202, {"id": "m1", "probesCount": 1}, {})) as request:
            measurement_id = gp._create_measurement(
                "http://example.test/live.ts",
                locations,
                budget,
            )

        self.assertEqual(measurement_id, "m1")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["measurementOptions"]["ipVersion"], 4)

    def test_split_url_rejects_ipv6_literal(self):
        with self.assertRaises(gp.GlobalpingError):
            gp._split_url("http://[2001:db8::1]/live.ts")

    def test_split_url_rejects_credentials_and_unsupported_scheme(self):
        for url in ("http://user:pass@example.test/a", "ftp://example.test/a"):
            with self.subTest(url=url):
                with self.assertRaises(gp.GlobalpingError):
                    gp._split_url(url)

    def test_raw_body_rejects_missing_truncated_and_lossy_text(self):
        self.assertIsNone(gp._raw_body_bytes({}))
        self.assertIsNone(gp._raw_body_bytes({"rawBody": "abc", "truncated": True}))
        self.assertIsNone(gp._raw_body_bytes({"rawBody": "a\ufffdb", "truncated": False}))

class PlaylistTests(unittest.TestCase):
    def test_parses_media_playlist_and_resolves_segment(self):
        parsed = gp._parse_playlist(MEDIA_PLAYLIST, "https://example.test/live/index.m3u8")
        self.assertEqual(parsed["kind"], "media")
        self.assertEqual(parsed["segments"], ["https://example.test/live/segment.ts"])

    def test_parses_master_playlist_and_resolves_variant(self):
        parsed = gp._parse_playlist(MASTER_PLAYLIST, "https://example.test/live/master.m3u8")
        self.assertEqual(parsed["kind"], "master")
        self.assertEqual(parsed["child_url"], "https://example.test/live/variant/index.m3u8")

    def test_rejects_channel_subscription_m3u(self):
        body = b"#EXTM3U\n#EXTINF:-1,CCTV-5\nhttp://example.test/live.ts\n"
        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/list.m3u")
        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_encrypted_hls(self):
        body = MEDIA_PLAYLIST.replace(b"#EXTINF:10,", b'#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:10,')
        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/index.m3u8")
        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_fmp4_hls(self):
        body = MEDIA_PLAYLIST.replace(b"#EXTINF:10,", b'#EXT-X-MAP:URI="init.mp4"\n#EXTINF:10,')
        with self.assertRaises(gp.GlobalpingError) as caught:
            gp._parse_playlist(body, "https://example.test/index.m3u8")
        self.assertEqual(caught.exception.status, gp.FAIL)

    def test_rejects_malformed_extinf_pairing(self):
        body = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n#EXTINF:10,\nseg.ts\n"
        with self.assertRaises(gp.GlobalpingError):
            gp._parse_playlist(body, "https://example.test/index.m3u8")

class StatusAndFlowTests(unittest.TestCase):
    def test_na_does_not_count_as_pass(self):
        checks = {
            "ipv4": gp._status(gp.PASS, "ok"),
            "http": gp._status(gp.PASS, "ok"),
            "hls": gp._status(gp.NA, "not applicable"),
            "ts": gp._status(gp.INCOMPLETE, "not checked"),
        }
        self.assertEqual(gp._overall_status(checks), gp.INCOMPLETE)

    def test_unknown_media_type_fails_without_api_calls(self):
        with patch.object(gp, "read_limits") as limits, patch.object(gp, "choose_probes") as probes:
            output = gp.check_candidates([{"name": "x", "media_type": "UNKNOWN", "normalized_url": "http://example.test/a"}], {"max_probes": 3})
        limits.assert_not_called()
        probes.assert_not_called()
        self.assertEqual(output[0]["globalping"]["status"], gp.FAIL)

    def test_direct_ts_marks_hls_and_ts_na(self):
        """DIRECT_TS 必须经过三个不同 ASN 的完整检测。"""

        http_result = finished_http("", 200)
        asns = iter([9808, 4134, 4837])

        def choose_one(*args, **kwargs):
            asn = next(asns)
            return [{
                "country": "CN",
                "asn": asn,
                "tags": ["eyeball-network"],
                "limit": 1,
            }]

        def run_stage(url, locations, budget, headers=None):
            probe = {**PROBE, "asn": locations[0]["asn"]}
            return (
                f"m-{probe['asn']}",
                measurement(result_item(http_result, probe=probe)),
            )

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 10, "reset": 1},
        ), patch.object(
            gp,
            "choose_probes",
            side_effect=choose_one,
        ) as probes, patch.object(
            gp,
            "_stage",
            side_effect=run_stage,
        ) as stage:
            output = gp.check_candidates(
                [
                    {
                        "id": "c1",
                        "media_type": "DIRECT_TS",
                        "normalized_url": "http://example.test/live.ts",
                    }
                ],
                {"max_probes": 3},
            )

        result = output[0]["globalping"]
        checks = result["checks"]

        self.assertEqual(checks["hls"]["status"], gp.NA)
        self.assertEqual(checks["ts"]["status"], gp.NA)
        self.assertEqual(checks["ipv4"]["status"], gp.PASS)
        self.assertEqual(checks["http"]["status"], gp.PASS)
        self.assertEqual(result["status"], gp.PASS)
        self.assertEqual(probes.call_count, 3)
        self.assertEqual(stage.call_count, 3)
        self.assertEqual(
            [result["stages"][name]["asn"] for name in (
                "probe_1", "probe_2", "probe_3"
            )],
            [9808, 4134, 4837],
        )

    def test_hls_requires_main_and_media_playlist_only(self):
        """每个 ASN 都必须完成主清单和适用的媒体子清单检测。"""

        main_result = finished_http(MASTER_PLAYLIST, 200)
        media_result = finished_http(MEDIA_PLAYLIST, 200)
        asns = iter([9808, 4134, 4837])
        active_probe = {}
        stage_calls = []
        current_main_id = None

        def choose_one(*args, **kwargs):
            asn = next(asns)
            active_probe.clear()
            active_probe.update({**PROBE, "asn": asn})
            return [{
                "country": "CN",
                "asn": asn,
                "tags": ["eyeball-network"],
                "limit": 1,
            }]

        def run_stage(url, locations, budget, headers=None):
            nonlocal current_main_id

            if url.endswith("/master.m3u8"):
                if not isinstance(locations, list) or len(locations) != 1:
                    raise AssertionError("主清单必须使用当前阶段指定的单个探针。")
                if locations[0].get("asn") != active_probe.get("asn"):
                    raise AssertionError("主清单探针 ASN 与当前阶段不一致。")

                current_main_id = f"main-{active_probe['asn']}"
                stage_calls.append(("main", url, locations))

                return (
                    current_main_id,
                    measurement(result_item(main_result, probe=dict(active_probe))),
                )

            if locations != current_main_id:
                raise AssertionError("子清单必须复用主清单的测量 ID。")

            stage_calls.append(("media", url, locations))

            return (
                f"media-{active_probe['asn']}",
                measurement(result_item(media_result, probe=dict(active_probe))),
            )

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ), patch.object(
            gp,
            "choose_probes",
            side_effect=choose_one,
        ) as probes, patch.object(
            gp,
            "_stage",
            side_effect=run_stage,
        ) as stage:
            output = gp.check_candidates(
                [
                    {
                        "id": "c1",
                        "media_type": "HLS",
                        "normalized_url": "http://example.test/master.m3u8",
                    }
                ],
                {"max_probes": 3},
            )

        result = output[0]["globalping"]

        self.assertEqual(result["status"], gp.PASS)
        self.assertEqual(
            set(result["stages"]),
            {"probe_1", "probe_2", "probe_3"},
        )
        self.assertEqual(probes.call_count, 3)
        self.assertEqual(stage.call_count, 6)
        self.assertEqual(len(result["measurements"]), 6)
        self.assertEqual(result["checks"]["ts"]["status"], gp.NA)

        for stage_name in ("probe_1", "probe_2", "probe_3"):
            sub_stages = result["stages"][stage_name]["sub_stages"]
            self.assertEqual(
                set(sub_stages),
                {"main_playlist", "media_playlist"},
            )
            self.assertEqual(
                result["stages"][stage_name]["status"],
                gp.PASS,
            )

        for index in range(0, len(stage_calls), 2):
            main_call = stage_calls[index]
            media_call = stage_calls[index + 1]

            self.assertEqual(main_call[0], "main")
            self.assertEqual(media_call[0], "media")
            self.assertEqual(media_call[2], main_call[0] if False else f"main-{main_call[2][0]['asn']}")

    def test_fail_or_incomplete_stops_later_probe_stages(self):
        candidate = {
            "id": "c1",
            "media_type": "DIRECT_TS",
            "normalized_url": "http://example.test/live.ts",
        }

        for failure_stage in (1, 2):
            for failure_status in (gp.FAIL, gp.INCOMPLETE):
                with self.subTest(
                    failure_stage=failure_stage,
                    failure_status=failure_status,
                ):
                    probe_asns = iter([9808, 4134, 4837])
                    stage_calls = []

                    def choose_one(*args, **kwargs):
                        asn = next(probe_asns)
                        return [{
                            "country": "CN",
                            "asn": asn,
                            "tags": ["eyeball-network"],
                            "limit": 1,
                        }]

                    def run_stage(url, locations, budget, headers=None):
                        stage_calls.append(locations[0]["asn"])

                        if len(stage_calls) == failure_stage:
                            raise gp.GlobalpingError(
                                "模拟阶段未通过",
                                status=failure_status,
                            )

                        probe = {
                            **PROBE,
                            "asn": locations[0]["asn"],
                        }
                        return (
                            f"m-{probe['asn']}",
                            measurement(
                                result_item(
                                    finished_http("", 200),
                                    probe=probe,
                                )
                            ),
                        )

                    with patch.object(
                        gp,
                        "read_limits",
                        return_value={
                            "limit": 50,
                            "remaining": 20,
                            "reset": 1,
                        },
                    ), patch.object(
                        gp,
                        "choose_probes",
                        side_effect=choose_one,
                    ) as probes, patch.object(
                        gp,
                        "_stage",
                        side_effect=run_stage,
                    ) as stage:
                        output = gp.check_candidates(
                            [candidate],
                            {"max_probes": 3},
                        )

                    result = output[0]["globalping"]

                    self.assertEqual(
                        stage.call_count,
                        failure_stage,
                    )
                    self.assertEqual(
                        probes.call_count,
                        failure_stage,
                    )
                    self.assertEqual(
                        len(result["stages"]),
                        failure_stage,
                    )
                    self.assertEqual(
                        result["stages"][
                            f"probe_{failure_stage}"
                        ]["status"],
                        failure_status,
                    )
                    self.assertEqual(
                        result["status"],
                        failure_status,
                    )

    
    def test_rate_limit_stops_later_candidate_measurements(self):
        calls = []
        def rate_limited(*args, **kwargs):
            calls.append(1)
            raise gp.GlobalpingError("HTTP 429", rate_limited=True)
        with patch.object(gp, "read_limits", return_value={"limit": 50, "remaining": 20, "reset": 1}), \
             patch.object(gp, "choose_probes", return_value=[{"country": "CN", "asn": 9808, "tags": ["eyeball-network"], "limit": 1}]), \
             patch.object(gp, "_stage", side_effect=rate_limited):
            output = gp.check_candidates([
                {"id": "c1", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/a.ts"},
                {"id": "c2", "media_type": "DIRECT_TS", "normalized_url": "http://example.test/b.ts"},
            ], {"max_probes": 3})
        self.assertEqual(len(calls), 1)
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)
        self.assertEqual(output[1]["globalping"]["status"], gp.INCOMPLETE)
        self.assertIn("停止", output[1]["globalping"]["error"])

    def test_uncertain_measurement_creation_stops_later_candidates(self):
        settings = {"max_probes": 3}
        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ), patch.object(
            gp,
            "_remaining_budget",
            return_value=20,
        ), patch.object(
            gp,
            "choose_probes",
            return_value=[
                {
                    "country": "CN",
                    "asn": 9808,
                    "tags": ["eyeball-network"],
                    "limit": 1,
                }
            ],
        ) as probes, patch.object(
            gp,
            "_api_request",
            side_effect=gp.GlobalpingError(
                "模拟网络中断，测量创建结果未知"
            ),
        ) as request:
            session = gp.create_session(settings)

            output = gp.check_candidates(
                candidates,
                settings,
                session=session,
            )

        self.assertTrue(session["budget"]["halted"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(probes.call_count, 1)
        self.assertEqual(
            output[0]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertEqual(
            output[1]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertIn(
            "停止",
            output[1]["globalping"]["error"],
        )
    
    def test_halted_budget_prevents_new_measurement(self):
        with patch.object(gp, "_api_request") as request:
            with self.assertRaises(gp.GlobalpingError):
                gp._create_measurement(
                    "http://example.test/a.ts",
                    [{"country": "CN", "asn": 9808, "limit": 1}],
                    {"remaining": 10, "probe_count": 1, "halted": True},
                )
        request.assert_not_called()

    def test_insufficient_quota_stops_later_candidate_measurements(self):
        def quota_exhausted(url, locations, budget, headers=None):
            gp._create_measurement(
                url,
                locations,
                budget,
                headers,
            )

        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 10, "reset": 1},
        ), patch.object(
            gp,
            "choose_probes",
            return_value=[
                {
                    "country": "CN",
                    "asn": 9808,
                    "tags": ["eyeball-network"],
                    "limit": 1,
                }
            ],
        ), patch.object(
            gp,
            "_remaining_budget",
            return_value=0,
        ) as remaining, patch.object(
            gp,
            "_stage",
            side_effect=quota_exhausted,
        ) as stage:
            output = gp.check_candidates(
                candidates,
                {"max_probes": 3},
            )

        self.assertEqual(stage.call_count, 1)
        self.assertEqual(remaining.call_count, 1)
        self.assertEqual(
            output[0]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertEqual(
            output[1]["globalping"]["status"],
            gp.INCOMPLETE,
        )
        self.assertIn(
            "停止",
            output[1]["globalping"]["error"],
        )


    def test_shared_session_reuses_quota_and_selects_probes_per_candidate(self):
        settings = {"max_probes": 3}
        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]
        ts_result = finished_http("", 200)
        asns = iter([9808, 4134, 4837, 9808, 4134, 4837])

        def choose_one(*args, **kwargs):
            asn = next(asns)
            return [{
                "country": "CN",
                "asn": asn,
                "tags": ["eyeball-network"],
                "limit": 1,
            }]

        def run_stage(url, locations, budget, headers=None):
            probe = {**PROBE, "asn": locations[0]["asn"]}
            return (
                f"m-{probe['asn']}",
                measurement(result_item(ts_result, probe=probe)),
            )

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ) as limits, patch.object(
            gp,
            "choose_probes",
            side_effect=choose_one,
        ) as probes, patch.object(
            gp,
            "_stage",
            side_effect=run_stage,
        ) as stage:
            session = gp.create_session(settings)

            first = gp.check_candidates(
                [candidates[0]], settings, session=session
            )
            second = gp.check_candidates(
                [candidates[1]], settings, session=session
            )

        self.assertEqual(limits.call_count, 1)
        self.assertEqual(probes.call_count, 6)
        self.assertEqual(stage.call_count, 6)

        for output in (first, second):
            result = output[0]["globalping"]
            self.assertEqual(result["status"], gp.PASS)
            self.assertEqual(
                [result["stages"][name]["asn"] for name in (
                    "probe_1", "probe_2", "probe_3"
                )],
                [9808, 4134, 4837],
            )

    def test_shared_session_stops_after_rate_limit(self):
        settings = {"max_probes": 3}
        candidates = [
            {
                "id": "c1",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/a.ts",
            },
            {
                "id": "c2",
                "media_type": "DIRECT_TS",
                "normalized_url": "http://example.test/b.ts",
            },
        ]

        with patch.object(
            gp,
            "read_limits",
            return_value={"limit": 50, "remaining": 20, "reset": 1},
        ), patch.object(
            gp,
            "choose_probes",
            return_value=[
                {
                    "country": "CN",
                    "asn": 9808,
                    "tags": ["eyeball-network"],
                    "limit": 1,
                }
            ],
        ):
            session = gp.create_session(settings)

        with patch.object(
            gp,
            "_stage",
            side_effect=gp.GlobalpingError(
                "HTTP 429", rate_limited=True
            ),
        ) as stage:
            first = gp.check_candidates(
                [candidates[0]], settings, session=session
            )
            second = gp.check_candidates(
                [candidates[1]], settings, session=session
            )

        self.assertEqual(stage.call_count, 1)
        self.assertTrue(session["budget"]["halted"])
        self.assertEqual(first[0]["globalping"]["status"], gp.INCOMPLETE)
        self.assertEqual(second[0]["globalping"]["status"], gp.INCOMPLETE)

    def test_session_initialization_error_is_preserved(self):
        settings = {"max_probes": 3}
        candidate = {
            "id": "c1",
            "media_type": "DIRECT_TS",
            "normalized_url": "http://example.test/a.ts",
        }

        with patch.object(
            gp,
            "read_limits",
            side_effect=gp.GlobalpingError("额度查询失败"),
        ), patch.object(gp, "choose_probes") as probes:
            session = gp.create_session(settings)

        with patch.object(gp, "_stage") as stage:
            output = gp.check_candidates(
                [candidate], settings, session=session
            )

        self.assertIn("额度查询失败", session["error"])
        self.assertEqual(output[0]["globalping"]["status"], gp.INCOMPLETE)
        probes.assert_not_called()
        stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()

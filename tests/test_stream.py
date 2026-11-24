
import json
import subprocess
import unittest
from unittest.mock import patch

import stream


class StreamQualityTests(unittest.TestCase):

    def test_unvalidated_bitrate_window_cannot_pass(self):
        with (
            patch("stream.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("stream._probe", return_value=self.metadata()),
            patch("stream.BITRATE_SAMPLE_VALIDATED", False),
            patch("stream._decode") as decode,
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        self.assertIn("真实线路样本验证", result["reason"])
        decode.assert_not_called()
    
    def metadata(self, **overrides):
        result = {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "field_order": "progressive",
            "fps": 25.0,
            "bitrate": 5_000_000,
            "scan_type": "progressive",
        }
        result.update(overrides)
        return result

    def check(self, metadata):
        with (
            patch("stream.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("stream._probe", return_value=metadata),
            patch("stream.BITRATE_SAMPLE_VALIDATED", True),
            patch("stream._decode") as decode,
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )
        return result, decode

    def test_h264_1080p_meeting_all_thresholds_passes(self):
        result, decode = self.check(self.metadata())

        self.assertEqual(result["status"], stream.PASS)
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        self.assertEqual(result["fps"], 25.0)
        self.assertEqual(result["bitrate"], 5_000_000)
        decode.assert_called_once()

    def test_hevc_1080p_with_4mbps_passes(self):
        result, decode = self.check(
            self.metadata(
                codec="hevc",
                bitrate=4_000_000,
            )
        )

        self.assertEqual(result["status"], stream.PASS)
        decode.assert_called_once()

    def test_resolution_width_below_threshold_fails(self):
        result, decode = self.check(
            self.metadata(width=1919)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_resolution_height_below_threshold_fails(self):
        result, decode = self.check(
            self.metadata(height=1079)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_unsupported_codec_fails(self):
        result, decode = self.check(
            self.metadata(codec="mpeg2video")
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_fps_below_23_fails(self):
        result, decode = self.check(
            self.metadata(fps=22.99)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_h264_bitrate_below_5mbps_fails(self):
        result, decode = self.check(
            self.metadata(bitrate=4_999_999)
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_hevc_bitrate_below_4mbps_fails(self):
        result, decode = self.check(
            self.metadata(
                codec="hevc",
                bitrate=3_999_999,
            )
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    def test_interlaced_video_is_not_rejected_by_field_order(self):
        result, decode = self.check(
            self.metadata(field_order="tt")
        )

        self.assertEqual(result["status"], stream.PASS)
        decode.assert_called_once()

    def test_missing_ffmpeg_returns_incomplete(self):
        with patch("stream.shutil.which", return_value=None):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)

    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    def test_probe_failure_does_not_pass(self, probe, which):
        probe.side_effect = stream.StreamQualityError(
            "无法确认真实视频分辨率"
        )

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.INCOMPLETE)

    def test_unexpected_probe_exception_returns_incomplete(self):
        with (
            patch("stream.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch(
                "stream._probe",
                side_effect=RuntimeError("unexpected probe error"),
            ),
        ):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        self.assertIn("RuntimeError", result["reason"])
    
    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    @patch("stream._decode")
    def test_decode_failure_does_not_pass(
        self, decode, probe, which
    ):
        probe.return_value = self.metadata()
        decode.side_effect = stream.StreamQualityError(
            "视频解码失败"
        )

        with patch("stream.BITRATE_SAMPLE_VALIDATED", True):
            result = stream.check_stream(
                "https://example.com/live.ts"
            )

        self.assertEqual(result["status"], stream.INCOMPLETE)
        decode.assert_called_once()

    def test_invalid_url_fails(self):
        result = stream.check_stream("not-a-valid-url")

        self.assertEqual(result["status"], stream.FAIL)

    @patch("stream.check_stream")
    def test_check_candidates_preserves_candidate(self, check):
        check.return_value = {
            "status": stream.PASS,
            "width": 1920,
            "height": 1080,
        }

        candidate = {
            "channel": "CCTV-1",
            "normalized_url": "https://example.com/live.ts",
        }

        results = stream.check_candidates([candidate])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["channel"], "CCTV-1")
        self.assertEqual(
            results[0]["stream_quality"]["status"],
            stream.PASS,
        )
        check.assert_called_once_with(
            "https://example.com/live.ts"
        )

    def test_quality_passed_requires_explicit_pass(self):
        self.assertTrue(
            stream.quality_passed({
                "stream_quality": {"status": stream.PASS}
            })
        )

        self.assertFalse(
            stream.quality_passed({
                "stream_quality": {"status": stream.FAIL}
            })
        )

        self.assertFalse(
            stream.quality_passed({
                "stream_quality": {"status": stream.INCOMPLETE}
            })
        )


class StreamProbeMeasurementTests(unittest.TestCase):

    def probe_with_data(self, data):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )

        with patch("stream._run", return_value=result):
            return stream._probe("https://example.com/live.ts")

    def base_data(self):
        return {
            "streams": [{
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "field_order": "progressive",
                "avg_frame_rate": "60/1",
                "r_frame_rate": "60/1",
                "bit_rate": "6000000",
            }],
            "frames": [
                {"best_effort_timestamp_time": "0.00"},
                {"best_effort_timestamp_time": "0.04"},
                {"best_effort_timestamp_time": "0.08"},
            ],
            "packets": [
                {
                    "pts_time": "0",
                    "size": "312500",
                    "duration_time": "1",
                },
                {
                    "pts_time": "1",
                    "size": "312500",
                    "duration_time": "1",
                },
            ],
        }

    def test_packet_bitrate_includes_last_packet_duration(self):
        bitrate = stream._packet_bitrate([
            {
                "pts_time": "0",
                "size": "312500",
                "duration_time": "1",
            },
            {
                "pts_time": "1",
                "size": "312500",
                "duration_time": "1",
            },
        ])

        self.assertEqual(bitrate, 2_500_000)

    def test_missing_packet_duration_returns_none(self):
        bitrate = stream._packet_bitrate([
            {
                "pts_time": "0",
                "size": "312500",
                "duration_time": "1",
            },
            {
                "pts_time": "1",
                "size": "312500",
            },
        ])

        self.assertIsNone(bitrate)

    def test_fps_uses_actual_frame_timestamps(self):
        data = self.base_data()

        result = self.probe_with_data(data)

        self.assertEqual(result["fps"], 25.0)
        self.assertEqual(result["scan_type"], "progressive")

    def test_missing_frame_timestamps_does_not_fallback_to_avg_fps(self):
        data = self.base_data()
        data["frames"] = []

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)

    def test_invalid_frame_timestamp_returns_incomplete_data(self):
        data = self.base_data()
        data["frames"][1]["best_effort_timestamp_time"] = "N/A"

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)
    def test_out_of_order_frame_timestamps_are_rejected(self):
        data = self.base_data()
        data["frames"][2]["best_effort_timestamp_time"] = "0.02"

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)
    
    def test_interlaced_field_order_is_identified(self):
        self.assertEqual(
            stream._classify_scan_type("tt"),
            "interlaced",
        )

    def test_unknown_field_order_is_preserved_as_unknown(self):
        self.assertEqual(
            stream._classify_scan_type(None),
            "unknown",
        )

    def test_invalid_field_order_type_is_unknown(self):
        self.assertEqual(
            stream._classify_scan_type([]),
            "unknown",
        )
        self.assertEqual(
            stream._classify_scan_type({}),
            "unknown",
        )
    
    def test_packet_bitrate_rejects_malformed_packet_instead_of_skipping(self):
        bitrate = stream._packet_bitrate([
            {
                "pts_time": "0",
                "size": "312500",
                "duration_time": "1",
            },
            None,
            {
                "pts_time": "1",
                "size": "312500",
                "duration_time": "1",
            },
        ])

        self.assertIsNone(bitrate)

    def test_packet_bitrate_rejects_invalid_packet_size(self):
        bitrate = stream._packet_bitrate([
            {
                "pts_time": "0",
                "size": "312500",
                "duration_time": "1",
            },
            {
                "pts_time": "1",
                "size": "N/A",
                "duration_time": "1",
            },
        ])

        self.assertIsNone(bitrate)

    def test_packet_bitrate_uses_dts_when_all_pts_are_unavailable(self):
        bitrate = stream._packet_bitrate([
            {
                "pts_time": "N/A",
                "dts_time": "0",
                "size": "312500",
                "duration_time": "1",
            },
            {
                "pts_time": "N/A",
                "dts_time": "1",
                "size": "312500",
                "duration_time": "1",
            },
        ])

        self.assertEqual(bitrate, 2_500_000)
    
    def test_packet_bitrate_rejects_mixed_pts_and_dts(self):
        bitrate = stream._packet_bitrate([
            {
                "pts_time": "N/A",
                "dts_time": "0",
                "size": "312500",
                "duration_time": "1",
            },
            {
                "pts_time": "1",
                "size": "312500",
                "duration_time": "1",
            },
        ])

        self.assertIsNone(bitrate)

    def test_probe_rejects_non_object_json(self):
        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data([])

    def test_probe_rejects_invalid_stream_record(self):
        data = self.base_data()
        data["streams"][0] = None

        with self.assertRaises(stream.StreamQualityError):
            self.probe_with_data(data)

    def test_boolean_frame_timestamp_is_rejected(self):
        self.assertIsNone(
            stream._frame_rate_from_timestamps([
                {"best_effort_timestamp_time": True},
                {"best_effort_timestamp_time": "0.04"},
            ])
        )

class StreamDecodeTests(unittest.TestCase):

    @patch("stream._run")
    def test_decode_passes_with_30_frames(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="frame=30\nprogress=end\n",
            stderr="",
        )

        stream._decode("https://example.com/live.ts")

        command = run.call_args.args[0]
        self.assertIn("-progress", command)
        self.assertIn("pipe:1", command)
        run.assert_called_once_with(
            command,
            stream.DECODE_TIMEOUT,
        )

    @patch("stream._run")
    def test_decode_rejects_fewer_than_30_frames(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="frame=10\nprogress=end\n",
            stderr="",
        )

        with self.assertRaises(stream.StreamQualityError):
            stream._decode("https://example.com/live.ts")

    @patch("stream._run")
    def test_decode_rejects_missing_frame_count(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="progress=end\n",
            stderr="",
        )

        with self.assertRaises(stream.StreamQualityError):
            stream._decode("https://example.com/live.ts")

    @patch("stream._run")
    def test_decode_rejects_ffmpeg_error(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="frame=10\n",
            stderr="decode error",
        )

        with self.assertRaises(stream.StreamQualityError):
            stream._decode("https://example.com/live.ts")


if __name__ == "__main__":
    unittest.main()

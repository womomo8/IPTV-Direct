
import unittest
from unittest.mock import patch

import stream


class StreamQualityTests(unittest.TestCase):

    def metadata(self, **overrides):
        result = {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "field_order": "progressive",
            "fps": 25.0,
            "bitrate": 5_000_000,
        }
        result.update(overrides)
        return result

    def check(self, metadata):
        with (
            patch("stream.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("stream._probe", return_value=metadata),
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

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.INCOMPLETE)

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


if __name__ == "__main__":
    unittest.main()

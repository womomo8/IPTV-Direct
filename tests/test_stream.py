
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
            "bitrate": 4000000,
        }
        result.update(overrides)
        return result

    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    @patch("stream._decode")
    def test_1080p_progressive_decode_passes(
        self, decode, probe, which
    ):
        probe.return_value = self.metadata()

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.PASS)
        self.assertEqual(result["width"], 1920)
        self.assertEqual(result["height"], 1080)
        decode.assert_called_once()

    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    @patch("stream._decode")
    def test_resolution_below_1080p_fails(
        self, decode, probe, which
    ):
        probe.return_value = self.metadata(width=1280, height=720)

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    @patch("stream.shutil.which", return_value="/usr/bin/ffmpeg")
    @patch("stream._probe")
    @patch("stream._decode")
    def test_interlaced_video_fails(
        self, decode, probe, which
    ):
        probe.return_value = self.metadata(
            field_order="tt"
        )

        result = stream.check_stream(
            "https://example.com/live.ts"
        )

        self.assertEqual(result["status"], stream.FAIL)
        decode.assert_not_called()

    @patch("stream.shutil.which", return_value=None)
    def test_missing_ffmpeg_returns_incomplete(self, which):
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

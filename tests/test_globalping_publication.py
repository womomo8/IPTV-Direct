import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import globalping


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

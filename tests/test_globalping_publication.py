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

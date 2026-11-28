
"""来源测试：验证 collector → main → normalizer 的真实交接。"""

import contextlib
import io
import unittest
from unittest.mock import patch

import collector
import main


class SourceFlowTests(unittest.TestCase):

    def test_stream_concurrency_settings_are_loaded_and_required(self):
        config = main.load_configuration()
        detection = config["settings"]["detection"]

        self.assertEqual(
            detection["stream_probe_concurrency"],
            4,
        )
        self.assertEqual(
            detection["stream_decode_concurrency"],
            2,
        )

        for key in (
            "stream_probe_concurrency",
            "stream_decode_concurrency",
        ):
            settings = dict(config["settings"])
            settings["detection"] = dict(detection)
            del settings["detection"][key]

            with self.assertRaisesRegex(
                ValueError,
                f"detection\\.{key}",
            ):
                main._check_settings(settings)

    def test_real_automatic_sources_are_loaded(self):
        config = main.load_configuration()
        automatic_sources = config["automatic_sources"]

        self.assertTrue(
            automatic_sources,
            "sources.json 没有加载出启用的自动来源",
        )

        source_ids = [
            source["id"]
            for source in automatic_sources
        ]

        self.assertEqual(
            len(source_ids),
            len(set(source_ids)),
            "正式自动来源存在重复 ID",
        )

        for source in automatic_sources:
            self.assertEqual(source["source_type"], "automatic")
            self.assertTrue(source["enabled"])
            self.assertIn(source["mode"], ("playlist", "index"))
            self.assertTrue(source["url"].startswith(("http://", "https://")))
    
    def test_real_source_flow_with_all_source_types(self):
        from normalizer import parse_source_content

        config = main.load_configuration()

        # 人工来源全部来自正式 manual.txt。
        remote_sources = [
            source
            for source in config["manual_sources"]
            if source.get("mode") == "manual"
        ]
        inline_sources = [
            source
            for source in config["manual_sources"]
            if source.get("mode") == "inline"
        ]

        fixture_channels = config["channels"]
        self.assertTrue(fixture_channels, "channels.txt 没有目标频道")

        fixture_channel = fixture_channels[0]

        # 自动来源使用独立模拟配置，避免访问真实网络。
        playlist_url = "https://playlist.test/list.m3u"
        index_url = "https://index.test/catalog"
        index_playlist_url = "https://index.test/from-index.txt"

        config["automatic_sources"] = [
            {
                "id": "test-playlist",
                "name": "模拟自动播放列表",
                "url": playlist_url,
                "enabled": True,
                "mode": "playlist",
                "source_type": "automatic",
                "priority": 1,
                "order": 0,
            },
            {
                "id": "test-index",
                "name": "模拟索引",
                "url": index_url,
                "enabled": True,
                "mode": "index",
                "source_type": "automatic",
                "priority": 1,
                "order": 1,
            },
        ]

        # 每条远程人工来源都使用自己的正式 URL。
        # 下载内容采用模拟 M3U，不依赖外部网站当前返回的数据格式。
        downloads = {}

        for index, source in enumerate(remote_sources):
            channel = fixture_channels[index % len(fixture_channels)]
            downloads[source["url"]] = {
                "content": (
                    "#EXTM3U\n"
                    f'#EXTINF:-1 tvg-name="{channel}",{channel}\n'
                    f"https://media.test/manual-{index}.m3u8\n"
                ).encode("utf-8"),
                "content_type": "application/vnd.apple.mpegurl",
            }

        downloads[playlist_url] = {
            "content": (
                "#EXTM3U\n"
                f'#EXTINF:-1 tvg-name="{fixture_channel}",'
                f"{fixture_channel}\n"
                "https://media.test/automatic.m3u8\n"
            ).encode("utf-8"),
            "content_type": "application/vnd.apple.mpegurl",
        }

        downloads[index_url] = {
            "content": (
                '<html><body>'
                '<a href="from-index.txt">播放列表</a>'
                '<a href="ordinary.html">普通网页</a>'
                "</body></html>"
            ).encode("utf-8"),
            "content_type": "text/html",
        }

        downloads[index_playlist_url] = {
            "content": (
                f"{fixture_channel},"
                "https://media.test/index.m3u8\n"
            ).encode("utf-8"),
            "content_type": "text/plain",
        }

        downloaded_urls = []
        detected_candidates = []

        def fake_download(url, *args, **kwargs):
            downloaded_urls.append(url)

            if url not in downloads:
                raise AssertionError(
                    f"测试访问了未设置的来源：{url}"
                )

            item = downloads[url]

            return {
                "url": url,
                "final_url": url,
                "content": item["content"],
                "content_type": item["content_type"],
                "http_status": 200,
                "truncated_media_probe": False,
            }

        def fake_detection(
            candidates,
            accepted_routes,
            settings,
            channels,
        ):
            detected_candidates.extend(candidates)

            admission = {
                "accepted_routes": accepted_routes,
                "channel_counts": {
                    channel: 0 for channel in channels
                },
                "all_channels_reached": False,
                "results": [],
            }

            return candidates, admission, candidates

        subscriptions = {
            "route_count": 0,
            "channel_counts": {
                channel: 0 for channel in config["channels"]
            },
            "m3u": "",
            "txt": "",
        }

        publish_result = {
            "published": True,
            "channel_count": 0,
            "route_count": 0,
            "m3u_path": "模拟路径/iptv.m3u",
            "txt_path": "模拟路径/iptv.txt",
            "first_publish": True,
        }

        # 保留真实 collector、main、normalizer。
        # 只模拟网络、线路检测、准入和发布边界。
        with (
            patch.object(
                collector,
                "download_url",
                side_effect=fake_download,
            ),
            patch.object(
                main,
                "run_source_batch_pipeline",
                side_effect=fake_detection,
            ),
            patch.object(main, "_print_detection_summary"),
            patch.object(
                main.publisher,
                "build_subscriptions",
                return_value=subscriptions,
            ),
            patch.object(
                main.publisher,
                "publish_subscriptions",
                return_value=publish_result,
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = main.run_formal_flow(config)

        self.assertEqual(result, 0)

        configured_channels = set(config["channels"])
        detected_channels = {
            candidate["channel"]
            for candidate in detected_candidates
        }

        self.assertTrue(detected_candidates)
        self.assertTrue(
            detected_channels.issubset(configured_channels),
            f"检测结果出现正式频道配置之外的频道："
            f"{detected_channels - configured_channels}",
        )

        manual_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "manual"
        ]

        manual_source_urls = {
            candidate["source_url"]
            for candidate in manual_candidates
        }

        # 所有正式远程人工来源都必须进入实际采集流程。
        expected_remote_urls = {
            source["url"] for source in remote_sources
        }

        self.assertTrue(
            expected_remote_urls.issubset(manual_source_urls),
            "有正式远程人工来源没有进入检测",
        )

        # 对内嵌来源，先根据其真实内容判断是否存在有效目标频道，
        # 再验证有效候选确实进入检测，不固定来源数量或频道名单。
        for source in inline_sources:
            parsed = parse_source_content(
                source["inline_content"],
                source,
                config["channels"],
                content_type="application/vnd.apple.mpegurl",
                aliases=config["settings"].get("aliases", {}),
            )

            has_target_candidate = any(
                candidate.get("channel") in configured_channels
                for candidate in parsed["candidates"]
            )

            if has_target_candidate:
                self.assertIn(
                    source["url"],
                    manual_source_urls,
                    f"有效内嵌来源没有进入检测：{source['url']}",
                )

        # 自动播放列表和索引发现的播放列表分别进入检测。
        automatic_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "automatic"
        ]

        automatic_source_urls = {
            candidate["source_url"]
            for candidate in automatic_candidates
        }

        self.assertEqual(
            automatic_source_urls,
            {playlist_url, index_playlist_url},
        )

        # 索引页面本身不能作为播放列表进入解析。
        self.assertNotIn(index_url, automatic_source_urls)

        expected_download_urls = (
            expected_remote_urls
            | {playlist_url, index_url, index_playlist_url}
        )

        self.assertEqual(
            set(downloaded_urls),
            expected_download_urls,
        )
        self.assertEqual(
            len(downloaded_urls),
            len(expected_download_urls),
            "来源采集出现重复下载",
        )


if __name__ == "__main__":
    unittest.main()

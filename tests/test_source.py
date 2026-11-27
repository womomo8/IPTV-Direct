"""来源测试：验证 collector → main → normalizer 的真实交接。"""

import contextlib
import io
import unittest
from unittest.mock import patch

import collector
import main


class SourceFlowTests(unittest.TestCase):

    def test_real_source_flow_with_all_source_types(self):
        config = main.load_configuration()
        configured_channels = config["channels"]
        self.assertTrue(configured_channels, "channels.txt 没有目标频道")

        # 所有模拟频道均从当前 channels.txt 动态选取。
        test_channels = configured_channels
        test_channel_txt = test_channels[0]
        test_channel_m3u = test_channels[1 % len(test_channels)]
        test_channel_playlist = test_channels[2 % len(test_channels)]
        test_channel_index = test_channels[3 % len(test_channels)]

        # 使用仓库真实人工来源配置中的前两条远程来源。
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

        self.assertGreaterEqual(len(remote_sources), 2)
        self.assertTrue(
            inline_sources,
            "manual.txt 没有配置内嵌 M3U 来源",
        )

        manual_txt = remote_sources[0]
        manual_m3u = remote_sources[1]

        config["manual_sources"] = [
            manual_txt,
            manual_m3u,
            *inline_sources,
        ]

        # 模拟自动播放列表和索引来源。
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

        # 所有远程下载均使用模拟内容，不访问真实网络。
        downloads = {
            manual_txt["url"]: {
                "content": (
                    f"{test_channel_txt},https://media.test/manual-txt.m3u8\n"
                ).encode("utf-8"),
                "content_type": "text/plain",
            },
            manual_m3u["url"]: {
                "content": (
                    "#EXTM3U\n"
                    f'#EXTINF:-1 tvg-name="{test_channel_m3u}",{test_channel_m3u}\n'
                    "https://media.test/manual-m3u.m3u8\n"
                ).encode("utf-8"),
                "content_type": "application/vnd.apple.mpegurl",
            },
            playlist_url: {
                "content": (
                    "#EXTM3U\n"
                    f'#EXTINF:-1 tvg-name="{test_channel_playlist}",{test_channel_playlist}\n'
                    "https://media.test/automatic.m3u8\n"
                ).encode("utf-8"),
                "content_type": "application/vnd.apple.mpegurl",
            },
            index_url: {
                "content": (
                    '<html><body>'
                    '<a href="from-index.txt">播放列表</a>'
                    '<a href="ordinary.html">普通网页</a>'
                    "</body></html>"
                ).encode("utf-8"),
                "content_type": "text/html",
            },
            index_playlist_url: {
                "content": (
                    f"{test_channel_index},https://media.test/index.m3u8\n"
                ).encode("utf-8"),
                "content_type": "text/plain",
            },
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
                main,
                "normalize_source_content",
                wraps=main.normalize_source_content,
            ) as normalize_spy,
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

        # 四条模拟远程来源均应产生候选，不固定候选总数。
        self.assertGreaterEqual(len(detected_candidates), 4)

        channels = {
            candidate["channel"]
            for candidate in detected_candidates
        }

        self.assertTrue(channels)
        self.assertTrue(
            channels.issubset(set(configured_channels)),
            f"检测结果包含不在 channels.txt 中的频道："
            f"{sorted(channels - set(configured_channels))}",
        )

        # 四种模拟来源的频道均从当前 channels.txt 动态选取。
        expected_channels = {
            manual_txt["url"]: test_channel_txt,
            manual_m3u["url"]: test_channel_m3u,
            playlist_url: test_channel_playlist,
            index_playlist_url: test_channel_index,
        }

        for source_url, expected_channel in expected_channels.items():
            self.assertTrue(
                any(
                    candidate["source_url"] == source_url
                    and candidate["channel"] == expected_channel
                    for candidate in detected_candidates
                ),
                f"来源 {source_url} 未产生预期频道候选",
            )

        # manual.txt 中所有内嵌 M3U 均必须进入真实解析流程。
        normalized_source_urls = {
            call.kwargs.get("source", {}).get("url")
            for call in normalize_spy.call_args_list
        }

        for inline_source in inline_sources:
            self.assertIn(
                inline_source["url"],
                normalized_source_urls,
            )

        # 人工来源：普通 URL、远程 M3U、全部内嵌 M3U。
        manual_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "manual"
        ]
        self.assertGreaterEqual(len(manual_candidates), 2)

        # 两个模拟远程人工来源各产生一条候选。
        self.assertEqual(
            sum(
                candidate["source_url"] == manual_txt["url"]
                for candidate in manual_candidates
            ),
            1,
        )
        self.assertEqual(
            sum(
                candidate["source_url"] == manual_m3u["url"]
                for candidate in manual_candidates
            ),
            1,
        )

        manual_source_urls = {
            candidate["source_url"]
            for candidate in manual_candidates
        }

        self.assertIn(manual_txt["url"], manual_source_urls)
        self.assertIn(manual_m3u["url"], manual_source_urls)

        # 自动 M3U 和索引发现的播放列表分别进入检测。
        automatic_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "automatic"
        ]
        self.assertEqual(len(automatic_candidates), 2)

        automatic_source_urls = {
            candidate["source_url"]
            for candidate in automatic_candidates
        }

        self.assertEqual(
            automatic_source_urls,
            {playlist_url, index_playlist_url},
        )

        # 索引页面本身和普通网页均不能作为播放列表进入解析。
        self.assertNotIn(index_url, automatic_source_urls)

        self.assertEqual(
            downloaded_urls,
            [
                manual_txt["url"],
                manual_m3u["url"],
                playlist_url,
                index_url,
                index_playlist_url,
            ],
        )


if __name__ == "__main__":
    unittest.main()

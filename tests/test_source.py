
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
        self.assertEqual(len(inline_sources), 2)

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
                    "CCTV-2,https://media.test/manual-txt.m3u8\n"
                ).encode("utf-8"),
                "content_type": "text/plain",
            },
            manual_m3u["url"]: {
                "content": (
                    "#EXTM3U\n"
                    '#EXTINF:-1 tvg-name="CCTV-3",CCTV-3\n'
                    "https://media.test/manual-m3u.m3u8\n"
                ).encode("utf-8"),
                "content_type": "application/vnd.apple.mpegurl",
            },
            playlist_url: {
                "content": (
                    "#EXTM3U\n"
                    '#EXTINF:-1 tvg-name="CCTV-4",CCTV-4\n'
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
                    "CCTV-5,https://media.test/index.m3u8\n"
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

        # 来源线路数量可能变化，不固定候选总数。
        # 至少验证两个远程人工来源和两个内嵌来源均进入检测。
        self.assertGreaterEqual(len(detected_candidates), 6)

        channels = {
            candidate["channel"]
            for candidate in detected_candidates
        }

        # 四条模拟来源分别提供 CCTV-2 至 CCTV-5。
        # 这些频道必须进入检测；其他候选必须符合正式频道配置。
        self.assertTrue({
            "CCTV-2",
            "CCTV-3",
            "CCTV-4",
            "CCTV-5",
        }.issubset(channels))

        configured_channels = set(config["channels"])
        self.assertTrue(
            all(
                channel is None or channel in configured_channels
                for channel in channels
            ),
            "检测候选包含 channels.txt 之外的非空频道名称",
        )

        # 人工来源：普通 URL、远程 M3U、两个内嵌 M3U。
        manual_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "manual"
        ]
        self.assertGreaterEqual(len(manual_candidates), 4)

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

        for inline_source in inline_sources:
            self.assertIn(
                inline_source["url"],
                manual_source_urls,
            )
            self.assertTrue(
                any(
                    candidate["source_url"] == inline_source["url"]
                    for candidate in manual_candidates
                )
            )
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


"""来源测试：验证 collector → main → normalizer 的真实交接。

使用仓库实际配置和福建移动六频道内嵌 M3U；
网络下载、线路检测、准入和正式发布均使用模拟结果。
不访问真实直播源，不调用 Globalping，不修改正式订阅。
"""

import contextlib
import io
import unittest
from unittest.mock import patch

import collector
import main


class SourceFlowTests(unittest.TestCase):

    def test_real_source_flow_with_manual_playlist_and_index(self):
        # 读取仓库当前真实配置。
        config = main.load_configuration()

        # 保留 manual.txt 中真实的福建移动六频道内嵌 M3U。
        config["manual_sources"] = [
            source
            for source in config["manual_sources"]
            if source.get("mode") == "inline"
        ]

        self.assertEqual(len(config["manual_sources"]), 1)

        # 增加模拟自动播放列表和索引来源。
        playlist_url = "https://playlist.test/list.m3u"
        index_url = "https://index.test/catalog"
        index_playlist_url = "https://index.test/from-index.txt"

        config["automatic_sources"] = [
            {
                "id": "test-playlist",
                "name": "模拟播放列表",
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

        # 所有网络内容均为模拟数据。
        downloads = {
            playlist_url: {
                "content": (
                    '#EXTM3U\n'
                    '#EXTINF:-1 tvg-name="CCTV-2",CCTV-2\n'
                    'https://media.test/cctv2.m3u8\n'
                ).encode("utf-8"),
                "content_type": "application/vnd.apple.mpegurl",
            },
            index_url: {
                "content": (
                    '<html><body>'
                    '<a href="from-index.txt">播放列表</a>'
                    '<a href="ordinary.html">普通网页</a>'
                    '</body></html>'
                ).encode("utf-8"),
                "content_type": "text/html",
            },
            index_playlist_url: {
                "content": (
                    "CCTV-3,https://media.test/cctv3.m3u8\n"
                ).encode("utf-8"),
                "content_type": "text/plain",
            },
        }

        downloaded_urls = []
        detected_candidates = []

        def fake_download(url, *args, **kwargs):
            downloaded_urls.append(url)

            if url not in downloads:
                raise AssertionError(f"测试访问了未设置的来源：{url}")

            item = downloads[url]

            return {
                "url": url,
                "final_url": url,
                "content": item["content"],
                "content_type": item["content_type"],
                "http_status": 200,
                "truncated_media_probe": False,
            }

        def fake_detection(candidates, settings):
            detected_candidates.extend(candidates)
            return candidates

        def fake_admission(
            candidates,
            accepted_routes,
            channels,
            settings,
        ):
            return {
                "accepted_routes": accepted_routes,
                "channel_counts": {
                    channel: 0 for channel in channels
                },
                "all_channels_reached": False,
                "results": [],
            }

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

        # 真实执行 collector、main、normalizer。
        # 只模拟网络下载、检测、准入和发布边界。
        with (
            patch.object(
                collector,
                "download_url",
                side_effect=fake_download,
            ),
            patch.object(
                main,
                "run_detection_pipeline",
                side_effect=fake_detection,
            ),
            patch.object(
                main,
                "process_admission_batch",
                side_effect=fake_admission,
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

        # 六个福建移动频道 + 自动 M3U + 索引发现的 TXT。
        self.assertEqual(len(detected_candidates), 8)

        channels = {
            candidate["channel"]
            for candidate in detected_candidates
        }

        self.assertTrue({
            "CCTV-1",
            "CCTV-2",
            "CCTV-3",
            "CCTV-5",
            "CCTV-5+",
            "CCTV-7",
            "CCTV-9",
        }.issubset(channels))

        # 人工内嵌来源确实进入解析和检测。
        manual_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "manual"
        ]
        self.assertEqual(len(manual_candidates), 6)

        # 自动播放列表和索引发现的播放列表均进入检测。
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

        # 索引页面只用于提取链接，不能作为播放列表进入解析。
        self.assertNotIn(index_url, automatic_source_urls)

        self.assertEqual(
            downloaded_urls,
            [playlist_url, index_url, index_playlist_url],
        )


if __name__ == "__main__":
    unittest.main()


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

        # 六条福建移动线路、十四条专项补充线路，
        # 加上四条模拟远程来源候选。
        self.assertEqual(len(detected_candidates), 24)

        channels = {
            candidate["channel"]
            for candidate in detected_candidates
        }

        self.assertTrue({
            "CCTV-1",
            "CCTV-2",
            "CCTV-3",
            "CCTV-4",
            "CCTV-5",
            "CCTV-5+",
            "CCTV-7",
            "CCTV-9",
            "广东体育",
            "睛彩竞技",
            "睛彩篮球",
            "凤凰卫视中文台",
            "凤凰资讯台",
            "翡翠台",
            "澳门莲花卫视",
        }.issubset(channels))

        # 人工来源：普通 URL、远程 M3U、两个内嵌 M3U。
        manual_candidates = [
            candidate
            for candidate in detected_candidates
            if candidate["source_type"] == "manual"
        ]
        self.assertEqual(len(manual_candidates), 22)

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

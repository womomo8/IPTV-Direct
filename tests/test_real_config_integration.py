
"""真实配置下 collector 与 normalizer 集成测试。

读取仓库当前真实配置，实际执行来源采集和频道解析。
网络下载使用模拟响应，不访问真实来源、不执行线路检测、
不调用 Globalping、不消耗额度、不修改正式订阅。
"""

import unittest
from unittest.mock import patch

import collector
import main
from normalizer import normalize_source_content


class RealConfigurationIntegrationTests(unittest.TestCase):

    def test_real_configuration_collector_to_normalizer(self):
        config = main.load_configuration(root=main.ROOT)

        channels = config["channels"]
        manual_sources = config["manual_sources"]
        automatic_sources = config["automatic_sources"]
        settings = config["settings"]

        self.assertTrue(channels, "真实频道配置不能为空")
        self.assertTrue(manual_sources, "真实人工来源配置不能为空")
        self.assertTrue(automatic_sources, "真实自动来源配置不能为空")

        self.assertTrue(
            any(
                source.get("mode") == "inline"
                for source in manual_sources
            ),
            "当前人工来源配置应包含内嵌 M3U",
        )

        downloaded_urls = []

        test_channel = channels[0]

        def fake_download(url, *args):
            downloaded_urls.append(url)

            route_number = len(downloaded_urls)

            content = (
                "#EXTM3U\n"
                f'#EXTINF:-1 tvg-name="{test_channel}",'
                f"{test_channel}\n"
                f"https://media.integration.test/"
                f"route-{route_number}.m3u8\n"
            ).encode("utf-8")

            return {
                "url": url,
                "final_url": url,
                "content": content,
                "content_type": "application/vnd.apple.mpegurl",
                "http_status": 200,
                "truncated_media_probe": False,
            }

        # 真实 collector，只有网络下载函数被模拟。
        with patch(
            "collector.download_url",
            side_effect=fake_download,
        ) as download_mock:
            collected = list(
                collector.iter_source_contents(
                    manual_sources,
                    automatic_sources,
                    settings,
                )
            )

        self.assertTrue(collected, "collector 没有返回任何来源")

        source_types = [
            item["source"].get("source_type")
            for item in collected
        ]

        self.assertIn("manual", source_types)
        self.assertIn("automatic", source_types)

        first_automatic = source_types.index("automatic")

        self.assertTrue(
            all(
                item == "manual"
                for item in source_types[:first_automatic]
            ),
            "自动来源出现于人工来源之前",
        )

        self.assertTrue(
            all(
                item == "automatic"
                for item in source_types[first_automatic:]
            ),
            "人工来源出现在自动来源之后",
        )

        inline_items = [
            item
            for item in collected
            if item["source"].get("mode") == "inline"
        ]

        self.assertEqual(
            len(inline_items),
            1,
            "内嵌 M3U 应作为一个独立来源处理",
        )

        self.assertEqual(
            inline_items[0]["download"]["http_status"],
            "LOCAL",
            "内嵌 M3U 不应触发网络下载",
        )

        network_source_count = sum(
            item["source"].get("mode") != "inline"
            for item in collected
        )

        self.assertEqual(
            download_mock.call_count,
            network_source_count,
            "网络下载次数与实际网络来源数量不一致",
        )

        seen_urls = set()
        all_candidates = []
        source_candidate_counts = []

        collection = settings["collection"]
        detection = settings["detection"]
        aliases = settings.get("aliases", {})

        # 使用真实 normalizer，按正式流程共享全局 URL 集合。
        for item in collected:
            source = item["source"]
            downloaded = item["download"]

            self.assertFalse(
                item.get("errors"),
                f"模拟来源不应采集失败：{source.get('name')}",
            )

            self.assertIsInstance(downloaded, dict)

            remaining = (
                collection["max_candidates_total"]
                - len(all_candidates)
            )

            self.assertGreater(
                remaining,
                0,
                "测试候选数量不应超过真实配置总上限",
            )

            source_info = {
                **source,
                "final_url": downloaded["final_url"],
            }

            normalized = normalize_source_content(
                content=downloaded["content"],
                source=source_info,
                channels=channels,
                max_candidates_per_source=(
                    collection["max_candidates_per_source"]
                ),
                seen_urls=seen_urls,
                content_type=downloaded["content_type"],
                max_hls_manifest_bytes=(
                    detection["max_hls_manifest_bytes"]
                ),
                aliases=aliases,
                max_candidates_remaining=remaining,
            )

            self.assertTrue(
                normalized["recognized"],
                f"来源内容未能识别：{source.get('name')}",
            )

            candidates = normalized["candidates"]

            source_candidate_counts.append(len(candidates))

            for candidate in candidates:
                self.assertIn(
                    candidate["channel"],
                    channels,
                    "候选频道不属于当前真实目标频道配置",
                )

                self.assertEqual(
                    candidate["source_url"],
                    source["url"],
                    "候选来源 URL 丢失或错误",
                )

                self.assertEqual(
                    candidate["source_type"],
                    source["source_type"],
                    "候选来源类型丢失或错误",
                )

                self.assertEqual(
                    candidate["priority"],
                    source["priority"],
                    "候选来源优先级丢失或错误",
                )

                self.assertEqual(
                    candidate["source_order"],
                    source["order"],
                    "候选来源原始顺序丢失或错误",
                )

            all_candidates.extend(candidates)

        self.assertTrue(
            all_candidates,
            "真实配置经过采集和解析后没有形成候选池",
        )

        candidate_urls = [
            item["normalized_url"]
            for item in all_candidates
        ]

        self.assertEqual(
            len(candidate_urls),
            len(set(candidate_urls)),
            "候选池存在重复规范化 URL",
        )

        self.assertEqual(
            set(candidate_urls),
            seen_urls,
            "候选池与全局去重集合不一致",
        )

        self.assertLessEqual(
            len(all_candidates),
            collection["max_candidates_total"],
            "候选池超过真实配置总上限",
        )

        manual_candidates = [
            item
            for item in all_candidates
            if item["priority"] == 0
        ]

        automatic_candidates = [
            item
            for item in all_candidates
            if item["priority"] == 1
        ]

        self.assertTrue(
            manual_candidates,
            "没有形成任何人工来源候选",
        )

        self.assertTrue(
            automatic_candidates,
            "没有形成任何自动来源候选",
        )

        first_automatic_candidate = next(
            index
            for index, item in enumerate(all_candidates)
            if item["priority"] == 1
        )

        self.assertTrue(
            all(
                item["priority"] == 0
                for item in all_candidates[:first_automatic_candidate]
            ),
            "候选池没有保持人工来源优先顺序",
        )

        self.assertTrue(
            all(
                item["priority"] == 1
                for item in all_candidates[first_automatic_candidate:]
            ),
            "候选池中的自动来源顺序异常",
        )


if __name__ == "__main__":
    unittest.main()

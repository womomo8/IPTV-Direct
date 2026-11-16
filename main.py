
#!/usr/bin/env python3
"""IPTV-Direct 总入口与统一调度。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from collector import (
    SourceConfigError,
    iter_source_contents,
    prepare_sources,
)
from normalizer import (
    ChannelConfigError,
    load_channels,
    normalize_source_content,
)

ROOT = Path(__file__).resolve().parent

CONFIG_FILES = (
    "channels.txt",
    "manual.txt",
    "sources.json",
    "settings.json",
)

TEST_CHANNEL = "IPTV-Direct 模拟测试频道"


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path.name}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"配置文件不是有效 UTF-8 文本：{path.name}"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"读取配置文件失败：{path.name}：{exc}"
        ) from exc


def _read_json_file(path: Path) -> dict[str, Any]:
    raw = _read_text_file(path)

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 格式错误：{path.name}，"
            f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError(
            f"配置文件顶层必须是 JSON 对象：{path.name}"
        )

    return value


def _check_settings(settings: dict[str, Any]) -> None:
    """检查正式参数结构和数据类型，不使用备用默认值。"""

    required = {
        "collection": {
            "max_valid_sources": int,
            "max_candidates_per_source": int,
            "max_candidates_total": int,
            "max_index_links_per_page": int,
        },
        "detection": {
            "local_concurrency": int,
            "per_host_concurrency": int,
            "queue_limit": int,
            "max_queue_seconds": int,
            "candidate_active_budget_seconds": int,
            "source_download_timeout_seconds": int,
            "max_source_download_bytes": int,
            "max_hls_manifest_bytes": int,
            "max_ts_read_bytes": int,
            "max_redirects": int,
        },
        "globalping": {"max_probes": int},
        "selection": {"max_routes_per_channel": int},
        "schedule": {
            "timezone": str,
            "daily_time": str,
        },
    }

    for section, fields in required.items():
        section_value = settings.get(section)

        if not isinstance(section_value, dict):
            raise ValueError(
                f"settings.json 缺少有效配置区段：{section}"
            )

        for field, expected_type in fields.items():
            value = section_value.get(field)

            if expected_type is int:
                valid_type = (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                )

                if not valid_type or value <= 0:
                    raise ValueError(
                        f"settings.json 参数 "
                        f"{section}.{field} 必须是正整数"
                    )

            elif (
                not isinstance(value, expected_type)
                or not value.strip()
            ):
                raise ValueError(
                    f"settings.json 参数 "
                    f"{section}.{field} 必须是非空文本"
                )

    timezone = settings["schedule"]["timezone"]
    daily_time = settings["schedule"]["daily_time"]

    if timezone != "Asia/Shanghai":
        raise ValueError(
            "settings.json schedule.timezone 必须为 Asia/Shanghai"
        )

    if len(daily_time) != 5 or daily_time[2] != ":":
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        )

    try:
        hour, minute = (
            int(part) for part in daily_time.split(":")
        )
    except ValueError as exc:
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        ) from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            "settings.json schedule.daily_time 不是有效时间"
        )


def load_configuration(
    root: Path = ROOT,
    test_mode: bool = False,
) -> dict[str, Any]:
    """读取并检查四个根目录配置文件。"""

    for filename in CONFIG_FILES:
        if not (root / filename).is_file():
            raise ValueError(f"缺少配置文件：{filename}")

    try:
        channels = load_channels(str(root / "channels.txt"))
    except ChannelConfigError as exc:
        # 测试模式使用独立模拟频道，不要求用户配置真实频道。
        if test_mode and str(exc) == "channels.txt 没有配置目标频道":
            channels = []
        else:
            raise ValueError(str(exc)) from exc

    manual_raw = _read_text_file(root / "manual.txt")
    sources_config = _read_json_file(root / "sources.json")
    settings = _read_json_file(root / "settings.json")

    _check_settings(settings)

    try:
        source_data = prepare_sources(manual_raw, sources_config)
    except SourceConfigError as exc:
        raise ValueError(str(exc)) from exc

    return {
        "channels": channels,
        "manual_sources": source_data["manual_sources"],
        "automatic_sources": source_data["automatic_sources"],
        "disabled_sources": source_data["disabled_sources"],
        "source_errors": source_data["errors"],
        "settings": settings,
    }


def collect_and_parse(config: dict[str, Any]) -> dict[str, Any]:
    """调度来源采集和 normalizer 处理，汇总本轮候选数量。"""

    collection = config["settings"]["collection"]
    detection = config["settings"]["detection"]

    max_valid_sources = collection["max_valid_sources"]
    max_per_source = collection["max_candidates_per_source"]
    max_total = collection["max_candidates_total"]
    max_hls_bytes = detection["max_hls_manifest_bytes"]

    parsed_sources = []
    errors = []
    valid_sources = 0
    total_candidates = 0
    duplicate_candidates = 0
    stopped_reason = "所有配置来源均已处理"

    seen_urls: set[str] = set()

    contents = iter_source_contents(
        config["manual_sources"],
        config["automatic_sources"],
        config["settings"],
    )

    for item in contents:
        source = item["source"]

        if item["errors"]:
            errors.extend(
                f"{source.get('name', source.get('url', '来源'))}：{error}"
                for error in item["errors"]
            )
            continue

        downloaded = item["download"]

        source = {
            **source,
            "final_url": downloaded["final_url"],
        }

        remaining = max_total - total_candidates
        if remaining <= 0:
            stopped_reason = f"达到全部候选总上限 {max_total}"
            break

        try:
            normalized = normalize_source_content(
                content=downloaded["content"],
                source=source,
                channels=config["channels"],
                max_candidates_per_source=max_per_source,
                seen_urls=seen_urls,
                content_type=downloaded["content_type"],
                max_hls_manifest_bytes=max_hls_bytes,
                max_candidates_remaining=remaining,
            )
        except (TypeError, ValueError) as exc:
            errors.append(
                f"{source.get('name', source.get('url', '来源'))}："
                f"内容处理异常：{exc}"
            )
            continue

        if not normalized["recognized"]:
            detail = (
                "；".join(normalized["errors"])
                or "无法识别内容格式"
            )
            errors.append(
                f"{source.get('name', source.get('url', '来源'))}：{detail}"
            )
            continue

        source_candidate_count = normalized["extracted_candidates"]
        candidates = normalized["candidates"]

        total_candidates += source_candidate_count
        duplicate_candidates += normalized["duplicate_candidates"]

        matched_count = normalized["matched_target_candidates"]

        parsed_sources.append({
            "source": source,
            "format": normalized["format"],
            "recognized": normalized["recognized"],
            "candidates": candidates,
            "extracted_candidates": source_candidate_count,
            "duplicate_candidates": normalized["duplicate_candidates"],
            "retained_candidates": len(candidates),
            "matched_target_candidates": matched_count,
            "download": {
                "requested_url": downloaded["url"],
                "final_url": downloaded["final_url"],
                "content_type": downloaded["content_type"],
                "http_status": downloaded["http_status"],
                "truncated_media_probe": downloaded["truncated_media_probe"],
            },
        })

        errors.extend(
            f"{source.get('name', source.get('url', '来源'))}：{error}"
            for error in normalized["errors"]
        )

        if matched_count > 0:
            valid_sources += 1

        if valid_sources >= max_valid_sources:
            stopped_reason = f"达到有效来源上限 {max_valid_sources}"
            break

        if total_candidates >= max_total:
            stopped_reason = f"达到全部候选总上限 {max_total}"
            break

    return {
        "sources": parsed_sources,
        "errors": errors,
        "valid_sources": valid_sources,
        "total_candidates": total_candidates,
        "duplicate_candidates": duplicate_candidates,
        "unique_candidates": sum(
            len(source["candidates"]) for source in parsed_sources
        ),
        "stopped_reason": stopped_reason,
    }


def run_test_flow(config: dict[str, Any]) -> int:
    """
    单条模拟 HLS 测试流程。

    使用真实 normalizer 处理模拟输入；
    检测结果为模拟数据，不调用 checker 的真实网络检测，
    不调用 Globalping，不进入正式准入和发布流程。
    """

    print("测试流程：单条模拟 HLS")

    source_url = "https://example.invalid/iptv-direct-test.m3u"
    stream_url = "https://example.invalid/live/test.m3u8"

    simulated_source = {
        "name": "IPTV-Direct 模拟测试来源",
        "url": source_url,
        "final_url": source_url,
        "source_type": "manual",
        "priority": 0,
        "order": 0,
    }

    simulated_content = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="IPTV-Direct-Test" '
        'tvg-name="IPTV-Direct 模拟测试频道" '
        'group-title="测试",IPTV-Direct 模拟测试频道\n'
        f"{stream_url}\n"
    )

    try:
        normalized = normalize_source_content(
            content=simulated_content,
            source=simulated_source,
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type="audio/x-mpegurl",
            max_hls_manifest_bytes=(
                config["settings"]["detection"]["max_hls_manifest_bytes"]
            ),
            max_candidates_remaining=1,
        )

        if not normalized["recognized"]:
            raise ValueError("模拟来源未能被 normalizer 识别")

        candidates = normalized["candidates"]

        if len(candidates) != 1:
            raise ValueError(
                f"模拟候选数量错误：预期 1 条，实际 {len(candidates)} 条"
            )

        candidate = candidates[0]

        if candidate["channel"] != TEST_CHANNEL:
            raise ValueError("模拟频道匹配结果不正确")

        if candidate["normalized_url"] != stream_url:
            raise ValueError("模拟线路 URL 归一结果不正确")

        print("来源解析：PASS（normalizer 实际执行）")
        print("频道匹配：PASS（模拟测试频道）")
        print("候选数量：1")
        print("媒体线路：HLS（模拟输入标记，不代表实际媒体识别）")

        # 按 MASTER_PLAN.md 测试运行要求，检测结果使用模拟数据。
        # 不调用 checker.py 的真实网络检测。
        simulated_detection = {
            **candidate,
            "media_type": "HLS",
            "simulated": True,
            "checks": {
                "ipv4": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未建立真实 IPv4 连接",
                },
                "http": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未发送真实 HTTP 请求",
                },
                "hls": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未读取真实 HLS 清单",
                },
                "ts": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未读取真实 TS 分片",
                },
            },
        }

        expected_checks = ("ipv4", "http", "hls", "ts")

        if simulated_detection["media_type"] != "HLS":
            raise ValueError("模拟检测线路类型错误")

        if not simulated_detection["simulated"]:
            raise ValueError("模拟检测标记缺失")

        for name in expected_checks:
            check = simulated_detection["checks"].get(name)

            if not isinstance(check, dict):
                raise ValueError(f"模拟检测结果缺少项目：{name}")

            if check.get("status") != "PASS":
                raise ValueError(f"模拟检测项目状态错误：{name}")

            if "模拟检测结果" not in check.get("reason", ""):
                raise ValueError(f"模拟检测项目缺少模拟说明：{name}")

        print("本地检测结果结构：PASS（模拟结果，非真实检测）")
        print("Globalping：按测试规范跳过，未创建真实测量")
        print("正式准入：未调用")
        print("订阅发布：未调用，正式订阅未修改")
        print("国内真实网络验证：未执行")

        print(
            "测试运行通过：模拟来源解析及模拟检测结果交接符合预期。"
        )
        print(
            "注意：这不代表 checker.py 实际检测通过，"
            "也不代表线路在中国移动网络中能够播放。"
        )

        return 0

    except Exception as exc:
        print(
            f"测试运行失败：{exc}",
            file=sys.stderr,
        )
        return 1


def parse_args(
    argv: list[str] | None = None
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IPTV-Direct 自动订阅系统"
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("formal", "test"),
        help="运行模式：formal 正式运行；test 测试运行",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    mode_name = (
        "正式运行"
        if args.mode == "formal"
        else "测试运行"
    )

    print(f"IPTV-Direct：{mode_name}")

    try:
        config = load_configuration(test_mode=args.mode == "test")
    except ValueError as exc:
        print(
            f"配置检查失败：{exc}",
            file=sys.stderr
        )
        return 1

    print(
        "配置读取成功："
        f"目标频道 {len(config['channels'])} 个，"
        f"人工来源 {len(config['manual_sources'])} 条，"
        f"启用的自动来源 {len(config['automatic_sources'])} 条，"
        f"禁用的自动来源 {config['disabled_sources']} 条。"
    )

    for error in config["source_errors"]:
        print(f"来源配置警告：{error}", file=sys.stderr)

    if args.mode == "test":
        return run_test_flow(config)

    result = collect_and_parse(config)

    print(
        "来源处理结果："
        f"已识别来源 {len(result['sources'])} 个，"
        f"有效来源 {result['valid_sources']} 个，"
        f"累计候选 {result['total_candidates']} 条，"
        f"去重后候选 {result['unique_candidates']} 条，"
        f"重复候选 {result['duplicate_candidates']} 条。"
    )
    print(f"停止原因：{result['stopped_reason']}")

    for error in result["errors"]:
        print(f"来源处理警告：{error}", file=sys.stderr)

    print(
        "当前尚未接入本地线路检测、Globalping 辅助检测、"
        "正式准入和订阅发布，本次运行在来源处理后安全结束。"
    )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

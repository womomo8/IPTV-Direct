
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


def load_configuration(root: Path = ROOT) -> dict[str, Any]:
    """读取并检查四个根目录配置文件。"""

    for filename in CONFIG_FILES:
        if not (root / filename).is_file():
            raise ValueError(f"缺少配置文件：{filename}")

    try:
        channels = load_channels(str(root / "channels.txt"))
    except ChannelConfigError as exc:
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

    # 整轮共用，由 normalizer 负责 URL 归一和全局去重。
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

        # 总候选上限由 main 统一汇总和控制；
        # 单来源上限及 URL 去重由 normalizer 执行。
        source_candidate_count = min(
            normalized["extracted_candidates"],
            remaining,
        )

        candidates = normalized["candidates"][:remaining]

        total_candidates += source_candidate_count
        duplicate_candidates += normalized["duplicate_candidates"]

        matched_count = sum(
            1 for candidate in candidates
            if candidate.get("channel") is not None
        )

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

        # 有效来源必须至少含有一条匹配目标频道的候选。
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
        config = load_configuration()
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
        print(
            "测试运行流程尚未接入，"
            "本次不会下载真实来源或修改正式订阅。"
        )
        return 2

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

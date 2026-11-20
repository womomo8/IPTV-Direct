#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct 全链路唯一调度入口。

本模块只负责：读取配置、调用阶段程序、核对交接身份、汇总日志和收尾。
不在 main 中实现质量排序、正式准入、订阅生成或发布。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import collector
import checker
import globalping
import normalizer
import publisher

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("iptv_direct")


class PipelineError(RuntimeError):
    """阶段调用或阶段交接失败。"""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PipelineError(f"读取文件失败 {path.name}: {exc}") from exc


def load_runtime_config(root: Path = ROOT) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取正式配置；不在代码中复制 settings.json 的业务参数。"""
    try:
        settings = json.loads(_read_text(root / "settings.json"))
        sources = json.loads(_read_text(root / "sources.json"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"JSON 配置格式错误：{exc}") from exc
    if not isinstance(settings, dict) or not isinstance(sources, dict):
        raise PipelineError("settings.json 和 sources.json 顶层必须是 JSON 对象")
    for section in ("collection", "detection", "globalping", "selection"):
        if not isinstance(settings.get(section), dict):
            raise PipelineError(f"settings.json 缺少有效配置区段：{section}")
    return settings, sources


def _identity(candidate: dict[str, Any]) -> tuple[str, str, str | None]:
    """阶段交接身份：频道、规范化 URL、primary_source 的稳定标识。"""
    channel = candidate.get("channel")
    url = candidate.get("normalized_url")
    source = candidate.get("primary_source")
    if not isinstance(channel, str) or not channel:
        raise PipelineError("候选缺少标准频道身份")
    if not isinstance(url, str) or not url:
        raise PipelineError(f"候选 {channel} 缺少规范化 URL")
    if not isinstance(source, dict):
        raise PipelineError(f"候选 {channel} 缺少 primary_source")
    source_id = source.get("source_id") or source.get("source_url") or source.get("source_name")
    if not isinstance(source_id, str) or not source_id:
        raise PipelineError(f"候选 {channel} 的 primary_source 身份无效")
    return channel, url, source_id


def _assert_unique_ordered(candidates: list[dict[str, Any]], stage: str) -> list[tuple[str, str, str | None]]:
    identities = [_identity(item) for item in candidates]
    if len(identities) != len(set(identities)):
        raise PipelineError(f"{stage} 返回重复候选身份")
    return identities


def _verify_preserved(input_items: list[dict[str, Any]], output_items: list[dict[str, Any]], stage: str) -> None:
    """要求阶段保留输入身份和顺序；只允许 publisher 按契约筛选并重新组织队列。"""
    before = _assert_unique_ordered(input_items, f"{stage} 输入")
    after = _assert_unique_ordered(output_items, f"{stage} 输出")
    if before != after:
        raise PipelineError(f"{stage} 未按输入顺序完整返回候选，已阻止进入下一阶段")

def _verify_publisher_result(
    checked: list[dict[str, Any]],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise PipelineError("publisher 返回值不是对象")

    queue = result.get("queue")
    excluded = result.get("excluded")
    ranking = result.get("ranking")

    if (
        not isinstance(queue, list)
        or not isinstance(excluded, list)
        or not isinstance(ranking, list)
    ):
        raise PipelineError("publisher 未返回完整 queue/excluded/ranking")

    checked_id_list = _assert_unique_ordered(checked, "checker")
    checked_ids = set(checked_id_list)

    queue_ids = _assert_unique_ordered(queue, "publisher queue")
    if any(identity not in checked_ids for identity in queue_ids):
        raise PipelineError("publisher queue 返回了不属于 checker 输入的候选")

    excluded_ids = []
    for index, item in enumerate(excluded):
        if not isinstance(item, dict):
            raise PipelineError(f"publisher excluded 第 {index + 1} 条记录无效")

        candidate = item.get("candidate")
        if not isinstance(candidate, dict):
            raise PipelineError(f"publisher excluded 第 {index + 1} 条缺少候选")

        identity = _identity(candidate)
        declared_identity = item.get("identity")

        if (
            not isinstance(declared_identity, (tuple, list))
            or len(declared_identity) != 2
            or tuple(declared_identity) != identity[:2]
        ):
            raise PipelineError(f"publisher excluded 第 {index + 1} 条身份不一致")

        if not isinstance(item.get("reason"), str) or not item["reason"]:
            raise PipelineError(f"publisher excluded 第 {index + 1} 条缺少排除原因")

        excluded_ids.append(identity)

    if len(excluded_ids) != len(set(excluded_ids)):
        raise PipelineError("publisher excluded 存在重复候选身份")

    if any(identity not in checked_ids for identity in excluded_ids):
        raise PipelineError("publisher excluded 返回了不属于 checker 输入的候选")

    # queue 与 excluded 必须完整覆盖 checker 输入，不能遗漏或重复。
    combined_ids = queue_ids + excluded_ids
    if (
        len(combined_ids) != len(checked_id_list)
        or set(combined_ids) != checked_ids
    ):
        raise PipelineError("publisher queue 与 excluded 未完整覆盖 checker 输入")

    # publisher 可以按本地质量规则筛选并组织队列，但 main 不改变其顺序。
    if len(ranking) != len(queue):
        raise PipelineError("publisher ranking 与 queue 数量不一致")

    ranked_ids = []
    for index, item in enumerate(ranking):
        if not isinstance(item, dict):
            raise PipelineError("publisher ranking 存在无效记录")

        identity = item.get("identity")
        if not isinstance(identity, (tuple, list)) or len(identity) != 2:
            raise PipelineError(f"publisher ranking 第 {index + 1} 条身份无效")

        ranked_ids.append((identity[0], identity[1]))

    queue_channel_urls = [(item[0], item[1]) for item in queue_ids]
    if ranked_ids != queue_channel_urls:
        raise PipelineError("publisher ranking 与 queue 身份或顺序不一致")

    return queue

def run_pipeline(mode: str = "formal", root: Path = ROOT) -> dict[str, Any]:
    if mode not in {"formal", "test"}:
        raise ValueError("main.py 只支持 formal/test；诊断由独立诊断脚本执行")
    dry_run = mode == "test"
    settings, sources_config = load_runtime_config(root)

    LOG.info("运行模式：%s", "测试（不创建远程测量、不发布）" if dry_run else "正式")
    manual_text = _read_text(root / "manual.txt")
    channels = normalizer.load_channels(root / "channels.txt")

    # 1. collector：只采集来源内容，返回 main。
    prepared = collector.prepare_sources(manual_text, sources_config)
    source_results = list(collector.iter_source_contents(
        prepared["manual_sources"], prepared["automatic_sources"], settings
    ))
    valid_source_count = sum(bool(item.get("structure_valid")) for item in source_results)
    LOG.info(
        "采集完成：人工来源=%d，自动来源=%d，结构有效来源=%d，来源错误=%d",
        len(prepared["manual_sources"]), len(prepared["automatic_sources"]),
        valid_source_count, len(prepared.get("errors", [])) +
        sum(len(item.get("errors", [])) for item in source_results),
    )

    # 2. main 接收采集结果，再把每个有效来源交给 normalizer。
    collection = settings["collection"]
    detection = settings["detection"]
    aliases = settings.get("aliases", {})
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    source_history_by_url: dict[str, list[dict[str, Any]]] = {}
    channel_valid_sources = 0
    normalization_errors: list[str] = []
    duplicate_count = invalid_count = 0

    for record in source_results:
        downloaded = record.get("download")
        if not record.get("structure_valid") or not isinstance(downloaded, dict):
            continue
        source = dict(record.get("source") or {})
        source["final_url"] = downloaded.get("final_url") or source.get("url", "")
        source["content_type"] = downloaded.get("content_type", "")
        remaining = collection["max_candidates_total"] - len(candidates)
        if remaining <= 0:
            LOG.info("达到整轮候选上限，停止发现新候选")
            break
        normalized = normalizer.normalize_source_content(
            downloaded.get("content", b""),
            source,
            channels,
            collection["max_candidates_per_source"],
            seen_urls,
            content_type=downloaded.get("content_type", ""),
            max_hls_manifest_bytes=detection["max_hls_manifest_bytes"],
            aliases=aliases,
            max_candidates_remaining=remaining,
            source_history_by_url=source_history_by_url,
        )
        candidates.extend(normalized.get("candidates", []))
        channel_valid_sources += int(normalized.get("channel_valid_source_count", 0))
        duplicate_count += int(normalized.get("duplicate_candidates", 0))
        invalid_count += int(normalized.get("invalid_candidates", 0))
        normalization_errors.extend(normalized.get("errors", []))

    _assert_unique_ordered(candidates, "normalizer")
    LOG.info(
        "解析完成：候选=%d，频道有效来源=%d，重复=%d，无效 URL=%d，解析错误=%d",
        len(candidates), channel_valid_sources, duplicate_count, invalid_count,
        len(normalization_errors),
    )

    # 3. checker：本地检测，必须按输入顺序返回完整候选。
    checked = checker.check_candidates(candidates, detection)
    if not isinstance(checked, list) or len(checked) != len(candidates):
        raise PipelineError("checker 返回数量与候选池不一致")
    _verify_preserved(candidates, checked, "checker")
    local_pass_count = sum(checker.local_checks_passed(item) for item in checked)
    LOG.info("本地检测完成：输入=%d，本地适用项目全部 PASS=%d", len(checked), local_pass_count)

    # 4. publisher：唯一负责质量比较、分组和完整优选队列。
    prioritized = publisher.prioritize_candidates_for_globalping(checked, list(channels))
    queue = _verify_publisher_result(checked, prioritized)
    LOG.info("publisher 完成：优选队列=%d，排除=%d；main 不调整其顺序",
             len(queue), len(prioritized.get("excluded", [])))

    # 5. globalping：只接收 main 核对后的完整优选队列。
    remote_result = globalping.run_globalping(
        queue=queue,
        settings=settings,
        channels=list(channels),
        category_by_channel=dict(getattr(channels, "category_by_channel", {})),
        category_order=list(getattr(channels, "category_order", ())),
        dry_run=dry_run,
        output_dir=root / "output",
        local_pass_count=local_pass_count,
    )
    if not isinstance(remote_result, dict):
        raise PipelineError("globalping 返回值不是完整结果对象")

    returned_queue = remote_result.get("input_queue_identities")
    expected_queue = [(item[0], item[1]) for item in _assert_unique_ordered(queue, "globalping 输入")]
    if returned_queue is not None and [tuple(x) for x in returned_queue] != expected_queue:
        raise PipelineError("globalping 返回的输入队列身份或顺序与 publisher 不一致")

    stats = remote_result.get("stats", {})
    if isinstance(stats, dict):
        LOG.info("远程统计：%s", json.dumps(stats, ensure_ascii=False, sort_keys=True))
    LOG.info("运行结束：模式=%s，发布状态=%s", mode, remote_result.get("publication", {}).get("status", "未发布"))

    return {
        "mode": mode,
        "collection": {
            "source_records": len(source_results),
            "valid_sources": valid_source_count,
            "channel_valid_sources": channel_valid_sources,
            "candidates": len(candidates),
        },
        "local": {"checked": len(checked), "pass": local_pass_count},
        "publisher": {
            "queue": len(queue),
            "excluded": len(prioritized.get("excluded", [])),
        },
        "globalping": remote_result,
        "errors": {
            "collection": prepared.get("errors", []),
            "normalization": normalization_errors,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IPTV-Direct 全链路调度")
    parser.add_argument("--mode", choices=("formal", "test"), default="formal")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        result = run_pipeline(args.mode)
        publication = result.get("globalping", {}).get("publication", {})
        if args.mode == "formal" and publication.get("status") not in {"published", "unchanged"}:
            LOG.error("正式流程未完成发布：%s", publication.get("reason", "未知原因"))
            return 1
        return 0
    except Exception as exc:
                LOG.error("IPTV-Direct 流程失败，异常类型：%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())

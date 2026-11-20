
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
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import collector
import checker
import globalping
import normalizer
import publisher

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("iptv_direct")


def _safe_log_text(value: Any) -> str:
    """日志脱敏：隐藏完整 URL、Bearer Token 和敏感参数。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ")

    def hide_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,;)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        try:
            host = urlsplit(raw).hostname or "URL"
        except ValueError:
            host = "URL"
        return host + suffix

    text = re.sub(r"https?://[^\s<>\"']+", hide_url, text, flags=re.I)
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|api[_-]?key|authorization)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[已隐藏]",
        text,
    )
    return text[:500]


def _safe_log_host(value: Any) -> str:
    """只记录主机名，不记录完整 URL 和参数。"""
    try:
        return urlsplit(str(value or "")).hostname or "无法识别"
    except ValueError:
        return "无法识别"


def _source_label(source: dict[str, Any]) -> str:
    """优先使用来源名称或 ID，不输出来源 URL。"""
    return _safe_log_text(
        source.get("name") or source.get("id") or "未命名来源"
    )


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
def _verify_globalping_result(
    queue: list[dict[str, Any]],
    remote_result: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """核对 Globalping 是否完整、按序返回候选及最终准入结果。"""
    if not isinstance(remote_result, dict):
        raise PipelineError("globalping 返回值不是完整结果对象")

    expected_queue = [
        (item[0], item[1])
        for item in _assert_unique_ordered(queue, "globalping 输入")
    ]

    returned_queue = remote_result.get("input_queue_identities")
    if not isinstance(returned_queue, list):
        raise PipelineError("globalping 未返回有效的输入队列身份")

    returned_identities = []
    for index, identity in enumerate(returned_queue):
        if (
            not isinstance(identity, (list, tuple))
            or len(identity) != 2
            or not all(isinstance(value, str) and value for value in identity)
        ):
            raise PipelineError(
                f"globalping 输入队列身份第 {index + 1} 条无效"
            )
        returned_identities.append(tuple(identity))

    if returned_identities != expected_queue:
        raise PipelineError(
            "globalping 返回的输入队列身份或顺序与 publisher 不一致"
        )

    stats = remote_result.get("stats")
    if not isinstance(stats, dict):
        raise PipelineError("globalping 未返回有效统计对象")

    results = remote_result.get("results")
    if not isinstance(results, list):
        raise PipelineError("globalping 未返回有效候选结果列表")

    accepted = remote_result.get("accepted")
    if not isinstance(accepted, list):
        raise PipelineError("globalping 未返回有效正式准入列表")

    publication = remote_result.get("publication")
    if (
        not isinstance(publication, dict)
        or not isinstance(publication.get("status"), str)
        or not publication["status"]
    ):
        raise PipelineError("globalping 未返回有效发布状态")

    preflight_blocked = bool(stats.get("preflight_failed"))
    no_probes = bool(stats.get("no_probes"))

    if preflight_blocked and no_probes:
        raise PipelineError("globalping 返回了互相冲突的预检阻断状态")

    if preflight_blocked or no_probes:
        if results or accepted:
            raise PipelineError(
                "globalping 整轮预检阻断却返回了候选处理或准入结果"
            )
        if publication["status"] != "blocked":
            raise PipelineError(
                "globalping 预检阻断的发布状态必须为 blocked"
            )
        return stats, results, publication

    if len(results) != len(expected_queue):
        raise PipelineError(
            "globalping 候选结果数量与输入队列不一致"
        )

    allowed_admissions = {
        "NOT_RUN",
        "SKIPPED_CHANNEL_FULL",
        "REJECTED_LOCAL",
        "INCOMPLETE",
        "REJECTED_REMOTE",
        "ADMITTED",
    }

    admitted_identities = []

    for index, record in enumerate(results):
        if not isinstance(record, dict):
            raise PipelineError(
                f"globalping 候选结果第 {index + 1} 条不是对象"
            )

        identity = record.get("identity")
        if (
            not isinstance(identity, (list, tuple))
            or len(identity) != 2
            or not all(isinstance(value, str) and value for value in identity)
        ):
            raise PipelineError(
                f"globalping 候选结果第 {index + 1} 条身份无效"
            )

        result_identity = tuple(identity)
        if result_identity != expected_queue[index]:
            raise PipelineError(
                f"globalping 候选结果第 {index + 1} 条身份或顺序错误"
            )

        admission = record.get("admission")
        if (
            not isinstance(admission, str)
            or admission not in allowed_admissions
        ):
            raise PipelineError(
                f"globalping 候选结果第 {index + 1} 条准入状态无效"
            )

        if admission == "ADMITTED":
            admitted_identities.append(result_identity)

    accepted_identities = []
    for index, item in enumerate(accepted):
        if not isinstance(item, dict):
            raise PipelineError(
                f"globalping 正式准入第 {index + 1} 条不是对象"
            )

        channel = item.get("channel")
        url = item.get("normalized_url")
        if (
            not isinstance(channel, str)
            or not channel
            or not isinstance(url, str)
            or not url
        ):
            raise PipelineError(
                f"globalping 正式准入第 {index + 1} 条身份无效"
            )

        accepted_identities.append((channel, url))

    if (
        len(accepted_identities) != len(set(accepted_identities))
        or accepted_identities != admitted_identities
    ):
        raise PipelineError(
            "globalping accepted 与逐条 ADMITTED 结果不一致"
        )

    return stats, results, publication

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

            "source_records": source_record_count,

    _assert_unique_ordered(candidates, "normalizer")
    LOG.info(
        "解析汇总：候选=%d，频道有效来源=%d，"
        "重复=%d，无效 URL=%d，解析错误=%d",
        len(candidates),
        channel_valid_sources,
        duplicate_count,
        invalid_count,
        len(normalization_errors),
    )

    # 3. checker：本地检测，必须按输入顺序返回完整候选。
    checked = checker.check_candidates(candidates, detection)
    if not isinstance(checked, list) or len(checked) != len(candidates):
        raise PipelineError("checker 返回数量与候选池不一致")
    _verify_preserved(candidates, checked, "checker")
    local_pass_count = sum(
        checker.local_checks_passed(item) for item in checked
    )

    for item in checked:
        checks = item.get("checks", {})
        if not isinstance(checks, dict):
            checks = {}

        check_summary = {}
        for name in ("ipv4", "http", "hls", "ts"):
            check = checks.get(name, {})
            if not isinstance(check, dict):
                check = {}
            check_summary[name] = {
                "status": check.get("status", "缺少结果"),
                "reason": _safe_log_text(
                    check.get("reason", "未提供原因")
                ),
            }

        LOG.info(
            "本地检测明细：频道=%s，主机=%s，媒体类型=%s，"
            "全部适用项目通过=%s，检测结果=%s",
            _safe_log_text(item.get("channel", "未知频道")),
            _safe_log_host(item.get("normalized_url")),
            item.get("media_type", "UNKNOWN"),
            checker.local_checks_passed(item),
            json.dumps(
                check_summary,
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    LOG.info(
        "本地检测汇总：输入=%d，本地适用项目全部 PASS=%d，"
        "未全部 PASS=%d",
        len(checked),
        local_pass_count,
        len(checked) - local_pass_count,
    )

    # 4. publisher：唯一负责质量比较、分组和完整优选队列。
    prioritized = publisher.prioritize_candidates_for_globalping(checked, list(channels))
    queue = _verify_publisher_result(checked, prioritized)

    for item in prioritized.get("ranking", []):
        identity = item.get("identity", ())
        channel = (
            _safe_log_text(identity[0])
            if isinstance(identity, (tuple, list)) and identity
            else "未知频道"
        )
        group = item.get("group", {})
        if not isinstance(group, dict):
            group = {}

        LOG.info(
            "优选排序：顺序=%s，原始位置=%s，频道=%s，媒体类型=%s，"
            "本地状态=%s，质量排序依据=%s",
            item.get("priority_order"),
            item.get("original_index"),
            channel,
            group.get("media_type", "未知"),
            json.dumps(
                item.get("local_status", {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(
                item.get("quality_key", {}),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )

    for item in prioritized.get("excluded", []):
        candidate = item.get("candidate", {})
        if not isinstance(candidate, dict):
            candidate = {}

        LOG.warning(
            "本地优选排除：频道=%s，媒体类型=%s，原因=%s",
            _safe_log_text(candidate.get("channel", "未知频道")),
            candidate.get("media_type", "未知"),
            _safe_log_text(item.get("reason", "未提供原因")),
        )

    LOG.info(
        "publisher 汇总：输入=%d，优选队列=%d，排除=%d；"
        "main 保留 publisher 原始顺序",
        len(checked),
        len(queue),
        len(prioritized.get("excluded", [])),
    )

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
    stats, results, publication = _verify_globalping_result(
        queue,
        remote_result,
    )

    if isinstance(stats, dict):
        LOG.info(
            "Globalping 统计：%s",
            json.dumps(stats, ensure_ascii=False, sort_keys=True),
        )

    for record in remote_result.get("results", []):
        if not isinstance(record, dict):
            LOG.warning("Globalping 返回了无效候选记录")
            continue

        identity = record.get("identity", [])
        channel = (
            _safe_log_text(identity[0])
            if isinstance(identity, (list, tuple)) and identity
            else "未知频道"
        )

        remote = record.get("remote", {})
        if not isinstance(remote, dict):
            remote = {}

        measurements = remote.get("measurements", [])
        measurement_summary = [
            {
                "phase": item.get("phase"),
                "id": item.get("id"),
                "probes_count": item.get("probes_count"),
            }
            for item in measurements
            if isinstance(item, dict)
        ] if isinstance(measurements, list) else []

        remote_reasons = remote.get("reasons", [])
        if not isinstance(remote_reasons, list):
            remote_reasons = []

        reason = _safe_log_text(record.get("reason", "无"))
        if remote_reasons:
            reason += "；" + _safe_log_text("；".join(
                str(item) for item in remote_reasons
            ))

        LOG.info(
            "远程候选结果：优先级=%s，频道=%s，准入=%s，"
            "总体状态=%s，检测状态=%s，测量阶段=%s，原因=%s",
            record.get("priority_order"),
            channel,
            record.get("admission", "NOT_RUN"),
            remote.get("overall", "NOT_RUN"),
            json.dumps(
                remote.get("checks", {}),
                ensure_ascii=False,
                sort_keys=True,
            ),
            json.dumps(
                measurement_summary,
                ensure_ascii=False,
                sort_keys=True,
            ),
            reason,
        )

    LOG.info(
        "运行结束：模式=%s，发布状态=%s，原因=%s",
        mode,
        publication.get("status", "未发布"),
        _safe_log_text(publication.get("reason", "未提供")),
    )

    return {
        "mode": mode,
        "collection": {
                        "source_records": source_record_count,
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
            "collection": collection_errors,
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
            LOG.error(
                "正式流程未完成发布：%s",
                _safe_log_text(publication.get("reason", "未知原因")),
            )
            return 1
        return 0
    except Exception as exc:
        LOG.error(
            "IPTV-Direct 流程失败：异常类型=%s，原因=%s",
            type(exc).__name__,
            _safe_log_text(exc),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

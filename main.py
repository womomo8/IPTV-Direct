#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct 总入口：采集、检测、线路准入及订阅生成。"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import checker
import globalping
import publisher

from collector import (
    SourceConfigError,
    SourceDownloadError,
    download_url,
    iter_source_contents,
    prepare_sources,
)
from normalizer import (
    ChannelConfigError,
    load_channels,
    normalize_source_content,
    normalize_url,
)

ROOT = Path(__file__).resolve().parent
CONFIG_FILES = ("channels.txt", "manual.txt", "sources.json", "settings.json")

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
NA = "N/A"
TEST_CHANNEL = "IPTV-Direct 模拟测试频道"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path.name}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"配置文件不是有效 UTF-8 文本：{path.name}") from exc
    except OSError as exc:
        raise ValueError(f"读取配置文件失败：{path.name}：{exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 格式错误：{path.name}，第 {exc.lineno} 行：{exc.msg}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError(f"配置文件顶层必须是 JSON 对象：{path.name}")
    return value


def _check_settings(settings: dict[str, Any]) -> None:
    required = {
        "collection": (
            "max_valid_sources",
            "max_candidates_per_source",
            "max_candidates_total",
            "max_index_links_per_page",
        ),
        "detection": (
            "local_concurrency",
            "per_host_concurrency",
            "queue_limit",
            "max_queue_seconds",
            "candidate_active_budget_seconds",
            "source_download_timeout_seconds",
            "max_source_download_bytes",
            "max_hls_manifest_bytes",
            "max_ts_read_bytes",
            "max_redirects",
        ),
        "globalping": ("max_probes",),
        "selection": ("max_routes_per_channel",),
        "schedule": ("timezone", "daily_time"),
    }

    for section, keys in required.items():
        data = settings.get(section)
        if not isinstance(data, dict):
            raise ValueError(f"settings.json 缺少有效配置区段：{section}")

        for key in keys:
            value = data.get(key)

            if key in ("timezone", "daily_time"):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"settings.json 参数 {section}.{key} 必须是非空文本"
                    )
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(
                    f"settings.json 参数 {section}.{key} 必须是正整数"
                )

    if settings["schedule"]["timezone"] != "Asia/Shanghai":
        raise ValueError("settings.json schedule.timezone 必须为 Asia/Shanghai")

    clock = settings["schedule"]["daily_time"]

    try:
        hour, minute = (int(part) for part in clock.split(":"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        ) from exc

    if len(clock) != 5 or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("settings.json schedule.daily_time 不是有效 HH:MM 时间")


def load_configuration(
    root: Path = ROOT,
    test_mode: bool = False,
) -> dict[str, Any]:
    for name in CONFIG_FILES:
        if not (root / name).is_file():
            raise ValueError(f"缺少配置文件：{name}")

    try:
        channels = load_channels(str(root / "channels.txt"))
    except ChannelConfigError as exc:
        if test_mode and str(exc) == "channels.txt 没有配置目标频道":
            channels = []
        else:
            raise ValueError(str(exc)) from exc

    try:
        prepared = prepare_sources(
            _read_text(root / "manual.txt"),
            _read_json(root / "sources.json"),
        )
    except SourceConfigError as exc:
        raise ValueError(str(exc)) from exc

    settings = _read_json(root / "settings.json")
    _check_settings(settings)

    return {
        "channels": channels,
        **prepared,
        "settings": settings,
    }


# ----------------------------------------------------------------------
# 本地 checker 与 Globalping 检测交接
# ----------------------------------------------------------------------

def _local_pass(candidate: dict[str, Any]) -> bool:
    checks = candidate.get("checks")

    if not isinstance(checks, dict):
        return False

    media_type = candidate.get("media_type")
    required = ("ipv4", "http", "ts")

    if media_type == "HLS":
        required += ("hls",)
    elif media_type == "DIRECT_TS":
        hls_check = checks.get("hls")

        if not isinstance(hls_check, dict) or hls_check.get("status") != NA:
            return False
    else:
        return False

    return all(
        isinstance(checks.get(key), dict)
        and checks[key].get("status") == PASS
        for key in required
    )


def _identity(item: dict[str, Any]) -> tuple[Any, Any]:
    return item.get("channel"), item.get("normalized_url")


def _remote_incomplete(
    candidate: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    media_type = candidate.get("media_type")

    return {
        **candidate,
        "globalping": {
            "status": INCOMPLETE,
            "checks": {
                "ipv4": {"status": INCOMPLETE, "reason": reason},
                "http": {"status": INCOMPLETE, "reason": reason},
                "hls": (
                    {"status": NA, "reason": "直接 TS 不适用 HLS 检测。"}
                    if media_type == "DIRECT_TS"
                    else {"status": INCOMPLETE, "reason": reason}
                ),
                "ts": {"status": INCOMPLETE, "reason": reason},
            },
            "stages": {},
            "probes": {},
            "measurements": [],
            "error": reason,
        },
    }


def run_detection_pipeline(
    candidates: list[dict[str, Any]],
    settings: dict[str, Any],
    local_check=checker.check_candidates,
    remote_check=globalping.check_candidates,
) -> list[dict[str, Any]]:
    """本地检测全部适用项目通过后，才提交 Globalping。"""

    if not candidates:
        return []

    originals = [_identity(item) for item in candidates]

    try:
        local_results = local_check(candidates, settings["detection"])

        if (
            not isinstance(local_results, list)
            or len(local_results) != len(candidates)
        ):
            raise ValueError("checker 返回数量与输入候选数量不一致")

        if any(
            not isinstance(item, dict)
            or _identity(item) != originals[index]
            for index, item in enumerate(local_results)
        ):
            raise ValueError("checker 返回顺序或候选身份不匹配")

    except Exception as exc:
        reason = f"本地检测调用异常或结果校验失败：{exc}"

        local_results = [
            {
                **item,
                "media_type": "UNKNOWN",
                "checks": {
                    key: {"status": INCOMPLETE, "reason": reason}
                    for key in ("ipv4", "http", "hls", "ts")
                },
            }
            for item in candidates
        ]

    eligible = []
    output: list[dict[str, Any] | None] = [None] * len(local_results)

    for index, item in enumerate(local_results):
        if _local_pass(item):
            eligible.append(item)
        else:
            output[index] = _remote_incomplete(
                item,
                "本地检测未全部通过，未提交 Globalping；不代表远程检测失败。",
            )

    if eligible:
        try:
            remote_results = remote_check(eligible, settings["globalping"])

            if (
                not isinstance(remote_results, list)
                or len(remote_results) != len(eligible)
            ):
                raise ValueError("Globalping 返回数量与输入候选数量不一致")

            for position, result in enumerate(remote_results):
                if (
                    not isinstance(result, dict)
                    or _identity(result) != _identity(eligible[position])
                ):
                    raise ValueError("Globalping 返回顺序或候选身份不匹配")

                remote = result.get("globalping")

                if (
                    not isinstance(remote, dict)
                    or remote.get("status") not in (PASS, FAIL, INCOMPLETE)
                ):
                    raise ValueError("Globalping 缺少有效总体状态")

                original_index = next(
                    index
                    for index, item in enumerate(local_results)
                    if _identity(item) == _identity(eligible[position])
                )

                output[original_index] = result

        except Exception as exc:
            reason = f"Globalping 调用异常或结果校验失败：{exc}"

            for item in eligible:
                original_index = next(
                    index
                    for index, local in enumerate(local_results)
                    if _identity(local) == _identity(item)
                )

                output[original_index] = _remote_incomplete(item, reason)

    return [
        item
        if item is not None
        else _remote_incomplete(
            local_results[index],
            "检测结果未能完成交接。",
        )
        for index, item in enumerate(output)
    ]


def _print_detection_summary(results: list[dict[str, Any]]) -> None:
    """输出本地检测统计，以及真正提交给 Globalping 的检测明细。"""

    local_ok = sum(_local_pass(item) for item in results)
    local_bad = len(results) - local_ok

    not_submitted_reason = "本地检测未全部通过，未提交 Globalping"

    submitted_items = []
    not_submitted = 0

    for item in results:
        remote = item.get("globalping", {})
        error = remote.get("error", "") if isinstance(remote, dict) else ""

        if isinstance(error, str) and error.startswith(not_submitted_reason):
            not_submitted += 1
        else:
            submitted_items.append(item)

    remote_counts = {
        state: 0 for state in (PASS, FAIL, INCOMPLETE)
    }

    for item in submitted_items:
        remote = item.get("globalping", {})
        if not isinstance(remote, dict):
            continue

        state = remote.get("status")
        if state in remote_counts:
            remote_counts[state] += 1

    print(
        f"检测统计：候选 {len(results)} 条；"
        f"本地全部通过 {local_ok} 条；"
        f"本地未通过/未完成 {local_bad} 条；"
        f"Globalping 已提交 {len(submitted_items)} 条、"
        f"未提交 {not_submitted} 条；"
        f"实际远程结果 PASS {remote_counts[PASS]} 条、"
        f"FAIL {remote_counts[FAIL]} 条、"
        f"INCOMPLETE {remote_counts[INCOMPLETE]} 条。"
    )

    for index, item in enumerate(submitted_items, 1):
        remote = item.get("globalping", {})
        if not isinstance(remote, dict):
            continue

        channel = item.get("channel", "未知频道")
        media_type = item.get("media_type", "未知类型")
        url = item.get("normalized_url", "")

        try:
            host = urlsplit(str(url)).hostname or "无法识别"
        except ValueError:
            host = "无法识别"

        print(
            f"Globalping 明细 {index}/{len(submitted_items)}："
            f"频道 {channel}；类型 {media_type}；"
            f"目标主机 {host}；"
            f"总体状态 {remote.get('status', '未知')}。"
        )

        probes = remote.get("probes", {})
        if isinstance(probes, dict) and probes:
            for probe in probes.values():
                if not isinstance(probe, dict):
                    continue

                print(
                    "  探针："
                    f"{probe.get('country', '未知地区')} "
                    f"{probe.get('city', '未知城市')}；"
                    f"网络 {probe.get('network', '未知网络')}；"
                    f"ASN {probe.get('asn', '未知')}。"
                )
        else:
            print("  探针：没有取得有效探针结果。")

        stages = remote.get("stages", {})
        if isinstance(stages, dict) and stages:
            for stage_name, stage in stages.items():
                if not isinstance(stage, dict):
                    continue

                measurement_id = stage.get("measurement_id") or "无"
                reason = stage.get("reason") or "没有提供原因"

                print(
                    f"  阶段 {stage_name}："
                    f"{stage.get('status', '未知')}；"
                    f"测量ID {measurement_id}；"
                    f"原因：{reason}"
                )
        else:
            print("  阶段：没有取得阶段记录。")

        error = remote.get("error")
        if error:
            print(f"  远程异常/未完成原因：{error}")



# ----------------------------------------------------------------------
# publisher 交接与停止条件
# ----------------------------------------------------------------------

def process_admission_batch(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    channels: list[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """将本批完整检测结果交给 publisher 执行正式准入。"""

    return publisher.process_batch(
        candidates=candidates,
        accepted_routes=accepted_routes,
        channels=channels,
        settings=settings,
    )


def _collection_stop_reason(
    valid_sources: int,
    extracted_total: int,
    admission: dict[str, Any],
    limits: dict[str, Any],
) -> str | None:
    """依据正式参数和 publisher 返回的频道统计判断是否停止。"""

    if valid_sources >= limits["max_valid_sources"]:
        return f"达到有效来源上限 {limits['max_valid_sources']}"

    if extracted_total >= limits["max_candidates_total"]:
        return f"达到全部候选总上限 {limits['max_candidates_total']}"

    if admission.get("all_channels_reached"):
        return "所有目标频道均已达到每频道 2 条有效线路目标"

    return None


# ----------------------------------------------------------------------
# 正式流程：逐来源采集、检测、准入和订阅生成
# ----------------------------------------------------------------------

def run_formal_flow(config: dict[str, Any]) -> int:
    settings = config["settings"]
    limits = settings["collection"]
    channels = config["channels"]

    seen_urls: set[str] = set()
    errors: list[str] = []
    detected: list[dict[str, Any]] = []

    accepted_routes: list[dict[str, Any]] = []
    admission = {
        "accepted_routes": [],
        "channel_counts": {channel: 0 for channel in channels},
        "all_channels_reached": False,
    }

    valid_sources = 0
    extracted_total = 0
    duplicate_total = 0
    stopped = "所有配置来源均已处理"
    system_error = False

    stream = iter_source_contents(
        config["manual_sources"],
        config["automatic_sources"],
        settings,
    )

    for item in stream:
        source = item["source"]
        label = source.get("name", source.get("url", "来源"))

        if item.get("errors"):
            errors.extend(
                f"{label}：{message}" for message in item["errors"]
            )
            continue

        downloaded = item.get("download")

        if not isinstance(downloaded, dict):
            errors.append(f"{label}：采集器未返回下载内容")
            continue

        remaining = limits["max_candidates_total"] - extracted_total

        if remaining <= 0:
            stopped = f"达到全部候选总上限 {limits['max_candidates_total']}"
            break

        source_info = {
            **source,
            "final_url": downloaded["final_url"],
        }

        try:
            normalized = normalize_source_content(
                content=downloaded["content"],
                source=source_info,
                channels=channels,
                max_candidates_per_source=limits["max_candidates_per_source"],
                seen_urls=seen_urls,
                content_type=downloaded["content_type"],
                max_hls_manifest_bytes=(
                    settings["detection"]["max_hls_manifest_bytes"]
                ),
                max_candidates_remaining=remaining,
            )
        except Exception as exc:
            errors.append(f"{label}：归一解析异常：{exc}")
            continue

        if not normalized.get("recognized"):
            errors.extend(
                f"{label}：{error}"
                for error in normalized.get("errors", [])
            )
            continue

        extracted_total += normalized["extracted_candidates"]
        duplicate_total += normalized["duplicate_candidates"]

        candidates = normalized["candidates"]

        if normalized.get("matched_target_candidates", 0) > 0:
            valid_sources += 1

        accepted_now = 0
        rejected_now = 0

        if candidates:
            batch_results = run_detection_pipeline(candidates, settings)
            detected.extend(batch_results)
            _print_detection_summary(batch_results)

            try:
                admission = process_admission_batch(
                    candidates=batch_results,
                    accepted_routes=accepted_routes,
                    channels=channels,
                    settings=settings,
                )
            except publisher.PublisherError as exc:
                errors.append(f"线路准入系统错误：{exc}")
                stopped = "publisher 正式准入发生系统错误"
                system_error = True
                break

            accepted_routes = admission["accepted_routes"]

            accepted_now = sum(
                result.get("status") == publisher.ACCEPTED
                for result in admission["results"]
            )
            rejected_now = sum(
                result.get("status") == publisher.REJECTED
                for result in admission["results"]
            )

            print(
                f"线路准入：本批准入 {accepted_now} 条；"
                f"本批拒绝 {rejected_now} 条；"
                f"累计保留 {len(accepted_routes)} 条。"
            )

            print(
                "频道有效线路统计："
                + "；".join(
                    f"{channel} {count}/2"
                    for channel, count in admission["channel_counts"].items()
                )
            )

        print(
            f"来源处理：{label}；"
            f"识别格式 {normalized['format']}；"
            f"提取候选 {normalized['extracted_candidates']}；"
            f"本批去重 {normalized['duplicate_candidates']}；"
            f"进入检测 {len(candidates)}。"
        )

        stop_reason = _collection_stop_reason(
            valid_sources,
            extracted_total,
            admission,
            limits,
        )

        if stop_reason:
            stopped = stop_reason
            break

    print(
        f"来源汇总：有效来源 {valid_sources}；"
        f"累计提取候选 {extracted_total}；"
        f"重复候选 {duplicate_total}；"
        f"实际完成检测 {len(detected)}。"
    )
    print(f"采集停止原因：{stopped}")

    for error in errors:
        print(f"来源警告：{error}", file=sys.stderr)

    if detected:
        _print_detection_summary(detected)

    if system_error:
        print(
            "发生准入系统错误，停止后续处理；"
            "正式订阅未修改。",
            file=sys.stderr,
        )
        return 1

    # 第8阶段：生成并检查订阅内容。
    try:
        subscriptions = publisher.build_subscriptions(
            accepted_routes,
            channels,
        )
    except publisher.PublisherError as exc:
        print(
            f"订阅生成或格式检查失败：{exc}",
            file=sys.stderr,
        )
        print("正式订阅未修改。", file=sys.stderr)
        return 1

    print(
        f"订阅内容生成并校验通过："
        f"线路 {subscriptions['route_count']} 条；"
        f"频道 {sum(count > 0 for count in subscriptions['channel_counts'].values())} 个。"
    )

    print(
        "本轮频道统计："
        + "；".join(
            f"{channel} {count}/2"
            for channel, count in subscriptions["channel_counts"].items()
        )
    )

    print(
        "第8阶段处理完成：已执行正式准入、频道统计、"
        "M3U/TXT 生成及一致性检查。"
    )

    # 第9阶段：执行正式订阅安全发布。
    try:
        publish_result = publisher.publish_subscriptions(
            subscriptions,
            channels,
            output_dir=ROOT / "output",
        )
    except Exception as exc:
        print(
            f"第9阶段正式订阅发布失败：{exc}",
            file=sys.stderr,
        )
        print(
            "请检查发布保护和回滚结果；"
            "本轮不能视为发布成功。",
            file=sys.stderr,
        )
        return 1

    print(
        "第9阶段正式订阅发布成功："
        f"频道 {publish_result['channel_count']} 个；"
        f"线路 {publish_result['route_count']} 条。"
    )
    print(f"M3U 文件：{publish_result['m3u_path']}")
    print(f"TXT 文件：{publish_result['txt_path']}")

    if publish_result["first_publish"]:
        print("本次属于首次发布。")

    return 0

# ----------------------------------------------------------------------
# 阶段4：采集、下载、解析、索引专项测试
# ----------------------------------------------------------------------

def _run_stage4_collection_tests(config: dict[str, Any]) -> None:
    """本机模拟 HTTP 服务验收来源下载、解析和索引处理。"""

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def make_response(
        body: str | bytes,
        content_type: str = "text/plain; charset=utf-8",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ):
        data = body.encode("utf-8") if isinstance(body, str) else body
        return status, content_type, data, headers or {}

    playlist = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="IPTV-Direct 模拟测试频道",'
        "IPTV-Direct 模拟测试频道\n"
        "http://example.invalid/live/test.ts\n"
    )

    txt = (
        "IPTV-Direct 模拟测试频道,"
        "http://example.invalid/live/txt.ts\n"
    )

    hls = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXTINF:10,\n"
        "segment.ts\n"
        "#EXT-X-ENDLIST\n"
    )

    routes: dict[str, tuple] = {
        "/bad.m3u": make_response("服务暂时不可用", status=503),
        "/playlist.m3u": make_response(playlist, "audio/x-mpegurl"),
        "/channels.txt": make_response(txt),
        "/redirect.txt": make_response(
            b"", status=302, headers={"Location": "/channels.txt"}
        ),
        "/partial.txt": make_response(
            txt,
            status=206,
            headers={"Content-Range": "bytes 0-71/72"},
        ),
        "/live.m3u8": make_response(hls, "audio/x-mpegurl"),
        "/child.m3u": make_response(
            '<html><a href="sub/hidden.m3u">hidden</a></html>',
            "text/html; charset=utf-8",
        ),
        "/large.txt": make_response("X" * 128),
        "/oversized-hls.m3u8": make_response(
            "#EXTM3U\n#EXT-X-VERSION:3\n" + ("X" * 96),
            "audio/x-mpegurl",
        ),
    }

    links = [
        "sub/one.m3u",
        "sub/one.m3u#duplicate",
        "sub/two.txt",
        "child.m3u",
    ]

    links.extend(
        f"sub/extra{number:02d}.m3u"
        for number in range(38)
    )

    links.extend(("child.html", "ftp://example.invalid/x.m3u"))

    routes["/index.html"] = make_response(
        "<html>"
        + "".join(
            f'<a href="{link}">{link}</a>'
            for link in links
        )
        + "</html>",
        "text/html; charset=utf-8",
    )

    class Handler(BaseHTTPRequestHandler):
        route_table = routes
        request_counts: dict[str, int] = {}

        def do_GET(self):
            path = urlsplit(self.path).path

            self.request_counts[path] = (
                self.request_counts.get(path, 0) + 1
            )

            item = self.route_table.get(path)

            if item is None and path.startswith("/sub/"):
                item = make_response(playlist, "audio/x-mpegurl")

            if item is None:
                item = make_response("Not found", status=404)

            status, content_type, body, headers = item

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))

            for name, value in headers.items():
                self.send_header(name, value)

            self.end_headers()

            if body:
                self.wfile.write(body)

        def log_message(self, format_string, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    base = f"http://127.0.0.1:{server.server_port}"

    try:
        settings = copy.deepcopy(config["settings"])

        require(
            settings["collection"]["max_index_links_per_page"] == 40,
            "索引链接上限必须与 MASTER_PLAN.md 的40条标准一致",
        )

        # M3U 下载、MIME识别、频道解析。
        m3u_url = f"{base}/playlist.m3u"

        m3u = download_url(
            m3u_url,
            10,
            1048576,
            5,
            1048576,
        )

        parsed = normalize_source_content(
            content=m3u["content"],
            source={
                "url": m3u_url,
                "final_url": m3u["final_url"],
            },
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=m3u["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        require(
            m3u["content_type"] == "audio/x-mpegurl"
            and parsed["recognized"]
            and parsed["format"] == "m3u"
            and len(parsed["candidates"]) == 1
            and parsed["candidates"][0]["channel"] == TEST_CHANNEL,
            "M3U 下载或 MIME/频道解析失败",
        )

        print("阶段4测试：M3U 下载、MIME识别和频道匹配 PASS")

        # TXT 下载和频道解析。
        txt_url = f"{base}/channels.txt"

        txt_download = download_url(
            txt_url,
            10,
            1048576,
            5,
            1048576,
        )

        txt_parsed = normalize_source_content(
            content=txt_download["content"],
            source={
                "url": txt_url,
                "final_url": txt_download["final_url"],
            },
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=txt_download["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        require(
            txt_parsed["recognized"]
            and txt_parsed["format"] == "txt"
            and len(txt_parsed["candidates"]) == 1
            and txt_parsed["candidates"][0]["channel"] == TEST_CHANNEL,
            "TXT 下载或频道解析失败",
        )

        print("阶段4测试：TXT 下载和频道解析 PASS")

        # HLS 清单识别。
        hls_url = f"{base}/live.m3u8"

        hls_download = download_url(
            hls_url,
            10,
            1048576,
            5,
            1048576,
        )

        hls_parsed = normalize_source_content(
            content=hls_download["content"],
            source={
                "url": hls_url,
                "final_url": hls_download["final_url"],
            },
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=hls_download["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        require(
            hls_parsed["recognized"]
            and hls_parsed["format"] == "hls"
            and hls_parsed["extracted_candidates"] == 0
            and hls_parsed["matched_target_candidates"] == 0
            and hls_parsed["retained_candidates"] == 0
            and hls_parsed["candidates"] == [],
            "HLS 清单识别或频道处理错误",
        )

        print("阶段4测试：HLS M3U8 清单识别 PASS")

        manual_sources = [
            {
                "name": "失败人工来源",
                "url": f"{base}/bad.m3u",
                "source_type": "manual",
                "priority": 0,
                "order": 0,
            },
            {
                "name": "人工 M3U",
                "url": m3u_url,
                "source_type": "manual",
                "priority": 0,
                "order": 1,
            },
            {
                "name": "重定向 TXT",
                "url": f"{base}/redirect.txt",
                "source_type": "manual",
                "priority": 0,
                "order": 2,
            },
            {
                "name": "HTTP 206 TXT",
                "url": f"{base}/partial.txt",
                "source_type": "manual",
                "priority": 0,
                "order": 3,
            },
        ]

        automatic_sources = [
            {
                "name": "自动 playlist",
                "url": hls_url,
                "source_type": "automatic",
                "mode": "playlist",
                "priority": 1,
                "order": 4,
            },
            {
                "name": "自动 index",
                "url": f"{base}/index.html",
                "source_type": "automatic",
                "mode": "index",
                "priority": 1,
                "order": 5,
            },
        ]

        items = list(
            iter_source_contents(
                manual_sources,
                automatic_sources,
                settings,
            )
        )

        require(
            len(items) == 45,
            f"采集条数错误：预期45，实际{len(items)}",
        )

        require(
            [item["source"]["url"] for item in items[:4]]
            == [source["url"] for source in manual_sources],
            "人工来源没有优先按配置顺序处理",
        )

        require(
            items[0]["download"] is None and items[0]["errors"],
            "503 来源没有被隔离为单来源错误",
        )

        require(
            items[1]["download"] is not None
            and not items[1]["errors"],
            "首条来源失败后没有继续处理后续人工来源",
        )

        require(
            items[2]["download"] is not None
            and not items[2]["errors"]
            and items[2]["download"]["final_url"] == txt_url,
            "HTTP 重定向 final_url 不正确",
        )

        require(
            items[3]["download"] is not None
            and not items[3]["errors"]
            and items[3]["download"]["http_status"] == 206,
            "HTTP 206 未被接受",
        )

        require(
            items[4]["source"]["url"] == hls_url
            and items[4]["download"] is not None
            and not items[4]["errors"],
            "自动 playlist 模式下载失败",
        )

        require(
            all(
                item["source"]["source_type"] == "automatic"
                for item in items[5:]
            ),
            "自动来源处理顺序错误",
        )

        redirected = normalize_source_content(
            content=items[2]["download"]["content"],
            source={
                **items[2]["source"],
                "final_url": items[2]["download"]["final_url"],
            },
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=items[2]["download"]["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )

        require(
            redirected["recognized"]
            and redirected["format"] == "txt"
            and len(redirected["candidates"]) == 1
            and redirected["candidates"][0]["channel"] == TEST_CHANNEL,
            "重定向后的 TXT 内容未正确解析",
        )

        print("阶段4测试：人工优先、失败隔离、重定向、HTTP 206 PASS")

        # 索引链接：相对链接、去重、过滤、40条上限、不递归。
        index_items = items[5:]

        require(
            len(index_items) == 40,
            f"索引页处理数量错误：预期40，实际{len(index_items)}",
        )

        found = [
            item["source"]["url"]
            for item in index_items
        ]

        require(
            found[0] == f"{base}/sub/one.m3u"
            and found[1] == f"{base}/sub/two.txt",
            "索引相对链接解析或重复链接去重失败",
        )

        require(
            Handler.request_counts.get("/sub/one.m3u", 0) == 1,
            "相同 URL 的 fragment 变体被重复下载",
        )

        require(
            Handler.request_counts.get("/sub/extra36.m3u", 0) == 1
            and Handler.request_counts.get("/sub/extra37.m3u", 0) == 0,
            "索引页40条上限没有正确生效",
        )

        require(
            Handler.request_counts.get("/sub/hidden.m3u", 0) == 0,
            "索引处理发生递归爬取",
        )

        require(
            Handler.request_counts.get("/child.html", 0) == 0,
            "索引未过滤不支持的 HTML 后缀",
        )

        print(
            "阶段4测试：index相对链接、过滤、去重、"
            "40条上限、不递归 PASS"
        )

        # 普通来源大小限制。
        try:
            download_url(
                f"{base}/large.txt",
                10,
                64,
                5,
                1024,
            )
        except SourceDownloadError:
            pass
        else:
            raise AssertionError(
                "普通来源超过64字节却未触发大小限制"
            )

        # HLS 清单大小限制。
        try:
            download_url(
                f"{base}/oversized-hls.m3u8",
                10,
                1024,
                5,
                32,
            )
        except SourceDownloadError:
            pass
        else:
            raise AssertionError(
                "HLS清单超过32字节却未触发大小限制"
            )

        print("阶段4测试：普通来源和 HLS 清单大小限制 PASS")
        print("阶段4来源下载与解析测试：全部 PASS")

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ----------------------------------------------------------------------
# 阶段5：频道归一、URL归一、去重和候选上限专项测试
# ----------------------------------------------------------------------

def _run_stage5_normalization_tests() -> None:
    """验收频道匹配、URL归一、全局去重和候选上限。"""

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    channels = ["CCTV-5"]

    def make_playlist(entries: list[tuple[str, str]]) -> str:
        lines = ["#EXTM3U"]

        for name, url in entries:
            lines.extend([
                f'#EXTINF:-1 tvg-name="{name}",{name}',
                url,
            ])

        return "\n".join(lines) + "\n"

    def normalize(
        playlist: str,
        source_name: str,
        seen_urls: set[str],
        max_per_source: int = 10,
        remaining: int | None = None,
        aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        source_url = f"https://example.com/{source_name}.m3u"

        return normalize_source_content(
            content=playlist,
            source={
                "name": source_name,
                "url": source_url,
                "final_url": source_url,
                "source_type": "manual",
                "priority": 0,
                "order": 0,
            },
            channels=channels,
            max_candidates_per_source=max_per_source,
            seen_urls=seen_urls,
            content_type="application/x-mpegurl",
            aliases=aliases,
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=remaining,
        )

    # 1. 标准频道名称必须精确匹配。
    exact = normalize(
        make_playlist([
            ("CCTV-5", "https://example.com/live/one.ts"),
        ]),
        "exact",
        set(),
    )

    require(
        len(exact["candidates"]) == 1
        and exact["candidates"][0]["channel"] == "CCTV-5",
        "标准频道名称精确匹配失败",
    )

    # 2. 只允许明确提供的别名映射。
    alias = normalize(
        make_playlist([
            ("中央五套", "https://example.com/live/two.ts"),
        ]),
        "alias",
        set(),
        aliases={"中央五套": "CCTV-5"},
    )

    require(
        len(alias["candidates"]) == 1
        and alias["candidates"][0]["channel"] == "CCTV-5",
        "明确别名映射失败",
    )

    # 3. 相似名称不能被模糊匹配。
    fuzzy = normalize(
        make_playlist([
            ("CCTV-5+", "https://example.com/live/three.ts"),
        ]),
        "fuzzy",
        set(),
    )

    require(
        fuzzy["candidates"] == [],
        "相似频道名称被错误匹配",
    )

    print(
        "阶段5测试：标准频道精确匹配、明确别名和拒绝模糊匹配 PASS"
    )

    # 4. URL协议、主机名、默认端口和片段归一。
    normalized_url = normalize_url(
        "HTTPS://Example.COM:443/live/index.m3u8?token=1#fragment"
    )

    require(
        normalized_url
        == "https://example.com/live/index.m3u8?token=1",
        f"URL归一结果错误：{normalized_url}",
    )

    print(
        "阶段5测试：URL大小写、默认端口、片段和查询参数处理 PASS"
    )

    # 5. 不同来源的相同URL只能保留一条。
    shared_seen: set[str] = set()

    first = normalize(
        make_playlist([
            (
                "CCTV-5",
                "HTTPS://Example.COM:443/live/shared.ts?token=1#first",
            ),
        ]),
        "first",
        shared_seen,
    )

    second = normalize(
        make_playlist([
            (
                "CCTV-5",
                "https://example.com/live/shared.ts?token=1#second",
            ),
        ]),
        "second",
        shared_seen,
    )

    require(
        len(first["candidates"]) == 1
        and second["candidates"] == []
        and second["duplicate_candidates"] == 1,
        "跨来源全局URL去重失败",
    )

    print("阶段5测试：跨来源全局URL去重 PASS")

    # 6. 无效URL和未匹配频道不得进入候选结果。
    invalid_and_unmatched = normalize(
        make_playlist([
            ("CCTV-5", "http://example.com:bad/live.ts"),
            (
                "不在目标列表的频道",
                "https://example.com/live/other.ts",
            ),
        ]),
        "invalid-and-unmatched",
        set(),
    )

    require(
        invalid_and_unmatched["candidates"] == []
        and invalid_and_unmatched["retained_candidates"] == 0
        and any(
            "无效线路 URL" in error
            for error in invalid_and_unmatched["errors"]
        ),
        "无效URL或未匹配频道过滤失败",
    )

    print("阶段5测试：无效URL和未匹配频道过滤 PASS")

    # 7. 单来源达到上限，只限制当前来源。
    source_seen: set[str] = set()

    limited = normalize(
        make_playlist([
            ("无关频道A", "https://example.com/live/irrelevant-a.ts"),
            ("无关频道B", "https://example.com/live/irrelevant-b.ts"),
            ("无关频道C", "https://example.com/live/irrelevant-c.ts"),
            ("CCTV-5", "https://example.com/live/limit-one.ts"),
            ("CCTV-5", "https://example.com/live/limit-two.ts"),
        ]),
        "limited",
        source_seen,
        max_per_source=1,
    )

    next_source = normalize(
        make_playlist([
            ("CCTV-5", "https://example.com/live/next-source.ts"),
        ]),
        "next-source",
        source_seen,
        max_per_source=1,
    )

    require(
        limited["extracted_candidates"] == 1
        and limited["limit_reached"]
        and len(limited["candidates"]) == 1
        and len(next_source["candidates"]) == 1,
        "单来源候选上限错误影响其他来源",
    )

    print("阶段5测试：单来源候选上限及其他来源继续处理 PASS")

    # 8. 本轮剩余额度必须限制当前提取数量。
    remaining_limited = normalize(
        make_playlist([
            ("无关频道A", "https://example.com/live/remaining-irrelevant-a.ts"),
            ("无关频道B", "https://example.com/live/remaining-irrelevant-b.ts"),
            ("无关频道C", "https://example.com/live/remaining-irrelevant-c.ts"),
            ("CCTV-5", "https://example.com/live/remaining-one.ts"),
            ("CCTV-5", "https://example.com/live/remaining-two.ts"),
        ]),
        "remaining",
        set(),
        max_per_source=10,
        remaining=1,
    )

    require(
        remaining_limited["extracted_candidates"] == 1
        and remaining_limited["retained_candidates"] == 1,
        "本轮剩余候选额度执行错误",
    )

    print("阶段5测试：本轮剩余候选额度 PASS")
    print("阶段5线路归一和去重专项测试：全部 PASS")


# ----------------------------------------------------------------------
# 主程序检测交接模拟测试
# ----------------------------------------------------------------------

def _run_pipeline_mock_test(config: dict[str, Any]) -> None:
    """用模拟函数验证主程序交接，不发起真实线路检测或 Globalping 请求。"""

    candidate = {
        "channel": TEST_CHANNEL,
        "normalized_url": "https://example.invalid/test.m3u8",
    }

    calls = {"local": 0, "remote": 0}

    def fake_local(items, settings):
        calls["local"] += 1

        return [
            {
                **items[0],
                "media_type": "HLS",
                "checks": {
                    key: {"status": PASS, "reason": "mock"}
                    for key in ("ipv4", "http", "hls", "ts")
                },
            }
        ]

    def fake_remote(items, settings):
        calls["remote"] += 1

        return [
            {
                **items[0],
                "globalping": {
                    "status": PASS,
                    "checks": {},
                    "stages": {},
                    "probes": {},
                    "measurements": [],
                },
            }
        ]

    result = run_detection_pipeline(
        [candidate],
        config["settings"],
        fake_local,
        fake_remote,
    )

    if (
        calls != {"local": 1, "remote": 1}
        or result[0]["globalping"]["status"] != PASS
    ):
        raise AssertionError("主程序检测模块交接模拟测试失败")

    def failing_local(items, settings):
        return [
            {
                **items[0],
                "media_type": "HLS",
                "checks": {
                    "ipv4": {"status": FAIL},
                    "http": {"status": PASS},
                    "hls": {"status": PASS},
                    "ts": {"status": PASS},
                },
            }
        ]

    def forbidden_remote(items, settings):
        raise AssertionError(
            "本地未通过的线路不应调用 Globalping"
        )

    failed = run_detection_pipeline(
        [candidate],
        config["settings"],
        failing_local,
        forbidden_remote,
    )

    if failed[0]["globalping"]["status"] != INCOMPLETE:
        raise AssertionError(
            "本地失败线路的 Globalping 状态处理错误"
        )

    print(
        "主程序交接测试：本地检测→Globalping、失败隔离、"
        "模拟调用均 PASS；未发起真实网络请求。"
    )


# ----------------------------------------------------------------------
# 主程序与 publisher 交接模拟测试
# ----------------------------------------------------------------------

def _run_publisher_mock_test(config: dict[str, Any]) -> None:
    """使用全部配置目标频道模拟准入和订阅生成。"""

    channels = config["channels"] or [TEST_CHANNEL]
    settings = copy.deepcopy(config["settings"])
    settings["selection"]["max_routes_per_channel"] = 2

    automatic_candidates = []
    for index, channel in enumerate(channels):
        for route_index in range(2):
            automatic_candidates.append({
                "channel": channel,
                "normalized_url": (
                    f"https://example.invalid/auto-{index}-{route_index}.m3u8"
                ),
                "priority": 1,
                "source_order": route_index,
                "source_type": "automatic",
            })

    manual_candidate = {
        "channel": channels[0],
        "normalized_url": "https://example.invalid/manual.m3u8",
        "priority": 0,
        "source_order": 0,
        "source_type": "manual",
    }

    def fake_local(items, detection_settings):
        return [
            {
                **item,
                "media_type": "HLS",
                "checks": {
                    key: {"status": PASS, "reason": "模拟本地检测通过"}
                    for key in ("ipv4", "http", "hls", "ts")
                },
            }
            for item in items
        ]

    def fake_remote(items, globalping_settings):
        return [
            {
                **item,
                "globalping": {
                    "status": INCOMPLETE,
                    "checks": {
                        "ipv4": {"status": PASS},
                        "http": {"status": INCOMPLETE},
                        "hls": {"status": INCOMPLETE},
                        "ts": {"status": PASS},
                    },
                    "stages": {},
                    "probes": {},
                    "measurements": [],
                    "error": "模拟未完成",
                },
            }
            for item in items
        ]

    # 第一批：每个目标频道两条自动来源线路。
    first_detected = run_detection_pipeline(
        automatic_candidates,
        settings,
        fake_local,
        fake_remote,
    )

    first = process_admission_batch(
        candidates=first_detected,
        accepted_routes=[],
        channels=channels,
        settings=settings,
    )

    if (
        len(first["accepted_routes"]) != len(channels) * 2
        or any(first["channel_counts"][channel] != 2 for channel in channels)
        or not first["all_channels_reached"]
    ):
        raise AssertionError("全部目标频道第一批准入或数量统计错误")

    # 第二批：第一频道加入人工线路，验证人工来源优先。
    manual_detected = run_detection_pipeline(
        [manual_candidate],
        settings,
        fake_local,
        fake_remote,
    )

    second = process_admission_batch(
        candidates=manual_detected,
        accepted_routes=first["accepted_routes"],
        channels=channels,
        settings=settings,
    )

    retained_urls = [
        route["normalized_url"]
        for route in second["accepted_routes"]
    ]

    if (
        len(second["accepted_routes"]) != len(channels) * 2
        or manual_candidate["normalized_url"] not in retained_urls
        or not second["all_channels_reached"]
    ):
        raise AssertionError("多批次累计或人工来源优先规则错误")

    for channel in channels:
        count = sum(
            route["channel"] == channel
            for route in second["accepted_routes"]
        )
        if count != 2:
            raise AssertionError(
                f"频道 {channel} 最终线路数量错误：{count}"
            )

    if any(
        route.get("globalping", {}).get("status") != INCOMPLETE
        for route in second["accepted_routes"]
    ):
        raise AssertionError("Globalping INCOMPLETE 状态未被正确保留")

    if any(
        route.get("domestic_verification", {}).get("status")
        != "NOT_VERIFIED"
        for route in second["accepted_routes"]
    ):
        raise AssertionError("国内实播未验证状态处理错误")

    output = publisher.build_subscriptions(
        second["accepted_routes"],
        channels,
    )

    expected_routes = len(channels) * 2

    if (
        output["route_count"] != expected_routes
        or not publisher.validate_subscriptions(
            output["m3u"],
            output["txt"],
            channels,
        )
    ):
        raise AssertionError("M3U/TXT生成或一致性检查失败")

    if manual_candidate["normalized_url"] not in output["m3u"]:
        raise AssertionError("最终M3U没有保留人工优先线路")

    print(
        f"publisher模拟测试通过：{len(channels)}个目标频道，"
        f"{expected_routes}条模拟线路；"
        "人工优先、每频道两条上限、"
        "Globalping降级状态、国内验证状态、"
        "M3U/TXT一致性全部 PASS。"
    )

# ----------------------------------------------------------------------
# 命令入口
# ----------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IPTV-Direct 自动订阅系统"
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("formal", "test"),
        help="formal 正式运行；test 隔离测试",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_configuration(
            test_mode=args.mode == "test"
        )
    except ValueError as exc:
        print(f"配置检查失败：{exc}", file=sys.stderr)
        return 1

    mode_name = (
        "正式运行"
        if args.mode == "formal"
        else "测试运行"
    )

    print(f"IPTV-Direct：{mode_name}")

    print(
        f"配置读取成功：目标频道 {len(config['channels'])} 个；"
        f"人工来源 {len(config['manual_sources'])} 条；"
        f"启用自动来源 {len(config['automatic_sources'])} 条。"
    )

    for error in config.get("errors", []):
        print(f"来源配置警告：{error}", file=sys.stderr)

    if args.mode == "test":
        try:
            _run_stage4_collection_tests(config)
            _run_stage5_normalization_tests()
            _run_pipeline_mock_test(config)
            _run_publisher_mock_test(config)

            print(
                "测试运行通过。测试模式未调用真实 checker 网络检测、"
                "Globalping API、正式线路准入或订阅发布。"
            )

            return 0

        except Exception as exc:
            print(f"测试运行失败：{exc}", file=sys.stderr)
            return 1

    return run_formal_flow(config)


if __name__ == "__main__":
    raise SystemExit(main())

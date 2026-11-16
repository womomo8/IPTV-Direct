#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct 总入口：采集、归一、本地检测及 Globalping 辅助检测。"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import checker
import globalping
from collector import SourceConfigError, download_url, iter_source_contents, prepare_sources
from normalizer import ChannelConfigError, load_channels, normalize_source_content, normalize_url

ROOT = Path(__file__).resolve().parent
CONFIG_FILES = ("channels.txt", "manual.txt", "sources.json", "settings.json")
PASS, FAIL, INCOMPLETE, NA = "PASS", "FAIL", "INCOMPLETE", "N/A"
TEST_CHANNEL = "IPTV-Direct 模拟测试频道"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path.name}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"读取配置文件失败：{path.name}：{exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式错误：{path.name}，第 {exc.lineno} 行：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"配置文件顶层必须是 JSON 对象：{path.name}")
    return value


def _check_settings(settings: dict[str, Any]) -> None:
    required = {
        "collection": ("max_valid_sources", "max_candidates_per_source", "max_candidates_total", "max_index_links_per_page"),
        "detection": ("local_concurrency", "per_host_concurrency", "queue_limit", "max_queue_seconds", "candidate_active_budget_seconds", "source_download_timeout_seconds", "max_source_download_bytes", "max_hls_manifest_bytes", "max_ts_read_bytes", "max_redirects"),
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
                    raise ValueError(f"settings.json 参数 {section}.{key} 必须是非空文本")
            elif isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"settings.json 参数 {section}.{key} 必须是正整数")
    if settings["schedule"]["timezone"] != "Asia/Shanghai":
        raise ValueError("settings.json schedule.timezone 必须为 Asia/Shanghai")
    clock = settings["schedule"]["daily_time"]
    try:
        hour, minute = (int(part) for part in clock.split(":"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("settings.json schedule.daily_time 必须使用 HH:MM 格式") from exc
    if len(clock) != 5 or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("settings.json schedule.daily_time 不是有效 HH:MM 时间")


def load_configuration(root: Path = ROOT, test_mode: bool = False) -> dict[str, Any]:
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
        prepared = prepare_sources(_read_text(root / "manual.txt"), _read_json(root / "sources.json"))
    except SourceConfigError as exc:
        raise ValueError(str(exc)) from exc
    settings = _read_json(root / "settings.json")
    _check_settings(settings)
    return {"channels": channels, **prepared, "settings": settings}


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
    return all(isinstance(checks.get(key), dict) and checks[key].get("status") == PASS for key in required)


def _identity(item: dict[str, Any]) -> tuple[Any, Any]:
    return item.get("channel"), item.get("normalized_url")


def _remote_incomplete(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    media_type = candidate.get("media_type")
    return {**candidate, "globalping": {
        "status": INCOMPLETE,
        "checks": {
            "ipv4": {"status": INCOMPLETE, "reason": reason},
            "http": {"status": INCOMPLETE, "reason": reason},
            "hls": {"status": NA, "reason": "直接 TS 不适用 HLS 检测。"} if media_type == "DIRECT_TS" else {"status": INCOMPLETE, "reason": reason},
            "ts": {"status": INCOMPLETE, "reason": reason},
        },
        "stages": {}, "probes": {}, "measurements": [], "error": reason,
    }}


def run_detection_pipeline(candidates: list[dict[str, Any]], settings: dict[str, Any],
                           local_check=checker.check_candidates,
                           remote_check=globalping.check_candidates) -> list[dict[str, Any]]:
    """先本地检测；只有本地全部适用项目 PASS 的线路才交给 Globalping。"""
    if not candidates:
        return []
    originals = [_identity(item) for item in candidates]
    try:
        local_results = local_check(candidates, settings["detection"])
        if not isinstance(local_results, list) or len(local_results) != len(candidates):
            raise ValueError("checker 返回数量与输入候选数量不一致")
        if any(not isinstance(item, dict) or _identity(item) != originals[i] for i, item in enumerate(local_results)):
            raise ValueError("checker 返回顺序或候选身份不匹配")
    except Exception as exc:
        reason = f"本地检测调用异常或结果校验失败：{exc}"
        local_results = [{**item, "media_type": "UNKNOWN", "checks": {
            key: {"status": INCOMPLETE, "reason": reason} for key in ("ipv4", "http", "hls", "ts")
        }} for item in candidates]

    eligible: list[dict[str, Any]] = []
    output: list[dict[str, Any] | None] = [None] * len(local_results)
    for index, item in enumerate(local_results):
        if _local_pass(item):
            eligible.append(item)
        else:
            output[index] = _remote_incomplete(item, "本地检测未全部通过，未提交 Globalping；不代表远程检测失败。")

    if eligible:
        try:
            remote_results = remote_check(eligible, settings["globalping"])
            if not isinstance(remote_results, list) or len(remote_results) != len(eligible):
                raise ValueError("Globalping 返回数量与输入候选数量不一致")
            for position, result in enumerate(remote_results):
                if not isinstance(result, dict) or _identity(result) != _identity(eligible[position]):
                    raise ValueError("Globalping 返回顺序或候选身份不匹配")
                remote = result.get("globalping")
                if not isinstance(remote, dict) or remote.get("status") not in (PASS, FAIL, INCOMPLETE):
                    raise ValueError("Globalping 缺少有效总体状态")
                original_index = next(i for i, item in enumerate(local_results) if _identity(item) == _identity(eligible[position]))
                output[original_index] = result
        except Exception as exc:
            reason = f"Globalping 调用异常或结果校验失败：{exc}"
            for item in eligible:
                original_index = next(i for i, local in enumerate(local_results) if _identity(local) == _identity(item))
                output[original_index] = _remote_incomplete(item, reason)
    return [item if item is not None else _remote_incomplete(local_results[i], "检测结果未能完成交接。") for i, item in enumerate(output)]


def _print_detection_summary(results: list[dict[str, Any]]) -> None:
    local_ok = sum(_local_pass(item) for item in results)
    local_bad = len(results) - local_ok
    remote_counts = {state: 0 for state in (PASS, FAIL, INCOMPLETE)}
    for item in results:
        state = item.get("globalping", {}).get("status")
        if state in remote_counts:
            remote_counts[state] += 1
    print(f"检测统计：候选 {len(results)} 条；本地全部通过 {local_ok} 条；本地未通过/未完成 {local_bad} 条；Globalping PASS {remote_counts[PASS]} 条、FAIL {remote_counts[FAIL]} 条、INCOMPLETE {remote_counts[INCOMPLETE]} 条。")


def run_formal_flow(config: dict[str, Any]) -> int:
    """逐来源执行采集、归一、本地检测和 Globalping 检测。"""
    settings = config["settings"]
    limits = settings["collection"]
    detection = settings["detection"]
    seen_urls: set[str] = set()
    errors: list[str] = []
    detected: list[dict[str, Any]] = []
    valid_sources = extracted_total = duplicate_total = 0
    stopped = "所有配置来源均已处理"
    stream = iter_source_contents(config["manual_sources"], config["automatic_sources"], settings)
    for item in stream:
        source = item["source"]
        label = source.get("name", source.get("url", "来源"))
        if item.get("errors"):
            errors.extend(f"{label}：{message}" for message in item["errors"])
            continue
        downloaded = item.get("download")
        if not isinstance(downloaded, dict):
            errors.append(f"{label}：采集器未返回下载内容")
            continue
        remaining = limits["max_candidates_total"] - extracted_total
        if remaining <= 0:
            stopped = f"达到全部候选总上限 {limits['max_candidates_total']}"
            break
        source_info = {**source, "final_url": downloaded["final_url"]}
        try:
            normalized = normalize_source_content(
                content=downloaded["content"], source=source_info, channels=config["channels"],
                max_candidates_per_source=limits["max_candidates_per_source"], seen_urls=seen_urls,
                content_type=downloaded["content_type"], max_hls_manifest_bytes=detection["max_hls_manifest_bytes"],
                max_candidates_remaining=remaining,
            )
        except Exception as exc:
            errors.append(f"{label}：归一解析异常：{exc}")
            continue
        if not normalized.get("recognized"):
            errors.extend(f"{label}：{e}" for e in normalized.get("errors", []))
            continue
        extracted_total += normalized["extracted_candidates"]
        duplicate_total += normalized["duplicate_candidates"]
        candidates = normalized["candidates"]
        if normalized.get("matched_target_candidates", 0) > 0:
            valid_sources += 1
        if candidates:
            batch_results = run_detection_pipeline(candidates, settings)
            detected.extend(batch_results)
            _print_detection_summary(batch_results)
        print(f"来源处理：{label}；识别格式 {normalized['format']}；提取候选 {normalized['extracted_candidates']}；本批去重 {normalized['duplicate_candidates']}；进入检测 {len(candidates)}。")
        if valid_sources >= limits["max_valid_sources"]:
            stopped = f"达到有效来源上限 {limits['max_valid_sources']}"
            break
        if extracted_total >= limits["max_candidates_total"]:
            stopped = f"达到全部候选总上限 {limits['max_candidates_total']}"
            break
    print(f"来源汇总：有效来源 {valid_sources}；累计提取候选 {extracted_total}；重复候选 {duplicate_total}；实际完成检测 {len(detected)}。")
    print(f"采集停止原因：{stopped}")
    for error in errors:
        print(f"来源警告：{error}", file=sys.stderr)
    if detected:
        _print_detection_summary(detected)
    print("本阶段未实现 publisher.py：已完成检测结果交接，但未执行正式准入、订阅生成或发布；正式订阅未修改。")
    return 2


def _run_stage4_collection_tests(config: dict[str, Any]) -> None:
    """本机 HTTP 模拟验证采集下载、错误隔离和来源迭代。"""
    playlist = (
        "#EXTM3U\n"
        f'#EXTINF:-1 tvg-name="{TEST_CHANNEL}",{TEST_CHANNEL}\n'
        "https://example.invalid/live/test.ts\n"
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/bad":
                body, status = b"unavailable", 503
            else:
                body, status = playlist, 200
            self.send_response(status)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        settings = config["settings"]
        # 失败来源必须被隔离，后续来源仍继续处理。
        items = list(iter_source_contents(
            [{"name": "失败来源", "url": f"{base}/bad", "source_type": "manual", "priority": 0, "order": 0},
             {"name": "正常来源", "url": f"{base}/ok", "source_type": "manual", "priority": 0, "order": 1}],
            [], settings,
        ))
        if len(items) != 2 or not items[0]["errors"] or items[0]["download"] is not None:
            raise AssertionError("采集失败来源隔离测试失败")
        if items[1]["errors"] or not items[1]["download"]:
            raise AssertionError("单来源失败后没有继续处理后续来源")
        parsed = normalize_source_content(
            content=items[1]["download"]["content"],
            source={**items[1]["source"], "final_url": items[1]["download"]["final_url"]},
            channels=[TEST_CHANNEL], max_candidates_per_source=1, seen_urls=set(),
            content_type=items[1]["download"]["content_type"],
            max_hls_manifest_bytes=settings["detection"]["max_hls_manifest_bytes"],
            max_candidates_remaining=1,
        )
        if not parsed["recognized"] or len(parsed["candidates"]) != 1:
            raise AssertionError("采集结果交接 normalizer 失败")
        print("阶段4测试：本地 HTTP 下载、失败隔离、后续来源继续处理 PASS")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_stage5_normalization_tests() -> None:
    """验证精确频道匹配、明确别名、拒绝模糊匹配、URL归一和去重。"""
    url = normalize_url("HTTPS://Example.COM:443/live/a.ts?token=1#part")
    if url != "https://example.com/live/a.ts?token=1":
        raise AssertionError("URL 归一规则测试失败")

    def parse(name: str, target_url: str, seen: set[str], aliases=None):
        content = f'#EXTM3U\n#EXTINF:-1 tvg-name="{name}",{name}\n{target_url}\n'
        return normalize_source_content(
            content=content,
            source={"name": "测试", "url": "https://example.invalid/list.m3u", "final_url": "https://example.invalid/list.m3u", "source_type": "manual", "priority": 0, "order": 0},
            channels=["CCTV-5"], max_candidates_per_source=5, seen_urls=seen,
            content_type="application/x-mpegurl", aliases=aliases,
            max_hls_manifest_bytes=1048576, max_candidates_remaining=5,
        )
    exact = parse("CCTV-5", "https://example.com/one.ts", set())
    alias = parse("中央五套", "https://example.com/two.ts", set(), {"中央五套": "CCTV-5"})
    fuzzy = parse("CCTV-5+", "https://example.com/three.ts", set())
    if len(exact["candidates"]) != 1 or len(alias["candidates"]) != 1 or fuzzy["candidates"]:
        raise AssertionError("频道精确匹配、别名或拒绝模糊匹配测试失败")
    seen: set[str] = set()
    first = parse("CCTV-5", "HTTPS://Example.COM:443/live/shared.ts#one", seen)
    second = parse("CCTV-5", "https://example.com/live/shared.ts#two", seen)
    if len(first["candidates"]) != 1 or second["candidates"] or second["duplicate_candidates"] != 1:
        raise AssertionError("跨来源 URL 去重测试失败")
    print("阶段5测试：频道精确匹配、明确别名、拒绝模糊匹配、URL归一和全局去重 PASS")


def _run_pipeline_mock_test(config: dict[str, Any]) -> None:
    """只用模拟函数验证主程序交接；不发起真实网络或 Globalping 请求。"""
    candidate = {"channel": TEST_CHANNEL, "normalized_url": "https://example.invalid/test.m3u8"}
    calls = {"local": 0, "remote": 0}
    def fake_local(items, settings):
        calls["local"] += 1
        return [{**items[0], "media_type": "HLS", "checks": {k: {"status": PASS, "reason": "mock"} for k in ("ipv4", "http", "hls", "ts")}}]
    def fake_remote(items, settings):
        calls["remote"] += 1
        return [{**items[0], "globalping": {"status": PASS, "checks": {}, "stages": {}, "probes": {}, "measurements": []}}]
    result = run_detection_pipeline([candidate], config["settings"], fake_local, fake_remote)
    if calls != {"local": 1, "remote": 1} or result[0]["globalping"]["status"] != PASS:
        raise AssertionError("主程序检测模块交接模拟测试失败")
    def failing_local(items, settings):
        return [{**items[0], "media_type": "HLS", "checks": {"ipv4": {"status": FAIL}, "http": {"status": PASS}, "hls": {"status": PASS}, "ts": {"status": PASS}}}]
    def forbidden_remote(items, settings):
        raise AssertionError("本地未通过的线路不应调用 Globalping")
    failed = run_detection_pipeline([candidate], config["settings"], failing_local, forbidden_remote)
    if failed[0]["globalping"]["status"] != INCOMPLETE:
        raise AssertionError("本地失败线路的 Globalping 状态处理错误")
    print("主程序交接测试：本地检测→Globalping、失败隔离、模拟调用均 PASS；未发起真实网络请求。")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IPTV-Direct 自动订阅系统")
    parser.add_argument("--mode", required=True, choices=("formal", "test"), help="formal 正式运行；test 隔离测试")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_configuration(test_mode=args.mode == "test")
    except ValueError as exc:
        print(f"配置检查失败：{exc}", file=sys.stderr)
        return 1
    print(f"IPTV-Direct：{'正式运行' if args.mode == 'formal' else '测试运行'}")
    print(f"配置读取成功：目标频道 {len(config['channels'])} 个；人工来源 {len(config['manual_sources'])} 条；启用自动来源 {len(config['automatic_sources'])} 条。")
    for error in config.get("errors", []):
        print(f"来源配置警告：{error}", file=sys.stderr)
    if args.mode == "test":
        try:
            _run_stage4_collection_tests(config)
            _run_stage5_normalization_tests()
            _run_pipeline_mock_test(config)
            print("测试运行通过。测试模式未调用真实 checker 网络检测、Globalping API、正式准入或订阅发布。")
            return 0
        except Exception as exc:
            print(f"测试运行失败：{exc}", file=sys.stderr)
            return 1
    return run_formal_flow(config)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""IPTV-Direct Globalping 中国大陆 IPv4 诊断。

本脚本只诊断公开 Mux HLS 测试视频，不修改正式订阅。
正式接入时，候选线路必须先经过 IPTV-Direct 自身初筛。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

API_BASE = "https://api.globalping.io/v1"
PROBES_URL = f"{API_BASE}/probes"
LIMITS_URL = f"{API_BASE}/limits"
MAIN_URL = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"

USER_AGENT = "IPTV-Direct-Globalping-Diagnostic/3.0"
MAX_PROBES = 3
RUN_BUDGET_CAP = 200
RESERVE_TESTS = 50
POLL_INTERVAL = 0.8
MAX_POLL_SECONDS = 120

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "User-Agent": USER_AGENT,
}


def auth_headers():
    """生成请求头；Token 只从环境变量读取，不输出、不写入文件。"""
    headers = dict(HEADERS)
    token = os.environ.get("GLOBALPING_API_TOKEN", "").strip()

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


class GlobalpingError(RuntimeError):
    pass


def api_request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=auth_headers(),
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()

            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                import gzip
                raw = gzip.decompress(raw)

            body = raw.decode("utf-8", errors="replace")
            parsed = json.loads(body) if body else {}

            return response.status, parsed, dict(response.headers.items())

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        headers = dict(exc.headers.items()) if exc.headers else {}

        if exc.code == 429:
            rate_headers = {
                k: v for k, v in headers.items()
                if "rate" in k.lower() or "reset" in k.lower()
            }

            raise GlobalpingError(
                "Globalping 返回 HTTP 429，已触及当前限额。"
                f"限额响应头：{json.dumps(rate_headers, ensure_ascii=False)}；"
                f"响应内容：{detail[:1000]}"
            ) from exc

        raise GlobalpingError(
            f"Globalping API HTTP {exc.code}: {detail[:1500]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise GlobalpingError(f"无法连接 Globalping API：{exc}") from exc

    except (json.JSONDecodeError, OSError) as exc:
        raise GlobalpingError(f"Globalping API 响应处理失败：{exc}") from exc


def read_limits():
    print("正在读取 Globalping 当前 IP/API 限额……")
    _, data, _ = api_request(LIMITS_URL)

    try:
        details = data["rateLimit"]["measurements"]["create"]
        limit = int(details["limit"])
        remaining = int(details["remaining"])
        reset = int(details["reset"])

    except (KeyError, TypeError, ValueError) as exc:
        raise GlobalpingError(
            "无法识别 /v1/limits 返回的限额字段；为避免超额，本次停止测试。"
        ) from exc

    print(f"当前创建测量限额：{limit} 次测试/限额周期")
    print(f"当前剩余额度：{remaining}")
    print(f"额度重置倒计时：{reset} 秒")

    return limit, remaining, reset


def is_eyeball_probe(probe):
    tags = probe.get("tags", [])
    return isinstance(tags, list) and "eyeball-network" in tags


def is_china_mobile(asn, network):
    # 仅在中国大陆探针筛选之后调用。
    name = str(network or "").strip().lower()

    return (
        asn in {9808, 24445}
        or "china mobile" in name
        or "中国移动" in name
        or "mobile communications" in name
        or name.endswith(" mobile")
        or " mobile " in f" {name} "
    )


def choose_locations():
    print("\n正在读取中国大陆在线普通用户网络探针……")
    _, probes, _ = api_request(PROBES_URL)

    if not isinstance(probes, list):
        raise GlobalpingError("Globalping 探针列表格式异常。")

    mobile = Counter()
    other = Counter()
    names = {}

    for probe in probes:
        if not isinstance(probe, dict) or not is_eyeball_probe(probe):
            continue

        loc = probe.get("location") or {}

        if not isinstance(loc, dict) or loc.get("country") != "CN":
            continue

        try:
            asn = int(loc.get("asn"))
        except (TypeError, ValueError):
            continue

        network = str(loc.get("network") or "未知网络")
        names[asn] = network

        if is_china_mobile(asn, network):
            mobile[asn] += 1
        else:
            other[asn] += 1

    if not mobile and not other:
        raise GlobalpingError(
            "没有发现符合条件的中国大陆 eyeball-network 探针。"
        )

    allocation = {}

    def allocate_evenly(counts, slots):
        chosen = Counter()
        asns = sorted(counts, key=lambda a: (-counts[a], a))

        while slots:
            progressed = False

            for asn in asns:
                if chosen[asn] < counts[asn]:
                    chosen[asn] += 1
                    slots -= 1
                    progressed = True

                    if slots == 0:
                        break

            if not progressed:
                break

        return chosen

    # 最多优先选择两个移动探针。
    mobile_pick = allocate_evenly(mobile, min(2, MAX_PROBES))
    allocation.update(mobile_pick)

    left = MAX_PROBES - sum(mobile_pick.values())

    # 剩余名额由其他中国大陆运营商补充。
    if left:
        other_pick = allocate_evenly(other, left)
        allocation.update(other_pick)
        left -= sum(other_pick.values())

    # 如果其他运营商探针不足，再由移动探针补齐。
    if left:
        remaining_mobile = Counter({
            asn: max(0, count - allocation.get(asn, 0))
            for asn, count in mobile.items()
        })

        extra = allocate_evenly(remaining_mobile, left)

        for asn, count in extra.items():
            allocation[asn] = allocation.get(asn, 0) + count

    locations = []

    print("\n本次探针分配：")

    for asn in sorted(
        allocation,
        key=lambda a: (a not in mobile, a)
    ):
        count = allocation[asn]

        group = (
            "中国移动优先"
            if asn in mobile
            else "大陆其他运营商补充"
        )

        print(
            f"  {group}：AS{asn} "
            f"{names.get(asn, '未知网络')}，最多 {count} 个"
        )

        locations.append({
            "country": "CN",
            "asn": asn,
            "tags": ["eyeball-network"],
            "limit": count,
        })

    total = sum(allocation.values())

    if total < 1:
        raise GlobalpingError("没有可用探针名额。")

    print(f"本次最多请求 {total} 个探针。")

    return locations, total


def split_target_url(url):
    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise GlobalpingError(f"无效或不支持的 URL：{url}")

    if parsed.username or parsed.password:
        raise GlobalpingError("测试 URL 不允许包含用户名或密码。")

    protocol = parsed.scheme.upper()

    return {
        "target": parsed.hostname,
        "protocol": protocol,
        "port": parsed.port or (443 if protocol == "HTTPS" else 80),
        "path": (parsed.path or "/") + (
            ("?" + parsed.query) if parsed.query else ""
        ),
    }


def create_measurement(url, locations, budget, extra_headers=None):
    if isinstance(locations, list):
        estimated = sum(
            int(item.get("limit", 1))
            for item in locations
        )
    else:
        # 使用之前的测量 ID 复用探针。
        estimated = MAX_PROBES

    if budget["remaining"] < estimated:
        raise GlobalpingError(
            f"剩余额度预算只有 {budget['remaining']}，"
            f"不足以安全发起本阶段（最多需要 {estimated}）；停止后续测试。"
        )

    parts = split_target_url(url)

    payload = {
        "type": "http",
        "target": parts["target"],
        "locations": locations,
        "measurementOptions": {
            "protocol": parts["protocol"],
            "port": parts["port"],
            "ipVersion": 4,
            "request": {
                "method": "GET",
                "path": parts["path"],
                "headers": dict(extra_headers or {}),
            },
        },
    }

    print(f"目标：{parts['target']}{parts['path']}")

    status, result, _ = api_request(
        f"{API_BASE}/measurements",
        method="POST",
        payload=payload,
    )

    measurement_id = result.get("id")

    if status != 202 or not measurement_id:
        raise GlobalpingError(
            "创建测量失败："
            + json.dumps(result, ensure_ascii=False)[:1500]
        )

    actual = result.get("probesCount")

    try:
        actual = int(actual)
    except (TypeError, ValueError):
        actual = estimated

    if actual < 1:
        actual = estimated

    budget["remaining"] = max(
        0,
        budget["remaining"] - actual
    )

    print(f"测量 ID：{measurement_id}")
    print(f"本阶段实际探针数：{actual}")
    print(f"本次运行剩余预算：{budget['remaining']}")

    return measurement_id


def wait_for_measurement(measurement_id):
    url = f"{API_BASE}/measurements/{measurement_id}"

    started = time.monotonic()
    etag = None

    while True:
        headers = auth_headers()

        if etag:
            headers["If-None-Match"] = etag

        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()

                if response.headers.get(
                    "Content-Encoding", ""
                ).lower() == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)

                etag = response.headers.get("ETag", etag)

                data = json.loads(
                    raw.decode("utf-8", errors="replace")
                )

        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                data = None
            else:
                detail = exc.read().decode(
                    "utf-8",
                    errors="replace"
                )

                raise GlobalpingError(
                    f"读取测量结果失败 HTTP {exc.code}: "
                    f"{detail[:1000]}"
                ) from exc

        if data is not None:
            status = data.get("status")
            print(f"测量状态：{status}")

            if status != "in-progress":
                return data

        if time.monotonic() - started > MAX_POLL_SECONDS:
            raise GlobalpingError("等待测量结果超过 120 秒。")

        # 官方要求每次收到响应后至少等待 500ms。
        time.sleep(POLL_INTERVAL)


def get_successful_http_results(measurement):
    valid = []

    for item in measurement.get("results", []):
        result = item.get("result") or {}
        code = result.get("statusCode")

        if (
            result.get("status") == "finished"
            and isinstance(code, int)
            and 200 <= code < 300
        ):
            valid.append(item)

    return valid


def extract_body(item):
    body = (item.get("result") or {}).get("rawBody")

    return body if isinstance(body, str) else ""


def first_media_url(body, base_url):
    for line in body.splitlines():
        line = line.strip()

        if line and not line.startswith("#"):
            return urllib.parse.urljoin(base_url, line)

    return None


def run_stage(title, url, locations, budget, headers=None):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

    measurement_id = create_measurement(
        url,
        locations,
        budget,
        extra_headers=headers,
    )

    measurement = wait_for_measurement(measurement_id)

    results = measurement.get("results", [])

    print(f"返回探针结果：{len(results)} 个")

    for item in results:
        probe = item.get("probe") or {}
        result = item.get("result") or {}

        print(
            f"  {probe.get('city', '未知')} / "
            f"{probe.get('network', '未知')} / "
            f"AS{probe.get('asn', '未知')} / "
            f"HTTP {result.get('statusCode', '无')} / "
            f"{result.get('status', '未知')}"
        )

    return measurement_id, measurement


def check_range(measurement):
    passed = 0

    for item in measurement.get("results", []):
        probe = item.get("probe") or {}
        result = item.get("result") or {}

        code = result.get("statusCode")
        headers = result.get("headers") or {}

        normalized = {
            str(k).lower(): str(v).strip()
            for k, v in headers.items()
        }

        content_range = normalized.get("content-range", "")
        content_length = normalized.get("content-length", "")

        total = (
            content_range.rsplit("/", 1)[-1]
            if "/" in content_range
            else ""
        )

        ok = (
            result.get("status") == "finished"
            and code == 206
            and content_range.startswith("bytes 0-1023/")
            and total.isdigit()
            and int(total) > 1023
            and (
                not content_length
                or content_length == "1024"
            )
        )

        passed += int(ok)

        print(
            f"  {probe.get('city', '未知')} / "
            f"{probe.get('network', '未知')}："
            f"HTTP {code}，"
            f"TS Range {'通过' if ok else '未通过'}"
        )

        if content_range:
            print(f"    Content-Range: {content_range}")

    return passed


def main():
    print("=" * 64)
    print("IPTV-Direct Globalping 中国大陆 IPv4 诊断")
    print("中国移动优先，其他中国大陆普通用户网络补充")
    print("测试目标是公开 Mux HLS VOD，不是真实电视直播")
    print("=" * 64)

    try:
        _, remaining, _ = read_limits()

        available_budget = max(
            0,
            min(
                RUN_BUDGET_CAP,
                remaining - RESERVE_TESTS,
            ),
        )

        if available_budget < MAX_PROBES:
            raise GlobalpingError(
                f"当前剩余额度 {remaining}，扣除预留 "
                f"{RESERVE_TESTS} 后，不足以启动本次诊断。"
            )

        print(
            f"本次运行预算：最多 {available_budget} 次探针测试"
            f"（每小时保护上限 {RUN_BUDGET_CAP}，"
            f"预留 {RESERVE_TESTS}）。"
        )

        budget = {"remaining": available_budget}

        locations, probe_count = choose_locations()

        if probe_count > budget["remaining"]:
            raise GlobalpingError(
                "本次探针需求超过当前运行预算，停止测试。"
            )

        main_id, main_measurement = run_stage(
            "第一阶段：主 M3U8",
            MAIN_URL,
            locations,
            budget,
        )

        main_valid = get_successful_http_results(
            main_measurement
        )

        if not main_valid:
            raise GlobalpingError(
                "主 M3U8 没有取得 HTTP 2xx 结果，停止后续阶段。"
            )

        main_body = extract_body(main_valid[0])
        child_url = first_media_url(main_body, MAIN_URL)

        if not child_url:
            raise GlobalpingError(
                "无法从主 M3U8 正文解析子播放列表。"
            )

        _, child_measurement = run_stage(
            "第二阶段：子 M3U8",
            child_url,
            main_id,
            budget,
        )

        child_valid = get_successful_http_results(
            child_measurement
        )

        if not child_valid:
            raise GlobalpingError(
                "子 M3U8 没有取得 HTTP 2xx 结果，停止 TS 测试。"
            )

        child_body = extract_body(child_valid[0])
        ts_url = first_media_url(child_body, child_url)

        if not ts_url:
            raise GlobalpingError(
                "无法从子 M3U8 正文解析媒体分片地址。"
            )

        _, range_measurement = run_stage(
            "第三阶段：TS Range",
            ts_url,
            main_id,
            budget,
            headers={"Range": "bytes=0-1023"},
        )

        range_success = check_range(range_measurement)

        print("\n" + "=" * 64)
        print("诊断摘要")
        print("=" * 64)

        print(f"主 M3U8：{len(main_valid)} 个探针 HTTP 2xx")
        print(f"子 M3U8：{len(child_valid)} 个探针 HTTP 2xx")
        print(
            f"TS Range：{range_success} 个探针确认 "
            "HTTP 206 和正确 Content-Range"
        )

        print(f"本次运行预算剩余：{budget['remaining']}")
        print(
            "注意：本测试不能证明真实 IPTV 频道可以长期、连续播放。"
        )

    except Exception as exc:
        print("\n诊断未完整完成：")
        print(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()

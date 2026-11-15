#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""IPTV-Direct Globalping diagnostic.

中国移动优先，其他中国大陆 eyeball-network 探针辅助。
同一次诊断的主清单、子清单和 TS Range 尽量复用同一批探针。
仅用于诊断，不修改正式订阅。
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

API_BASE = "https://api.globalping.io/v1"
PROBES_URL = f"{API_BASE}/probes"
MAIN_URL = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
USER_AGENT = "IPTV-Direct-Globalping-Test/2.0"
MAX_PROBES = 50
MOBILE_PRIORITY_CAP = 40
POLL_INTERVAL = 0.8
MAX_POLL_SECONDS = 120

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}


def api_request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=API_HEADERS, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Globalping API HTTP {exc.code}: {detail[:2000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 Globalping API：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Globalping API 返回的内容不是有效 JSON。") from exc


def is_eyeball_probe(probe):
    tags = probe.get("tags", [])
    return isinstance(tags, list) and "eyeball-network" in tags


def is_china_mobile(asn, network):
    name = str(network or "").lower()
    return asn == 9808 or "china mobile" in name or "中国移动" in name


def distribute_evenly(counts, slots):
    """在不同 ASN 之间轮流分配名额，避免辅助探针集中在单一网络。"""
    allocated = {asn: 0 for asn in counts}
    remaining = min(slots, sum(counts.values()))
    asns = sorted(counts, key=lambda item: (-counts[item], item))

    while remaining > 0:
        progressed = False
        for asn in asns:
            if allocated[asn] < counts[asn]:
                allocated[asn] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
        if not progressed:
            break
    return {asn: count for asn, count in allocated.items() if count > 0}


def discover_probe_locations():
    print("正在读取 Globalping 当前在线探针……")
    _, probes = api_request(PROBES_URL)
    if not isinstance(probes, list):
        raise RuntimeError("Globalping 在线探针列表格式异常。")

    china_counts = Counter()
    network_names = {}
    mobile_counts = Counter()
    other_counts = Counter()

    for probe in probes:
        if not isinstance(probe, dict) or not is_eyeball_probe(probe):
            continue
        location = probe.get("location", {})
        if not isinstance(location, dict) or location.get("country") != "CN":
            continue
        try:
            asn = int(location.get("asn"))
        except (TypeError, ValueError):
            continue
        network = location.get("network") or "未知网络"
        china_counts[asn] += 1
        network_names[asn] = network
        if is_china_mobile(asn, network):
            mobile_counts[asn] += 1
        else:
            other_counts[asn] += 1

    if not china_counts:
        raise RuntimeError(
            "当前没有发现带 eyeball-network 标签的中国大陆在线探针。"
        )

    # 先尽可能选择中国移动探针，移动最多占用40个名额。
    mobile_allocation = distribute_evenly(
        mobile_counts, min(MOBILE_PRIORITY_CAP, MAX_PROBES)
    )
    mobile_used = sum(mobile_allocation.values())

    # 剩余名额再从其他中国大陆 ASN 中平均补充，最多不超过50个。
    other_allocation = distribute_evenly(
        other_counts, MAX_PROBES - mobile_used
    )

    allocations = {}
    allocations.update(mobile_allocation)
    allocations.update(other_allocation)

    locations = []
    print("\n在线探针概况（仅统计中国大陆普通用户网络）：")
    for asn in sorted(china_counts):
        kind = "中国移动优先组" if asn in mobile_counts else "其他运营商辅助组"
        print(
            f"  AS{asn} | {network_names[asn]} | 在线 {china_counts[asn]} 个 | {kind}"
        )

    print("\n本次测量探针分配：")
    for asn in sorted(allocations, key=lambda item: (item not in mobile_counts, item)):
        count = allocations[asn]
        locations.append({
            "country": "CN",
            "asn": asn,
            "tags": ["eyeball-network"],
            "limit": count,
        })
        group = "移动优先" if asn in mobile_counts else "辅助"
        print(f"  {group}：AS{asn} {network_names[asn]}，请求 {count} 个探针")

    print(f"探针请求总上限：{sum(allocations.values())}（免费未认证单次最多50个）")
    return locations


def split_target_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError(f"不支持或无效的 URL：{url}")
    if parsed.username or parsed.password:
        raise RuntimeError("测试 URL 不允许包含用户名或密码。")

    protocol = parsed.scheme.upper()
    port = parsed.port or (443 if protocol == "HTTPS" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return {
        "target": parsed.hostname,
        "protocol": protocol,
        "port": port,
        "path": path,
    }


def create_measurement(url, locations, extra_headers=None):
    parts = split_target_url(url)
    headers = dict(extra_headers or {})
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
                "headers": headers,
            },
        },
    }

    print(f"目标域名：{parts['target']}")
    print(f"请求路径：{parts['path']}")
    print("IP 版本：IPv4")

    status_code, result = api_request(
        f"{API_BASE}/measurements", method="POST", payload=payload
    )
    measurement_id = result.get("id")
    if status_code != 202 or not measurement_id:
        raise RuntimeError(
            "创建测量失败：" + json.dumps(result, ensure_ascii=False)
        )
    print(f"测量 ID：{measurement_id}")
    print(f"Globalping 实际接受探针数：{result.get('probesCount', '未知')}")
    return measurement_id


def wait_for_measurement(measurement_id):
    url = f"{API_BASE}/measurements/{measurement_id}"
    start_time = time.monotonic()
    while True:
        _, result = api_request(url)
        status = result.get("status")
        print(f"测量状态：{status}")
        if status != "in-progress":
            return result
        if time.monotonic() - start_time > MAX_POLL_SECONDS:
            raise RuntimeError("等待 Globalping 测量结果超时。")
        time.sleep(POLL_INTERVAL)


def print_probe_results(measurement):
    results = measurement.get("results", [])
    print(f"\n实际返回探针数：{len(results)}")
    for index, item in enumerate(results, start=1):
        probe = item.get("probe", {})
        result = item.get("result", {})
        print(
            f"探针 {index}：{probe.get('city', '未知')} / "
            f"{probe.get('network', '未知')} / "
            f"AS{probe.get('asn', '未知')} / "
            f"HTTP {result.get('statusCode', '无')} / "
            f"{result.get('status', '未知')}"
        )
        if result.get("failureSource"):
            print(f"  失败来源：{result['failureSource']}")
    return results


def successful_results(measurement):
    valid = []
    for item in measurement.get("results", []):
        result = item.get("result", {})
        code = result.get("statusCode")
        if result.get("status") == "finished" and isinstance(code, int) and 200 <= code < 300:
            valid.append(item)
    return valid


def extract_body(item):
    body = item.get("result", {}).get("rawBody")
    return body if isinstance(body, str) else ""


def extract_first_media_url(body, base_url):
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return urllib.parse.urljoin(base_url, line)
    return None


def run_stage(title, url, locations, extra_headers=None):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)
    print(f"完整目标：{url}\n")
    measurement_id = create_measurement(url, locations, extra_headers)
    measurement = wait_for_measurement(measurement_id)
    results = print_probe_results(measurement)
    return measurement_id, measurement, results


def check_range_result(measurement):
    success_count = 0
    for item in measurement.get("results", []):
        probe = item.get("probe", {})
        result = item.get("result", {})
        code = result.get("statusCode")
        headers = result.get("headers", {})
        normalized = {str(k).lower(): str(v).strip() for k, v in headers.items()}
        content_range = normalized.get("content-range", "")
        content_length = normalized.get("content-length", "")
        total_size = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
        ok = (
            result.get("status") == "finished"
            and code == 206
            and content_range.startswith("bytes 0-1023/")
            and total_size.isdigit()
            and int(total_size) > 1023
            and (not content_length or content_length == "1024")
        )
        if ok:
            success_count += 1
        print(
            f"{probe.get('city', '未知')} / {probe.get('network', '未知')} "
            f"(AS{probe.get('asn', '未知')})：HTTP {code}，"
            f"Range {'成功' if ok else '未确认成功'}"
        )
        if content_range:
            print(f"  Content-Range：{content_range}")
        if content_length:
            print(f"  Content-Length：{content_length}")
    return success_count


def main():
    print("=" * 64)
    print("IPTV-Direct Globalping 中国大陆 IPv4 诊断")
    print("策略：中国移动优先，其他中国大陆普通用户网络辅助")
    print("测试目标：公开 Mux HLS VOD 测试视频，不是真实电视直播源")
    print("=" * 64)

    try:
        locations = discover_probe_locations()

        # 第一阶段负责挑选探针；后续阶段复用这批探针。
        main_id, main_measurement, _ = run_stage(
            "第一阶段：主 M3U8 测试", MAIN_URL, locations
        )
        main_valid = successful_results(main_measurement)
        if not main_valid:
            raise RuntimeError("主 M3U8 没有取得成功的 HTTP 2xx 结果。")
        main_body = extract_body(main_valid[0])
        if not main_body:
            raise RuntimeError("主 M3U8 正文为空，无法继续解析。")
        child_url = extract_first_media_url(main_body, MAIN_URL)
        if not child_url:
            raise RuntimeError("没有找到主 M3U8 中的子播放列表地址。")
        print(f"\n解析到子 M3U8：{child_url}")

        # locations 使用上一步 measurement ID，尽量保证三个阶段是同一批探针。
        _, child_measurement, _ = run_stage(
            "第二阶段：子 M3U8 测试", child_url, main_id
        )
        child_valid = successful_results(child_measurement)
        if not child_valid:
            raise RuntimeError("子 M3U8 没有取得成功的 HTTP 2xx 结果。")
        child_body = extract_body(child_valid[0])
        if not child_body:
            raise RuntimeError("子 M3U8 正文为空，无法继续解析。")
        ts_url = extract_first_media_url(child_body, child_url)
        if not ts_url:
            raise RuntimeError("没有找到子 M3U8 中的媒体分片地址。")
        print(f"\n解析到媒体分片：{ts_url}")

        _, range_measurement, _ = run_stage(
            "第三阶段：TS Range 分段读取",
            ts_url,
            main_id,
            extra_headers={"Range": "bytes=0-1023"},
        )
        range_success = check_range_result(range_measurement)

        print("\n" + "=" * 64)
        print("最终诊断摘要")
        print("=" * 64)
        print(f"主 M3U8：{len(successful_results(main_measurement))} 个探针 HTTP 2xx")
        print(f"子 M3U8：{len(successful_results(child_measurement))} 个探针 HTTP 2xx")
        print(f"TS Range：{range_success} 个探针确认 HTTP 206 和正确 Content-Range")
        print("\n说明：")
        print("1. 中国移动探针优先，最多优先分配40个名额；其余名额由其他大陆网络补充。")
        print("2. 单次未认证测量最多请求50个探针，实际返回数量可能更少。")
        print("3. 三个阶段尽量复用同一批探针，但探针离线等情况仍可能造成结果减少。")
        print("4. HTTP 成功不等于真实电视直播能持续播放；本次目标是公开 VOD 测试视频。")

    except Exception as exc:
        print("\n" + "=" * 64)
        print("测试未完整完成")
        print("=" * 64)
        print(f"错误原因：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPTV-Direct Globalping API diagnostic test.

Tests:
1. Main HLS playlist from China eyeball probes.
2. First child playlist.
3. First TS segment using an HTTP Range request.

This is a diagnostic tool only. It does not modify IPTV subscriptions.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.globalping.io/v1"
MAIN_URL = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"

POLL_INTERVAL = 0.7
MAX_POLL_SECONDS = 90

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "IPTV-Direct-Globalping-Test/1.0",
}


def api_request(url, method="GET", payload=None):
    data = None
    headers = dict(API_HEADERS)

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Globalping API HTTP {exc.code}: {detail[:1500]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"无法连接 Globalping API：{exc}"
        ) from exc


def create_measurement(target, extra_headers=None):
    request_headers = {
        "User-Agent": "IPTV-Direct-Globalping-Test/1.0",
    }

    if extra_headers:
        request_headers.update(extra_headers)

    payload = {
        "type": "http",
        "target": target,
        "locations": [
            {
                "country": "CN",
                "tags": ["eyeball-network"],
                "limit": 3,
            }
        ],
        "measurementOptions": {
            "ipVersion": 4,
            "request": {
                "method": "GET",
                "headers": request_headers,
            },
        },
    }

    status_code, result = api_request(
        f"{API_BASE}/measurements",
        method="POST",
        payload=payload,
    )

    measurement_id = result.get("id")

    if status_code != 202 or not measurement_id:
        raise RuntimeError(
            f"创建测量失败：{json.dumps(result, ensure_ascii=False)}"
        )

    print(f"已创建测量：{measurement_id}")
    print(f"Globalping 探针数量：{result.get('probesCount', '未知')}")

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
            raise RuntimeError("等待测量结果超时。")

        # 官方建议：同一个测量结果查询间隔至少 500 毫秒。
        time.sleep(POLL_INTERVAL)


def get_http_results(measurement):
    results = measurement.get("results", [])

    if not results:
        print("没有返回探针结果。")
        return []

    for index, item in enumerate(results, start=1):
        probe = item.get("probe", {})
        result = item.get("result", {})

        print(f"\n探针 {index}")
        print(
            "位置："
            f"{probe.get('city', '未知')}, "
            f"{probe.get('country', '未知')}"
        )
        print(f"ASN：{probe.get('asn', '未知')}")
        print(f"网络：{probe.get('network', '未知')}")
        print(f"探针状态：{result.get('status', '未知')}")
        print(f"HTTP 状态码：{result.get('statusCode', '无')}")
        print(f"正文是否截断：{result.get('truncated', False)}")

    return results


def extract_body(result):
    body = result.get("rawBody")

    if not isinstance(body, str):
        return ""

    return body


def successful_http_results(measurement):
    valid = []

    for item in measurement.get("results", []):
        result = item.get("result", {})
        status_code = result.get("statusCode")

        if (
            result.get("status") == "finished"
            and isinstance(status_code, int)
            and 200 <= status_code < 300
        ):
            valid.append(item)

    return valid


def extract_playlist_url(body, base_url):
    for line in body.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        return urllib.parse.urljoin(base_url, line)

    return None


def run_stage(title, target, extra_headers=None):
    print("\n" + "=" * 60)
    print(title)
    print(f"目标：{target}")
    print("=" * 60)

    measurement_id = create_measurement(
        target,
        extra_headers=extra_headers,
    )

    measurement = wait_for_measurement(measurement_id)
    results = get_http_results(measurement)

    return measurement, results


def main():
    print("IPTV-Direct：Globalping API 自动化诊断")
    print("测试网络：China + eyeball-network")
    print("IP 版本：IPv4")
    print("探针上限：每次 3 个")
    print("说明：这是公开 HLS 测试视频，不是真实电视直播。")

    try:
        # 第一阶段：主播放列表
        main_measurement, main_results = run_stage(
            "第一阶段：主 M3U8",
            MAIN_URL,
        )

        main_valid = successful_http_results(main_measurement)

        if not main_valid:
            raise RuntimeError(
                "主 M3U8 没有取得成功的 HTTP 2xx 结果。"
            )

        playlist_body = extract_body(
            main_valid[0].get("result", {})
        )

        if not playlist_body:
            raise RuntimeError(
                "主 M3U8 正文为空，无法继续解析。"
            )

        child_url = extract_playlist_url(
            playlist_body,
            MAIN_URL,
        )

        if not child_url:
            raise RuntimeError(
                "没有在主 M3U8 中找到子播放列表地址。"
            )

        print(f"\n解析到子播放列表：{child_url}")

        # 第二阶段：子播放列表
        child_measurement, child_results = run_stage(
            "第二阶段：子 M3U8",
            child_url,
        )

        child_valid = successful_http_results(child_measurement)

        if not child_valid:
            raise RuntimeError(
                "子 M3U8 没有取得成功的 HTTP 2xx 结果。"
            )

        child_body = extract_body(
            child_valid[0].get("result", {})
        )

        if not child_body:
            raise RuntimeError(
                "子 M3U8 正文为空，无法继续解析。"
            )

        ts_url = extract_playlist_url(
            child_body,
            child_url,
        )

        if not ts_url:
            raise RuntimeError(
                "没有在子 M3U8 中找到 TS 分片地址。"
            )

        print(f"\n解析到 TS 分片：{ts_url}")

        # 第三阶段：Range 请求读取 1024 字节
        range_measurement, range_results = run_stage(
            "第三阶段：TS Range 分段读取",
            ts_url,
            extra_headers={
                "Range": "bytes=0-1023",
            },
        )

        print("\n" + "=" * 60)
        print("最终诊断摘要")
        print("=" * 60)

        range_valid = []

        for item in range_measurement.get("results", []):
            probe = item.get("probe", {})
            result = item.get("result", {})
            code = result.get("statusCode")
            headers = result.get("headers", {})

            content_range = ""

            if isinstance(headers, dict):
                content_range = (
                    headers.get("content-range")
                    or headers.get("Content-Range")
                    or ""
                )

            range_ok = (
                code == 206
                and "0-1023/" in str(content_range)
            )

            if range_ok:
                range_valid.append(item)

            print(
                f"{probe.get('city', '未知')} / "
                f"{probe.get('network', '未知')}："
                f"HTTP {code}，"
                f"Range {'成功' if range_ok else '未确认成功'}"
            )

        print("\n主 M3U8：已完成测量")
        print("子 M3U8：已完成测量")
        print(
            "TS Range："
            + (
                "至少一个探针返回 206 和正确 Content-Range"
                if range_valid
                else "没有确认成功，需检查响应详情"
            )
        )

        print("\n注意：")
        print("1. 结果只代表本次实际参与测量的大陆探针。")
        print("2. Range 失败不等于 TS 分片不能正常下载。")
        print("3. API 测试成功不等于真实播放器持续播放成功。")
        print("4. 这次测试不会证明所有中国运营商均可访问。")

    except Exception as exc:
        print("\n测试未完整完成。")
        print(f"原因：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

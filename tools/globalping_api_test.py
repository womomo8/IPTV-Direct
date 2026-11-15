
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPTV-Direct Globalping API Diagnostic Test

测试顺序：
1. 中国大陆探针获取主 M3U8
2. 获取子 M3U8
3. 使用 Range 请求获取 TS 分片开头 1024 字节

仅用于诊断，不修改正式订阅。
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://api.globalping.io/v1"

MAIN_URL = (
    "https://test-streams.mux.dev/"
    "x36xhzz/x36xhzz.m3u8"
)

POLL_INTERVAL = 0.7
MAX_POLL_SECONDS = 120

USER_AGENT = "IPTV-Direct-Globalping-Test/1.0"

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}


def api_request(url, method="GET", payload=None):
    data = None

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers=API_HEADERS,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            body = response.read().decode(
                "utf-8",
                errors="replace",
            )

            return response.status, json.loads(body)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Globalping API HTTP {exc.code}: "
            f"{detail[:2000]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"无法连接 Globalping API：{exc}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Globalping API 返回的内容不是有效 JSON。"
        ) from exc


def split_target_url(url):
    """
    将完整 URL 拆分为 Globalping 所需字段。

    target 只填写主机名。
    协议、端口、路径和查询参数放入 measurementOptions。
    """

    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(
            f"不支持的 URL 协议：{parsed.scheme}"
        )

    if not parsed.hostname:
        raise RuntimeError(
            f"URL 缺少有效主机名：{url}"
        )

    if parsed.username or parsed.password:
        raise RuntimeError(
            "测试 URL 不允许包含用户名或密码。"
        )

    protocol = parsed.scheme.upper()
    target = parsed.hostname

    if parsed.port is not None:
        port = parsed.port
    elif protocol == "HTTPS":
        port = 443
    else:
        port = 80

    path = parsed.path or "/"

    if parsed.query:
        path += "?" + parsed.query

    return {
        "target": target,
        "protocol": protocol,
        "port": port,
        "path": path,
    }


def create_measurement(url, extra_headers=None):
    parts = split_target_url(url)

    request_headers = {}

    if extra_headers:
        request_headers.update(extra_headers)

    measurement_options = {
        "protocol": parts["protocol"],
        "port": parts["port"],
        "ipVersion": 4,
        "request": {
            "method": "GET",
            "path": parts["path"],
            "headers": request_headers,
        },
    }

    payload = {
        "type": "http",
        "target": parts["target"],
        "locations": [
            {
                "country": "CN",
                "tags": ["eyeball-network"],
                "limit": 3,
            }
        ],
        "measurementOptions": measurement_options,
    }

    print(f"目标域名：{parts['target']}")
    print(f"请求协议：{parts['protocol']}")
    print(f"请求端口：{parts['port']}")
    print(f"请求路径：{parts['path']}")
    print("探针区域：中国大陆")
    print("探针类型：eyeball-network")
    print("IP 版本：IPv4")
    print("探针上限：3")

    status_code, result = api_request(
        f"{API_BASE}/measurements",
        method="POST",
        payload=payload,
    )

    measurement_id = result.get("id")

    if status_code != 202 or not measurement_id:
        raise RuntimeError(
            "创建测量失败："
            + json.dumps(
                result,
                ensure_ascii=False,
            )
        )

    print(f"测量 ID：{measurement_id}")
    print(
        "实际探针数量（创建时）："
        f"{result.get('probesCount', '待确认')}"
    )

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
            raise RuntimeError(
                "等待 Globalping 测量结果超时。"
            )

        time.sleep(POLL_INTERVAL)


def print_probe_results(measurement):
    results = measurement.get("results", [])

    if not results:
        print("没有返回探针结果。")
        return []

    print(f"\n实际返回探针数：{len(results)}")

    for index, item in enumerate(results, start=1):
        probe = item.get("probe", {})
        result = item.get("result", {})

        print(f"\n探针 {index}")
        print(f"国家：{probe.get('country', '未知')}")
        print(f"城市：{probe.get('city', '未知')}")
        print(f"ASN：{probe.get('asn', '未知')}")
        print(f"网络：{probe.get('network', '未知')}")
        print(f"探针状态：{result.get('status', '未知')}")
        print(f"HTTP 状态码：{result.get('statusCode', '无')}")

        if result.get("failureSource"):
            print(
                "失败来源："
                f"{result.get('failureSource')}"
            )

        if result.get("status") == "failed":
            print(
                "失败详情："
                f"{result.get('rawOutput', '无')}"
            )

    return results


def successful_results(measurement):
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


def extract_body(item):
    result = item.get("result", {})
    body = result.get("rawBody")

    if isinstance(body, str):
        return body

    return ""


def extract_first_media_url(body, base_url):
    """
    解析 M3U8 中第一条非注释媒体地址。
    自动处理绝对地址和相对地址。
    """

    for line in body.splitlines():
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        return urllib.parse.urljoin(
            base_url,
            line,
        )

    return None


def run_stage(title, url, extra_headers=None):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

    print(f"完整目标：{url}\n")

    measurement_id = create_measurement(
        url,
        extra_headers=extra_headers,
    )

    measurement = wait_for_measurement(
        measurement_id
    )

    results = print_probe_results(measurement)

    return measurement, results


def check_range_result(measurement):
    success_count = 0

    for item in measurement.get("results", []):
        probe = item.get("probe", {})
        result = item.get("result", {})

        status_code = result.get("statusCode")
        headers = result.get("headers", {})

        normalized_headers = {
            str(key).lower(): str(value).strip()
            for key, value in headers.items()
        }

        content_range = normalized_headers.get(
            "content-range",
            "",
        )

        content_length = normalized_headers.get(
            "content-length",
            "",
        )

        # TS 是二进制数据。
        # Globalping 的 rawBody 文本长度不能作为
        # 原始 TS 字节数的可靠判断依据。
        #
        # 这里使用 HTTP 206 和 Content-Range
        # 判断服务器是否正确响应指定字节范围。

        total_size = ""

        if "/" in content_range:
            total_size = content_range.rsplit("/", 1)[-1]

        range_ok = (
            result.get("status") == "finished"
            and status_code == 206
            and content_range.startswith("bytes 0-1023/")
            and total_size.isdigit()
            and int(total_size) > 1023
            and (
                not content_length
                or content_length == "1024"
            )
        )

        if range_ok:
            success_count += 1

        print(
            f"{probe.get('city', '未知')} / "
            f"{probe.get('network', '未知')}："
            f"HTTP {status_code}，"
            f"Range "
            f"{'成功' if range_ok else '未确认成功'}"
        )

        if content_range:
            print(f"Content-Range：{content_range}")

        if content_length:
            print(f"Content-Length：{content_length}")

        if result.get("status") == "failed":
            print(
                "失败详情："
                f"{result.get('rawOutput', '无')}"
            )

    return success_count


def main():
    print("=" * 60)
    print("IPTV-Direct Globalping API 自动化诊断")
    print("=" * 60)

    print("测试目标：公开 Mux HLS VOD 测试视频")
    print("注意：这不是真实电视直播源。")

    try:
        # 第一阶段：主 M3U8
        main_measurement, _ = run_stage(
            "第一阶段：主 M3U8 测试",
            MAIN_URL,
        )

        main_valid = successful_results(
            main_measurement
        )

        if not main_valid:
            raise RuntimeError(
                "主 M3U8 没有取得成功的 HTTP 2xx 结果。"
            )

        main_body = extract_body(
            main_valid[0]
        )

        if not main_body:
            raise RuntimeError(
                "主 M3U8 正文为空，无法继续解析。"
            )

        child_url = extract_first_media_url(
            main_body,
            MAIN_URL,
        )

        if not child_url:
            raise RuntimeError(
                "没有找到主 M3U8 中的子播放列表地址。"
            )

        print(f"\n解析到子 M3U8：{child_url}")

        # 第二阶段：子 M3U8
        child_measurement, _ = run_stage(
            "第二阶段：子 M3U8 测试",
            child_url,
        )

        child_valid = successful_results(
            child_measurement
        )

        if not child_valid:
            raise RuntimeError(
                "子 M3U8 没有取得成功的 HTTP 2xx 结果。"
            )

        child_body = extract_body(
            child_valid[0]
        )

        if not child_body:
            raise RuntimeError(
                "子 M3U8 正文为空，无法继续解析。"
            )

        ts_url = extract_first_media_url(
            child_body,
            child_url,
        )

        if not ts_url:
            raise RuntimeError(
                "没有找到子 M3U8 中的 TS 分片地址。"
            )

        print(f"\n解析到 TS 分片：{ts_url}")

        # 第三阶段：TS Range 请求
        range_measurement, _ = run_stage(
            "第三阶段：TS Range 分段读取",
            ts_url,
            extra_headers={
                "Range": "bytes=0-1023",
            },
        )

        range_success = check_range_result(
            range_measurement
        )

        # 最终摘要
        print("\n" + "=" * 60)
        print("最终诊断摘要")
        print("=" * 60)

        print("主 M3U8：HTTP 测试已完成")
        print("子 M3U8：HTTP 测试已完成")

        if range_success:
            print(
                "TS Range：请求通过（HTTP 206），"
                f"{range_success} 个探针确认返回正确的字节范围"
            )
        else:
            print(
                "TS Range：未确认成功"
            )

        print("\n结果解释：")
        print("1. 只代表本次实际参与测量的探针。")
        print("2. HTTP 206 和正确的 Content-Range 证明服务器响应了范围请求。")
        print("3. 这不代表完整 TS 已下载，也不代表真实播放器持续播放成功。")
        print("4. 不能据此证明所有大陆运营商均可播放。")

    except Exception as exc:
        print("\n" + "=" * 60)
        print("测试未完整完成")
        print("=" * 60)
        print(f"错误原因：{exc}")

        sys.exit(1)


if __name__ == "__main__":
    main()

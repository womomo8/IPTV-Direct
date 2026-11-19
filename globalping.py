
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct Globalping 辅助检测模块。

只负责 Globalping API、额度、探针、测量和结构化结果；
不负责本地检测、正式准入、订阅生成或发布。
"""

from __future__ import annotations

import gzip
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://api.globalping.io/v1"
USER_AGENT = "IPTV-Direct-Globalping/1.0"

PASS = "PASS"
FAIL = "FAIL"
INCOMPLETE = "INCOMPLETE"
NA = "N/A"

REQUEST_TIMEOUT = 30
POLL_INTERVAL = 0.8
MAX_POLL_SECONDS = 120
MAX_BODY_BYTES = 10 * 1024

MOBILE_ASNS = {
    9808,
    24445,
    56040,
    56041,
    56042,
    134810,
}

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip",
    "User-Agent": USER_AGENT,
}


class GlobalpingError(RuntimeError):
    """Globalping API 或测量流程异常。"""

    def __init__(
        self,
        message: str,
        status: str = INCOMPLETE,
        *,
        rate_limited: bool = False,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited
        self.response_headers = response_headers or {}


def _status(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _headers_lower(headers: Any) -> dict[str, str]:
    if not hasattr(headers, "items"):
        return {}
    return {str(key).lower(): str(value).strip() for key, value in headers.items()}


def _api_request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    """请求 Globalping API；自动使用 GitHub Secret 中的 Token。"""
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = dict(HEADERS)

    # Token 只从环境变量读取，不写入文件，也不输出到日志。
    token = os.environ.get("GLOBALPING_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            response_headers = _headers_lower(response.headers)
            raw = response.read()
            if response_headers.get("content-encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            parsed = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, parsed, response_headers

    except urllib.error.HTTPError as exc:
        response_headers = _headers_lower(exc.headers)
        raw_error = exc.read()

        if exc.code == 304:
            return 304, None, response_headers

        detail = raw_error.decode("utf-8", errors="replace")[:1500]

        if exc.code == 429:
            rate_info = {
                key: value
                for key, value in response_headers.items()
                if "rate" in key
                or "limit" in key
                or "reset" in key
                or key == "retry-after"
            }
            raise GlobalpingError(
                "Globalping 返回 HTTP 429，已停止后续测量创建。"
                f"响应头：{json.dumps(rate_info, ensure_ascii=False)}；"
                f"响应：{detail}",
                rate_limited=True,
                response_headers=response_headers,
            ) from exc

        raise GlobalpingError(
            f"Globalping API HTTP {exc.code}：{detail}",
            response_headers=response_headers,
        ) from exc

    except urllib.error.URLError as exc:
        raise GlobalpingError(f"无法连接 Globalping API：{exc}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GlobalpingError(f"Globalping API 响应处理失败：{exc}") from exc


def read_limits() -> dict[str, int]:
    """读取当前调用方的实时测量创建额度。"""
    _, data, _ = _api_request(f"{API_BASE}/limits")
    if not isinstance(data, dict):
        raise GlobalpingError("Globalping /limits 未返回 JSON 对象。")

    try:
        details = data["rateLimit"]["measurements"]["create"]
        values = {
            "limit": int(details["limit"]),
            "remaining": int(details["remaining"]),
            "reset": int(details["reset"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GlobalpingError("无法识别 Globalping 实时额度结构，停止检测。") from exc

    if values["limit"] < 0 or values["remaining"] < 0 or values["reset"] < 0:
        raise GlobalpingError("Globalping 返回了无效的额度数值。")

    return values


def _probe_location(probe: dict[str, Any]) -> dict[str, Any]:
    location = probe.get("location")
    if isinstance(location, dict):
        return location
    return probe


def _is_mobile(probe: dict[str, Any]) -> bool:
    location = _probe_location(probe)

    try:
        asn = int(location.get("asn"))
    except (TypeError, ValueError):
        asn = 0

    network = str(location.get("network") or "").strip().lower()

    return (
        asn in MOBILE_ASNS
        or "china mobile" in network
        or "中国移动" in network
        or "china mobile communications" in network
        or "china mobile group" in network
    )


def _allocate_asns(counts: dict[int, int], slots: int) -> dict[int, int]:
    """按在线探针数量轮流分配 ASN，避免单个 ASN 独占所有名额。"""
    allocation: dict[int, int] = {}
    ordered = sorted(counts, key=lambda asn: (-counts[asn], asn))

    while slots > 0:
        progressed = False

        for asn in ordered:
            if allocation.get(asn, 0) < counts[asn]:
                allocation[asn] = allocation.get(asn, 0) + 1
                slots -= 1
                progressed = True

                if slots == 0:
                    break

        if not progressed:
            break

    return allocation


def choose_probes(max_probes: int = 3) -> list[dict[str, Any]]:
    """选择中国大陆普通用户网络探针，移动网络优先，最多三个。"""
    if isinstance(max_probes, bool) or not isinstance(max_probes, int) or max_probes < 1:
        raise ValueError("max_probes 必须是正整数。")

    max_probes = min(max_probes, 3)

    _, data, _ = _api_request(f"{API_BASE}/probes")
    if not isinstance(data, list):
        raise GlobalpingError("Globalping /probes 未返回探针数组。")

    mobile: dict[int, int] = {}
    other: dict[int, int] = {}

    for probe in data:
        if not isinstance(probe, dict):
            continue

        tags = probe.get("tags") or []
        if not isinstance(tags, list) or "eyeball-network" not in tags:
            continue

        location = _probe_location(probe)
        if location.get("country") != "CN":
            continue

        try:
            asn = int(location.get("asn"))
        except (TypeError, ValueError):
            continue

        if asn <= 0:
            continue

        target = mobile if _is_mobile(probe) else other
        target[asn] = target.get(asn, 0) + 1

    if not mobile and not other:
        raise GlobalpingError("没有发现符合要求的中国大陆普通用户网络探针。")

    mobile_slots = min(2, max_probes, sum(mobile.values()))
    allocation = _allocate_asns(mobile, mobile_slots)
    left = max_probes - sum(allocation.values())

    if left:
        extra = _allocate_asns(other, left)
        for asn, count in extra.items():
            allocation[asn] = allocation.get(asn, 0) + count
        left = max_probes - sum(allocation.values())

    if left:
        remaining_mobile = {
            asn: count - allocation.get(asn, 0)
            for asn, count in mobile.items()
            if count - allocation.get(asn, 0) > 0
        }
        extra = _allocate_asns(remaining_mobile, left)
        for asn, count in extra.items():
            allocation[asn] = allocation.get(asn, 0) + count

    locations = [
        {
            "country": "CN",
            "asn": asn,
            "tags": ["eyeball-network"],
            "limit": count,
        }
        for asn, count in sorted(
            allocation.items(),
            key=lambda item: (item[0] not in mobile, item[0]),
        )
        if count > 0
    ]

    if not locations:
        raise GlobalpingError("没有可用的中国大陆 IPv4 探针名额。")

    return locations


def _split_url(url: str) -> dict[str, Any]:
    try:
        parsed = urllib.parse.urlsplit(url)

        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("只支持带主机名的 HTTP/HTTPS URL。")

        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL 不允许包含用户名或密码。")

        port = parsed.port

        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None

        if literal is not None and literal.version != 4:
            raise ValueError("IPv6 字面地址不符合本项目 IPv4 检测要求。")

    except ValueError as exc:
        raise GlobalpingError(f"候选 URL 无效：{exc}", FAIL) from exc

    protocol = parsed.scheme.upper()

    return {
        "target": parsed.hostname,
        "protocol": protocol,
        "port": port or (443 if protocol == "HTTPS" else 80),
        "path": parsed.path or "/",
        "query": parsed.query,
    }


def _create_measurement(
    url: str,
    locations: list[dict[str, Any]] | str,
    budget: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> str:
    """创建测量；只使用本轮额度预算，不重复请求 /limits。"""
    if budget.get("halted"):
        raise GlobalpingError("本轮 Globalping 测量已停止，不再创建新任务。")

    parts = _split_url(url)

    if isinstance(locations, list):
        estimated = sum(int(item.get("limit", 1)) for item in locations)
    else:
        estimated = int(budget["probe_count"])

    if estimated < 1:
        raise GlobalpingError("没有可用探针。")

    remaining = int(budget["remaining"])

    if remaining < estimated:
        raise GlobalpingError(
            f"Globalping 剩余额度不足：剩余 {remaining}，"
            f"本阶段最多需要 {estimated}。"
        )

    request_options: dict[str, Any] = {
        "method": "GET",
        "path": parts["path"],
        "headers": dict(extra_headers or {}),
    }

    if parts["query"]:
        request_options["query"] = parts["query"]

    measurement_options: dict[str, Any] = {
        "protocol": parts["protocol"],
        "port": parts["port"],
        "request": request_options,
    }

    try:
        ipaddress.ip_address(parts["target"])
    except ValueError:
        measurement_options["ipVersion"] = 4

    payload = {
        "type": "http",
        "target": parts["target"],
        "locations": locations,
        "timeout": 20,
        "measurementOptions": measurement_options,
    }

    try:
        status, data, _ = _api_request(
            f"{API_BASE}/measurements",
            method="POST",
            payload=payload,
        )
    except GlobalpingError as exc:
        if exc.rate_limited:
            budget["halted"] = True
        raise

    if status != 202 or not isinstance(data, dict):
        raise GlobalpingError("Globalping 未返回有效的 202 测量创建响应。")

    measurement_id = data.get("id")
    if not isinstance(measurement_id, str) or not measurement_id:
        raise GlobalpingError("Globalping 创建响应缺少有效测量 ID。")

    try:
        actual_count = int(data.get("probesCount", estimated))
    except (TypeError, ValueError):
        actual_count = estimated

    if actual_count < 1 or actual_count > 3:
        raise GlobalpingError(f"Globalping 实际探针数量异常：{actual_count}。")

    budget["remaining"] = max(0, remaining - actual_count)

    if isinstance(locations, list):
        budget["probe_count"] = actual_count

    return measurement_id


def _wait_measurement(measurement_id: str, budget: dict[str, Any]) -> dict[str, Any]:
    """轮询单个测量；每次响应后等待 800 ms，处理 ETag/304。"""
    url = f"{API_BASE}/measurements/{measurement_id}"
    started = time.monotonic()
    etag: str | None = None

    while time.monotonic() - started < MAX_POLL_SECONDS:
        headers = {"If-None-Match": etag} if etag else None

        try:
            status, data, response_headers = _api_request(
                url,
                extra_headers=headers,
            )
        except GlobalpingError as exc:
            if exc.rate_limited:
                budget["halted"] = True

            raise GlobalpingError(
                f"查询测量结果失败：{exc}",
                rate_limited=exc.rate_limited,
            ) from exc

        etag = response_headers.get("etag") or etag

        if status == 304:
            time.sleep(POLL_INTERVAL)
            continue

        if not isinstance(data, dict):
            raise GlobalpingError("Globalping 测量结果不是 JSON 对象。")

        if data.get("status") != "in-progress":
            return data

        time.sleep(POLL_INTERVAL)

    raise GlobalpingError(
        f"测量任务 {measurement_id} 等待超过 {MAX_POLL_SECONDS} 秒。"
    )


def _probe_key(probe: dict[str, Any]) -> tuple[Any, ...]:
    """使用公开探针元数据匹配复用测量；Globalping 不公开稳定 probe ID。"""
    return (
        probe.get("country"),
        probe.get("state"),
        probe.get("city"),
        probe.get("asn"),
        probe.get("network"),
        probe.get("latitude"),
        probe.get("longitude"),
        tuple(probe.get("tags") or []),
        tuple(probe.get("resolvers") or []),
    )


def _result_map(
    measurement: dict[str, Any],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    results = measurement.get("results")

    if not isinstance(results, list):
        raise GlobalpingError("测量结果缺少 results 数组。")

    mapped: dict[tuple[Any, ...], dict[str, Any]] = {}

    for item in results:
        if not isinstance(item, dict):
            continue

        probe = item.get("probe")
        result = item.get("result")

        if not isinstance(probe, dict) or not isinstance(result, dict):
            continue

        key = _probe_key(probe)

        if key in mapped:
            raise GlobalpingError("测量结果出现无法唯一对应的重复探针信息。")

        mapped[key] = item

    if not mapped:
        raise GlobalpingError("测量已结束，但没有返回有效探针结果。")

    return mapped


def _http_ok(result: dict[str, Any]) -> bool:
    if result.get("status") != "finished":
        return False

    try:
        code = int(result.get("statusCode"))
    except (TypeError, ValueError):
        return False

    return 200 <= code < 300


def _ipv4_ok(result: dict[str, Any]) -> bool:
    address = result.get("resolvedAddress")

    if not isinstance(address, str) or not address:
        return False

    try:
        return ipaddress.ip_address(address).version == 4
    except ValueError:
        return False


def _failure_status(result: dict[str, Any]) -> str:
    """只有明确的目标端失败才记 FAIL；探针离线/内部/解析异常记 INCOMPLETE。"""
    state = result.get("status")

    if state == "offline":
        return INCOMPLETE

    if state == "failed" and result.get("failureSource") == "target":
        return FAIL

    return INCOMPLETE


def _aggregate_ipv4(
    results: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, str]:
    """按有效可达证据和明确目标端失败证据聚合 IPv4。"""
    items = [item["result"] for item in results.values()]

    if any(_ipv4_ok(item) for item in items):
        return _status(
            PASS,
            "至少一个探针确认目标 IPv4 可达。",
        )

    if any(_failure_status(item) == FAIL for item in items):
        return _status(
            FAIL,
            "存在能够确认的目标端 IPv4 连接失败证据。",
        )

    return _status(
        INCOMPLETE,
        "没有取得有效 IPv4 可达证据，也没有明确目标端失败证据。",
    )


def _aggregate_http(
    results: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, str]:
    """按有效 HTTP 响应聚合，不把探针侧异常当作目标端失败。"""
    items = [item["result"] for item in results.values()]

    if any(_ipv4_ok(item) and _http_ok(item) for item in items):
        return _status(
            PASS,
            "至少一个 IPv4 探针取得有效 HTTP 2xx 响应。",
        )

    for item in items:
        if item.get("status") != "finished" or not _ipv4_ok(item):
            continue

        try:
            code = int(item.get("statusCode"))
        except (TypeError, ValueError):
            continue

        if code and not 200 <= code < 300:
            return _status(
                FAIL,
                f"目标端实际返回 HTTP {code}，不是成功响应。",
            )

    if any(_failure_status(item) == FAIL for item in items):
        return _status(
            FAIL,
            "存在能够确认的目标端 HTTP 失败证据。",
        )

    return _status(
        INCOMPLETE,
        "没有取得有效 HTTP 成功响应，也没有明确目标端失败证据。",
    )


def _raw_body_bytes(result: dict[str, Any]) -> bytes | None:
    """只接受完整且可无损还原的 UTF-8 HLS 清单正文。"""
    body = result.get("rawBody")

    if not isinstance(body, str) or result.get("truncated"):
        return None

    if "\ufffd" in body:
        return None

    try:
        data = body.encode("utf-8", errors="strict")
        status_code = int(result.get("statusCode"))
    except (UnicodeError, TypeError, ValueError):
        return None

    if len(data) > MAX_BODY_BYTES:
        return None

    if status_code == 200:
        return data

    if status_code != 206:
        return None

    headers = result.get("headers")

    if not isinstance(headers, dict):
        return None

    content_range = next(
        (
            value
            for name, value in headers.items()
            if isinstance(name, str) and name.lower() == "content-range"
        ),
        None,
    )

    if not isinstance(content_range, str):
        return None

    match = re.fullmatch(
        r"bytes\s+(\d+)-(\d+)/(\d+)",
        content_range.strip(),
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    start, end, total = map(int, match.groups())

    if (
        total <= 0
        or start != 0
        or end != total - 1
        or len(data) != total
    ):
        return None

    return data

def _parse_playlist(body: bytes | None, base_url: str) -> dict[str, Any]:
    """按实际 HLS 标签识别清单；仅有 #EXTM3U/#EXTINF 的频道 M3U 不算 HLS。"""
    if body is None:
        raise GlobalpingError("没有取得完整且可解析的播放清单正文。")

    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GlobalpingError(
            "播放清单不是有效 UTF-8 文本。",
            FAIL,
        ) from exc

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if not lines or lines[0] != "#EXTM3U":
        raise GlobalpingError("响应正文不是有效 M3U8 清单。", FAIL)

    hls_tags = (
        "#EXT-X-STREAM-INF:",
        "#EXT-X-TARGETDURATION:",
        "#EXT-X-MEDIA-SEQUENCE:",
        "#EXT-X-PLAYLIST-TYPE:",
        "#EXT-X-I-FRAMES-ONLY",
        "#EXT-X-ENDLIST",
        "#EXT-X-DISCONTINUITY",
        "#EXT-X-VERSION:",
    )

    if not any(line.startswith(hls_tags) for line in lines):
        raise GlobalpingError(
            "正文只有频道订阅 M3U 标记，不是 HLS 媒体清单。",
            FAIL,
        )

    for line in lines:
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            method = re.search(
                r"(?:^|,)\s*METHOD=([^,]+)",
                line,
                flags=re.IGNORECASE,
            )

            if not method or method.group(1).strip().upper() != "NONE":
                raise GlobalpingError("检测到加密 HLS，首期不支持。", FAIL)

        if line.startswith("#EXT-X-MAP:"):
            raise GlobalpingError("检测到 fMP4 HLS，首期不支持。", FAIL)

        if line.startswith(
            ("#EXT-X-BYTERANGE:", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")
        ):
            raise GlobalpingError(
                "检测到首期不支持的 HLS 分片结构。",
                FAIL,
            )

    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        variants: list[str] = []

        for index, line in enumerate(lines[:-1]):
            if line.startswith("#EXT-X-STREAM-INF:"):
                next_line = lines[index + 1]

                if not next_line.startswith("#"):
                    variants.append(urllib.parse.urljoin(base_url, next_line))

        if not variants:
            raise GlobalpingError(
                "Master 清单没有有效的媒体子清单地址。",
                FAIL,
            )

        return {"kind": "master", "child_url": variants[0]}

    segments: list[str] = []
    pending_extinf = False

    for line in lines:
        if line.startswith("#EXTINF:"):
            if pending_extinf:
                raise GlobalpingError(
                    "媒体清单存在未配对的 EXTINF。",
                    FAIL,
                )
            pending_extinf = True

        elif not line.startswith("#"):
            if not pending_extinf:
                raise GlobalpingError(
                    "媒体分片缺少对应的 EXTINF。",
                    FAIL,
                )

            segments.append(urllib.parse.urljoin(base_url, line))
            pending_extinf = False

    if pending_extinf:
        raise GlobalpingError("媒体清单最后一个 EXTINF 缺少分片地址。")

    if not segments:
        raise GlobalpingError("媒体清单没有可验证的媒体分片。", FAIL)

    return {"kind": "media", "segments": segments[:3]}


def _stage(
    url: str,
    locations: list[dict[str, Any]] | str,
    budget: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    measurement_id = _create_measurement(
        url,
        locations,
        budget,
        headers,
    )

    measurement = _wait_measurement(measurement_id, budget)

    if measurement.get("status") != "finished":
        raise GlobalpingError("Globalping 测量未完成。")

    return measurement_id, measurement


def _stage_record(
    measurement_id: str | None,
    status: str,
    reason: str,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "measurement_id": measurement_id,
        "status": status,
        "reason": reason,
        "probe": probe or {},
    }


def _probe_summary(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "country": probe.get("country"),
        "city": probe.get("city"),
        "network": probe.get("network"),
        "asn": probe.get("asn"),
        "latitude": probe.get("latitude"),
        "longitude": probe.get("longitude"),
    }


def _select_hls_response(
    results: dict[tuple[Any, ...], dict[str, Any]],
    base_url: str,
) -> tuple[
    tuple[Any, ...] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    GlobalpingError | None,
]:
    incomplete_seen = False
    last_error: GlobalpingError | None = None

    for key, item in results.items():
        result = item["result"]

        if not _ipv4_ok(result) or not _http_ok(result):
            continue

        body = _raw_body_bytes(result)

        if body is None:
            incomplete_seen = True
            continue

        try:
            parsed = _parse_playlist(body, base_url)
            return key, result, parsed, None

        except GlobalpingError as exc:
            if exc.status == INCOMPLETE:
                incomplete_seen = True
            else:
                last_error = exc

    if incomplete_seen:
        return (
            None,
            None,
            None,
            GlobalpingError("没有探针返回完整可解析的 HLS 清单。"),
        )

    if last_error:
        return None, None, None, last_error

    return (
        None,
        None,
        None,
        GlobalpingError("没有探针取得可用的 IPv4 HTTP HLS 响应。"),
    )


def _run_hls(
    candidate: dict[str, Any],
    locations: list[dict[str, Any]],
    budget: dict[str, Any],
) -> dict[str, Any]:
    url = candidate["normalized_url"]

    checks = {
        "ipv4": _status(INCOMPLETE, "尚未检测"),
        "http": _status(INCOMPLETE, "尚未检测"),
        "hls": _status(INCOMPLETE, "尚未检测"),
        "ts": _status(INCOMPLETE, "尚未检测"),
    }

    stages: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    measurements: list[str] = []

    try:
        main_id, main_measurement = _stage(url, locations, budget)
        measurements.append(main_id)

        main_results = _result_map(main_measurement)

        checks["ipv4"] = _aggregate_ipv4(main_results)
        checks["http"] = _aggregate_http(main_results)

        for key, item in main_results.items():
            probes[str(key)] = _probe_summary(item["probe"])

        selected_key, selected_result, parsed, parse_error = _select_hls_response(
            main_results,
            url,
        )

        if selected_key is None or selected_result is None or parsed is None:
            error = parse_error or GlobalpingError("主 M3U8 未能完成验证。")

            checks["hls"] = _status(error.status, str(error))
            checks["ts"] = _status(
                INCOMPLETE,
                "主/媒体播放清单未完成验证，无法检查 TS 分片。",
            )

            stages["main_playlist"] = _stage_record(
                main_id,
                checks["hls"]["status"],
                checks["hls"]["reason"],
            )

            return {
                "checks": checks,
                "stages": stages,
                "probes": probes,
                "measurements": measurements,
                "error": str(error),
            }

        stages["main_playlist"] = _stage_record(
            main_id,
            PASS,
            "主 M3U8 已读取并按 HLS 结构解析。",
            _probe_summary(main_results[selected_key]["probe"]),
        )

        media_id = main_id
        media_url = url
        media_parsed = parsed

        if parsed["kind"] == "master":
            media_url = parsed["child_url"]

            media_id, media_measurement = _stage(
                media_url,
                main_id,
                budget,
            )

            measurements.append(media_id)
            media_results = _result_map(media_measurement)
            media_item = media_results.get(selected_key)

            if media_item is None:
                raise GlobalpingError(
                    "媒体子清单测量没有返回与主清单相同的探针。"
                )

            media_result = media_item["result"]

            if not _ipv4_ok(media_result) or not _http_ok(media_result):
                try:
                    media_code = int(media_result.get("statusCode"))
                except (TypeError, ValueError):
                    media_code = 0

                if media_code and not 200 <= media_code < 300:
                    raise GlobalpingError(
                        f"媒体子清单返回 HTTP {media_code}。",
                        FAIL,
                    )

                raise GlobalpingError(
                    "同一探针未能完成媒体子清单的 IPv4 HTTP 检测。"
                )

            media_body = _raw_body_bytes(media_result)
            media_parsed = _parse_playlist(media_body, media_url)

            if media_parsed["kind"] != "media":
                raise GlobalpingError(
                    "媒体子清单仍然是 Master 清单，未取得媒体分片列表。",
                    FAIL,
                )

            stages["media_playlist"] = _stage_record(
                media_id,
                PASS,
                "媒体子 M3U8 已在同一探针读取并解析。",
                _probe_summary(media_item["probe"]),
            )

        else:
            stages["media_playlist"] = _stage_record(
                main_id,
                PASS,
                "目标 M3U8 本身是有效媒体播放清单。",
                _probe_summary(main_results[selected_key]["probe"]),
            )

        checks["hls"] = _status(
            PASS,
            "主清单及适用的媒体子清单均已完整读取并解析。",
        )

        checks["ts"] = _status(
            INCOMPLETE,
            "Globalping HTTP rawBody 不提供可证明无损的 TS 二进制数据；"
            "TS 数据有效性由本地 checker.py 检查。",
        )
        stages["ts_binary"] = _stage_record(
            None,
            INCOMPLETE,
            checks["ts"]["reason"],
            _probe_summary(main_results[selected_key]["probe"]),
        )

        return {
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
        }

    except GlobalpingError as exc:
        if exc.rate_limited:
            budget["halted"] = True

        for name in ("ipv4", "http", "hls", "ts"):
            if (
                checks[name]["status"] == INCOMPLETE
                and checks[name]["reason"] == "尚未检测"
            ):
                checks[name] = _status(exc.status, str(exc))

        stages.setdefault(
            "interrupted",
            _stage_record(None, exc.status, str(exc)),
        )

        return {
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
            "error": str(exc),
        }


def _run_direct_ts(
    candidate: dict[str, Any],
    locations: list[dict[str, Any]],
    budget: dict[str, Any],
) -> dict[str, Any]:
    url = candidate["normalized_url"]

    checks = {
        "ipv4": _status(INCOMPLETE, "尚未检测"),
        "http": _status(INCOMPLETE, "尚未检测"),
        "hls": _status(
            NA,
            "已由本地检测识别为直接 TS，HLS 不适用。",
        ),
        "ts": _status(INCOMPLETE, "尚未检测"),
    }

    stages: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    measurements: list[str] = []

    try:
        measurement_id, measurement = _stage(
            url,
            locations,
            budget,
        )

        measurements.append(measurement_id)
        results = _result_map(measurement)

        checks["ipv4"] = _aggregate_ipv4(results)
        checks["http"] = _aggregate_http(results)

        for key, item in results.items():
            probes[str(key)] = _probe_summary(item["probe"])

        selected = None

        for key, item in results.items():
            result = item["result"]

            if _ipv4_ok(result) and _http_ok(result):
                selected = (key, item)
                break

        if selected is None:
            checks["ts"] = _status(
                INCOMPLETE,
                "没有取得可用的 IPv4 HTTP 响应；"
                "Globalping 不负责验证 TS 二进制数据。",
            )
            stages["direct_ts_http"] = _stage_record(
                measurement_id,
                checks["http"]["status"],
                checks["http"]["reason"],
            )
        else:
            key, item = selected
            checks["ts"] = _status(
                INCOMPLETE,
                "Globalping 已完成 IPv4/HTTP 检测，但其 rawBody "
                "不具备可靠的 TS 二进制校验能力；"
                "TS 数据有效性由本地 checker.py 检查。",
            )
            stages["direct_ts_http"] = _stage_record(
                measurement_id,
                PASS,
                "已取得中国大陆探针的 IPv4 HTTP 2xx 响应；"
                "不据此宣称 TS 数据有效。",
                _probe_summary(item["probe"]),
            )
            stages["ts_binary"] = _stage_record(
                None,
                INCOMPLETE,
                checks["ts"]["reason"],
                _probe_summary(item["probe"]),
            )

        return {
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
        }

    except GlobalpingError as exc:
        if exc.rate_limited:
            budget["halted"] = True

        for name in ("ipv4", "http", "ts"):
            if (
                checks[name]["status"] == INCOMPLETE
                and checks[name]["reason"] == "尚未检测"
            ):
                checks[name] = _status(exc.status, str(exc))

        stages.setdefault(
            "interrupted",
            _stage_record(None, exc.status, str(exc)),
        )

        return {
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
            "error": str(exc),
        }


def _overall_status(checks: dict[str, dict[str, str]]) -> str:
    applicable = [
        item["status"]
        for item in checks.values()
        if item["status"] != NA
    ]

    if FAIL in applicable:
        return FAIL

    if INCOMPLETE in applicable:
        return INCOMPLETE

    if applicable and all(value == PASS for value in applicable):
        return PASS

    return INCOMPLETE


def incomplete_result(
    candidate: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    media_type = candidate.get("media_type")

    checks = {
        "ipv4": _status(INCOMPLETE, reason),
        "http": _status(INCOMPLETE, reason),
        "hls": (
            _status(NA, "直接 TS 不适用 HLS 检测。")
            if media_type == "DIRECT_TS"
            else _status(INCOMPLETE, reason)
        ),
        "ts": _status(INCOMPLETE, reason),
    }

    return {
        **candidate,
        "globalping": {
            "status": INCOMPLETE,
            "checks": checks,
            "stages": {},
            "probes": {},
            "measurements": [],
            "error": reason,
        },
    }


def _failed_candidate(
    candidate: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    media_type = candidate.get("media_type")

    checks = {
        "ipv4": _status(FAIL, reason),
        "http": _status(FAIL, reason),
        "hls": (
            _status(NA, "直接 TS 不适用 HLS 检测。")
            if media_type == "DIRECT_TS"
            else _status(FAIL, reason)
        ),
        "ts": _status(FAIL, reason),
    }

    result = {
        "status": FAIL,
        "checks": checks,
        "stages": {},
        "probes": {},
        "measurements": [],
        "error": reason,
    }

    return {**candidate, "globalping": result}


def check_candidates(
    candidates: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """检测 Publisher 传入的完整本地优选队列。"""
    if not isinstance(candidates, list):
        raise TypeError("candidates 必须是列表。")

    if not isinstance(settings, dict):
        raise TypeError("settings 必须是配置对象。")

    max_probes = settings.get("max_probes")

    if (
        isinstance(max_probes, bool)
        or not isinstance(max_probes, int)
        or max_probes < 1
    ):
        raise ValueError("globalping.max_probes 必须是正整数。")

    max_probes = min(max_probes, 3)

    # 先完整核验输入队列。任何一条不符合 Publisher 契约，
    # 都拒绝整个队列，绝不把输入错误伪装成线路检测 FAIL。
    identities: set[tuple[str, str]] = set()

    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise TypeError(
                f"第 {index + 1} 条候选线路必须是对象。"
            )

        channel = candidate.get("channel")

        if (
            not isinstance(channel, str)
            or not channel
            or channel != channel.strip()
        ):
            raise ValueError(
                f"第 {index + 1} 条候选线路缺少有效频道名称。"
            )

        media_type = candidate.get("media_type")

        if media_type not in ("HLS", "DIRECT_TS"):
            raise ValueError(
                f"第 {index + 1} 条候选线路媒体类型无效："
                f"{media_type!r}。"
            )

        url = candidate.get("normalized_url")

        if (
            not isinstance(url, str)
            or not url
            or url != url.strip()
            or any(char.isspace() for char in url)
        ):
            raise ValueError(
                f"第 {index + 1} 条候选线路缺少有效规范化 URL。"
            )

        try:
            parsed = urllib.parse.urlsplit(url)
            scheme = parsed.scheme.lower()
            host = parsed.hostname
            port = parsed.port

            if (
                scheme not in ("http", "https")
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or parsed.netloc.endswith(":")
                or not parsed.path
                or not parsed.path.startswith("/")
            ):
                raise ValueError("URL 格式不符合规范。")

            host = host.lower()

            if ":" in host:
                host = f"[{host}]"

            expected_netloc = host

            if port is not None and not (
                (scheme == "http" and port == 80)
                or (scheme == "https" and port == 443)
            ):
                if not 1 <= port <= 65535:
                    raise ValueError("URL 端口无效。")
                expected_netloc += f":{port}"

            expected_url = (
                f"{scheme}://{expected_netloc}{parsed.path}"
            )

            if parsed.query:
                expected_url += f"?{parsed.query}"

            if parsed.scheme != scheme or parsed.netloc != expected_netloc:
                raise ValueError("URL 尚未规范化。")

            if url != expected_url:
                raise ValueError("URL 不是规范化后的完整地址。")

            # 同时执行 Globalping 自身的 URL 和 IPv4 目标校验。
            _split_url(url)

        except (TypeError, ValueError, GlobalpingError) as exc:
            raise ValueError(
                f"第 {index + 1} 条候选线路 URL 无效：{exc}"
            ) from exc

        identity = (channel, url)

        if identity in identities:
            raise ValueError(
                f"候选队列存在重复线路：频道 {channel}，URL {url}。"
            )

        identities.add(identity)

        checks = candidate.get("checks")

        if not isinstance(checks, dict):
            raise ValueError(
                f"第 {index + 1} 条候选线路缺少本地 checks 检测结果。"
            )

        if media_type == "HLS":
            required_checks = ("ipv4", "http", "hls", "ts")
        else:
            required_checks = ("ipv4", "http", "ts")

            hls_check = checks.get("hls")

            if (
                not isinstance(hls_check, dict)
                or hls_check.get("status") != NA
            ):
                raise ValueError(
                    f"第 {index + 1} 条 DIRECT_TS 候选线路的 "
                    "本地 HLS 状态必须为 N/A。"
                )

        for name in required_checks:
            item = checks.get(name)

            if not isinstance(item, dict) or item.get("status") != PASS:
                state = (
                    item.get("status")
                    if isinstance(item, dict)
                    else "缺少结果"
                )
                raise ValueError(
                    f"第 {index + 1} 条候选线路本地 "
                    f"{name.upper()} 检测未通过：{state}。"
                )

    # 空队列直接返回，不访问 Globalping API。
    if not candidates:
        return []

    # 输入契约全部通过后，才允许访问 Globalping。
    try:
        limits = read_limits()

        if limits["remaining"] < 1:
            raise GlobalpingError("Globalping 当前没有剩余额度。")

        locations = choose_probes(
            min(max_probes, limits["remaining"])
        )

    except GlobalpingError as exc:
        return [
            incomplete_result(candidate, str(exc))
            for candidate in candidates
        ]

    probe_count = sum(
        int(item.get("limit", 1))
        for item in locations
    )

    budget: dict[str, Any] = {
        "remaining": limits["remaining"],
        "probe_count": probe_count,
        "halted": False,
    }

    output: list[dict[str, Any]] = []

    for candidate in candidates:
        if budget["halted"]:
            output.append(
                incomplete_result(
                    candidate,
                    "Globalping 返回 HTTP 429，已停止本轮后续测量创建。",
                )
            )
            continue

         # 第一阶段只需要当前探针数量的额度。
        # HLS 是否需要第二阶段，要等第一阶段解析出 Master 清单后才能确定。
        # 第二阶段额度由 _create_measurement() 在真正创建测量前单独核验。
        required_budget = probe_count

        if budget["remaining"] < required_budget:
            output.append(
                incomplete_result(
                    candidate,
                    (
                        "Globalping 第一阶段额度不足："
                        f"剩余 {budget['remaining']}，"
                        f"本阶段需要 {required_budget}。"
                    ),
                )
            )
            continue

        if candidate["media_type"] == "HLS":
            result = _run_hls(candidate, locations, budget)
        else:
            result = _run_direct_ts(candidate, locations, budget)

        result["status"] = _overall_status(result["checks"])

        output.append({
            **candidate,
            "globalping": result,
        })

    return output


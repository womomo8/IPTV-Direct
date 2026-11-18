
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct Globalping 远程检测、正式准入和订阅发布模块。

负责 Globalping API、额度、探针、测量、正式准入、订阅生成和发布；
不负责本地质量检测、质量比较、优选排序或总流程调度。
"""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import os
import re
import time
import tempfile
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    items = [item["result"] for item in results.values()]

    if any(_ipv4_ok(item) for item in items):
        return _status(
            PASS,
            "至少一个中国大陆探针通过 IPv4 目标解析并完成连接测量。",
        )

    if any(
        item.get("status") == "finished"
        and isinstance(item.get("resolvedAddress"), str)
        and item.get("resolvedAddress")
        and not _ipv4_ok(item)
        for item in items
    ):
        return _status(FAIL, "测量结果未取得 IPv4 目标地址。")

    if any(_failure_status(item) == FAIL for item in items):
        return _status(FAIL, "目标端明确连接失败。")

    return _status(
        INCOMPLETE,
        "探针未能完成 IPv4 测量，不能确认连通性。",
    )


def _aggregate_http(
    results: dict[tuple[Any, ...], dict[str, Any]],
) -> dict[str, str]:
    items = [item["result"] for item in results.values()]

    if any(_ipv4_ok(item) and _http_ok(item) for item in items):
        return _status(PASS, "至少一个 IPv4 探针取得 HTTP 2xx 响应。")

    for item in items:
        if item.get("status") == "finished":
            try:
                code = int(item.get("statusCode"))
            except (TypeError, ValueError):
                continue

            if code and not 200 <= code < 300:
                return _status(
                    FAIL,
                    f"目标返回 HTTP {code}，不是成功响应。",
                )

    if any(_failure_status(item) == FAIL for item in items):
        return _status(FAIL, "目标端 HTTP 请求明确失败。")

    return _status(INCOMPLETE, "探针未能完成 HTTP 请求。")


def _raw_body_bytes(result: dict[str, Any]) -> bytes | None:
    """只接受完整且可无损还原的 UTF-8 文本正文，供 HLS 清单解析使用。"""
    body = result.get("rawBody")

    if not isinstance(body, str) or result.get("truncated"):
        return None

    if "\ufffd" in body:
        return None

    try:
        data = body.encode("utf-8", errors="strict")
    except UnicodeError:
        return None

    if len(data) > MAX_BODY_BYTES:
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
            headers={"Range": "bytes=0-0"},
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
    """检测候选列表；settings 接收 settings.json 的 globalping 区段。"""
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

    output: list[dict[str, Any] | None] = [None] * len(candidates)
    eligible: list[tuple[int, dict[str, Any]]] = []

    # 先按本地 checker 已确认的媒体类型筛除不支持项；
    # 未知类型和无效 URL 不消耗任何 Globalping 额度。
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise TypeError("每条候选线路必须是对象。")

        media_type = candidate.get("media_type")

        if media_type not in ("HLS", "DIRECT_TS"):
            output[index] = _failed_candidate(
                candidate,
                "未知或不支持的媒体类型，不允许 Globalping 准入。",
            )
            continue

        url = candidate.get("normalized_url")

        if not isinstance(url, str) or not url:
            output[index] = _failed_candidate(
                candidate,
                "缺少规范化 URL。",
            )
            continue

        try:
            _split_url(url)
        except GlobalpingError as exc:
            output[index] = _failed_candidate(candidate, str(exc))
            continue

        eligible.append((index, candidate))

    if not eligible:
        return [item for item in output if item is not None]

    # 每轮只读取一次额度；后续测量由本地预算统一管理。
    try:
        limits = read_limits()

        if limits["remaining"] < 1:
            raise GlobalpingError("Globalping 当前没有剩余额度。")

        locations = choose_probes(
            min(max_probes, limits["remaining"])
        )

    except GlobalpingError as exc:
        for index, candidate in eligible:
            output[index] = incomplete_result(candidate, str(exc))

        return [item for item in output if item is not None]

    probe_count = sum(
        int(item.get("limit", 1))
        for item in locations
    )

    budget: dict[str, Any] = {
        "remaining": limits["remaining"],
        "probe_count": probe_count,
        "halted": False,
    }

    for index, candidate in eligible:
        if budget["halted"]:
            output[index] = incomplete_result(
                candidate,
                "Globalping 返回 HTTP 429，已停止本轮后续测量创建。",
            )
            continue

        # HLS 最多两个阶段：主清单、媒体子清单；不再远程下载 TS 二进制。
        # 启动前预留完整两阶段预算，避免 Master 清单检测到一半额度耗尽。
        # Direct TS 只需要一个轻量 HTTP Range 阶段。
        if candidate["media_type"] == "HLS":
            required_stages = 2
        else:
            required_stages = 1

        required_budget = probe_count * required_stages

        if budget["remaining"] < required_budget:
            output[index] = incomplete_result(
                candidate,
                (
                    "Globalping 剩余额度不足以完成整条线路："
                    f"剩余 {budget['remaining']}，"
                    f"预计至少需要 {required_budget}。"
                ),
            )
            continue

        if candidate["media_type"] == "HLS":
            result = _run_hls(candidate, locations, budget)
        else:
            result = _run_direct_ts(candidate, locations, budget)

        result["status"] = _overall_status(result["checks"])
        output[index] = {
            **candidate,
            "globalping": result,
        }

    return [item for item in output if item is not None]
    # ----------------------------------------------------------------------
# 正式准入与频道名额管理
# ----------------------------------------------------------------------

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
MAX_ROUTES_PER_CHANNEL = 2


def _category_map(channels: list[str]) -> dict[str, str]:
    """读取 main 传入的唯一频道分类映射。"""
    mapping = getattr(channels, "category_by_channel", None)

    if mapping is None:
        return {}

    if not isinstance(mapping, dict):
        raise GlobalpingError("频道分类映射格式错误")

    if set(mapping) != set(channels):
        raise GlobalpingError("频道分类映射与目标频道列表不一致")

    return mapping


def _admission_status(checks: Any, name: str) -> str | None:
    """读取单项检测状态。"""
    if not isinstance(checks, dict):
        return None

    item = checks.get(name)

    if not isinstance(item, dict):
        return None

    value = item.get("status")
    return value if isinstance(value, str) else None


def _valid_admission_url(value: Any) -> bool:
    """检查规范化 HTTP/HTTPS 地址。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(char.isspace() for char in value)
    ):
        return False

    try:
        parsed = urlsplit(value)
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
        ):
            return False

        if port is not None and not 1 <= port <= 65535:
            return False

        host = host.lower()

        if ":" in host:
            host = f"[{host}]"

        expected_netloc = host

        if port is not None and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            expected_netloc += f":{port}"

        return (
            parsed.scheme == scheme
            and parsed.netloc == expected_netloc
            and bool(parsed.path)
            and parsed.path.startswith("/")
        )

    except (TypeError, ValueError):
        return False


def _required_admission_checks(
    media_type: str,
) -> tuple[str, ...] | None:
    """按实际媒体类型确定正式准入所需检测项目。"""
    if media_type == "HLS":
        return ("ipv4", "http", "hls", "ts")

    if media_type == "DIRECT_TS":
        return ("ipv4", "http", "ts")

    return None


def _local_admission_reason(
    candidate: dict[str, Any],
) -> str | None:
    """保留原 publisher 的本地正式准入规则。"""
    media_type = candidate.get("media_type")
    required = _required_admission_checks(media_type)

    if required is None:
        return "未知或不支持的媒体类型，不允许准入"

    checks = candidate.get("checks")

    if not isinstance(checks, dict):
        return "缺少本地检测结果"

    if media_type == "DIRECT_TS":
        if _admission_status(checks, "hls") != NA:
            return "直接 TS 的 HLS 检测必须为 N/A"

    for name in required:
        state = _admission_status(checks, name)

        if state != PASS:
            return (
                f"本地 {name.upper()} 检测未通过："
                f"{state or '缺少结果'}"
            )

    return None


def _globalping_admission_reason(
    candidate: dict[str, Any],
) -> str | None:
    """保留原 publisher 的 Globalping 正式准入规则。"""
    remote = candidate.get("globalping")

    if not isinstance(remote, dict):
        return "缺少 Globalping 检测结果"

    overall = remote.get("status")

    if overall == FAIL:
        return "Globalping 检测明确失败"

    if overall not in (PASS, INCOMPLETE):
        return f"Globalping 总体状态无效：{overall}"

    media_type = candidate.get("media_type")
    required = _required_admission_checks(media_type)

    if required is None:
        return "未知或不支持的媒体类型，不允许准入"

    checks = remote.get("checks")

    if not isinstance(checks, dict):
        return "缺少 Globalping 分项检测结果"

    if media_type == "DIRECT_TS":
        if _admission_status(checks, "hls") != NA:
            return "Globalping 直接 TS 的 HLS 状态必须为 N/A"

    states = [
        _admission_status(checks, name)
        for name in required
    ]

    if any(state is None for state in states):
        return "Globalping 缺少适用检测项目的状态"

    if FAIL in states:
        return "Globalping 存在明确失败的检测项目"

    if overall == PASS and any(state != PASS for state in states):
        return "Globalping 总体 PASS 与分项检测结果不一致"

    if overall == INCOMPLETE and any(
        state not in (PASS, INCOMPLETE)
        for state in states
    ):
        return "Globalping INCOMPLETE 状态与分项检测结果不一致"

    return None


def _prepare_admission_candidate(
    candidate: dict[str, Any],
    channels: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """核验候选身份、本地条件和 Globalping 条件。"""
    if not isinstance(candidate, dict):
        return None, "候选线路不是有效对象"

    channel = candidate.get("channel")

    if not isinstance(channel, str) or channel not in channels:
        return None, "频道名称不属于目标频道配置"

    url = candidate.get("normalized_url")

    if not _valid_admission_url(url):
        return None, "缺少有效的规范化播放 URL"

    priority = candidate.get("priority")
    source_order = candidate.get("source_order")

    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or priority < 0
    ):
        return None, "候选线路缺少有效来源优先级"

    if (
        isinstance(source_order, bool)
        or not isinstance(source_order, int)
        or source_order < 0
    ):
        return None, "候选线路缺少有效来源顺序"

    reason = _local_admission_reason(candidate)

    if reason:
        return None, reason

    reason = _globalping_admission_reason(candidate)

    if reason:
        return None, reason

    result = dict(candidate)
    category_map = _category_map(channels)

    if category_map:
        result["category"] = category_map[channel]

    if "domestic_verification" not in result:
        result["domestic_verification"] = {
            "status": "NOT_VERIFIED",
            "reason": "本轮没有中国移动国内真实网络播放验证证据",
        }

    result["selection"] = {
        "status": ACCEPTED,
        "reason": (
            "本地适用检测全部 PASS，"
            f"Globalping 状态为 {result['globalping']['status']}"
        ),
    }

    return result, None


def _channel_counts(
    routes: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, int]:
    """统计各目标频道累计准入线路数量。"""
    counts = {channel: 0 for channel in channels}

    for route in routes:
        channel = route.get("channel")

        if channel in counts:
            counts[channel] += 1

    return counts


def process_admission_batch(
    candidates: list[dict[str, Any]],
    accepted_routes: list[dict[str, Any]],
    channels: list[str],
    settings: dict[str, Any],
) -> dict[str, Any]:
    """
    按 publisher 传入的完整优选顺序执行正式准入。

    不重新排序、不重新比较质量。
    远程失败或准入失败后，继续处理原队列中的下一条候补。
    """
    if not isinstance(candidates, list):
        raise GlobalpingError("candidates 必须是列表")

    if not isinstance(accepted_routes, list):
        raise GlobalpingError("accepted_routes 必须是列表")

    if (
        not isinstance(channels, list)
        or any(not isinstance(item, str) or not item for item in channels)
        or len(channels) != len(set(channels))
    ):
        raise GlobalpingError("channels 必须是有效且不重复的频道名称列表")

    if not isinstance(settings, dict):
        raise GlobalpingError("settings 必须是配置对象")

    selection = settings.get("selection")

    if not isinstance(selection, dict):
        raise GlobalpingError("settings 缺少 selection 配置")

    maximum = selection.get("max_routes_per_channel")

    if maximum != MAX_ROUTES_PER_CHANNEL:
        raise GlobalpingError(
            "每个频道最多保留线路数必须符合 MASTER_PLAN.md：2"
        )

    category_map = _category_map(channels)
    existing = []

    for route in accepted_routes:
        if not isinstance(route, dict):
            raise GlobalpingError("累计准入数据中存在无效线路")

        saved_route = dict(route)

        if category_map and saved_route.get("channel") in category_map:
            saved_route["category"] = category_map[saved_route["channel"]]

        existing.append(saved_route)

    counts = _channel_counts(existing, channels)

    if any(value > MAX_ROUTES_PER_CHANNEL for value in counts.values()):
        raise GlobalpingError("已有累计准入线路超过每频道 2 条上限")

    seen_urls = {
        route.get("normalized_url")
        for route in existing
        if isinstance(route.get("normalized_url"), str)
    }

    retained = list(existing)
    results = []

    for candidate in candidates:
        route, reason = _prepare_admission_candidate(
            candidate,
            channels,
        )

        if route is None:
            results.append({
                "candidate": candidate,
                "status": REJECTED,
                "reason": reason,
            })
            continue

        url = route["normalized_url"]
        channel = route["channel"]

        if url in seen_urls:
            results.append({
                "candidate": candidate,
                "status": REJECTED,
                "reason": "线路 URL 已存在，不重复准入",
            })
            continue

        if counts[channel] >= MAX_ROUTES_PER_CHANNEL:
            results.append({
                "candidate": route,
                "status": REJECTED,
                "reason": "该频道已达到最多保留 2 条线路的上限",
            })
            continue

        seen_urls.add(url)
        retained.append(route)
        counts[channel] += 1

        results.append({
            "candidate": route,
            "status": ACCEPTED,
            "reason": route["selection"]["reason"],
        })

    all_reached = bool(channels) and all(
        counts[channel] >= MAX_ROUTES_PER_CHANNEL
        for channel in channels
    )

    accepted_count = sum(
        result.get("status") == ACCEPTED
        for result in results
    )

    rejected_count = sum(
        result.get("status") == REJECTED
        for result in results
    )

    return {
        "results": results,
        "accepted_routes": retained,
        "channel_counts": counts,
        "all_channels_reached": all_reached,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
    }
def _safe_m3u_attribute(value: Any) -> str:
    """清理 M3U 属性值，避免破坏格式。"""
    if not isinstance(value, str):
        return ""

    value = value.replace("\r", " ").replace("\n", " ").strip()
    return value.replace('"', "&quot;")


def _valid_output_route(
    route: dict[str, Any],
    channels: list[str],
) -> bool:
    """检查最终输出线路。"""
    if not isinstance(route, dict):
        return False

    channel = route.get("channel")

    if channel not in channels:
        return False

    category_map = _category_map(channels)

    if category_map and route.get("category") != category_map[channel]:
        return False

    return (
        _valid_admission_url(route.get("normalized_url"))
        and isinstance(route.get("selection"), dict)
        and route["selection"].get("status") == ACCEPTED
    )


def build_subscriptions(
    routes: list[dict[str, Any]],
    channels: list[str],
) -> dict[str, Any]:
    """使用同一份最终线路数据生成 M3U 和 TXT。"""
    if not isinstance(routes, list):
        raise GlobalpingError("routes 必须是列表")

    if not isinstance(channels, list) or not channels:
        raise GlobalpingError("没有有效目标频道")

    ordered = []

    for route in routes:
        if not _valid_output_route(route, channels):
            raise GlobalpingError("最终准入线路包含无效数据")

        ordered.append(route)

    channel_index = {
        channel: index
        for index, channel in enumerate(channels)
    }

    ordered.sort(
        key=lambda route: channel_index[route["channel"]]
    )

    counts = _channel_counts(ordered, channels)

    if any(value > MAX_ROUTES_PER_CHANNEL for value in counts.values()):
        raise GlobalpingError("最终线路数量超过每频道 2 条上限")

    urls = [route["normalized_url"] for route in ordered]

    if len(urls) != len(set(urls)):
        raise GlobalpingError("最终订阅存在重复 URL")

    m3u_lines = ["#EXTM3U"]
    txt_lines = []
    category_map = _category_map(channels)

    for route in ordered:
        channel = route["channel"]
        url = route["normalized_url"]

        category = (
            category_map[channel]
            if category_map
            else route.get("category", "")
        )

        if any(char in channel for char in ("\r", "\n", ",")):
            raise GlobalpingError("频道名称包含不支持的格式字符")

        attributes = []

        for key, attribute in (
            ("tvg_id", "tvg-id"),
            ("channel", "tvg-name"),
            ("category", "group-title"),
        ):
            raw_value = category if key == "category" else route.get(key)
            value = _safe_m3u_attribute(raw_value)

            if value:
                attributes.append(f'{attribute}="{value}"')

        suffix = (
            " " + " ".join(attributes)
            if attributes
            else ""
        )

        m3u_lines.append(f"#EXTINF:-1{suffix},{channel}")
        m3u_lines.append(url)

        txt_lines.append(f"{channel},{url}")

    m3u_text = "\n".join(m3u_lines) + "\n"
    txt_text = (
        "\n".join(txt_lines) + "\n"
        if txt_lines
        else ""
    )

    validate_subscriptions(m3u_text, txt_text, channels)

    return {
        "m3u": m3u_text,
        "txt": txt_text,
        "route_count": len(ordered),
        "channel_counts": counts,
    }


def validate_subscriptions(
    m3u_text: str,
    txt_text: str,
    channels: list[str],
) -> bool:
    """检查 M3U、TXT 格式及两份订阅内容一致性。"""
    if not isinstance(m3u_text, str) or not m3u_text:
        raise GlobalpingError("M3U 内容为空")

    if not isinstance(txt_text, str):
        raise GlobalpingError("TXT 内容格式无效")

    m3u_lines = m3u_text.splitlines()

    if not m3u_lines or m3u_lines[0] != "#EXTM3U":
        raise GlobalpingError("M3U 文件缺少 #EXTM3U 文件头")

    m3u_entries = []
    index = 1
    category_map = _category_map(channels)

    while index < len(m3u_lines):
        if index + 1 >= len(m3u_lines):
            raise GlobalpingError("M3U 存在不完整的频道条目")

        extinf = m3u_lines[index]
        url = m3u_lines[index + 1]

        if not extinf.startswith("#EXTINF:") or "," not in extinf:
            raise GlobalpingError("M3U 频道条目格式错误")

        channel = extinf.split(",", 1)[1].strip()

        if channel not in channels:
            raise GlobalpingError("M3U 包含非目标频道")

        if category_map:
            attributes = dict(
                re.findall(r'([\w-]+)="([^"]*)"', extinf)
            )
            expected_category = category_map[channel]

            if attributes.get("group-title") != expected_category:
                raise GlobalpingError(
                    f"M3U 频道分类错误：{channel}，"
                    f"要求 {expected_category}"
                )

        if not _valid_admission_url(url):
            raise GlobalpingError("M3U 包含无效播放 URL")

        m3u_entries.append((channel, url))
        index += 2

    if not m3u_entries:
        raise GlobalpingError("订阅没有任何有效线路")

    txt_entries = []

    for line in txt_text.splitlines():
        if not line or "," not in line:
            raise GlobalpingError("TXT 订阅行格式错误")

        channel, url = line.split(",", 1)

        if channel not in channels:
            raise GlobalpingError("TXT 包含非目标频道")

        if not _valid_admission_url(url):
            raise GlobalpingError("TXT 包含无效播放 URL")

        txt_entries.append((channel, url))

    if not txt_entries:
        raise GlobalpingError("TXT 订阅没有任何有效线路")

    if m3u_entries != txt_entries:
        raise GlobalpingError("M3U 与 TXT 的频道和线路内容不一致")

    urls = [url for _, url in m3u_entries]

    if len(urls) != len(set(urls)):
        raise GlobalpingError("订阅中存在重复 URL")

    counts = _channel_counts(
        [{"channel": channel} for channel, _ in m3u_entries],
        channels,
    )

    if any(value > MAX_ROUTES_PER_CHANNEL for value in counts.values()):
        raise GlobalpingError("订阅超过每频道 2 条线路上限")

    return True


def _subscription_snapshot(
    m3u_text: str,
    txt_text: str,
) -> dict[str, int]:
    """从旧订阅自身提取频道和线路数量，并检查两份文件一致性。"""
    if not isinstance(m3u_text, str) or not isinstance(txt_text, str):
        raise GlobalpingError("旧订阅不是有效文本")

    lines = m3u_text.splitlines()

    if not lines or lines[0] != "#EXTM3U":
        raise GlobalpingError("旧 M3U 缺少文件头")

    channels = []
    route_count = 0
    index = 1

    while index < len(lines):
        if index + 1 >= len(lines):
            raise GlobalpingError("旧 M3U 存在不完整条目")

        extinf = lines[index]
        url = lines[index + 1]

        if not extinf.startswith("#EXTINF:") or "," not in extinf:
            raise GlobalpingError("旧 M3U 条目格式错误")

        channel = extinf.split(",", 1)[1].strip()

        if not channel or not _valid_admission_url(url):
            raise GlobalpingError("旧 M3U 包含无效频道或 URL")

        channels.append(channel)
        route_count += 1
        index += 2

    if not channels:
        raise GlobalpingError("旧订阅没有有效线路")

    unique_channels = list(dict.fromkeys(channels))

    validate_subscriptions(
        m3u_text,
        txt_text,
        unique_channels,
    )

    return {
        "channel_count": len(unique_channels),
        "route_count": route_count,
    }
_PUBLISH_STATE_DIR = ".iptv-publish-state"
_PUBLISH_UNKNOWN_FILE = "UNKNOWN.json"
_PUBLISH_LATEST_FILE = "latest.json"


def _write_temp_file(
    directory: Path,
    prefix: str,
    data: bytes,
) -> Path:
    """在目标目录写入并同步临时文件。"""
    fd, filename = tempfile.mkstemp(
        prefix=prefix,
        suffix=".tmp",
        dir=str(directory),
    )
    path = Path(filename)

    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise

    return path


def _write_durable_file(path: Path, data: bytes) -> None:
    """通过同目录临时文件替换持久化文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _write_temp_file(
        path.parent,
        f".{path.name}.",
        data,
    )

    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_subscription_pair(
    m3u_data: bytes,
    txt_data: bytes,
    channels: list[str],
) -> tuple[str, str, dict[str, int]]:
    """读取并验证一组备份订阅自身的完整性。"""
    try:
        m3u_text = m3u_data.decode("utf-8-sig")
        txt_text = txt_data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GlobalpingError("订阅备份不是有效 UTF-8 文件") from exc

    snapshot = _subscription_snapshot(
        m3u_text,
        txt_text,
    )

    return m3u_text, txt_text, snapshot


def _load_latest_backup(
    state_dir: Path,
    channels: list[str],
) -> tuple[Path, bytes, bytes, dict[str, int]] | None:
    """读取最近一次已知良好备份，并验证其内容。"""
    manifest_path = state_dir / _PUBLISH_LATEST_FILE

    if not manifest_path.is_file():
        return None

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GlobalpingError(
            "最近一次已知良好备份索引损坏，禁止发布"
        ) from exc

    if not isinstance(manifest, dict):
        raise GlobalpingError("最近一次已知良好备份索引格式无效")

    generation = manifest.get("generation")

    if (
        not isinstance(generation, str)
        or re.fullmatch(r"[0-9a-f]{32}", generation) is None
    ):
        raise GlobalpingError("备份索引中的版本标识无效")

    backup_dir = state_dir / generation
    m3u_path = backup_dir / "iptv.m3u"
    txt_path = backup_dir / "iptv.txt"

    try:
        m3u_data = m3u_path.read_bytes()
        txt_data = txt_path.read_bytes()
    except OSError as exc:
        raise GlobalpingError(
            "最近一次已知良好备份文件缺失或无法读取"
        ) from exc

    _m3u, _txt, snapshot = _read_subscription_pair(
        m3u_data,
        txt_data,
        channels,
    )

    expected_hashes = manifest.get("sha256")

    actual_hashes = {
        "iptv.m3u": hashlib.sha256(m3u_data).hexdigest(),
        "iptv.txt": hashlib.sha256(txt_data).hexdigest(),
    }

    if expected_hashes != actual_hashes:
        raise GlobalpingError("最近一次已知良好备份校验值不一致")

    return backup_dir, m3u_data, txt_data, snapshot


def _save_known_good_backup(
    state_dir: Path,
    m3u_data: bytes,
    txt_data: bytes,
    channels: list[str],
) -> None:
    """保存一组已验证的双文件备份，并最后更新备份索引。"""
    _read_subscription_pair(
        m3u_data,
        txt_data,
        channels,
    )

    state_dir.mkdir(parents=True, exist_ok=True)

    generation = uuid.uuid4().hex
    backup_dir = state_dir / generation
    backup_dir.mkdir()

    _write_durable_file(
        backup_dir / "iptv.m3u",
        m3u_data,
    )
    _write_durable_file(
        backup_dir / "iptv.txt",
        txt_data,
    )

    saved_m3u = (backup_dir / "iptv.m3u").read_bytes()
    saved_txt = (backup_dir / "iptv.txt").read_bytes()

    if saved_m3u != m3u_data or saved_txt != txt_data:
        raise GlobalpingError("备份写入后的内容核验失败")

    _read_subscription_pair(
        saved_m3u,
        saved_txt,
        channels,
    )

    manifest = {
        "generation": generation,
        "sha256": {
            "iptv.m3u": hashlib.sha256(saved_m3u).hexdigest(),
            "iptv.txt": hashlib.sha256(saved_txt).hexdigest(),
        },
    }

    manifest_data = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
    ).encode("utf-8")

    _write_durable_file(
        state_dir / _PUBLISH_LATEST_FILE,
        manifest_data,
    )


def _write_publish_unknown(
    state_dir: Path,
    reason: str,
) -> None:
    """持久化发布状态未知标记。"""
    state_dir.mkdir(parents=True, exist_ok=True)

    marker = {
        "status": "UNKNOWN",
        "reason": str(reason),
        "timestamp": int(time.time()),
    }

    data = (
        json.dumps(
            marker,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
    ).encode("utf-8")

    _write_durable_file(
        state_dir / _PUBLISH_UNKNOWN_FILE,
        data,
    )


def _verify_published_pair(
    targets: list[Path],
    expected_m3u: bytes,
    expected_txt: bytes,
    channels: list[str],
) -> dict[str, int]:
    """重新读取正式文件并核验内容和统计。"""
    try:
        actual_m3u = targets[0].read_bytes()
        actual_txt = targets[1].read_bytes()
    except OSError as exc:
        raise GlobalpingError("正式订阅文件无法重新读取") from exc

    if actual_m3u != expected_m3u or actual_txt != expected_txt:
        raise GlobalpingError("正式文件内容与预期不一致")

    _m3u, _txt, snapshot = _read_subscription_pair(
        actual_m3u,
        actual_txt,
        channels,
    )

    return snapshot


def _restore_known_good_backup(
    targets: list[Path],
    state_dir: Path,
    channels: list[str],
) -> dict[str, int]:
    """从最近一次已知良好备份恢复，并重新读取核验。"""
    backup = _load_latest_backup(
        state_dir,
        channels,
    )

    if backup is None:
        raise GlobalpingError(
            "发布状态未知，且没有可用的已知良好备份，禁止自动发布"
        )

    _backup_dir, m3u_data, txt_data, snapshot = backup

    staged = []

    try:
        for target, data in zip(
            targets,
            (m3u_data, txt_data),
        ):
            staged.append(
                _write_temp_file(
                    target.parent,
                    f".{target.name}.recovery-",
                    data,
                )
            )

        for temporary, target in zip(staged, targets):
            os.replace(temporary, target)

        actual = _verify_published_pair(
            targets,
            m3u_data,
            txt_data,
            channels,
        )

        if actual != snapshot:
            raise GlobalpingError("恢复后的订阅数量核验失败")

    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)

    return snapshot


def _recover_unknown_publish_state(
    targets: list[Path],
    state_dir: Path,
    channels: list[str],
) -> dict[str, int] | None:
    """发现上次发布状态未知时，必须先恢复再允许新发布。"""
    marker_path = state_dir / _PUBLISH_UNKNOWN_FILE

    if not marker_path.exists():
        return None

    try:
        marker = json.loads(
            marker_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GlobalpingError(
            "发布状态未知标记损坏，禁止自动覆盖"
        ) from exc

    if not isinstance(marker, dict) or marker.get("status") != "UNKNOWN":
        raise GlobalpingError(
            "发布状态未知标记内容无效，禁止自动覆盖"
        )

    snapshot = _restore_known_good_backup(
        targets,
        state_dir,
        channels,
    )

    marker_path.unlink()

    return snapshot


def _restore_previous_publish(
    targets: list[Path],
    old_data: list[bytes | None],
    state_dir: Path,
    channels: list[str],
) -> None:
    """恢复上一版订阅；恢复前先核验持久化备份。"""
    if old_data == [None, None]:
        for target in targets:
            target.unlink(missing_ok=True)

        if any(target.exists() for target in targets):
            raise GlobalpingError("首次发布失败后的文件清理核验失败")

        return

    if old_data == [b"", b""]:
        staged = []

        try:
            for target in targets:
                staged.append(
                    _write_temp_file(
                        target.parent,
                        f".{target.name}.restore-",
                        b"",
                    )
                )

            for temporary, target in zip(staged, targets):
                os.replace(temporary, target)

            if any(target.read_bytes() != b"" for target in targets):
                raise GlobalpingError("空订阅文件恢复后的内容核验失败")

        finally:
            for temporary in staged:
                temporary.unlink(missing_ok=True)

        return

    backup = _load_latest_backup(
        state_dir,
        channels,
    )

    if backup is None:
        raise GlobalpingError("没有可核验的旧版本备份，无法安全回滚")

    _backup_dir, m3u_data, txt_data, snapshot = backup

    if old_data != [m3u_data, txt_data]:
        raise GlobalpingError(
            "旧文件与已知良好备份不一致，拒绝自动回滚"
        )

    _restore_known_good_backup(
        targets,
        state_dir,
        channels,
    )

    actual = _verify_published_pair(
        targets,
        m3u_data,
        txt_data,
        channels,
    )

    if actual != snapshot:
        raise GlobalpingError("回滚后的数量核验失败")


def publish_subscriptions(
    subscriptions: dict[str, Any],
    channels: list[str],
    output_dir: str | Path = "output",
) -> dict[str, Any]:
    """执行正式订阅生成后的安全发布、备份、核验和失败恢复。"""
    if not isinstance(subscriptions, dict):
        raise GlobalpingError("订阅数据格式错误")

    m3u_text = subscriptions.get("m3u")
    txt_text = subscriptions.get("txt")

    validate_subscriptions(
        m3u_text,
        txt_text,
        channels,
    )

    new_snapshot = _subscription_snapshot(
        m3u_text,
        txt_text,
    )

    if subscriptions.get("route_count") != new_snapshot["route_count"]:
        raise GlobalpingError("订阅线路数量统计不一致")

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    targets = [
        directory / "iptv.m3u",
        directory / "iptv.txt",
    ]

    state_dir = directory / _PUBLISH_STATE_DIR

    # 必须先处理上次运行留下的未知状态。
    recovered_snapshot = _recover_unknown_publish_state(
        targets,
        state_dir,
        channels,
    )

    old_data = []

    for target in targets:
        try:
            old_data.append(target.read_bytes())
        except FileNotFoundError:
            old_data.append(None)

    has_m3u = old_data[0] is not None
    has_txt = old_data[1] is not None

    if has_m3u != has_txt:
        raise GlobalpingError(
            "旧订阅只有一份存在有效内容，阻止发布"
        )

    both_empty = (
        old_data[0] == b""
        and old_data[1] == b""
    )

    first_publish = (
        (not has_m3u and not has_txt)
        or both_empty
    )

    old_snapshot = None

    if not first_publish:
        old_m3u, old_txt, old_snapshot = _read_subscription_pair(
            old_data[0],
            old_data[1],
            channels,
        )

        if (
            new_snapshot["channel_count"] * 2
            < old_snapshot["channel_count"]
        ):
            raise GlobalpingError(
                "频道数量下降超过50%，阻止发布"
            )

        if (
            new_snapshot["route_count"] * 2
            < old_snapshot["route_count"]
        ):
            raise GlobalpingError(
                "线路数量下降超过50%，阻止发布"
            )

        # 替换正式文件之前，先把当前有效版本保存为持久化备份。
        _save_known_good_backup(
            state_dir,
            old_data[0],
            old_data[1],
            channels,
        )

    new_data = [
        m3u_text.encode("utf-8"),
        txt_text.encode("utf-8"),
    ]

    staged = []
    rollback_error = None
    publish_error = None

    try:
        for target, data in zip(targets, new_data):
            staged.append(
                _write_temp_file(
                    directory,
                    f".{target.name}.stage-",
                    data,
                )
            )

        for temporary, target in zip(staged, targets):
            os.replace(temporary, target)

        actual_snapshot = _verify_published_pair(
            targets,
            new_data[0],
            new_data[1],
            channels,
        )

        if actual_snapshot != new_snapshot:
            raise GlobalpingError("正式文件数量核验失败")

        # 只有正式文件全部核验通过，才将新版本登记为已知良好版本。
        _save_known_good_backup(
            state_dir,
            new_data[0],
            new_data[1],
            channels,
        )

    except Exception as exc:
        publish_error = exc

        try:
            _restore_previous_publish(
                targets,
                old_data,
                state_dir,
                channels,
            )
        except Exception as restore_exc:
            rollback_error = restore_exc

            try:
                _write_publish_unknown(
                    state_dir,
                    f"发布失败：{publish_error}；回滚失败：{rollback_error}",
                )
            except Exception as marker_exc:
                raise GlobalpingError(
                    "发布失败且回滚失败；发布状态未知标记也无法持久化："
                    f"{marker_exc}"
                ) from publish_error

        if rollback_error is not None:
            # 回滚失败时保留临时文件，供人工检查。
            raise GlobalpingError(
                "发布失败，回滚失败，发布状态未知；"
                f"发布错误：{publish_error}；回滚错误：{rollback_error}"
            ) from publish_error

        raise GlobalpingError(
            f"发布失败，旧文件已恢复：{publish_error}"
        ) from publish_error

    finally:
        if rollback_error is None:
            for temporary in staged:
                temporary.unlink(missing_ok=True)

    return {
        "published": True,
        "status": "PUBLISHED",
        "channel_count": new_snapshot["channel_count"],
        "route_count": new_snapshot["route_count"],
        "m3u_path": str(targets[0]),
        "txt_path": str(targets[1]),
        "first_publish": first_publish,
        "recovered_previous_unknown_state": (
            recovered_snapshot is not None
        ),
        "verified": True,
    }

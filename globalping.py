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
POLL_INTERVAL = 0.8  # 官方要求每次结果响应后至少等待 500 ms
MAX_POLL_SECONDS = 120
MAX_BODY_BYTES = 10 * 1024  # Globalping HTTP rawBody 最多返回前 10 KB
# 与项目现有 Globalping 诊断工具的移动网络识别口径保持一致，
# 同时保留常见中国移动 ASN；最终优先级仍结合探针 network 字段判断。
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
    """请求 Globalping API；JSON 顶层允许对象或数组，304 作为正常状态返回。"""
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = dict(HEADERS)
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

        # urllib 将 304 抛为 HTTPError；它是 ETag 缓存的正常响应。
        if exc.code == 304:
            return 304, None, response_headers

        detail = raw_error.decode("utf-8", errors="replace")[:1500]
        if exc.code == 429:
            rate_info = {
                key: value
                for key, value in response_headers.items()
                if "rate" in key or "limit" in key or "reset" in key or key == "retry-after"
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


def choose_probes(
    max_probes: int = 3,
    excluded_asns: set[int] | None = None,
) -> list[dict[str, Any]]:
    """选择未使用过的中国大陆 eyeball-network ASN 探针。"""
    if excluded_asns is None:
        excluded_asns = set()
    if (
        not isinstance(excluded_asns, set)
        or any(
            isinstance(asn, bool)
            or not isinstance(asn, int)
            or asn <= 0
            for asn in excluded_asns
        )
    ):
        raise ValueError("excluded_asns 必须是正整数 ASN 集合。")
    if (
        isinstance(max_probes, bool)
        or not isinstance(max_probes, int)
        or max_probes < 1
    ):
        raise ValueError("max_probes 必须是正整数。")

    max_probes = min(max_probes, 3)

    _, data, _ = _api_request(f"{API_BASE}/probes")
    if not isinstance(data, list):
        raise GlobalpingError("Globalping /probes 未返回探针数组。")

    eligible: dict[int, dict[str, Any]] = {}

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

        if asn <= 0 or asn in excluded_asns:
            continue

        if asn not in eligible:
            eligible[asn] = {
                "mobile": _is_mobile(probe),
                "count": 0,
            }

        eligible[asn]["count"] += 1

    if not eligible:
        raise GlobalpingError(
            "没有发现符合要求的中国大陆普通用户网络探针。"
        )

    ordered = sorted(
        eligible.items(),
        key=lambda item: (
            not item[1]["mobile"],
            -item[1]["count"],
            item[0],
        ),
    )

    selected = ordered[:max_probes]

    return [
        {
            "country": "CN",
            "asn": asn,
            "tags": ["eyeball-network"],
            "limit": 1,
        }
        for asn, _ in selected
    ]


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


def _remaining_budget() -> int:
    return read_limits()["remaining"]

def _notify_progress(
    progress_callback: Any,
    event: dict[str, Any],
) -> None:
    """发送结构化进度事件；日志回调异常不得影响检测流程。"""
    if not callable(progress_callback):
        return

    try:
        progress_callback(event)
    except Exception:
        pass

def _create_measurement(
    url: str,
    locations: list[dict[str, Any]] | str,
    budget: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> str:
    if budget.get("halted"):
        raise GlobalpingError(
            "本轮 Globalping 测量已停止，不再创建新任务。"
        )

    parts = _split_url(url)

    if isinstance(locations, list):
        estimated = sum(
            int(item.get("limit", 1))
            for item in locations
        )
    else:
        estimated = int(budget["probe_count"])

    if estimated < 1:
        raise GlobalpingError("没有可用探针。")

    try:
        live_remaining = _remaining_budget()
    except GlobalpingError as exc:
        if exc.rate_limited:
            budget["halted"] = True
        raise

    budget["remaining"] = min(
        int(budget["remaining"]),
        live_remaining,
    )

    if budget["remaining"] < estimated:
        budget["halted"] = True
        raise GlobalpingError(
            f"Globalping 剩余额度不足：剩余 {budget['remaining']}，"
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
    except GlobalpingError:
        budget["halted"] = True
        raise

    if status != 202 or not isinstance(data, dict):
        budget["halted"] = True
        raise GlobalpingError(
            "Globalping 未返回有效的 202 测量创建响应，"
            "无法确认测量创建状态，停止本轮后续测量。"
        )

    measurement_id = data.get("id")

    if not isinstance(measurement_id, str) or not measurement_id:
        budget["halted"] = True
        raise GlobalpingError(
            "Globalping 创建响应缺少有效测量 ID，"
            "无法追踪已创建任务，停止本轮后续测量。"
        )

    actual_count = data.get("probesCount")

    if (
        isinstance(actual_count, bool)
        or not isinstance(actual_count, int)
        or actual_count < 1
    ):
        budget["halted"] = True
        raise GlobalpingError(
            "Globalping 创建响应缺少有效的实际探针数量，"
            "额度消耗无法确认，停止本轮后续测量。"
        )

    budget["remaining"] = max(
        0,
        budget["remaining"] - actual_count,
    )

    if budget["remaining"] <= 0:
        budget["halted"] = True

    if actual_count != 1:
        raise GlobalpingError(
            f"Globalping 本次实际返回 {actual_count} 个探针，"
            "要求必须恰好一个；当前线路本阶段不通过。"
        )

    budget["probe_count"] = 1

    return measurement_id

def _wait_measurement(
    measurement_id: str,
    budget: dict[str, Any],
    progress_callback: Any = None,
) -> dict[str, Any]:
    """轮询单个测量；每次响应后等待 800 ms，处理 ETag/304。"""
    url = f"{API_BASE}/measurements/{measurement_id}"
    started = time.monotonic()
    last_report_at = started
    etag: str | None = None

    while time.monotonic() - started < MAX_POLL_SECONDS:
        headers = {"If-None-Match": etag} if etag else None
        try:
            status, data, response_headers = _api_request(url, extra_headers=headers)
        except GlobalpingError as exc:
            if exc.rate_limited:
                budget["halted"] = True
            raise GlobalpingError(f"查询测量结果失败：{exc}", rate_limited=exc.rate_limited) from exc

        etag = response_headers.get("etag") or etag

        if status == 304:
            current_time = time.monotonic()
            if current_time - last_report_at >= 5:
                _notify_progress(
                    progress_callback,
                    {
                        "event": "measurement_polling",
                        "measurement_id": measurement_id,
                        "status": "in-progress",
                        "elapsed_seconds": round(current_time - started, 1),
                    },
                )
                last_report_at = current_time
            time.sleep(POLL_INTERVAL)
            continue

        if not isinstance(data, dict):
            raise GlobalpingError("Globalping 测量结果不是 JSON 对象。")

        measurement_status = data.get("status")
        current_time = time.monotonic()

        if measurement_status != "in-progress":
            _notify_progress(
                progress_callback,
                {
                    "event": "measurement_finished",
                    "measurement_id": measurement_id,
                    "status": measurement_status,
                    "elapsed_seconds": round(current_time - started, 1),
                },
            )
            return data

        if current_time - last_report_at >= 5:
            _notify_progress(
                progress_callback,
                {
                    "event": "measurement_polling",
                    "measurement_id": measurement_id,
                    "status": measurement_status,
                    "elapsed_seconds": round(current_time - started, 1),
                },
            )
            last_report_at = current_time

        time.sleep(POLL_INTERVAL)

    raise GlobalpingError(f"测量任务 {measurement_id} 等待超过 {MAX_POLL_SECONDS} 秒。")

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
    expected_locations: list[dict[str, Any]] | None = None,
    expected_probe_key: tuple[Any, ...] | None = None,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    results = measurement.get("results")
    if not isinstance(results, list):
        raise GlobalpingError("测量结果缺少 results 数组。")

    expected_asns: set[int] | None = None
    expected_count: int | None = None

    if expected_locations is not None:
        if not isinstance(expected_locations, list) or not expected_locations:
            raise GlobalpingError("请求探针列表为空或格式无效。")

        expected_asns = set()
        expected_count = 0

        for location in expected_locations:
            if not isinstance(location, dict):
                raise GlobalpingError("请求探针信息格式无效。")

            asn = location.get("asn")
            limit = location.get("limit", 1)

            if (
                isinstance(asn, bool)
                or not isinstance(asn, int)
                or asn <= 0
                or asn in expected_asns
            ):
                raise GlobalpingError("请求探针 ASN 无效或重复。")

            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit != 1
            ):
                raise GlobalpingError("每个 ASN 阶段只能请求一个探针。")

            if (
                location.get("country") != "CN"
                or "eyeball-network" not in location.get("tags", [])
            ):
                raise GlobalpingError("请求探针不符合中国大陆普通用户网络条件。")

            expected_asns.add(asn)
            expected_count += limit

    if expected_probe_key is not None:
        expected_count = 1

    if expected_count is not None and len(results) != expected_count:
        raise GlobalpingError(
            f"测量实际返回 {len(results)} 个结果，"
            f"要求必须恰好返回 {expected_count} 个。"
        )

    mapped: dict[tuple[Any, ...], dict[str, Any]] = {}
    returned_asns: set[int] = set()

    for item in results:
        if not isinstance(item, dict):
            raise GlobalpingError("测量结果包含无效项目。")

        probe = item.get("probe")
        result = item.get("result")

        if not isinstance(probe, dict) or not isinstance(result, dict):
            raise GlobalpingError("测量结果缺少有效探针信息或检测结果。")

        location = _probe_location(probe)

        country = location.get("country")
        asn = location.get("asn")
        tags = probe.get("tags")

        if country != "CN":
            raise GlobalpingError("实际返回探针不属于中国大陆。")

        if (
            isinstance(asn, bool)
            or not isinstance(asn, int)
            or asn <= 0
        ):
            raise GlobalpingError("实际返回探针 ASN 无效。")

        if not isinstance(tags, list) or "eyeball-network" not in tags:
            raise GlobalpingError("实际返回探针缺少 eyeball-network 标签。")

        if expected_asns is not None and asn not in expected_asns:
            raise GlobalpingError(
                f"实际返回 ASN {asn} 与请求指定的 ASN 不一致。"
            )

        if asn in returned_asns:
            raise GlobalpingError("实际返回结果包含重复 ASN。")

        returned_asns.add(asn)

        key = _probe_key(probe)
        if key in mapped:
            raise GlobalpingError("测量结果出现无法唯一对应的重复探针信息。")

        mapped[key] = item

    if not mapped:
        raise GlobalpingError("测量已结束，但没有返回有效探针结果。")

    if expected_asns is not None and returned_asns != expected_asns:
        raise GlobalpingError("实际返回 ASN 集合与请求指定的 ASN 集合不一致。")

    if expected_probe_key is not None and expected_probe_key not in mapped:
        raise GlobalpingError("测量没有返回本阶段指定的同一个探针。")

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
    if result.get("status") != "finished":
        return False

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


def _aggregate_ipv4(results: dict[tuple[Any, ...], dict[str, Any]]) -> dict[str, str]:
    items = [item["result"] for item in results.values()]
    if any(_ipv4_ok(item) for item in items):
        return _status(PASS, "至少一个中国大陆探针通过 IPv4 目标解析并完成连接测量。")
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
    return _status(INCOMPLETE, "探针未能完成 IPv4 测量，不能确认连通性。")


def _aggregate_http(results: dict[tuple[Any, ...], dict[str, Any]]) -> dict[str, str]:
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
                return _status(FAIL, f"目标返回 HTTP {code}，不是成功响应。")
    if any(_failure_status(item) == FAIL for item in items):
        return _status(FAIL, "目标端 HTTP 请求明确失败。")
    return _status(INCOMPLETE, "探针未能完成 HTTP 请求。")


def _raw_body_bytes(result: dict[str, Any]) -> bytes | None:
    """只接受未截断且可通过 UTF-8 往返还原的正文；不把损坏二进制冒充 TS。"""
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
        raise GlobalpingError("播放清单不是有效 UTF-8 文本。", FAIL) from exc

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
        raise GlobalpingError("正文只有频道订阅 M3U 标记，不是 HLS 媒体清单。", FAIL)

    for line in lines:
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            method = re.search(r"(?:^|,)\s*METHOD=([^,]+)", line, flags=re.IGNORECASE)
            if not method or method.group(1).strip().upper() != "NONE":
                raise GlobalpingError("检测到加密 HLS，首期不支持。", FAIL)
        if line.startswith("#EXT-X-MAP:"):
            raise GlobalpingError("检测到 fMP4 HLS，首期不支持。", FAIL)
        if line.startswith(("#EXT-X-BYTERANGE:", "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:")):
            raise GlobalpingError("检测到首期不支持的 HLS 分片结构。", FAIL)

    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        variants: list[str] = []
        for index, line in enumerate(lines[:-1]):
            if line.startswith("#EXT-X-STREAM-INF:"):
                next_line = lines[index + 1]
                if not next_line.startswith("#"):
                    variants.append(urllib.parse.urljoin(base_url, next_line))
        if not variants:
            raise GlobalpingError("Master 清单没有有效的媒体子清单地址。", FAIL)
        return {"kind": "master", "child_url": variants[0]}

    segments: list[str] = []
    pending_extinf = False
    for line in lines:
        if line.startswith("#EXTINF:"):
            if pending_extinf:
                raise GlobalpingError("媒体清单存在未配对的 EXTINF。", FAIL)
            pending_extinf = True
        elif not line.startswith("#"):
            if not pending_extinf:
                raise GlobalpingError("媒体分片缺少对应的 EXTINF。", FAIL)
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
    progress_callback: Any = None,
    stage_name: str = "measurement",
) -> tuple[str, dict[str, Any]]:
    started = time.monotonic()
    _notify_progress(
        progress_callback,
        {
            "event": "measurement_stage_started",
            "stage_name": stage_name,
        },
    )

    try:
        measurement_id = _create_measurement(
            url, locations, budget, headers
        )
        _notify_progress(
            progress_callback,
            {
                "event": "measurement_created",
                "stage_name": stage_name,
                "measurement_id": measurement_id,
            },
        )

        if progress_callback is None:
            measurement = _wait_measurement(measurement_id, budget)
        else:
            measurement = _wait_measurement(
                measurement_id,
                budget,
                progress_callback=progress_callback,
            )

        if measurement.get("status") != "finished":
            raise GlobalpingError("Globalping 测量未完成。")

        _notify_progress(
            progress_callback,
            {
                "event": "measurement_stage_completed",
                "stage_name": stage_name,
                "measurement_id": measurement_id,
                "status": measurement.get("status"),
                "elapsed_seconds": round(time.monotonic() - started, 1),
            },
        )
        return measurement_id, measurement

    except GlobalpingError as exc:
        _notify_progress(
            progress_callback,
            {
                "event": "measurement_stage_failed",
                "stage_name": stage_name,
                "status": exc.status,
                "reason": str(exc),
                "elapsed_seconds": round(time.monotonic() - started, 1),
            },
        )
        raise

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
) -> tuple[tuple[Any, ...] | None, dict[str, Any] | None, dict[str, Any] | None, GlobalpingError | None]:
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
        return None, None, None, GlobalpingError("没有探针返回完整可解析的 HLS 清单。")
    if last_error:
        return None, None, None, last_error
    return None, None, None, GlobalpingError("没有探针取得可用的 IPv4 HTTP HLS 响应。")

def _run_hls(
    candidate: dict[str, Any],
    locations: list[dict[str, Any]],
    budget: dict[str, Any],
    progress_callback: Any = None,
) -> dict[str, Any]:
    url = candidate["normalized_url"]
    checks = {
        "ipv4": _status(INCOMPLETE, "尚未检测"),
        "http": _status(INCOMPLETE, "尚未检测"),
        "hls": _status(INCOMPLETE, "尚未检测"),
        "ts": _status(NA, "TS 内容由本地 checker.py 检测，Globalping 不重复检测。"),
    }
    stages: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    measurements: list[str] = []

    try:
        if progress_callback is None:
            main_id, main_measurement = _stage(
                url, locations, budget
            )
        else:
            main_id, main_measurement = _stage(
                url,
                locations,
                budget,
                progress_callback=progress_callback,
                stage_name="main_playlist",
            )
        measurements.append(main_id)
        main_results = _result_map(main_measurement, locations)
        checks["ipv4"] = _aggregate_ipv4(main_results)
        checks["http"] = _aggregate_http(main_results)

        for key, item in main_results.items():
            probes[str(key)] = _probe_summary(item["probe"])

        selected_key, selected_result, parsed, parse_error = _select_hls_response(
            main_results, url
        )

        if selected_key is None or selected_result is None or parsed is None:
            error = parse_error or GlobalpingError("主 M3U8 未能完成验证。")
            checks["hls"] = _status(error.status, str(error))
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
            if progress_callback is None:
                media_id, media_measurement = _stage(
                    media_url, main_id, budget
                )
            else:
                media_id, media_measurement = _stage(
                    media_url,
                    main_id,
                    budget,
                    progress_callback=progress_callback,
                    stage_name="media_playlist",
                )

            measurements.append(media_id)
            media_results = _result_map(media_measurement, expected_probe_key=selected_key)
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

        return {
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
        }

    except GlobalpingError as exc:
        if exc.rate_limited:
            budget["halted"] = True

        for name in ("ipv4", "http", "hls"):
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
    progress_callback: Any = None,
) -> dict[str, Any]:
    url = candidate["normalized_url"]
    checks = {
        "ipv4": _status(INCOMPLETE, "尚未检测"),
        "http": _status(INCOMPLETE, "尚未检测"),
        "hls": _status(NA, "直接 TS 不适用 HLS 检测。"),
        "ts": _status(NA, "TS 内容由本地 checker.py 检测，Globalping 不重复检测。"),
    }
    stages: dict[str, Any] = {}
    probes: dict[str, Any] = {}
    measurements: list[str] = []

    try:
        if progress_callback is None:
            measurement_id, measurement = _stage(
                url, locations, budget
            )
        else:
            measurement_id, measurement = _stage(
                url,
                locations,
                budget,
                progress_callback=progress_callback,
                stage_name="direct_ts",
            )

        results = _result_map(measurement, locations)
        checks["ipv4"] = _aggregate_ipv4(results)
        checks["http"] = _aggregate_http(results)

        for key, item in results.items():
            probes[str(key)] = _probe_summary(item["probe"])

        return {
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
        }

    except GlobalpingError as exc:
        if exc.rate_limited:
            budget["halted"] = True

        for name in ("ipv4", "http"):
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

def _overall_status(
    checks: dict[str, dict[str, str]],
    stages: dict[str, dict[str, Any]] | None = None,
) -> str:
    applicable = [
        item["status"]
        for item in checks.values()
        if item["status"] != NA
    ]

    if FAIL in applicable:
        return FAIL

    if INCOMPLETE in applicable:
        return INCOMPLETE

    if not applicable or not all(value == PASS for value in applicable):
        return INCOMPLETE

    if stages is None:
        return PASS

    if len(stages) != 3:
        return INCOMPLETE

    used_asns = set()

    for stage in stages.values():
        if not isinstance(stage, dict):
            return INCOMPLETE

        if stage.get("status") == FAIL:
            return FAIL

        if stage.get("status") != PASS:
            return INCOMPLETE

        asn = stage.get("asn")

        if (
            isinstance(asn, bool)
            or not isinstance(asn, int)
            or asn <= 0
            or asn in used_asns
        ):
            return INCOMPLETE

        used_asns.add(asn)

    return PASS if len(used_asns) == 3 else INCOMPLETE


def _merge_stage_checks(
    candidate: dict[str, Any],
    stages: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    checks = incomplete_result(
        candidate,
        "尚未完成三个不同 ASN 的完整检测。",
    )["globalping"]["checks"]

    for name, current in checks.items():
        if current["status"] == NA:
            continue

        stage_items = [
            stage.get("checks", {}).get(name)
            for stage in stages.values()
            if isinstance(stage.get("checks"), dict)
        ]

        statuses = [
            item.get("status")
            for item in stage_items
            if isinstance(item, dict)
        ]

        failed = next(
            (
                item for item in stage_items
                if isinstance(item, dict)
                and item.get("status") == FAIL
            ),
            None,
        )

        incomplete = next(
            (
                item for item in stage_items
                if isinstance(item, dict)
                and item.get("status") == INCOMPLETE
            ),
            None,
        )

        if failed:
            checks[name] = _status(
                FAIL,
                failed.get("reason", "探针阶段明确失败。"),
            )
        elif (
            len(stages) == 3
            and len(statuses) == 3
            and all(value == PASS for value in statuses)
        ):
            checks[name] = _status(
                PASS,
                "三个不同 ASN 阶段均明确通过。",
            )
        else:
            reason = (
                incomplete.get("reason")
                if incomplete
                else "三个不同 ASN 阶段尚未全部完成并通过。"
            )
            checks[name] = _status(INCOMPLETE, reason)

    return checks


def incomplete_result(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    media_type = candidate.get("media_type")
    checks = {
        "ipv4": _status(INCOMPLETE, reason),
        "http": _status(INCOMPLETE, reason),
        "hls": (
            _status(NA, "直接 TS 不适用 HLS 检测。")
            if media_type == "DIRECT_TS"
            else _status(INCOMPLETE, reason)
        ),
        "ts": _status(
            NA,
            "TS 内容由本地 checker.py 检测，Globalping 不重复检测。",
        ),
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


def _failed_candidate(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    media_type = candidate.get("media_type")
    checks = {
        "ipv4": _status(FAIL, reason),
        "http": _status(FAIL, reason),
        "hls": (
            _status(NA, "直接 TS 不适用 HLS 检测。")
            if media_type == "DIRECT_TS"
            else _status(FAIL, reason)
        ),
        "ts": _status(
            NA,
            "TS 内容由本地 checker.py 检测，Globalping 不重复检测。",
        ),
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


def create_session(settings: dict[str, Any]) -> dict[str, Any]:
    """初始化来源批次共享额度；不预选或共享探针。"""
    if not isinstance(settings, dict):
        raise TypeError("settings 必须是配置对象。")

    max_probes = settings.get("max_probes")

    if (
        isinstance(max_probes, bool)
        or not isinstance(max_probes, int)
        or max_probes < 1
    ):
        raise ValueError("globalping.max_probes 必须是正整数。")

    try:
        limits = read_limits()
        remaining = limits["remaining"]

        if remaining < 1:
            raise GlobalpingError(
                "Globalping 当前没有剩余额度。"
            )

    except GlobalpingError as exc:
        return {
            "budget": {
                "remaining": 0,
                "probe_count": 1,
                "halted": True,
            },
            "error": str(exc),
        }

    return {
        "budget": {
            "remaining": remaining,
            "probe_count": 1,
            "halted": False,
        },
        "error": None,
    }


def check_candidates(
    candidates: list[dict[str, Any]],
    settings: dict[str, Any],
    session: dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """每条线路独立执行三个不同 ASN 的顺序探针阶段。"""
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

    if max_probes < 3:
        for index, candidate in eligible:
            output[index] = incomplete_result(
                candidate,
                "配置的探针阶段不足三个，不能满足三个不同 ASN 全部通过的准入要求。",
            )
        return [item for item in output if item is not None]

    if session is None:
        session = create_session(settings)

    if not isinstance(session, dict):
        raise TypeError("Globalping session 必须是对象。")

    budget = session.get("budget")
    error = session.get("error")

    if (
        not isinstance(budget, dict)
        or not isinstance(budget.get("halted"), bool)
        or isinstance(budget.get("remaining"), bool)
        or not isinstance(budget.get("remaining"), int)
        or isinstance(budget.get("probe_count"), bool)
        or not isinstance(budget.get("probe_count"), int)
        or budget["probe_count"] != 1
        or (error is not None and not isinstance(error, str))
    ):
        raise TypeError("Globalping session 结构无效。")

    if error or budget["halted"]:
        reason = error or (
            "Globalping 额度不足或触发限流，已停止本轮后续测量创建。"
        )

        for index, candidate in eligible:
            output[index] = incomplete_result(candidate, reason)

        return [item for item in output if item is not None]

    for index, candidate in eligible:
        used_asns: set[int] = set()
        stages: dict[str, dict[str, Any]] = {}
        probes: dict[str, Any] = {}
        measurements: list[str] = []
        errors: list[str] = []

        for stage_number in range(1, 4):
            stage_name = f"probe_{stage_number}"
            requested_asn = None

            if budget["halted"]:
                reason = (
                    "Globalping 额度不足或触发限流，"
                    "已停止当前线路剩余探针阶段。"
                )
                stage_checks = incomplete_result(
                    candidate, reason
                )["globalping"]["checks"]

                stages[stage_name] = {
                    "asn": None,
                    "probe": {},
                    "measurement_id": None,
                    "measurement_ids": [],
                    "status": INCOMPLETE,
                    "reason": reason,
                    "checks": stage_checks,
                    "sub_stages": {},
                }
                errors.append(reason)
                break

            try:
                locations = choose_probes(
                    1,
                    excluded_asns=used_asns,
                )

                if not isinstance(locations, list) or len(locations) != 1:
                    raise GlobalpingError(
                        "探针选择结果不是恰好一个探针。"
                    )

                location = locations[0]

                if not isinstance(location, dict):
                    raise GlobalpingError("探针请求信息格式无效。")

                requested_asn = location.get("asn")
                tags = location.get("tags")
                limit = location.get("limit")

                if (
                    isinstance(requested_asn, bool)
                    or not isinstance(requested_asn, int)
                    or requested_asn <= 0
                    or requested_asn in used_asns
                ):
                    raise GlobalpingError(
                        "本阶段探针 ASN 无效或与之前阶段重复。"
                    )

                if (
                    location.get("country") != "CN"
                    or not isinstance(tags, list)
                    or "eyeball-network" not in tags
                    or isinstance(limit, bool)
                    or limit != 1
                ):
                    raise GlobalpingError(
                        "本阶段探针不符合中国大陆普通用户网络单探针条件。"
                    )

                used_asns.add(requested_asn)

                def report_stage_progress(event):
                    _notify_progress(
                        progress_callback,
                        {
                            "candidate_index": index + 1,
                            "candidate_total": len(candidates),
                            "media_type": candidate["media_type"],
                            "stage": stage_name,
                            "asn": requested_asn,
                            **event,
                        },
                    )

                report_stage_progress({
                    "event": "stage_started",
                    "status": "IN_PROGRESS",
                })

                if candidate["media_type"] == "HLS":
                    if progress_callback is None:
                        stage_result = _run_hls(
                            candidate,
                            locations,
                            budget,
                        )
                    else:
                        stage_result = _run_hls(
                            candidate,
                            locations,
                            budget,
                            progress_callback=report_stage_progress,
                        )
                else:
                    if progress_callback is None:
                        stage_result = _run_direct_ts(
                            candidate,
                            locations,
                            budget,
                        )
                    else:
                        stage_result = _run_direct_ts(
                            candidate,
                            locations,
                            budget,
                            progress_callback=report_stage_progress,
                        )

                stage_status = _overall_status(
                    stage_result["checks"]
                )
                report_stage_progress({
                    "event": "stage_completed",
                    "status": stage_status,
                    "reason": stage_result.get("error"),
                    "measurement_ids": stage_result.get("measurements", []),
                })

                probe = next(
                    iter(stage_result.get("probes", {}).values()),
                    {},
                )

                stage_measurements = stage_result.get(
                    "measurements", []
                )

                stage_reason = stage_result.get("error")

                if not stage_reason:
                    stage_reason = (
                        "本 ASN 阶段所有适用检测明确通过。"
                        if stage_status == PASS
                        else "；".join(
                            item["reason"]
                            for item in stage_result["checks"].values()
                            if item["status"] in (FAIL, INCOMPLETE)
                        )
                    )

                stages[stage_name] = {
                    "asn": requested_asn,
                    "probe": probe,
                    "measurement_id": (
                        stage_measurements[0]
                        if stage_measurements
                        else None
                    ),
                    "measurement_ids": stage_measurements,
                    "status": stage_status,
                    "reason": stage_reason,
                    "checks": stage_result["checks"],
                    "sub_stages": stage_result.get("stages", {}),
                }

                if probe:
                    probes[stage_name] = probe

                measurements.extend(stage_measurements)

                if stage_result.get("error"):
                    errors.append(stage_result["error"])

                if stage_status != PASS:
                    break

            except GlobalpingError as exc:
                if exc.rate_limited:
                    budget["halted"] = True

                reason = str(exc)
                stage_checks = incomplete_result(
                    candidate,
                    reason,
                )["globalping"]["checks"]

                if exc.status == FAIL:
                    for name, item in stage_checks.items():
                        if item["status"] != NA:
                            stage_checks[name] = _status(FAIL, reason)

                stages[stage_name] = {
                    "asn": requested_asn,
                    "probe": {},
                    "measurement_id": None,
                    "measurement_ids": [],
                    "status": exc.status,
                    "reason": reason,
                    "checks": stage_checks,
                    "sub_stages": {},
                }

                errors.append(reason)
                break

        checks = _merge_stage_checks(candidate, stages)
        overall = _overall_status(checks, stages)

        if errors:
            error = "；".join(errors)
        elif overall != PASS:
            error = "三个不同 ASN 阶段没有全部明确通过。"
        else:
            error = None

        result = {
            "status": overall,
            "checks": checks,
            "stages": stages,
            "probes": probes,
            "measurements": measurements,
        }

        if error:
            result["error"] = error

        output[index] = {
            **candidate,
            "globalping": result,
        }

    return [item for item in output if item is not None]


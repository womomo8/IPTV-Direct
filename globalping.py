
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
            f"Globalping 剩余额度不足：剩余 {re

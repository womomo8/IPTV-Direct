
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""IPTV-Direct Globalping 正式辅助检测程序。

职责：
- 查询 Globalping 实时额度。
- 选择中国大陆 IPv4 探针，中国移动优先。
- 对本地检测已识别的 HLS / DIRECT_TS 候选执行辅助检测。
- 返回结构化结果，不负责最终准入和订阅发布。

注意：
- 本程序不把 Globalping 结果冒充中国移动家庭宽带实播结果。
- 未完成或无法可靠验证的项目必须返回 INCOMPLETE。
"""

from __future__ import annotations

import ipaddress
import json
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

POLL_INTERVAL = 0.8
MAX_POLL_SECONDS = 120
REQUEST_TIMEOUT = 30
MAX_BODY_BYTES = 10240

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
    """Globalping API 或检测流程异常。"""

    def __init__(self, message: str, status: str = INCOMPLETE):
        super().__init__(message)
        self.status = status


def _status(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _api_request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    data = None
    if payload is not None:
        data = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            raw = response.read()

            if response.headers.get(
                "Content-Encoding", ""
            ).lower() == "gzip":
                import gzip
                raw = gzip.decompress(raw)

            body = raw.decode("utf-8")
            parsed = json.loads(body) if body else {}

            if not isinstance(parsed, dict):
                raise GlobalpingError(
                    "Globalping API 返回的 JSON 不是对象。"
                )

            return (
                response.status,
                parsed,
                dict(response.headers.items()),
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )[:1500]

        if exc.code == 429:
            raise GlobalpingError(
                "Globalping 返回 HTTP 429，停止创建新测量。"
                f"响应：{detail}"
            ) from exc

        raise GlobalpingError(
            f"Globalping API HTTP {exc.code}：{detail}"
        ) from exc

    except urllib.error.URLError as exc:
        raise GlobalpingError(
            f"无法连接 Globalping API：{exc}"
        ) from exc

    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GlobalpingError(
            f"Globalping API 响应处理失败：{exc}"
        ) from exc


def read_limits() -> dict[str, int]:
    """读取实时测量创建额度。"""

    _, data, _ = _api_request(
        f"{API_BASE}/limits"
    )

    try:
        details = data["rateLimit"]["measurements"]["create"]

        result = {
            "limit": int(details["limit"]),
            "remaining": int(details["remaining"]),
            "reset": int(details["reset"]),
        }

    except (KeyError, TypeError, ValueError) as exc:
        raise GlobalpingError(
            "无法识别 Globalping 实时额度结构，停止检测。"
        ) from exc

    if result["remaining"] < 0:
        raise GlobalpingError("Globalping 剩余额度异常。")

    return result


def _is_mobile(probe: dict[str, Any]) -> bool:
    location = probe.get("location") or {}

    try:
        asn = int(location.get("asn"))
    except (TypeError, ValueError):
        asn = 0

    network = str(
        location.get("network") or ""
    ).lower()

    return (
        asn in MOBILE_ASNS
        or "china mobile" in network
        or "中国移动" in network
        or "mobile communications" in network
    )


def choose_probes(max_probes: int = 3) -> list[dict[str, Any]]:
    """选择中国大陆 eyeball-network 探针，移动优先。"""

    if not isinstance(max_probes, int) or max_probes < 1:
        raise ValueError("max_probes 必须是正整数。")

    max_probes = min(max_probes, 3)

    _, data, _ = _api_request(
        f"{API_BASE}/probes"
    )

    if not isinstance(data, list):
        raise GlobalpingError(
            "Globalping 探针列表格式异常。"
        )

    mobile = []
    other = []

    for probe in data:
        if not isinstance(probe, dict):
            continue

        tags = probe.get("tags") or []
        location = probe.get("location") or {}

        if not isinstance(tags, list):
            continue

        if "eyeball-network" not in tags:
            continue

        if not isinstance(location, dict):
            continue

        if location.get("country") != "CN":
            continue

        try:
            asn = int(location.get("asn"))
        except (TypeError, ValueError):
            continue

        item = {
            "country": "CN",
            "asn": asn,
            "tags": ["eyeball-network"],
            "limit": 1,
        }

        if _is_mobile(probe):
            mobile.append(item)
        else:
            other.append(item)

    if not mobile and not other:
        raise GlobalpingError(
            "没有发现符合要求的中国大陆普通用户网络探针。"
        )

    # 先选移动探针，再使用其他大陆运营商补充。
    selected = mobile[:2]

    if len(selected) < max_probes:
        selected.extend(
            other[:max_probes - len(selected)]
        )

    if len(selected) < max_probes:
        selected.extend(
            mobile[len(selected):max_probes]
        )

    # ASN 重复时合并配额，避免重复提交同一位置。
    merged: dict[int, dict[str, Any]] = {}

    for item in selected:
        asn = item["asn"]

        if asn in merged:
            merged[asn]["limit"] += 1
        else:
            merged[asn] = dict(item)

    return list(merged.values())


def _split_url(url: str) -> dict[str, Any]:
    try:
        parsed = urllib.parse.urlsplit(url)

        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("只支持 HTTP/HTTPS。")

        if not parsed.hostname:
            raise ValueError("URL 缺少主机名。")

        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL 不允许包含用户名或密码。")

        port = parsed.port

    except ValueError as exc:
        raise GlobalpingError(
            f"候选 URL 无效：{exc}",
            FAIL,
        ) from exc

    scheme = parsed.scheme.upper()

    return {
        "target": parsed.hostname,
        "protocol": scheme,
        "port": port or (443 if scheme == "HTTPS" else 80),
        "path": parsed.path or "/",
        "query": parsed.query,
    }


def _remaining_budget() -> int:
    return read_limits()["remaining"]


def _create_measurement(
    url: str,
    locations: list[dict[str, Any]] | str,
    budget: dict[str, int],
    extra_headers: dict[str, str] | None = None,
) -> str:
    parts = _split_url(url)

    if isinstance(locations, list):
        estimated = sum(
            int(item.get("limit", 1))
            for item in locations
        )
    else:
        # 复用上一次测量的探针。
        estimated = budget["probe_count"]

    if estimated < 1:
        raise GlobalpingError("没有可用探针。")

    # 每次创建前重新查询实时额度，避免使用过期额度。
    live_remaining = _remaining_budget()
    budget["remaining"] = min(
        budget["remaining"],
        live_remaining,
    )

    if budget["remaining"] < estimated:
        raise GlobalpingError(
            f"Globalping 剩余额度不足："
            f"剩余 {budget['remaining']}，"
            f"本阶段最多需要 {estimated}。"
        )

    request_options: dict[str, Any] = {
        "method": "GET",
        "path": parts["path"],
        "headers": dict(extra_headers or {}),
    }

    if parts["query"]:
        request_options["query"] = parts["query"]

    payload = {
        "type": "http",
        "target": parts["target"],
        "locations": locations,
        "limit": None,
        "measurementOptions": {
            "protocol": parts["protocol"],
            "port": parts["port"],
            "ipVersion": 4,
            "request": request_options,
        },
    }

    # locations 与全局 limit 互斥。
    payload.pop("limit", None)

    status, data, _ = _api_request(
        f"{API_BASE}/measurements",
        method="POST",
        payload=payload,
    )

    measurement_id = data.get("id")

    if status not in (200, 202) or not measurement_id:
        raise GlobalpingError(
            "Globalping 未返回有效测量 ID："
            + json.dumps(data, ensure_ascii=False)[:1000]
        )

    try:
        actual_count = int(data.get("probesCount", estimated))
    except (TypeError, ValueError):
        actual_count = estimated

    if actual_count < 1:
        raise GlobalpingError(
            "Globalping 创建任务后没有可用探针。"
        )

    budget["remaining"] = max(
        0,
        budget["remaining"] - actual_count,
    )

    return str(measurement_id)


def _wait_measurement(
    measurement_id: str,
) -> dict[str, Any]:
    """轮询测量结果，支持 ETag 和 304。"""

    url = f"{API_BASE}/measurements/{measurement_id}"
    started = time.monotonic()
    etag = None

    while time.monotonic() - started < MAX_POLL_SECONDS:
        headers = {}

        if etag:
            headers["If-None-Match"] = etag

        try:
            status, data, response_headers = _api_request(
                url,
                extra_headers=headers,
            )

            etag = (
                response_headers.get("ETag")
                or response_headers.get("Etag")
                or etag
            )

            if status == 304:
                time.sleep(POLL_INTERVAL)
                continue

            if data.get("status") != "in-progress":
                return data

        except GlobalpingError as exc:
            # 轮询阶段遇到 429 或服务错误，不能伪造完成。
            raise GlobalpingError(
                f"查询测量结果失败：{exc}"
            ) from exc

        time.sleep(POLL_INTERVAL)

    raise GlobalpingError(
        f"测量任务 {measurement_id} 等待超过 "
        f"{MAX_POLL_SECONDS} 秒。"
    )


def _result_key(item: dict[str, Any]) -> tuple[Any, ...]:
    probe = item.get("probe") or {}

    return (
        probe.get("id"),
        probe.get("asn"),
        probe.get("city"),
        probe.get("network"),
        probe.get("publicIp"),
    )


def _result_map(
    measurement: dict[str, Any],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    results = measurement.get("results")

    if not isinstance(results, list):
        return {}

    mapped = {}

    for item in results:
        if not isinstance(item, dict):
            continue

        probe = item.get("probe")
        result = item.get("result")

        if not isinstance(probe, dict):
            continue

        if not isinstance(result, dict):
            continue

        mapped[_result_key(item)] = item

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

    if not address:
        return False

    try:
        return ipaddress.ip_address(address).version == 4
    except ValueError:
        return False


def _raw_body_bytes(
    result: dict[str, Any],
) -> bytes | None:
    """只有能够无损还原的 UTF-8 正文才允许进行 TS 字节检查。"""

    body = result.get("rawBody")

    if not isinstance(body, str):
        return None

    if result.get("truncated"):
        return None

    if "\ufffd" in body:
        return None

    try:
        data = body.encode("utf-8")
    except UnicodeError:
        return None

    if len(data) > MAX_BODY_BYTES:
        return None

    return data


def _ts_sync_ok(data: bytes | None) -> bool:
    if data is None:
        return False

    # 连续五个 188 字节间隔同步点。
    if len(data) < 4 * 188 + 1:
        return False

    return all(
        data[index] == 0x47
        for index in (0, 188, 376, 564, 752)
    )


def _parse_playlist(
    body: bytes | None,
    base_url: str,
) -> dict[str, Any]:
    if body is None:
        raise GlobalpingError(
            "Globalping 没有返回可完整验证的播放清单正文。"
        )

    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GlobalpingError(
            "播放清单不是有效 UTF-8 文本。"
        ) from exc

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines or lines[0] != "#EXTM3U":
        raise GlobalpingError(
            "响应正文不是有效 HLS 播放清单。",
            FAIL,
        )

    if any(line.startswith("#EXT-X-KEY") for line in lines):
        raise GlobalpingError(
            "检测到加密 HLS，首期不支持。",
            FAIL,
        )

    if any(
        line.startswith("#EXT-X-MAP")
        for line in lines
    ):
        raise GlobalpingError(
            "检测到 fMP4 初始化段，首期不支持。",
            FAIL,
        )

    is_master = any(
        line.startswith("#EXT-X-STREAM-INF")
        for line in lines
    )

    if is_master:
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for child in lines[index + 1:]:
                    if not child.startswith("#"):
                        return {
                            "kind": "master",
                            "child_url": urllib.parse.urljoin(
                                base_url,
                                child,
                            ),
                        }

        raise GlobalpingError(
            "Master 清单没有可用的媒体子清单地址。"
        )

    segments = []
    pending_segment = False

    for line in lines:
        if line.startswith("#EXTINF"):
            pending_segment = True
            continue

        if pending_segment and not line.startswith("#"):
            segments.append(
                urllib.parse.urljoin(base_url, line)
            )
            pending_segment = False

    if not segments:
        raise GlobalpingError(
            "媒体清单没有可验证的媒体分片地址。"
        )

    return {
        "kind": "media",
        "segments": segments[:3],
    }


def _stage(
    url: str,
    locations: list[dict[str, Any]] | str,
    budget: dict[str, int],
    headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    measurement_id = _create_measurement(
        url,
        locations,
        budget,
        extra_headers=headers,
    )

    measurement = _wait_measurement(measurement_id)

    if measurement.get("status") != "finished":
        raise GlobalpingError(
            "Globalping 测量未完成。"
        )

    return measurement_id, measurement


def _stage_results(
    measurement: dict[str, Any],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    mapped = _result_map(measurement)

    if not mapped:
        raise GlobalpingError(
            "测量已结束，但没有返回有效探针结果。"
        )

    return mapped


def _run_hls(
    candidate: dict[str, Any],
    locations: list[dict[str, Any]],
    budget: dict[str, int],
) -> dict[str, Any]:
    url = candidate["normalized_url"]

    checks = {
        "ipv4": _status(INCOMPLETE, "尚未检测"),
        "http": _status(INCOMPLETE, "尚未检测"),
        "hls": _status(INCOMPLETE, "尚未检测"),
        "ts": _status(INCOMPLETE, "尚未检测"),
    }

    probes: dict[str, Any] = {}
    measurements = []

    try:
        first_id, first = _stage(
            url,
            locations,
            budget,
        )
        measurements.append(first_id)

        first_results = _stage_results(first)

        successful = []

        for key, item in first_results.items():
            result = item["result"]
            probe = item.get("probe") or {}

            probes[str(key)] = {
                "city": probe.get("city"),
                "network": probe.get("network"),
                "asn": probe.get("asn"),
                "public_ip": probe.get("publicIp"),
            }

            if _ipv4_ok(result):
                successful.append(key)

        if successful:
            checks["ipv4"] = _status(
                PASS,
                "至少一个中国大陆探针确认目标解析地址为 IPv4。",
            )
        else:
            checks["ipv4"] = _status(
                FAIL,
                "没有探针取得 IPv4 目标地址。",
            )

        first_body = None
        first_key = None

        for key in successful:
            item = first_results[key]

            if _http_ok(item["result"]):
                first_body = _raw_body_bytes(item["result"])
                first_key = key
                break

        if first_key is None:
            checks["http"] = _status(
                FAIL,
                "没有探针取得 HTTP 2xx 响应。",
            )
            checks["hls"] = _status(
                INCOMPLETE,
                "主清单 HTTP 检测未通过。",
            )
            checks["ts"] = _status(
                INCOMPLETE,
                "未取得可检查的媒体分片。",
            )
            return {
                "checks": checks,
                "probes": probes,
                "measurements": measurements,
            }

        checks["http"] = _status(
            PASS,
            "探针取得 HTTP 2xx 响应。",
        )

        if first_body is None:
            checks["hls"] = _status(
                INCOMPLETE,
                "主清单正文无法完整取得或超过 API 正文限制。",
            )
            checks["ts"] = _status(
                INCOMPLETE,
                "主清单正文不完整，无法继续验证媒体分片。",
            )
            return {
                "checks": checks,
                "probes": probes,
                "measurements": measurements,
            }

        parsed = _parse_playlist(first_body, url)

        if parsed["kind"] == "master":
            checks["hls"] = _status(
                PASS,
                "主 M3U8 已读取并解析出媒体子清单。",
            )
            media_url = parsed["child_url"]

            media_id, media = _stage(
                media_url,
                first_id,
                budget,
            )
            measurements.append(media_id)

            media_results = _stage_results(media)

            media_item = media_results.get(first_key)

            if not media_item or not _http_ok(media_item["result"]):
                checks["hls"] = _status(
                    INCOMPLETE,
                    "未能在同一探针完成媒体子清单验证。",
                )
                checks["ts"] = _status(
                    INCOMPLETE,
                    "媒体子清单未能完成验证。",
                )
                return {
                    "checks": checks,
                    "probes": probes,
                    "measurements": measurements,
                }

            media_body = _raw_body_bytes(media_item["result"])

        else:
            checks["hls"] = _status(
                PASS,
                "目标本身是有效媒体 M3U8。",
            )
            media_url = url
            media_body = first_body

        if media_body is None:
            checks["hls"] = _status(
                INCOMPLETE,
                "媒体清单正文不完整。",
            )
            checks["ts"] = _status(
                INCOMPLETE,
                "无法从不完整媒体清单验证 TS 分片。",
            )
            return {
                "checks": checks,
                "probes": probes,
                "measurements": measurements,
            }

        media_parsed = (
            parsed
            if parsed["kind"] == "media"
            else _parse_playlist(media_body, media_url)
        )

        segment_url = media_parsed["segments"][0]

        range_id, range_result = _stage(
            segment_url,
            first_id,
            budget,
            headers={"Range": "bytes=0-1023"},
        )
        measurements.append(range_id)

        range_results = _stage_results(range_result)
        range_item = range_results.get(first_key)

        if not range_item:
            checks["ts"] = _status(
                INCOMPLETE,
                "TS Range 结果未返回原探针数据。",
            )
        else:
            result = range_item["result"]
            code = result.get("statusCode")
            response_headers = {
                str(k).lower(): str(v).strip()
                for k, v in (result.get("headers") or {}).items()
            }

            content_range = response_headers.get(
                "content-range",
                "",
            )

            body = _raw_body_bytes(result)

            if (
                result.get("status") == "finished"
                and code == 206
                and content_range.startswith("bytes 0-")
                and _ts_sync_ok(body)
            ):
                checks["ts"] = _status(
                    PASS,
                    "TS Range 响应及五个 188 字节同步位置通过。",
                )
            elif result.get("status") != "finished":
                checks["ts"] = _status(
                    INCOMPLETE,
                    "TS Range 测量未完成。",
                )
            elif body is None:
                checks["ts"] = _status(
                    INCOMPLETE,
                    "Globalping 未返回可无损验证的 TS 正文。",
                )
            else:
                checks["ts"] = _status(
                    FAIL,
                    "TS Range 响应或同步结构不符合要求。",
                )

        return {
            "checks": checks,
            "probes": probes,
            "measurements": measurements,
        }

    except GlobalpingError as exc:
        for name in checks:
            if checks[name]["status"] == INCOMPLETE:
                checks[name] = _status(
                    exc.status,
                    str(exc),
                )

        return {
            "checks": checks,
            "probes": probes,
            "measurements": measurements,
            "error": str(exc),
        }


def _run_direct_ts(
    candidate: dict[str, Any],
    locations: list[dict[str, Any]],
    budget: dict[str, int],
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

    probes = {}
    measurements = []

    try:
        measurement_id, measurement = _stage(
            url,
            locations,
            budget,
            headers={"Range": "bytes=0-1023"},
        )
        measurements.append(measurement_id)

        results = _stage_results(measurement)

        for key, item in results.items():
            probe = item.get("probe") or {}
            probes[str(key)] = {
                "city": probe.get("city"),
                "network": probe.get("network"),
                "asn": probe.get("asn"),
                "public_ip": probe.get("publicIp"),
            }

        successful = [
            key for key, item in results.items()
            if _ipv4_ok(item["result"])
        ]

        if successful:
            checks["ipv4"] = _status(
                PASS,
                "探针确认目标地址为 IPv4。",
            )
        else:
            checks["ipv4"] = _status(
                FAIL,
                "没有探针取得 IPv4 目标地址。",
            )

        selected = None

        for key in successful:
            if _http_ok(results[key]["result"]):
                selected = key
                break

        if selected is None:
            checks["http"] = _status(
                FAIL,
                "没有探针取得 HTTP 2xx 响应。",
            )
            checks["ts"] = _status(
                INCOMPLETE,
                "HTTP 检测未通过，未完成 TS 检测。",
            )
        else:
            result = results[selected]["result"]
            checks["http"] = _status(
                PASS,
                "探针取得 HTTP 2xx 响应。",
            )

            code = result.get("statusCode")
            response_headers = {
                str(k).lower(): str(v).strip()
                for k, v in (result.get("headers") or {}).items()
            }
            content_range = response_headers.get(
                "content-range",
                "",
            )
            body = _raw_body_bytes(result)

            range_valid = (
                code == 206
                and content_range.startswith("bytes 0-")
            )

            if (
                result.get("status") == "finished"
                and range_valid
                and _ts_sync_ok(body)
            ):
                checks["ts"] = _status(
                    PASS,
                    "直接 TS Range 数据通过五个 188 字节同步位置检查。",
                )
            elif body is None:
                checks["ts"] = _status(
                    INCOMPLETE,
                    "TS 正文无法完整或无损还原，不能确认同步结构。",
                )
            elif result.get("status") != "finished":
                checks["ts"] = _status(
                    INCOMPLETE,
                    "TS 测量未完成。",
                )
            else:
                checks["ts"] = _status(
                    FAIL,
                    "直接 TS Range 响应或同步结构不符合要求。",
                )

        return {
            "checks": checks,
            "probes": probes,
            "measurements": measurements,
        }

    except GlobalpingError as exc:
        for name in ("ipv4", "http", "ts"):
            if checks[name]["status"] == INCOMPLETE:
                checks[name] = _status(
                    exc.status,
                    str(exc),
                )

        return {
            "checks": checks,
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

    if applicable and all(
        item == PASS for item in applicable
    ):
        return PASS

    return INCOMPLETE


def check_candidates(
    candidates: list[dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """检测候选列表，返回与输入顺序一致的结构化结果。

    settings 接收 settings.json 中的 globalping 区段。
    """

    if not isinstance(candidates, list):
        raise TypeError("candidates 必须是列表。")

    if not isinstance(settings, dict):
        raise TypeError("settings 必须是配置对象。")

    max_probes = settings.get("max_probes", 3)

    if (
        not isinstance(max_probes, int)
        or isinstance(max_probes, bool)
        or max_probes < 1
    ):
        raise ValueError("globalping.max_probes 配置无效。")

    max_probes = min(max_probes, 3)

    output = []

    if not candidates:
        return output

    # 本次调用统一读取额度和探针，失败时所有候选如实标记未完成。
    try:
        limits = read_limits()
        if limits["remaining"] < 1:
            raise GlobalpingError(
                "Globalping 当前没有剩余额度。"
            )

        locations = choose_probes(max_probes)

    except GlobalpingError as exc:
        for candidate in candidates:
            checks = {
                "ipv4": _status(INCOMPLETE, str(exc)),
                "http": _status(INCOMPLETE, str(exc)),
                "hls": _status(INCOMPLETE, str(exc)),
                "ts": _status(INCOMPLETE, str(exc)),
            }

            if candidate.get("media_type") == "DIRECT_TS":
                checks["hls"] = _status(
                    NA,
                    "直接 TS 不适用 HLS 检测。",
                )

            output.append({
                **candidate,
                "globalping": {
                    "status": INCOMPLETE,
                    "checks": checks,
                    "probes": {},
                    "measurements": [],
                    "error": str(exc),
                },
            })

        return output

    budget = {
        "remaining": limits["remaining"],
        "probe_count": sum(
            int(item.get("limit", 1))
            for item in locations
        ),
    }

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        media_type = candidate.get("media_type")
        url = candidate.get("normalized_url")

        if media_type not in ("HLS", "DIRECT_TS"):
            checks = {
                name: _status(
                    FAIL,
                    "未知或不支持的媒体类型，不允许 Globalping 准入。",
                )
                for name in ("ipv4", "http", "hls", "ts")
            }
            result = {
                "checks": checks,
                "probes": {},
                "measurements": [],
                "error": "未知或不支持的媒体类型。",
            }

        elif not isinstance(url, str) or not url:
            checks = {
                name: _status(FAIL, "缺少规范化 URL。")
                for name in ("ipv4", "http", "hls", "ts")
            }
            result = {
                "checks": checks,
                "probes": {},
                "measurements": [],
                "error": "缺少规范化 URL。",
            }

        elif media_type == "HLS":
            result = _run_hls(
                candidate,
                locations,
                budget,
            )

        else:
            result = _run_direct_ts(
                candidate,
                locations,
                budget,
            )

        result["status"] = _overall_status(
            result["checks"]
        )

        output.append({
            **candidate,
            "globalping": result,
        })

    return output

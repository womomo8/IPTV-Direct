#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV-Direct Globalping 远程检测、正式准入及安全发布。

本模块只接受 main.py 传入的 publisher 完整优选队列；不导入、不调用
collector/normalizer/checker/publisher。远程检测失败或证据不足时保留真实状态。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

LOG = logging.getLogger("iptv_direct.globalping")
API = "https://api.globalping.io/v1"
UA = "IPTV-Direct/3.9.5"
PASS, FAIL, INCOMPLETE, NA = "PASS", "FAIL", "INCOMPLETE", "N/A"


class GlobalpingError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: str | None = None, uncertain: bool = False,
                 no_probes: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.uncertain = uncertain
        self.no_probes = no_probes


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    token = os.environ.get("GLOBALPING_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_request(url: str, method: str = "GET", payload: dict[str, Any] | None = None,
                 timeout: float = 20) -> tuple[int, dict[str, Any], dict[str, str]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            body = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            return response.status, body, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        headers = dict(exc.headers.items()) if exc.headers else {}
        try:
            body = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            body = {}
        no_probes = exc.code == 422 and (
            "no_probes_found" in detail.lower() or "no matching" in detail.lower()
        )
        # POST 超时/断连时不能确认服务端是否已创建，调用方不得重试创建。
        raise GlobalpingError(
            f"Globalping API HTTP {exc.code}: {detail[:700]}",
            status=exc.code, retry_after=headers.get("Retry-After"),
            no_probes=no_probes,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GlobalpingError(f"Globalping API 请求或响应处理失败：{exc}", uncertain=(method == "POST")) from exc


def _read_limits() -> dict[str, int]:
    _, body, _ = _api_request(f"{API}/limits")
    try:
        item = body["rateLimit"]["measurements"]["create"]
        return {"limit": int(item["limit"]), "remaining": int(item["remaining"]),
                "reset": int(item.get("reset", 0))}
    except (KeyError, TypeError, ValueError) as exc:
        raise GlobalpingError("无法读取 Globalping 创建额度；为避免超额，停止新增测量") from exc


def _probe_locations(max_probes: int) -> tuple[list[dict[str, Any]], int]:
    """优先中国移动，其他中国大陆 eyeball 探针补足；不把缺少某运营商视为线路失败。"""
    _, probes, _ = _api_request(f"{API}/probes")
    if not isinstance(probes, list):
        raise GlobalpingError("Globalping 探针列表格式异常")
    mobile: Counter[int] = Counter()
    other: Counter[int] = Counter()
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        tags = probe.get("tags") or []
        loc = probe.get("location") or {}
        if "eyeball-network" not in tags or loc.get("country") != "CN":
            continue
        try:
            asn = int(loc.get("asn"))
        except (TypeError, ValueError):
            continue
        network = str(loc.get("network") or "").lower()
        is_mobile = asn in {9808, 24445} or any(
            token in network for token in ("china mobile", "中国移动", "mobile communications")
        )
        (mobile if is_mobile else other)[asn] += 1

    chosen: Counter[int] = Counter()
    slots = max(0, min(int(max_probes), 3))

    def allocate(pool: Counter[int], wanted: int) -> int:
        used = 0
        for asn in sorted(pool, key=lambda value: (-pool[value], value)):
            take = min(pool[asn], wanted - used)
            if take > 0:
                chosen[asn] += take
                used += take
            if used >= wanted:
                break
        return used

    mobile_slots = min(slots, 2)
    used = allocate(mobile, mobile_slots)
    used += allocate(other, slots - used)
    if used < slots:
        remaining_mobile = Counter({asn: max(0, count - chosen[asn]) for asn, count in mobile.items()})
        used += allocate(remaining_mobile, slots - used)

    locations: list[dict[str, Any]] = []
    for asn in sorted(chosen, key=lambda value: (value not in mobile, value)):
        locations.append({"country": "CN", "asn": asn,
                          "tags": ["eyeball-network"], "limit": chosen[asn]})
    return locations, sum(chosen.values())


def _target(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("候选 URL 不是有效 HTTP/HTTPS 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("候选 URL 不允许包含用户名或密码")
    protocol = parsed.scheme.upper()
    return {"target": parsed.hostname, "protocol": protocol,
            "port": parsed.port or (443 if protocol == "HTTPS" else 80),
            "path": (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")}


def _limit_locations(locations: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    remaining = max(0, int(limit))
    selected: list[dict[str, Any]] = []
    for item in locations:
        if remaining <= 0:
            break
        take = min(remaining, int(item.get("limit", 1)))
        if take > 0:
            selected.append({**item, "limit": take})
            remaining -= take
    return selected


def _create_measurement(kind: str, url: str, locations: list[dict[str, Any]],
                        probe_limit: int, previous_measurement: str | None = None) -> tuple[str, int]:
    parts = _target(url)
    if previous_measurement:
        # Globalping 官方支持以之前测量 ID 作为 locations 复用探针的尽力而为机制。
        selected_locations: Any = previous_measurement
    else:
        selected_locations = locations
    if kind == "ping":
        payload = {"type": "ping", "target": parts["target"],
                   "locations": selected_locations,
                   "limit": None if previous_measurement else probe_limit,
                   "measurementOptions": {"packets": 2, "ipVersion": 4}}
    else:
        payload = {"type": "http", "target": parts["target"],
                   "locations": selected_locations,
                   "limit": None if previous_measurement else probe_limit,
                   "measurementOptions": {
                       "protocol": parts["protocol"], "port": parts["port"], "ipVersion": 4,
                       "request": {"method": "GET", "path": parts["path"], "headers": {"Accept-Encoding": "identity"}},
                   }}
    payload = {key: value for key, value in payload.items() if value is not None}
    # 创建请求超时/断连是结果不确定，绝不自动重试 POST。
    status, body, _ = _api_request(f"{API}/measurements", "POST", payload, timeout=20)
    measurement_id = body.get("id")
    if status != 202 or not isinstance(measurement_id, str) or not measurement_id:
        raise GlobalpingError("Globalping 创建测量未返回有效测量 ID")
    try:
        actual_probes = int(body.get("probesCount", probe_limit))
    except (TypeError, ValueError):
        actual_probes = probe_limit
    return measurement_id, max(0, actual_probes)


def _retry_seconds(value: str | None, fallback: float) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    return fallback


def _poll(measurement_id: str, boundary_seconds: float) -> tuple[dict[str, Any] | None, str | None]:
    """单阶段独立轮询边界；429 的 Retry-After 不延长边界。"""
    deadline = time.monotonic() + max(1.0, boundary_seconds)
    delay = 0.5
    etag: str | None = None
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        headers = _headers()
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(f"{API}/measurements/{measurement_id}", headers=headers)
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                etag = response.headers.get("ETag", etag)
                latest = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                pass
            elif exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            else:
                detail = exc.read().decode("utf-8", errors="replace")
                LOG.warning("读取测量 %s 结果失败 HTTP %s：%s", measurement_id, exc.code, detail[:300])
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            LOG.warning("读取测量 %s 结果暂时失败：%s", measurement_id, exc)

        if latest and latest.get("status") == "finished":
            return latest, None
        wait = _retry_seconds(retry_after, delay)
        remaining = deadline - time.monotonic()
        if wait >= remaining:
            break
        time.sleep(wait)
        delay = min(delay * 2, 2.0)
    return latest, "轮询边界耗尽"


def _probe_name(item: dict[str, Any]) -> dict[str, Any]:
    probe = item.get("probe") or {}
    return {"city": probe.get("city"), "country": probe.get("country"),
            "network": probe.get("network"), "asn": probe.get("asn"),
            "continent": probe.get("continent")}


def _ping_aggregate(measurement: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]]]:
    raw_results = measurement.get("results", []) if isinstance(measurement, dict) else []
    evidence: list[dict[str, Any]] = []
    success = False
    explicit_target_failure = False
    for item in raw_results if isinstance(raw_results, list) else []:
        result = item.get("result") or {}
        status = str(result.get("status", "unknown"))
        output = str(result.get("rawOutput", ""))
        # Ping success must include an actual received reply, not merely finished probe status.
        ok = status == "finished" and bool(re.search(r"bytes from|icmp_seq|time[=<]", output, re.I))
        fail_target = status == "finished" and bool(re.search(r"destination (?:host|net) unreachable|connection refused", output, re.I))
        success |= ok
        explicit_target_failure |= fail_target
        evidence.append({"probe": _probe_name(item), "status": status,
                         "raw_output": output[:1000], "success_evidence": ok,
                         "target_failure_evidence": fail_target})
    if success:
        return PASS, evidence
    if explicit_target_failure:
        return FAIL, evidence
    return INCOMPLETE, evidence


def _complete_206(record_body: str, headers: dict[str, str]) -> bool:
    value = headers.get("content-range", "")
    match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", value, re.I)
    if not match:
        return False
    end, total = map(int, match.groups())
    if end + 1 != total:
        return False
    content_length = headers.get("content-length")
    if content_length:
        try:
            if int(content_length) != total:
                return False
        except ValueError:
            return False
    return len(record_body.encode("utf-8")) == total


def _http_aggregate(measurement: dict[str, Any] | None) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    raw_results = measurement.get("results", []) if isinstance(measurement, dict) else []
    evidence: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    explicit_fail = False
    for item in raw_results if isinstance(raw_results, list) else []:
        result = item.get("result") or {}
        status = str(result.get("status", "unknown"))
        code = result.get("statusCode")
        body = result.get("rawBody")
        headers = result.get("headers") or {}
        headers_lc = {str(k).lower(): str(v).strip() for k, v in headers.items()}
        code_ok = isinstance(code, int) and 200 <= code < 300
        # A valid HTTP response is target evidence; probe-side execution errors are not.
        target_response = status == "finished" and isinstance(code, int)
        if target_response and not code_ok:
            explicit_fail = True
        body_text = body if isinstance(body, str) else ""
        complete_response = code != 206 or _complete_206(body_text, headers_lc)
        ok = status == "finished" and code_ok and complete_response
        record = {"probe": _probe_name(item), "status": status, "status_code": code,
                  "headers": headers_lc, "body": body_text,
                  "success_evidence": ok,
                  "target_failure_evidence": bool(target_response and not code_ok),
                  "incomplete_reason": ("HTTP 206 正文或 Content-Range 不完整" if code == 206 and not complete_response else None)}
        evidence.append(record)
        if ok:
            successes.append(record)
    if successes:
        return PASS, evidence, successes
    if explicit_fail:
        return FAIL, evidence, []
    return INCOMPLETE, evidence, []


def _hls_content_complete(record: dict[str, Any]) -> tuple[bool, str]:
    code = record.get("status_code")
    body = record.get("body") or ""
    headers = record.get("headers") or {}
    if not isinstance(body, str) or not body:
        return False, "HLS 清单正文缺失"
    if len(body.encode("utf-8")) >= 10000:
        return False, "Globalping rawBody 接近 10KB 上限，无法确认清单完整"
    content_length = headers.get("content-length")
    if content_length:
        try:
            if len(body.encode("utf-8")) != int(content_length):
                return False, "HLS 清单正文长度与 Content-Length 不一致"
        except ValueError:
            return False, "Content-Length 格式无效"
    if code == 206:
        value = headers.get("content-range", "")
        match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", value, re.I)
        if not match:
            return False, "HTTP 206 缺少从字节 0 开始的完整 Content-Range"
        end, total = map(int, match.groups())
        if end + 1 != total or len(body.encode("utf-8")) != total:
            return False, "HTTP 206 未覆盖完整清单或正文长度不一致"
    elif code != 200:
        return False, f"不支持的清单 HTTP 状态：{code}"
    if not body.lstrip("\ufeff\r\n ").startswith("#EXTM3U"):
        return False, "正文不是有效 M3U 清单"
    return True, "清单正文完整且结构头有效"


def _master_child_url(successful_http: list[dict[str, Any]], base_url: str) -> str | None:
    """仅当正文明确是 HLS 主播放清单时提取第一条子媒体清单。"""
    for record in successful_http:
        complete, _ = _hls_content_complete(record)
        if not complete:
            continue
        lines = [line.strip() for line in (record.get("body") or "").splitlines()]
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                for child in lines[index + 1:]:
                    if child and not child.startswith("#"):
                        return urllib.parse.urljoin(base_url, child)
    return None


def _probe_fingerprint(record: dict[str, Any]) -> str:
    probe = record.get("probe") or {}
    # Globalping 不公开客户端可指定的探针 UUID；第二阶段通过官方
    # locations=<previous measurement id> 复用机制，并比较 API 返回的完整探针描述。
    return json.dumps(probe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hls_aggregate(successful_http: list[dict[str, Any]], all_http: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    hls_evidence = []
    any_complete = False
    explicit_target_failure = False
    for record in all_http:
        code = record.get("status_code")
        if isinstance(code, int) and not 200 <= code < 300:
            explicit_target_failure = True
        if record not in successful_http:
            hls_evidence.append({"probe": record.get("probe"), "status": INCOMPLETE,
                                 "reason": "该探针没有取得可用 HTTP 成功响应"})
            continue
        complete, reason = _hls_content_complete(record)
        body = record.get("body") or ""
        if complete:
            # 主播放清单和普通媒体清单均必须有可识别的 HLS 标签。
            if not any(line.strip().startswith(("#EXT-X-", "#EXTINF:")) for line in body.splitlines()):
                complete, reason = False, "HLS 清单缺少可识别播放条目"
        any_complete |= complete
        hls_evidence.append({"probe": record.get("probe"), "status": PASS if complete else INCOMPLETE,
                             "reason": reason})
    if any_complete:
        return PASS, hls_evidence
    if explicit_target_failure:
        return FAIL, hls_evidence
    return INCOMPLETE, hls_evidence


def _local_admission_reason(candidate: dict[str, Any]) -> str | None:
    media = candidate.get("media_type")
    checks = candidate.get("checks")
    if not isinstance(checks, dict):
        return "缺少本地检测结果"
    required = ("ipv4", "http", "hls", "ts") if media == "HLS" else ("ipv4", "http", "ts") if media == "DIRECT_TS" else None
    if required is None:
        return "媒体类型未知或不支持"
    if media == "DIRECT_TS" and (not isinstance(checks.get("hls"), dict) or checks["hls"].get("status") != NA):
        return "直接 TS 的本地 HLS 状态必须为 N/A"
    for key in required:
        item = checks.get(key)
        if not isinstance(item, dict) or item.get("status") != PASS:
            return f"本地 {key.upper()} 未通过"
    return None


def _remote_admission(candidate: dict[str, Any], remote: dict[str, Any]) -> tuple[bool, str]:
    media = candidate.get("media_type")
    expected = ("ipv4", "http", "hls") if media == "HLS" else ("http",) if media == "DIRECT_TS" else None
    if expected is None:
        return False, "媒体类型无效"
    checks = remote.get("checks")
    if not isinstance(checks, dict):
        return False, "缺少远程分项结果"
    if media == "DIRECT_TS" and (checks.get("ipv4") != NA or checks.get("hls") != NA):
        return False, "直接 TS 的远程 IPv4 和 HLS 状态必须均为 N/A"
    for key in expected:
        state = checks.get(key)
        if state == FAIL:
            return False, f"远程 {key.upper()} 明确失败"
        if state not in {PASS, INCOMPLETE}:
            return False, f"远程 {key.upper()} 缺少有效状态"
    derived = FAIL if any(checks.get(key) == FAIL for key in expected) else PASS if all(checks.get(key) == PASS for key in expected) else INCOMPLETE
    if remote.get("overall") != derived:
        return False, "远程总体状态与适用分项状态不一致"
    # INCOMPLETE 按计划允许降级参与名额竞争，但不改写真实分项状态。
    return True, "远程状态符合正式准入门槛（含真实 INCOMPLETE 降级）"


def _ordered_accepted(accepted: list[dict[str, Any]], channels: list[str]) -> list[dict[str, Any]]:
    """按 channels.txt 的频道顺序组织；频道内保留 Globalping 准入顺序。"""
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for item in accepted:
        by_channel.setdefault(item["channel"], []).append(item)
    ordered: list[dict[str, Any]] = []
    for channel in channels:
        ordered.extend(by_channel.pop(channel, []))
    # 未配置频道按其在最终准入队列中的首次出现顺序追加。
    for group in by_channel.values():
        ordered.extend(group)
    return ordered


def _format_subscriptions(accepted: list[dict[str, Any]], channels: list[str],
                          category_by_channel: dict[str, str], category_order: list[str]) -> tuple[str, str]:
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for item in accepted:
        by_channel.setdefault(item["channel"], []).append(item)
    m3u = ["#EXTM3U"]
    txt: list[str] = []
    # 分类标题来自 channels.txt；空分类仍保留。未在配置中的频道最后追加。
    for category in category_order:
        m3u.append(f"# {category}")
        for channel in channels:
            if category_by_channel.get(channel) != category:
                continue
            for item in by_channel.get(channel, []):
                url = item["normalized_url"]
                m3u.extend([f"#EXTINF:-1,{channel}", url])
                txt.append(f"{channel},{url}")
    for channel, items in by_channel.items():
        if channel in channels:
            continue
        for item in items:
            url = item["normalized_url"]
            m3u.extend([f"#EXTINF:-1,{channel}", url])
            txt.append(f"{channel},{url}")
    return "\n".join(m3u) + "\n", "\n".join(txt) + ("\n" if txt else "")


def _parse_existing(m3u_path: Path, txt_path: Path) -> tuple[bool, int, int, str]:
    exists = [m3u_path.exists(), txt_path.exists()]
    if not any(exists):
        return True, 0, 0, "首次发布"
    try:
        m3u = m3u_path.read_text(encoding="utf-8-sig") if exists[0] else ""
        txt = txt_path.read_text(encoding="utf-8-sig") if exists[1] else ""
    except OSError as exc:
        return False, 0, 0, f"读取旧订阅失败：{exc}"
    if not m3u.strip() and not txt.strip():
        return True, 0, 0, "旧订阅均为空，按首次发布处理"
    if not m3u.strip() or not txt.strip():
        return False, 0, 0, "旧订阅一份缺失/为空而另一份有效，阻止发布"
    if not m3u.lstrip().startswith("#EXTM3U"):
        return False, 0, 0, "旧 M3U 格式无法解析"
    m3u_pairs = []
    lines = m3u.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXTINF:"):
            name = line.split(",", 1)[-1].strip()
            url = lines[i + 1].strip()
            if url.startswith(("http://", "https://")):
                m3u_pairs.append((name, url))
    txt_pairs = []
    for line in txt.splitlines():
        if "," not in line:
            return False, 0, 0, "旧 TXT 格式无法解析"
        name, url = line.split(",", 1)
        if not name.strip() or not url.strip().startswith(("http://", "https://")):
            return False, 0, 0, "旧 TXT 存在无效频道或 URL"
        txt_pairs.append((name.strip(), url.strip()))
    if not m3u_pairs or not txt_pairs or m3u_pairs != txt_pairs:
        return False, 0, 0, "旧 M3U/TXT 无法解析或两份内容不一致"
    return True, len({name for name, _ in txt_pairs}), len(txt_pairs), "旧订阅核验通过"


def _validate_new(m3u: str, txt: str, accepted: list[dict[str, Any]], channels: list[str]) -> tuple[bool, str]:
    if not m3u.startswith("#EXTM3U\n"):
        return False, "新 M3U 缺少 #EXTM3U 头"
    m3u_pairs = []
    lines = m3u.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXTINF:"):
            name = line.split(",", 1)[-1].strip()
            url = lines[i + 1].strip()
            m3u_pairs.append((name, url))
    txt_pairs = []
    for line in txt.splitlines():
        if "," not in line:
            return False, "新 TXT 行格式无效"
        name, url = line.split(",", 1)
        txt_pairs.append((name.strip(), url.strip()))
    expected = [(item["channel"], item["normalized_url"])
                for item in _ordered_accepted(accepted, channels)]
    if m3u_pairs != expected or txt_pairs != expected or m3u_pairs != txt_pairs:
        return False, "新 M3U/TXT 与最终准入数据不一致"
    if len(set(expected)) != len(expected):
        return False, "新订阅存在重复频道线路"
    counts = Counter(name for name, _ in expected)
    if any(count > 2 for count in counts.values()):
        return False, "新订阅违反每频道最多两条线路限制"
    return True, "新订阅格式、身份、重复项及两份一致性检查通过"


def _recover_unknown(output_dir: Path) -> tuple[bool, str]:
    """发布状态未知时，优先恢复最近一次已知良好文件并核验。"""
    state_dir = output_dir / ".iptv-publish-state"
    marker = state_dir / "publication-unknown.json"
    if not marker.exists():
        return True, "无状态未知标记"
    known = state_dir / "known-good"
    m3u_backup, txt_backup = known / "iptv.m3u", known / "iptv.txt"
    if not m3u_backup.exists() or not txt_backup.exists():
        return False, "存在发布状态未知标记，但没有完整的已知良好备份，需人工确认"
    try:
        m3u_text = m3u_backup.read_text(encoding="utf-8-sig")
        txt_text = txt_backup.read_text(encoding="utf-8-sig")
        if not m3u_text.lstrip().startswith("#EXTM3U") or not txt_text.strip():
            return False, "已知良好备份无法解析，需人工确认"
        m3u_pairs = []
        lines = m3u_text.splitlines()
        for index, line in enumerate(lines[:-1]):
            if line.startswith("#EXTINF:"):
                m3u_pairs.append((line.split(",", 1)[-1].strip(), lines[index + 1].strip()))
        txt_pairs = [tuple(line.split(",", 1)) for line in txt_text.splitlines() if "," in line]
        if not m3u_pairs or m3u_pairs != txt_pairs:
            return False, "已知良好备份两份内容不一致，需人工确认"
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m3u_backup, output_dir / "iptv.m3u")
        shutil.copy2(txt_backup, output_dir / "iptv.txt")
        if (output_dir / "iptv.m3u").read_text(encoding="utf-8") != m3u_text or (output_dir / "iptv.txt").read_text(encoding="utf-8") != txt_text:
            return False, "恢复后重新读取核验失败，需人工确认"
        marker.unlink(missing_ok=True)
        return True, "已从最近一次已知良好备份恢复并核验"
    except OSError as exc:
        return False, f"自动恢复失败：{exc}"


def _publish(accepted: list[dict[str, Any]], output_dir: Path, channels: list[str],
             category_by_channel: dict[str, str], category_order: list[str]) -> dict[str, Any]:
    if not accepted:
        return {"status": "blocked", "reason": "没有正式准入线路，拒绝覆盖现有订阅"}
    output_dir.mkdir(parents=True, exist_ok=True)
    m3u_path, txt_path = output_dir / "iptv.m3u", output_dir / "iptv.txt"
    state_dir = output_dir / ".iptv-publish-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    unknown_marker = state_dir / "publication-unknown.json"
    recovered, recovery_reason = _recover_unknown(output_dir)
    if not recovered:
        return {"status": "blocked", "reason": recovery_reason}

    m3u, txt = _format_subscriptions(accepted, channels, category_by_channel, category_order)
    valid, reason = _validate_new(m3u, txt, accepted, channels)
    if not valid:
        return {"status": "blocked", "reason": reason}
    old_valid, old_channels, old_routes, old_reason = _parse_existing(m3u_path, txt_path)
    if not old_valid:
        return {"status": "blocked", "reason": old_reason}
    if m3u_path.exists() and txt_path.exists():
        try:
            known_good = state_dir / "known-good"
            known_good.mkdir(parents=True, exist_ok=True)
            shutil.copy2(m3u_path, known_good / "iptv.m3u")
            shutil.copy2(txt_path, known_good / "iptv.txt")
        except OSError as exc:
            return {"status": "blocked", "reason": f"无法建立已知良好发布备份：{exc}"}
    new_channels = len({item["channel"] for item in accepted})
    new_routes = len(accepted)
    if old_channels and new_channels < old_channels * 0.5:
        return {"status": "blocked", "reason": f"频道数量下降超过 50%（{old_channels}→{new_channels}）"}
    if old_routes and new_routes < old_routes * 0.5:
        return {"status": "blocked", "reason": f"线路数量下降超过 50%（{old_routes}→{new_routes}）"}
    if m3u_path.exists() and txt_path.exists():
        try:
            if m3u_path.read_text(encoding="utf-8-sig") == m3u and txt_path.read_text(encoding="utf-8-sig") == txt:
                return {"status": "unchanged", "reason": "新旧订阅完全一致，无需替换", "channels": new_channels, "routes": new_routes}
        except OSError:
            pass

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_dir = state_dir / f"backup-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    staged_m3u = output_dir / ".iptv.m3u.tmp"
    staged_txt = output_dir / ".iptv.txt.tmp"
    backup_m3u, backup_txt = backup_dir / "iptv.m3u", backup_dir / "iptv.txt"
    had_m3u, had_txt = m3u_path.exists(), txt_path.exists()
    m3u_replaced = False
    txt_replaced = False
    preserve_staged = False

    try:
        staged_m3u.write_text(m3u, encoding="utf-8")
        staged_txt.write_text(txt, encoding="utf-8")

        # 再读临时文件验证，避免写入不完整内容。
        if staged_m3u.read_text(encoding="utf-8") != m3u:
            raise OSError("临时 M3U 写入后核验不一致")
        if staged_txt.read_text(encoding="utf-8") != txt:
            raise OSError("临时 TXT 写入后核验不一致")

        # 保存旧文件备份。
        if had_m3u:
            shutil.copy2(m3u_path, backup_m3u)
        if had_txt:
            shutil.copy2(txt_path, backup_txt)

        # 正式替换之前，完整检查备份是否存在、可读、可解析且两份一致。
        backup_valid, backup_channels, backup_routes, backup_reason = (
            _parse_existing(backup_m3u, backup_txt)
        )
        if not backup_valid:
            raise OSError(f"发布前备份核验失败：{backup_reason}")

        if backup_m3u.exists() != had_m3u or backup_txt.exists() != had_txt:
            raise OSError("发布前备份文件与原文件存在状态不一致")

        if backup_channels != old_channels or backup_routes != old_routes:
            raise OSError("发布前备份数量与原订阅数量不一致")

        # 两文件替换不是原子操作；任何异常都进入明确回滚路径。
        os.replace(staged_m3u, m3u_path)
        m3u_replaced = True

        os.replace(staged_txt, txt_path)
        txt_replaced = True

        # 正式文件替换后重新读取核验。
        if m3u_path.read_text(encoding="utf-8") != m3u:
            raise OSError("正式 M3U 替换后核验不一致")
        if txt_path.read_text(encoding="utf-8") != txt:
            raise OSError("正式 TXT 替换后核验不一致")

        # 保存当前正式文件作为最近一次已知良好版本。
        known_good = state_dir / "known-good"
        known_good.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m3u_path, known_good / "iptv.m3u")
        shutil.copy2(txt_path, known_good / "iptv.txt")

        (state_dir / "last-known-good.json").write_text(json.dumps({
            "backup_dir": str(backup_dir.relative_to(output_dir)),
            "m3u": "iptv.m3u",
            "txt": "iptv.txt",
            "verified_at": stamp,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "status": "published",
            "reason": "两份订阅已替换并重新读取核验",
            "channels": new_channels,
            "routes": new_routes,
            "backup": str(backup_dir.relative_to(output_dir)),
        }

    except Exception as exc:
        rollback_ok = True
        rollback_errors = []

        # 如果尚未替换任何正式文件，只核验原文件仍然完整，
        # 不使用可能尚未完成的备份覆盖原文件。
        if not m3u_replaced and not txt_replaced:
            current_valid, current_channels, current_routes, current_reason = (
                _parse_existing(m3u_path, txt_path)
            )

            if (
                not current_valid
                or current_channels != old_channels
                or current_routes != old_routes
                or m3u_path.exists() != had_m3u
                or txt_path.exists() != had_txt
            ):
                rollback_ok = False
                rollback_errors.append(
                    f"原文件状态核验失败：{current_reason}"
                )

        else:
            # 发生过正式文件替换，必须先重新核验两份备份。
            backup_valid, backup_channels, backup_routes, backup_reason = (
                _parse_existing(backup_m3u, backup_txt)
            )

            if (
                not backup_valid
                or backup_channels != old_channels
                or backup_routes != old_routes
                or backup_m3u.exists() != had_m3u
                or backup_txt.exists() != had_txt
            ):
                rollback_ok = False
                rollback_errors.append(
                    f"回滚备份核验失败：{backup_reason}"
                )

            # 备份完整有效后，才允许恢复。
            if rollback_ok:
                try:
                    if had_m3u:
                        shutil.copy2(backup_m3u, m3u_path)
                    else:
                        m3u_path.unlink(missing_ok=True)

                    if had_txt:
                        shutil.copy2(backup_txt, txt_path)
                    else:
                        txt_path.unlink(missing_ok=True)

                except Exception as restore_exc:
                    rollback_ok = False
                    rollback_errors.append(
                        f"旧文件恢复失败：{restore_exc}"
                    )

            # 恢复完成后，再次读取正式文件并检查两份内容。
            if rollback_ok:
                try:
                    if (
                        m3u_path.exists() != had_m3u
                        or txt_path.exists() != had_txt
                    ):
                        raise OSError("恢复后文件存在状态不一致")

                    restored_valid, restored_channels, restored_routes, restored_reason = (
                        _parse_existing(m3u_path, txt_path)
                    )

                    if not restored_valid:
                        raise OSError(
                            f"恢复后订阅解析失败：{restored_reason}"
                        )

                    if (
                        restored_channels != old_channels
                        or restored_routes != old_routes
                    ):
                        raise OSError("恢复后频道数或线路数与旧版本不一致")

                    if had_m3u:
                        if (
                            m3u_path.read_text(encoding="utf-8-sig")
                            != backup_m3u.read_text(encoding="utf-8-sig")
                        ):
                            raise OSError("恢复后的 M3U 与备份不一致")

                    if had_txt:
                        if (
                            txt_path.read_text(encoding="utf-8-sig")
                            != backup_txt.read_text(encoding="utf-8-sig")
                        ):
                            raise OSError("恢复后的 TXT 与备份不一致")

                except Exception as verify_exc:
                    rollback_ok = False
                    rollback_errors.append(
                        f"恢复后核验失败：{verify_exc}"
                    )

        if not rollback_ok:
            # 回滚失败：保留临时文件、备份和现场，不允许 finally 清理。
            preserve_staged = True

            try:
                unknown_marker.write_text(json.dumps({
                    "status": "unknown",
                    "error": str(exc),
                    "rollback_errors": rollback_errors,
                    "backup_dir": str(backup_dir.relative_to(output_dir)),
                    "staged_m3u": staged_m3u.name,
                    "staged_txt": staged_txt.name,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as marker_exc:
                rollback_errors.append(
                    f"发布状态未知标记写入失败：{marker_exc}"
                )

            return {
                "status": "unknown",
                "reason": f"发布失败且回滚未能完成核验：{exc}",
                "rollback_errors": rollback_errors,
                "backup": str(backup_dir.relative_to(output_dir)),
            }

        return {
            "status": "rolled_back",
            "reason": f"发布失败，旧文件已恢复并重新核验：{exc}",
            "backup": str(backup_dir.relative_to(output_dir)),
        }

    finally:
        # 只有发布成功或回滚成功才清理临时文件。
        # 回滚失败必须保留现场供后续恢复和人工检查。
        if not preserve_staged:
            for path in (staged_m3u, staged_txt):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def _candidate_remote(candidate: dict[str, Any], locations: list[dict[str, Any]],
                      probe_limit: int, boundary: float, budget: dict[str, int],
                      stats: Counter[str]) -> dict[str, Any]:
    url = candidate["normalized_url"]
    is_direct_ts = candidate.get("media_type") == "DIRECT_TS"
    result: dict[str, Any] = {"checks": {"ipv4": NA if is_direct_ts else INCOMPLETE,
                                         "http": INCOMPLETE,
                                         "hls": NA if is_direct_ts else INCOMPLETE},
                              "measurements": [], "probe_results": {}, "overall": INCOMPLETE}
    stats["attempted_candidates"] += 1
    phases = (("http", "http"),) if is_direct_ts else (("ipv4", "ping"), ("http", "http"))
    for phase, kind in phases:
        if budget["remaining"] <= 0:
            result["checks"][phase] = INCOMPLETE
            result.setdefault("reasons", []).append(f"额度不足，未创建 {phase} 测量")
            stats["quota_blocked_candidates"] += 1
            break
        expected = min(probe_limit, budget["remaining"])
        try:
            stage_locations = _limit_locations(locations, expected)
            budget["remaining"] -= expected
            measurement_id, probes_count = _create_measurement(kind, url, stage_locations, expected)
        except GlobalpingError as exc:
            if exc.no_probes:
                budget["remaining"] += expected
                result["checks"][phase] = INCOMPLETE
                result.setdefault("reasons", []).append(f"无可用探针：{phase}；不再重复创建该候选测量")
                return result
            if exc.uncertain:
                result["creation_uncertain"] = True
                result.setdefault("reasons", []).append(f"{phase} 创建结果不确定")
                stats["creation_uncertain_candidates"] += 1
                return result
            if exc.status == 429:
                budget["remaining"] += expected
                budget["stop_new"] = 1
                result.setdefault("reasons", []).append("Globalping 创建测量触发 HTTP 429，停止新增测量")
                return result
            budget["remaining"] += expected
            result.setdefault("reasons", []).append(f"{phase} 创建失败：{exc}")
            continue
        stats["actual_measurements"] += 1
        stats["actual_probe_slots"] += probes_count
        budget["remaining"] = max(0, budget["remaining"] + expected - probes_count)
        result["measurements"].append({"phase": phase, "id": measurement_id, "probes_count": probes_count})
        measurement, boundary_reason = _poll(measurement_id, boundary)
        if measurement and measurement.get("status") == "finished":
            stats["completed_measurements"] += 1
        else:
            stats["incomplete_measurements"] += 1
        if boundary_reason:
            result.setdefault("reasons", []).append(f"{phase} {boundary_reason}")
        if phase == "ipv4":
            state, evidence = _ping_aggregate(measurement)
        else:
            state, evidence, success = _http_aggregate(measurement)
            result["http_success_evidence"] = success
        result["checks"][phase] = state
        result["probe_results"][phase] = evidence
        if state == FAIL:
            # 明确目标端失败足以拒绝，不必为同一候选创建额外阶段。
            break
        if budget.get("stop_new"):
            break

    if candidate.get("media_type") == "HLS":
        http_success = result.get("http_success_evidence", [])
        http_evidence = result.get("probe_results", {}).get("http", [])
        child_url = _master_child_url(http_success, url)
        if child_url:
            parent_record = next((item for item in result["measurements"]
                                  if item.get("phase") == "http"), None)
            parent_measurement = parent_record.get("id") if parent_record else None
            required_probe_slots = int(parent_record.get("probes_count", 0)) if parent_record else 0
            if not parent_measurement:
                result["checks"]["hls"] = INCOMPLETE
                result.setdefault("reasons", []).append("主播放清单缺少第一阶段测量身份")
            elif budget["stop_new"] or required_probe_slots < 1 or budget["remaining"] < required_probe_slots:
                result["checks"]["hls"] = INCOMPLETE
                result.setdefault("reasons", []).append("主播放清单需要第二阶段，但额度不足")
            else:
                try:
                    budget["remaining"] -= required_probe_slots
                    child_id, child_probes = _create_measurement(
                        "http", child_url, _limit_locations(locations, required_probe_slots), required_probe_slots,
                        previous_measurement=parent_measurement,
                    )
                    stats["actual_measurements"] += 1
                    stats["actual_probe_slots"] += child_probes
                    budget["remaining"] = max(0, budget["remaining"] + required_probe_slots - child_probes)
                    result["measurements"].append({"phase": "hls_child", "id": child_id,
                                                    "probes_count": child_probes,
                                                    "parent_measurement_id": parent_measurement})
                    child_measurement, child_boundary = _poll(child_id, boundary)
                    if child_measurement and child_measurement.get("status") == "finished":
                        stats["completed_measurements"] += 1
                    else:
                        stats["incomplete_measurements"] += 1
                    child_state, child_evidence, child_success = _http_aggregate(child_measurement)
                    parent_fingerprints = {_probe_fingerprint(item) for item in http_success}
                    child_fingerprints = {_probe_fingerprint(item) for item in child_success}
                    same_probe_set = bool(parent_fingerprints) and parent_fingerprints == child_fingerprints
                    if not same_probe_set:
                        result["checks"]["hls"] = INCOMPLETE
                        result.setdefault("reasons", []).append(
                            "HLS 第二阶段未能核对为第一阶段相同探针，保留 INCOMPLETE"
                        )
                    else:
                        child_hls_state, child_hls_evidence = _hls_aggregate(child_success, child_evidence)
                        result["checks"]["hls"] = child_hls_state
                        result["probe_results"]["hls_child"] = child_hls_evidence
                    result["probe_results"]["hls_child_http"] = child_evidence
                    if child_boundary:
                        result.setdefault("reasons", []).append(f"HLS 第二阶段 {child_boundary}")
                except GlobalpingError as exc:
                    if exc.uncertain:
                        stats["creation_uncertain_candidates"] += 1
                        result["creation_uncertain"] = True
                        result.setdefault("reasons", []).append("HLS 第二阶段创建结果不确定")
                    elif exc.no_probes:
                        budget["remaining"] += required_probe_slots
                        result.setdefault("reasons", []).append("HLS 第二阶段没有可用探针")
                    elif exc.status == 429:
                        budget["remaining"] += required_probe_slots
                        budget["stop_new"] = 1
                        result.setdefault("reasons", []).append("HLS 第二阶段触发 HTTP 429")
                    else:
                        budget["remaining"] += required_probe_slots
                        result.setdefault("reasons", []).append(f"HLS 第二阶段创建失败：{exc}")
                    result["checks"]["hls"] = INCOMPLETE
        else:
            hls_state, hls_evidence = _hls_aggregate(http_success, http_evidence)
            result["checks"]["hls"] = hls_state
            result["probe_results"]["hls"] = hls_evidence
    applicable = [result["checks"]["http"]] if is_direct_ts else [
        result["checks"]["ipv4"], result["checks"]["http"]]
    if candidate.get("media_type") == "HLS":
        applicable.append(result["checks"]["hls"])
    result["overall"] = FAIL if FAIL in applicable else PASS if all(x == PASS for x in applicable) else INCOMPLETE
    return result


def run_globalping(queue: list[dict[str, Any]], settings: dict[str, Any],
                   channels: list[str], category_by_channel: dict[str, str] | None = None,
                   category_order: list[str] | None = None, dry_run: bool = False,
                   output_dir: Path | str = "output", local_pass_count: int | None = None) -> dict[str, Any]:
    if not isinstance(queue, list):
        raise ValueError("Globalping 只接受 main 传入的候选队列")
    identities = []
    for candidate in queue:
        if not isinstance(candidate, dict) or not candidate.get("channel") or not candidate.get("normalized_url"):
            raise ValueError("Globalping 收到缺少频道/规范化 URL 的候选")
        identities.append((candidate["channel"], candidate["normalized_url"]))
    if len(identities) != len(set(identities)):
        raise ValueError("Globalping 输入队列存在重复候选身份")
    output_dir = Path(output_dir)
    stats: Counter[str] = Counter()
    stats["publisher_queue_candidates"] = len(queue)
    stats["local_pass_candidates"] = len(queue) if local_pass_count is None else int(local_pass_count)
    input_identities = [(channel, url) for channel, url in identities]
    if dry_run:
        # 测试模式绝不查询额度、创建测量或写入正式订阅。
        return {"input_queue_identities": input_identities,
                "results": [{"identity": list(identity), "remote": {"overall": "NOT_RUN"},
                             "admission": "NOT_RUN"} for identity in identities],
                "accepted": [], "stats": dict(stats),
                "publication": {"status": "not_run", "reason": "测试模式不创建 Globalping 测量、不发布订阅"}}

    stats["remote_eligible_candidates"] = len(queue)
    try:
        limits = _read_limits()
        max_probes = int(settings.get("globalping", {}).get("max_probes", 3))
        locations, available_probes = _probe_locations(max_probes)
    except GlobalpingError as exc:
        LOG.error("Globalping 预检失败，停止新增测量：%s", exc)
        return {"input_queue_identities": input_identities, "results": [], "accepted": [],
                "stats": {**dict(stats), "preflight_failed": 1},
                "publication": {"status": "blocked", "reason": str(exc)}}

    budget = {"remaining": max(0, limits["remaining"]), "stop_new": 0}
    probe_limit = min(max_probes, available_probes)
    if probe_limit < 1:
        return {"input_queue_identities": input_identities, "results": [], "accepted": [],
                "stats": {**dict(stats), "no_probes": 1},
                "publication": {"status": "blocked", "reason": "没有符合要求的中国大陆 eyeball 探针"}}
    boundary = max(1.0, float(settings.get("detection", {}).get("candidate_active_budget_seconds", 8)))
    max_per_channel = int(settings.get("selection", {}).get("max_routes_per_channel", 2))
    accepted: list[dict[str, Any]] = []
    channel_counts: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    for priority, candidate in enumerate(queue, 1):
        channel = candidate["channel"]
        identity = (channel, candidate["normalized_url"])
        if channel_counts[channel] >= max_per_channel:
            stats["channel_full_skipped"] += 1
            records.append({"identity": list(identity), "priority_order": priority,
                            "admission": "SKIPPED_CHANNEL_FULL", "reason": "该频道已达到正式准入名额"})
            continue
        local_reason = _local_admission_reason(candidate)
        if local_reason:
            records.append({"identity": list(identity), "priority_order": priority,
                            "admission": "REJECTED_LOCAL", "reason": local_reason})
            continue
        if budget["stop_new"] or budget["remaining"] <= 0:
            stats["quota_blocked_candidates"] += 1
            records.append({"identity": list(identity), "priority_order": priority,
                            "admission": "INCOMPLETE", "reason": "额度不足或限流，未创建新测量"})
            continue
        remote = _candidate_remote(candidate, locations, probe_limit, boundary, budget, stats)
        eligible, reason = _remote_admission(candidate, remote)
        stats["remote_pass_candidates"] += int(remote.get("overall") == PASS)
        stats["remote_fail_candidates"] += int(remote.get("overall") == FAIL)
        stats["remote_incomplete_candidates"] += int(remote.get("overall") == INCOMPLETE)
        if remote.get("creation_uncertain"):
            eligible = False
            reason = "测量创建结果不确定，不参与正式准入"
        if eligible and channel_counts[channel] < max_per_channel:
            accepted.append(candidate)
            channel_counts[channel] += 1
            stats["admitted_candidates"] += 1
            admission = "ADMITTED"
        else:
            admission = "REJECTED_REMOTE"
        records.append({"identity": list(identity), "priority_order": priority,
                        "remote": remote, "admission": admission, "reason": reason})

    stats["accepted_routes"] = len(accepted)
    stats["accepted_channels"] = len(channel_counts)
    publication = _publish(accepted, output_dir, channels,
                           category_by_channel or {}, category_order or [])
    LOG.info("Globalping 统计：%s", json.dumps(dict(stats), ensure_ascii=False, sort_keys=True))
    LOG.info("订阅发布状态：%s；原因：%s", publication.get("status"), publication.get("reason"))
    return {"input_queue_identities": input_identities, "results": records,
            "accepted": [{"channel": item["channel"], "normalized_url": item["normalized_url"]} for item in accepted],
            "stats": dict(stats), "publication": publication,
            "limits": {"remaining_at_start": limits["remaining"],
                       "probes_per_measurement_requested": probe_limit}}

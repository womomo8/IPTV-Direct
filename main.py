
#!/usr/bin/env python3
"""IPTV-Direct 总入口与统一调度。"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from collector import (
    SourceConfigError,
    SourceDownloadError,
    download_url,
    iter_source_contents,
    prepare_sources,
)
from normalizer import (
    ChannelConfigError,
    load_channels,
    normalize_source_content,
    normalize_url,
)

ROOT = Path(__file__).resolve().parent

CONFIG_FILES = (
    "channels.txt",
    "manual.txt",
    "sources.json",
    "settings.json",
)

TEST_CHANNEL = "IPTV-Direct 模拟测试频道"


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path.name}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"配置文件不是有效 UTF-8 文本：{path.name}"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"读取配置文件失败：{path.name}：{exc}"
        ) from exc


def _read_json_file(path: Path) -> dict[str, Any]:
    raw = _read_text_file(path)

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 格式错误：{path.name}，"
            f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc

    if not isinstance(value, dict):
        raise ValueError(
            f"配置文件顶层必须是 JSON 对象：{path.name}"
        )

    return value


def _check_settings(settings: dict[str, Any]) -> None:
    """检查正式参数结构和数据类型，不使用备用默认值。"""

    required = {
        "collection": {
            "max_valid_sources": int,
            "max_candidates_per_source": int,
            "max_candidates_total": int,
            "max_index_links_per_page": int,
        },
        "detection": {
            "local_concurrency": int,
            "per_host_concurrency": int,
            "queue_limit": int,
            "max_queue_seconds": int,
            "candidate_active_budget_seconds": int,
            "source_download_timeout_seconds": int,
            "max_source_download_bytes": int,
            "max_hls_manifest_bytes": int,
            "max_ts_read_bytes": int,
            "max_redirects": int,
        },
        "globalping": {"max_probes": int},
        "selection": {"max_routes_per_channel": int},
        "schedule": {
            "timezone": str,
            "daily_time": str,
        },
    }

    for section, fields in required.items():
        section_value = settings.get(section)

        if not isinstance(section_value, dict):
            raise ValueError(
                f"settings.json 缺少有效配置区段：{section}"
            )

        for field, expected_type in fields.items():
            value = section_value.get(field)

            if expected_type is int:
                valid_type = (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                )

                if not valid_type or value <= 0:
                    raise ValueError(
                        f"settings.json 参数 "
                        f"{section}.{field} 必须是正整数"
                    )

            elif (
                not isinstance(value, expected_type)
                or not value.strip()
            ):
                raise ValueError(
                    f"settings.json 参数 "
                    f"{section}.{field} 必须是非空文本"
                )

    timezone = settings["schedule"]["timezone"]
    daily_time = settings["schedule"]["daily_time"]

    if timezone != "Asia/Shanghai":
        raise ValueError(
            "settings.json schedule.timezone 必须为 Asia/Shanghai"
        )

    if len(daily_time) != 5 or daily_time[2] != ":":
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        )

    try:
        hour, minute = (
            int(part) for part in daily_time.split(":")
        )
    except ValueError as exc:
        raise ValueError(
            "settings.json schedule.daily_time 必须使用 HH:MM 格式"
        ) from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(
            "settings.json schedule.daily_time 不是有效时间"
        )


def load_configuration(
    root: Path = ROOT,
    test_mode: bool = False,
) -> dict[str, Any]:
    """读取并检查四个根目录配置文件。"""

    for filename in CONFIG_FILES:
        if not (root / filename).is_file():
            raise ValueError(f"缺少配置文件：{filename}")

    try:
        channels = load_channels(str(root / "channels.txt"))
    except ChannelConfigError as exc:
        if test_mode and str(exc) == "channels.txt 没有配置目标频道":
            channels = []
        else:
            raise ValueError(str(exc)) from exc

    manual_raw = _read_text_file(root / "manual.txt")
    sources_config = _read_json_file(root / "sources.json")
    settings = _read_json_file(root / "settings.json")

    _check_settings(settings)

    try:
        source_data = prepare_sources(manual_raw, sources_config)
    except SourceConfigError as exc:
        raise ValueError(str(exc)) from exc

    return {
        "channels": channels,
        "manual_sources": source_data["manual_sources"],
        "automatic_sources": source_data["automatic_sources"],
        "disabled_sources": source_data["disabled_sources"],
        "source_errors": source_data["errors"],
        "settings": settings,
    }


def collect_and_parse(config: dict[str, Any]) -> dict[str, Any]:
    """调度来源采集和 normalizer 处理，汇总本轮候选数量。"""

    collection = config["settings"]["collection"]
    detection = config["settings"]["detection"]

    max_valid_sources = collection["max_valid_sources"]
    max_per_source = collection["max_candidates_per_source"]
    max_total = collection["max_candidates_total"]
    max_hls_bytes = detection["max_hls_manifest_bytes"]

    parsed_sources = []
    errors = []
    valid_sources = 0
    total_candidates = 0
    duplicate_candidates = 0
    stopped_reason = "所有配置来源均已处理"

    seen_urls: set[str] = set()

    contents = iter_source_contents(
        config["manual_sources"],
        config["automatic_sources"],
        config["settings"],
    )

    for item in contents:
        source = item["source"]

        if item["errors"]:
            errors.extend(
                f"{source.get('name', source.get('url', '来源'))}：{error}"
                for error in item["errors"]
            )
            continue

        downloaded = item["download"]

        source = {
            **source,
            "final_url": downloaded["final_url"],
        }

        remaining = max_total - total_candidates
        if remaining <= 0:
            stopped_reason = f"达到全部候选总上限 {max_total}"
            break

        try:
            normalized = normalize_source_content(
                content=downloaded["content"],
                source=source,
                channels=config["channels"],
                max_candidates_per_source=max_per_source,
                seen_urls=seen_urls,
                content_type=downloaded["content_type"],
                max_hls_manifest_bytes=max_hls_bytes,
                max_candidates_remaining=remaining,
            )
        except (TypeError, ValueError) as exc:
            errors.append(
                f"{source.get('name', source.get('url', '来源'))}："
                f"内容处理异常：{exc}"
            )
            continue

        if not normalized["recognized"]:
            detail = (
                "；".join(normalized["errors"])
                or "无法识别内容格式"
            )
            errors.append(
                f"{source.get('name', source.get('url', '来源'))}：{detail}"
            )
            continue

        source_candidate_count = normalized["extracted_candidates"]
        candidates = normalized["candidates"]

        total_candidates += source_candidate_count
        duplicate_candidates += normalized["duplicate_candidates"]

        matched_count = normalized["matched_target_candidates"]

        parsed_sources.append({
            "source": source,
            "format": normalized["format"],
            "recognized": normalized["recognized"],
            "candidates": candidates,
            "extracted_candidates": source_candidate_count,
            "duplicate_candidates": normalized["duplicate_candidates"],
            "retained_candidates": len(candidates),
            "matched_target_candidates": matched_count,
            "download": {
                "requested_url": downloaded["url"],
                "final_url": downloaded["final_url"],
                "content_type": downloaded["content_type"],
                "http_status": downloaded["http_status"],
                "truncated_media_probe": downloaded["truncated_media_probe"],
            },
        })

        errors.extend(
            f"{source.get('name', source.get('url', '来源'))}：{error}"
            for error in normalized["errors"]
        )

        if matched_count > 0:
            valid_sources += 1

        if valid_sources >= max_valid_sources:
            stopped_reason = f"达到有效来源上限 {max_valid_sources}"
            break

        if total_candidates >= max_total:
            stopped_reason = f"达到全部候选总上限 {max_total}"
            break

    return {
        "sources": parsed_sources,
        "errors": errors,
        "valid_sources": valid_sources,
        "total_candidates": total_candidates,
        "duplicate_candidates": duplicate_candidates,
        "unique_candidates": sum(
            len(source["candidates"]) for source in parsed_sources
        ),
        "stopped_reason": stopped_reason,
    }


def _run_stage4_collection_tests(config: dict[str, Any]) -> None:
    """通过本机模拟 HTTP 服务验收来源下载、解析和索引处理。"""

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def make_response(
        body: str | bytes,
        content_type: str = "text/plain; charset=utf-8",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ):
        data = body.encode("utf-8") if isinstance(body, str) else body
        return status, content_type, data, headers or {}

    playlist = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="IPTV-Direct 模拟测试频道",'
        "IPTV-Direct 模拟测试频道\n"
        "http://example.invalid/live/test.ts\n"
    )
    txt = (
        "IPTV-Direct 模拟测试频道,"
        "http://example.invalid/live/txt.ts\n"
    )
    hls = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXTINF:10,\n"
        "segment.ts\n"
        "#EXT-X-ENDLIST\n"
    )

    routes: dict[str, tuple] = {
        "/bad.m3u": make_response("服务暂时不可用", status=503),
        "/playlist.m3u": make_response(playlist, "audio/x-mpegurl"),
        "/channels.txt": make_response(txt),
        "/redirect.txt": make_response(
            b"", status=302, headers={"Location": "/channels.txt"}
        ),
        "/partial.txt": make_response(
            txt,
            status=206,
            headers={"Content-Range": "bytes 0-71/72"},
        ),
        "/live.m3u8": make_response(hls, "audio/x-mpegurl"),
        "/child.m3u": make_response(
            '<html><a href="sub/hidden.m3u">hidden</a></html>',
            "text/html; charset=utf-8",
        ),
        "/large.txt": make_response("X" * 128),
        "/oversized-hls.m3u8": make_response(
            "#EXTM3U\n#EXT-X-VERSION:3\n" + ("X" * 96),
            "audio/x-mpegurl",
        ),
    }

    links = [
        "sub/one.m3u",
        "sub/one.m3u#duplicate",
        "sub/two.txt",
        "child.m3u",
    ]
    links.extend(f"sub/extra{number:02d}.m3u" for number in range(38))
    links.extend(("child.html", "ftp://example.invalid/x.m3u"))
    routes["/index.html"] = make_response(
        "<html>" + "".join(
            f'<a href="{link}">{link}</a>' for link in links
        ) + "</html>",
        "text/html; charset=utf-8",
    )

    class Handler(BaseHTTPRequestHandler):
        route_table = routes
        request_counts: dict[str, int] = {}

        def do_GET(self):
            path = urlsplit(self.path).path
            self.request_counts[path] = self.request_counts.get(path, 0) + 1

            item = self.route_table.get(path)
            if item is None and path.startswith("/sub/"):
                item = make_response(playlist, "audio/x-mpegurl")
            if item is None:
                item = make_response("Not found", status=404)

            status, content_type, body, headers = item
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format_string, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    try:
        settings = copy.deepcopy(config["settings"])
        require(
            settings["collection"]["max_index_links_per_page"] == 40,
            "索引链接上限必须与 MASTER_PLAN.md 的40条标准一致",
        )

        m3u_url = f"{base}/playlist.m3u"
        m3u = download_url(m3u_url, 10, 1048576, 5, 1048576)
        parsed = normalize_source_content(
            content=m3u["content"],
            source={"url": m3u_url, "final_url": m3u["final_url"]},
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=m3u["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )
        require(
            m3u["content_type"] == "audio/x-mpegurl"
            and parsed["recognized"]
            and parsed["format"] == "m3u"
            and len(parsed["candidates"]) == 1
            and parsed["candidates"][0]["channel"] == TEST_CHANNEL,
            "M3U 下载或 MIME/频道解析失败",
        )
        print("阶段4测试：M3U 下载、MIME识别和频道匹配 PASS")

        txt_url = f"{base}/channels.txt"
        txt_download = download_url(txt_url, 10, 1048576, 5, 1048576)
        txt_parsed = normalize_source_content(
            content=txt_download["content"],
            source={"url": txt_url, "final_url": txt_download["final_url"]},
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=txt_download["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )
        require(
            txt_parsed["recognized"]
            and txt_parsed["format"] == "txt"
            and len(txt_parsed["candidates"]) == 1
            and txt_parsed["candidates"][0]["channel"] == TEST_CHANNEL,
            "TXT 下载或频道解析失败",
        )
        print("阶段4测试：TXT 下载和频道解析 PASS")

        hls_url = f"{base}/live.m3u8"
        hls_download = download_url(hls_url, 10, 1048576, 5, 1048576)
        hls_parsed = normalize_source_content(
            content=hls_download["content"],
            source={"url": hls_url, "final_url": hls_download["final_url"]},
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=hls_download["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )
        require(
            hls_parsed["recognized"]
            and hls_parsed["format"] == "hls"
            and hls_parsed["extracted_candidates"] == 1
            and hls_parsed["matched_target_candidates"] == 0
            and hls_parsed["retained_candidates"] == 0
            and hls_parsed["candidates"] == [],
            "HLS 清单识别或频道处理错误",
        )
        print("阶段4测试：HLS M3U8 清单识别 PASS")

        manual_sources = [
            {
                "name": "失败人工来源", "url": f"{base}/bad.m3u",
                "source_type": "manual", "priority": 0, "order": 0,
            },
            {
                "name": "人工 M3U", "url": m3u_url,
                "source_type": "manual", "priority": 0, "order": 1,
            },
            {
                "name": "重定向 TXT", "url": f"{base}/redirect.txt",
                "source_type": "manual", "priority": 0, "order": 2,
            },
            {
                "name": "HTTP 206 TXT", "url": f"{base}/partial.txt",
                "source_type": "manual", "priority": 0, "order": 3,
            },
        ]
        automatic_sources = [
            {
                "name": "自动 playlist", "url": hls_url,
                "source_type": "automatic", "mode": "playlist",
                "priority": 1, "order": 4,
            },
            {
                "name": "自动 index", "url": f"{base}/index.html",
                "source_type": "automatic", "mode": "index",
                "priority": 1, "order": 5,
            },
        ]

        items = list(iter_source_contents(
            manual_sources, automatic_sources, settings
        ))
        require(len(items) == 45, f"采集条数错误：预期45，实际{len(items)}")
        require(
            [item["source"]["url"] for item in items[:4]]
            == [source["url"] for source in manual_sources],
            "人工来源没有优先按配置顺序处理",
        )
        require(
            items[0]["download"] is None and items[0]["errors"],
            "503 来源没有被隔离为单来源错误",
        )
        require(
            items[1]["download"] is not None and not items[1]["errors"],
            "首条来源失败后没有继续处理后续人工来源",
        )
        require(
            items[2]["download"] is not None
            and not items[2]["errors"]
            and items[2]["download"]["final_url"] == txt_url,
            "HTTP 重定向 final_url 不正确",
        )
        require(
            items[3]["download"] is not None
            and not items[3]["errors"]
            and items[3]["download"]["http_status"] == 206,
            "HTTP 206 未被接受",
        )
        require(
            items[4]["source"]["url"] == hls_url
            and items[4]["download"] is not None
            and not items[4]["errors"],
            "自动 playlist 模式下载失败",
        )
        require(
            all(
                item["source"]["source_type"] == "automatic"
                for item in items[5:]
            ),
            "自动来源处理顺序错误",
        )

        redirected = normalize_source_content(
            content=items[2]["download"]["content"],
            source={
                **items[2]["source"],
                "final_url": items[2]["download"]["final_url"],
            },
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type=items[2]["download"]["content_type"],
            max_hls_manifest_bytes=1048576,
            max_candidates_remaining=1,
        )
        require(
            redirected["recognized"]
            and redirected["format"] == "txt"
            and len(redirected["candidates"]) == 1
            and redirected["candidates"][0]["channel"] == TEST_CHANNEL,
            "重定向后的 TXT 内容未正确解析",
        )
        print("阶段4测试：人工优先、失败隔离、重定向、HTTP 206 PASS")

        index_items = items[5:]
        require(
            len(index_items) == 40,
            f"索引页处理数量错误：预期40，实际{len(index_items)}",
        )
        found = [item["source"]["url"] for item in index_items]
        require(
            found[0] == f"{base}/sub/one.m3u"
            and found[1] == f"{base}/sub/two.txt",
            "索引相对链接解析或重复链接去重失败",
        )
        require(
            Handler.request_counts.get("/sub/one.m3u", 0) == 1,
            "相同 URL 的 fragment 变体被重复下载",
        )
        require(
            Handler.request_counts.get("/sub/extra36.m3u", 0) == 1
            and Handler.request_counts.get("/sub/extra37.m3u", 0) == 0,
            "索引页40条上限没有正确生效",
        )
        require(
            Handler.request_counts.get("/sub/hidden.m3u", 0) == 0,
            "索引处理发生递归爬取",
        )
        require(
            Handler.request_counts.get("/child.html", 0) == 0,
            "索引未过滤不支持的 HTML 后缀",
        )
        print("阶段4测试：index相对链接、过滤、去重、40条上限、不递归 PASS")

        try:
            download_url(f"{base}/large.txt", 10, 64, 5, 1024)
        except SourceDownloadError:
            pass
        else:
            raise AssertionError("普通来源超过64字节却未触发大小限制")

        try:
            download_url(
                f"{base}/oversized-hls.m3u8", 10, 1024, 5, 32
            )
        except SourceDownloadError:
            pass
        else:
            raise AssertionError("HLS清单超过32字节却未触发大小限制")

        print("阶段4测试：普通来源和 HLS 清单大小限制 PASS")
        print("阶段4来源下载与解析测试：全部 PASS")

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_stage5_normalization_tests() -> None:
    """验收频道匹配、URL归一、全局去重和候选上限。"""

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    channels = ["CCTV-5"]

    def make_playlist(entries: list[tuple[str, str]]) -> str:
        lines = ["#EXTM3U"]

        for name, url in entries:
            lines.extend([
                f'#EXTINF:-1 tvg-name="{name}",{name}',
                url,
            ])

        return "\n".join(lines) + "\n"

    def normalize(
        playlist: str,
        source_name: str,
        seen_urls: set[str],
        max_per_source: int = 10,
        remaining: int | None = None,
        aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        source_url = f"https://example.com/{source_name}.m3u"

        return normalize_source_content(
            content=playlist,
            source={
                "name": source_name,
                "url": source_url,
                "final_url": source_url,
                "source_type": "manual",
                "priority": 0,
                "order": 0,
            },
            channels=channels,
            max_candidates_per_source=max_per_source,
            seen_urls=seen_urls,
            content_type="application/x-mpegurl",
            aliases=aliases,
            max_candidates_remaining=remaining,
        )

    # 1. 标准频道名称必须精确匹配。
    exact = normalize(
        make_playlist([
            ("CCTV-5", "https://example.com/live/one.ts"),
        ]),
        "exact",
        set(),
    )
    require(
        len(exact["candidates"]) == 1
        and exact["candidates"][0]["channel"] == "CCTV-5",
        "标准频道名称精确匹配失败",
    )

    # 2. 只允许明确提供的别名映射。
    alias = normalize(
        make_playlist([
            ("中央五套", "https://example.com/live/two.ts"),
        ]),
        "alias",
        set(),
        aliases={"中央五套": "CCTV-5"},
    )
    require(
        len(alias["candidates"]) == 1
        and alias["candidates"][0]["channel"] == "CCTV-5",
        "明确别名映射失败",
    )

    # 3. 相似名称不能被模糊匹配。
    fuzzy = normalize(
        make_playlist([
            ("CCTV-5+", "https://example.com/live/three.ts"),
        ]),
        "fuzzy",
        set(),
    )
    require(
        fuzzy["candidates"] == [],
        "相似频道名称被错误匹配",
    )

    print("阶段5测试：标准频道精确匹配、明确别名和拒绝模糊匹配 PASS")

    # 4. URL协议、主机名、默认端口和片段归一。
    normalized_url = normalize_url(
        "HTTPS://Example.COM:443/live/index.m3u8?token=1#fragment"
    )
    require(
        normalized_url
        == "https://example.com/live/index.m3u8?token=1",
        f"URL归一结果错误：{normalized_url}",
    )

    print("阶段5测试：URL大小写、默认端口、片段和查询参数处理 PASS")

    # 5. 不同来源的相同URL只能保留一条。
    shared_seen: set[str] = set()

    first = normalize(
        make_playlist([
            (
                "CCTV-5",
                "HTTPS://Example.COM:443/live/shared.ts?token=1#first",
            ),
        ]),
        "first",
        shared_seen,
    )

    second = normalize(
        make_playlist([
            (
                "CCTV-5",
                "https://example.com/live/shared.ts?token=1#second",
            ),
        ]),
        "second",
        shared_seen,
    )

    require(
        len(first["candidates"]) == 1
        and second["candidates"] == []
        and second["duplicate_candidates"] == 1,
        "跨来源全局URL去重失败",
    )

    print("阶段5测试：跨来源全局URL去重 PASS")

    # 6. 无效URL和未匹配频道不得进入候选结果。
    invalid_and_unmatched = normalize(
        make_playlist([
            ("CCTV-5", "http://example.com:bad/live.ts"),
            ("不在目标列表的频道", "https://example.com/live/other.ts"),
        ]),
        "invalid-and-unmatched",
        set(),
    )

    require(
        invalid_and_unmatched["candidates"] == []
        and invalid_and_unmatched["retained_candidates"] == 0
        and any(
            "无效线路 URL" in error
            for error in invalid_and_unmatched["errors"]
        ),
        "无效URL或未匹配频道过滤失败",
    )

    print("阶段5测试：无效URL和未匹配频道过滤 PASS")

    # 7. 单来源达到上限，只限制当前来源。
    source_seen: set[str] = set()

    limited = normalize(
        make_playlist([
            ("CCTV-5", "https://example.com/live/limit-one.ts"),
            ("CCTV-5", "https://example.com/live/limit-two.ts"),
        ]),
        "limited",
        source_seen,
        max_per_source=1,
    )

    next_source = normalize(
        make_playlist([
            ("CCTV-5", "https://example.com/live/next-source.ts"),
        ]),
        "next-source",
        source_seen,
        max_per_source=1,
    )

    require(
        limited["extracted_candidates"] == 1
        and limited["limit_reached"]
        and len(limited["candidates"]) == 1
        and len(next_source["candidates"]) == 1,
        "单来源候选上限错误影响其他来源",
    )

    print("阶段5测试：单来源候选上限及其他来源继续处理 PASS")

    # 8. 本轮剩余额度必须限制当前提取数量。
    remaining_limited = normalize(
        make_playlist([
            ("CCTV-5", "https://example.com/live/remaining-one.ts"),
            ("CCTV-5", "https://example.com/live/remaining-two.ts"),
        ]),
        "remaining",
        set(),
        max_per_source=10,
        remaining=1,
    )

    require(
        remaining_limited["extracted_candidates"] == 1
        and remaining_limited["retained_candidates"] == 1,
        "本轮剩余候选额度执行错误",
    )

    print("阶段5测试：本轮剩余候选额度 PASS")
    print("阶段5线路归一和去重专项测试：全部 PASS")


def run_test_flow(config: dict[str, Any]) -> int:
    """
    阶段4来源采集、阶段5线路归一测试和模拟检测结果交接。

    不调用真实线路检测，不调用 Globalping，
    不进入正式准入和订阅发布流程。
    """

    print("测试流程：阶段4来源采集与解析 + 阶段5线路归一和去重 + 模拟检测结果交接")

    try:
        _run_stage4_collection_tests(config)
    except Exception as exc:
        print(f"阶段4来源采集与解析测试失败：{exc}", file=sys.stderr)
        return 1

    try:
        _run_stage5_normalization_tests()
    except Exception as exc:
        print(f"阶段5线路归一和去重测试失败：{exc}", file=sys.stderr)
        return 1

    source_url = "https://example.invalid/iptv-direct-test.m3u"
    stream_url = "https://example.invalid/live/test.m3u8"

    simulated_source = {
        "name": "IPTV-Direct 模拟测试来源",
        "url": source_url,
        "final_url": source_url,
        "source_type": "manual",
        "priority": 0,
        "order": 0,
    }

    simulated_content = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="IPTV-Direct-Test" '
        'tvg-name="IPTV-Direct 模拟测试频道" '
        'group-title="测试",IPTV-Direct 模拟测试频道\n'
        f"{stream_url}\n"
    )

    try:
        normalized = normalize_source_content(
            content=simulated_content,
            source=simulated_source,
            channels=[TEST_CHANNEL],
            max_candidates_per_source=1,
            seen_urls=set(),
            content_type="audio/x-mpegurl",
            max_hls_manifest_bytes=(
                config["settings"]["detection"]["max_hls_manifest_bytes"]
            ),
            max_candidates_remaining=1,
        )

        if not normalized["recognized"]:
            raise ValueError("模拟来源未能被 normalizer 识别")

        candidates = normalized["candidates"]

        if len(candidates) != 1:
            raise ValueError(
                f"模拟候选数量错误：预期 1 条，实际 {len(candidates)} 条"
            )

        candidate = candidates[0]

        if candidate["channel"] != TEST_CHANNEL:
            raise ValueError("模拟频道匹配结果不正确")

        if candidate["normalized_url"] != stream_url:
            raise ValueError("模拟线路 URL 归一结果不正确")

        print("来源解析：PASS（normalizer 实际执行）")
        print("频道匹配：PASS（模拟测试频道）")
        print("候选数量：1")
        print("媒体线路：HLS（模拟输入标记，不代表实际媒体识别）")

        simulated_detection = {
            **candidate,
            "media_type": "HLS",
            "simulated": True,
            "checks": {
                "ipv4": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未建立真实 IPv4 连接",
                },
                "http": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未发送真实 HTTP 请求",
                },
                "hls": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未读取真实 HLS 清单",
                },
                "ts": {
                    "status": "PASS",
                    "reason": "模拟检测结果，未读取真实 TS 分片",
                },
            },
        }

        expected_checks = ("ipv4", "http", "hls", "ts")

        if simulated_detection["media_type"] != "HLS":
            raise ValueError("模拟检测线路类型错误")

        if not simulated_detection["simulated"]:
            raise ValueError("模拟检测标记缺失")

        for name in expected_checks:
            check = simulated_detection["checks"].get(name)

            if not isinstance(check, dict):
                raise ValueError(f"模拟检测结果缺少项目：{name}")

            if check.get("status") != "PASS":
                raise ValueError(f"模拟检测项目状态错误：{name}")

            if "模拟检测结果" not in check.get("reason", ""):
                raise ValueError(f"模拟检测项目缺少模拟说明：{name}")

        print("本地检测结果结构：PASS（模拟结果，非真实检测）")
        print("Globalping：按测试规范跳过，未创建真实测量")
        print("正式准入：未调用")
        print("订阅发布：未调用，正式订阅未修改")
        print("国内真实网络验证：未执行")

        print(
            "测试运行通过：模拟来源解析及模拟检测结果交接符合预期。"
        )
        print(
            "注意：这不代表 checker.py 实际检测通过，"
            "也不代表线路在中国移动网络中能够播放。"
        )

        return 0

    except Exception as exc:
        print(
            f"测试运行失败：{exc}",
            file=sys.stderr,
        )
        return 1


def parse_args(
    argv: list[str] | None = None
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IPTV-Direct 自动订阅系统"
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("formal", "test"),
        help="运行模式：formal 正式运行；test 测试运行",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    mode_name = (
        "正式运行"
        if args.mode == "formal"
        else "测试运行"
    )

    print(f"IPTV-Direct：{mode_name}")

    try:
        config = load_configuration(test_mode=args.mode == "test")
    except ValueError as exc:
        print(
            f"配置检查失败：{exc}",
            file=sys.stderr
        )
        return 1

    print(
        "配置读取成功："
        f"目标频道 {len(config['channels'])} 个，"
        f"人工来源 {len(config['manual_sources'])} 条，"
        f"启用的自动来源 {len(config['automatic_sources'])} 条，"
        f"禁用的自动来源 {config['disabled_sources']} 条。"
    )

    for error in config["source_errors"]:
        print(f"来源配置警告：{error}", file=sys.stderr)

    if args.mode == "test":
        return run_test_flow(config)

    result = collect_and_parse(config)

    print(
        "来源处理结果："
        f"已识别来源 {len(result['sources'])} 个，"
        f"有效来源 {result['valid_sources']} 个，"
        f"累计候选 {result['total_candidates']} 条，"
        f"去重后候选 {result['unique_candidates']} 条，"
        f"重复候选 {result['duplicate_candidates']} 条。"
    )
    print(f"停止原因：{result['stopped_reason']}")

    for error in result["errors"]:
        print(f"来源处理警告：{error}", file=sys.stderr)

    print(
        "当前尚未接入本地线路检测、Globalping 辅助检测、"
        "正式准入和订阅发布，本次运行在来源处理后安全结束。"
    )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

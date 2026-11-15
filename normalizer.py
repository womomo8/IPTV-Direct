def normalize_source_content(
    content: bytes,
    source: dict[str, Any],
    channels: list[str],
    max_candidates_per_source: int,
    seen_urls: set[str],
    content_type: str = "",
    max_hls_manifest_bytes: int = 1048576,
    aliases: Optional[Mapping[str, str]] = None,
    max_candidates_remaining: Optional[int] = None,
) -> dict[str, Any]:
    """解析单个来源，执行来源上限、全轮剩余额度、URL归一和去重。"""

    if (
        not isinstance(max_candidates_per_source, int)
        or isinstance(max_candidates_per_source, bool)
        or max_candidates_per_source <= 0
    ):
        raise ValueError("单来源候选上限必须是正整数")

    if max_candidates_remaining is not None and (
        not isinstance(max_candidates_remaining, int)
        or isinstance(max_candidates_remaining, bool)
        or max_candidates_remaining <= 0
    ):
        raise ValueError("本轮剩余候选额度必须是正整数")

    if not isinstance(seen_urls, set):
        raise TypeError("seen_urls 必须是整轮共用的 set")

    parsed = parse_source_content(
        content,
        source,
        channels,
        content_type=content_type,
        max_hls_manifest_bytes=max_hls_manifest_bytes,
        aliases=aliases,
    )

    if not parsed["recognized"]:
        return {
            **parsed,
            "candidates": [],
            "extracted_candidates": 0,
            "matched_target_candidates": 0,
            "duplicate_candidates": 0,
            "retained_candidates": 0,
            "limit_reached": False,
        }

    all_candidates = parsed["candidates"]
    extracted_count = len(all_candidates)

    candidate_limit = max_candidates_per_source

    if max_candidates_remaining is not None:
        candidate_limit = min(
            candidate_limit,
            max_candidates_remaining,
        )

    source_limit_reached = (
        extracted_count > max_candidates_per_source
        and candidate_limit == max_candidates_per_source
    )

    # 先执行单来源上限和全轮剩余额度。
    candidates = all_candidates[:candidate_limit]

    # 在 URL 去重前统计目标频道候选。
    matched_target_candidates = sum(
        1 for candidate in candidates
        if candidate.get("channel") is not None
    )

    retained = []
    duplicate_count = 0

    for candidate in candidates:
        raw_url = candidate.get("raw_url")

        try:
            normalized = normalize_url(raw_url)
        except (TypeError, ValueError):
            continue

        if normalized in seen_urls:
            duplicate_count += 1
            continue

        seen_urls.add(normalized)

        retained.append({
            **candidate,
            "normalized_url": normalized,
        })

    result_errors = list(parsed["errors"])

    if source_limit_reached:
        result_errors.append(
            f"达到单来源候选上限 {max_candidates_per_source}，"
            "当前来源其余候选已停止处理"
        )

    return {
        **parsed,
        "candidates": retained,
        "extracted_candidates": min(
            extracted_count,
            candidate_limit,
        ),
        "matched_target_candidates": matched_target_candidates,
        "duplicate_candidates": duplicate_count,
        "retained_candidates": len(retained),
        "limit_reached": source_limit_reached,
        "errors": result_errors,
    }

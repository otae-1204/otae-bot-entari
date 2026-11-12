"""终末地 /ef 关卡查询的候选解析与出图（由 handlers 的查询派发调用）。"""

from __future__ import annotations

from time import perf_counter

from loguru import logger

from ..catalog.commands import CANDIDATE_SCORE_THRESHOLD, EndfieldCandidate, score_candidate
from ..state import stage_service
from .draw import draw_stage_card, draw_stage_catalog_cards


async def _resolve_stage_candidates_akedata(query: str) -> list[EndfieldCandidate]:
    return await _resolve_stage_candidates(query, "akedata")


async def _resolve_stage_candidates(query: str, source: str = "") -> list[EndfieldCandidate]:
    query = query.strip()
    if not query:
        return []
    if query == "__all__":
        catalog = await stage_service.get_catalog_view(source)
        return [
            EndfieldCandidate(
                kind="stage_catalog",
                key="",
                display_name="关卡资料目录",
                score=100,
                source=source,
                reason="catalog",
                mode="catalog",
                revision=catalog.revision,
            )
        ]
    candidates: list[EndfieldCandidate] = []
    for match in await stage_service.discover_matches(query, source):
        score = score_candidate(match.query_text, match.display_name, match.title)
        if score < CANDIDATE_SCORE_THRESHOLD:
            continue
        candidates.append(
            EndfieldCandidate(
                kind="stage",
                key=match.key,
                display_name=match.display_name,
                score=score,
                source=match.source,
                reason="stage-directory",
                variant=match.selector,
                mode=match.mode,
                revision=match.revision,
            )
        )
    return candidates


async def _render_stage(
    key: str,
    source: str = "",
    *,
    mode: str = "detail",
    selector: str = "",
) -> tuple[bytes | None, bool]:
    """Returns the card and whether it is missing data purely because a fetch failed."""
    if source and source != "akedata":
        return None, False
    started = perf_counter()
    view = await stage_service.get_stage_view(
        key,
        mode=mode,
        selector=selector,
        source=source or "akedata",
    )
    data_seconds = perf_counter() - started
    output = await draw_stage_card(view)
    logger.info(
        f"[endfield] render kind=stage mode={mode} data={data_seconds:.3f}s "
        f"draw={perf_counter() - started - data_seconds:.3f}s "
        f"unreachable={len(view.unreachable_enemies)}"
    )
    return output, bool(view.unreachable_enemies)


async def _render_stage_catalog(key: str, source: str = "") -> tuple[bytes, ...] | None:
    del key
    if source and source != "akedata":
        return None
    return await draw_stage_catalog_cards(
        await stage_service.get_catalog_view(source or "akedata")
    )

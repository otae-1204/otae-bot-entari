"""Challenge parsing, independent of browser rendering and event handlers."""

from __future__ import annotations

import re
from html import escape
from datetime import (
    datetime,
)
from difflib import (
    SequenceMatcher,
)
from typing import (
    Any,
    Sequence,
)
from .i18n import (
    ChallengeLocale,
)
from ..i18n import (
    localized_text,
    semantic_label,
)
from .models import (
    ChallengeAmbiguousError,
    ChallengeEnemy,
    ChallengeMember,
    ChallengeRecord,
    ChallengeResolutionError,
    MonumentDungeon,
    MonumentGroup,
    MonumentPayload,
    WarAchievement,
    WarDungeon,
    WarEchoPayload,
    WarGroup,
    WarSeason,
    WarWeek,
    _int,
)


def parse_monument(raw: dict[str, Any] | None, locale: ChallengeLocale | None = None) -> MonumentPayload:
    data = _unwrap_data(raw, "indieHard")
    groups: list[MonumentGroup] = []
    for item in _list(data.get("indieHardGroups")):
        group_id = _text(item.get("id"))
        stages: list[tuple[MonumentDungeon, MonumentDungeon]] = []
        for pair in _list(item.get("dungeonGroups")):
            normal = _monument_dungeon(pair.get("normalDungeon"), "normal", locale)
            hard = _monument_dungeon(pair.get("hardDungeon"), "hard", locale)
            if normal or hard:
                stages.append((normal or _empty_monument("normal"), hard or _empty_monument("hard")))
        achieve = _dict(item.get("achieve"))
        achievement_data = _dict(achieve.get("achievementData"))
        achievement_id = _text(achievement_data.get("id"))
        groups.append(
            MonumentGroup(
                id=group_id,
                name=(
                    locale.monument_group_text(group_id, item.get("name"))
                    if locale is not None
                    else _localized_text(item.get("name"), None)
                ) or "未命名主题",
                pic_url=_text(item.get("pic")),
                activity_name=_localized_text(item.get("activityName"), locale),
                start_ts=_timestamp(item.get("activityStartTs")),
                end_ts=_timestamp(item.get("activityEndTs")),
                is_active=_flag(item.get("isInActivity")),
                stages=tuple(stages),
                medal_name=(
                    locale.achievement_text(achievement_id, achievement_data.get("name"))
                    if locale is not None
                    else _localized_text(achievement_data.get("name"), None)
                ),
                medal_icon_url=_text(achievement_data.get("initIcon")),
                medal_plated_icon_url=_text(achievement_data.get("platedIcon")),
                medal_level=_int(achieve.get("level")),
                medal_plated=_flag(achieve.get("isPlated")),
            )
        )
    return MonumentPayload(tuple(groups))


def parse_war_echoes(raw: dict[str, Any] | None, locale: ChallengeLocale | None = None) -> WarEchoPayload:
    data = _unwrap_data(raw, "warEchoes")
    seasons: list[WarSeason] = []
    for item in _list(data.get("seasons")):
        weeks: list[WarWeek] = []
        for week in _list(item.get("weeks")):
            groups: list[WarGroup] = []
            for group in _list(week.get("dungeonGroups")):
                normal = _war_dungeon(group.get("normalDungeon"), "normal", locale)
                hard = _war_dungeon(group.get("hardDungeon"), "hard", locale)
                cruel = _war_dungeon(group.get("cruelDungeon"), "cruel", locale)
                groups.append(
                    WarGroup(
                        name=(
                            _localized_text(group.get("name"), locale)
                            or next((item.name for item in (normal, hard, cruel) if item and item.name), "")
                            or "未命名轮换关卡"
                        ),
                        star=_int(group.get("star")),
                        plus_task=_flag(group.get("plusTask")),
                        normal=normal,
                        hard=hard,
                        cruel=cruel,
                    )
                )
            weeks.append(
                WarWeek(
                    id=_text(week.get("id")),
                    name=_localized_text(week.get("name"), locale) or "未命名轮换",
                    start_ts=_timestamp(week.get("startTs")),
                    end_ts=_timestamp(week.get("endTs")),
                    stars=_int(week.get("stars")),
                    all_plus_tasks=_flag(week.get("allPlusTasks")),
                    groups=tuple(groups),
                )
            )
        seasons.append(
            WarSeason(
                id=_text(item.get("id")),
                name=_localized_text(item.get("name"), locale) or "未命名赛季",
                kv_url=_text(item.get("kvImage")),
                header_url=_text(item.get("headerImage")),
                start_ts=_timestamp(item.get("startTs")),
                end_ts=_timestamp(item.get("endTs")),
                stars=_int(item.get("stars")),
                all_plus_tasks=_flag(item.get("allPlusTasks")),
                weeks=tuple(weeks),
            )
        )
    achievements = tuple(
        WarAchievement(
            name=name,
            star=_int(item.get("star")),
            first_pass_ts=_timestamp(item.get("firstPassTs")),
        )
        for item in _list(data.get("achieves"))
        if (name := _localized_text(item.get("name"), locale))
    )
    return WarEchoPayload(tuple(seasons), achievements)


def resolve_monument_detail(
    payload: MonumentPayload,
    terms: Sequence[str],
    difficulty: str = "hard",
) -> tuple[MonumentGroup, MonumentDungeon]:
    if not terms:
        raise ChallengeResolutionError("请指定影拓主题或关卡名称")
    normalized_difficulty = difficulty if difficulty in {"normal", "hard"} else "hard"
    group: MonumentGroup | None = None
    stage_query = " ".join(str(item) for item in terms).strip()
    group_query = str(terms[0]).strip()
    if len(terms) > 1:
        group = _pick(group_query, payload.groups, lambda item: item.name)
        stage_query = " ".join(str(item) for item in terms[1:]).strip()
    else:
        group = _pick_or_none(group_query, payload.groups, lambda item: item.name)
    if group is not None:
        stages = [pair[0 if normalized_difficulty == "normal" else 1] for pair in group.stages]
        if len(terms) == 1:
            if not stages:
                raise ChallengeResolutionError(f"主题“{group.name}”暂无关卡记录")
            return group, stages[0]
        stage = _pick(stage_query, stages, lambda item: item.name)
        return group, stage
    candidates: list[tuple[MonumentGroup, MonumentDungeon]] = []
    for item in payload.groups:
        for pair in item.stages:
            dungeon = pair[0 if normalized_difficulty == "normal" else 1]
            candidates.append((item, dungeon))
    chosen = _pick(stage_query, candidates, lambda item: item[1].name)
    return chosen


def resolve_war_detail(
    payload: WarEchoPayload,
    terms: Sequence[str],
    difficulty: str = "cruel",
) -> tuple[WarSeason, WarWeek, WarGroup, WarDungeon]:
    if not terms:
        raise ChallengeResolutionError("请指定战争回响赛季、轮换或关卡名称")
    normalized_difficulty = difficulty if difficulty in {"normal", "hard", "cruel"} else "cruel"
    season: WarSeason | None = _pick_or_none(str(terms[0]), payload.seasons, lambda item: item.name)
    if season is not None:
        rest = list(terms[1:])
        week: WarWeek | None = None
        if rest:
            week = _pick_or_none(str(rest[0]), season.weeks, lambda item: item.name)
            if week is not None:
                rest.pop(0)
        if week is None:
            week = season.current_week() or (season.weeks[-1] if season.weeks else None)
        if week is None:
            raise ChallengeResolutionError(f"赛季“{season.name}”暂无轮换记录")
        if rest:
            group = _pick_or_none(" ".join(rest), week.groups, lambda item: item.name)
            if group is not None:
                dungeon = group.dungeon(normalized_difficulty)
                if dungeon is not None:
                    return season, week, group, dungeon
            dungeon_candidates = [
                (week_item, group_item, group_item.dungeon(normalized_difficulty))
                for week_item in season.weeks
                for group_item in week_item.groups
                if group_item.dungeon(normalized_difficulty) is not None
            ]
            selected_week, group, dungeon = _pick(" ".join(rest), dungeon_candidates, lambda item: item[2].name)
            return season, selected_week, group, dungeon
        group = week.groups[0] if week.groups else None
        dungeon = group.dungeon(normalized_difficulty) if group else None
        if group is None or dungeon is None:
            raise ChallengeResolutionError(f"轮换“{week.name}”暂无{_difficulty_label(normalized_difficulty)}数据")
        return season, week, group, dungeon

    candidates = [
        (season_item, week, group, group.dungeon(normalized_difficulty))
        for season_item in payload.seasons
        for week in season_item.weeks
        for group in week.groups
        if group.dungeon(normalized_difficulty) is not None
    ]
    return _pick(" ".join(str(item) for item in terms), candidates, lambda item: item[3].name)


def _monument_dungeon(raw, difficulty, locale: ChallengeLocale | None = None):
    if not isinstance(raw, dict):
        return None
    dungeon_id = _text(raw.get("id"))
    return MonumentDungeon(
        id=dungeon_id,
        name=_localized_dungeon_text(locale, dungeon_id, "name", raw.get("name")),
        difficulty=difficulty,
        passed=_flag(raw.get("isPass")),
        desc=_localized_dungeon_plain(locale, dungeon_id, "desc", raw.get("desc")),
        feature=_localized_dungeon_plain(locale, dungeon_id, "feature", raw.get("feature")),
        recommend_level=_int(raw.get("recommendLevel")), record=_record(raw.get("bestRecord")),
        enemies=_enemies(raw.get("enemies"), locale),
    )


def _war_dungeon(raw, difficulty, locale: ChallengeLocale | None = None):
    if not isinstance(raw, dict):
        return None
    dungeon_id = _text(raw.get("id"))
    return WarDungeon(
        id=dungeon_id,
        name=_localized_dungeon_text(locale, dungeon_id, "name", raw.get("name")),
        difficulty=difficulty,
        passed=_flag(raw.get("isPass")), first_pass_ts=_timestamp(raw.get("firstPassTs")),
        desc=_localized_dungeon_plain(locale, dungeon_id, "desc", raw.get("desc")),
        feature=_localized_dungeon_plain(locale, dungeon_id, "feature", raw.get("feature")),
        recommend_level=_int(raw.get("recommendLevel")), plus_task=_flag(raw.get("plusTask")),
        additional_target=_localized_dungeon_plain(
            locale,
            dungeon_id,
            "additional_target",
            raw.get("additionalChallengeTarget"),
        ),
        record=_record(raw.get("bestRecord")),
        enemies=_enemies(raw.get("enemies"), locale),
    )


def _empty_monument(difficulty):
    return MonumentDungeon("", "", difficulty)


def _record(raw) -> ChallengeRecord:
    if not isinstance(raw, dict):
        return ChallengeRecord()
    return ChallengeRecord(
        members=tuple(_member(item) for item in _list(raw.get("chars"))),
        record_ts=_timestamp(raw.get("ts")),
        pass_time=_int(raw.get("passTs")),
        first_pass_ts=_timestamp(raw.get("firstPassTs")),
    )


def _member(raw) -> ChallengeMember:
    property_data = _dict(raw.get("property"))
    rarity_data = _dict(raw.get("rarity"))
    return ChallengeMember(
        char_id=_text(raw.get("charId")), avatar_url=_text(raw.get("avatarUrl")), level=_int(raw.get("level")),
        potential=_int(raw.get("potentialLevel")), rarity=_text(rarity_data.get("value")),
        property=semantic_label(property_data, default=_text(property_data.get("value"))),
    )


def _enemies(raw, locale: ChallengeLocale | None = None) -> tuple[ChallengeEnemy, ...]:
    output = []
    for item in _list(raw):
        if not isinstance(item, dict):
            continue
        enemy_id = _text(item.get("id"))
        output.append(
            ChallengeEnemy(
                name=(
                    locale.enemy_text(enemy_id, "name", item.get("name"))
                    if locale is not None
                    else _text(item.get("name"))
                ),
                level=_int(item.get("level")),
                image_url=_text(item.get("imageUrl")),
                ability=_plain(
                    locale.enemy_text(enemy_id, "ability", item.get("ability"))
                    if locale is not None
                    else _text(item.get("ability"))
                ),
                desc=_plain(
                    locale.enemy_text(enemy_id, "desc", item.get("desc"))
                    if locale is not None
                    else _text(item.get("desc"))
                ),
            )
        )
    return tuple(output)


def _unwrap_data(raw, key):
    data = _dict(raw)
    if key in data and isinstance(data[key], dict):
        return data[key]
    if key == "indieHard" and "indieHardGroups" in data:
        return data
    if key == "warEchoes" and "seasons" in data:
        return data
    nested = _dict(data.get("data"))
    if key in nested and isinstance(nested[key], dict):
        return nested[key]
    detail = _dict(nested.get("detail"))
    return _dict(detail.get(key))


def _latest_monument_record(group):
    entries = [(group.name, dungeon) for pair in group.stages for dungeon in pair if dungeon.record.available]
    return max(entries, key=lambda item: (item[1].record.record_ts, item[1].record.first_pass_ts), default=None)


def _latest_war_record(season):
    records = [
        dungeon.record
        for week in season.weeks
        for group in week.groups
        for dungeon in (group.normal, group.hard, group.cruel)
        if dungeon and dungeon.record.available
    ]
    return max(records, key=lambda item: (item.record_ts, item.first_pass_ts), default=None)


def _pick(query, items, label):
    if not items:
        raise ChallengeResolutionError(f"未找到“{query}”")
    normalized = _normalize(query)
    exact = [item for item in items if _normalize(label(item)) == normalized]
    if len(exact) == 1:
        return exact[0]
    scored = sorted(((item, _score(normalized, _normalize(label(item)))) for item in items), key=lambda pair: pair[1], reverse=True)
    if not scored or scored[0][1] < 0.38:
        raise ChallengeResolutionError(f"未找到“{query}”，请使用“历史”查看可用名称")
    if len(scored) > 1 and scored[0][1] - scored[1][1] < 0.08:
        raise ChallengeAmbiguousError(query, [label(item) for item, _ in scored[:5]])
    return scored[0][0]


def _pick_or_none(query, items, label):
    try:
        return _pick(query, items, label)
    except ChallengeResolutionError:
        return None


def _score(query, candidate):
    if not query or not candidate:
        return 0.0
    if query in candidate or candidate in query:
        return 0.88 + min(len(query), len(candidate)) / max(len(query), len(candidate)) * 0.1
    return SequenceMatcher(None, query, candidate).ratio()


def _normalize(value):
    return re.sub(r"[\s·•,，。:：/／_\-]+", "", str(value or "").casefold())


def _list(value):
    return value if isinstance(value, list) else []


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return localized_text(value)


def _localized_text(value, locale: ChallengeLocale | None) -> str:
    return locale.text(value) if locale is not None else _text(value)


def _localized_plain(value, locale: ChallengeLocale | None) -> str:
    return _plain(_localized_text(value, locale))


def _localized_dungeon_text(
    locale: ChallengeLocale | None,
    dungeon_id: str,
    field_name: str,
    fallback,
) -> str:
    if locale is None:
        return _text(fallback)
    return locale.dungeon_text(dungeon_id, field_name, fallback)


def _localized_dungeon_plain(
    locale: ChallengeLocale | None,
    dungeon_id: str,
    field_name: str,
    fallback,
) -> str:
    return _plain(_localized_dungeon_text(locale, dungeon_id, field_name, fallback))


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "none", "null", "否"}
    return bool(value)


def _timestamp(value):
    number = _int(value)
    if not number and isinstance(value, str):
        text = value.strip()
        if text:
            try:
                number = int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
            except (TypeError, ValueError, OverflowError, OSError):
                number = 0
    return number // 1000 if number > 10_000_000_000 else number


def _plain(value):
    text = _text(value)
    text = re.sub(r"<@[^>]+>", "", text)
    text = re.sub(r"</?[^>]*>", "", text)
    text = re.sub(r"\{[^}]+\}", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _multiline(value):
    text = _plain(value)
    if not text:
        return ""
    return escape(text[:420], quote=False)


def _difficulty_label(value):
    return {"normal": "普通", "hard": "困难", "cruel": "残酷"}.get(value, value or "--")


def _monument_difficulty_label(value):
    return {"normal": "普通", "hard": "苦难"}.get(value, value or "--")


def _display_stage_name(value):
    return re.sub(r"·(?:苦难|困难|残酷)$", "", str(value or "")) or "未命名关卡"


def _format_duration(value):
    value = _int(value)
    if value <= 0:
        return "--"
    return f"{value // 60:02d}:{value % 60:02d}"


def _date(value):
    number = _timestamp(value)
    if not number:
        return "--"
    try:
        return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "--"


def _period_short(start, end) -> str:
    """档案头里的周期只到日期：带时分在窄栏里会折成一坨，起止同年时省掉第二个年份。"""
    s, e = _timestamp(start), _timestamp(end)
    s_text = _date(s)[:10] if s else ""
    e_text = _date(e)[:10] if e else ""
    if s_text and e_text:
        tail = e_text[5:] if e_text[:4] == s_text[:4] else e_text
        return f"{s_text} → {tail}"
    if s_text:
        return f"{s_text} 起"
    if e_text:
        return f"截至 {e_text}"
    return "时间未公开"


def _period(start, end):
    start_text = _date(start) if _timestamp(start) else ""
    end_text = _date(end) if _timestamp(end) else ""
    if start_text and end_text:
        return f"{start_text} — {end_text}"
    if start_text:
        return f"{start_text} 起"
    if end_text:
        return f"截至 {end_text}"
    return "时间未公开"

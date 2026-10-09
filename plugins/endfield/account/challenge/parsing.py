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

# 关卡特性里官方灰字提示（``<@ba.info>…</>``）的占位哨兵，只在解析与渲染之间传递。
_INFO_OPEN = "\x01"
_INFO_CLOSE = "\x02"


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
        raise ChallengeResolutionError("请提供影拓主题或关卡名称。")
    normalized_difficulty = difficulty if difficulty in {"normal", "hard"} else "hard"
    group: MonumentGroup | None = None
    stage_query = " ".join(str(item) for item in terms).strip()
    group_query = str(terms[0]).strip()
    if len(terms) > 1:
        group = _pick(group_query, payload.groups, lambda item: item.name, rank=_monument_group_rank)
        stage_query = " ".join(str(item) for item in terms[1:]).strip()
    else:
        group = _pick_or_none(group_query, payload.groups, lambda item: item.name, rank=_monument_group_rank)
    if group is not None:
        stages = [pair[0 if normalized_difficulty == "normal" else 1] for pair in group.stages]
        if len(terms) == 1:
            if not stages:
                raise ChallengeResolutionError(f"主题“{group.name}”下暂无关卡记录。")
            return group, stages[0]
        stage = _pick(
            stage_query,
            stages,
            lambda item: item.name,
            rank=_monument_stage_rank,
            path=lambda item: (group.name,),
        )
        return group, stage
    candidates: list[tuple[MonumentGroup, MonumentDungeon]] = []
    for item in payload.groups:
        for pair in item.stages:
            dungeon = pair[0 if normalized_difficulty == "normal" else 1]
            candidates.append((item, dungeon))
    chosen = _pick(
        stage_query,
        candidates,
        lambda item: item[1].name,
        rank=_monument_pair_rank,
        path=lambda item: (item[0].name,),
    )
    return chosen


def resolve_war_detail(
    payload: WarEchoPayload,
    terms: Sequence[str],
    difficulty: str = "cruel",
) -> tuple[WarSeason, WarWeek, WarGroup, WarDungeon]:
    if not terms:
        raise ChallengeResolutionError("请提供战争回响赛季、轮换或关卡名称。")
    normalized_difficulty = difficulty if difficulty in {"normal", "hard", "cruel"} else "cruel"
    season, week, rest = _war_scope(payload, terms)
    if season is not None:
        if week is None:
            week = _war_shown_week(season)
        if week is None:
            raise ChallengeResolutionError(f"赛季“{season.name}”下暂无轮换记录。")
        if not rest:
            # 只点到赛季/轮换时该看赛季卡（resolve_war_season_page），
            # 不能替玩家挑轮换里的第一关。
            raise ChallengeResolutionError(f"请在“{week.name}”后补充关卡名称。")
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
        selected_week, group, dungeon = _pick(
            " ".join(rest),
            dungeon_candidates,
            lambda item: item[2].name,
            rank=lambda item: (item[0].current(), item[0].end_ts),
            path=lambda item: (season.name, item[0].name),
        )
        return season, selected_week, group, dungeon

    candidates = [
        (season_item, week, group, group.dungeon(normalized_difficulty))
        for season_item in payload.seasons
        for week in season_item.weeks
        for group in week.groups
        if group.dungeon(normalized_difficulty) is not None
    ]
    return _pick(
        " ".join(str(item) for item in terms),
        candidates,
        lambda item: item[3].name,
        rank=_war_rank,
        path=lambda item: (item[0].name, item[1].name),
    )


def resolve_war_season_page(payload: WarEchoPayload, terms: Sequence[str]) -> int | None:
    """Route terms that stop at a season or rotation to that season's card.

    ``None`` means a stage was named and the caller should resolve the detail.
    ``0`` is the running overview (what ``/ef 回响`` shows), only for the
    rotation it shows; any other value is the 1-based ``/ef 回响 历史`` page
    holding that season, whose card lists every rotation.  A bare season name
    asks for the whole season, so it gets that page even while it is running.
    """
    season, week, rest = _war_scope(payload, terms)
    if season is None or rest:
        return None
    if week is not None and season is payload.current() and week is _war_shown_week(season):
        return 0
    return next(index for index, item in enumerate(payload.seasons, 1) if item is season)


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
        feature=_localized_dungeon_plain(
            locale, dungeon_id, "feature", raw.get("feature"), keep_info=True
        ),
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
        feature=_localized_dungeon_plain(
            locale, dungeon_id, "feature", raw.get("feature"), keep_info=True
        ),
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


def _pick(query, items, label, *, rank=None, path=None):
    """Resolve one record by name.

    ``rank`` breaks ties between records that share a name: the same stage is
    reused across rotations and seasons, so an identical label is not real
    ambiguity -- only close-but-different labels are.  ``path`` maps a
    candidate to the season/rotation names that narrow it down, so the
    ambiguity message can suggest an example that really resolves.
    """
    if not items:
        raise ChallengeResolutionError(f"未查询到“{query}”。")
    normalized = _normalize(query)
    exact = [item for item in items if _normalize(label(item)) == normalized]
    if exact:
        return _best(exact, rank)
    scored = sorted(((item, _score(normalized, _normalize(label(item)))) for item in items), key=lambda pair: pair[1], reverse=True)
    if not scored or scored[0][1] < 0.38:
        raise ChallengeResolutionError(f"未查询到“{query}”，可使用“历史”指令查看可用名称。")
    best = scored[0][1]
    close = [item for item, score in scored if best - score < 0.08]
    if len({_normalize(label(item)) for item in close}) > 1:
        preferred = _best(close, rank)
        # Keep the preferred candidate first: callers echo ``candidates[0]``
        # together with ``path``, so the two must describe the same record.
        preferred_label = label(preferred)
        ordered = [preferred_label] + [label(item) for item in close if label(item) != preferred_label]
        raise ChallengeAmbiguousError(
            query,
            ordered,
            path=path(preferred) if path is not None else (),
        )
    return _best(close, rank)


def _best(items, rank):
    if rank is None:
        return items[0]
    return max(items, key=rank)


def _pick_or_none(query, items, label, *, rank=None, path=None):
    try:
        return _pick(query, items, label, rank=rank, path=path)
    except ChallengeResolutionError:
        return None


def _war_rank(item):
    """Prefer the rotation that is running now, then the newest one."""
    week, season = item[1], item[0]
    return (week.current(), season.current(), week.end_ts, season.end_ts)


def _war_period_rank(item):
    """Same preference for a bare season or rotation: running now, then newest."""
    return (item.current(), item.end_ts)


# 赛季与轮换共用一个词干：「错视赛季」「错视轮换Ⅲ」都是「错视」。不带编号的
# 「错视轮换」因此是赛季眼下那一轮的别称而不是关卡名；轮换编号的 Ⅲ / III / 3 视为同一个。
_WAR_ROTATION_NUMBER = re.compile(r"[ⅰ-ⅻ]|[ivx]+|\d+")
_WAR_SCOPE_NAME = re.compile(rf"(?P<stem>.*?)(?:赛季|轮换(?P<number>{_WAR_ROTATION_NUMBER.pattern})?)")
_UNICODE_ROMAN = "ⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹⅺⅻ"
_ASCII_ROMAN = {"i": 1, "v": 5, "x": 10}


def _war_scope(payload, terms):
    """Split the leading season/rotation terms off; whatever is left names a stage."""
    rest = [str(item) for item in terms]
    if not rest:
        return None, None, rest
    # 关卡全名不让给赛季/轮换的模糊匹配：0.38 的门槛下，和赛季名撞两个字就够了。
    stages = _war_stage_names(payload)
    season, week = _war_scope_head(payload, rest[0], stages)
    if season is None:
        return None, None, rest
    rotation = _war_rotation_alias(rest.pop(0))
    if week is None and rest:
        week = _war_scope_week(season, rest[0], stages)
        if week is not None or _war_season_alias(season, rest[0]):
            rotation = rotation or _war_rotation_alias(rest[0])
            rest.pop(0)
    if week is None and rotation:
        # 「错视轮换」点的是轮换，不是整个赛季：落到赛季眼下显示的那一轮。
        week = _war_shown_week(season)
    return season, week, rest


def _war_scope_head(payload, query, stages):
    """Resolve the first term to ``(season, rotation-or-None)``, before any stage name."""
    normalized = _normalize(query)
    exact = [item for item in payload.seasons if _normalize(item.name) == normalized]
    if exact:
        return _best(exact, _war_period_rank), None
    stem, number = _war_scope_key(query)
    rotations = [
        (season, week)
        for season in payload.seasons
        for week in season.weeks
        if _normalize(week.name) == normalized or (number and _war_scope_key(week.name) == (stem, number))
    ]
    if rotations:
        return _best(rotations, _war_rank)
    seasons = [item for item in payload.seasons if stem and stem in _war_season_stems(item)]
    if seasons:
        return _best(seasons, _war_period_rank), None
    if normalized in stages:
        return None, None
    season = _pick_or_none(
        query,
        payload.seasons,
        lambda item: item.name,
        rank=_war_period_rank,
        path=lambda item: (item.name,),
    )
    return season, None


def _war_scope_week(season, query, stages):
    stem, number = _war_scope_key(query)
    if not number and _WAR_ROTATION_NUMBER.fullmatch(stem):
        # 「错视轮换 3」：编号被空格拆成了单独一项。
        stem, number = "", _rotation_order(stem)
    if number:
        numbered = [
            item for item in season.weeks
            if _war_scope_key(item.name)[1] == number and stem in ("", _war_scope_key(item.name)[0])
        ]
        if numbered:
            return _best(numbered, _war_period_rank)
    if _normalize(query) in stages:
        return None
    return _pick_or_none(
        query,
        season.weeks,
        lambda item: item.name,
        rank=_war_period_rank,
        path=lambda item: (season.name, item.name),
    )


def _war_season_alias(season, query):
    """「错视赛季 错视轮换」里的第二项：没有编号、词干还是这个赛季。"""
    stem, number = _war_scope_key(query)
    return not number and (not stem or stem in _war_season_stems(season))


def _war_rotation_alias(query):
    """「错视轮换」「轮换」：不带编号的轮换，区别于只点到赛季的「错视赛季」「错视」。"""
    return _normalize(query).endswith("轮换")


def _war_shown_week(season):
    return season.current_week() or (season.weeks[-1] if season.weeks else None)


def _war_stage_names(payload):
    return {
        _normalize(name)
        for season in payload.seasons
        for week in season.weeks
        for group in week.groups
        for name in (group.name, *(item.name for item in (group.normal, group.hard, group.cruel) if item))
    }


def _war_season_stems(season):
    stems = {_war_scope_key(season.name)[0]} | {_war_scope_key(week.name)[0] for week in season.weeks}
    stems.discard("")
    return stems


def _war_scope_key(value):
    """「错视赛季」→ ("错视", 0)；「错视轮换Ⅲ」「错视轮换III」「错视轮换3」→ ("错视", 3)。"""
    text = _normalize(value)
    match = _WAR_SCOPE_NAME.fullmatch(text)
    if match is None:
        return text, 0
    number = match.group("number")
    return match.group("stem"), _rotation_order(number) if number else 0


def _rotation_order(number):
    if number.isdecimal():
        return int(number)
    if number in _UNICODE_ROMAN:
        return _UNICODE_ROMAN.index(number) + 1
    digits = [_ASCII_ROMAN[char] for char in number]
    return sum(-item if item < following else item for item, following in zip(digits, digits[1:] + [0]))


def _monument_group_rank(group):
    return (group.is_active, group.end_ts)


def _monument_stage_rank(dungeon):
    return (dungeon.record.available, dungeon.record.record_ts, dungeon.passed)


def _monument_pair_rank(item):
    group, dungeon = item
    return (_monument_group_rank(group), _monument_stage_rank(dungeon))


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
    *,
    keep_info: bool = False,
) -> str:
    text = _localized_dungeon_text(locale, dungeon_id, field_name, fallback)
    return _plain_feature(text) if keep_info else _plain(text)


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


def _plain_feature(value):
    """关卡特性专用：留下官方 ``<@ba.info>`` 灰字提示的边界。

    ``_plain`` 会把 ``<@ba.info>`` 和普通标签一起删掉，官方灰字小字提示就混成了
    普通条目。这里把 info 段落换成一对哨兵字符，让渲染层能把它还原成灰字；
    哨兵不出卡片，由 draw 侧剥离。
    """
    text = _text(value)
    text = re.sub(
        r"<@ba\.info>(.*?)</>",
        lambda match: f"{_INFO_OPEN}{match.group(1)}{_INFO_CLOSE}",
        text,
        flags=re.S,
    )
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

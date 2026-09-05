from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AccountUiPayload:
    detail: JsonObject
    crisis_contract: JsonObject | None
    indie_hard_groups: tuple[JsonObject, ...]
    crisis_record: JsonObject | None = None

    @classmethod
    def from_responses(
        cls,
        detail_response: Mapping[str, Any],
        crisis_response: Mapping[str, Any] | None = None,
        indie_hard_response: Mapping[str, Any] | None = None,
        crisis_record_response: Mapping[str, Any] | None = None,
    ) -> "AccountUiPayload":
        detail = dict(((detail_response.get("data") or {}).get("detail") or {}))
        crisis = None
        if crisis_response is not None:
            crisis_value = (crisis_response.get("data") or {}).get("crisisContract")
            if isinstance(crisis_value, Mapping):
                crisis = dict(crisis_value)
        indie_groups: tuple[JsonObject, ...] = ()
        if indie_hard_response is not None:
            groups = ((indie_hard_response.get("data") or {}).get("indieHard") or {}).get(
                "indieHardGroups"
            ) or []
            indie_groups = tuple(dict(group) for group in groups if isinstance(group, Mapping))
        crisis_record = None
        if crisis_record_response is not None:
            record_value = (crisis_record_response.get("data") or {}).get("recordDetail")
            if isinstance(record_value, Mapping):
                crisis_record = dict(record_value)
        return cls(
            detail=detail,
            crisis_contract=crisis,
            indie_hard_groups=indie_groups,
            crisis_record=crisis_record,
        )

    @property
    def base(self) -> JsonObject:
        return dict(self.detail.get("base") or {})

    @property
    def characters(self) -> tuple[JsonObject, ...]:
        return tuple(
            dict(character)
            for character in (self.detail.get("chars") or [])
            if isinstance(character, Mapping)
        )

    def character_map(self) -> dict[str, JsonObject]:
        return {
            str(character.get("id")): character
            for character in self.characters
            if character.get("id")
        }

    def displayed_characters(self, limit: int = 4) -> tuple[JsonObject, ...]:
        characters = self.character_map()
        config = self.detail.get("config") or {}
        ordered = [
            characters[character_id]
            for character_id in (config.get("charIds") or [])
            if character_id in characters
        ]
        if not ordered:
            ordered = sorted(
                self.characters,
                key=lambda character: (
                    int(character.get("level") or 0),
                    int((character.get("charData") or {}).get("rarity", {}).get("value") or 0),
                ),
                reverse=True,
            )
        return tuple(ordered[:limit])

    def displayed_medals(self, limit: int = 10) -> tuple[JsonObject, ...]:
        achieve = self.detail.get("achieve") or {}
        medals = {
            str((medal.get("achievementData") or {}).get("id")): medal
            for medal in (achieve.get("achieveMedals") or [])
            if isinstance(medal, Mapping)
        }
        display = achieve.get("display") or {}
        ordered = [
            medals[medal_id]
            for _, medal_id in sorted(display.items(), key=lambda item: int(item[0]))
            if medal_id in medals
        ]
        if not ordered:
            ordered = list(medals.values())
        return tuple(dict(medal) for medal in ordered[:limit])

    def active_indie_group(self) -> JsonObject | None:
        for group in self.indie_hard_groups:
            if group.get("isInActivity"):
                return group
        return self.indie_hard_groups[0] if self.indie_hard_groups else None

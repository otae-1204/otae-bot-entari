from __future__ import annotations

import hashlib
import html
import mimetypes
import re
import tempfile
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from utils.http_client import fetch_many_resilient
from utils.image_utils import BrowserResource, screenshot_web_element
from utils.temp_files import schedule_temp_file_cleanup

from .account_models import AccountUiPayload, JsonObject
from .account_i18n import localized_text, semantic_label


CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 1080
PROFILE_CANVAS_WIDTH = 1700
PROFILE_CANVAS_HEIGHT = 998
REMOTE_ASSET_NAMESPACE = "endfield-account-ui-assets"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WARFARIN_STATIC_BASE = "https://static.warfarin.wiki/v4"
FONT_ASSETS = {
    "cn": (
        "https://endfield-account.local/fonts/HarmonyOS-Sans-SC-Regular.ttf",
        PROJECT_ROOT / "plugins/endfield/assets/fonts/HarmonyOS_Sans_SC_Regular.ttf",
        400,
    ),
    "cn-bold": (
        "https://endfield-account.local/fonts/HarmonyOS-Sans-SC-Bold.ttf",
        PROJECT_ROOT / "plugins/endfield/assets/fonts/HarmonyOS_Sans_SC_Bold.ttf",
        700,
    ),
    "cn-medium": (
        "https://endfield-account.local/fonts/HarmonyOS-Sans-SC-Medium.ttf",
        PROJECT_ROOT / "plugins/endfield/assets/fonts/HarmonyOS_Sans_SC_Medium.ttf",
        500,
    ),
    "hud": (
        "https://endfield-account.local/fonts/Bahnschrift.ttf",
        Path("C:/Windows/Fonts/bahnschrift.ttf"),
        400,
    ),
    "hud-bold": (
        "https://endfield-account.local/fonts/ArialNarrowBold.ttf",
        Path("C:/Windows/Fonts/ARIALNB.TTF"),
        700,
    ),
}
UI_ASSET_ROOT = PROJECT_ROOT / "plugins/endfield/assets/ui"
UI_ASSETS = {
    "profile-background": ("https://endfield-account.local/ui/profile-background.png", UI_ASSET_ROOT / "profile_background.png"),
    "profile-mission": ("https://endfield-account.local/ui/profile-mission.png", UI_ASSET_ROOT / "profile_mission.png"),
    "profile-personal": ("https://endfield-account.local/ui/profile-personal.png", UI_ASSET_ROOT / "profile_personal.png"),
    "profile-friend": ("https://endfield-account.local/ui/profile-friend.png", UI_ASSET_ROOT / "profile_friend.png"),
    "profile-add": ("https://endfield-account.local/ui/profile-add.png", UI_ASSET_ROOT / "profile_add.png"),
    "profile-authority": ("https://endfield-account.local/ui/profile-authority.png", UI_ASSET_ROOT / "profile_authority.png"),
    "profile-explore": ("https://endfield-account.local/ui/profile-explore.png", UI_ASSET_ROOT / "profile_explore.png"),
    "profile-reset": ("https://endfield-account.local/ui/profile-reset.png", UI_ASSET_ROOT / "profile_reset.png"),
    "profile-theme": ("https://endfield-account.local/ui/profile-theme.png", UI_ASSET_ROOT / "profile_theme.png"),
    "profile-medal-side": ("https://endfield-account.local/ui/profile-medal-side.png", UI_ASSET_ROOT / "profile_medal_side.png"),
    "profile-medal-entry": ("https://endfield-account.local/ui/profile-medal-entry.png", UI_ASSET_ROOT / "profile_medal_entry.png"),
    "profile-medal-deco": ("https://endfield-account.local/ui/profile-medal-deco.png", UI_ASSET_ROOT / "profile_medal_deco.png"),
    "profile-medal-detail": ("https://endfield-account.local/ui/profile-medal-detail.png", UI_ASSET_ROOT / "profile_medal_detail.png"),
    "profile-medal-caption": ("https://endfield-account.local/ui/profile-medal-caption.png", UI_ASSET_ROOT / "profile_medal_caption.png"),
    "profile-medal-label": ("https://endfield-account.local/ui/profile-medal-label.png", UI_ASSET_ROOT / "profile_medal_label.png"),
    "profile-role-bg": ("https://endfield-account.local/ui/profile-role-bg.png", UI_ASSET_ROOT / "profile_role_bg.png"),
    "profile-stat-agent": ("https://endfield-account.local/ui/profile-stat-agent.png", UI_ASSET_ROOT / "profile_stat_agent.png"),
    "profile-stat-arms": ("https://endfield-account.local/ui/profile-stat-arms.png", UI_ASSET_ROOT / "profile_stat_arms.png"),
    "profile-stat-files": ("https://endfield-account.local/ui/profile-stat-files.png", UI_ASSET_ROOT / "profile_stat_files.png"),
    "profile-avatar-corner": ("https://endfield-account.local/ui/profile-avatar-corner.png", UI_ASSET_ROOT / "profile_avatar_corner.png"),
    "profile-avatar-stripes": ("https://endfield-account.local/ui/profile-avatar-stripes.png", UI_ASSET_ROOT / "profile_avatar_stripes.png"),
    "profile-avatar-code": ("https://endfield-account.local/ui/profile-avatar-code.png", UI_ASSET_ROOT / "profile_avatar_code.png"),
    "profile-role-ghost": ("https://endfield-account.local/ui/profile-role-ghost.png", UI_ASSET_ROOT / "profile_role_ghost.png"),
    "profile-role-logo": ("https://endfield-account.local/ui/profile-role-logo.png", UI_ASSET_ROOT / "profile_role_logo.png"),
    "profile-profession-0": ("https://endfield-account.local/ui/profile-profession-0.png", UI_ASSET_ROOT / "profile_profession_0.png"),
    "profile-profession-1": ("https://endfield-account.local/ui/profile-profession-1.png", UI_ASSET_ROOT / "profile_profession_1.png"),
    "profile-profession-2": ("https://endfield-account.local/ui/profile-profession-2.png", UI_ASSET_ROOT / "profile_profession_2.png"),
    "profile-profession-3": ("https://endfield-account.local/ui/profile-profession-3.png", UI_ASSET_ROOT / "profile_profession_3.png"),
    "profile-profession-4": ("https://endfield-account.local/ui/profile-profession-4.png", UI_ASSET_ROOT / "profile_profession_4.png"),
    "profile-profession-5": ("https://endfield-account.local/ui/profile-profession-5.png", UI_ASSET_ROOT / "profile_profession_5.png"),
    "profile-profession-6": ("https://endfield-account.local/ui/profile-profession-6.png", UI_ASSET_ROOT / "profile_profession_6.png"),
    "profile-profession-7": ("https://endfield-account.local/ui/profile-profession-7.png", UI_ASSET_ROOT / "profile_profession_7.png"),
    "profile-profession-8": ("https://endfield-account.local/ui/profile-profession-8.png", UI_ASSET_ROOT / "profile_profession_8.png"),
    "profile-property-cryst": ("https://endfield-account.local/ui/profile-property-cryst.png", UI_ASSET_ROOT / "profile_property_cryst.png"),
    "profile-property-fire": ("https://endfield-account.local/ui/profile-property-fire.png", UI_ASSET_ROOT / "profile_property_fire.png"),
    "profile-property-natural": ("https://endfield-account.local/ui/profile-property-natural.png", UI_ASSET_ROOT / "profile_property_natural.png"),
    "profile-property-physical": ("https://endfield-account.local/ui/profile-property-physical.png", UI_ASSET_ROOT / "profile_property_physical.png"),
    "profile-property-pulse": ("https://endfield-account.local/ui/profile-property-pulse.png", UI_ASSET_ROOT / "profile_property_pulse.png"),
    "profile-potential-0": ("https://endfield-account.local/ui/profile-potential-0.png", UI_ASSET_ROOT / "profile_potential_0.png"),
    "profile-medal-device": ("https://endfield-account.local/ui/profile-medal-device.png", UI_ASSET_ROOT / "profile_medal_device.png"),
    "profile-medal-micro": ("https://endfield-account.local/ui/profile-medal-micro.png", UI_ASSET_ROOT / "profile_medal_micro.png"),
    "profile-panel-mask": ("https://endfield-account.local/ui/profile-panel-mask.png", UI_ASSET_ROOT / "profile_panel_mask.png"),
    "copy": ("https://endfield-account.local/ui/common-copy.png", UI_ASSET_ROOT / "common_copy.png"),
    "hide": ("https://endfield-account.local/ui/common-hide.png", UI_ASSET_ROOT / "common_hide.png"),
    "edit": ("https://endfield-account.local/ui/common-edit.png", UI_ASSET_ROOT / "common_edit.png"),
    "more": ("https://endfield-account.local/ui/common-more.png", UI_ASSET_ROOT / "common_more.png"),
    "close": ("https://endfield-account.local/ui/common-close.png", UI_ASSET_ROOT / "common_close.png"),
    "confirm": ("https://endfield-account.local/ui/common-confirm.png", UI_ASSET_ROOT / "common_confirm.png"),
    "info": ("https://endfield-account.local/ui/common-info.png", UI_ASSET_ROOT / "common_info.png"),
    "contract-score": ("https://endfield-account.local/ui/contract-score.png", UI_ASSET_ROOT / "contract_score.png"),
    "contract-entry": ("https://endfield-account.local/ui/contract-entry.png", UI_ASSET_ROOT / "contract_entry.png"),
    "contract-tier": ("https://endfield-account.local/ui/contract-tier.png", UI_ASSET_ROOT / "contract_tier.png"),
    "contract-wave-bg": ("https://endfield-account.local/ui/contract-wave-bg.png", UI_ASSET_ROOT / "contract_wave_bg.png"),
    "contract-highest": ("https://endfield-account.local/ui/contract-highest.png", UI_ASSET_ROOT / "contract_highest.png"),
    "contract-total": ("https://endfield-account.local/ui/contract-total.png", UI_ASSET_ROOT / "contract_total.png"),
    "contract-again": ("https://endfield-account.local/ui/contract-again.png", UI_ASSET_ROOT / "contract_again.png"),
    "contract-weapon-bg": ("https://endfield-account.local/ui/contract-weapon-bg.png", UI_ASSET_ROOT / "contract_weapon_bg.png"),
    "contract-role-cell": ("https://endfield-account.local/ui/contract-role-cell.png", UI_ASSET_ROOT / "contract_role_cell.png"),
    "contract-equip-mask": ("https://endfield-account.local/ui/contract-equip-mask.png", UI_ASSET_ROOT / "contract_equip_mask.png"),
    "contract-logo": ("https://endfield-account.local/ui/contract-logo.png", UI_ASSET_ROOT / "contract_logo.png"),
    "contract-stripes": ("https://endfield-account.local/ui/contract-stripes.png", UI_ASSET_ROOT / "contract_stripes.png"),
    "contract-title-deco": ("https://endfield-account.local/ui/contract-title-deco.png", UI_ASSET_ROOT / "contract_title_deco.png"),
    "contract-success-line": ("https://endfield-account.local/ui/contract-success-line.png", UI_ASSET_ROOT / "contract_success_line.png"),
    "contract-role-watermark": ("https://endfield-account.local/ui/contract-role-watermark.png", UI_ASSET_ROOT / "contract_role_watermark.png"),
    "potential-0": ("https://endfield-account.local/ui/potential-0.png", UI_ASSET_ROOT / "potential_0.png"),
    "potential-1": ("https://endfield-account.local/ui/potential-1.png", UI_ASSET_ROOT / "potential_1.png"),
    "potential-2": ("https://endfield-account.local/ui/potential-2.png", UI_ASSET_ROOT / "potential_2.png"),
    "potential-3": ("https://endfield-account.local/ui/potential-3.png", UI_ASSET_ROOT / "potential_3.png"),
    "potential-4": ("https://endfield-account.local/ui/potential-4.png", UI_ASSET_ROOT / "potential_4.png"),
    "potential-5": ("https://endfield-account.local/ui/potential-5.png", UI_ASSET_ROOT / "potential_5.png"),
    "dungeon-correct": ("https://endfield-account.local/ui/dungeon-correct.png", UI_ASSET_ROOT / "dungeon_correct.png"),
    "dungeon-active-triangle": ("https://endfield-account.local/ui/dungeon-active-triangle.png", UI_ASSET_ROOT / "dungeon_active_triangle.png"),
    "dungeon-more": ("https://endfield-account.local/ui/dungeon-more.png", UI_ASSET_ROOT / "dungeon_more.png"),
    "dungeon-hard-bg": ("https://endfield-account.local/ui/dungeon-hard-bg.png", UI_ASSET_ROOT / "dungeon_hard_bg.png"),
    "dungeon-title": ("https://endfield-account.local/ui/dungeon-title.png", UI_ASSET_ROOT / "dungeon_title.png"),
    "dungeon-redline": ("https://endfield-account.local/ui/dungeon-redline.png", UI_ASSET_ROOT / "dungeon_redline.png"),
}
PROFESSION_ICON_IDS = {
    "profession_guard": 0,
    "profession_defender": 2,
    "profession_supporter": 4,
    "profession_caster": 5,
    "profession_vanguard": 7,
    "profession_assault": 8,
}
PROPERTY_ICON_NAMES = {
    "char_property_cryst": "icon_charattrtype_cold",
    "char_property_fire": "icon_charattrtype_fire",
    "char_property_natural": "icon_charattrtype_nature",
    "char_property_physical": "icon_charattrtype_physical",
    "char_property_pulse": "icon_charattrtype_pulse",
}


HUD_SVG_DEFS = """
<svg class="hud-defs" aria-hidden="true" focusable="false">
  <defs>
    <symbol id="hud-search" viewBox="0 0 24 24">
      <circle cx="10.5" cy="10.5" r="5.4" fill="none" stroke="currentColor" stroke-width="2.4"/>
      <path d="m14.4 14.4 4.8 4.8" fill="none" stroke="currentColor" stroke-width="2.7" stroke-linecap="square"/>
    </symbol>
    <symbol id="hud-profile-card" viewBox="0 0 32 24">
      <path d="M2 3h28v18H2zM7 7h7v10H7z" fill="none" stroke="currentColor" stroke-width="2.2"/>
      <path d="M18 8h8M18 12h8M18 16h5" fill="none" stroke="currentColor" stroke-width="2"/>
    </symbol>
    <symbol id="hud-user" viewBox="0 0 24 24">
      <circle cx="12" cy="7" r="4" fill="currentColor"/>
      <path d="M4.1 21c.6-5.2 3.1-8 7.9-8s7.3 2.8 7.9 8h-4.1c-.5-2.8-1.6-4.1-3.8-4.1S8.7 18.2 8.2 21z" fill="currentColor"/>
    </symbol>
    <symbol id="hud-user-plus" viewBox="0 0 28 24">
      <circle cx="9.5" cy="7" r="4" fill="currentColor"/>
      <path d="M1.5 21c.5-5.2 3-8 8-8 3.1 0 5.2 1.1 6.6 3.2-1.1.8-1.8 2-2.1 3.4-.9-1.8-2.2-2.7-4.5-2.7-2.2 0-3.4 1.3-3.9 4.1zM21 11v10M16 16h10" fill="currentColor" stroke="currentColor" stroke-width="2.5"/>
    </symbol>
    <symbol id="hud-close" viewBox="0 0 32 32">
      <path d="M4 8.2 8.2 4 16 11.8 23.8 4 28 8.2 20.2 16l7.8 7.8-4.2 4.2-7.8-7.8L8.2 28 4 23.8l7.8-7.8z" fill="currentColor"/>
      <path d="m7.5 3 8.5 8.5L24.5 3 29 7.5 20.5 16l8.5 8.5-4.5 4.5-8.5-8.5L7.5 29 3 24.5l8.5-8.5L3 7.5z" fill="none" stroke="currentColor" stroke-width="1.3" opacity=".45"/>
    </symbol>
    <symbol id="hud-ellipsis" viewBox="0 0 24 8">
      <circle cx="4" cy="4" r="2" fill="currentColor"/><circle cx="12" cy="4" r="2" fill="currentColor"/><circle cx="20" cy="4" r="2" fill="currentColor"/>
    </symbol>
    <symbol id="hud-copy" viewBox="0 0 24 24">
      <path d="M4 4h11v13H4zM9 8h11v13H9" fill="none" stroke="currentColor" stroke-width="2"/>
      <path d="M15 4v4h4" fill="none" stroke="currentColor" stroke-width="1.5"/>
    </symbol>
    <symbol id="hud-authority" viewBox="0 0 32 28">
      <path d="m3 9 6-5 7 6-6 5zm9 0 6-5 7 6-6 5zm-9 9 6-5 7 6-6 5zm9 0 6-5 7 6-6 5z" fill="currentColor"/>
      <path d="m20 17 4-4 5 5-5 6z" fill="none" stroke="currentColor" stroke-width="2"/>
    </symbol>
    <symbol id="hud-explore" viewBox="0 0 34 26">
      <path d="M2 13c3.6-6.2 8.6-9.2 15-9.2S28.4 6.8 32 13c-3.6 6.2-8.6 9.2-15 9.2S5.6 19.2 2 13Z" fill="none" stroke="currentColor" stroke-width="2.5"/>
      <circle cx="17" cy="13" r="5" fill="none" stroke="currentColor" stroke-width="3"/>
      <path d="M1 8 4 6M1 18l3 2M33 8l-3-2M33 18l-3 2" fill="none" stroke="currentColor" stroke-width="2"/>
    </symbol>
    <symbol id="hud-quest" viewBox="0 0 64 64">
      <path d="M32 3 43 18l18 7-9 17 2 19-22-9-22 9 2-19-9-17 18-7z" fill="none" stroke="currentColor" stroke-width="3"/>
      <path d="m21 18 11 34 11-34M13 30l39 12M51 30 12 42" fill="none" stroke="currentColor" stroke-width="3"/>
      <circle cx="32" cy="34" r="6" fill="currentColor"/>
    </symbol>
    <symbol id="hud-double-play" viewBox="0 0 28 18">
      <path d="m2 2 10 7-10 7zm12 0 10 7-10 7z" fill="currentColor"/>
    </symbol>
    <symbol id="hud-panel-open" viewBox="0 0 28 28">
      <path d="M3 3h22v22H3z" fill="none" stroke="currentColor" stroke-width="2.5"/>
      <path d="M10 18 20 8M13 8h7v7" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="square"/>
    </symbol>
    <symbol id="hud-medal-device" viewBox="0 0 52 52">
      <path d="M4 4h17v17H4zM31 4h17v17H31zM4 31h17v17H4z" fill="none" stroke="currentColor" stroke-width="3"/>
      <path d="m33 31 15 15M48 31 33 46" fill="none" stroke="currentColor" stroke-width="4"/>
    </symbol>
    <symbol id="hud-operators" viewBox="0 0 100 86">
      <circle cx="31" cy="22" r="15" fill="none" stroke="currentColor" stroke-width="4"/><circle cx="66" cy="22" r="15" fill="none" stroke="currentColor" stroke-width="4"/>
      <path d="M9 80V53c0-13 9-20 22-20s22 7 22 20v27M47 80V53c0-13 7-20 19-20s21 7 21 20v27" fill="none" stroke="currentColor" stroke-width="4"/>
      <path d="M4 58h30M7 65h27M10 72h24" stroke="currentColor" stroke-width="2" opacity=".8"/>
    </symbol>
    <symbol id="hud-theme-card" viewBox="0 0 28 28">
      <path d="M3 6h22v16H3z" fill="none" stroke="currentColor" stroke-width="2.4"/>
      <path d="m7 18 6-8 3 4 5-6M7 9h3" fill="none" stroke="currentColor" stroke-width="2.7"/>
      <path d="M2 4h22M6 24h20" stroke="currentColor" stroke-width="1.5" opacity=".65"/>
    </symbol>
    <symbol id="hud-eye-off" viewBox="0 0 30 24">
      <path d="M2 12c3.7-5.4 8-8 13-8s9.3 2.6 13 8c-3.7 5.4-8 8-13 8S5.7 17.4 2 12Z" fill="none" stroke="currentColor" stroke-width="2.4"/>
      <circle cx="15" cy="12" r="4" fill="currentColor"/><path d="M4 22 26 2" stroke="currentColor" stroke-width="3.5"/>
    </symbol>
    <symbol id="hud-reset" viewBox="0 0 28 28">
      <path d="M23.5 9A10 10 0 1 1 18 4.8" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="square"/>
      <path d="M17 1h8v8" fill="none" stroke="currentColor" stroke-width="3"/>
    </symbol>
    <symbol id="hud-stage-pass" viewBox="0 0 28 24">
      <path d="M2 6h5v5h4v5h6v-5h4V6h5v10h-4v4h-4v3h-8v-3H6v-4H2z" fill="currentColor"/>
    </symbol>
    <symbol id="hud-info" viewBox="0 0 24 24">
      <circle cx="12" cy="12" r="10" fill="currentColor"/><path d="M12 10v7M12 6.5v.2" stroke="var(--icon-cut,#222)" stroke-width="2.5" stroke-linecap="square"/>
    </symbol>
    <symbol id="hud-score" viewBox="0 0 34 28">
      <path d="M3 18h9l4-7 4 13 4-9h7" fill="none" stroke="currentColor" stroke-width="3"/>
      <path d="M5 7h13M3 11h9M22 5h9M25 9h6" fill="none" stroke="currentColor" stroke-width="3"/>
      <path d="m7 22 5 4m9-2 4-4" fill="none" stroke="currentColor" stroke-width="2"/>
    </symbol>
    <symbol id="hud-clock" viewBox="0 0 28 28">
      <circle cx="14" cy="15" r="10" fill="currentColor"/><path d="M10 2h8v4h-8z" fill="currentColor"/><path d="M14 9v7l5 3" fill="none" stroke="var(--icon-cut,#333)" stroke-width="2.4"/>
    </symbol>
    <symbol id="hud-tier-shield" viewBox="0 0 60 64">
      <path d="M6 13 30 1l24 12-4 38-20 12L10 51z" fill="#252932" stroke="#8b1622" stroke-width="6"/>
      <path d="m13 20 17 31 17-31-7-4-10 19-10-19z" fill="#f4f3ef"/>
      <path d="M8 12h44M13 51h34" stroke="#b21d2c" stroke-width="4"/>
    </symbol>
    <symbol id="hud-rarity" viewBox="0 0 58 68">
      <path d="M3 0L6 6L6 8L11 13L14 14L18 18L16 20L15 20L14 22L13 22L11 20L10 15L10 18L8 20L7 26L4 29L0 31L0 38L4 36L7 33L9 33L11 35L11 38L10 39L6 39L6 40L10 42L15 47L16 54L18 56L18 59L19 60L20 66L21 67L22 67L22 63L23 62L23 55L22 54L22 51L20 47L21 45L26 45L27 46L26 50L27 50L33 45L57 45L57 43L54 42L51 39L36 39L35 38L38 32L40 34L42 34L43 35L41 28L39 25L41 22L41 19L43 15L43 12L45 9L46 3L47 2L39 8L38 13L36 15L36 18L35 20L33 21L29 18L33 14L34 14L23 14L14 7L11 6L7 2ZM22 22L26 23L32 28L32 31L31 32L30 37L28 39L19 39L17 37L17 35L15 33L15 30L14 29Z" fill="currentColor" fill-rule="evenodd"/>
    </symbol>
    <symbol id="hud-capsule" viewBox="0 0 22 12"><path d="M6 1h15l-5 10H1z" fill="currentColor"/><path d="m8 3 5 6" stroke="var(--icon-cut,#1a1a1c)" stroke-width="1.4"/></symbol>
    <symbol id="hud-slot" viewBox="0 0 22 20"><path d="m5 1 12 0 4 9-4 9H5l-4-9z" fill="currentColor"/><circle cx="11" cy="10" r="3.5" fill="var(--icon-cut,#142536)"/></symbol>
    <symbol id="hud-retry" viewBox="0 0 28 28">
      <path d="M23 9A10 10 0 1 1 18 4" fill="none" stroke="currentColor" stroke-width="3"/>
      <path d="M17 1h8v8" fill="none" stroke="currentColor" stroke-width="3"/>
    </symbol>
    <symbol id="hud-challenge" viewBox="0 0 28 26">
      <path d="m3 19 8-13h7L10 19zm12 1 8-13h3l-8 13z" fill="currentColor"/>
      <path d="M2 23h23" stroke="currentColor" stroke-width="2" opacity=".55"/>
    </symbol>
    <symbol id="hud-confirm" viewBox="0 0 28 28">
      <circle cx="14" cy="14" r="12" fill="currentColor"/><path d="m8 14 4 4 8-9" fill="none" stroke="var(--icon-cut,#333)" stroke-width="3"/>
    </symbol>
    <symbol id="hud-edit" viewBox="0 0 26 26">
      <path d="m5 18 2-7L18 0l7 7-11 11-7 2zM3 23h19" fill="currentColor"/><path d="m8 11 7 7" stroke="var(--icon-cut,#222)" stroke-width="2"/>
    </symbol>
    <symbol id="hud-network" viewBox="0 0 24 22"><path d="M2 20h3v-6H2zm5 0h3V10H7zm5 0h3V6h-3zm5 0h3V2h-3z" fill="currentColor"/></symbol>
    <symbol id="hud-challenge-mode" viewBox="0 0 34 34">
      <path d="M2 21h7v-7h6V7h6v7h6v7h7l-16 12z" fill="currentColor"/>
      <path d="m12 4 6 6 6-6" fill="none" stroke="currentColor" stroke-width="3"/>
    </symbol>
  </defs>
</svg>
"""


@dataclass(frozen=True, slots=True)
class PreparedAccountHtml:
    html: str
    resources: dict[str, BrowserResource]


async def draw_account_overview(payload: AccountUiPayload) -> bytes:
    return await _draw_prepared(await prepare_account_overview_html(payload))


async def draw_indie_hard(payload: AccountUiPayload) -> bytes:
    return await _draw_prepared(await prepare_indie_hard_html(payload))


async def draw_crisis_contract(payload: AccountUiPayload) -> bytes:
    return await _draw_prepared(await prepare_crisis_contract_html(payload))


async def render_account_overview_html(payload: AccountUiPayload) -> str:
    return (await _prepare_account_overview_html(payload, inline=True)).html


async def render_indie_hard_html(payload: AccountUiPayload) -> str:
    return (await _prepare_indie_hard_html(payload, inline=True)).html


async def render_crisis_contract_html(payload: AccountUiPayload) -> str:
    return (await _prepare_crisis_contract_html(payload, inline=True)).html


async def prepare_account_overview_html(payload: AccountUiPayload) -> PreparedAccountHtml:
    return await _prepare_account_overview_html(payload, inline=False)


async def prepare_indie_hard_html(payload: AccountUiPayload) -> PreparedAccountHtml:
    return await _prepare_indie_hard_html(payload, inline=False)


async def prepare_crisis_contract_html(payload: AccountUiPayload) -> PreparedAccountHtml:
    return await _prepare_crisis_contract_html(payload, inline=False)


async def _draw_prepared(prepared: PreparedAccountHtml) -> bytes:
    path = _write_temp_html(prepared.html)
    try:
        return await screenshot_web_element(
            path.resolve().as_uri(),
            ".account-ui",
            viewport=(CANVAS_WIDTH, CANVAS_HEIGHT),
            timeout_ms=20000,
            max_height=CANVAS_HEIGHT + 120,
            device_scale_factor=1.0,
            settle_ms=120,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(".panel", ".operator-card"),
        )
    finally:
        schedule_temp_file_cleanup(path, delay_seconds=30)


async def _prepare_account_overview_html(
    payload: AccountUiPayload,
    *,
    inline: bool,
) -> PreparedAccountHtml:
    base = payload.base
    characters = payload.displayed_characters(4)
    medals = payload.displayed_medals(10)
    background = _profile_background(payload)
    urls = [
        background,
        base.get("avatarUrl"),
        *(_profile_character_portrait(character) for character in characters),
        *(_medal_icon(medal) for medal in medals),
    ]
    assets = await _prepare_assets(urls, inline=inline)
    return PreparedAccountHtml(
        _profile_html(payload, assets.urls),
        assets.resources,
    )


async def _prepare_indie_hard_html(
    payload: AccountUiPayload,
    *,
    inline: bool,
) -> PreparedAccountHtml:
    group = payload.active_indie_group() or {}
    pairs = tuple(group.get("dungeonGroups") or [])
    dungeons = [dict(pair.get("normalDungeon") or {}) for pair in pairs if pair.get("normalDungeon")]
    selected_pair = pairs[-1] if pairs else {}
    selected = dict(
        selected_pair.get("hardDungeon")
        or selected_pair.get("normalDungeon")
        or (dungeons[-1] if dungeons else {})
    )
    achievement = group.get("achieve") or {}
    urls = [
        group.get("pic"),
        _medal_icon(achievement),
        *((enemy.get("imageUrl") for enemy in (selected.get("enemies") or [])[:4])),
        *((member.get("avatarUrl") for member in ((selected.get("bestRecord") or {}).get("chars") or [])[:4])),
    ]
    assets = await _prepare_assets(urls, inline=inline)
    return PreparedAccountHtml(
        _indie_html(group, dungeons, selected, assets.urls),
        assets.resources,
    )


async def _prepare_crisis_contract_html(
    payload: AccountUiPayload,
    *,
    inline: bool,
) -> PreparedAccountHtml:
    crisis = payload.crisis_contract or {}
    status = crisis.get("status") or {}
    best = (crisis.get("history") or {}).get("bestRecord") or {}
    team = _crisis_team(payload, best)
    indicators = crisis.get("indicators") or []
    urls = [
        status.get("kvImage"),
        status.get("headerImage"),
        _medal_icon(status.get("achieve") or {}),
        *((indicator.get("icon") for indicator in indicators[:28])),
    ]
    for character in team:
        urls.append(_character_portrait(character))
        urls.append(_weapon_icon(character))
        for equip in _character_equips(character):
            urls.append(_equip_icon(equip))
    assets = await _prepare_assets(urls, inline=inline)
    return PreparedAccountHtml(
        _crisis_html(payload, crisis, team, indicators[:28], assets.urls),
        assets.resources,
    )


@dataclass(frozen=True, slots=True)
class _PreparedAssets:
    urls: dict[str, str]
    resources: dict[str, BrowserResource]


async def _prepare_assets(urls: Iterable[Any], *, inline: bool) -> _PreparedAssets:
    unique_urls = tuple(
        dict.fromkeys(str(url) for url in urls if isinstance(url, str) and url)
    )
    # 图床的 404 与超时都是间歇的：走共享的退避重试。这里以前是单次 fetch_many，
    # 一次抖动就静默丢图；缺图原因由 fetch_many_resilient 自己写日志。
    fetched = await fetch_many_resilient(
        unique_urls,
        namespace=REMOTE_ASSET_NAMESPACE,
        timeout_seconds=12.0,
        max_bytes=16 * 1024 * 1024,
        log_prefix="[endfield]",
    )
    resources = fetched[0] if isinstance(fetched, tuple) else fetched
    output: dict[str, str] = {}
    browser_resources: dict[str, BrowserResource] = {}
    for url, resource in resources.items():
        if resource is None:
            output[url] = ""
            continue
        mime = resource.content_type or mimetypes.guess_type(url)[0] or "image/png"
        if inline:
            output[url] = f"data:{mime};base64,{base64.b64encode(resource.content).decode('ascii')}"
            continue
        digest = hashlib.sha256(resource.content).hexdigest()
        browser_url = f"https://endfield-account.local/assets/{digest}"
        output[url] = browser_url
        browser_resources[browser_url] = BrowserResource(resource.content, mime)
    for virtual_url, path, _weight in FONT_ASSETS.values():
        if not path.exists():
            continue
        content = path.read_bytes()
        mime = "font/ttf"
        if inline:
            output[virtual_url] = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        else:
            output[virtual_url] = virtual_url
            browser_resources[virtual_url] = BrowserResource(content, mime)
    for virtual_url, path in UI_ASSETS.values():
        if not path.exists():
            continue
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        if inline:
            output[virtual_url] = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        else:
            output[virtual_url] = virtual_url
            browser_resources[virtual_url] = BrowserResource(content, mime)
    return _PreparedAssets(output, browser_resources)


def _profile_html(payload: AccountUiPayload, assets: Mapping[str, str]) -> str:
    base = payload.base
    characters = payload.displayed_characters(4)
    medals = payload.displayed_medals(10)
    domains = tuple(payload.detail.get("domain") or [])[:2]
    crisis = payload.crisis_contract or {}
    status = crisis.get("status") or {}
    highest = status.get("highest") or "--"
    background = assets.get(_profile_background(payload), "")
    avatar = assets.get(str(base.get("avatarUrl") or ""), "")
    character_cards = "".join(_profile_character_card(character, assets) for character in characters)
    medal_cards = "".join(_profile_medal(medal, assets) for medal in medals)
    domain_cards = "".join(
        f'<div class="domain-card"><span>等级</span><b>{_text(domain.get("level"))}</b><strong>{_text(domain.get("name"))}</strong></div>'
        for domain in domains
    )
    return _document(
        "account-ui profile-ui",
        f"""
<div class="profile-stage">
<div class="profile-bg" style="--bg:url('{_attr(background)}')"></div>
<div class="profile-shade"></div>
<div class="profile-landmarks"><i></i><i></i><i></i><b></b></div>
<div class="profile-rail"></div>
<main class="profile-layout">
  <section class="identity-column">
    <div class="identity-row">
      <div class="avatar-wrap"><div class="avatar-frame"><img class="avatar-image" src="{_attr(avatar)}">{_ui_icon('profile-avatar-stripes', assets, 'avatar-stripes')}{_ui_icon('profile-avatar-code', assets, 'avatar-code')}{_ui_icon('profile-avatar-corner', assets, 'avatar-corner')}<span>PROFILE</span><i class="avatar-corners"></i></div></div>
      <div class="identity-copy">
        <div class="name-line"><strong>{_text(base.get('name') or '未知角色')}</strong>{_score_badge(highest, assets)}</div>
        <small class="identity-code"><i></i><i></i><i></i></small>
        <div class="sync-line"><b>COMMENT</b>{_hud_icon('double-play')}<strong>苏醒日</strong><span>{_format_date(base.get('saveTime'))}</span></div>
        <div class="public-id"><span>PUBLIC ID</span><b>{_text(base.get('roleId') or '--')}</b>{_ui_icon('copy', assets)}</div>
      </div>
    </div>
    <div class="level-stack">
      <div>{_ui_icon('profile-authority', assets, 'rank-icon')}<span>权限等级</span><strong>{_text(base.get('level'))}</strong></div>
      <div>{_ui_icon('profile-explore', assets, 'rank-icon')}<span>探索等级</span><strong>{_text(base.get('worldLevel'))}</strong></div>
    </div>
    <div class="mission-card">{_ui_icon('profile-stat-agent', assets, 'mission-texture')}<span class="mission-emblem">{_ui_icon('profile-mission', assets)}</span><div><b>{_text((base.get('mainMission') or {}).get('description') or '暂无记录')}</b><span>当前主线进度</span></div><em>ENDFIELD INDUSTRIES</em></div>
    <div class="stat-grid">
      {_stat_card(base.get('charNum'), '干员', 'profile-stat-agent', assets)}
      {_stat_card(base.get('weaponNum'), '武器', 'profile-stat-arms', assets)}
      {_stat_card(base.get('docNum'), '档案', 'profile-stat-files', assets)}
    </div>
    <div class="domain-title"><span>地区建设概况</span><i></i></div>
    <div class="domain-grid">{domain_cards}</div>
  </section>
  <section class="showcase-column">
    <div class="medal-panel panel">{_ui_icon('profile-medal-deco', assets, 'medal-panel-deco')}{_ui_icon('profile-medal-entry', assets, 'medal-entry-bg')}{_ui_icon('profile-medal-side', assets, 'medal-side-bg')}{_hud_icon('panel-open', 'panel-open')}{_ui_icon('profile-medal-device', assets, 'medal-device')}{_ui_icon('profile-medal-detail', assets, 'medal-detail')}{_ui_icon('profile-medal-label', assets, 'medal-label')}{_ui_icon('profile-medal-caption', assets, 'medal-caption')}
      <header><b>光荣之路</b></header>
      <div class="medal-grid">{medal_cards}</div>
      <div class="medal-micro">{_ui_icon('profile-medal-micro', assets)}</div>
    </div>
    <div class="operators-panel panel">{_ui_icon('profile-role-bg', assets, 'role-panel-bg')}{_hud_icon('panel-open', 'panel-open')}{_ui_icon('profile-role-ghost', assets, 'operator-ghost')}
      <header><span>{_ui_icon('profile-role-logo', assets)}</span><b><i></i><i></i><i></i>干员展示</b></header>
      <div class="operator-row">{character_cards}</div>
      <i class="operator-scroll"></i>
    </div>
    <div class="profile-signature"><span>“</span><b>终末地档案已同步</b>{_ui_icon('edit', assets)}<em>”</em></div>
  </section>
</main>
<div class="profile-network">{_hud_icon('network')}</div>
<footer class="hud-footer"><span>ANCHOR POINT</span><i></i><i></i><i></i></footer>
</div>
""",
        _profile_css(),
        assets,
    )


def _indie_html(
    group: JsonObject,
    dungeons: list[JsonObject],
    selected: JsonObject,
    assets: Mapping[str, str],
) -> str:
    background = assets.get(str(group.get("pic") or ""), "")
    achievement = group.get("achieve") or {}
    achievement_icon = assets.get(_medal_icon(achievement), "")
    dungeon_items = "".join(
        f"""<div class="dungeon-item {'active' if _same_indie_stage(dungeon, selected) else ''}">
<i class="dungeon-pass {'done' if dungeon.get('isPass') else 'open'}">{_ui_icon('dungeon-correct', assets)}</i><span>{_text(dungeon.get('name'))}<small></small></span><b>{_ui_icon('dungeon-active-triangle', assets)}</b><em><i></i><i></i></em></div>"""
        for dungeon in dungeons[:6]
    )
    features = _feature_lines(selected.get("feature"))
    enemies = "".join(
        f'<div class="enemy"><img src="{_attr(assets.get(str(enemy.get("imageUrl") or ""), ""))}"><span>{_text(enemy.get("name"))}</span></div>'
        for enemy in (selected.get("enemies") or [])[:4]
    )
    best_record = selected.get("bestRecord") or {}
    record_team = "".join(
        f'<img src="{_attr(assets.get(str(member.get("avatarUrl") or ""), ""))}">'
        for member in (best_record.get("chars") or [])[:4]
    )
    return _document(
        "account-ui indie-ui",
        f"""
<div class="indie-bg" style="--bg:url('{_attr(background)}')"></div>
<div class="indie-vignette"></div>
{_ui_icon('dungeon-redline', assets, 'indie-redline')}
<div class="indie-rail"></div>
<button class="close-mark indie-close" aria-label="close">{_ui_icon('close', assets)}</button>
<header class="indie-title"><span>// {_text(group.get('name') or '影拓丰碑')}</span><small>MONUMENT ARCHIVE</small></header>
<section class="dungeon-list">{dungeon_items}</section>
<section class="indie-detail">
  <div class="detail-title">{_ui_icon('dungeon-title', assets, 'dungeon-title-mark')}<i></i><h1>{_text(selected.get('name') or '暂无关卡')}</h1><span class="detail-dots">{_ui_icon('dungeon-more', assets)}</span></div>
  <div class="recommend">/ 推荐干员等级 <b>LV.{_text(selected.get('recommendLevel') or '--')}</b></div>
  <p class="dungeon-desc">{_text(selected.get('desc') or '暂无关卡描述')}</p>
  <div class="feature-panel panel"><h2>/ 机制特性 <i></i></h2>{features}</div>
  <div class="enemy-panel"><header><b></b>敌方情报 <i></i>{_ui_icon('dungeon-more', assets)}</header><div>{enemies or '<span class="empty">暂无敌方资料</span>'}</div></div>
  <div class="record-panel"><header><b></b>最佳记录 <i></i>{_ui_icon('dungeon-more', assets)}</header><div><strong>{_format_duration(best_record.get('passTs'))}</strong><span>{record_team}</span><em>{'已通过' if selected.get('isPass') else '未通过'}</em></div></div>
  <div class="challenge-bar">{_ui_icon('dungeon-hard-bg', assets, 'hard-bar-texture')}<div class="challenge-mode">{_hud_icon('challenge-mode')}<span>苦难模式</span></div><div class="mode-toggle"><b>开</b><em>关</em></div><button><i></i><span>挑战影拓丰碑</span>{_hud_icon('retry')}</button></div>
</section>
<div class="indie-achievement"><img src="{_attr(achievement_icon)}"><span>{_text((achievement.get('achievementData') or {}).get('name') or '活动奖章')}</span><b>{'已镀层' if achievement.get('isPlated') else '已获得'}</b></div>
<div class="indie-network">{_hud_icon('network')}</div>
""",
        _indie_css(),
        assets,
    )


def _crisis_html(
    payload: AccountUiPayload,
    crisis: JsonObject,
    team: list[JsonObject],
    indicators: list[JsonObject],
    assets: Mapping[str, str],
) -> str:
    status = crisis.get("status") or {}
    history = crisis.get("history") or {}
    best = history.get("bestRecord") or {}
    background = assets.get(str(status.get("kvImage") or ""), "")
    header_art = assets.get(str(status.get("headerImage") or ""), "")
    indicator_grid = "".join(_indicator_tile(indicator, assets) for indicator in indicators)
    operator_cards = "".join(_crisis_character_card(character, assets) for character in team[:4])
    achievement = status.get("achieve") or {}
    achievement_icon = assets.get(_medal_icon(achievement), "")
    return _document(
        "account-ui crisis-ui",
        f"""
<div class="crisis-bg" style="--bg:url('{_attr(background)}')"></div>
<div class="crisis-overlay"></div>
<img class="crisis-header-art" src="{_attr(header_art)}" alt="">
<section class="result-column">
  <div class="success-mark"><small>OPERATION</small><h1>{_ui_icon('contract-entry', assets)}<span>行动成功</span></h1><i></i><em></em></div>
  <div class="tier-row">{''.join(f'<i>{_ui_icon("contract-wave-bg", assets, "wave-bg")}{_ui_icon("contract-tier", assets, "wave-mark")}<b>{label}</b></i>' for label in ("I", "II", "III", "IV"))}</div>
  <div class="result-stats">
    <div class="score-stat"><span>指标总计</span>{_ui_icon('contract-total', assets, 'stat-icon')}{_ui_icon('contract-total', assets, 'stat-watermark')}<small>新纪录</small><strong>{_text(best.get('indicatorCount') or status.get('highest') or 0)}</strong></div>
    <div class="time-stat"><span>行动时长</span>{_hud_icon('clock', 'stat-icon')}{_hud_icon('clock', 'stat-watermark')}<strong>{_format_duration(best.get('passTs'))}</strong></div>
  </div>
  <div class="indicator-grid">{indicator_grid}</div>
  <div class="result-note">{_ui_icon('info', assets)}<span>{_text((achievement.get('achievementData') or {}).get('name') or '行动记录已保存')}</span>{_score_badge(status.get('highest') or 0, assets)}</div>
</section>
<section class="squad-column">
  <div class="squad-grid">{operator_cards}</div>
</section>
<div class="crisis-medal"><img src="{_attr(achievement_icon)}"><span>{'PLATED' if achievement.get('isPlated') else 'MEDAL'}</span></div>
<div class="crisis-bottom-line"></div>
""",
        _crisis_css(),
        assets,
    )


def _profile_character_card(character: JsonObject, assets: Mapping[str, str]) -> str:
    data = character.get("charData") or {}
    portrait = assets.get(_profile_character_portrait(character), "")
    profession_id = PROFESSION_ICON_IDS.get(_semantic_key(data.get("profession")))
    profession_icon = _ui_icon(f"profile-profession-{profession_id}", assets, "profession-icon") if profession_id is not None else ""
    property_key = _semantic_key(data.get("property"))
    property_name_key = property_key.removeprefix("char_property_")
    property_icon = _ui_icon(f"profile-property-{property_name_key}", assets, "property-icon")
    potential_level = _potential_level(character)
    potential_asset = "profile-potential-0" if potential_level == 0 else f"potential-{potential_level}"
    potential_icon = _ui_icon(potential_asset, assets, "profile-potential-icon")
    property_name = semantic_label(data.get("property"), default=localized_text(data.get("property"), default="--"))
    return f"""<article class="operator-card">
<img src="{_attr(portrait)}"><div class="operator-badges">{profession_icon}</div><span class="operator-potential" title="干员潜能 {_text(potential_level)}">{potential_icon}</span><div class="property-chip property-{_attr(property_name_key)}">{property_icon}<span>{_text(property_name)}</span></div><div class="operator-meta"><span>LV.</span><b>{_text(character.get('level'))}</b></div>
<strong>{_text(data.get('name'))}</strong></article>"""


def _crisis_character_card(character: JsonObject, assets: Mapping[str, str]) -> str:
    portrait = assets.get(_character_portrait(character), "")
    weapon = character.get("weapon") or {}
    weapon_icon = assets.get(_weapon_icon(character), "")
    equips = _character_equips(character)
    equip_tiles = "".join(
        f'<span class="equip-slot">{_hud_icon("slot")}<img src="{_attr(assets.get(_equip_icon(equip), ""))}"></span>'
        for equip in equips[:4]
    )
    weapon_skill_pips = "".join(
        f'<i title="{_attr(name)}">{_hud_icon("capsule")}<b>{_text(level)}</b></i>'
        for name, level in _weapon_skills(character)
    )
    potential_level = _potential_level(character)
    potential_mark = f'<span class="potential-mark" title="干员潜能 {_text(potential_level)}">{_ui_icon(f"potential-{potential_level}", assets)}</span>'
    level = character.get("level") or "--"
    return f"""<article class="crisis-operator operator-card">
<div class="portrait"><img src="{_attr(portrait)}">{potential_mark}<span class="level-label">LV</span><b>{_text(level)}</b></div>
<div class="weapon"><img src="{_attr(weapon_icon)}"><span>LV {_text(weapon.get('level') or '--')}</span><div class="weapon-skill-pips">{weapon_skill_pips}</div><em>{_hud_icon('rarity')}</em></div>
<div class="equips">{equip_tiles}</div>
<div class="operator-watermark">{_ui_icon('contract-role-watermark', assets)}</div></article>"""


def _profile_medal(medal: JsonObject, assets: Mapping[str, str]) -> str:
    icon = assets.get(_medal_icon(medal), "")
    name = (medal.get("achievementData") or {}).get("name") or "奖章"
    return f'<div class="medal"><img src="{_attr(icon)}"><span>{_text(name)}</span></div>'


def _indicator_tile(indicator: JsonObject, assets: Mapping[str, str]) -> str:
    icon = assets.get(str(indicator.get("icon") or ""), "")
    state = "unlocked" if indicator.get("isUnlock") else "locked"
    return f'<div class="indicator {state}"><img src="{_attr(icon)}"><span>{_text(indicator.get("score") or "")}</span></div>'


def _stat_card(
    value: Any,
    label: str,
    texture_name: str,
    assets: Mapping[str, str],
) -> str:
    return f'<div class="stat-card">{_ui_icon(texture_name, assets, "stat-texture")}<strong>{_text(value)}</strong><span>· {label}</span></div>'


def _feature_lines(value: Any) -> str:
    text = _plain_rich_text(value)
    lines = [line.strip(" -") for line in text.splitlines() if line.strip(" -")]
    return "".join(f"<p>— {_text(line)}</p>" for line in lines[:6]) or "<p>— 暂无机制说明</p>"


def _profile_background(payload: AccountUiPayload) -> str:
    return UI_ASSETS["profile-background"][0]


def _potential_level(character: JsonObject) -> int:
    try:
        return max(0, min(5, int(character.get("potentialLevel") or 0)))
    except (TypeError, ValueError):
        return 0


def _weapon_skills(character: JsonObject) -> tuple[tuple[str, Any], ...]:
    weapon = character.get("weapon") or {}
    terms = weapon.get("weaponTerms")
    if isinstance(terms, list):
        return tuple((f"武器词条 {index}", level) for index, level in enumerate(terms[:3], 1))
    weapon_data = weapon.get("weaponData") or {}
    try:
        weapon_level = max(1, int(weapon.get("level") or 1))
        skill_level: Any = max(1, min(9, (weapon_level + 9) // 10))
    except (TypeError, ValueError):
        skill_level = "--"
    output: list[tuple[str, Any]] = []
    for skill in weapon_data.get("skills") or []:
        if not isinstance(skill, Mapping):
            continue
        output.append((localized_text(skill.get("value"), default="武器技能"), skill_level))
        if len(output) == 3:
            break
    return tuple(output)


def _character_portrait(character: JsonObject) -> str:
    data = character.get("charData") or {}
    return str(character.get("recordAvatarUrl") or data.get("avatarRtUrl") or data.get("avatarSqUrl") or character.get("avatarUrl") or "")


def _profile_character_portrait(character: JsonObject) -> str:
    data = character.get("charData") or {}
    return str(data.get("avatarSqUrl") or data.get("avatarRtUrl") or character.get("avatarUrl") or "")


def _character_semantic_icon_urls(character: JsonObject) -> tuple[str, ...]:
    data = character.get("charData") or {}
    return tuple(
        url
        for url in (
            _profession_icon_url(_semantic_key(data.get("profession"))),
            _property_icon_url(_semantic_key(data.get("property"))),
        )
        if url
    )


def _semantic_key(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("key") or "")
    return str(value or "")


def _profession_icon_url(key: str) -> str:
    profession_id = PROFESSION_ICON_IDS.get(key)
    if profession_id is None:
        return ""
    return f"{WARFARIN_STATIC_BASE}/charprofessionicon/icon_profession_{profession_id}_s.webp"


def _property_icon_url(key: str) -> str:
    icon_name = PROPERTY_ICON_NAMES.get(key)
    if not icon_name:
        return ""
    return f"{WARFARIN_STATIC_BASE}/elementicon/{icon_name}.webp"


def _icon_img(src: str, class_name: str) -> str:
    if not src:
        return ""
    return f'<img class="{class_name}" src="{_attr(src)}">'


def _hud_icon(name: str, class_name: str = "") -> str:
    safe_name = re.sub(r"[^a-z0-9-]", "", str(name).lower())
    classes = "hud-icon"
    if class_name:
        classes = f"{classes} {_attr(class_name)}"
    return (
        f'<svg class="{classes}" aria-hidden="true" focusable="false">'
        f'<use href="#hud-{safe_name}"></use></svg>'
    )


def _ui_icon(
    name: str,
    assets: Mapping[str, str],
    class_name: str = "",
) -> str:
    asset = UI_ASSETS.get(name)
    if asset is None:
        return ""
    virtual_url, _path = asset
    classes = "ui-icon"
    if class_name:
        classes = f"{classes} {_attr(class_name)}"
    return f'<img class="{classes}" src="{_attr(assets.get(virtual_url, virtual_url))}">'


def _score_badge(
    value: Any,
    assets: Mapping[str, str],
    class_name: str = "",
) -> str:
    classes = "score-badge"
    if class_name:
        classes = f"{classes} {_attr(class_name)}"
    return f'<span class="{classes}"><b>{_text(value)}</b>{_ui_icon("contract-score", assets)}</span>'


def _character_equips(character: JsonObject) -> list[JsonObject]:
    output = []
    record_equips = character.get("equips") or {}
    for key in ("bodyEquip", "armEquip", "firstAccessory", "secondAccessory"):
        value = record_equips.get(key) if isinstance(record_equips, Mapping) else None
        if not isinstance(value, dict):
            value = character.get(key)
        if isinstance(value, dict) and (value.get("equipData") or value.get("icon")):
            output.append(value)
    return output


def _weapon_icon(character: Mapping[str, Any]) -> str:
    weapon = character.get("weapon") or {}
    weapon_data = weapon.get("weaponData") or {}
    return str(weapon.get("icon") or weapon_data.get("iconUrl") or "")


def _equip_icon(equip: Mapping[str, Any]) -> str:
    equip_data = equip.get("equipData") or {}
    return str(equip.get("icon") or equip_data.get("iconUrl") or "")


def _crisis_team(payload: AccountUiPayload, best: Mapping[str, Any]) -> list[JsonObject]:
    record = payload.crisis_record or {}
    members = record.get("chars") if record.get("id") == best.get("id") else None
    if not isinstance(members, list):
        members = best.get("chars") or []
    character_map = payload.character_map()
    output: list[JsonObject] = []
    for member in members:
        if not isinstance(member, Mapping):
            continue
        current = character_map.get(str(member.get("charId")), {})
        merged = dict(current)
        merged.update(member)
        merged["recordAvatarUrl"] = member.get("avatarUrl")
        output.append(merged)
    return output


def _medal_icon(medal: Mapping[str, Any]) -> str:
    data = medal.get("achievementData") or {}
    if medal.get("isPlated") and data.get("platedIcon"):
        return str(data["platedIcon"])
    level = int(medal.get("level") or 1)
    if level >= 3 and data.get("reforge3Icon"):
        return str(data["reforge3Icon"])
    if level >= 2 and data.get("reforge2Icon"):
        return str(data["reforge2Icon"])
    return str(data.get("initIcon") or "")


def _same_indie_stage(menu_dungeon: Mapping[str, Any], selected: Mapping[str, Any]) -> bool:
    menu_name = str(menu_dungeon.get("name") or "")
    selected_name = str(selected.get("name") or "")
    return bool(menu_name and (selected_name == menu_name or selected_name.startswith(f"{menu_name}·")))


def _format_date(value: Any) -> str:
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(value)).strftime("%Y/%m/%d")
    except (TypeError, ValueError, OSError):
        return "----/--/--"


def _format_duration(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return "--:--"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _plain_rich_text(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", localized_text(value)).replace("\\n", "\n")


def _text(value: Any) -> str:
    return html.escape(localized_text(value, default="--"))


def _attr(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _write_temp_html(content: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".html",
        delete=False,
    )
    with handle:
        handle.write(content)
    return Path(handle.name)


def _document(class_name: str, body: str, css: str, assets: Mapping[str, str]) -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{_base_css(assets)}
{css}
</style></head><body>{HUD_SVG_DEFS}<div class="{class_name}">{body}</div></body></html>"""


def _base_css(assets: Mapping[str, str]) -> str:
    font_faces = "\n".join(
        f"""@font-face {{ font-family: "{'EndfieldHUD' if name.startswith('hud') else 'EndfieldCN'}"; src: url("{_attr(assets.get(url, url))}") format("truetype"); font-weight: {weight}; font-style: normal; font-display: block; }}"""
        for name, (url, _path, weight) in FONT_ASSETS.items()
    )
    return f"""
{font_faces}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; width: {CANVAS_WIDTH}px; height: {CANVAS_HEIGHT}px; overflow: hidden; }}
body {{ font-family: "EndfieldCN", "HarmonyOS Sans SC", "Microsoft YaHei UI", sans-serif; background: #101313; font-synthesis:none; }}
.account-ui {{ position: relative; width: {CANVAS_WIDTH}px; height: {CANVAS_HEIGHT}px; overflow: hidden; color: #f5f7f5; isolation: isolate; }}
.profile-ui {{ width: {PROFILE_CANVAS_WIDTH}px; height: {PROFILE_CANVAS_HEIGHT}px; }}
.account-ui {{ --contract-again:url("{_attr(assets.get(UI_ASSETS['contract-again'][0], UI_ASSETS['contract-again'][0]))}"); --contract-weapon-bg:url("{_attr(assets.get(UI_ASSETS['contract-weapon-bg'][0], UI_ASSETS['contract-weapon-bg'][0]))}"); --contract-role-cell:url("{_attr(assets.get(UI_ASSETS['contract-role-cell'][0], UI_ASSETS['contract-role-cell'][0]))}"); --contract-equip-mask:url("{_attr(assets.get(UI_ASSETS['contract-equip-mask'][0], UI_ASSETS['contract-equip-mask'][0]))}"); --contract-logo:url("{_attr(assets.get(UI_ASSETS['contract-logo'][0], UI_ASSETS['contract-logo'][0]))}"); --contract-stripes:url("{_attr(assets.get(UI_ASSETS['contract-stripes'][0], UI_ASSETS['contract-stripes'][0]))}"); --contract-title-deco:url("{_attr(assets.get(UI_ASSETS['contract-title-deco'][0], UI_ASSETS['contract-title-deco'][0]))}"); --contract-success-line:url("{_attr(assets.get(UI_ASSETS['contract-success-line'][0], UI_ASSETS['contract-success-line'][0]))}"); }}
.account-ui::after {{ content: ""; position: absolute; inset: 0; z-index: 20; pointer-events: none; opacity: .12; background-image: repeating-linear-gradient(115deg, transparent 0 3px, rgba(255,255,255,.1) 3px 4px); mix-blend-mode: overlay; }}
.panel {{ border: 1px solid rgba(255,255,255,.18); box-shadow: 0 18px 35px rgba(0,0,0,.26); }}
img {{ display: block; }}
.hud-defs {{ position:absolute; width:0; height:0; overflow:hidden; pointer-events:none; }}
.hud-icon {{ display:inline-block; width:1em; height:1em; flex:0 0 auto; overflow:visible; color:inherit; fill:currentColor; vertical-align:middle; }}
.ui-icon {{ display:block; width:1em; height:1em; flex:0 0 auto; object-fit:contain; }}
.score-badge {{ --icon-cut:#320405; display:inline-flex; height:31px; align-items:center; gap:3px; padding:0 5px 0 9px; border:2px solid #d7514d; color:#fff; background:linear-gradient(90deg,rgba(67,4,5,.95),rgba(111,9,9,.8)); box-shadow:inset 0 0 0 1px rgba(255,255,255,.1); font-family:"EndfieldHUD","Arial Narrow",sans-serif; }}
.score-badge b {{ font-size:24px; font-weight:400; line-height:1; }}
.score-badge .ui-icon {{ width:28px; height:24px; object-fit:contain; }}
.topline b, small, .sync-line b, .hud-footer, .medal-panel header span, .operators-panel header span, .crisis-footer, .crisis-medal span, .success-mark small, .result-note em {{ font-family:"EndfieldHUD","Arial Narrow",sans-serif; }}
.name-line em, .level-stack strong, .stat-card strong, .domain-card b, .operator-meta b, .result-stats strong, .crisis-operator .portrait b, .weapon em {{ font-family:"EndfieldHUD","Arial Narrow",sans-serif; letter-spacing:0; }}
.operator-badges {{ position:absolute; z-index:5; top:0; left:0; display:grid; place-items:center; width:26px; height:26px; padding:1px; background:rgba(35,37,39,.94); border:1px solid rgba(255,255,255,.86); }}
.operator-badges .ui-icon {{ width:23px; height:23px; object-fit:contain; filter:drop-shadow(0 1px 2px rgba(0,0,0,.65)); }}
.close-mark {{ --icon-cut:#fff; position:absolute; z-index:30; top:21px; right:51px; display:grid; place-items:center; width:43px; height:43px; padding:0; border:0; border-radius:50%; color:#4d4d4d; background:rgba(255,255,255,.94); box-shadow:0 2px 6px rgba(0,0,0,.25); }}
.close-mark .ui-icon {{ width:43px; height:43px; }}
"""


def _profile_css() -> str:
    return """
.profile-stage { position:absolute; left:-220px; top:-82px; width:1920px; height:1080px; padding-top:82px; box-sizing:border-box; overflow:hidden; }
.profile-bg { position:absolute; inset:0; z-index:-5; overflow:hidden; background-image:var(--bg); background-size:cover; background-repeat:no-repeat; background-position:center; }
.profile-bg::before,.profile-bg::after { content:none; }
.profile-shade { position:absolute; inset:0; z-index:-4; background:linear-gradient(90deg,rgba(23,29,27,.96) 0 13%,rgba(23,29,27,.48) 13% 40%,transparent 63%,rgba(19,36,28,.45)); }
.profile-landmarks { display:none; }
.profile-landmarks i { position:absolute; display:block; background:linear-gradient(180deg,rgba(32,55,50,.58),rgba(19,38,31,.18)); filter:blur(.2px); clip-path:polygon(16% 0,82% 0,100% 18%,84% 100%,12% 100%,0 16%); }
.profile-landmarks i:nth-child(1) { left:372px; top:82px; width:82px; height:205px; }
.profile-landmarks i:nth-child(2) { right:348px; top:74px; width:102px; height:260px; }
.profile-landmarks i:nth-child(3) { left:1010px; top:420px; width:88px; height:170px; border-radius:45% 45% 20% 20%; opacity:.55; }
.profile-landmarks b { position:absolute; left:250px; right:0; bottom:0; height:180px; background:radial-gradient(ellipse at 56% 100%,rgba(90,123,70,.72),transparent 36%),linear-gradient(0deg,rgba(34,64,38,.68),transparent 72%); }
.profile-landmarks::before { content:""; position:absolute; left:520px; right:310px; bottom:240px; height:190px; background:linear-gradient(130deg,transparent 0 44%,rgba(31,250,177,.68) 45%,rgba(31,250,177,.12) 49%,transparent 54%),linear-gradient(52deg,transparent 0 38%,rgba(31,250,177,.5) 39%,transparent 45%); filter:blur(1px); }
.profile-rail { position:absolute; z-index:-3; left:0; top:82px; bottom:0; width:144px; background:linear-gradient(90deg,rgba(21,25,24,.92),rgba(21,25,24,.58)); border-right:1px solid rgba(255,255,255,.05); }
.profile-rail::after { content:""; position:absolute; inset:0; opacity:.08; background:repeating-linear-gradient(90deg,#fff 0 1px,transparent 1px 46px),repeating-linear-gradient(0deg,#fff 0 1px,transparent 1px 56px); }
.topline { height:82px; display:flex; align-items:center; padding:0 52px; gap:34px; font-size:22px; font-weight:500; background:linear-gradient(90deg,rgba(35,43,40,.92),rgba(35,43,40,.46)); border-bottom:1px solid rgba(255,255,255,.15); }
.topline i { flex:1; height:16px; opacity:.25; background:repeating-linear-gradient(-45deg,#fff 0 2px,transparent 2px 5px); }
.topline b { letter-spacing:.24em; font-size:14px; }
.profile-nav { position:absolute; z-index:8; top:0; left:50%; transform:translateX(-50%); height:82px; display:flex; align-items:center; gap:18px; }
.nav-search { display:grid; place-items:center; width:27px; height:27px; margin-right:-9px; color:#fff; background:rgba(35,39,38,.78); box-shadow:0 0 0 1px rgba(255,255,255,.18); }
.nav-search .hud-icon { width:18px; height:18px; }
.profile-nav button { position:relative; display:grid; place-items:center; width:64px; height:82px; padding:0; border:0; color:#fff; background:transparent; }
.profile-nav button.active { width:98px; background:#ffe800; box-shadow:0 3px 10px rgba(0,0,0,.35); }
.profile-nav button .ui-icon { width:31px; height:31px; object-fit:contain; }.profile-nav button.active .ui-icon { width:34px; height:34px; filter:brightness(0); }
.profile-nav kbd { display:grid; place-items:center; width:24px; height:24px; border:0; color:#fff; background:rgba(40,43,43,.78); box-shadow:0 0 0 1px rgba(255,255,255,.22); font:13px/1 "EndfieldHUD"; }
.profile-layout { display:grid; grid-template-columns:750px 1fr; gap:65px; width:1450px; margin:140px auto 0; }
.identity-column { position:relative; padding:14px 0 0 34px; }
.identity-column::before { content:""; position:absolute; left:0; top:0; bottom:0; width:7px; background:repeating-linear-gradient(#50f28e 0 9px,transparent 9px 42px); }
.identity-row { display:flex; align-items:center; gap:14px; }
.avatar-wrap { position:relative; }
.avatar-frame { position:relative; width:168px; height:168px; padding:8px; border:3px solid #ff7b00; background:#1b1d1d; clip-path:polygon(0 0,100% 0,100% 92%,92% 100%,0 100%); box-shadow:-12px -12px 0 -9px rgba(197,202,198,.7); }
.avatar-image { width:100%; height:100%; object-fit:cover; }
.avatar-stripes { position:absolute; z-index:1; left:8px; top:8px; width:148px; height:102px; opacity:.18; object-fit:cover; }
.avatar-code { position:absolute; z-index:2; right:8px; top:15px; width:92px; height:36px; opacity:.22; object-fit:contain; }
.avatar-corner { position:absolute; z-index:3; left:-1px; top:-1px; width:48px; height:48px; }
.avatar-frame span { position:absolute; bottom:9px; left:10px; padding:2px 8px; background:#ff6500; font-size:10px; letter-spacing:.18em; }
.avatar-frame::before { content:""; position:absolute; z-index:2; left:-10px; top:25px; width:7px; height:46px; background:repeating-linear-gradient(135deg,#ff7200 0 4px,transparent 4px 8px); }
.avatar-frame::after { content:""; position:absolute; z-index:2; left:-20px; top:-3px; width:14px; height:7px; background:#53f492; box-shadow:174px 151px 0 #ff4b36; }
.avatar-corners { position:absolute; inset:-9px; border:1px solid transparent; pointer-events:none; background:linear-gradient(#aeb2b0,#aeb2b0) left top/27px 3px no-repeat,linear-gradient(#aeb2b0,#aeb2b0) left top/3px 27px no-repeat,linear-gradient(#ff7b00,#ff7b00) right bottom/27px 3px no-repeat,linear-gradient(#ff7b00,#ff7b00) right bottom/3px 27px no-repeat; }
.avatar-more { position:absolute; left:9px; top:178px; display:grid; place-items:center; width:38px; height:38px; padding:0; border:2px solid #707373; border-radius:50%; background:#f6f6f4; color:#4c4c4c; box-shadow:0 2px 5px rgba(0,0,0,.35); }.avatar-more .ui-icon { width:23px; height:10px; filter:brightness(.28); }
.identity-copy { flex:1; }
.name-line { display:flex; align-items:center; gap:14px; }
.name-line strong { font-size:28px; font-weight:500; }
.name-line .score-badge { height:29px; }.name-line .score-badge b { font-size:23px; }
.identity-code { display:flex; align-items:center; gap:3px; width:70px; height:14px; margin:7px 0 35px; color:#a4aaa8; }.identity-code i { display:block; width:16px; height:2px; background:currentColor; transform:skew(-30deg); opacity:.45; }.identity-code i:nth-child(2) { width:7px; }.identity-code i:nth-child(3) { width:28px; opacity:.25; }
.sync-line { display:flex; align-items:center; width:max-content; background:rgba(12,15,14,.72); }
.sync-line b { position:absolute; margin-top:-20px; padding:0; color:#a9aeab; font-size:7px; letter-spacing:.05em; }
.sync-line .hud-icon { width:25px; height:18px; margin-left:7px; color:#eeff00; }
.sync-line strong { padding:4px 7px; color:#f4f4f1; font-size:15px; }
.sync-line span { padding:5px 12px; color:#222; background:#f5f6f4; font-weight:700; }
.public-id { display:flex; align-items:center; gap:5px; margin-top:6px; color:#d0d5d2; opacity:.72; font:18px/1 "EndfieldHUD"; }
.public-id span { display:none; }.public-id b { font-weight:400; }.public-id .ui-icon { width:21px; height:21px; }
.level-stack { width:278px; margin:42px 0 22px 7px; }
.level-stack div { display:grid; grid-template-columns:35px 1fr auto; align-items:center; height:34px; margin:5px 0; padding:0 12px 0 7px; background:linear-gradient(90deg,rgba(14,16,16,.92),rgba(14,16,16,.45)); font-size:22px; font-weight:500; }
.level-stack strong { font-size:31px; font-weight:400; }
.rank-icon { width:28px; height:26px; opacity:.96; }
.mission-card { position:relative; display:flex; width:450px; height:94px; margin-left:8px; padding:14px 20px; gap:18px; align-items:center; overflow:hidden; background:linear-gradient(110deg,rgba(21,24,23,.93),rgba(21,24,23,.36)); border-top:5px solid #4cf39a; }
.mission-texture { position:absolute; z-index:0; right:0; top:-12px; width:220px; height:116px; object-fit:cover; opacity:.34; }
.mission-card>span,.mission-card>div,.mission-card>em { position:relative; z-index:1; }
.mission-emblem { display:grid; place-items:center; width:62px; height:62px; }.mission-emblem .ui-icon { width:66px; height:66px; object-fit:contain; }
.mission-card b,.mission-card span { display:block; }
.mission-card b { font-size:24px; font-weight:500; }.mission-card span { color:#b5bab7; }
.mission-card em { position:absolute; margin:65px 0 0 4px; font:7px/1 "EndfieldHUD"; letter-spacing:.14em; opacity:.55; }
.stat-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; width:450px; margin:26px 0 0 8px; }
.stat-card { position:relative; overflow:hidden; padding:10px 13px 8px; background:rgba(20,25,23,.72); border-bottom:2px solid rgba(79,245,146,.55); }
.stat-texture { position:absolute; z-index:0; right:-20px; top:-7px; width:125px; height:88px; object-fit:cover; opacity:.34; filter:grayscale(1); }
.stat-card strong,.stat-card span { position:relative; z-index:1; }
.stat-card strong { display:block; color:#55f09a; font-size:42px; font-weight:400; line-height:1; }.stat-card span { display:block; margin-top:8px; color:#55f09a; font-size:19px; line-height:1; }
.domain-title { display:flex; align-items:center; gap:14px; width:450px; margin:22px 0 0 8px; color:#55f09a; font-size:23px; }.domain-title i { flex:1; height:1px; background:#55f09a; }
.domain-grid { display:grid; grid-template-columns:1fr 1fr; width:450px; gap:8px; margin:8px 0 0 8px; }
.domain-card { display:grid; grid-template-columns:1fr auto; padding:12px 16px; color:#404442; background:rgba(241,241,225,.82); }.domain-card b { font-size:38px; }.domain-card strong { grid-column:1/3; text-align:right; font-size:18px; }
.showcase-column { padding-top:15px; transform:translateX(15px); }
.medal-panel { position:relative; width:612px; height:175px; padding:18px 22px; overflow:hidden; color:#1b1e1d; background:#e6e7e6; border-radius:18px; }
.medal-panel-deco { position:absolute; z-index:0; left:55px; top:4px; width:192px; height:168px; object-fit:contain; opacity:1; }
.medal-entry-bg { position:absolute; z-index:0; left:148px; top:-3px; width:411px; height:164px; object-fit:fill; opacity:1; }
.medal-side-bg { position:absolute; z-index:0; right:1px; top:1px; width:132px; height:172px; object-fit:fill; opacity:1; }
.panel-open { position:absolute; right:18px; top:18px; width:29px; height:29px; color:rgba(255,255,255,.8); opacity:.8; }
.medal-device { position:absolute; z-index:2; left:23px; top:17px; width:44px; height:40px; opacity:.72; }
.medal-detail { position:absolute; z-index:2; left:57px; top:17px; width:100px; height:24px; opacity:.58; }
.medal-label { position:absolute; z-index:2; left:23px; top:62px; width:20px; height:16px; opacity:.52; }
.medal-caption { position:absolute; z-index:2; left:23px; top:124px; width:44px; height:12px; opacity:.52; }
.medal-panel header,.medal-grid,.medal-micro { position:relative; z-index:1; }.medal-panel header { z-index:3; transform:translateY(75px); }.operators-panel header span { display:block; font-size:10px; letter-spacing:.16em; }.medal-panel header b { font-size:26px; }
.medal-grid { position:absolute; left:139px; top:-3px; width:424px; height:162px; }
.medal { position:absolute; width:94px; height:94px; text-align:center; }
.medal:nth-child(1) { left:0; top:0; }.medal:nth-child(2) { left:37px; top:68px; }
.medal:nth-child(3) { left:74px; top:0; }.medal:nth-child(4) { left:111px; top:68px; }
.medal:nth-child(5) { left:148px; top:0; }.medal:nth-child(6) { left:185px; top:68px; }
.medal:nth-child(7) { left:222px; top:0; }.medal:nth-child(8) { left:259px; top:68px; }
.medal:nth-child(9) { left:296px; top:0; }.medal:nth-child(10) { left:333px; top:68px; }
.medal img { width:94px; height:94px; object-fit:contain; filter:drop-shadow(0 3px 2px rgba(0,0,0,.25)); }.medal span { display:none; }
.medal-micro { position:absolute; z-index:2; left:23px; bottom:17px; opacity:.42; }.medal-micro .ui-icon { width:84px; height:28px; }
.operators-panel { position:relative; width:612px; height:252px; margin-top:15px; padding:17px 22px; overflow:hidden; background:rgba(18,22,21,.9); border-radius:15px; }
.role-panel-bg { position:absolute; z-index:0; left:1px; top:1px; width:calc(100% - 2px); height:calc(100% - 2px); object-fit:cover; opacity:.88; }
.operator-ghost { position:absolute; z-index:1; left:23px; bottom:0; width:84px; height:95px; opacity:.35; }
.operators-panel header,.operator-row,.operator-scroll { position:relative; z-index:2; }.operators-panel header { z-index:3; padding-bottom:8px; border-bottom:1px solid rgba(255,255,255,.38); }.operators-panel header span { display:flex; align-items:center; gap:3px; width:max-content; height:14px; }.operators-panel header span::before { content:"[·"; color:rgba(255,255,255,.92); font:700 10px/1 "EndfieldHUD"; }.operators-panel header span .ui-icon { width:160px; height:14px; object-fit:fill; }.operators-panel header b { position:relative; display:block; width:max-content; margin-top:7px; font-size:23px; transform:translateY(66px); }
.operators-panel header b i { position:absolute; top:-11px; width:12px; height:6px; background:#fff; transform:skew(-28deg); }.operators-panel header b i:nth-child(1) { left:0; }.operators-panel header b i:nth-child(2) { left:15px; }.operators-panel header b i:nth-child(3) { left:30px; }
.operator-row { display:flex; justify-content:flex-start; gap:7px; margin:13px 0 0 130px; }
.operator-card { position:relative; overflow:hidden; }
.operators-panel .operator-card { width:97px; height:140px; background:#d8d9d5; border:3px solid #f4f4ee; border-bottom:5px solid #ff6a00; border-radius:0 9px 0 0; }
.operators-panel .operator-card>img { width:100%; height:115px; object-fit:cover; object-position:center; }
.operator-potential { position:absolute; z-index:4; left:0; bottom:23px; display:grid; place-items:center; width:24px; height:24px; filter:drop-shadow(0 1px 2px rgba(0,0,0,.45)); }.operator-potential .ui-icon { width:24px; height:24px; object-fit:contain; }
.property-chip { --icon-cut:#fff; position:absolute; z-index:6; left:1px; bottom:5px; display:grid; place-items:center; width:20px; height:20px; overflow:hidden; color:#fff; background:#73777a; }.property-chip::after { content:""; position:absolute; left:25px; bottom:4px; width:27px; height:3px; opacity:.4; background:repeating-linear-gradient(90deg,#7a7d7c 0 2px,transparent 2px 4px); }.property-chip img { width:18px; height:18px; max-width:none; object-fit:contain; object-position:center; }.property-chip span { display:none; }
.property-chip.property-cryst { background:#2bcbd8; }.property-chip.property-fire { background:#ec654d; }.property-chip.property-natural { background:#77c92f; }.property-chip.property-physical { background:#969a99; }.property-chip.property-pulse { background:#a46bd0; }
.operator-meta { position:absolute; z-index:5; left:0; right:0; bottom:5px; display:flex; justify-content:flex-end; align-items:flex-end; height:12px; padding:0 5px 0 24px; color:#222; background:#f4f4ee; }.operator-meta span { margin:0 2px 1px 0; font:6px/1 "EndfieldHUD"; }.operator-meta b { position:relative; top:2px; font-size:17px; font-weight:400; line-height:.95; }
.operators-panel .operator-card>strong { display:none; }
.operator-scroll { position:absolute; right:18px; bottom:18px; width:0; height:0; border-left:8px solid transparent; border-right:0 solid transparent; border-bottom:10px solid rgba(255,255,255,.35); }
.profile-signature { display:flex; align-items:center; gap:7px; margin:20px 0 0 310px; font-size:22px; }.profile-signature span,.profile-signature em { font-size:45px; font-style:normal; }.profile-signature .ui-icon { width:22px; height:22px; filter:brightness(0) invert(1); }
.profile-actions { position:absolute; z-index:6; right:240px; bottom:77px; display:flex; align-items:center; gap:18px; color:#f3f3f0; font-size:21px; font-weight:500; }
.profile-actions i { width:2px; height:31px; background:#f3f3f0; opacity:.9; }
.theme-icon { display:grid; place-items:center; width:43px; height:43px; padding:0; border:0; border-radius:50%; color:#fff; background:rgba(42,46,45,.82); box-shadow:0 2px 8px rgba(0,0,0,.32); }.theme-icon .ui-icon { width:28px; height:28px; }
.profile-network { position:absolute; left:26px; bottom:17px; color:#fff; }.profile-network .hud-icon { width:22px; height:22px; }
.hud-footer { position:absolute; left:276px; right:238px; bottom:63px; display:flex; align-items:center; gap:80px; color:#4cf39a; font-size:9px; }.hud-footer i { flex:1; height:8px; opacity:.35; background:repeating-linear-gradient(90deg,#4cf39a 0 16px,transparent 16px 20px); }
"""


def _indie_css() -> str:
    return """
.indie-bg { position:absolute; inset:0; z-index:-5; background-image:var(--bg); background-size:cover; background-position:center; filter:saturate(.68) brightness(.75); transform:scale(1.03); }
.indie-vignette { position:absolute; inset:0; z-index:-4; background:linear-gradient(90deg,rgba(18,18,25,.86) 0 25%,rgba(35,31,46,.18) 45%,rgba(8,8,12,.86) 88%),radial-gradient(circle at 49% 52%,rgba(208,201,225,.45),transparent 34%),linear-gradient(0deg,rgba(120,0,0,.38),transparent 35%); }
.indie-redline { position:absolute; z-index:-3; right:0; bottom:0; width:928px; height:432px; object-fit:fill; opacity:.26; }
.indie-rail { position:absolute; z-index:-3; left:0; top:0; bottom:0; width:80px; background:rgba(8,8,11,.56); border-right:1px solid rgba(255,255,255,.08); }
.indie-close { top:22px; right:50px; }
.indie-title { position:absolute; top:34px; left:48px; }.indie-title span { display:block; font-size:24px; font-weight:700; }.indie-title small { color:#7c7b82; letter-spacing:.18em; }
.dungeon-list { position:absolute; top:137px; left:48px; width:395px; }
.dungeon-item { position:relative; display:flex; align-items:center; height:61px; margin-bottom:23px; padding:0 17px; gap:16px; background:rgba(12,13,15,.86); border-left:6px solid #6af54e; box-shadow:0 9px 20px rgba(0,0,0,.28); }
.dungeon-pass { position:relative; display:grid; place-items:center; flex:0 0 auto; width:34px; height:34px; color:#f6e700; background:linear-gradient(135deg,#2e3032,#45484a); box-shadow:inset 0 0 0 1px rgba(255,255,255,.1); overflow:visible; }.dungeon-pass .ui-icon { width:40px; height:40px; max-width:none; filter:brightness(0) saturate(100%) invert(90%) sepia(95%) saturate(2081%) hue-rotate(2deg) brightness(106%) contrast(105%); }.dungeon-pass.open { opacity:.55; }
.dungeon-item span { position:relative; z-index:1; font-size:23px; font-weight:500; }.dungeon-item span small { position:absolute; left:0; bottom:-7px; width:43px; height:3px; opacity:.45; background:repeating-linear-gradient(90deg,currentColor 0 7px,transparent 7px 10px); }
.dungeon-item>b { position:absolute; z-index:2; right:7px; top:-11px; display:none; width:100px; height:88px; }
.dungeon-item>b .ui-icon { width:100px; height:88px; object-fit:contain; opacity:.22; }
.dungeon-item>em { position:absolute; z-index:3; right:6px; top:5px; display:flex; gap:4px; }.dungeon-item>em i { width:5px; height:5px; border-radius:50%; background:#aaa; }
.dungeon-item.active { color:#232326; background:rgba(250,250,250,.95); }.dungeon-item.active>b { display:block; }.dungeon-item.active>b .ui-icon { opacity:.95; filter:drop-shadow(0 3px 5px rgba(0,0,0,.45)); }.dungeon-item.active>em i { background:#777; }
.indie-detail { position:absolute; top:105px; right:52px; width:703px; }
.detail-title { position:relative; display:flex; gap:10px; align-items:center; min-height:40px; }.detail-title::after { content:""; position:absolute; z-index:-1; left:0; right:0; top:14px; height:16px; background:rgba(255,255,255,.14); }.dungeon-title-mark { position:absolute; z-index:-1; left:8px; top:-46px; width:164px; height:132px; opacity:.18; }.detail-title>i { width:10px; height:36px; background:#fff; }.detail-title h1 { margin:0; font-size:33px; font-weight:500; line-height:1; }.detail-dots { position:absolute; right:5px; top:-2px; color:rgba(255,255,255,.22); }.detail-dots .ui-icon { width:52px; height:16px; opacity:.28; }
.recommend { display:inline-block; margin:3px 0 6px; padding:3px 12px 3px 0; color:#fff; background:#09090b; font-size:18px; }.recommend b { margin-left:8px; padding:2px 10px; color:#1b1b20; background:#ff5b62; }
.dungeon-desc { margin:0 0 18px; color:#c7c4ca; font-size:18px; line-height:1.5; }
.feature-panel { min-height:190px; padding:16px 20px; background:rgba(18,17,21,.76); border-radius:8px; }.feature-panel h2 { display:flex; align-items:center; gap:8px; margin:0 0 12px; font-size:22px; }.feature-panel h2 i { display:inline-block; width:24px; height:7px; border-top:2px solid rgba(255,255,255,.25); border-right:7px dotted rgba(255,255,255,.25); }.feature-panel p { margin:4px 0; color:#d0ced4; font-size:16px; }
.enemy-panel { position:absolute; top:685px; left:0; width:100%; }.enemy-panel header,.record-panel header { display:flex; height:40px; align-items:center; gap:9px; padding:0 15px; background:rgba(126,86,99,.55); border:1px solid rgba(255,255,255,.25); }.enemy-panel header>b,.record-panel header>b { width:8px; height:8px; border:2px solid rgba(255,255,255,.7); }.enemy-panel header>i,.record-panel header>i { flex:1; height:1px; background:rgba(255,255,255,.3); }.enemy-panel header .ui-icon,.record-panel header .ui-icon { width:52px; height:16px; }.enemy-panel>div { display:none; }
.enemy { display:flex; align-items:center; gap:8px; width:165px; }.enemy img { width:58px; height:58px; object-fit:contain; background:rgba(0,0,0,.3); }.enemy span { font-size:12px; }
.record-panel { position:absolute; top:735px; left:0; width:100%; }.record-panel>div { display:grid; grid-template-columns:105px 1fr 90px; align-items:center; height:82px; padding:8px 14px; background:rgba(110,35,42,.5); }.record-panel strong { font:29px/1 "EndfieldHUD"; }.record-panel span { display:flex; gap:5px; }.record-panel span img { width:58px; height:58px; object-fit:cover; border:2px solid rgba(255,255,255,.5); }.record-panel em { font-style:normal; opacity:.8; }
.challenge-bar { position:absolute; top:850px; left:0; display:grid; grid-template-columns:180px 180px 1fr; align-items:center; width:100%; height:70px; padding:0 8px 0 16px; overflow:hidden; background:rgba(89,19,25,.84); }.hard-bar-texture { position:absolute; z-index:2; left:14px; top:13px; width:44px; height:44px; object-fit:contain; filter:brightness(0) saturate(100%) invert(39%) sepia(94%) saturate(3768%) hue-rotate(350deg) brightness(103%) contrast(106%); }.challenge-bar>div,.challenge-bar>button { position:relative; z-index:1; }.challenge-mode { display:flex; align-items:center; gap:10px; padding-left:52px; }.challenge-mode>.hud-icon { display:none; }.challenge-mode span { font-size:21px; }.mode-toggle { display:grid; grid-template-columns:1fr 1fr; height:34px; color:#303035; border-radius:20px; overflow:hidden; background:#4c4c51; }.mode-toggle b,.mode-toggle em { display:grid; place-items:center; font-style:normal; }.mode-toggle b { background:#fff; border-radius:20px; }.challenge-bar button { justify-self:end; display:flex; align-items:center; gap:20px; height:58px; min-width:300px; padding:0 10px 0 30px; color:#303035; background:#fff; border:3px solid #6f2226; border-radius:32px; font:21px/1 "EndfieldCN"; }.challenge-bar button>i { width:37px; height:1px; background:#aaa; box-shadow:16px 0 0 -0.2px #aaa; }.challenge-bar button .hud-icon { width:38px; height:38px; padding:7px; color:#fff; background:#4b4d50; border-radius:50%; }
.indie-achievement { position:absolute; left:76px; bottom:72px; display:flex; align-items:center; gap:17px; }.indie-achievement img { width:94px; height:94px; object-fit:contain; }.indie-achievement span { font-size:20px; }.indie-achievement b { color:#a9a8ae; }
.indie-network { position:absolute; left:26px; bottom:17px; color:#fff; }.indie-network .hud-icon { width:22px; height:22px; }
"""


def _crisis_css() -> str:
    return """
.crisis-bg { position:absolute; inset:0; z-index:-5; background-image:var(--bg); background-size:cover; background-position:center; filter:saturate(.65) brightness(.58); transform:scale(1.04); }
.crisis-overlay { position:absolute; inset:0; z-index:-4; background:linear-gradient(90deg,rgba(153,30,22,.78) 0 31%,rgba(28,33,51,.34) 45%,rgba(14,12,17,.72)),linear-gradient(0deg,rgba(255,58,28,.25),transparent 46%),repeating-linear-gradient(90deg,transparent 0 78px,rgba(255,255,255,.035) 78px 80px); }
.crisis-header-art { position:absolute; z-index:-3; left:0; top:0; width:100%; height:285px; object-fit:cover; object-position:center; opacity:.12; mix-blend-mode:screen; }
.crisis-header { position:absolute; left:42px; top:35px; }.crisis-header span { display:block; font-size:21px; }.crisis-header b { display:block; margin-top:2px; color:rgba(255,255,255,.55); font-size:18px; letter-spacing:.12em; }
.result-column { position:absolute; left:65px; top:170px; width:880px; }
.success-mark { position:relative; }.success-mark::before { content:""; position:absolute; left:0; top:-31px; width:88px; height:36px; background:var(--contract-logo) center/contain no-repeat; opacity:.82; }.success-mark small { display:none; }.success-mark h1 { position:relative; display:flex; align-items:center; margin:0; font-size:78px; line-height:1.1; }.success-mark h1::before { content:""; position:absolute; left:-65px; top:-43px; width:48px; height:64px; background:var(--contract-stripes) center/contain no-repeat; }.success-mark h1::after { content:""; position:absolute; left:310px; top:13px; width:118px; height:42px; background:var(--contract-title-deco) left center/contain no-repeat; opacity:1; filter:saturate(1.2) brightness(1.25); }.success-mark h1 .ui-icon { display:none; }.success-mark>i { display:block; width:316px; height:12px; margin-top:18px; background:var(--contract-success-line) center/100% 100% no-repeat; }.success-mark>em { position:absolute; left:35px; bottom:-3px; width:125px; height:8px; background:repeating-linear-gradient(90deg,#fff 0 7px,transparent 7px 12px); opacity:.45; }
.result-stats { display:grid; grid-template-columns:500px 380px; height:136px; margin-top:10px; }.result-stats>div { position:relative; overflow:hidden; padding:14px 20px; background:rgba(41,48,64,.82); }.result-stats>div:first-child { background:rgba(202,42,33,.92); }.result-stats span { position:relative; z-index:2; display:block; font-size:22px; }.result-stats .stat-icon { position:absolute; z-index:2; left:18px; bottom:10px; width:72px; height:72px; }.result-stats .time-stat .stat-icon { left:20px; bottom:18px; width:42px; height:42px; }.result-stats .stat-watermark { position:absolute; z-index:0; left:auto; right:76px; top:7px; width:138px; height:124px; color:#171b26; opacity:.28; filter:brightness(.38) saturate(.35); }.result-stats .time-stat .stat-watermark { right:70px; top:1px; width:132px; height:132px; color:#161b28; opacity:.32; }.result-stats small { position:absolute; z-index:3; right:12px; top:10px; padding:3px 12px; color:#fff; background:#32343b; font-size:15px; }.result-stats strong { position:absolute; z-index:2; right:25px; bottom:2px; font-size:72px; font-weight:400; }
.indicator-grid { display:grid; grid-template-columns:repeat(7,1fr); grid-auto-rows:103px; gap:6px; margin-top:7px; padding:26px 53px; background:rgba(8,8,11,.9); }
.indicator { position:relative; display:grid; place-items:center; border:1px solid rgba(255,255,255,.14); }.indicator img { width:70px; height:70px; object-fit:contain; filter:grayscale(1) brightness(2.45); }.indicator.unlocked:nth-child(16) img { filter:none; }.indicator.locked { opacity:.35; }.indicator span { display:none; }
.result-note { --icon-cut:#2b292e; display:flex; align-items:center; height:58px; padding:0 18px; background:rgba(22,19,23,.92); border-left:7px solid #ff4b18; }.result-note>.ui-icon { width:31px; height:31px; margin-right:15px; }.result-note>.score-badge { margin-left:auto; }.result-note>.score-badge .ui-icon { margin:0; }
.tier-row { position:absolute; z-index:5; right:13px; top:31px; display:flex; gap:2px; }.tier-row>i { position:relative; display:grid; place-items:center; width:62px; height:70px; font-style:normal; }.tier-row .wave-bg { position:absolute; left:1px; top:-1px; width:60px; height:72px; object-fit:fill; filter:saturate(1.2) brightness(.82); }.tier-row .wave-mark { position:absolute; left:2px; top:6px; width:58px; height:58px; object-fit:fill; filter:brightness(1.25) saturate(1.25); }.tier-row b { position:relative; z-index:2; margin-top:-7px; color:#fff; font-family:"EndfieldHUD"; font-size:18px; font-weight:700; text-shadow:0 1px 2px #391016; }
.squad-column { position:absolute; top:64px; right:68px; width:844px; }
.squad-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:31px; margin:0; }
.crisis-operator { height:900px; color:#fff; background:#090a0d var(--contract-role-cell) top/100% 580px no-repeat; border:1px solid rgba(255,255,255,.15); }
.crisis-operator .portrait { position:relative; height:530px; overflow:hidden; }.crisis-operator .portrait>img { width:100%; height:100%; object-fit:cover; object-position:center top; }.crisis-operator .portrait::after { content:""; position:absolute; inset:auto 0 0; height:45%; background:linear-gradient(transparent,#0b0c0f); }.crisis-operator .portrait .level-label { position:absolute; z-index:2; left:14px; bottom:51px; }.crisis-operator .portrait>b { position:absolute; z-index:2; left:12px; bottom:4px; font-size:50px; }.potential-mark { position:absolute; z-index:4; right:3px; top:2px; display:grid; place-items:center; width:64px; height:64px; filter:drop-shadow(0 1px 2px rgba(0,0,0,.5)); }.potential-mark .ui-icon { width:64px; height:64px; object-fit:contain; }
.crisis-operator .weapon { position:relative; height:118px; background:#1f2430 var(--contract-weapon-bg) center/100% 100% no-repeat; overflow:hidden; }.crisis-operator .weapon>img { width:100%; height:100%; object-fit:contain; transform:scale(1.12); }.crisis-operator .weapon>span { position:absolute; left:8px; bottom:7px; }.crisis-operator .weapon>em { position:absolute; left:7px; top:6px; color:#fffde1; font-style:normal; filter:drop-shadow(0 1px 1px rgba(0,0,0,.45)); }.crisis-operator .weapon>em .hud-icon { width:35px; height:35px; }.weapon-skill-pips { position:absolute; right:7px; top:8px; display:flex; flex-direction:column; gap:3px; }.weapon-skill-pips i { display:flex; align-items:center; gap:2px; height:18px; color:#eaf700; font:17px/1 "EndfieldHUD"; font-style:normal; }.weapon-skill-pips .hud-icon { width:20px; height:11px; }.weapon-skill-pips b { font-weight:400; }
.crisis-operator .equips { display:grid; grid-template-columns:1fr 1fr; grid-template-rows:repeat(2,1fr); height:200px; gap:3px; padding:7px; }.equip-slot { --icon-cut:#162331; position:relative; display:grid; place-items:center; min-height:0; background:#162331; border-bottom:3px solid #e6bd24; overflow:hidden; }.equip-slot::after { content:""; position:absolute; inset:auto 0 0; height:12px; background:var(--contract-equip-mask) center/100% 100% no-repeat; opacity:.12; }.equip-slot>img { width:100%; height:100%; object-fit:contain; }.equip-slot>.hud-icon { position:absolute; z-index:2; left:4px; top:4px; width:22px; height:20px; color:#36a9ff; }
.operator-watermark { display:grid; place-items:center; height:48px; overflow:hidden; background:#090a0d; }.operator-watermark .ui-icon { width:48px; height:48px; object-fit:contain; opacity:.16; filter:grayscale(1) blur(.7px) brightness(1.4); }
.crisis-medal { display:none; }
.crisis-bottom-line { position:absolute; left:65px; right:25px; bottom:13px; height:8px; background:repeating-linear-gradient(90deg,#ff4b18 0 22px,transparent 22px 26px); }
"""

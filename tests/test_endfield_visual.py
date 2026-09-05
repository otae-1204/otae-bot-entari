from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
from PIL import Image, ImageDraw

from plugins.endfield.rendering import cards as draw
from plugins.endfield.stages import draw as stage_draw
from plugins.endfield.catalog.models import (
    EffectView,
    OperatorView,
    SkillLevelView,
    SkillView,
    WeaponSkillLevelView,
    WeaponSkillView,
    WeaponView,
)
from plugins.endfield.stages.models import (
    BossRushStageDetails,
    Stage,
    StageCardView,
    StageCatalogGroup,
    StageCatalogItem,
    StageCatalogView,
    StageEnemy,
    StageEnemyPoise,
    StageEnemyResistance,
    StageReward,
    StageSourceRef,
    StageVariant,
)
from otae_bot.infrastructure.rendering.browser import screenshot_web_element


# Captured offline from unchanged 9d0b1bb; verified byte-for-byte against this change.
# Keep fixtures network-independent: missing remote images must not alter snapshots.
EXPECTED_VISUALS: dict[str, tuple[str, str]] = {
    name: tuple(case["visual_signature"])
    for name, case in json.loads(
        (Path(__file__).parent / "fixtures" / "endfield_visual_baseline.json").read_text()
    )["cases"].items()
}


EXPECTED_STAGE_VISUALS: dict[str, tuple[str, str]] = {
    # 1875x4654
    "stage_detail_dense": (
        "0f6e60606676677a",
        "l5mTg4V9hIV9fX94fH56YmVhXWBcXmFdRUhDOz46TVFMYGNfXmFdXWFea2xfjpCJ4OHg8vLv8vPw7Ozp9/fz6erm8/Tw8vLuxsbAvL230NDL6uvn6uvm6Onl7Ozo7/Ht4+Pd29zc6Onp5ufn5OXl5OXl5Obl5ebm6evr7/Hx5ujo4uTj5Obl5+np8PLy9PX15ePW0dLQ2NnZ2NnZ2tvb3N3d3t/e3N7d2tva4OHg3N3d3N3d3t/f6uvr9vf29PX08e/f7+3e3+Hj5OXl9/j4+vv79vf39Pb1+/z8+fr6+fr69PX1+vv79/j49ff28/Tz8vDe7evc3t/h4uTk9PX19vj48/T08fLy9/n59/j49vf38fLy9/j49/j49/n48/X08u/e7evc3uDh4uTk9Pb29/j49PX18vPz+Pn59/j49vf38fLy+Pn59/j49/j48/T08e7d7Orb3d/g4ePj8/T09vf38vPz8fLy9/j49vf39fb27/Dw9vf39vf39vf38vTz9PLg9PHi4uTm5+jp+vv8/P39+Pn59vj5/f///f7+/f399fb2/P7+/f7+/f7+9vf33NzUwsK7zc/M0NLP1NbT1tjX1NXU09PQ19bT0tXV0tbW0NLS1NTUz9LRz9LR3eDf1dfYwsXF5efm4eTi5OXk7/Dw8O/v7uLN5+LK1+La0t7Y5OHV6ODJ8PDr7O7v7/Hw1dfVw8TC2drY1tjV2tzZ4OHe3Nza4N3T3NvQ1trU1trV2djR2tnN39/a1tfV4uTi09XTvb++3N7c3d7c5+fl4uLg6+vp6Ono7Ozs6+rp5+bk7O3r4+Tk19fW5+fl6+zr4OHi3N7f9/j38PLx+fr69PT1+Pn5+fr59fb1+vv79fb39/j4+fr57e7t+vv79Pb20tPUv8LC0NLRzc7N09XVz9DRzc/P0tPSzM7Nz9HQy8zNzc7Oz9DQztDPy83N2dzcz9HOrK+vztDP7e/q6Ojj6url7e3n5eXg7e3o6enk4ODc7Ozn4+Pe6Onk5ubh4ePh",
    ),
    # 1875x1425
    "stage_detail_sparse": (
        "2b6a6a7b7b109784",
        "mpyXc3VtfH12dnl1cHR1cnZ3cnZ3cnZ3cXZ3cXV2cXV2cXV2cHV2cXV2cnV2lJiYbHByZGhpXWFhWF1gW2BnFx4lBg4WChIaCRAYCRAYCREYCRAYChEYBw8ZLC0iWV1blpaIdHRiZ2dVZmZUaGhUYF9MXV1KXV1KYGBNYWFOX19MYWFOYGBNXl5MYmBGiIh33NzW9PLq+fjv8O7m///67Ozj///4//725OPb7+7m4+Lb4+La6+rj7Ozk///7/Pv25Obm8PLz9fb39PX2+/z97O7v9vj58vT15+nr7e7w4uTm5ujq6uzu6uzu9vj58fP02dvbyMrJ4uTi3uDe3N/c6uzp6uzq6uzq7e7s7O3r7e/s7e/s7O7r6+3r6uvp6u3r7Ovi4eLe5ufm5OXl4+Xk8PLw8/Tz8vPz8vPy8vTz8vPy8vPy8vPy8/Tz9PX08PLy7uvb4eHe5+jn5+jo5ebm4+Tk5OXm4+Tl4eLj6Onp4+Tk4uPk5OXl8PHx/Pz89fb23N7d19rb8fPx7vDu7e/t7e/t7e7t7e7t7e7t7O7s7e7t7e7s7O7s7O3s6+3r6+3s7+3d7evi0dLT0tPU7u/v9PX19vf29/f39vb29vf29vb29vf39vf29vf39vf38fLy8/Db6ufN4OPl6Orp9ff39vf47e7v6evr9/j49fb28/T15+np9vf39Pb29ff38fPz6uzn5ejn8vPy8vTy8/Tz8vTy8fPy8PLx8vPy8fPy8fPy8PLx8fPy8fLy8fPy7e/v1NbYx8nI7e/t6+zr6+zr6+zr6+3r6+3r6+zr6+zr6+zr6+3r6+zr6+3r7O3r6+3s6unt8PHx+fr59fb09fb09fb09fb09fb09fb09fb09fb09fb09Pb09Pb09ff17/Hx4+bl297e3ODg4eTk6ezs6Ovr6Ovr6Ovr6Ovr5+rr5+rq5+rq5+rq5+rq5urq5enp4uXk297e297e5Ofn5efn5Ofm4+bm4ubm4uXl4eXl4eTk4OPk4OPk29/g1tvc2t7f",
    ),
    # 1875x3523
    "stage_overview": (
        "0f7a602c3b6b691d",
        "hYiDbG5nbnBpaGplYmZkRkpIPkJAQERCQERCQERCPkNBPkJBP0NBPUJBTlBEeHt10tLN19fR19fQzc3G2djRx8fAysrDzMvFwMG6zMvFzc7Gzc3FzM3FzM3Fzc3D1tfQ5ufn7O3u+fr68/X1+/z98/T19Pb29PX17vDw8/X19/n4+Pr5+Pr69vj4+vz8+Pr6z9HQrrGxwMLCv8HBw8XEwcPCv8HAw8bFwMPBv8LAvsC/vcC/v8HAv8HAvb+/19nY3+Hh19rb6evr8PHx7u7v7e7u8fLy5+jp8PHx7e7v4OHi8fLy5ebn7e7v7O3t5ufn5ebh3t7X8fDo8PDo7Ozk7u7n7ezl7+/n8O/o7e3l7ezm7Ozk7+/n8O/o7ezk7u/r3+DgtLa4uru9t7i6xsfJ3t7fs7S1uru8uLm6xcbG3t/fs7S1uru8ubm6wcLD6+3s4+TgycnFz8/Lzc3J2djU5+flyMrKzM3OysvM19jZ6OnpycrKzM3Oy8zM1dbW7/Hw4OHexMTCy8zJysrIzs7M8vPw+vv5+fn3+fr4+Pn39/j2+fr4+fr4+fr4+vv48vTz4+TkvsDCwsTGwcLFzs/S+fv6/f/8/P78/f79/v78/v78/f78/f78/f78/v/99ff21NbTs7WxxcbCx8jEyMnGzM3My83Ly8rHzMvIx8vKx8zMyMrKycnJwsbFwsbF1tnY0tTTvL683t/d2tzZ3d/d6Ono6Ofn59zG4dvD0NvTzNfR3drN4dnB6enj5efn6+3s3uDg19na8vT17/Hy8fP0+/z8+vr6+fLi9PLg6fHq5u7o8vDl9fHc/f349/j58/X1zM7Kra6pwcK9v8G7xsfBx8jDxMS/ysvHyMnExsbCycnExMXBw8TAwcK+vL241NbT2NrZyszM7vDv7e7u+fj48vLx///++fj4/Pz8/f389fX1///+9vb15ubm/f389/j43uDg1djY5ejo6Ovr7e/v6uzs7/Hx7e7v7vDw7vDw6+zt7/Dx6+3t4uTl6uzt6Ovs",
    ),
    # 1875x1409
    "stage_catalog": (
        "0a7a7a7a7883c587",
        "m52Zc3Vte3x0dnl1c3d4c3d4c3d4c3d4cnZ3cnZ3cnZ3cnZ3cXV3c3d4bHBykZWWbHFyWV1fV1tbUFRYCRAZChEZChEZChEaChEZChEZChEZChIaCBAYExoiKTA4U1lfiop8bm5db29eYGBOWFhGWFhGWFhGWFhGWFhGWFhGWFhGWFhGWFhGWVlHWVlHhIR39fLe9vLg8O3d6+rh5+bd9PHl/Pfj8e7f8O/m///4///3///3///3///4///3+/r0293dwsTG2tzd7/Hy6+7w6+7v6+7v6+7w6+3v6+3v6+3v6u3v6+7v6evs4ePk5Obo3d/eys3O4+Xm3+Dd2tnT9vXu6Ofh2djS9vXu6Ojh2NjS9vXu4eLeyczN4ePj5uno5+jl3t7b9vbz9vbu9/fq+fjs+Pfs9/bq+vjs+Pfs9/bq+vjs9PXu8PLx8/Xz8PLw7u7m6efa8/Hj9/jx9fj38/X09Pb29ff38/X09Pb19ff38vT09Pb19vj28/Xz7vHv19nZu76/2Nvb5+np4uTk6+3t5+jp4uTl6+3t5+np4uTk6+3t5ujn3d/e3N/e4uTj3+Hh0tXX6ezv5eXg4+HV+/nt7uzh4uDV+/ns7+3i4uDV/Pns5ubg0dTV6Orp6evr6+zl5+bc/Prw+Pjw+frz9/fw+Pjy+fnz9/fw+Pjy+fnz9/fw9/fz9/j39/j28fLy5ebh2NnQ5eXd9PXx8vT07/Hx8fPy8vT07/Hx8PLy8vTz7vDw8fPy8vTy6uzr6Orq19nZvsHC293e4eLi2tvb7/Dv5ebl2tvb7+/u5ebl2tva7+/u4uPi0NLS3d/e4uTk4uTk2Nvc8PL07u7n7+3e/fvs9vPm7uzd/fvs9vTm7uzd/vvs7+7n4OLj7/Dv7e/u6enh5ePW9fLl8/Ps8vTz8PHv8fPx8/Tz7/Hv8fLx8vTy7/Hv8fPx8/Xz8fLx7O7t5Obl5ejl5ejl5Ofm5Ofm5Ofm4+bm4uXl4eXl4OTk3+Pj4OPk3eHi1trb1drb2d3e",
    ),
}


def _data_image() -> str:
    image = Image.new("RGBA", (240, 420), (0, 0, 0, 0))
    painter = ImageDraw.Draw(image)
    painter.ellipse((58, 28, 182, 152), fill=(246, 222, 196, 255))
    painter.polygon(((38, 380), (120, 130), (210, 390)), fill=(72, 98, 138, 255))
    painter.rectangle((88, 152, 154, 394), fill=(236, 195, 0, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _operator_sample(*, dense: bool) -> OperatorView:
    levels = [
        SkillLevelView(f"Lv{level}", level, {"攻击倍率": f"{level * 11}%", "失衡值": str(level + 5)})
        for level in range(7, 11)
    ]
    skill_count = 4 if dense else 1
    skills = [
        SkillView(
            f"skill-{index}",
            f"测试技能标题 {index}",
            category=("普攻", "战技", "连携技", "终结技")[index],
            description=("对目标造成物理伤害，并根据目标状态追加失衡值。" * (3 if dense else 1)),
            levels=levels,
        )
        for index in range(skill_count)
    ]
    effects = [
        EffectView(f"effect-{index}", f"测试天赋标题 {index}", "提升攻击力并延长技能持续时间。", "talent")
        for index in range(2 if dense else 1)
    ]
    potentials = [
        EffectView(f"potential-{index}", f"P{index + 1} 潜能标题 {index + 1}", "提高属性并增强技能效果。", "potential")
        for index in range(5 if dense else 1)
    ]
    return OperatorView(
        name="视觉测试干员",
        slug="visual-operator",
        operator_id="visual-operator",
        english_name="Visual Operator",
        rarity=6,
        profession="先锋",
        damage_type="物理",
        weapon_type="单手剑",
        species="黎博利",
        portrait_url=_data_image(),
        skills=skills,
        talents=effects,
        potentials=potentials,
        source_version="visual-v2",
    )


def _weapon_sample(*, dense: bool) -> WeaponView:
    description = (
        "攻击力+{value}，命中目标后提升伤害并延长效果持续时间，叠加达到上限时追加一次攻击。"
        if dense
        else "攻击力+{value}。"
    )
    skill_count = 3 if dense else 2
    return WeaponView(
        name="视觉测试武器",
        slug="visual-weapon",
        title="武器/视觉测试武器",
        english_name="Visual Weapon",
        rarity=6,
        weapon_type="双手剑",
        max_atk=510,
        icon_url=_data_image(),
        skills=[
            WeaponSkillView(
                f"测试武器技能 {index}",
                description * (3 if dense and index == 2 else 1),
                [WeaponSkillLevelView(level, {"value": level * 10}) for level in range(1, 10)],
            )
            for index in range(skill_count)
        ],
        source_version="visual-v2",
    )


_LONG_MECHANIC = (
    "场地中央周期性生成能量潮汐，潮汐覆盖范围内的敌人获得护盾并提升攻击速度，"
    "需要在潮汐结算前打断核心；未打断时全场敌人恢复生命并重置失衡值。"
)


def _stage_resistances() -> tuple[StageEnemyResistance, ...]:
    rows = (
        ("Physical", "物理", 100.0, "#a8865c"),
        ("Fire", "热能", 75.0, "#d4542a"),
        ("Cryst", "凝结", 120.0, "#2f86c4"),
        ("Pulse", "电冲", 60.0, "#8353c8"),
        ("Natural", "自然", 100.0, "#3f9a52"),
    )
    return tuple(
        StageEnemyResistance(element=element, label=label, percent=percent, scalar=percent / 100, color=color)
        for element, label, percent, color in rows
    )


def _stage_enemy(index: int, *, detailed: bool) -> StageEnemy:
    return StageEnemy(
        enemy_id=f"enemy-{index}",
        name=f"视觉测试敌人 {index}",
        icon_url=_data_image(),
        level=60 + index,
        count=index + 1,
        hp=120000 + index * 5000,
        attack=1800 + index * 40,
        defense=900 + index * 20,
        article_title=f"敌人/视觉测试敌人 {index}",
        resistances=_stage_resistances() if detailed else None,
        poise=(
            StageEnemyPoise(max_value=3200.0, damage_scalar=1.2, recover_seconds=8.5, knots=(0.25, 0.5, 0.75))
            if detailed
            else None
        ),
    )


def _stage_variant(index: int, *, dense: bool) -> StageVariant:
    return StageVariant(
        id=f"depth-{index}",
        label=f"{'一二三四'[index]}级",
        sort_order=index,
        recommended_level=60 + index * 10,
        stamina_cost=20 + index * 5,
        mechanics=tuple(f"{_LONG_MECHANIC}（阶段 {step + 1}）" for step in range(4 if dense else 1)),
        enemies=tuple(_stage_enemy(item, detailed=dense) for item in range(8 if dense else 1)),
        rewards=tuple(
            StageReward(
                item_id=f"item-{item}",
                name=f"测试掉落物 {item}",
                icon_url=_data_image(),
                quantity_text=f"×{(item + 1) * 3}",
                rarity=4 + item % 2,
            )
            for item in range(6 if dense else 1)
        ),
    )


def _stage_sample(*, dense: bool) -> Stage:
    variant_count = 4 if dense else 1
    return Stage(
        id="visual-stage",
        name="危境再现·视觉测试",
        aliases=("视觉测试",),
        family_key="boss-rush",
        family_name="危境再现",
        summary="用于视觉回归的关卡说明文本，覆盖长句换行与标点密度。" * (3 if dense else 1),
        location="测试地区 · 北部高地",
        unlock_condition="通关主线第五章后开放",
        source=StageSourceRef("FZ Wiki", "副本/危境再现·视觉测试", "visual-r1", "2026-07-27"),
        variants=tuple(_stage_variant(index, dense=dense) for index in range(variant_count)),
        extension=BossRushStageDetails(
            boss_name="视觉测试首领",
            series_id="series-1",
            series_name="视觉测试系列",
            depth_count=variant_count,
            icon_url=_data_image(),
        ),
    )


def _stage_catalog_sample() -> StageCatalogView:
    return StageCatalogView(
        groups=tuple(
            StageCatalogGroup(
                f"family-{group}",
                f"视觉测试玩法族 {group}",
                tuple(
                    StageCatalogItem(
                        f"副本/关卡 {group}-{index}",
                        f"视觉测试关卡 {group}-{index}",
                        f"family-{group}",
                        f"视觉测试玩法族 {group}",
                        "visual-r1",
                        "2026-07-27",
                        queryable=index % 4 != 0,
                        recommended_level=60 + index,
                        region="测试地区",
                    )
                    for index in range(6)
                ),
            )
            for group in range(3)
        ),
        source="FZ Wiki",
        revision="visual-catalog-r1",
        updated_at="2026-07-27",
    )


def _visual_signature(content: bytes) -> tuple[str, str]:
    image = Image.open(io.BytesIO(content)).convert("RGB")
    snapshot = image.resize((16, 16), Image.Resampling.LANCZOS).tobytes()
    gray = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
    frequency = cv2.dct(gray)[:8, :8]
    values = frequency.flatten()[1:]
    median = float(np.median(values))
    bits = [value >= median for value in values]
    packed = 0
    for bit in bits:
        packed = (packed << 1) | int(bit)
    return f"{packed:016x}", base64.b64encode(snapshot).decode("ascii")


def _hash_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def _normalized_pixel_error(first: str, second: str) -> float:
    current = np.frombuffer(base64.b64decode(first), dtype=np.uint8).astype(np.int16)
    expected = np.frombuffer(base64.b64decode(second), dtype=np.uint8).astype(np.int16)
    return float(np.abs(current - expected).mean() / 255 * 100)


class EndfieldVisualRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.remote_assets = patch.object(draw, "fetch_many_resilient", AsyncMock(return_value=({}, {})))
        self.remote_assets.start()

    async def asyncTearDown(self):
        from otae_bot.infrastructure.http.client import close_http_client
        from otae_bot.infrastructure.rendering.browser import close_browser
        from otae_bot.infrastructure.rendering.executor import close_image_executor

        self.remote_assets.stop()
        await close_browser()
        await close_image_executor()
        await close_http_client()

    async def test_four_card_density_snapshots(self):
        cases = {
            "operator_dense": (await draw.draw_operator_card(_operator_sample(dense=True)), 3200),
            "operator_sparse": (await draw.draw_operator_card(_operator_sample(dense=False)), 3200),
            "weapon_dense": (await draw.draw_weapon_card(_weapon_sample(dense=True)), 3200),
            "weapon_sparse": (await draw.draw_weapon_card(_weapon_sample(dense=False)), 2720),
        }
        self.assertEqual(set(EXPECTED_VISUALS), set(cases))
        for name, (content, expected_width) in cases.items():
            image = Image.open(io.BytesIO(content)).convert("RGB")
            self.assertEqual(image.width, expected_width)
            self.assertGreaterEqual(image.height, 1440)
            self.assertLessEqual(image.height, draw.CARD_MAX_HEIGHT * 2)
            near_white = np.asarray(image.resize((80, 80), Image.Resampling.BILINEAR)).mean(axis=2) > 246
            self.assertLess(float(near_white.mean()), 0.82)
            current_hash, current_snapshot = _visual_signature(content)
            expected_hash, expected_snapshot = EXPECTED_VISUALS[name]
            self.assertLessEqual(_hash_distance(current_hash, expected_hash), 12)
            self.assertLessEqual(_normalized_pixel_error(current_snapshot, expected_snapshot), 6.0)

    async def test_stage_card_density_snapshots(self):
        dense = _stage_sample(dense=True)
        sparse = _stage_sample(dense=False)
        cases = {
            "stage_detail_dense": await stage_draw.draw_stage_card(
                StageCardView(dense, "detail", selected_variant=dense.variants[3])
            ),
            "stage_detail_sparse": await stage_draw.draw_stage_card(
                StageCardView(sparse, "detail", selected_variant=sparse.variants[0])
            ),
            "stage_overview": await stage_draw.draw_stage_card(StageCardView(dense, "overview")),
            "stage_catalog": await stage_draw.draw_stage_catalog_card(_stage_catalog_sample()),
        }
        self.assertEqual(set(EXPECTED_STAGE_VISUALS), set(cases))
        for name, content in cases.items():
            image = Image.open(io.BytesIO(content)).convert("RGB")
            self.assertEqual(image.width, 1875)
            self.assertLessEqual(image.height, stage_draw.STAGE_CARD_MAX_HEIGHT * 1.25)
            near_white = np.asarray(image.resize((80, 80), Image.Resampling.BILINEAR)).mean(axis=2) > 246
            self.assertLess(float(near_white.mean()), 0.82)
            current_hash, current_snapshot = _visual_signature(content)
            expected_hash, expected_snapshot = EXPECTED_STAGE_VISUALS[name]
            self.assertLessEqual(_hash_distance(current_hash, expected_hash), 12)
            self.assertLessEqual(_normalized_pixel_error(current_snapshot, expected_snapshot), 6.0)

    async def test_every_paginated_catalog_page_renders_inside_the_ceiling(self):
        base = _stage_catalog_sample()
        # Well past the ~450 entries that still fit a single 12000px image.
        huge = replace(
            base,
            groups=tuple(
                replace(group, items=group.items * 60) for group in base.groups
            ),
        )

        pages = await stage_draw.draw_stage_catalog_cards(huge)

        self.assertGreater(len(pages), 1)
        for page in pages:
            image = Image.open(io.BytesIO(page)).convert("RGB")
            self.assertEqual(image.width, 1875)
            self.assertLessEqual(image.height, stage_draw.STAGE_CARD_MAX_HEIGHT * 1.25)

    async def test_strict_height_rejects_oversized_element(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.html"
            path.write_text('<div id="card" style="width:100px;height:200px"></div>', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "exceeds limit"):
                await screenshot_web_element(
                    path.resolve().as_uri(),
                    "#card",
                    viewport=(100, 1),
                    max_height=100,
                    strict_max_height=True,
                    settle_ms=0,
                )

    async def test_cdp_export_captures_the_measured_element(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cdp-export.html"
            path.write_text(
                '<style>html,body{margin:0}.card{width:120px;height:80px;background:#286cd6}</style>'
                '<div class="card"></div>',
                encoding="utf-8",
            )
            content = await screenshot_web_element(
                path.resolve().as_uri(),
                ".card",
                viewport=(120, 80),
                settle_ms=0,
                wait_for_fonts=True,
                resource_wait_timeout_ms=1000,
                screenshot_backend="cdp",
            )
            image = Image.open(io.BytesIO(content))
            self.assertEqual(image.size, (120, 80))


if __name__ == "__main__":
    unittest.main()

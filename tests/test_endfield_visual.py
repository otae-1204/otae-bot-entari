from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from plugins.endfield import draw
from plugins.endfield.models import (
    EffectView,
    OperatorView,
    SkillLevelView,
    SkillView,
    WeaponSkillLevelView,
    WeaponSkillView,
    WeaponView,
)
from utils.image_utils import screenshot_web_element


EXPECTED_VISUALS: dict[str, tuple[str, str]] = {
    "operator_dense": (
        "71ce56935271d8f0",
        "9ff09vj2+fv4+vv77u/n8NBF8ssw8cwh1dbTycvN6uvr5+jmsLCoqKuupqmrwMLEyMnJn6GirrCvyMrL8PDn8ty39t3G8NJfurmup6mt5OXk4OHfqqmgnqCim52dury91dbVt7m5z9DP3N/e7enk9dzD99/K79iixcGm0dLW+fr78/Lp2NfN7e3v////+fr67vDu9PXz8/Px/v//6uXf9tzA+N/F7dmwzsevwsHBwMXJ2NfLycGuu7y+w8bI2tzd3N3d9vf239/f/f7+5uTa8NnC9d7T59GP1tXF3eDj3d/goZ+Q19XO5ObozM7Lo6Sg2NnX4eLh19fV6uzw6uXI3L9R2L5p48hLycm+1NTV9fb46unexcS23d7h8/T05ufo397Uz9HP7e7s8PP27OfK6MAA674A6M5N0MvBv767v8PH1tXK0cm2vL29y83O6evr6enl09TT4uPi/P7/7ObB6cEF68AD585Y2tzc4+Xk3+LjoqCQ0tHF4OHez8/Is7St6Onk1NXT4+Xj+vz+7OS56cAA6sAA5tBj4OPm8fHv+fn68vHs5ufj9/f44+Xkw8XE6Onl1NXT4+Ti+v3+6OCz1rUM47sA08Z6x8fGzM3M8vLy9vXyy8rE19jY+vr5+vz86enl1NXS4uTi/f7+29azvaUe37kAsLKd9PT36+vq7O3s7+/u6+vq6uzr4+Xl4OLj6erm1tfU4uTj/v7+xsi3q5or47wAkJym4uXp2dze2Nze1trc1Nnb09faz9TXz9XY6Ojh0dLN3uDh/v7+q7XCoJQx6cAAdIWV4+fs4eXm3+Pm3eHk29/i2d3g19zf1Nrd8vPx8/Ty8PPy///6iZ3Gm5A17sQAXnKI0tjh4+bn3eHj29/i2d3g19zf1drd0tjc9Pb0+Pn39ff2///6fZCtopQu8MYBTGJ9t8HQ5uno2t/i2t7h2Nzg1tve09nc0dfb7vDv7/Hx7/Dt7/P83Mt24LgC68IEsrGRytLh4OPj2t7i2d3g1tvf1Nrd0tjb0Nba",
    ),
    "operator_sparse": (
        "7186465a5a72d9f8",
        "9ff09vj2+fv4+vv77u/n8NBF8ssw8cwh1dbTycvN6uvr5+jmsbGpqKuupqmrwMLEyMnJn6GirrCvyMrL8PDn8ty39t3G8NJfurmuqKqu5OXl3t/bpqWbnp+im52eury91dbVt7m5z9DP3N/e7unk9dzD99/K79iixcGlzc/T9vb2////////////////+Pn57vDu9PXz8/Px/v//6uXf9tzA+N/F7divzsy53d3e0dTUtrez9vb28/Py9PTz7u/v3N3d9vf239/f/f7+5uTa8NnC9d7T6NKQ0tLF6+7w8PHvw8TC9/f39fX19/f28PDw2NnX4eLh19fV6uzw6uXI3L9R2L5p4shQxcbEzs/P8/Pz////9/f29/f2+fn48fHx397Uz9DP7u7s8PP27OfK6MAA678A5cxR7fD47O3q7e7u8PDw8PHw8vLx6enp5Obm6erm1dbU4ePh/P7/7ebB6cEF68AD589e3+Xy3+Hg3eDi2t7g2Nze19vd1Nja1dnc9fb1+vv5+fn19vn+7eW56cAA68AA6NJo5+z75ejn5Ofp4uXo4OPm3uLk2+Dj2d7h9Pb09vf19/bz9/v/6eGz1rUN47sA1ch+6e344+bm4ubo4OTm3uLk3ODj2t7h2Nzf9PX09/j29vfz+v3/3NezvaUe37kAs7Wg7O/04eXm4eXn3+Pl3eHk29/i2N3g1tvf9PX09/j29ffz/v/+x8i4q5or47sAkZ6o6u3x4OTm4OPm3uLk2+Dj2d7h19zf1dre9PX09/j29Pb0///8rLbCoJQx6cAAdISU4ubr4eTm3uLl3ODj2t/i2N3g1tve1Nnd9PX09/j28/b0///6iZ3Gm5A17sQAXnKI0tjh4+bn3eHk29/i2d7h19zf1drd09jc9PX09/n39ff1///6fpCtopQu8MYBTGJ9t8HQ5uno2t/i2t7h2Nzg1tve09nc0dfb7vDw7/Hx7/Dt7/P83Mt24LgC68IEs7GRytLh4OPj2t7i2d3g1tvf1Nrd0tjb0Nba",
    ),
    "weapon_dense": (
        "6b489497c8b7f05a",
        "9vb09vf29/j2+vr58vPx0NLRy83M6uzq7/Hw8vTy8vTz7O7t7O7t7O7t7O7t6uzswcLClpial5mbsbKz+Pj3tri5r7Cx5+jn7O3s2NrZ19jY7e7u7e3t7O3t6uvr6Orp2NnZwcLCz8/P2tra9PX03N3fur3AyczQ3uDj2dvdsLO3wMPHw8bKxcjM1Nba5+nr7e3s8/Py8vLx9/j38fLx6OrsuLvArbC10tXY7e7ws7a7naKno6esp6uwyMrO5Ofo4OHg7u7u5ufn6Ono8/Tz5efpuLvAs7a71djb6erssrW6pamuqq6yrbG2ys3R5efp+Pn3+Pj3+fj1+/v68fLx5ujquLzAsrW61dfa6evtsrW6o6esqKyxrLC1ys3R5Ofo9ff49u3i9N7F9/bz8vPy5efpt7q+sbS509bZ6evtsrW6o6esqKyxrLC0ys3Q5Obo9Pb39ebX9tzE8+7p8fPz6uztv8LGtbi92dve6evtsra6o6etqa2xrLC1ys3Q5Obo9ff46uPY4cyb8O/s8/T01dfYzM3P7u/x8/T16uvtsra6pKitqa2yrbC1y83R5Obo9vf88O3Z5sUe9vTp8vP13N7ftLe7u77C1tjb6OnsrbC1nqKno6etp6uwyMrO4+Xn9fb49fPo58Ui8u7a8fP36Onrub3BsLO409XZ8PHx3d/g2tzd297f3d/g7O3u6+3t9/f36errzbAW1tS59fb85ObnuLzAs7a709XY9vb1+/z5+vz6+vz5+vv5+Pn37e/u+/r309nlvKMVsLCT8vX95ebnubzAs7e71NbZ9fX09Pb09Pb09PX09PXz9vf17e/u//33tcHatJwMkJBl3eP06urptbi9rrK30NLW9PX09ff19Pb19Pb19PXz9vf17e/u+fn20tfiwLZ3x8Op5Ofu6erqw8bJw8bJ4ePl9vj29/n39/j39/j2+Pn29/j27O7u7/Hx8/Pw5+rz7vD16uzr3eDg3eDh4uTl5+rq4+bm4uXl4eTk4OPj4OPj29/f1dnb",
    ),
    "weapon_sparse": (
        "1f200f3f709ee0ba",
        "9/j1+vv5+fr5+/z6+fr45Obk0dPS2tzb8fLx8fPy8fPy7/Hx7/Hw7/Dw7vDw6u3t1NXUrK2utLW2q62u39/f1tjXnZ+gtbe35ufm5OXk4uTi5ufm5ufm5ufm5ufm6OnpyMnIkJKUo6WllZeYzc7O7e7uvL3A0dLV9fX329zexsfK8vP17/Dy7/Hy8PHz7O3u9PXz7+/v+Pj3////+vr46+3t3d/i6ert9vf59vf2+Pn39vf19vf19vf19/j27/Hw3+Df6urq4uPi4OHh7/Dv7e/v3N7h6ers9fb49PX09vf19vf19vf19vf19/j27vDw5eXk7e3s7e7u6urq8vLx7O7u3d/i6ert9vb59PX19vf19vf19vf19vf19/j27vDv+Pn3+vr5+PXw+/v69PX06+3t293g6Ons9vf59PX19vf19vf19vf19ff19vj27fDv9Pb29vLs9ty/9vDp9PX17e7u5efp7e/x9fb49fb19vf19vf19vf19ff19vj27e/v9ff38uzm8NjF7+nk9vf35ufnzM7P3N3f9fb39PX09vf19vf19vf19ff19vj27e/v9vb07/Dz3sZj7unR9vf85ujoxcfK2tzf9/f59PX09vf19vf19ff19fb19vf27O7u9PTx9vj/68048+q99Pb/7O3s4uTn7O3w9fb49fb19vf19ff19ff19fb19vf27O7t9PTw9Pf/2MBD3dSg9fj/6uvq3N7h6Ors9vf59fb19vf19ff19ff19fb19vf17O7t9vXx6e7+vqw/t7F48fT/6+zq3d/i6ers9fb49fb19ff19ff19fb19fb09/f16+3t+fjy2eD3qJs3nZZO2eD28PHt293h6evt9vf58/X09ff19fb19fb19PXz9/f16+3t9vbz6OvywLyZyMOl5unw6uvq4+Xm5+jq9PX29vj39/n39/j39/j2+Pn29vb06evq7vDw8fLw6Orx6Ovx7u/v3eDh3d/g2Nvb3N/g4OPj3uHi3eDh3N/f29/f1dna09fZ",
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


if __name__ == "__main__":
    unittest.main()

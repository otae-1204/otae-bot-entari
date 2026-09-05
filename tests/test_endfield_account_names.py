import unittest
from hashlib import md5

from plugins.endfield.account.detail.names import build_account_detail_name_map
from plugins.endfield.account.detail.service import build_account_detail_view
from plugins.endfield.account.i18n import localized_text, server_label


class EndfieldAccountDetailNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name_map = build_account_detail_name_map(
            {
                "chr_1": {"charId": "chr_1", "name": {"id": 1, "text": ""}},
            },
            {
                "chr_1": {
                    "skillGroupMap": {
                        "chr_1_NormalSkill": {
                            "skillGroupId": "chr_1_NormalSkill",
                            "name": {"id": 2, "text": ""},
                            "skillIdList": ["chr_1_normal_skill"],
                        }
                    }
                }
            },
            {
                "wpn_1": {"weaponId": "wpn_1", "engName": {"id": 3, "text": ""}},
            },
            {
                "item_1": {"id": "item_1", "name": {"id": 4, "text": ""}},
                "wpn_1": {"id": "wpn_1", "name": {"id": 6, "text": ""}},
            },
            {
                "suit_1": {"list": [{"suitName": {"id": 5, "text": ""}}]},
            },
            {
                "1": "中文角色",
                "2": "普通技能",
                "3": "中文武器",
                "4": "中文装备",
                "5": "中文套装",
                "6": "物品中文武器",
                "7": "基建技能",
                "8": "进驻制造舱时，舱室内干员心情消耗降低 8%",
                "9": "武陵",
                "10": "天王坪援建点",
                "11": "制造舱",
            },
            spaceship_skill_table={
                "spaceship_skill_chr_1_1_1": {
                    "id": "spaceship_skill_chr_1_1_1",
                    "name": {"id": 7, "text": ""},
                    "desc": {"id": 8, "text": ""},
                }
            },
            domain_table={
                "domain_2": {
                    "domainId": "domain_2",
                    "domainName": {"id": 9, "text": ""},
                }
            },
            settlement_table={
                "stm_hongs_1": {
                    "settlementId": "stm_hongs_1",
                    "settlementName": {"id": 10, "text": ""},
                }
            },
            spaceship_room_type_table={
                "1": {"type": 1, "name": {"id": 11, "text": ""}}
            },
            version="test-version",
        )

    def test_resolves_names_from_akedata_text_ids(self):
        self.assertEqual(self.name_map.character_names["chr_1"], "中文角色")
        self.assertEqual(self.name_map.character_names[md5(b"chr_1").hexdigest()], "中文角色")
        self.assertEqual(self.name_map.skill_names["chr_1_normal_skill"], "普通技能")
        self.assertEqual(self.name_map.weapon_names["wpn_1"], "物品中文武器")
        self.assertEqual(self.name_map.item_names["item_1"], "中文装备")
        self.assertEqual(self.name_map.suit_names["suit_1"], "中文套装")
        self.assertEqual(self.name_map.spaceship_skill_names["spaceship_skill_chr_1_1_1"], "基建技能")
        self.assertEqual(
            self.name_map.spaceship_skill_descriptions["spaceship_skill_chr_1_1_1"],
            "进驻制造舱时，舱室内干员心情消耗降低 8%",
        )
        self.assertEqual(self.name_map.domain_names["domain_2"], "武陵")
        self.assertEqual(self.name_map.settlement_names["stm_hongs_1"], "天王坪援建点")
        self.assertEqual(
            self.name_map.settlement_names[md5(b"stm_hongs_1").hexdigest()],
            "天王坪援建点",
        )
        self.assertEqual(self.name_map.spaceship_room_names["1"], "制造舱")

    def test_resolves_object_shaped_i18n_values(self):
        name_map = build_account_detail_name_map(
            {"chr_1": {"charId": "chr_1", "name": {"id": 1}}},
            {},
            {},
            {},
            {},
            {"1": {"text": "对象格式中文角色"}},
        )
        self.assertEqual(name_map.character_names["chr_1"], "对象格式中文角色")

    def test_localized_text_handles_locale_objects_and_server_aliases(self):
        self.assertEqual(
            localized_text({"zh-CN": "中文名称", "en": "English Name"}),
            "中文名称",
        )
        self.assertEqual(
            localized_text(
                {"id": "name_1"},
                translations={"name_1": {"zh": "翻译名称", "en": "Translated Name"}},
            ),
            "翻译名称",
        )
        self.assertEqual(server_label({"en": "China"}), "国服")

    def test_account_detail_prefers_akedata_names_over_api_names(self):
        detail = {
            "chars": [
                {
                    "id": "char-instance-1",
                    "level": 90,
                    "charData": {
                        "id": md5(b"chr_1").hexdigest(),
                        "name": "Arcane",
                        "skills": [
                            {"id": md5(b"chr_1_normal_skill").hexdigest(), "name": "Normal Skill"}
                        ],
                    },
                    "weapon": {
                        "weaponData": {
                            "id": md5(b"wpn_1").hexdigest(),
                            "name": "English Weapon",
                        },
                    },
                    "bodyEquip": {
                        "equipData": {
                            "id": md5(b"item_1").hexdigest(),
                            "name": "English Equip",
                            "suit": {
                                "id": md5(b"suit_1").hexdigest(),
                                "name": "English Suit",
                            },
                        }
                    },
                }
            ]
        }

        view = build_account_detail_view(detail, uid="uid", name_map=self.name_map)
        operator = view.operators[0]
        self.assertEqual(operator.name, "中文角色")
        self.assertEqual(operator.skills[0].name, "普通技能")
        self.assertEqual(operator.weapon.name, "物品中文武器")
        self.assertEqual(operator.equips[0].name, "中文装备")
        self.assertEqual(operator.equips[0].suit_name, "中文套装")


if __name__ == "__main__":
    unittest.main()

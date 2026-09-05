"""Labels and calculation constants shared by catalog view builders."""



INDEPENDENT_EQUIPMENT_GROUP_NAMES = frozenset({"纾难装备组", "涉渊装备组"})

EQUIPMENT_ABILITY_ATTRIBUTES = {"Str": "力量", "Agi": "敏捷", "Wisd": "智识", "Will": "意志"}

EQUIPMENT_ABILITY_WILDCARD = "通用"

OPERATOR_ELEMENT_ORDER = {name: index for index, name in enumerate(("物理", "灼热", "电磁", "寒冷", "自然"))}

OPERATOR_PROFESSION_ORDER = {
    name: index for index, name in enumerate(("近卫", "术师", "突击", "先锋", "重装", "辅助"))
}

WEAPON_TYPE_ORDER = {
    name: index for index, name in enumerate(("单手剑", "双手剑", "施术单元", "长柄武器", "手铳"))
}

_MIN_AKEDATA_MEDAL_COMPLETENESS = 0.8

FZ_ASSET_HOST = "assets.fz.wiki"

WEAPON_OPTIONS = ("单手剑", "双手剑", "施术单元", "长枪", "手铳")

SKILL_CATEGORY_ORDER = {
    "普攻": 0,
    "战技": 1,
    "终结技": 2,
    "连携技": 3,
}

WEAPON_NAMES = {
    1: "单手剑",
    2: "双手剑",
    3: "施术单元",
    4: "长枪",
    5: "手铳",
}

TERM_SUFFIXES = (
    "附着",
    "异常",
    "伤害",
    "脆弱",
    "爆发",
    "增幅",
    "抗性",
    "击飞",
    "破防",
    "猛击",
    "倒地",
    "碎甲",
    "碎冰",
    "冻结",
    "燃烧",
    "导电",
    "腐蚀",
    "失衡",
    "消耗",
)

WARFARIN_METRIC_LABELS = {
    "atk_scale": "攻击倍率",
    "atk_scale_will": "阵诀·意伤害倍率",
    "atk_scale_wisd": "阵诀·智伤害倍率",
    "atk_scale_touch": "触碰伤害倍率",
    "atk_scale_boom": "爆发伤害倍率",
    "atk_scale_laser": "集束打击伤害倍率",
    "atk_scale_laser_will": "阵诀·意集束打击倍率",
    "atk_scale_laser1": "第一段集束打击倍率",
    "atk_scale_laser2": "第二段诀明伤害倍率",
    "poise": "失衡值",
    "poise_touch": "触碰失衡值",
    "poise_boom": "爆发失衡值",
    "poise_laser": "集束打击失衡值",
    "laser_count": "集束打击次数",
    "usp": "获得终结技能量",
    "atb": "技力",
    "duration": "持续时间（秒）",
    "duration2": "阵法持续时间（秒）",
    "duration_will": "阵诀·意持续时间（秒）",
    "duration_wisd": "阵诀·智持续时间（秒）",
    "spell_vul_per_will": "每点意志脆弱效果",
    "rate_pre": "基础脆弱效果",
    "atb_return_wisd": "阵诀·智技力返还",
    "max_spell_vul_will": "阵诀·意最大脆弱效果",
}

WARFARIN_PERCENT_METRIC_KEYS = {
    "spell_vul_per_will",
    "rate_pre",
    "max_spell_vul_will",
}

LOADOUT_ATTRIBUTE_NAMES = {
    "Str": "力量",
    "Agi": "敏捷",
    "Wisd": "智识",
    "Will": "意志",
    "CriticalRate": "暴击率",
    "CriticalDamageIncrease": "暴击伤害",
    "PhysicalAndSpellInflictionEnhance": "源石技艺强度",
    "HealOutputIncrease": "治疗效率加成",
    "HealTakenIncrease": "受治疗效率加成",
    "ComboSkillCooldownScalar": "连携技冷却缩减",
    "UltimateSpGainScalar": "终结技充能效率",
    "PoiseDamageOutputScalar": "失衡效率加成",
    "NormalAttackDamageIncrease": "普通攻击伤害加成",
    "NormalSkillDamageIncrease": "战技伤害加成",
    "ComboSkillDamageIncrease": "连携技伤害加成",
    "UltimateSkillDamageIncrease": "终结技伤害加成",
    "PhysicalDamageIncrease": "物理伤害加成",
    "SpellDamageIncrease": "法术伤害加成",
    "FireDamageIncrease": "灼热伤害加成",
    "PulseDamageIncrease": "电磁伤害加成",
    "CrystDamageIncrease": "寒冷伤害加成",
    "NaturalDamageIncrease": "自然伤害加成",
    "EtherDamageIncrease": "超域伤害加成",
    "AllDamageIncrease": "所有伤害加成",
    "AllDamageTakenScalar": "全伤害减免",
}

LOADOUT_PERCENT_ATTRIBUTES = frozenset(
    {
        "CriticalRate",
        "CriticalDamageIncrease",
        "HealOutputIncrease",
        "HealTakenIncrease",
        "ComboSkillCooldownScalar",
        "UltimateSpGainScalar",
        "PoiseDamageOutputScalar",
        "NormalAttackDamageIncrease",
        "NormalSkillDamageIncrease",
        "ComboSkillDamageIncrease",
        "UltimateSkillDamageIncrease",
        "PhysicalDamageIncrease",
        "SpellDamageIncrease",
        "FireDamageIncrease",
        "PulseDamageIncrease",
        "CrystDamageIncrease",
        "NaturalDamageIncrease",
        "EtherDamageIncrease",
        "AllDamageIncrease",
    }
)

LOADOUT_EFFECT_KEY_TARGETS = {
    "str": "Str",
    "agi": "Agi",
    "wisd": "Wisd",
    "will": "Will",
    "atk": "AtkPercent",
    "atk_up": "AtkPercent",
    "primary_attr_up": "MainPercent",
    "main_attr_up": "MainPercent",
    "main_attribute_up": "MainPercent",
    "hp_up": "MaxHpFinal",
    "max_hp": "MaxHpFinal",
    "critical_rate": "CriticalRate",
    "criticalrate": "CriticalRate",
    "crit_rate": "CriticalRate",
    "critical_damage": "CriticalDamageIncrease",
    "criticaldamageincrease": "CriticalDamageIncrease",
    "crit_damage": "CriticalDamageIncrease",
    "heal": "HealOutputIncrease",
    "heal_up": "HealOutputIncrease",
    "heal_output_up": "HealOutputIncrease",
    "second_attr_up": "SubPercent",
    "sub_attr_up": "SubPercent",
    "dmg_up": "AllDamageIncrease",
    "ultimate_gain_up": "UltimateSpGainScalar",
    "phy_spell_up": "PhysicalAndSpellInflictionEnhance",
    "physicalandspellinflictionenhance": "PhysicalAndSpellInflictionEnhance",
    "phy_dmg_up": "PhysicalDamageIncrease",
    "spell_dmg_up": "SpellDamageIncrease",
    "fire_dmg_up": "FireDamageIncrease",
    "pulse_dmg_up": "PulseDamageIncrease",
    "cryst_dmg_up": "CrystDamageIncrease",
    "natural_dmg_up": "NaturalDamageIncrease",
    "ether_dmg_up": "EtherDamageIncrease",
}

LOADOUT_STATUS_TAGS = {
    "导电": "ba.conduct",
    "腐蚀": "ba.corrupt",
    "碎甲": "ba.fracture",
}

LOADOUT_STATUS_DURATION_KEYS = {
    "导电": "duration_conduct",
    "腐蚀": "duration_corrupt",
    "碎甲": "duration_fracture",
}

LOADOUT_STATUS_LEVELS = {
    "导电": tuple((value, duration) for value, duration in zip((0.12, 0.16, 0.20, 0.24), (12, 18, 24, 30))),
    "腐蚀": tuple(zip((3.6, 4.8, 6.0, 7.2), (0.84, 1.12, 1.4, 1.68), (12.0, 16.0, 20.0, 24.0))),
    "碎甲": tuple((value, duration) for value, duration in zip((0.12, 0.16, 0.20, 0.24), (12, 18, 24, 30))),
}

LOADOUT_STATUS_REACTIONS = {
    "自然附着": "腐蚀",
    "电磁附着": "导电",
}

LOADOUT_GROWTH_ATTRIBUTE_KEYS = {
    39: "Str",
    40: "Agi",
    41: "Wisd",
    42: "Will",
}

// Public art identifiers; all numeric values and records below are synthetic fixtures.
export const operators = [
  {
    id: "laevatain",
    name: "莱万汀",
    en: "LAEVATAIN",
    job: "突击",
    element: "灼热",
    rarity: 6,
    color: "#dc553c",
  },
  {
    id: "endministrator",
    name: "管理员",
    en: "ENDMINISTRATOR",
    job: "近卫",
    element: "物理",
    rarity: 6,
    color: "#b9b36a",
  },
  {
    id: "perlica",
    name: "佩丽卡",
    en: "PERLICA",
    job: "术师",
    element: "电磁",
    rarity: 5,
    color: "#c19a42",
  },
  {
    id: "chen",
    name: "陈千语",
    en: "CHEN QIANYU",
    job: "近卫",
    element: "物理",
    rarity: 5,
    color: "#799b95",
  },
  {
    id: "gilberta",
    name: "洁尔佩塔",
    en: "GILBERTA",
    job: "辅助",
    element: "自然",
    rarity: 6,
    color: "#8ea45c",
  },
  {
    id: "yvonne",
    name: "伊冯",
    en: "YVONNE",
    job: "突击",
    element: "寒冷",
    rarity: 6,
    color: "#69a7b3",
  },
  {
    id: "wulfgard",
    name: "狼卫",
    en: "WULFGARD",
    job: "术师",
    element: "灼热",
    rarity: 5,
    color: "#ad685b",
  },
  {
    id: "arclight",
    name: "弧光",
    en: "ARCLIGHT",
    job: "先锋",
    element: "电磁",
    rarity: 5,
    color: "#ad9855",
  },
];
export const weapons = ["熔铸火焰", "不知归", "赫拉芬格", "遗忘"];
export const slots = ["护甲", "护手", "配件Ⅰ", "配件Ⅱ", "道具"];
export const gearArt = [
  "gear-body",
  "gear-hand",
  "gear-accessory",
  "gear-accessory2",
];
export const stamp = "2026.09.05 12:00 / 示例快照";

// A route is one review surface, not a newly implemented plugin command.
const definitions = [
  [
    "入口与查询",
    [
      ["help", "帮助总览", "help", "/ef 帮助"],
      ["search", "搜索结果", "search", "/ef 搜索 火"],
      ["accounts", "账号管理", "accounts", "/ef 主账号 2"],
      ["calculator", "异常效果速算", "calculator", "/ef 速算 2腐蚀 200"],
    ],
  ],
  [
    "图鉴与配装",
    [
      ["operator", "干员详情", "operator", "/ef 莱万汀"],
      ["operators", "干员图鉴", "catalog", "/ef 干员", "operator"],
      ["weapons", "武器图鉴", "catalog", "/ef 武器", "weapon"],
      ["weapon", "武器详情", "item", "/ef 熔铸火焰", "weapon"],
      ["equipment", "装备总目录", "catalog", "/ef 装备", "equipment"],
      [
        "equipment-filter",
        "装备属性筛选",
        "catalog",
        "/ef 装备 主力量 副敏捷",
        "filter",
      ],
      [
        "equipment-detail",
        "装备详情",
        "item",
        "/ef 装备 示例护甲",
        "equipment",
      ],
      ["equipment-set", "单套装目录", "set", "/ef 装备 示例套装"],
      ["loadout", "干员配装", "loadout", "/ef 配装 莱万汀"],
    ],
  ],
  [
    "账号与基建",
    [
      ["profile", "个人名片", "profile", "既有名片预览"],
      ["roster", "账号详情 · 首页", "roster", "/ef 账号 1", "first"],
      ["roster-next", "账号详情 · 续页", "roster", "/ef 账号 1", "next"],
      ["base", "基建与帝江号", "base", "/ef 账号 基建"],
    ],
  ],
  [
    "签到与资源",
    [
      ["attendance", "签到回执", "attendance", "/ef 签到 全部"],
      ["investment", "养成投入总览", "investment", "/ef 养成统计", "summary"],
      [
        "investment-detail",
        "养成材料与排行",
        "investment",
        "/ef 养成统计",
        "detail",
      ],
      ["currency", "资源流水汇总", "currency", "/ef 流水 -d 7"],
    ],
  ],
  [
    "抽卡档案",
    [
      ["gacha-operator", "干员寻访分析", "gacha", "/ef 抽卡", "operator"],
      ["gacha-weapon", "武器申领分析", "gacha", "/ef 抽卡", "weapon"],
      ["gacha-history", "抽卡历史记录", "gacha-history", "/ef 抽卡记录 1 2"],
      ["gacha-sync", "同步与导入回执", "receipts", "/ef 抽卡同步", "sync"],
    ],
  ],
  [
    "奖章与持有率",
    [
      ["medals", "蚀刻章统计", "medals", "/ef 奖章", "stats"],
      ["medals-missing", "缺章报告", "medals", "/ef 奖章 缺章", "missing"],
      ["ownership-group", "群内干员持有率", "ownership", "/ef 持有率", "group"],
      [
        "ownership-global",
        "全局干员持有率",
        "ownership",
        "/ef 持有率 全局",
        "global",
      ],
    ],
  ],
  [
    "影拓丰碑",
    [
      [
        "monument",
        "影拓丰碑 · 总览",
        "challenge",
        "/ef 影拓",
        "monument-overview",
      ],
      [
        "monument-detail",
        "影拓丰碑 · 详情",
        "challenge",
        "/ef 影拓 示例关卡",
        "monument-detail",
      ],
      [
        "monument-history",
        "影拓丰碑 · 历史",
        "challenge",
        "/ef 影拓 历史",
        "monument-history",
      ],
      ["challenge-empty", "挑战暂无记录", "empty", "/ef 影拓"],
    ],
  ],
  [
    "超域回响",
    [
      ["war", "超域回响 · 总览", "challenge", "/ef 回响", "war-overview"],
      [
        "war-detail",
        "超域回响 · 详情",
        "challenge",
        "/ef 回响 示例周次",
        "war-detail",
      ],
      [
        "war-history",
        "超域回响 · 历史",
        "challenge",
        "/ef 回响 历史",
        "war-history",
      ],
      ["contract", "危机合约结算", "contract", "既有结算预览"],
    ],
  ],
  [
    "公开关卡",
    [
      ["stages", "副本目录", "stage", "/ef 副本", "catalog"],
      ["stage-detail", "关卡资料", "stage", "/ef 副本 示例关卡", "detail"],
      [
        "stage-compare",
        "关卡变体总览",
        "stage",
        "/ef 副本 示例关卡 总览",
        "compare",
      ],
      [
        "legacy-challenge",
        "旧版挑战预览",
        "challenge",
        "既有挑战预览",
        "legacy-detail",
      ],
    ],
  ],
  [
    "日历与系统状态",
    [
      ["calendar", "版本日历", "calendar", "/ef 版本日历", "data"],
      [
        "calendar-official",
        "官方版本日历",
        "calendar",
        "/ef 版本日历",
        "official",
      ],
      [
        "status",
        "异常与边界状态",
        "receipts",
        "现有文本流程视觉提案",
        "errors",
      ],
      [
        "maintenance",
        "数据源与维护回执",
        "receipts",
        "现有文本流程视觉提案",
        "maintenance",
      ],
    ],
  ],
];
export const pages = definitions.flatMap(([group, rows]) =>
  rows.map(([id, title, kind, command, variant = ""]) => ({
    id,
    title,
    kind,
    command,
    variant,
    group,
  })),
);
export const groups = definitions.map(([name]) => name);

export const helpGroups = [
  [
    "01",
    "账号管理",
    [
      ["/ef 绑定 / 添加账号", "仅私聊 · 多账号追加"],
      ["/ef 账号 [编号]", "干员与配装总览"],
      ["/ef 主账号 / 解绑 <编号>", "仅私聊 · 账号管理"],
    ],
  ],
  [
    "02",
    "图鉴与配装",
    [
      ["/ef <关键词>", "干员 / 武器 / 装备 / 关卡"],
      ["/ef 装备 主力量 副敏捷", "按属性筛选"],
      ["/ef 配装 <干员>", "武器 / 潜能 / 技能 / 锻造"],
    ],
  ],
  [
    "03",
    "签到与寻访",
    [
      ["/ef 签到 [账号/全部]", "签到结果与奖励"],
      ["/ef 抽卡 [账号]", "同步与分析"],
      ["/ef 抽卡记录 [账号] [页码]", "可指定卡池"],
    ],
  ],
  [
    "04",
    "挑战与资料",
    [
      ["/ef 影拓 / 回响", "个人挑战总览"],
      ["/ef 影拓 / 回响 历史", "历史快照与分页"],
      ["/ef 副本 [关卡] [变体|总览]", "公开关卡资料"],
    ],
  ],
  [
    "05",
    "资源统计",
    [
      ["/ef 养成统计 [账号]", "当前档案投入"],
      ["/ef 流水 [账号] [-d N]", "资源余额与原因汇总"],
      ["/ef 奖章 / 奖章 缺章", "蚀刻章统计与差异"],
    ],
  ],
  [
    "06",
    "其他查询",
    [
      ["/ef 版本日历", "活动与寻访日程"],
      ["/ef 持有率 [全局]", "有效样本 · 干员潜能分布"],
      ["/ef 速算 <等级><效果> <强度>", "腐蚀 / 导电 / 碎甲"],
    ],
  ],
];

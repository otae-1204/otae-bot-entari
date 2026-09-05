import { operators, weapons, slots, gearArt, helpGroups } from "./data.js";
import {
  esc,
  art,
  native,
  icon,
  tag,
  note,
  section,
  metric,
  metrics,
  table,
  team,
  gear,
  gearRow,
  identity,
  bar,
  statList,
} from "./components.js";

function operatorPage() {
  const skills = ["普攻", "战技", "连携技", "终结技"];
  return `<div class="operator-layout"><div class="operator-hero"><div class="hero-word">LAEVATAIN</div>${art("laevatain-art", "hero-art", "莱万汀游戏立绘")}<div class="hero-name"><span>OPERATOR / 016</span><h2>莱万汀</h2><div>${tag("突击")}${tag("灼热", "warm")}${tag("6 ★")}</div></div><div class="hero-caption">莱万汀 / LAEVATAIN<br>角色立绘来自游戏公开素材</div></div><div class="operator-data"><div class="level-block"><span>OPERATOR LEVEL</span><b>90<small> / 90</small></b></div>${statList(
    [
      ["生命", "12,480"],
      ["攻击", "1,032"],
      ["防御", "560"],
      ["力量 / 敏捷", "145 / 112"],
      ["智识 / 意志", "168 / 138"],
    ],
  )}${section("天赋", `<div class="text-row"><b>天赋效果Ⅰ</b><p>此处展示天赋描述、触发条件与效果参数。保留原始技能文本。</p></div><div class="text-row"><b>天赋效果Ⅱ</b><p>长文本自动换行，不用省略号隐藏关键信息。</p></div>`, "示例字段")}</div><div class="operator-skills">${section("技能档案", skills.map((s, i) => `<div class="skill-row"><div class="skill-icon">${icon("profile_profession_" + [0, 8, 7, 5][i])}</div><div><span class="eyebrow">0${i + 1} / ${s}</span><h3>${s}效果示例</h3><p>显示技能描述、伤害类型、附加效果和触发条件。这里仅演示信息排版，不作为真实技能资料。</p></div><b class="skill-level">10</b></div>`).join(""), "SKILL / LEVEL 10")}${table(
    ["参数", "Lv.7", "Lv.8", "Lv.9", "Lv.10"],
    [
      ["倍率", "—", "—", "—", "—"],
      ["消耗", "—", "—", "—", "—"],
      ["持续", "—", "—", "—", "—"],
    ],
    "skill-table",
  )}</div></div><div class="potential-strip"><div><span>POTENTIAL</span><h2>潜能 <b>0</b></h2></div>${Array.from({ length: 6 }, (_, i) => `<div class="potential-item">${native("potential_" + i, "", `${i}潜`)}<span>${i === 0 ? "基础态" : `${i} 潜`}</span></div>`).join("")}</div>`;
}

function catalogPage(p) {
  if (p.variant === "operator")
    return `${metrics([
      ["图鉴条目", "08", "当前示例页"],
      ["筛选条件", "全部", "职业 / 属性 / 稀有度"],
      ["分页", "01 / 03", "沿用现有分页规则"],
    ])}<div class="operator-catalog">${operators.map((o, i) => `<div class="operator-tile" style="--operator-color:${o.color}"><span class="tile-no">${String(i + 1).padStart(2, "0")}</span>${art(o.id, "", o.name)}<div class="operator-tile-copy"><small>${o.en}</small><h3>${o.name}</h3><span>${o.job} · ${o.element}</span><b>${"◆".repeat(o.rarity)}</b></div></div>`).join("")}</div>`;
  if (p.variant === "weapon")
    return `${note("分类：单手剑 / 双手剑 / 手铳 / 长柄 / 施术单元 · 01 / 02")}<div class="weapon-catalog">${Array.from({ length: 8 }, (_, i) => `<div class="weapon-tile"><div class="tile-no">0${i + 1} / WEAPON</div>${art("weapon-" + (i % 4), "", weapons[i % 4])}<div><h3>${weapons[i % 4]}</h3><span>六星 · 武器资料</span><b>◆ ◆ ◆ ◆ ◆ ◆</b></div></div>`).join("")}</div>`;
  if (p.variant === "filter")
    return `${note("查询条件：主力量 / 副敏捷 · 找到 6 个示例结果")}${table(
      ["装备", "部位", "等级", "主属性", "副属性", "附加效果"],
      Array.from({ length: 6 }, (_, i) => [
        `<div class="item-label">${art(gearArt[i % 4])}<b>示例装备 ${String.fromCharCode(65 + i)}</b></div>`,
        slots[i % 4],
        "70",
        "力量 +130",
        "敏捷 +96",
        "效果参数按原资料显示",
      ]),
    )}`;
  return `<div class="set-catalog">${["示例套装 A", "示例套装 B", "示例套装 C"].map((name, i) => `<section class="set-row"><div class="set-label"><small>EQUIPMENT SET / 0${i + 1}</small><h2>${name}</h2><strong>70<small> LV.</small></strong></div><div class="gear-row">${[0, 1, 2, 3].map((j) => gear(j)).join("")}</div><div class="set-effect"><span>套装效果 / 3 件</span><p>展示套装触发条件、效果与限制。实际数值来自原始资料。</p></div></section>`).join("")}</div>`;
}

function itemPage(p) {
  const weapon = p.variant === "weapon";
  return `<div class="item-detail"><div class="item-stage"><span class="eyebrow">${weapon ? "WEAPON / SINGLE-HANDED SWORD" : "EQUIPMENT / BODY"}</span><h2>${weapon ? "熔铸火焰" : "示例护甲"}</h2><div class="engineering-ring"></div>${art(weapon ? "weapon-0" : "gear-body", "item-large", weapon ? "熔铸火焰" : "护甲")}<div class="item-rarity">◆ ◆ ◆ ◆ ◆ ◆</div></div><div class="item-description"><div class="level-block"><span>${weapon ? "WEAPON" : "EQUIPMENT"} LEVEL</span><b>${weapon ? "90" : "70"}</b></div>${statList(
    weapon
      ? [
          ["基础攻击", "510"],
          ["类型", "单手剑"],
          ["稀有度", "六星"],
        ]
      : [
          ["防御", "+140"],
          ["主属性 · 力量", "+130"],
          ["副属性 · 敏捷", "+96"],
        ],
  )}${section(weapon ? "武器技能" : "附加效果", [1, 2, 3].map((i) => `<div class="text-row"><span class="number-label">0${i}</span><div><h3>${weapon ? "武器效果" : "装备效果"} ${i}</h3><p>参数、触发条件与持续时间完整展示，详细文案沿用原数据。本页仅展示代码排版。</p></div></div>`).join(""))}</div></div>${
    weapon
      ? section(
          "精炼等级对比",
          table(
            ["精炼", "1", "2", "3", "4", "5"],
            [
              ["属性Ⅰ", "—", "—", "—", "—", "—"],
              ["属性Ⅱ", "—", "—", "—", "—", "—"],
              ["效果参数", "—", "—", "—", "—", "—"],
            ],
          ),
        )
      : section(
          "所属套装",
          `<div class="set-summary"><h3>示例套装 A</h3><b>3 件效果</b><p>保留套装说明、装备部位与锻造参数，不增加配方或掉落功能。</p></div>`,
        )
  }`;
}

function setPage() {
  return `${metrics([
    ["套装", "示例套装 A"],
    ["穿戴等级", "70"],
    ["套装件数", "3", "效果生效要求"],
  ])}<div class="set-showcase">${[0, 1, 2, 3].map((i) => gear(i)).join("")}</div>${section("套装效果", `<div class="callout"><b>03</b><div><h3>套装效果说明</h3><p>完整显示触发条件、属性收益和持续时间。此处为设计占位文案，不代表真实游戏数值。</p></div></div>`)}${table(
    ["部位", "名称", "主属性", "副属性", "锻造"],
    slots
      .slice(0, 4)
      .map((s, i) => [s, `示例装备 ${i + 1}`, "力量 +130", "敏捷 +96", "Ⅰ"]),
  )}`;
}

function loadoutPage() {
  return `<div class="loadout-layout"><div class="loadout-portrait">${art("laevatain-art", "", "莱万汀")}<div><small>LOADOUT / 01</small><h2>莱万汀</h2>${tag("突击")}${tag("灼热", "warm")}<strong>LV.90</strong></div></div><div class="loadout-content"><div class="equipped-weapon">${art("weapon-0", "", "熔铸火焰")}<div><small>EQUIPPED WEAPON</small><h2>熔铸火焰</h2><p>LV.90 / 精炼 1</p></div><b>510<small> 基础攻击</small></b></div>${section("装备配置", gearRow(), "护甲 / 护手 / 双配件 / 道具")}${metrics(
    [
      ["潜能", "0"],
      ["技能等级", "10"],
      ["锻造", "Ⅰ"],
    ],
  )}${section("套装效果", note("示例套装 A · 3 件效果 / 完整保留触发条件与效果参数"))}${statList(
    [
      ["攻击 / 防御", "1,032 / 560"],
      ["力量 / 敏捷", "145 / 112"],
      ["智识 / 意志", "168 / 138"],
    ],
  )}</div></div>`;
}

function profilePage() {
  return `<div class="profile-scene"><div class="profile-left">${identity()}<div class="profile-rank"><small>AUTHORITY LEVEL</small><b>60</b><span>终末地工业 / 管理员</span></div>${section("当前任务", `<h3>主线任务示例</h3><p>向下一个目的地进发。</p>`)}${metrics(
    [
      ["干员收集", "24"],
      ["武器收集", "38"],
      ["文档收集", "126"],
    ],
  )}<div class="region-levels"><span>四号谷地 <b>LV.12</b></span><span>武陵 <b>LV.10</b></span></div></div><div class="profile-right">${section("展示蚀刻章", `<div class="medal-cluster">${Array.from({ length: 5 }, (_, i) => art("medal-" + ((i % 3) + 1), "medal", "展示奖章")).join("")}</div>`)}${section("展示干员", team(0, true), "SHOWCASE / 04")}<div class="profile-motto"><span>STATUS</span><p>一切运行正常。期待下一次开拓。</p></div></div></div>`;
}

function rosterPage(p) {
  return `${identity()}${
    p.variant === "first"
      ? metrics([
          ["干员", "24"],
          ["已装备武器", "18"],
          ["档案更新", "12:00"],
          ["页码", "01 / 03"],
        ])
      : note("配装档案续页 · 02 / 03 · 不重复首页汇总")
  }<div class="roster-list">${operators
    .slice(p.variant === "first" ? 0 : 3, p.variant === "first" ? 3 : 7)
    .map(
      (o, i) =>
        `<div class="roster-row"><div class="roster-operator">${art(o.id, "", o.name)}<div><h3>${o.name}</h3><small>${o.job} / ${o.element}</small><strong>LV.90</strong><span>生命 12,480 / 攻击 1,032</span></div></div><div class="roster-weapon">${art("weapon-" + (i % 4))}<span>${weapons[i % 4]}</span><small>LV.90 / 精炼 1</small></div>${gearRow()}<div class="roster-skills"><span>技能</span><b>10 / 10<br>10 / 10</b><small>潜能 ${i % 6}</small></div></div>`,
    )
    .join("")}</div>`;
}

function basePage() {
  return `${identity()}${section(
    "据点存票",
    `<div class="settlements">${["四号谷地", "武陵"]
      .map(
        (name, i) =>
          `<div class="settlement"><div class="section-title"><h3>${name} / 示例据点</h3>${tag(i ? "待采样" : "实测 · 中可信")}</div><div class="settlement-content">${art(operators[i].id, "avatar")}<div><small>当前存票 / 上限</small><b>23,500 <small>/ 30,000</small></b>${bar(78.3)}</div></div>${statList(
            [
              ["等级", "LV.7"],
              ["增长速度", i ? "待采样" : "+1,200 / 小时"],
              ["预计满仓", i ? "等待采样" : "约 5小时25分"],
            ],
          )}</div>`,
      )
      .join("")}</div>`,
    "增长速度来自历史快照",
  )}${section("帝江号心情", `<div class="rooms">${["制造室", "会客室", "休息舱"].map((room, j) => `<div class="room"><h3>${room} <small>LV.3</small></h3>${[0, 1].map((_, i) => `<div class="room-operator">${art(operators[i + j * 2].id, "avatar")}<div><b>${operators[i + j * 2].name}</b><strong>心情 ${88 - j * 20}%</strong>${bar(88 - j * 20, j === 2 ? "warning" : "")}<p>心情技能示例</p><small>工作 44小时 · 回满 6小时 · 消耗 -2.0%/h</small></div></div>`).join("")}</div>`).join("")}</div>`)}${note("心情时间为连续工作 / 休息理论值；增长速度仅使用同等级、同上限、同派驻配置的历史有效快照。")}`;
}

function investmentPage(p) {
  if (p.variant === "detail")
    return `${identity()}${section("材料明细", `<div class="material-groups">${["可折算理智材料", "非理智 / 稀有材料"].map((name, j) => `<div><h3>${name}</h3><div class="material-tiles">${[0, 1, 2, 3].map((i) => `<div>${art(gearArt[i])}<b>材料示例 ${j * 4 + i + 1}</b><span>× ${(1200 - i * 210).toLocaleString()}</span></div>`).join("")}</div></div>`).join("")}</div>`)}${section(
      "干员投入排行",
      table(
        ["干员", "本体", "技能", "武器", "合计"],
        operators
          .slice(0, 5)
          .map((o, i) => [
            `<div class="item-label">${art(o.id)}<b>${o.name}</b></div>`,
            (1600 - i * 100).toLocaleString(),
            (900 - i * 80).toLocaleString(),
            (600 - i * 50).toLocaleString(),
            `<b>${(3100 - i * 230).toLocaleString()}</b>`,
          ]),
      ),
      "按可折算理智排序",
    )}${note("非理智材料保留原始数量；经验显示经验值，不还原实际经验卡组合。")}`;
  return `${identity()}${metrics([
    ["折金票", "4,820,000", "等级、技能与突破"],
    ["干员经验", "12,450,000", "按经验值统计"],
    ["武器经验", "6,280,000", "按经验值统计"],
    ["可折算理智", "18,240", "理论最低等价"],
    ["统计对象", "24 / 18", "干员 / 已装备武器"],
    ["数据覆盖", "96%", "静态成本表映射"],
  ])}${section("五类投入", `<div class="investment-bars">${["干员等级", "干员突破", "技能升级", "武器等级", "武器突破"].map((s, i) => `<div><span>0${i + 1}</span><h3>${s}</h3>${bar([36, 18, 24, 14, 8][i])}<b>${[36, 18, 24, 14, 8][i]}%</b></div>`).join("")}</div>`, "以可折算理智归一")}${section("统计口径", note("仅统计当前档案可见对象；不含潜能、武器精炼、装备价值、未装备武器与历史替换投入。"))}`;
}

function currencyPage() {
  return `${identity()}${note("示例区间 08.30 — 09.05 / 获取与消耗 / 按原因汇总，不伪造逐日趋势")}<div class="currency-columns">${[
    "源石",
    "嵌晶玉",
    "武库配额",
  ]
    .map(
      (name, i) =>
        `<div class="currency-column"><h2>${name}</h2>${metric("期末余额", [32450, 2180, 1860][i].toLocaleString())}${statList(
          [
            ["期初余额", [26180, 1420, 1220][i].toLocaleString()],
            ["获取", "+" + [18920, 1360, 1350][i].toLocaleString()],
            ["消耗", "−" + [12650, 600, 710][i].toLocaleString()],
            ["净变化", "+" + [6270, 760, 640][i].toLocaleString()],
          ],
        )}${section(
          "获取原因",
          table(
            ["类型", "次数", "合计"],
            [
              ["活动奖励", "4", "+1,000"],
              ["任务奖励", "7", "+360"],
            ],
          ),
        )}${section(
          "消耗原因",
          table(
            ["类型", "次数", "合计"],
            [
              ["资源兑换", "2", "−400"],
              ["其他消耗", "1", "−200"],
            ],
          ),
        )}</div>`,
    )
    .join(
      "",
    )}</div>${note("原因明细展示代表性条目，汇总为独立示例数据。正式数值由原接口和统计逻辑产生。")}`;
}

function gachaPage(p) {
  const weapon = p.variant === "weapon";
  return `${identity()}${metrics([
    [
      "记录总数",
      weapon ? "240" : "480",
      weapon ? "付费 240 / 免费 0" : "付费 460 / 免费 20",
    ],
    ["六星记录", weapon ? "8" : "12"],
    ["逐抽明细", weapon ? "240" : "480", "统计补齐单独标注"],
    ["同步状态", "正常", "示例 · 12:00"],
  ])}<div class="gacha-pools">${[0, 1]
    .map(
      (_, j) =>
        `<section class="pool"><div class="pool-head"><span>${j ? "ARCHIVE" : "CURRENT"}</span><h2>${j ? "历史卡池" : "当前卡池"}</h2><b>${j ? 220 : 260}<small> 次记录</small></b></div>${metrics(
          [
            ["当前垫抽", "24"],
            ["距小保底", "—", "沿用原计算结果"],
            ["距大保底", "—", "沿用原计算结果"],
          ],
        )}<div class="pull-timeline">${[52, 28, 64, 36].map((n, i) => `<div class="pull-event">${art(weapon ? "weapon-" + i : operators[i + j].id)}<div><b>${weapon ? weapons[i] : operators[i + j].name}</b><small>08.${String(30 - i * 2).padStart(2, "0")} / 示例记录</small></div><div>${bar(n)}<strong>${n} 抽</strong></div>${tag(["小保底", "歪", "大保底", "六星"][i])}</div>`).join("")}</div><div class="free-pulls">${tag("免费十连")}<span>${weapon ? "本页暂无免费记录" : "免费 20 抽 · 单独列出，不计入保底"}</span></div><div class="expectation"><span>六星期望：—</span><span>实际记录：${weapon ? "4" : "6"}</span></div></section>`,
    )
    .join(
      "",
    )}</div>${note("官方接口近 90 天记录 · 本地同步累积保留 · 历史补齐与官方逐抽明细分别标注；卡池过滤与保底仍以现有逻辑为准。")}`;
}

function gachaHistoryPage() {
  return `${identity()}${note("全部卡池 / 第 02 页 / 每页 20 条 · 本设计页展示 10 条排版示例")}${table(
    ["时间", "卡池", "结果", "星级", "距上次六星"],
    Array.from({ length: 10 }, (_, i) => [
      `09.0${5 - Math.floor(i / 3)} 12:${String(i * 5).padStart(2, "0")}`,
      "示例寻访池",
      `<div class="item-label">${art(operators[i % 8].id)}<b>${operators[i % 8].name}</b></div>`,
      tag(
        `${operators[i % 8].rarity} ★`,
        operators[i % 8].rarity === 6 ? "accent" : "",
      ),
      String(i * 7 + 1),
    ]),
  )}<div class="pagination-label">PAGE <b>02</b> / 08 <span>超限自动分页，保留卡池筛选</span></div>`;
}

function medalsPage(p) {
  return p.variant === "missing"
    ? `${identity()}${metrics([
        ["未获得", "12"],
        ["未满级", "06"],
        ["未镀层", "03"],
      ])}${["未获得", "未满级", "未镀层"]
        .map((name, j) =>
          section(
            name,
            table(
              ["蚀刻章", "当前进度", "条件说明"],
              [0, 1].map((_, i) => [
                `<div class="item-label">${art("medal-2")}<b>蚀刻章示例 ${j * 2 + i + 1}</b></div>`,
                j ? "02 / 03" : "未获得",
                "完整保留原始获取或升级条件；此处为占位文本。",
              ]),
            ),
          ),
        )
        .join("")}`
    : `${metrics([
        ["蚀刻章总数", "128"],
        ["基础章", "72"],
        ["进阶章", "42"],
        ["典藏章", "14"],
      ])}<div class="medal-exhibition">${["基础蚀刻章", "进阶蚀刻章", "典藏蚀刻章"].map((n, i) => `<div>${art("medal-" + (i + 1), "", n)}<span>0${i + 1} / MEDAL ARCHIVE</span><h2>${n}</h2></div>`).join("")}</div>${section("版本变化", `<div class="version-diff"><div><span>版本 A</span><b>120</b></div><span>→</span><div><span>版本 B</span><b>128</b></div><div class="diff-increase"><span>本次新增</span><b>+08</b></div></div>`)}${note("版本名称与数量均为示例；真实统计保留当前与基线快照差异、等级和新增项。")}`;
}

function ownershipPage(p) {
  const total = p.variant === "global" ? 2048 : 128;
  return `${tag(p.variant === "global" ? "全局统计" : "当前群")}${metrics([
    ["总计有效样本", total.toLocaleString(), `合格 ${total + 8} · 排除 8`],
    ["国服", Math.floor(total * 0.75).toLocaleString(), "有效样本"],
    ["亚服", (total - Math.floor(total * 0.75)).toLocaleString(), "有效样本"],
  ])}<div class="ownership-overview">${[
    ["六星收集率", "73.4%", 73],
    ["非常驻六星收集率", "42.1%", 42],
    ["六星满潜率", "12.0%", 12],
    ["非常驻六星满潜率", "8.0%", 8],
  ]
    .map(
      ([name, val, percent]) =>
        `<div><div class="donut" style="--portion:${percent}%"><b>${val}</b></div><span>${name}</span></div>`,
    )
    .join("")}</div>${section(
    "干员持有与潜能分布",
    `<div class="potential-legend">${["未持有", "0潜", "1潜", "2潜", "3潜", "4潜", "5潜", "未知"].map((x, i) => `<span><i class="bucket-${i}"></i>${x}</span>`).join("")}</div>${operators
      .slice(0, 5)
      .map((o, i) => {
        const ratio = 80 - i * 9;
        const count = Math.floor((total * ratio) / 100);
        const rate = ((count / total) * 100).toFixed(1);
        return `<div class="ownership-row">${art(o.id, "avatar")}<h3>${o.name}</h3><div><b>${count} / ${total}</b><small>持有 / 有效样本</small></div><strong>${rate}%</strong><div class="stacked-bar">${[100 - Number(rate), Number(rate) * 0.48, Number(rate) * 0.2, Number(rate) * 0.1, Number(rate) * 0.07, Number(rate) * 0.06, Number(rate) * 0.05, Number(rate) * 0.04].map((v, j) => `<i class="bucket-${j}" style="width:${v}%" title="${v.toFixed(1)}%"></i>`).join("")}</div></div>`;
      })
      .join("")}`,
    "仅统计干员，不包含武器",
  )}${note("仅有效样本参与分母；未知潜能不当作零潜；收集概况与潜能分布使用各自口径。基于已收集样本，非全服普查。")}`;
}

function challengePage(p) {
  const war = p.variant.startsWith("war");
  const detail = p.variant.endsWith("detail");
  const history = p.variant.endsWith("history");
  const title = war ? "超域回响" : "影拓丰碑";
  const modes = war ? ["普通", "困难", "残酷"] : ["普通", "困难"];
  const modeClass = (i) =>
    war ? ["normal", "hard", "cruel"][i] : ["normal", "cruel"][i];
  const record = (i, offset = 0) =>
    `<div class="record ${modeClass(i)}"><div><span>${modes[i]}</span><small>${i === 2 ? "未通关" : "LV.90 / 历史快照"}</small></div><b>${i === 2 ? "—" : ["02:18.40", "03:42.18"][i]}</b>${i === 2 ? '<div class="no-team">暂无通关队伍</div>' : team(offset + i, false)}<small>${i === 2 ? "无记录" : "2026.09.03 12:00"}</small></div>`;
  if (detail)
    return `${identity()}${note(`${title} / 示例关卡 · ${war ? "最高已通关：困难 / 03:42.18" : "普通与困难分别展示"}`)}<div class="challenge-detail-layout"><div>${section("通关记录", modes.map((_, i) => record(i)).join(""), "不以当前配装替换历史快照")}</div><div>${section("关卡机制", `<div class="mechanics"><h3>环境效果示例</h3><p>这里完整保留关卡机制、触发条件、持续时间与限制，不用背景图代替文字信息。</p>${tag(war ? "困难专属" : "困难专属", "danger")}<h3>难度专属机制</h3><p>额外条件与难度绑定，不混合各难度描述。</p></div>`)}${section("敌方信息", `<div class="enemies">${[1, 2, 3].map((i) => `<div>${native("profile_profession_" + [2, 8, 5][i - 1])}<span>敌方单位示例 ${i}</span><small>属性 / 抗性 / 机制</small></div>`).join("")}</div>`)}</div></div>`;
  if (history)
    return `${identity()}${[0, 1]
      .map((_, j) =>
        section(
          `${war ? "赛季" : "主题"}档案 ${String.fromCharCode(65 + j)}`,
          `<div class="history-group"><div class="archive-poster"><span>${war ? "WAR ECHOES" : "MONUMENT"}</span><b>0${j + 1}</b><h3>历史${war ? "赛季" : "主题"}示例</h3>${tag("历史快照")}</div><div class="history-rows">${table(
            war
              ? ["周次", "最高通关", "星数", "时间", "队伍"]
              : ["关卡", "普通", "困难", "历史队伍"],
            Array.from({ length: war ? 4 : 3 }, (_, i) =>
              war
                ? [
                    `第 0${i + 1} 周`,
                    tag(i === 3 ? "无记录" : "困难", i === 3 ? "" : "steel"),
                    i === 3 ? "—" : "9 / 9",
                    i === 3 ? "—" : "03:42.18",
                    i === 3 ? "暂无记录" : team(i + j, false),
                  ]
                : [
                    `示例关卡 ${i + 1}`,
                    `02:${18 + i}.40`,
                    i === 2 ? "未通关" : "03:42.18",
                    team(i + j, false),
                  ],
            ),
          )}</div></div>`,
          "01 / 03",
        ),
      )
      .join(
        "",
      )}${note("按主题 / 赛季完整分组；回响保留赛季内全部周次，超长自动分页。")}`;
  return `${identity()}<div class="challenge-overview-layout"><div class="challenge-poster"><small>${war ? "WAR ECHOES" : "MONUMENT"} / ARCHIVE</small><div><span>示例${war ? "赛季" : "主题"}</span><h2>${war ? "超域回响" : "影拓丰碑"}</h2><b>${war ? "24" : "12"}<small> / ${war ? "36" : "20"}</small></b><p>${war ? "额外任务 6 / 9" : "普通 8 / 10 · 困难 4 / 10"}</p>${bar(60)}</div></div><div>${section(war ? "周次挑战" : "关卡记录", Array.from({ length: war ? 3 : 4 }, (_, i) => `<div class="challenge-summary-row"><div><span>0${i + 1}</span><h3>${war ? "第 " + (i + 1) + " 周" : "示例关卡 " + (i + 1)}</h3></div><div>${tag(war ? "最高通关 · 困难" : "普通", "steel")}<b>02:18.40</b>${team(i, false)}</div><div>${tag(war ? "荣誉 · 3 项" : "困难", "danger")}<b>${i === 3 ? "未通关" : "03:42.18"}</b>${i === 3 ? "<span>—</span>" : team(i + 1, false)}</div></div>`).join(""))}</div></div><div class="archive-strip"><b>历史档案</b><span>各主题 / 赛季独立保存</span><code>${war ? "/ef 回响 历史" : "/ef 影拓 历史"}</code></div>`;
}

function contractPage() {
  return `<div class="contract-banner">${native("contract_logo")}<div><small>CRISIS CONTRACT / OPERATION RESULT</small><h2>危机合约 · 行动完成</h2><span>已有结算预览 / 示例记录</span></div></div><div class="contract-layout"><div><div class="contract-score"><span>行动得分</span><b>620</b><strong>S <small>行动评级</small></strong></div>${section(
    "达成条件",
    table(
      ["条件", "状态"],
      Array.from({ length: 6 }, (_, i) => [
        `示例条件 0${i + 1}`,
        tag("已达成"),
      ]),
    ),
  )}<div class="contract-time"><span>行动用时</span><b>04:28.16</b></div></div><div class="contract-team">${operators
    .slice(0, 4)
    .map(
      (o, i) =>
        `<div>${art(o.id, "contract-portrait", o.name)}<h3>${o.name}</h3><span>LV.90 / 潜能 ${i}</span><div class="contract-weapon">${art("weapon-" + i)}</div><small>历史装备</small><div class="contract-gear">${[0, 1, 2, 3].map((j) => art(gearArt[j])).join("")}</div></div>`,
    )
    .join(
      "",
    )}</div></div>${note("队伍、武器与装备均为对应历史记录；不展示重试、分享、延迟等游戏交互控件。")}`;
}

function stagePage(p) {
  if (p.variant === "catalog")
    return `${note("公开关卡资料 / 不读取个人账号 / 空参数展示目录")}<div class="stage-catalog">${["资源关卡", "作战挑战", "周常关卡"].map((n, i) => `<div><div class="stage-tile-art"><b>0${i + 1}</b><span>OPERATION / PUBLIC FILE</span></div><div><small>STAGE CATALOG</small><h2>${n}</h2>${["A", "B"].map((s, j) => `<div class="text-row"><h3>示例关卡 ${s}</h3><span>LV.${30 + i * 20} · ${3 - j} 个变体</span></div>`).join("")}</div></div>`).join("")}</div>`;
  if (p.variant === "compare")
    return `${note("示例关卡 / 全部变体对比 · 未知数据保持空值，不编造消耗或奖励")}${table(
      ["对比字段", "变体Ⅰ", "变体Ⅱ", "变体Ⅲ"],
      [
        ["推荐等级", "30", "50", "70"],
        ["消耗", "—", "—", "—"],
        ["关卡机制", "基础条件说明", "增加难度条件", "展示完整机制文本"],
        [
          "敌方信息",
          icon("profile_profession_2"),
          icon("profile_profession_8"),
          icon("profile_profession_5"),
        ],
        [
          "奖励",
          art("gear-accessory", "table-art"),
          art("gear-accessory2", "table-art"),
          art("gear-hand", "table-art"),
        ],
      ],
      "comparison-table",
    )}`;
  return `<div class="stage-detail-layout"><div class="stage-hero"><span>PUBLIC OPERATION FILE</span><h2>示例关卡 A</h2><b>Ⅲ</b>${tag("变体Ⅲ / LV.70")}</div><div>${section("关卡机制", `<h3>示例机制标题</h3><p>公开资料中的机制、关卡描述与作战条件保持独立区块，长文本在内容区自然流动。</p>`)}${section(
    "作战条件",
    statList([
      ["推荐等级", "70"],
      ["消耗", "—"],
      ["关卡类型", "公开副本"],
    ]),
  )}${section(
    "敌方信息",
    table(
      ["敌方单位", "属性", "抗性"],
      [
        ["示例敌方 A", "物理", "—"],
        ["示例敌方 B", "灼热", "—"],
        ["示例敌方 C", "自然", "—"],
      ],
    ),
  )}</div></div>${section("关卡奖励", `<div class="reward-strip">${gearArt.map((x, i) => `<div>${art(x)}<b>示例奖励 ${i + 1}</b><span>× —</span></div>`).join("")}</div>`)}${note("来源标记保留 AKEData / FZ；示例不代表真实关卡掉落。")}`;
}

function calendarPage(p) {
  const official = p.variant === "official";
  const items = [
    ["限时活动 A", "活动", 1, 14],
    ["干员寻访 A", "寻访", 1, 13],
    ["示例挑战 A", "挑战", 5, 20],
    ["限时活动 B", "活动", 15, 28],
    ["武器申领 A", "申领", 13, 27],
    ["其他活动 A", "其他", 5, 19],
  ];
  return `${note(`${official ? "官方内容整理" : "AKEData 日程"} / 示例版本 · 09.01 — 09.28 · 以下日期不代表真实运营日程`)}${
    official
      ? `<div class="calendar-editorial"><div class="official-poster"><span>ENDFIELD / EVENT ARCHIVE</span><h2>版本活动<br>示例展示</h2><b>09 / 2026</b></div><div>${section(
          "活动日程一览",
          table(
            ["活动", "时间区间", "状态"],
            items.map(([n, t, s, e]) => [
              `${tag(t)} ${n}`,
              `09.${String(s).padStart(2, "0")} — 09.${e}`,
              tag(s < 6 ? "进行中" : "即将开始", s < 6 ? "accent" : ""),
            ]),
          ),
        )}${note("以官方公告为准；活动插图仅使用已有游戏背景，不伪造新活动海报。")}</div></div>`
      : `<div class="calendar-timeline"><div class="calendar-scale"><span>活动 / 分类</span><div>${["09.01", "09.08", "09.15", "09.22", "09.28"].map((d) => `<b>${d}</b>`).join("")}</div></div>${items.map(([name, type, start, end], i) => `<div class="calendar-row"><div><small>0${i + 1} / ${type}</small><h3>${name}</h3></div><div class="calendar-track"><div class="event-bar ${start > 6 ? "future" : ""}" style="left:${((start - 1) / 28) * 100}%;width:${((end - start + 1) / 28) * 100}%"><b>${name}</b><span>09.${String(start).padStart(2, "0")} — 09.${end}</span></div></div></div>`).join("")}<div class="calendar-legend">${tag("进行中", "accent")}${tag("未来活动")}${tag("已结束", "dark")}<span>时间条依据起止日期绘制</span></div></div>`
  }`;
}

function helpPage() {
  return `<div class="help-lead"><div><span>ENDFIELD / COMMAND REFERENCE</span><b>/ef</b></div><div><h2>终末地功能说明</h2><p>从干员档案到每一次开拓记录。<br>命令不变，信息更清晰。</p>${tag("仅私聊绑定")}${tag("超限自动分页")}</div></div><div class="help-grid">${helpGroups.map(([id, title, rows]) => section(`${id} / ${title}`, rows.map(([command, desc]) => `<div class="command-row"><code>${esc(command)}</code><span>${desc}</span></div>`).join(""))).join("")}</div>${note("< > 为必填参数，[ ] 为选填参数。数据源参数和其他完整用法仍以现有帮助为准。")}`;
}

function searchPage() {
  return `<div class="search-lead"><span>SEARCH QUERY</span><h2>“火”</h2><b>04 <small>条匹配结果</small></b></div>${table(
    ["序号", "匹配结果", "类型", "查询提示"],
    [
      [
        "01",
        `<div class="item-label">${art("laevatain")}<b>莱万汀</b></div>`,
        "干员",
        "/ef 莱万汀",
      ],
      [
        "02",
        `<div class="item-label">${art("weapon-0")}<b>熔铸火焰</b></div>`,
        "武器",
        "/ef 熔铸火焰",
      ],
      [
        "03",
        `<div class="item-label">${art("gear-body")}<b>示例护甲</b></div>`,
        "装备",
        "使用完整名称",
      ],
      ["04", "示例关卡 A", "关卡", "/ef 副本 示例关卡 A"],
    ],
  )}${note("此处只演示搜索回执的视觉，不执行真实检索；使用完整名称消除歧义，保留类型与数据源提示。")}`;
}

function accountsPage() {
  return `${tag("仅私聊", "accent")}<div class="account-cards">${[0, 1].map((_, i) => `<div>${art(operators[i + 1].id, "avatar")}<span class="account-no">0${i + 1}</span><div><h2>示例账号 ${i ? "B" : "A"}</h2><p>UID 10****${i ? "61" : "28"} / 国服</p></div>${tag(i ? "附加账号" : "主账号", i ? "" : "accent")}</div>`).join("")}</div>${section(
    "账号指令",
    table(
      ["命令", "用途"],
      [
        ["/ef 添加账号", "追加账号，限私聊"],
        ["/ef 主账号 2", "切换主账号"],
        ["/ef 解绑 2", "解绑指定账号"],
      ],
    ),
  )}${note("本预览不收集手机号、验证码、Token 或任何登录凭据，也不会进行绑定、解绑或切换操作。")}`;
}

function calculatorPage() {
  return `<div class="calculator-hero"><b>02</b><div><span>STATUS EFFECT / CORROSION</span><h2>腐蚀</h2>${tag("技艺强度 200", "accent")}</div>${native("profile_property_natural")}</div>${table(
    ["输出项目", "结果"],
    [
      ["最终效果", "由现有计算函数返回"],
      ["效果构成", "基础效果 + 强度增益"],
      ["持续时间", "由现有计算函数返回"],
    ],
  )}${note("等级 1–4；支持腐蚀 / 导电 / 碎甲。本预览不重新实现公式，避免产生与正式插件不一致的数值。")}`;
}

function attendancePage() {
  return `<div class="attendance-success"><div class="success-mark">✓</div><div><small>DAILY CHECK-IN / COMPLETED</small><h2>签到成功</h2><p>示例账号 A · 今日签到奖励</p></div></div><div class="reward-strip">${gearArt
    .slice(0, 3)
    .map(
      (x, i) =>
        `<div>${art(x)}<b>奖励示例 ${i + 1}</b><span>× ${[100, 20, 5][i]}</span></div>`,
    )
    .join("")}</div>${section(
    "多账号回执",
    table(
      ["账号", "状态", "说明"],
      [
        ["示例账号 A", tag("成功", "accent"), "奖励已展示"],
        ["示例账号 B", tag("今日已签到"), "不重复领取"],
      ],
    ),
  )}${note("仅演示签到卡片与多账号状态，不调用签到接口。")}`;
}

function receiptsPage(p) {
  let rows;
  if (p.variant === "sync")
    rows = [
      [
        "同步完成",
        "新增 24 条 · 重复 0 条 · 本地记录已更新",
        "/ef 抽卡同步",
        "success",
      ],
      [
        "导入完成",
        "有效记录 240 条 · 已合并入本地记录 · 仅私聊",
        "/ef 抽卡导入",
        "success",
      ],
    ];
  else if (p.variant === "maintenance")
    rows = [
      [
        "当前数据源",
        "Warfarin / AKEData / FZ · 保留数据来源标记",
        "数据源仅用于展示",
        "info",
      ],
      [
        "缓存刷新完成",
        "处理结果与统计数量沿用现有回执",
        "仅管理员可操作",
        "success",
      ],
      ["别名已添加", "示例别名 → 示例目标", "现有别名管理回执", "success"],
    ];
  else
    rows = [
      ["未绑定账号", "请先在私聊中绑定游戏账号", "/ef 绑定", "info"],
      [
        "数据展示未开启",
        "请检查森空岛名片的数据展示设置",
        "开启后重新查询",
        "warning",
      ],
      [
        "未找到匹配结果",
        "尝试完整名称，或指定其他数据源",
        "/ef 搜索 <关键词>",
        "warning",
      ],
      [
        "数据暂不可用",
        "请求未完成，已保留成功获取的数据",
        "请稍后重试",
        "error",
      ],
    ];
  return `<div class="receipt-list">${rows.map(([title, desc, hint, state]) => `<section class="receipt ${state}"><div class="receipt-icon">${{ success: "✓", warning: "!", info: "i", error: "×" }[state]}</div><div><small>SYSTEM RECEIPT / ${state.toUpperCase()}</small><h2>${title}</h2><p>${esc(desc)}</p></div><code>${esc(hint)}</code></section>`).join("")}</div>${note("现有文本流程的可选视觉提案；不增加真实按钮、管理中心、上传区或新功能。")}`;
}

function emptyPage() {
  return `${identity()}<div class="empty-state"><div class="empty-emblem">${native("dungeon_title")}</div><span>ARCHIVE / NO RECORD</span><h2>暂无可展示的挑战记录</h2><p>可能尚未参与，或未开启数据展示。<br>请检查森空岛名片设置后重试。</p>${tag("无记录不等于 0% 进度")}</div>`;
}

const renderers = {
  operator: operatorPage,
  catalog: catalogPage,
  item: itemPage,
  set: setPage,
  loadout: loadoutPage,
  profile: profilePage,
  roster: rosterPage,
  base: basePage,
  investment: investmentPage,
  currency: currencyPage,
  gacha: gachaPage,
  "gacha-history": gachaHistoryPage,
  medals: medalsPage,
  ownership: ownershipPage,
  challenge: challengePage,
  contract: contractPage,
  stage: stagePage,
  calendar: calendarPage,
  help: helpPage,
  search: searchPage,
  accounts: accountsPage,
  calculator: calculatorPage,
  attendance: attendancePage,
  receipts: receiptsPage,
  empty: emptyPage,
};
export const renderPage = (page) => {
  const renderer = renderers[page.kind];
  if (!renderer) throw new Error(`No renderer for ${page.kind}`);
  return renderer(page);
};

"""一次性脚本：把按日期聚合的更新日志重组为按版本号聚合。

日期 → 版本的划分写在这里；提交哈希全部从原 JSON 搬运，不手抄。

**只在 schema 1 上跑一次。** 当前数据已是 schema 2，本脚本会直接拒绝运行：
对已重组的数据重跑会把每条更新的 ``date`` 改成版本窗口的结束日，破坏数据。
新增版本请直接编辑 ``plugins/changelog/changelog.json``（见 docs/changelog_plugin.md）。
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "plugins/changelog/changelog.json"

# 版本定义（时间正序）：版本号、窗口起、窗口止、标题、一句话概括、标签
VERSIONS = [
    ("v1.0.0", "2026-06-17", "2026-06-17", "迁移到 Entari 框架",
     "项目从旧框架迁移到 Entari / Satori，后续所有功能都建立在这套底座上。", ["框架"]),
    ("v1.1.0", "2026-07-07", "2026-07-07", "终末地插件首发",
     "终末地插件上线，支持干员与武器卡片查询。", ["终末地"]),
    ("v1.2.0", "2026-07-10", "2026-07-10", "共享 I/O 与卡片渲染优化",
     "引入有界 HTTP 缓存与共享渲染线程池，整体响应更快。", ["框架"]),
    ("v1.3.0", "2026-07-20", "2026-07-21", "配装模拟器与资料查询",
     "新增终末地装备图鉴查询与配装模拟器，并修掉一批启动与刷新问题。", ["终末地", "Steam", "Minecraft"]),
    ("v1.4.0", "2026-07-23", "2026-07-24", "账号抽卡分析与搜索结果引用",
     "新增账号抽卡分析与状态查询，可用引用回复选定搜索结果。", ["终末地"]),
    ("v1.5.0", "2026-07-26", "2026-07-29", "奖章统计与账号详情总览",
     "新增蚀刻章/奖章统计模块与账号详情总览，装备支持按主副属性筛选。", ["终末地"]),
    ("v1.6.0", "2026-07-30", "2026-08-03", "奖章卡片改版与版本日历",
     "奖章卡片排版重做，新增官方版本日历与亚服账号绑定。", ["终末地"]),
    ("v1.7.0", "2026-08-04", "2026-08-13", "养成投入统计与多账号端点",
     "新增养成投入统计与货币流水汇总，支持同时连接多个 Satori 端点。", ["终末地", "框架"]),
    ("v1.8.0", "2026-08-19", "2026-08-22", "Tibo 雷达上线",
     "修复森空岛登录签名，新增战争回响查询与 Tibo 雷达插件。", ["终末地", "Tibo"]),
    ("v1.9.0", "2026-08-24", "2026-08-31", "订阅推送与持有率统计",
     "新增 Tibo 订阅推送、干员持有率统计与个人挑战报告。", ["Tibo", "终末地"]),
    ("v1.10.0", "2026-09-01", "2026-09-06", "架构重构与档案库",
     "拆分基础设施与插件业务域，新增档案库收集检查，并同步 1.5.3 别名。", ["框架", "终末地"]),
    ("v1.11.0", "2026-09-07", "2026-09-09", "Grok Bot 与群内插件开关",
     "接入 Grok Bot 与 HYW 搜索问答，新增按群生效的插件开关。", ["Grok", "HYW", "框架"]),
    ("v1.12.0", "2026-09-10", "2026-09-12", "日常仪表盘与自动重试",
     "新增 /ef 日常 仪表盘与档案库，问答加入 429 自动退避重试。", ["终末地", "HYW"]),
    ("v1.13.0", "2026-09-14", "2026-09-14", "AI 智商雷达上线",
     "新增只读的模型评测雷达插件，用卡片回答「哪个模型更适合我」。", ["雷达"]),
    ("v1.14.0", "2026-09-16", "2026-09-16", "模型档位长图",
     "雷达新增模型档位总览长图与 IQ 渐变色卡。", ["雷达"]),
]


def main() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    if payload.get("schema") != 1:
        raise SystemExit(
            f"{DATA} 的 schema 是 {payload.get('schema')!r}，不是 1。"
            "本脚本只用于把按日期聚合的 schema 1 数据一次性重组为按版本聚合；"
            "对已重组的 schema 2 数据重跑会按版本窗口重新贴标签、"
            "把每条更新的 date 改成窗口结束日，从而破坏数据。"
            "新增版本请直接编辑 changelog.json。"
        )
    by_date = {release["date"]: release for release in payload["releases"]}

    covered = set()
    releases = []
    for version, start, end, title, summary, tags in reversed(VERSIONS):
        highlights = []
        for date in sorted(by_date, reverse=True):
            if start <= date <= end:
                for item in by_date[date]["highlights"]:
                    highlights.append(
                        {
                            "date": date,
                            "kind": item["kind"],
                            "text": item["text"],
                            "commits": item["commits"],
                        }
                    )
                covered.add(date)
        releases.append(
            {
                "version": version,
                "date": end,
                "from": start,
                "to": end,
                "title": title,
                "summary": summary,
                "tags": tags,
                "highlights": highlights,
            }
        )

    missing = sorted(set(by_date) - covered)
    if missing:
        raise SystemExit(f"这些日期没有被任何版本覆盖：{missing}")

    commits = [short for r in releases for item in r["highlights"] for short in item["commits"]]
    out = {
        "schema": 2,
        "head": payload["head"],
        "generated_from": {
            **payload["generated_from"],
            "version_count": len(releases),
        },
        "releases": releases,
    }
    DATA.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"版本 {len(releases)} 个，提交 {len(commits)} 个，去重后 {len(set(commits))} 个")


if __name__ == "__main__":
    main()

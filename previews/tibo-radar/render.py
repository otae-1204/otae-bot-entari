"""Offline HTML/CSS/SVG UI samples. Never imports or starts the bot.

Run from anywhere: python previews/tibo-radar/render.py
Requires Playwright only for screenshots; --html-only uses the stdlib alone.
"""

from __future__ import annotations

import argparse
import asyncio
from html import escape
import json
from pathlib import Path
import shutil


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DISCLAIMER = "离线演示数据 · 内容为虚构样例，不代表 Tibo 实际发言或实时状态。预览未接入正式插件。"
POSTS = [
    (
        "01",
        "16:42",
        "direct",
        "Limits have been reset for the sample workspace.",
        "演示工作区的用量额度已重置。",
        "此处演示直接相关内容的排版。实际使用时，单条发言仍需结合来源、范围与完成证据核验。",
        "额度重置 / 范围核验",
    ),
    (
        "02",
        "16:18",
        "indirect",
        "We are targeting a reset window later today.",
        "我们计划在今天稍晚安排一次重置窗口。",
        "这是一条预告样例，仅提供时间意向，不能当作已经完成的重置。",
        "时间窗口 / 预告信号",
    ),
    (
        "03",
        "15:55",
        "indirect",
        "Improving the status page so updates are easier to follow.",
        "正在改进状态页，让更新进度更容易追踪。",
        "产品状态页相关内容，不构成重置已完成的直接证据。",
        "状态页面 / 间接相关",
    ),
]
STATUS = {
    "expected_window": (
        "预计窗口",
        "窗口仍在进行<br>等待完成核验",
        "观察窗口不是完成承诺。保留原始预告与后续核验记录。",
        "amber",
    ),
    "official_announcement": (
        "官方预告",
        "已发现公开预告<br>尚未进入核验阶段",
        "公开预告仅描述意向；不将计划自动升级为已完成。",
        "amber",
    ),
    "suspected": (
        "疑似信号",
        "有迹象，但还不足<br>以确认本次重置",
        "保留旁证，等待直接声明、范围确认与实际核验。",
        "amber",
    ),
    "confirmed": (
        "已确认完成",
        "完成证据已归档<br>本轮事件已核验",
        "仅在完成状态与适用范围均可核验时展示此状态。",
        "green",
    ),
    "unconfirmed": (
        "暂无确认信号",
        "当前没有足够证据<br>继续观察公开更新",
        "没有信号不等于不会发生重置，也不生成下一次时间预测。",
        "gray",
    ),
    "rejected": (
        "预告未获核验",
        "候选事件已否定<br>不计入完成统计",
        "历史预告保留供追溯；否定或未兑现的事件不会计为完成。",
        "red",
    ),
}


def tag(text: str, tone: str = "") -> str:
    return f'<span class="tag {escape(tone)}"><i class="dot"></i>{escape(text)}</span>'


def section(number: str, title: str, aside: str = "") -> str:
    return f'<div class="section-head"><h2><span>{number}</span>{title}</h2><small>{aside}</small></div>'


def radar_svg() -> str:
    return '<svg viewBox="0 0 260 260" fill="none" aria-label="装饰性观测刻度"><circle cx="130" cy="130" r="116" stroke="#758b6d" stroke-width="1" stroke-dasharray="1 9"/><circle cx="130" cy="130" r="96" stroke="#5f7658"/><circle cx="130" cy="130" r="73" stroke="#5f7658" stroke-dasharray="2 8"/><path d="M130 34 A96 96 0 0 1 219 166" stroke="#d7f47b" stroke-width="5" stroke-linecap="round"/><circle cx="219" cy="166" r="7" fill="#d7f47b"/><path d="M130 5V23 M237 130H255 M130 237V255 M5 130H23" stroke="#d7f47b"/><path d="M125 130H135 M130 125V135" stroke="#819574"/></svg>'


def hero(
    title: str,
    detail: str,
    label: str = "EXPECTED WINDOW",
    dial: str = "16:30",
    dial_label: str = "DEMO WINDOW / CST",
) -> str:
    return f'<div class="hero"><div><div class="status-pill"><i class="dot"></i>{label}</div><h2>{title}</h2><p>{detail}</p><small>仅示例状态 · 公开信息观察，不作时间承诺</small></div><div class="radar">{radar_svg()}<div class="radar-label"><b>{dial}</b><span>{dial_label}</span></div></div></div>'


def metrics(items: list[tuple[str, str, str, str]]) -> str:
    return (
        '<div class="metrics">'
        + "".join(
            f'<div class="metric"><div class="label">{label}</div><strong>{value}<em>{unit}</em></strong><small>{detail}</small></div>'
            for label, value, unit, detail in items
        )
        + "</div>"
    )


def health() -> str:
    return '<div class="health-row"><div class="health"><b>codexradar.com</b><p>采集成功 · 示例 2 分钟前</p></div><div class="health"><b>codex-reset / feed</b><p>采集成功 · 示例 2 分钟前</p></div><div class="health warn"><b>codex-reset / timeline</b><p>部分陈旧 · 使用最近成功快照</p></div></div>'


def overview() -> str:
    content = hero(
        "窗口仍在进行<br>等待完成核验",
        "公开预告 → 观察窗口 → 完成核验。<br>让信号、推测和已确认事实保持各自的位置。",
    )
    content += metrics(
        [
            ("最近确认距今", "4", "天", "示例：09.01 16:50"),
            ("已确认样本", "12", "次", "不计入预告 / 已否定事件"),
            ("采集周期", "10", "分钟", "本地缓存查询"),
            ("PT 当地时间", "01:50", "", "时区参考 · 非在线状态"),
        ]
    )
    content += section("01", "事件与证据", "CURRENT WINDOW / LAST VERIFIED")
    content += (
        '<div class="columns"><div class="panel"><div class="eyebrow">当前观察窗口</div><h3>09.05 · 今天稍晚</h3><p>仍在等待实际完成证据。<br>预计窗口仅供观察，不自动倒计时确认。</p><div class="window-bar"></div><div class="split-line"><span>16:30 CST</span><span>17:30 CST</span></div><div class="panel-bottom">'
        + tag("预计窗口", "amber")
        + '<span class="muted">样例进度</span></div></div><div class="panel"><div class="eyebrow">最近已确认记录</div><h3>完成证据已归档</h3><div class="time-large">09.01<span>16:50 CST</span></div><p>范围、状态、公开证据均保留。<br>仅以可核验完成事件计入历史。</p><div class="panel-bottom">'
        + tag("已确认完成")
        + '<span class="mono muted">DEMO-EVT-001 ↗</span></div></div></div>'
    )
    content += section("02", "最近相关动态", "原文 · 翻译 · 解读分层")
    for row in POSTS[:2]:
        number, time, tone, _, translation, _, _ = row
        content += f'<div class="signal-row"><span class="signal-no">/{number}</span><div><h3>{translation}</h3><p>演示帖 {number} · {time} CST · 查看原文与解读</p></div>{tag("直接相关" if tone == "direct" else "间接相关", "" if tone == "direct" else "amber")}</div>'
    return content + section("03", "来源健康", "数据状态 ≠ 事件状态") + health()


def post_card(row: tuple) -> str:
    number, time, tone, original, translation, analysis, phrases = row
    return f"""<article class="post"><div class="post-top">{tag("直接相关" if tone == "direct" else "间接相关", "" if tone == "direct" else "amber")}<span class="mono">DEMO POST / {number} · 09.05 {time} CST</span></div><p class="original">{escape(original)}</p><div class="field-label">TRANSLATION / 中文翻译</div><p class="translation">{translation}</p><div class="analysis"><div class="field-label">MODEL CONTEXT / 模型解读 · 非核验结论</div><p>{analysis}</p></div><div class="post-bottom"><span>{phrases}</span><a href="https://example.com/demo-post-{number}">演示原帖 ↗</a></div></article>"""


def feed(notification: bool = False) -> str:
    head = (
        '<div class="profile"><div class="profile-main"><span class="avatar">T</span><div><h2>Tibo / 公开信息观测</h2><p>原文、翻译与解读分别标注 · 所有发言均为排版样例</p></div></div><span class="mono">'
        + ("DELIVERY / 01" if notification else "3 POSTS / NEWEST FIRST")
        + "</span></div>"
    )
    if notification:
        head += '<div class="note">发现 2 条演示新帖 · 本轮合并为 1 张卡。来源链接随消息保留，发送回执确认后记录进度。</div>'
    return head + "".join(post_card(row) for row in POSTS[: 2 if notification else 3])


def status(kind: str = "expected_window") -> str:
    label, title, detail, tone = STATUS[kind]
    content = hero(
        title,
        detail,
        kind.upper().replace("_", " "),
        "4 / 4" if kind == "confirmed" else "OBS",
        "EVIDENCE / NOT PREDICTION",
    )
    active_steps = (
        4 if kind == "confirmed" else 0 if kind in {"unconfirmed", "rejected"} else 2
    )
    content += (
        '<div class="steps">'
        + "".join(
            f'<div class="step {"active" if i <= active_steps else ""}" data-step="{i}">{name}</div>'
            for i, name in enumerate(
                ["发现信号", "保留证据", "确认范围", "完成核验"], 1
            )
        )
        + "</div>"
    )
    content += section("01", "证据摘要", "DEMO-EVENT / 005")
    quotes = {
        "confirmed": ("The sample reset is now complete.", "演示重置现已完成。"),
        "rejected": (
            "The earlier sample window was withdrawn.",
            "先前的演示窗口已撤回。",
        ),
        "suspected": (
            "There may be a change in the sample usage indicators.",
            "演示用量指标可能出现了变化，但尚不足以确认重置。",
        ),
    }
    if kind == "unconfirmed":
        content += '<div class="evidence"><div class="field-label">NO VERIFIED EVIDENCE</div><h3>暂无可核验的相关证据</h3><p>保留最近成功的采集时间，不补写不存在的原始发言。</p></div>'
    else:
        original, translated = quotes.get(
            kind,
            (
                "We are targeting a reset window later today.",
                "我们计划在今天稍晚安排一次重置窗口。",
            ),
        )
        content += f'<div class="evidence"><div class="field-label">SOURCE QUOTE / 虚构排版样例</div><blockquote>{original}</blockquote><p>{translated}</p></div>'
    window = (
        "09.05 16:30–17:30 CST · 示例时间"
        if kind in {"expected_window", "official_announcement"}
        else "暂无有效的待确认窗口"
    )
    content += details(
        [
            ("状态分类", tag(label, tone)),
            (
                "适用范围",
                "暂无可确认范围"
                if kind == "unconfirmed"
                else "演示工作区 · 不能外推为所有账户",
            ),
            (
                "证据来源",
                "未发现相关证据（离线样例）"
                if kind == "unconfirmed"
                else "公开帖与核验记录（离线样例）",
            ),
            ("参考窗口", window),
        ]
    )
    return (
        content
        + '<div class="note amber">判定原则：预告不等于完成，预计时间不等于承诺，模型解读不等于官方结论。</div>'
    )


def details(rows: list[tuple[str, str]]) -> str:
    return (
        '<dl class="details">'
        + "".join(
            f'<div class="detail"><dt>{key}</dt><dd>{value}</dd></div>'
            for key, value in rows
        )
        + "</dl>"
    )


def recent() -> str:
    return (
        '<div class="subscription-hero"><div class="subscription-head"><div><div class="eyebrow">LAST VERIFIED RESET</div><h2>最近一次已确认重置</h2></div>'
        + tag("已核验 / 演示")
        + ' </div><div class="large-value">4<span>天</span> 00<span>小时</span></div><p class="muted">距示例确认时间 · 09.01 16:50 CST</p></div>'
        + section("01", "完成证据", "EVIDENCE FIRST")
        + '<div class="evidence"><div class="field-label">原始证据 · 虚构排版样例</div><blockquote>The sample reset is now complete.</blockquote><p>演示重置现已完成。</p></div>'
        + details(
            [
                ("确认状态", tag("已确认完成")),
                ("适用范围", "演示工作区"),
                ("事件编号", '<span class="mono">DEMO-EVT-001</span>'),
                ("核验来源", "公开证据与归档记录 · 离线演示"),
                (
                    "公开链接",
                    '<a href="https://example.com/demo-event-001">example.com/demo-event-001 ↗</a>',
                ),
            ]
        )
        + '<div class="note">只展示已核验完成的事件。未确认、预计窗口和已否定记录不会替代此条记录。</div>'
    )


def history() -> str:
    content = metrics(
        [
            ("已确认样本", "12", "次", "仅计完成记录"),
            ("最近间隔", "97", "小时", "历史描述 · 非预测"),
            ("候选事件", "04", "条", "仍需证据核验"),
            ("数据口径", "CST", "", "北京时间 / UTC+8"),
        ]
    )
    content += (
        section("01", "事件时间线", "最近 4 条样例 / 01") + '<div class="timeline">'
    )
    for date, clock, title, label, tone, desc in [
        (
            "09.05",
            "16:30",
            "预计时间窗口",
            "观察中",
            "amber",
            "保留公开预告，窗口期间等待完成核验。",
        ),
        (
            "09.01",
            "16:50",
            "完成证据已归档",
            "已确认",
            "",
            "范围与状态均完成核验，计入已确认历史。",
        ),
        (
            "08.28",
            "15:10",
            "候选事件被否定",
            "已否定",
            "red",
            "原预告未获核验，不作为实际重置样本。",
        ),
        (
            "08.24",
            "09:20",
            "上一轮确认记录",
            "已确认",
            "",
            "保留原帖、时间与核验状态，支持逐条追溯。",
        ),
    ]:
        content += f'<div class="timeline-item"><div class="timeline-time"><b>{date}</b>{clock} CST</div><div class="rail"></div><div class="timeline-content">{tag(label, tone)}<h3>{title}</h3><p>{desc}</p><span class="mono">DEMO ARCHIVE / {date.replace(".", "")} ↗</span></div></div>'
    content += "</div>" + section("02", "确认时刻分布", "历史样例 · 不预测下一次重置")
    content += (
        '<div class="panel"><div class="chart">'
        + "".join(
            f'<div class="bar {"focus" if i in (3, 8, 9) else ""}" style="height:{v}px"></div>'
            for i, v in enumerate([10, 16, 34, 64, 18, 10, 22, 44, 86, 68, 34, 12])
        )
        + '</div><div class="split-line"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00 CST</span></div></div>'
    )
    return content


def subscription() -> str:
    return (
        '<div class="subscription-hero"><div class="subscription-head"><div><div class="eyebrow">GROUP SUBSCRIPTION / DEMO GROUP</div><h2>本群的新帖通知已开启</h2></div><div class="toggle"></div></div>'
        + details(
            [
                ("最近发送", tag("图片 · 回执已确认")),
                (
                    "推送进度",
                    '<span class="mono">09.05 16:42:00 CST / DEMO-POST-001</span>',
                ),
                ("发送节奏", "每 10 分钟检测 · 每轮最多 1 张 / 3 条"),
                ("故障处理", "图片不可用时转为文字；全部失败则保留进度重试"),
            ]
        )
        + "</div>"
        + section("01", "推送链路", "采集进度与发送进度分别记录")
        + '<div class="delivery-track"><div class="node"><b>01</b>采集并保存</div><span class="arrow">→</span><div class="node"><b>02</b>图片 / 文字发送</div><span class="arrow">→</span><div class="node"><b>03</b>回执确认</div><span class="arrow">→</span><div class="node"><b>04</b>记录进度</div></div>'
        + section("02", "群订阅管理", "仅群主 / 管理员 / SUPERUSER")
        + '<div class="command-line">/tibo 订阅状态<span>查看当前发送方式与进度</span></div><div class="command-line">/tibo 取消订阅<span>停止本群后续通知</span></div><div class="note">首次订阅只接收之后发现的新帖。断线积压按顺序限量补发，不清空未投递记录，也不在重启时集中刷屏。</div>'
    )


def help_page() -> str:
    groups = [
        (
            "信息查询",
            [
                ("/tibo", "雷达总览：状态、证据、历史与来源健康。"),
                ("/tibo 动态 [数量]", "英文原文、中文翻译与模型解读；默认 6 条。"),
                ("/tibo 状态", "区分预告、观察窗口、疑似与已确认。"),
                ("/tibo 最近", "最近一次已核验完成的重置。"),
                ("/tibo 历史 [数量]", "默认 6 条事件，可按页追溯。"),
            ],
        ),
        (
            "群订阅",
            [
                ("/tibo 订阅", "从之后的新帖开始通知；需群管理权限。"),
                ("/tibo 取消订阅", "停止本群后续推送；保留历史数据。"),
                ("/tibo 订阅状态", "查看开关、发送进度与最近投递方式。"),
            ],
        ),
        (
            "辅助命令",
            [
                ("/雷达", "与 /tibo 等价的中文入口。"),
                ("/tibo 帮助", "重新打开此命令说明。"),
            ],
        ),
    ]
    content = '<div class="note" style="margin-top:0">输入命令即可使用，无需点击图片。数量支持 1–20；图片中的链接会同时作为消息正文提供。</div>'
    for name, rows in groups:
        content += (
            f'<div class="help-section"><h2>{name}</h2><div>'
            + "".join(
                f'<div class="help-row"><code>{cmd}</code><p>{desc}</p></div>'
                for cmd, desc in rows
            )
            + "</div></div>"
        )
    return (
        content
        + '<div class="panel"><div class="eyebrow">READ THE SIGNAL CORRECTLY</div><h3>先看证据，再看结论。</h3><p>预计时间不是完成承诺，相关动态不等于实际重置。<br>来源异常时明确标注缓存与最后成功时间。</p></div>'
    )


def empty() -> str:
    art = '<svg viewBox="0 0 220 190" fill="none"><circle cx="110" cy="94" r="73" stroke="#c8d4bf" stroke-dasharray="3 7"/><circle cx="110" cy="94" r="49" stroke="#9daf94"/><path d="M110 21V167M37 94H183" stroke="#c8d4bf"/><path d="M110 94L155 55" stroke="#087c56" stroke-width="2"/><circle cx="155" cy="55" r="5" fill="#087c56"/><circle cx="110" cy="94" r="6" fill="#d7f47b" stroke="#087c56"/></svg>'
    return (
        '<div class="empty">'
        + art
        + tag("暂无相关记录", "gray")
        + '<h2>还没有足够的信号。</h2><p>本地历史中暂未发现相关动态。<br>雷达会继续按采集周期观察，不凭空生成重置结论。</p><div class="result-actions"><div class="result-action primary">/tibo 总览</div><div class="result-action">/tibo 帮助</div></div></div>'
        + section("01", "采集状态", "空数据不等于采集故障")
        + health()
    )


def error() -> str:
    return (
        '<div class="note amber" style="margin-top:0">来源暂不可用 · 当前显示最近成功快照，不代表实时状态。</div>'
        + section("01", "来源健康", "DEGRADED / CACHED")
        + '<div class="panel">'
        + details(
            [
                (
                    "codexradar.com",
                    tag("请求超时", "amber") + "　最近成功：16:30 CST（示例）",
                ),
                (
                    "codex-reset / feed",
                    tag("暂不可用", "amber") + "　最近成功：16:30 CST（示例）",
                ),
                ("本地数据", tag("已保留") + "　可继续查询历史与已保存动态"),
                ("后续重试", "按周期重试，不将缺失数据解释为无信号"),
            ]
        )
        + "</div>"
        + section("02", "推送恢复状态", "DELIVERY HEALTH")
        + '<div class="panel"><h3>消息仍在队列中，不会被跳过。</h3><p>演示：图片与文字均失败，发送进度保持不变。<br>退避后继续重试，重启不会清除等待状态。</p>'
        + details(
            [
                ("最近发送方式", tag("文字兜底", "amber")),
                ("当前失败次数", "2 次 · TimeoutError（演示）"),
                ("下次可重试", "16:52 CST 之后的采集周期（演示）"),
            ]
        )
        + '</div><div class="note">来源健康、发送健康与重置事件是三个不同状态，不使用“重置失败”描述网络错误。</div>'
    )


def result(kind: str) -> str:
    entries = {
        "subscribed": (
            "✓",
            "",
            "订阅已开启。",
            "之后发现的新帖将按采集周期推送到本群。<br>首次订阅不补发历史，图片异常时会尝试文字摘要。",
            "/tibo 订阅状态",
            "/tibo 帮助",
        ),
        "unsubscribed": (
            "−",
            "",
            "本群通知已停止。",
            "后续新帖不再自动推送到本群。<br>已有历史与查询功能仍然保留，可随时重新订阅。",
            "/tibo 订阅",
            "/tibo 动态",
        ),
        "permission": (
            "!",
            "warn",
            "这一步需要群管理权限。",
            "仅群主、群管理员或 SUPERUSER 可管理订阅。<br>普通信息查询仍可正常使用；订阅只支持群聊。",
            "/tibo 总览",
            "/tibo 帮助",
        ),
        "invalid": (
            "?",
            "warn",
            "参数还需要调整一下。",
            "数量请输入 1–20 之间的整数。<br>例如 /tibo 动态 6；不认识的子命令可在帮助中查询。",
            "/tibo 动态 6",
            "/tibo 帮助",
        ),
    }
    icon, color, title, body, primary, secondary = entries[kind]
    return f'<div class="empty"><div class="notice-icon {color}">{icon}</div><h2>{title}</h2><p>{body}</p><div class="result-actions"><div class="result-action primary">{primary}</div><div class="result-action">{secondary}</div></div></div><div class="note">这是一张结果状态样板。预览中的命令与开关不会执行任何真实操作。</div>'


def pages() -> dict[str, tuple[str, str, str, str]]:
    result_pages = {
        "01-overview": ("信号总览", "SIGNAL OBSERVATORY", "/tibo", overview()),
        "02-feed": (
            "最新公开动态",
            "PUBLIC FEED / EVIDENCE STREAM",
            "/tibo 动态 3",
            feed(),
        ),
        "03-status": ("重置状态", "STATE / NOT A PREDICTION", "/tibo 状态", status()),
        "04-recent": ("最近确认", "LAST VERIFIED / ARCHIVE", "/tibo 最近", recent()),
        "05-history": (
            "重置事件档案",
            "EVENT HISTORY / TRACEABLE",
            "/tibo 历史",
            history(),
        ),
        "06-subscription": (
            "群订阅管理",
            "SUBSCRIPTION / DELIVERY",
            "/tibo 订阅状态",
            subscription(),
        ),
        "07-notification": (
            "你有新的公开信号",
            "NEW POSTS / GROUP DELIVERY",
            "自动订阅通知",
            feed(True),
        ),
        "08-help": (
            "雷达使用手册",
            "FIELD GUIDE / COMMANDS",
            "/tibo 帮助",
            help_page(),
        ),
        "09-empty": (
            "最新相关动态",
            "EMPTY STATE / KEEP OBSERVING",
            "/tibo 动态",
            empty(),
        ),
        "10-error": (
            "来源与发送状态",
            "HEALTH / GRACEFUL RECOVERY",
            "异常状态样例",
            error(),
        ),
    }
    for i, kind in enumerate(
        ("subscribed", "unsubscribed", "permission", "invalid"), 11
    ):
        title = {
            "subscribed": "订阅成功",
            "unsubscribed": "取消订阅",
            "permission": "权限提示",
            "invalid": "参数提示",
        }[kind]
        result_pages[f"{i:02}-{kind}"] = (
            title,
            "RESULT / FEEDBACK",
            "结果状态样例",
            result(kind),
        )
    for i, kind in enumerate(
        ("official_announcement", "suspected", "confirmed", "unconfirmed", "rejected"),
        15,
    ):
        result_pages[f"{i:02}-state-{kind}"] = (
            STATUS[kind][0],
            "STATE VARIANT / EVIDENCE",
            "/tibo 状态",
            status(kind),
        )
    return result_pages


def document(title: str, content: str) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + escape(title)
        + '</title><link rel="stylesheet" href="styles.css"></head><body>'
        + content
        + "</body></html>"
    )


def card(title: str, eyebrow: str, command: str, content: str) -> str:
    return f'<main class="sheet"><header class="masthead"><div class="brand"><span class="brand-mark">tr</span><div>TIBO RADAR<small>PUBLIC SIGNAL OBSERVATORY</small></div></div><span class="demo">DESIGN PREVIEW · 演示数据 / 非实时</span></header><div class="pagehead"><div><div class="eyebrow">{eyebrow}</div><h1>{title}</h1><p>公开信息 · 证据分层 · 可追溯记录</p></div><div class="command">{command}</div></div><div class="body">{content}</div><footer class="footer"><span>{DISCLAIMER}<br>预告 ≠ 已完成 · 模型解读 ≠ 官方结论</span><span class="mono">TR / 2026.09<br>SAMPLE SERIES 01</span></footer></main>'


def build(output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "styles.css", output / "styles.css")
    fonts = output / "fonts"
    fonts.mkdir(exist_ok=True)
    for name in ("MiSans-Regular.ttf", "MiSans-Bold.ttf"):
        shutil.copyfile(ROOT / "assets/font/steamInfo" / name, fonts / name)
    manifest = []
    for slug, (title, eyebrow, command, body) in pages().items():
        (output / f"{slug}.html").write_text(
            document(title, card(title, eyebrow, command, body)), encoding="utf-8"
        )
        manifest.append({"slug": slug, "title": title, "command": command})
    tiles = "".join(
        f'<a class="gallery-item" href="{p["slug"]}.html"><div class="gallery-image"><img src="{p["slug"]}.png" alt="{p["title"]}" loading="lazy"></div><h2>{p["title"]}</h2><small>{p["command"]} · 点击查看完整代码页面</small></a>'
        for p in manifest
    )
    (output / "index.html").write_text(
        document(
            "Tibo Radar / 样板图画廊",
            '<main class="gallery"><div class="eyebrow">TIBO RADAR / DESIGN SYSTEM 01</div><h1>公开信号，清晰呈现。</h1><p>19 张代码渲染样板 · 14 个功能 / 反馈页面 + 5 个状态变体。<br>'
            + DISCLAIMER
            + '</p><div class="gallery-grid">'
            + tiles
            + "</div></main>",
        ),
        encoding="utf-8",
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


async def capture(output: Path, manifest: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    report = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(
                viewport={"width": 1080, "height": 1400}, device_scale_factor=1
            )
            # Samples never make external network calls, even if a template is changed.
            await context.route("http://**/*", lambda route: route.abort())
            await context.route("https://**/*", lambda route: route.abort())
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            for item in manifest:
                await page.goto((output / f"{item['slug']}.html").as_uri())
                await page.evaluate("document.fonts.ready")
                metrics = await page.evaluate(
                    """() => ({width:document.querySelector('.sheet').offsetWidth,height:document.querySelector('.sheet').offsetHeight,overflow:document.documentElement.scrollWidth>1080,fonts:document.fonts.check('24px MiSans'),element_overflows:Array.from(document.querySelectorAll('.sheet *')).filter(el=>el.clientWidth>0 && el.scrollWidth>el.clientWidth+2).length})"""
                )
                if (
                    metrics["overflow"]
                    or metrics["element_overflows"]
                    or not metrics["fonts"]
                    or errors
                ):
                    raise RuntimeError(
                        f"Invalid layout for {item['slug']}: {metrics}; {errors}"
                    )
                await page.locator(".sheet").screenshot(
                    path=str(output / f"{item['slug']}.png")
                )
                report.append(
                    {
                        **item,
                        **metrics,
                        "bytes": (output / f"{item['slug']}.png").stat().st_size,
                    }
                )
            selected = [manifest[i] for i in (0, 1, 4, 5, 6, 7)]
            tiles = "".join(
                f'<div class="board-tile"><img src="{p["slug"]}.png"><b>{p["title"]}</b><small>{p["command"]}</small></div>'
                for p in selected
            )
            (output / "contact-sheet.html").write_text(
                document(
                    "Tibo UI / 总览板",
                    '<main class="board"><div class="eyebrow">TIBO RADAR / DESIGN PROPOSAL 01</div><h1>公开信号，清晰呈现。</h1><p class="board-sub">代码渲染 · 独立预览 · 所有内容均为离线演示数据 · 未接入正式插件</p><div class="board-grid">'
                    + tiles
                    + "</div></main>",
                ),
                encoding="utf-8",
            )
            await page.set_viewport_size({"width": 1800, "height": 1800})
            await page.goto((output / "contact-sheet.html").as_uri())
            await page.evaluate("document.fonts.ready")
            await page.locator(".board").screenshot(
                path=str(output / "contact-sheet.png")
            )
        finally:
            await browser.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output/tibo-radar-preview"
    )
    parser.add_argument("--html-only", action="store_true")
    parser.add_argument(
        "--publish-samples",
        action="store_true",
        help="Copy generated PNGs and validation to this preview's samples folder",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output == HERE:
        parser.error("Use an output directory distinct from the source directory")
    manifest = build(output)
    report = [] if args.html_only else asyncio.run(capture(output, manifest))
    validation = {"offline": True, "production_integrated": False, "pages": report}
    (output / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.publish_samples and not args.html_only:
        samples = HERE / "samples"
        samples.mkdir(exist_ok=True)
        for name in [
            *(f"{p['slug']}.png" for p in manifest),
            "contact-sheet.png",
            "validation.json",
        ]:
            shutil.copyfile(output / name, samples / name)
    print(
        f"Rendered {len(report)} PNGs; {len(manifest)} HTML pages: {output / 'index.html'}"
    )


if __name__ == "__main__":
    main()

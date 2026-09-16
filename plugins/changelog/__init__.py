"""更新日志插件。

让 Bot 用户自己查「我们历史版本更新了什么」：数据是仓库内按版本号聚合的
``changelog.json``，命令输出 HTML/CSS 图片卡片，渲染或发送失败时回退到纯文本。

分层（框架 §2.1）：

- ``models.py``：只读数据模型与加载校验，不出网、不读环境变量、不注册事件；
- ``formatters.py``：纯文本渲染，图片不可用时的降级通道；
- ``presentation.py``：只读数据 → 已转义的结构化卡片视图；
- ``rendering.py`` / ``assets/card.css``：离线 HTML/CSS → PNG；
- ``handlers.py``：命令解析 → 数据查询 → 图片 / 分段文本回复。

数据与仓库提交历史的对齐由 ``scripts/generate_changelog.py --check`` 强制：
每个非合并提交恰好出现在一个版本的一条更新里，不重不漏。
仓库没有打 tag，版本号按提交时间段划定，划分表见 ``scripts/_regroup_changelog.py``。
"""

from .handlers import *  # noqa: F401,F403

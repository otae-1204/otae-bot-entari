"""AI 智商雷达（DRadar）只读插件。

数据来自 ``api.codexradar.com`` 的公开只读端点；命令输出 HTML/CSS 图片卡片，
渲染或图片发送失败时回退到纯文本。

分层（框架 §2.1）：

- ``config.py``：配置与重试/缓存参数（唯一读环境变量的模块）；
- ``errors.py``：稳定错误码与降级链；
- ``models.py``：只读数据模型，不出网、不读环境变量；
- ``provider.py``：**唯一出网层**，只做 HTTP / 超时 / 缓存 / 错误归一化；
- ``service.py``：查询语义（排序、档位策略、别名、派生结构），不出网、不拼文案；
- ``formatters.py``：纯文本渲染，不碰网络与文件；
- ``presentation.py``：只读数据 → 结构化卡片视图与解释；
- ``rendering.py`` / ``assets/card.css``：离线 HTML/CSS → PNG；
- ``handlers.py``：命令解析 → service → presentation → 图片 / 分段文本回复。

前端实现请参考 ``docs/ai_radar_frontend_api.md``（数据接口文档）。
"""

from .handlers import *  # noqa: F401,F403

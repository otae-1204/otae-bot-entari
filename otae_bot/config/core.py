"""Core (non-plugin) settings: the ``otae:`` section at the root of ``entari.yml``.

Entari's own ``basic:`` section keeps what Entari defines (connections, prefix,
superusers, log); everything the bot core itself needs lives here. Plugin settings
live under ``plugins: plugins.<目录名>:`` and are declared next to each plugin.
"""

from __future__ import annotations

from .model import Settings, field


class LlmSettings(Settings):
    """通用 LLM 配置（部署诊断等；HYW 选择 config_source: llm 时也用这一组）"""

    api_key: str = field(default="", description="API Key（密钥），填 \"${{ env.LLM_API_KEY :- }}\"")
    base_url: str = field(default="", description="OpenAI 兼容接口基址；留空 = https://api.deepseek.com")
    model: str = field(default="", description="模型名；留空 = deepseek-v4-flash")


class HttpAssetSettings(Settings):
    """素材通道（图片/图标/头像），见 docs/http_asset_lane.md；留空的项用内置默认值"""

    connect_timeout: float | None = field(default=None, description="连接超时秒数（默认 4）")
    read_timeout: float | None = field(default=None, description="读超时秒数（默认 10）")
    retries: int | None = field(default=None, description="失败重试次数（默认 1）")
    concurrency: int | None = field(default=None, description="总并发（默认 8）")
    breaker_threshold: int | None = field(default=None, description="同一主机连续几次连接失败后熔断（默认 3；0 = 关闭）")
    breaker_cooldown: float | None = field(default=None, description="熔断冷却秒数（默认 300）")
    render_budget: float | None = field(default=None, description="每张卡素材准备总预算秒数（默认 25；0 = 不限时）")
    proxy: str | None = field(default=None, description="素材代理，如 http://127.0.0.1:7897（默认不走代理）")
    proxy_hosts: str | None = field(default=None, description="只让这些主机走素材代理，逗号分隔，支持 *.example.com")
    proxy_mode: str | None = field(default=None, description="fallback（默认，直连失败才走代理）或 always")
    direct_cooldown: float | None = field(default=None, description="fallback 下直连失败后多少秒内直接走代理（默认 300）")
    trust_env: bool | None = field(default=None, description="素材客户端是否读取 HTTP(S)_PROXY / NO_PROXY（默认 false）")
    host_rewrite: str | None = field(default=None, description="主机改写，如 源主机=目标主机,源2=目标2（默认关闭）")
    host_max_age: str | None = field(default=None, description="按主机后缀的磁盘缓存新鲜期，如 hycdn.cn=604800；off = 全部 600s")
    host_concurrency: str | None = field(default=None, description="按主机后缀的并发上限，如 hycdn.cn=4,fz.wiki=2；off = 只受总并发限制")
    hard_deadline: float | None = field(default=None, description="后台续传超过多少秒仍未完成才取消（默认 180）")
    queue_limit: int | None = field(default=None, description="未完成下载超过多少个时丢弃新的预取（默认 2000）")
    prefetch: bool | None = field(default=None, description="是否预取最近用过的素材（默认 true）")
    prefetch_daily_mb: int | None = field(default=None, description="预取每日流量上限 MB（默认 300，北京时间按天）")


class HttpCacheSettings(Settings):
    """共享 HTTP 客户端的内存与磁盘缓存预算"""

    api_mib: int = field(default=8, description="API 响应内存缓存 MiB")
    table_mib: int = field(default=64, description="数据表内存缓存 MiB")
    i18n_mib: int = field(default=64, description="I18n 文本表常驻内存 MiB")
    asset_mib: int = field(default=64, description="小图内存缓存 MiB")
    asset_large_entry_kib: int = field(default=1024, description="超过这个体积 (KiB) 的图有磁盘副本时不进内存")
    public_image_path: str = field(default="data/cache/public-images-v1.sqlite3", description="公共图片磁盘缓存文件")
    public_image_mib: int = field(default=256, description="公共图片磁盘缓存上限 MiB（0 = 不缓存）")
    public_table_path: str = field(default="data/cache/akedata-tables-v1.sqlite3", description="公共数据表磁盘缓存文件")
    public_table_mib: int = field(default=256, description="公共数据表磁盘缓存上限 MiB（0 = 不缓存）")


class BrowserSettings(Settings):
    """出图用的共享浏览器"""

    concurrency: int = field(default=2, description="同一浏览器同时渲染几张图（1–4；每多一张约多占 150–300 MB 内存）")


class MediaSettings(Settings):
    """媒体发送（图片、语音、视频、文件）"""

    shared_filesystem: bool = field(
        default=False,
        description="OneBot 接入端（LLBot）与 bot 在同一台机器、以同一用户运行时设 true：上传的图片以 file:// 路径交给它，省去 base64；默认 false",
    )


class CoreSettings(Settings):
    """otae_bot 核心配置（entari.yml 根部的 otae: 段）"""

    llm: LlmSettings = field(default_factory=LlmSettings, description="通用 LLM")
    http_asset: HttpAssetSettings = field(default_factory=HttpAssetSettings, description="素材通道")
    http_cache: HttpCacheSettings = field(default_factory=HttpCacheSettings, description="HTTP 缓存预算")
    browser: BrowserSettings = field(default_factory=BrowserSettings, description="出图浏览器")
    media: MediaSettings = field(default_factory=MediaSettings, description="媒体发送")
    help_cache_path: str = field(default="data/cache/help-cards", description="运行时帮助卡片缓存目录（相对仓库根）")

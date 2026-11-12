"""Convert a legacy deployment (.env + configs/*/config.json) to entari.yml + a secrets-only .env.

Settings move to ``entari.yml``; secrets stay in ``.env`` and are referenced from
``entari.yml`` as ``"${{ env.NAME :- }}"``. Variables that third-party code reads from
the process environment (proxies, TLS bundles, hyw-frontier) stay in ``.env`` as they
are. Unknown keys are kept in a marked section of the new ``.env``; nothing is dropped.

    python scripts/convert_config.py              # dry run: key names and destinations only
    python scripts/convert_config.py --apply      # write, backing up the originals (*.bak-<时间>)
    python scripts/convert_config.py --rollback   # restore the newest backups

Inputs, in the working directory (``--root``): ``.env``; ``configs/`` (or ``configs.legacy/``
when it was moved aside before ``git pull``); the old ``entari.yml`` (or ``entari.legacy.yml``)
for prefix / log level / ignore_self_message. ``configs/peek/config.json`` is runtime state, not
configuration: it is migrated to ``data/peek/state.json``.

Values are never printed. Runs only while the bot is stopped. Re-running after a successful
``--apply`` changes nothing. Standard library only (ruamel.yaml, when installed, is used to
read the old entari.yml; without it the old defaults are assumed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ENV_MARKER = "# otae-bot .env — secrets only (scripts/convert_config.py)"
YML_MARKER = "# otae-bot entari.yml (scripts/convert_config.py)"
ONEBOT11_ADAPTER = "otae_bot.boundary.onebot11:OneBotBoundaryAdapter"
LEGACY_SUPERUSERS = ["2461673400"]
LEGACY_BASIC = {"ignore_self_message": True, "log": {"level": "INFO"}, "prefix": ["/"]}

#: Read by third-party / vendored code from the process environment; must match
#: otae_bot.config.settings.PROCESS_ENV (checked by tests/test_convert_config.py).
PROCESS_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "JINA_API_KEY",
    "PARALLEL_API_KEY",
    "HYW_FRONTIER_HOME",
    "HYW_OPENCODE_SESSION",
    "HYW_RETRY_ATTEMPTS",
    "HYW_RETRY_BASE_DELAY",
    "HYW_RETRY_MAX_DELAY",
    "HYW_RETRY_BUDGET",
)
#: Entari's own variables (dotenv selection / config location) also stay in .env.
ENTARI_ENV = ("ENVIRONMENT", "ENTARI_CONFIG_FILE", "ENTARI_CONFIG_EXTENSION")
#: Documented once but never read by the bot.
OBSOLETE = ("MCSM_PANEL_URL", "MCSM_API_KEY", "MCSM_GROUP_WHITELIST", "NICKNAME", "COMMAND_START")


# ── legacy semantics ─────────────────────────────────────────────────────────────


def parse_legacy_env(text: str) -> dict[str, Any]:
    """The legacy parser (otae_bot/config/settings.py before entari.yml), line for line."""
    env_vars: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if "[" in value and "]" in value:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        env_vars[key] = value
    return env_vars


def _jsonish(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _text(value: Any) -> str:
    # What os.getenv returned for a JSON-looking value under the legacy loader.
    return value if isinstance(value, str) else json.dumps(value)


class Invalid(Exception):
    pass


def _int(raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise Invalid from None


def _float(raw: str) -> float:
    try:
        return float(raw.strip())
    except ValueError:
        raise Invalid from None


def _render_flag(raw: str) -> bool:  # HYW_RENDER: anything but false/0/no
    return str(_jsonish(raw)).lower() not in {"false", "0", "no"}


def _on_flag(raw: str) -> bool:  # 1/true/yes/on
    value = _jsonish(raw)
    if isinstance(value, bool):
        return value
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _not_off(raw: str) -> bool:  # anything but 0/false/no/off
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _not_off_or_disabled(raw: str) -> bool:
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _has_credentials(url: str) -> bool:
    try:
        parts = urlsplit(url if "://" in url else f"http://{url}")
        return bool(parts.username or parts.password)
    except ValueError:
        return "@" in url


# ── key table ────────────────────────────────────────────────────────────────────

P = "plugins"


@dataclass(frozen=True)
class Rule:
    path: tuple[str, ...]
    kind: str  # secret | proxy | str | keep | int | float | bool | csv
    parse: Callable[[str], Any] | None = None


def _plugin(name: str, key: str, kind: str, parse=None) -> Rule:
    return Rule((P, f"plugins.{name}", key), kind, parse)


RULES: dict[str, Rule] = {
    # core
    "LLM_API_KEY": Rule(("otae", "llm", "api_key"), "secret"),
    "LLM_BASE_URL": Rule(("otae", "llm", "base_url"), "str"),
    "LLM_MODEL": Rule(("otae", "llm", "model"), "str"),
    "OTAE_BROWSER_CONCURRENCY": Rule(("otae", "browser", "concurrency"), "int", _int),
    "OTAE_HELP_CACHE_PATH": Rule(("otae", "help_cache_path"), "str"),
    "OTAE_HTTP_API_CACHE_MIB": Rule(("otae", "http_cache", "api_mib"), "int", _int),
    "OTAE_HTTP_TABLE_CACHE_MIB": Rule(("otae", "http_cache", "table_mib"), "int", _int),
    "OTAE_HTTP_I18N_CACHE_MIB": Rule(("otae", "http_cache", "i18n_mib"), "int", _int),
    "OTAE_HTTP_ASSET_CACHE_MIB": Rule(("otae", "http_cache", "asset_mib"), "int", _int),
    "OTAE_HTTP_ASSET_LARGE_ENTRY_KIB": Rule(("otae", "http_cache", "asset_large_entry_kib"), "int", _int),
    "OTAE_PUBLIC_IMAGE_CACHE_PATH": Rule(("otae", "http_cache", "public_image_path"), "str"),
    "OTAE_PUBLIC_IMAGE_CACHE_MIB": Rule(("otae", "http_cache", "public_image_mib"), "int", _int),
    "OTAE_PUBLIC_TABLE_CACHE_PATH": Rule(("otae", "http_cache", "public_table_path"), "str"),
    "OTAE_PUBLIC_TABLE_CACHE_MIB": Rule(("otae", "http_cache", "public_table_mib"), "int", _int),
    # endfield / arknights
    "ENDFIELD_CREDENTIAL_KEY": _plugin("endfield", "credential_key", "secret"),
    "ENDFIELD_GACHA_LAYOUT": _plugin("endfield", "gacha_layout", "str"),
    "ENDFIELD_GACHA_FORWARD_ABOVE": _plugin("endfield", "gacha_forward_above", "int", _int),
    "ENDFIELD_GACHA_SHOW_STANDARD": _plugin("endfield", "gacha_show_standard", "bool", _not_off),
    "GACHA_SHOW_STANDARD": _plugin("endfield", "gacha_show_standard", "bool", _not_off),
    "ENDFIELD_GACHA_RERUN_SIDE": _plugin("endfield", "gacha_rerun_side", "str"),
    "ARKNIGHTS_CREDENTIAL_KEY": _plugin("arknights", "credential_key", "secret"),
    # hyw
    "HYW_CONFIG_SOURCE": _plugin("hyw", "config_source", "str"),
    "HYW_API_KEY": _plugin("hyw", "api_key", "secret"),
    "HYW_BASE_URL": _plugin("hyw", "base_url", "str"),
    "HYW_MODEL": _plugin("hyw", "model", "str"),
    "HYW_CREDENTIALS_FILE": _plugin("hyw", "credentials_file", "str"),
    "HYW_VERTEX_LOCATION": _plugin("hyw", "vertex_location", "str"),
    "HYW_VERTEX_BASE_URL": _plugin("hyw", "vertex_base_url", "str"),
    "HYW_PROXY": _plugin("hyw", "proxy", "proxy"),
    "HYW_SEARCH_PROXY": _plugin("hyw", "search_proxy", "proxy"),
    "HYW_RENDER": _plugin("hyw", "render", "bool", _render_flag),
    "HYW_SEARCH_PROVIDER": _plugin("hyw", "search_provider", "str"),
    "HYW_SEARCH_MODE": _plugin("hyw", "search_mode", "str"),
    "HYW_READER_ENGINE": _plugin("hyw", "reader_engine", "str"),
    "HYW_MAX_TOOL_IMAGES": _plugin("hyw", "max_tool_images", "int", _int),
    "HYW_MAX_READER_IMAGES": _plugin("hyw", "max_reader_images", "int", _int),
    "HYW_MAX_CONCURRENT": _plugin("hyw", "max_concurrent", "int", _int),
    "HYW_TIMEOUT": _plugin("hyw", "timeout", "float", _float),
    "HYW_REQUEST_TIMEOUT": _plugin("hyw", "request_timeout", "float", _float),
    "HYW_REASONING_MODE": _plugin("hyw", "reasoning_mode", "str"),
    "HYW_MAX_ROUNDS": _plugin("hyw", "max_rounds", "int", _int),
    "HYW_RENDER_TIMEOUT": _plugin("hyw", "render_timeout", "float", _float),
    "HYW_RENDER_MAX_IMAGES": _plugin("hyw", "render_max_images", "int", _int),
    "HYW_RENDER_MAX_EDGE": _plugin("hyw", "render_max_edge", "int", _int),
    "HYW_HOME": _plugin("hyw", "home", "str"),
    # grok_bot
    "GROKBOT_GATEWAY_URL": _plugin("grok_bot", "gateway_url", "str"),
    "GROKBOT_GATEWAY_TOKEN": _plugin("grok_bot", "gateway_token", "secret"),
    "GROKBOT_AGENT_ID": _plugin("grok_bot", "agent_id", "str"),
    "GROKBOT_TIMEOUT": _plugin("grok_bot", "timeout", "int", _int),
    "GROKBOT_MAX_PENDING": _plugin("grok_bot", "max_pending", "int", _int),
    "GROKBOT_MAX_CONCURRENT": _plugin("grok_bot", "max_concurrent", "int", _int),
    "GROKBOT_PERSONA_FILE": _plugin("grok_bot", "persona_file", "str"),
    # steamInfo
    "STEAM_API_KEYS": _plugin("steamInfo", "api_keys", "secret"),
    "STEAM_API_KEY": _plugin("steamInfo", "api_key", "secret"),
    "STEAM_REQUEST_INTERVAL": _plugin("steamInfo", "request_interval", "int", _int),
    "STEAM_DISABLE_BROADCAST_ON_STARTUP": _plugin("steamInfo", "disable_broadcast_on_startup", "bool", _on_flag),
    "STEAM_LLM_API_KEY": _plugin("steamInfo", "llm_api_key", "secret"),
    "STEAM_LLM_BASE_URL": _plugin("steamInfo", "llm_base_url", "str"),
    "STEAM_LLM_MODEL": _plugin("steamInfo", "llm_model", "str"),
    # mcsm / minecraft
    "MCSM_DEPLOY_LARGE_PACKAGE_MB": _plugin("mcsm", "deploy_large_package_mb", "int", _int),
    "MC_BROADCAST_INTERVAL": _plugin("minecraft_plugin", "broadcast_interval", "int", _int),
    # bilibili
    "BILI_SESSDATA": _plugin("bilibilibot", "sessdata", "secret"),
    "BILI_BUVID3": _plugin("bilibilibot", "buvid3", "secret"),
    "BILI_DM_IMG_LIST": _plugin("bilibilibot", "dm_img_list", "secret"),
    "BILI_DM_IMG_STR": _plugin("bilibilibot", "dm_img_str", "secret"),
    "BILI_DM_COVER_IMG_STR": _plugin("bilibilibot", "dm_cover_img_str", "secret"),
    "BILI_RSSHUB_BASE_URLS": _plugin("bilibilibot", "rsshub_base_urls", "csv", _csv),
    "BILI_LOTTERY_FILTER_UIDS": _plugin("bilibilibot", "lottery_filter_uids", "keep"),
    "BILI_LIVE_BATCH": _plugin("bilibilibot", "live_batch", "bool", _not_off),
    # radar / tibo_radar
    "RADAR_BASE_URL": _plugin("radar", "base_url", "str"),
    "RADAR_DEFAULT_BENCHMARK": _plugin("radar", "default_benchmark", "str"),
    "RADAR_TIMEOUT": _plugin("radar", "timeout", "float", _float),
    "RADAR_TABLE_TIMEOUT": _plugin("radar", "table_timeout", "float", _float),
    "RADAR_CONCURRENCY": _plugin("radar", "concurrency", "int", _int),
    "RADAR_MAX_BYTES": _plugin("radar", "max_bytes", "int", _int),
    "RADAR_MAX_TABLE_BYTES": _plugin("radar", "max_table_bytes", "int", _int),
    "RADAR_USER_AGENT": _plugin("radar", "user_agent", "str"),
    "RADAR_PROXY": _plugin("radar", "proxy", "proxy"),
    "TIBO_RADAR_REFRESH_MINUTES": _plugin("tibo_radar", "refresh_minutes", "int", _int),
    "TIBO_RADAR_TIMEOUT_SECONDS": _plugin("tibo_radar", "timeout_seconds", "int", _int),
    "TIBO_RADAR_CODEXRADAR_ENABLED": _plugin("tibo_radar", "codexradar_enabled", "bool", _not_off_or_disabled),
    "TIBO_RADAR_SUBSCRIPTION_BATCH": _plugin("tibo_radar", "subscription_batch", "int", _int),
    "TIBO_RADAR_SUBSCRIPTION_MAX_PAGES": _plugin("tibo_radar", "subscription_max_pages", "int", _int),
    "TIBO_RADAR_CODEXRADAR_URL": _plugin("tibo_radar", "codexradar_url", "str"),
    "TIBO_RADAR_FEED_URL": _plugin("tibo_radar", "feed_url", "str"),
    "TIBO_RADAR_TIMELINE_URL": _plugin("tibo_radar", "timeline_url", "str"),
}
_ASSET = {
    "CONNECT_TIMEOUT": ("float", _float), "READ_TIMEOUT": ("float", _float), "RETRIES": ("int", _int),
    "CONCURRENCY": ("int", _int), "BREAKER_THRESHOLD": ("int", _int), "BREAKER_COOLDOWN": ("float", _float),
    "RENDER_BUDGET": ("float", _float), "PROXY": ("proxy", None), "PROXY_HOSTS": ("str", None),
    "PROXY_MODE": ("str", None), "DIRECT_COOLDOWN": ("float", _float), "TRUST_ENV": ("bool", _on_flag),
    "HOST_REWRITE": ("str", None), "HOST_MAX_AGE": ("str", None), "HOST_CONCURRENCY": ("str", None),
    "HARD_DEADLINE": ("float", _float), "QUEUE_LIMIT": ("int", _int), "PREFETCH": ("bool", _on_flag),
    "PREFETCH_DAILY_MB": ("int", _int),
}
for _name, (_kind, _parse) in _ASSET.items():
    RULES[f"OTAE_HTTP_ASSET_{_name}"] = Rule(("otae", "http_asset", _name.lower()), _kind, _parse)
#: Keys handled by code of their own below.
STRUCTURED = ("SATORI_CLIENTS", "ONEBOT_CLIENTS", "SUPERUSERS")


# ── conversion ───────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    yml: dict = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)  # .env name -> value
    process: dict[str, str] = field(default_factory=dict)
    unknown: dict[str, str] = field(default_factory=dict)
    obsolete: dict[str, str] = field(default_factory=dict)
    yml_comments: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)  # key names and destinations only
    peek_state: Path | None = None


def placeholder(name: str) -> str:
    return "${{ env.%s :- }}" % name


def _set(tree: dict, path: tuple[str, ...], value: Any) -> None:
    node = tree
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _where(path: tuple[str, ...]) -> str:
    return ".".join(path[1:]) if path[0] == P else ".".join(path)


def _convert_key(plan: Plan, key: str, value: Any, rule: Rule) -> None:
    raw = _text(value).strip()
    where = _where(rule.path)
    if rule.kind == "secret" or (rule.kind == "proxy" and raw and _has_credentials(raw)):
        plan.secrets[key] = raw
        _set(plan.yml, rule.path, placeholder(key))
        plan.report.append(f"{key} -> .env {key}（密钥），entari.yml {where} 引用")
        return
    if raw == "" and rule.kind not in ("keep", "csv"):
        plan.report.append(f"{key} -> 留空，省略（{where} 用默认值）")
        return
    try:
        converted = rule.parse(raw) if rule.parse else raw
    except Invalid:
        plan.report.append(f"{key} -> 值无效，旧版按默认值处理；省略（{where} 用默认值）")
        return
    _set(plan.yml, rule.path, converted)
    plan.report.append(f"{key} -> entari.yml {where}")


def _connections(plan: Plan, env: dict[str, Any]) -> None:
    satori = env.get("SATORI_CLIENTS")
    onebot = env.get("ONEBOT_CLIENTS")
    satori = satori if isinstance(satori, list) else ([] if satori in (None, "") else None)
    onebot = onebot if isinstance(onebot, list) else ([] if onebot in (None, "") else None)
    if satori is None or onebot is None:
        raise SystemExit("SATORI_CLIENTS / ONEBOT_CLIENTS 不是 JSON 列表，无法转换；请先改正 .env")
    network = []
    # Legacy: with neither list the bot connected to localhost:5500; OneBot alone meant no Satori.
    entries = satori or ([] if onebot else [{}])
    for index, client in enumerate(entries, 1):
        client = client if isinstance(client, dict) else {}
        entry: dict[str, Any] = {"type": "ws", "host": str(client.get("host", "localhost")),
                                 "port": int(client.get("port", 5500)), "path": str(client.get("path", ""))}
        token = str(client.get("token", "") or "")
        if token:
            name = f"SATORI_TOKEN_{index}"
            plan.secrets[name] = token
            entry["token"] = placeholder(name)
            plan.report.append(f"SATORI_CLIENTS[{index}].token -> .env {name}（密钥）")
        if "secure" in client:
            plan.report.append(f"SATORI_CLIENTS[{index}].secure -> 旧版不读取该字段（一直是 ws://），按旧行为不写入；"
                               "需要 wss 时在 basic.network 里手动加 secure: true")
        network.append(entry)
    plan.yml.setdefault("basic", {})["network"] = network
    plan.report.append(f"SATORI_CLIENTS -> entari.yml basic.network（{len(network)} 条）")
    if onebot:
        adapters = []
        for index, client in enumerate(onebot, 1):
            client = client if isinstance(client, dict) else {}
            adapter: dict[str, Any] = {"$path": ONEBOT11_ADAPTER, "endpoint": str(client.get("endpoint", ""))}
            token = str(client.get("token", "") or "")
            if token:
                name = f"ONEBOT_TOKEN_{index}"
                plan.secrets[name] = token
                adapter["access_token"] = placeholder(name)
                plan.report.append(f"ONEBOT_CLIENTS[{index}].token -> .env {name}（密钥）")
            adapters.append(adapter)
        _set(plan.yml, (P, "server", "adapters"), adapters)
        plan.report.append(f"ONEBOT_CLIENTS -> entari.yml plugins.server.adapters（{len(adapters)} 条）")


def _superusers(plan: Plan, env: dict[str, Any]) -> None:
    value = env.get("SUPERUSERS", LEGACY_SUPERUSERS)
    values = value if isinstance(value, list) else [value]
    ids = [str(item).strip() for item in values if str(item).strip()]
    plan.yml.setdefault("basic", {})["superusers"] = {"qq": ids}
    source = "SUPERUSERS" if "SUPERUSERS" in env else "SUPERUSERS（未设置，沿用旧默认值）"
    plan.report.append(f"{source} -> entari.yml basic.superusers.qq（{len(ids)} 个）")


def _legacy_basic(path: Path | None) -> dict:
    basic = json.loads(json.dumps(LEGACY_BASIC))
    if path is None:
        return basic
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return basic
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    old = (data.get("entari", data) or {}).get("basic") or {}
    for key in ("ignore_self_message", "log", "prefix"):
        if key in old:
            basic[key] = old[key]
    return basic


def _configs(plan: Plan, directory: Path | None, root: Path) -> None:
    if directory is None:
        return
    for file in sorted(directory.glob("*/config.json")):
        name = file.parent.name
        try:
            content = json.loads(file.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            raise SystemExit(f"{file} 不是合法 JSON，请先检查") from None
        relative = f"configs/{name}/config.json"
        if name == "peek":
            if content:
                plan.peek_state = file
                plan.report.append(f"{relative} -> data/peek/state.json（运行时状态，迁移）")
            continue
        if name == "eft_helper" and isinstance(content, dict):
            if "use_proxy" in content:
                _set(plan.yml, (P, "plugins.eft_helper", "use_proxy"), bool(content.pop("use_proxy")))
                plan.report.append(f"{relative} use_proxy -> entari.yml plugins.eft_helper.use_proxy")
        if content:
            plan.yml_comments.append(f"# {relative}: {json.dumps(content, ensure_ascii=False)}")
            plan.report.append(f"{relative} -> 无插件读取的内容，原样写成 entari.yml 末尾的注释")


def build_plan(env: dict[str, Any], configs: Path | None, legacy_yml: Path | None, root: Path) -> Plan:
    plan = Plan()
    plan.yml["basic"] = {}
    _connections(plan, env)
    basic = plan.yml["basic"]
    basic.update(_legacy_basic(legacy_yml))
    _superusers(plan, env)
    plan.yml["basic"] = {k: basic[k] for k in ("network", "ignore_self_message", "log", "prefix", "superusers")}
    for key, value in env.items():
        if key in STRUCTURED:
            continue
        if key == "GACHA_SHOW_STANDARD" and str(env.get("ENDFIELD_GACHA_SHOW_STANDARD", "")).strip():
            plan.report.append("GACHA_SHOW_STANDARD -> 被 ENDFIELD_GACHA_SHOW_STANDARD 覆盖（与旧版相同），省略")
            continue
        if key in RULES:
            _convert_key(plan, key, value, RULES[key])
        elif key in PROCESS_ENV or key in ENTARI_ENV:
            plan.process[key] = _text(value)
            plan.report.append(f"{key} -> 留在 .env（第三方代码或 Entari 直接读取的环境变量）")
        elif key in OBSOLETE:
            plan.obsolete[key] = _text(value)
            plan.report.append(f"{key} -> 旧版就不读取；原样保留在 .env 的「不再读取」段")
        else:
            plan.unknown[key] = _text(value)
            plan.report.append(f"{key} -> 未识别；原样保留在 .env 的「未识别」段")
    _configs(plan, configs, root)
    return plan


# ── output ───────────────────────────────────────────────────────────────────────

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value == value and value not in (float("inf"), float("-inf")) else json.dumps(str(value))
    return json.dumps(str(value), ensure_ascii=False)


def _key(key: str) -> str:
    return key if _PLAIN_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def emit_yaml(value: Any, indent: int = 0) -> list[str]:
    """A tiny deterministic YAML emitter: block mappings, block sequences of mappings,
    flow sequences of scalars, JSON-quoted strings."""
    pad = " " * indent
    lines: list[str] = []
    for key, item in value.items():
        if isinstance(item, dict):
            if item:
                lines.append(f"{pad}{_key(key)}:")
                lines.extend(emit_yaml(item, indent + 2))
            else:
                lines.append(f"{pad}{_key(key)}: {{}}")
        elif isinstance(item, list) and item and all(isinstance(entry, dict) for entry in item):
            lines.append(f"{pad}{_key(key)}:")
            for entry in item:
                body = emit_yaml(entry, indent + 4)
                lines.append(f"{pad}  - {body[0].lstrip()}")
                lines.extend(body[1:])
        elif isinstance(item, list):
            lines.append(f"{pad}{_key(key)}: [{', '.join(_scalar(entry) for entry in item)}]")
        else:
            lines.append(f"{pad}{_key(key)}: {_scalar(item)}")
    return lines


def render_yml(plan: Plan) -> str:
    order = ("basic", "otae", P)
    tree = {key: plan.yml[key] for key in order if key in plan.yml}
    lines = [
        YML_MARKER,
        "# 由旧的 .env / configs 转换而来。全部可配置项、说明与默认值见 entari.example.yml。",
        '# 密钥只在 .env 里，这里用 "${{ env.名字 :- }}" 引用；改完重启 bot 生效。',
        "",
        *emit_yaml(tree),
    ]
    if plan.yml_comments:
        lines += ["", "# ── 旧 configs/ 里无插件读取的内容（原样保留备查，bot 不读取）──", *plan.yml_comments]
    return "\n".join(lines) + "\n"


_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@,+=%~-]*$")


def env_value(value: str, key: str = "") -> str:
    """Quote ``value`` so python-dotenv (Entari's .env reader) reads it back unchanged.

    python-dotenv expands ``${NAME}`` in every value and unescapes ``\\`` / ``\'`` inside
    single quotes, so values with a backslash or a single quote go into double quotes, and
    a value containing ``${`` cannot be written at all.
    """
    if _SAFE_VALUE.match(value):
        return value
    if "${" in value or "\n" in value or "\r" in value:
        raise SystemExit(f"{key or '某个键'} 的值含有 ${{ 或换行，python-dotenv 读不回原样；请手动处理后重试")
    if "'" not in value and "\\" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_env(plan: Plan, newline: str) -> str:
    lines = [ENV_MARKER, "# 只放密钥与第三方代码直接读取的环境变量；其余配置在 entari.yml。", ""]
    sections = (
        ("# ── 密钥（entari.yml 用 \"${{ env.名字 :- }}\" 引用）──", plan.secrets),
        ("# ── 第三方代码 / Entari 直接从进程环境读取 ──", plan.process),
        ("# ── 未识别的键（原样保留，bot 不读取；确认无用后可删除）──", plan.unknown),
        ("# ── 旧版就不读取的键（原样保留；确认无用后可删除）──", plan.obsolete),
    )
    for title, values in sections:
        if values:
            lines.append(title)
            lines.extend(f"{key}={env_value(value, key)}" for key, value in values.items())
            lines.append("")
    return newline.join(lines)


# ── files ────────────────────────────────────────────────────────────────────────


def _bot_running(root: Path) -> bool:
    lock_path = root / ".runtime" / "bot.lock"
    if not lock_path.exists():
        return False
    with lock_path.open("a+b") as lock_file:
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True
    return False


def _is_new_yml(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return text.startswith(YML_MARKER) or re.search(r"^otae:", text, re.MULTILINE) is not None


def _find(root: Path) -> tuple[Path, Path | None, Path | None]:
    configs = next((d for d in (root / "configs", root / "configs.legacy") if d.is_dir()), None)
    legacy_yml = next((f for f in (root / "entari.legacy.yml", root / "entari.yml")
                       if f.is_file() and not _is_new_yml(f)), None)
    return root / ".env", configs, legacy_yml


def _backup(path: Path, stamp: str) -> Path:
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copytree(path, target) if path.is_dir() else shutil.copy2(path, target)
    return target


def convert(root: Path, apply: bool, out=print) -> int:
    env_path, configs, legacy_yml = _find(root)
    yml_path = root / "entari.yml"
    env_text = env_path.read_bytes().decode("utf-8-sig") if env_path.is_file() else ""
    if env_text.startswith(ENV_MARKER):
        out("已转换过（.env 已是新格式），无需操作。")
        return 0
    if _is_new_yml(yml_path):
        out("entari.yml 已是新格式但 .env 仍是旧格式：请确认后移走其中一个再运行。")
        return 2
    if not env_path.is_file() and configs is None:
        out("没有找到旧的 .env 或 configs/，无需转换。")
        return 0
    plan = build_plan(parse_legacy_env(env_text), configs, legacy_yml, root)
    out("预演：" if not apply else "转换：")
    for line in plan.report:
        out(f"  {line}")
    out(f"  -> 写 entari.yml；.env 只留 {len(plan.secrets)} 个密钥、{len(plan.process)} 个环境变量、"
        f"{len(plan.unknown)} 个未识别键、{len(plan.obsolete)} 个不再读取的键")
    if not apply:
        out("（预演，未写入任何文件；加 --apply 执行）")
        return 0
    if _bot_running(root):
        out("bot 正在运行（.runtime/bot.lock 被占用），请先停止再转换。")
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    newline = "\r\n" if "\r\n" in env_text else "\n"
    peek_target = root / "data" / "peek" / "state.json"
    for path in (env_path, yml_path):
        if path.is_file():
            out(f"  备份 {path.name} -> {_backup(path, stamp).name}")
    if plan.peek_state is not None:
        if peek_target.exists():
            out("  data/peek/state.json 已存在，未覆盖")
        else:
            peek_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plan.peek_state, peek_target)
            out("  迁移 configs/peek/config.json -> data/peek/state.json")
    yml_path.write_text(render_yml(plan), encoding="utf-8", newline="\n")
    env_path.write_bytes(render_env(plan, newline).encode("utf-8"))
    if configs is not None and configs.name == "configs":
        target = configs.with_name(f"configs.bak-{stamp}")
        configs.rename(target)
        out(f"  configs/ -> {target.name}")
    out("完成。")
    return 0


def rollback(root: Path, out=print) -> int:
    """Put the newest backups back (the old code reads them); the converted files are kept as *.bak."""
    if _bot_running(root):
        out("bot 正在运行（.runtime/bot.lock 被占用），请先停止再回滚。")
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    restored = False
    for name in (".env", "entari.yml"):
        backups = sorted(root.glob(f"{name}.bak-*"), key=lambda p: p.name)
        backups = [b for b in backups if not b.name.endswith("-converted")]
        current = root / name
        if not backups:
            continue
        if current.is_file():
            current.rename(current.with_name(f"{name}.bak-{stamp}-converted"))
        shutil.copy2(backups[-1], current)
        out(f"  {backups[-1].name} -> {name}")
        restored = True
    configs = sorted(root.glob("configs.bak-*"), key=lambda p: p.name)
    if configs and not (root / "configs").exists():
        shutil.copytree(configs[-1], root / "configs")
        out(f"  {configs[-1].name} -> configs/")
        restored = True
    state = root / "data" / "peek" / "state.json"
    if state.is_file():
        target = root / "configs" / "peek" / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state, target)
        out("  data/peek/state.json -> configs/peek/config.json（带上转换后新增的改动）")
        restored = True
    out("完成。" if restored else "没有可回滚的备份。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="写入文件（默认只预演）")
    parser.add_argument("--rollback", action="store_true", help="恢复最近一次的备份")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="部署目录（默认当前目录）")
    args = parser.parse_args(argv)
    if args.rollback:
        return rollback(args.root)
    return convert(args.root, args.apply)


if __name__ == "__main__":
    sys.exit(main())

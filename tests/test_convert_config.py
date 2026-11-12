"""scripts/convert_config.py: legacy .env + configs/ -> entari.yml + secrets-only .env.

The round trip loads the produced files through the real loader and checks the settings
each plugin ends up with against what the legacy code path made of the same .env.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arclet.entari.config import EntariConfig

from otae_bot.config import settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import convert_config  # noqa: E402

LEGACY_EXAMPLE = ROOT / "tests/fixtures/config/legacy.env.example"
PARENT = "3fa2e78"  # the commit before entari.yml took over; its .env.example is the fixture above

ENDFIELD_KEY = base64.b64encode(b"e" * 32).decode()
#: One value per key of the legacy .env.example, plus a few undocumented and unknown keys.
#: Each entry is the literal right-hand side written to the fixture .env.
LEGACY = {
    "SATORI_CLIENTS": '[{"host":"10.0.0.2","port":"5500","path":"","token":"satori-secret-1"},'
                      '{"host":"10.0.0.3","port":5501,"token":""},'
                      '{"host":"bot.example.com","port":443,"path":"/satori/v1","token":"satori-secret-3","secure":true}]',
    "ONEBOT_CLIENTS": '[{"endpoint":"ws://127.0.0.1:3001","token":"onebot-secret-1"},{"endpoint":"ws://127.0.0.1:3002"}]',
    "SUPERUSERS": '["10001", "10002"]',
    "ENDFIELD_CREDENTIAL_KEY": ENDFIELD_KEY,
    "ENDFIELD_GACHA_LAYOUT": "v1",
    "ENDFIELD_GACHA_FORWARD_ABOVE": "0",
    "ENDFIELD_GACHA_SHOW_STANDARD": "off",
    "ARKNIGHTS_CREDENTIAL_KEY": "",
    "HTTP_PROXY": "http://127.0.0.1:7897",
    "HTTPS_PROXY": "",
    "OTAE_HTTP_ASSET_CONNECT_TIMEOUT": "3",
    "OTAE_HTTP_ASSET_READ_TIMEOUT": "",
    "OTAE_HTTP_ASSET_RETRIES": "2",
    "OTAE_HTTP_ASSET_CONCURRENCY": "",
    "OTAE_HTTP_ASSET_BREAKER_THRESHOLD": "0",
    "OTAE_HTTP_ASSET_BREAKER_COOLDOWN": "",
    "OTAE_HTTP_ASSET_RENDER_BUDGET": "40",
    "OTAE_HTTP_ASSET_PROXY": "'http://asset-user:asset-pass@proxy.test:8080'",
    "OTAE_HTTP_ASSET_PROXY_HOSTS": "bbs.hycdn.cn",
    "OTAE_HTTP_ASSET_PROXY_MODE": "always",
    "OTAE_HTTP_ASSET_DIRECT_COOLDOWN": "",
    "OTAE_HTTP_ASSET_TRUST_ENV": "yes",
    "OTAE_HTTP_ASSET_HOST_REWRITE": '{"a.test": "b.test"}',
    "OTAE_HTTP_ASSET_HOST_MAX_AGE": "",
    "OTAE_HTTP_ASSET_HOST_CONCURRENCY": "off",
    "OTAE_HTTP_ASSET_HARD_DEADLINE": "",
    "OTAE_HTTP_ASSET_QUEUE_LIMIT": "",
    "OTAE_HTTP_ASSET_PREFETCH": "false",
    "OTAE_HTTP_ASSET_PREFETCH_DAILY_MB": "",
    "OTAE_BROWSER_CONCURRENCY": "3",
    "LLM_API_KEY": '"llm-secret"',
    "LLM_BASE_URL": "https://llm.test/v1",
    "LLM_MODEL": "",
    "HYW_CONFIG_SOURCE": "hyw",
    "HYW_API_KEY": "'hyw-secret'",
    "HYW_BASE_URL": "https://openrouter.ai/api/v1/",
    "HYW_MODEL": "gpt-4o",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "HYW_CREDENTIALS_FILE": "",
    "HYW_VERTEX_LOCATION": "global",
    "HYW_VERTEX_BASE_URL": "",
    "HYW_RENDER": "false",
    "HYW_SEARCH_PROVIDER": "jina",
    "HYW_SEARCH_MODE": "turbo",
    "HYW_READER_ENGINE": "default",
    "HYW_MAX_TOOL_IMAGES": "100",
    "HYW_MAX_READER_IMAGES": "0",
    "HYW_MAX_CONCURRENT": "3",
    "HYW_TIMEOUT": "200",
    "HYW_REQUEST_TIMEOUT": "60.5",
    "HYW_REASONING_MODE": "high",
    "HYW_MAX_ROUNDS": "abc",
    "HYW_RENDER_TIMEOUT": "30",
    "HYW_RENDER_MAX_IMAGES": "8",
    "HYW_RENDER_MAX_EDGE": "800",
    "HYW_HOME": "data/hyw-test",
    "HYW_PROXY": "direct",
    "HYW_SEARCH_PROXY": "",
    "HYW_RETRY_ATTEMPTS": "2",
    "HYW_RETRY_BASE_DELAY": "0.5",
    "HYW_RETRY_MAX_DELAY": "8.0",
    "HYW_RETRY_BUDGET": "20.0",
    "GROKBOT_GATEWAY_URL": "http://100.64.0.1:1340",
    "GROKBOT_GATEWAY_TOKEN": "grok-secret",
    "GROKBOT_AGENT_ID": "",
    "GROKBOT_TIMEOUT": "600",
    "GROKBOT_MAX_PENDING": "8",
    "GROKBOT_MAX_CONCURRENT": "2",
    "GROKBOT_PERSONA_FILE": "",
    "STEAM_API_KEYS": "steam-a,steam-b",
    "STEAM_REQUEST_INTERVAL": "90",
    "STEAM_LLM_API_KEY": "steam-llm-secret",
    "STEAM_LLM_BASE_URL": "https://api.deepseek.com",
    "STEAM_LLM_MODEL": "deepseek-v4-flash",
    "STEAM_DISABLE_BROADCAST_ON_STARTUP": "true",
    "MCSM_PANEL_URL": "http://127.0.0.1:23333",
    "MCSM_API_KEY": "mcsm-obsolete-secret",
    "MCSM_GROUP_WHITELIST": "[]",
    "MC_BROADCAST_INTERVAL": "120",
    "BILI_SESSDATA": "bili-sess-secret",
    "BILI_BUVID3": "bili-buvid-secret",
    "BILI_DM_IMG_LIST": '[{"x":1,"y":2}]',
    "BILI_DM_IMG_STR": "bili-img-secret",
    "BILI_DM_COVER_IMG_STR": "bili-cover-secret",
    "BILI_RSSHUB_BASE_URLS": "https://rss.materium.io, https://rsshub.app",
    "BILI_LOTTERY_FILTER_UIDS": "",
    "RADAR_BASE_URL": "https://api.codexradar.com",
    "RADAR_DEFAULT_BENCHMARK": "pompeii-adjacency",
    "RADAR_TIMEOUT": "0.5",
    "RADAR_TABLE_TIMEOUT": "60",
    "RADAR_CONCURRENCY": "4",
    "RADAR_MAX_BYTES": "12582912",
    "RADAR_MAX_TABLE_BYTES": "25165824",
    "RADAR_USER_AGENT": "",
    "RADAR_PROXY": "",
    # read by code but never documented
    "BILI_LIVE_BATCH": "0",
    "TIBO_RADAR_REFRESH_MINUTES": "1",
    "OTAE_HTTP_API_CACHE_MIB": "16",
    # nobody reads these
    "SOME_LOCAL_NOTE": "keep me",
}
SECRETS = ("satori-secret-1", "satori-secret-3", "onebot-secret-1", ENDFIELD_KEY, "asset-pass", "llm-secret",
           "hyw-secret", "grok-secret", "steam-a,steam-b", "steam-llm-secret", "mcsm-obsolete-secret",
           "bili-sess-secret", "bili-buvid-secret", "bili-img-secret", "bili-cover-secret")


def legacy_env_text() -> str:
    lines = ["# 旧部署的 .env（合成夹具）", "", "  # 缩进的注释", "NOT A LINE"]
    lines += [f"{key}={value}" for key, value in LEGACY.items()]
    return "\r\n".join(lines) + "\r\n"


def write_legacy(root: Path) -> None:
    (root / ".env").write_bytes(legacy_env_text().encode("utf-8"))
    for name, content in {
        "peek": {"white_list": ["100"], "100": {"peek_list": {"pc": "http://10.0.0.9:8080"}}},
        "eft_helper": {"use_proxy": True},
        "minecraft_plugin": {"group_server": {}, "close_group": []},
        "web_screenshot": {"cmd_starts": ["截图"]},
    }.items():
        (root / "configs" / name).mkdir(parents=True)
        (root / "configs" / name / "config.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    (root / "entari.yml").write_text(
        'basic:\n  network:\n    - type: ws\n      host: "localhost"\n      port: 5500\n'
        '  ignore_self_message: true\n  log:\n    level: DEBUG\n  prefix: ["/", "!"]\nplugins: {}\n',
        encoding="utf-8",
    )


def run(root: Path, *args: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = convert_config.main(["--root", str(root), *args])
    return code, output.getvalue()


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@contextlib.contextmanager
def loaded(root: Path):
    """Load root/entari.yml + root/.env the way bot.py does, then restore global state."""
    missing = object()
    previous = getattr(EntariConfig, "instance", missing)
    with contextlib.chdir(root), patch.dict(os.environ), settings.override({}):
        for name in settings.PROCESS_ENV:
            os.environ.pop(name, None)
        try:
            yield settings.load_config()
        finally:
            if previous is missing:
                EntariConfig.load(root / "absent.yml")
            else:
                EntariConfig.instance = previous


class KeyTableTests(unittest.TestCase):
    def test_every_documented_legacy_key_has_a_destination(self):
        documented = set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]+)=", LEGACY_EXAMPLE.read_text(encoding="utf-8"), re.M))
        handled = (set(convert_config.RULES) | set(convert_config.PROCESS_ENV) | set(convert_config.OBSOLETE)
                   | set(convert_config.ARCHIVED)
                   | set(convert_config.STRUCTURED))
        self.assertEqual(sorted(documented - handled), [])
        self.assertEqual(sorted(documented - set(LEGACY)), [], "the fixture must cover every documented key")

    def test_fixture_is_the_parent_commits_env_example(self):
        try:
            text = subprocess.run(["git", "show", f"{PARENT}:.env.example"], cwd=ROOT, capture_output=True,
                                  check=True, text=True, encoding="utf-8").stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git history not available")
        self.assertEqual(text, LEGACY_EXAMPLE.read_text(encoding="utf-8"))

    def test_process_variables_match_the_loader(self):
        self.assertEqual(convert_config.PROCESS_ENV, settings.PROCESS_ENV)

    def test_every_destination_is_a_declared_setting(self):
        from otae_bot.config.core import CoreSettings
        from plugins.arknights.config import ArknightsSettings
        from plugins.bilibilibot.config import BiliSettings
        from plugins.eft_helper.config import EftSettings
        from plugins.endfield.config import EndfieldSettings
        from plugins.hyw.config import HywSettings
        from plugins.mcsm.config import McsmSettings
        from plugins.minecraft_plugin.config import MinecraftSettings
        from plugins.radar.config import RadarSettings
        from plugins.steamInfo.config import SteamSettings
        from plugins.tibo_radar.config import TiboRadarSettings

        models = {
            "plugins.arknights": ArknightsSettings, "plugins.bilibilibot": BiliSettings,
            "plugins.eft_helper": EftSettings, "plugins.endfield": EndfieldSettings,
            "plugins.hyw": HywSettings, "plugins.mcsm": McsmSettings,
            "plugins.minecraft_plugin": MinecraftSettings, "plugins.radar": RadarSettings,
            "plugins.steamInfo": SteamSettings, "plugins.tibo_radar": TiboRadarSettings,
        }

        def declared(model, path):
            for part in path[:-1]:
                model = {f.name: f for f in dataclasses.fields(model)}[part].default_factory
            return path[-1] in {f.name for f in dataclasses.fields(model)}

        for key, rule in convert_config.RULES.items():
            with self.subTest(key=key):
                if rule.path[0] == "plugins":
                    self.assertTrue(declared(models[rule.path[1]], rule.path[2:]), rule.path)
                else:
                    self.assertTrue(declared(CoreSettings, rule.path[1:]), rule.path)


class ParserTests(unittest.TestCase):
    def test_parser_matches_the_legacy_one_from_git_history(self):
        try:
            source = subprocess.run(["git", "show", f"{PARENT}:otae_bot/config/settings.py"], cwd=ROOT,
                                    capture_output=True, check=True, text=True, encoding="utf-8").stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git history not available")
        function = source[source.index("def _load_dotenv"):source.index("_dotenv = _load_dotenv()")]
        namespace = {"Path": Path, "json": json}
        exec(function, namespace)  # noqa: S102 - our own code from history
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            Path(".env").write_bytes(legacy_env_text().encode("utf-8"))
            legacy = namespace["_load_dotenv"]()
        self.assertEqual(convert_config.parse_legacy_env(legacy_env_text()), legacy)

    def test_env_values_survive_python_dotenv(self):
        from dotenv import dotenv_values

        values = ["plain", "with space", "a#b", "it's", "back\\slash", 'say "hi"', "$HOME", "q'$", "=x=",
                  "中文", "[1, 2]", ""]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("".join(f"K{i}={convert_config.env_value(v)}\n" for i, v in enumerate(values)),
                            encoding="utf-8")
            read = dotenv_values(path)
        self.assertEqual([read[f"K{i}"] for i in range(len(values))], values)
        with self.assertRaises(SystemExit) as caught:
            convert_config.env_value("a${HOME}b", "SOME_KEY")
        self.assertIn("SOME_KEY", str(caught.exception))
        self.assertNotIn("a${HOME}b", str(caught.exception))


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_legacy(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_prints_names_only_and_writes_nothing(self):
        before = snapshot(self.root)
        code, output = run(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(self.root), before)
        self.assertIn("预演", output)
        self.assertIn("HYW_API_KEY -> .env HYW_API_KEY", output)
        self.assertIn("SOME_LOCAL_NOTE", output)
        for value in (*SECRETS, "keep me", "10.0.0.2", "100.64.0.1"):
            self.assertNotIn(value, output)

    def test_apply_is_idempotent_and_backs_up(self):
        code, output = run(self.root, "--apply")
        self.assertEqual(code, 0, output)
        for value in SECRETS:
            self.assertNotIn(value, output)
        names = {p.name for p in self.root.iterdir()}
        self.assertTrue(any(n.startswith(".env.bak-") for n in names))
        self.assertTrue(any(n.startswith("entari.yml.bak-") for n in names))
        self.assertTrue(any(n.startswith("configs.bak-") for n in names))
        self.assertNotIn("configs", names)
        env_text = (self.root / ".env").read_bytes().decode("utf-8")
        self.assertTrue(env_text.startswith(convert_config.ENV_MARKER))
        self.assertIn("\r\n", env_text)
        # 已归档插件（grok_bot）的旧键原样保留在「不再读取」段，密钥不丢
        section = env_text.index("# ── 不再读取的键")
        for key in convert_config.ARCHIVED:
            self.assertGreater(env_text.index(f"\r\n{key}="), section, key)
        self.assertIn("grok-secret", env_text)
        yml = (self.root / "entari.yml").read_text(encoding="utf-8")
        self.assertNotIn("grok", yml.lower())
        for value in SECRETS:
            self.assertNotIn(value, yml)
        self.assertIn("# configs/web_screenshot/config.json:", yml)  # preserved as a comment
        state = json.loads((self.root / "data/peek/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["white_list"], ["100"])
        first = snapshot(self.root)
        code, output = run(self.root, "--apply")
        self.assertEqual(code, 0)
        self.assertIn("已转换过", output)
        self.assertEqual(snapshot(self.root), first)

    def test_round_trip_gives_the_legacy_effective_settings(self):
        self.assertEqual(run(self.root, "--apply")[0], 0)
        with loaded(self.root) as config:
            self.check_core(config)
            self.check_plugins()

    def check_core(self, config):
        from otae_bot.boundary import server_plugin_config
        from otae_bot.infrastructure.http.asset_policy import load_asset_settings

        self.assertEqual(
            [entry.dump() for entry in config.basic.network],
            [
                {"host": "10.0.0.2", "port": 5500, "path": "", "secure": False, "token": "satori-secret-1",
                 "timeout": None},
                {"host": "10.0.0.3", "port": 5501, "path": "", "secure": False, "token": None, "timeout": None},
                {"host": "bot.example.com", "port": 443, "path": "/satori/v1", "secure": False,
                 "token": "satori-secret-3", "timeout": None},
            ],
        )
        self.assertEqual(config.basic.prefix, ["/", "!"])
        self.assertEqual(config.basic.log.level, "DEBUG")
        self.assertTrue(config.basic.ignore_self_message)
        self.assertEqual(settings.Config.SUPERUSERS, ["10001", "10002"])
        server = server_plugin_config(settings.server_declaration(), self.root / ".runtime")
        self.assertEqual([(a["endpoint"], a["access_token"]) for a in server["adapters"]],
                         [("ws://127.0.0.1:3001", "onebot-secret-1"), ("ws://127.0.0.1:3002", None)])
        self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:7897")
        self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertEqual(os.environ.get("HYW_RETRY_ATTEMPTS"), "2")
        for name in ("HYW_API_KEY", "SOME_LOCAL_NOTE", "MCSM_API_KEY"):
            self.assertNotIn(name, os.environ)
        core = settings.core_settings()
        self.assertEqual((core.browser.concurrency, core.http_cache.api_mib, core.http_cache.table_mib), (3, 16, 64))
        asset = load_asset_settings()
        self.assertEqual(
            (asset.connect_timeout, asset.read_timeout, asset.retries, asset.breaker_threshold, asset.render_budget,
             asset.proxy, asset.proxy_hosts, asset.proxy_mode, asset.trust_env, asset.host_rewrites,
             asset.host_concurrency, asset.prefetch),
            (3.0, 10.0, 2, 0, 40.0, "http://asset-user:asset-pass@proxy.test:8080", ("bbs.hycdn.cn",), "always",
             True, (("a.test", "b.test"),), (), False),
        )

    def check_plugins(self):
        from plugins.arknights.crypto import ArknightsCipher, CredentialKeyError as ArknightsKeyError
        from plugins.bilibilibot.config import bili_settings
        from plugins.eft_helper.config import eft_settings
        from plugins.endfield.account.crypto import CredentialCipher
        from plugins.endfield.config import endfield_settings
        from plugins.hyw.config import HywConfig
        from plugins.mcsm.config import mcsm_settings
        from plugins.mcsm.deploy import llm_config
        from plugins.minecraft_plugin.config import minecraft_settings
        from plugins.radar.config import RadarConfig
        from plugins.steamInfo.config import load_config as steam_config
        from plugins.tibo_radar.config import tibo_radar_settings

        hyw = HywConfig.from_settings()
        self.assertEqual(
            (hyw.api_key, hyw.base_url, hyw.model, hyw.render, hyw.search_provider, hyw.reader_engine,
             hyw.max_tool_images, hyw.max_reader_images, hyw.max_concurrent, hyw.timeout, hyw.request_timeout,
             hyw.reasoning_mode, hyw.max_rounds, hyw.render_timeout, hyw.render_max_images, hyw.render_max_edge,
             hyw.home, hyw.proxy, hyw.search_proxy, hyw.auth_mode),
            ("hyw-secret", "https://openrouter.ai/api/v1", "gpt-4o", False, "jina", "default", 100, 0, 3, 200.0,
             60.5, "high", 10, 30.0, 8, 800, "data/hyw-test", "", "", "api_key"),
        )
        radar = RadarConfig.from_settings()
        self.assertEqual((radar.timeout, radar.default_benchmark, radar.proxy, radar.max_bytes),
                         (1.0, "pompeii-adjacency", None, 12582912))
        self.assertIn("otae-bot-radar/", radar.user_agent)
        steam = steam_config()
        self.assertEqual(
            (steam.steam_api_keys, steam.steam_request_interval, steam.steam_disable_broadcast_on_startup,
             steam.steam_llm_api_key, steam.steam_llm_model, steam.proxy),
            ("steam-a,steam-b", 90, True, "steam-llm-secret", "deepseek-v4-flash", "http://127.0.0.1:7897"),
        )
        self.assertEqual(llm_config(), {"api_key": "llm-secret", "base_url": "https://llm.test/v1",
                                        "model": "deepseek-v4-flash"})
        bili = bili_settings()
        self.assertEqual(
            (bili.sessdata, bili.buvid3, bili.dm_img_list, bili.dm_img_str, bili.rsshub_base_urls,
             bili.lottery_filter_uids, bili.live_batch),
            ("bili-sess-secret", "bili-buvid-secret", '[{"x": 1, "y": 2}]', "bili-img-secret",
             ["https://rss.materium.io", "https://rsshub.app"], "", False),
        )
        endfield = endfield_settings()
        self.assertEqual((endfield.gacha_layout, endfield.gacha_forward_above, endfield.gacha_show_standard),
                         ("v1", 0, False))
        self.assertIsInstance(CredentialCipher.from_settings(), CredentialCipher)
        with self.assertRaises(ArknightsKeyError):
            ArknightsCipher.from_settings()
        self.assertTrue(eft_settings().use_proxy)
        self.assertEqual(minecraft_settings().broadcast_interval, 120)
        self.assertEqual(mcsm_settings().deploy_large_package_mb, 200)
        self.assertEqual(tibo_radar_settings().refresh_minutes, 1)  # the handler still clamps it to 2

    def test_rollback_restores_the_legacy_files(self):
        original = (self.root / ".env").read_bytes()
        self.assertEqual(run(self.root, "--apply")[0], 0)
        code, output = run(self.root, "--rollback")
        self.assertEqual(code, 0, output)
        self.assertEqual((self.root / ".env").read_bytes(), original)
        self.assertIn("level: DEBUG", (self.root / "entari.yml").read_text(encoding="utf-8"))
        peek = json.loads((self.root / "configs/peek/config.json").read_text(encoding="utf-8"))
        self.assertEqual(peek["white_list"], ["100"])

    def test_onebot_only_and_unset_lists_follow_the_legacy_defaults(self):
        plan = convert_config.build_plan({}, None, None, self.root)
        self.assertEqual(plan.yml["basic"]["network"], [{"type": "ws", "host": "localhost", "port": 5500, "path": ""}])
        self.assertEqual(plan.yml["basic"]["superusers"], {"qq": convert_config.LEGACY_SUPERUSERS})
        plan = convert_config.build_plan({"ONEBOT_CLIENTS": [{"endpoint": "ws://h:1"}]}, None, None, self.root)
        self.assertEqual(plan.yml["basic"]["network"], [])
        self.assertIn("server", plan.yml["plugins"])

    def test_refuses_while_the_bot_runs(self):
        from otae_bot.lifecycle import acquire_run_lock

        lock = acquire_run_lock(self.root / ".runtime")
        try:
            code, output = run(self.root, "--apply")
        finally:
            lock.close()
        self.assertEqual(code, 2)
        self.assertIn("正在运行", output)
        self.assertFalse((self.root / ".env").read_bytes().startswith(convert_config.ENV_MARKER.encode()))


if __name__ == "__main__":
    unittest.main()

"""entari.yml + .env loading: interpolation, typed sections, write-back isolation."""

from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from otae_bot.config import settings
from otae_bot.config.model import Settings, field, validate
from tests.support.config import loaded_config_file


class DemoSettings(Settings):
    """测试用"""

    name: str = field(default="demo", description="名字")
    count: int = field(default=3, description="次数")
    ratio: float = field(default=0.5, description="比例")
    enabled: bool = field(default=True, description="开关")
    hosts: list[str] = field(default_factory=list, description="主机")
    token: str = field(default="", description="密钥")
    limit: int | None = field(default=None, description="上限")


YML = """\
# 注释要保留
basic:
  network:
    - type: ws
      host: 127.0.0.1
      port: 5500
      token: "${{ env.SATORI_TOKEN_1 :- }}"
    - type: ws
      host: 127.0.0.1
      port: 5501
      token: "${{ env.SATORI_TOKEN_2 :- }}"
    - type: ws
      host: bot.example.com
      port: 443
      path: /satori/v1
      secure: true
      token: "${{ env.SATORI_TOKEN_3 :- }}"
  prefix: ["/"]
  superusers:
    qq: ["100", "200"]
    other: ["200", "300"]
otae:
  browser:
    concurrency: 3
plugins:
  plugins.demo:
    count: "${{ env.DEMO_COUNT :- }}"
    ratio: "0.25"
    enabled: "false"
    hosts: [a.test, b.test]
    token: "${{ env.DEMO_TOKEN :- }}"
    limit: "${{ env.NOT_SET }}"
    typo: 1
"""
ENV = """\
SATORI_TOKEN_1=secret-one
SATORI_TOKEN_2=
DEMO_COUNT=7
DEMO_TOKEN=secret-demo
HTTP_PROXY=http://127.0.0.1:7897
UNLISTED_SECRET=should-stay-out
"""


class LoadConfigTests(unittest.TestCase):
    def test_sections_are_typed_and_interpolated(self):
        with loaded_config_file(YML, ENV) as (config, _), patch("otae_bot.config.model.logger") as log:
            demo = settings.plugin_settings("demo", DemoSettings)
            self.assertEqual(
                (demo.name, demo.count, demo.ratio, demo.enabled, demo.hosts, demo.token, demo.limit),
                ("demo", 7, 0.25, False, ["a.test", "b.test"], "secret-demo", None),
            )
            self.assertEqual(settings.core_settings().browser.concurrency, 3)
            self.assertEqual(settings.Config.SUPERUSERS, ["100", "200", "300"])
            self.assertEqual(settings.network_endpoints(),
                             ["127.0.0.1:5500", "127.0.0.1:5501", "bot.example.com:443"])
            networks = [entry.dump() for entry in config.basic.network]
        self.assertEqual([n["token"] for n in networks], ["secret-one", "", ""])
        self.assertEqual((networks[2]["secure"], networks[2]["path"]), (True, "/satori/v1"))
        warnings = " ".join(str(call.args[0]) for call in log.warning.call_args_list)
        self.assertIn("plugins.demo.limit", warnings)
        self.assertIn("plugins.demo.typo", warnings)
        self.assertNotIn("secret", warnings)
        # Outside the loaded configuration everything is back to defaults.
        self.assertEqual(settings.plugin_settings("demo", DemoSettings).count, 3)
        self.assertIsNone(settings.loaded_config())

    def test_only_listed_process_variables_are_exported(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://real-env:1"}, clear=False):
            os.environ.pop("HTTP_PROXY", None)
            with loaded_config_file(YML, ENV + "HTTPS_PROXY=http://dotenv:2\n"):
                self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:7897")
                self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://real-env:1")
                self.assertNotIn("UNLISTED_SECRET", os.environ)
                self.assertNotIn("DEMO_TOKEN", os.environ)
                self.assertEqual(settings.system_proxy(),
                                 {"http": "http://127.0.0.1:7897", "https": "http://real-env:1"})

    def test_entari_write_back_only_touches_the_private_copy(self):
        with loaded_config_file(YML, ENV) as (config, root):
            # What Entari.run() does on exit.
            config.save()
            self.assertEqual((root / "entari.yml").read_text(encoding="utf-8"), YML)
            copy = (root / ".runtime" / "entari.yml").read_text(encoding="utf-8")
        self.assertIn("# 注释要保留", copy)
        self.assertNotIn("secret-one", copy)
        self.assertNotIn("secret-demo", copy)

    def test_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(settings.ConfigFileMissing):
                settings.load_config(Path(directory) / "entari.yml", Path(directory) / ".runtime")

    def test_override_and_server_declaration(self):
        with settings.override({"plugins": {"server": {"adapters": [{"endpoint": "ws://h:1"}]}}}):
            declared = settings.server_declaration()
            declared["adapters"].append({})
            self.assertEqual(len(settings.server_declaration()["adapters"]), 1)
        with settings.override({}):
            self.assertIsNone(settings.server_declaration())


class ValidateTests(unittest.TestCase):
    def test_bad_values_fall_back_to_defaults(self):
        with patch("otae_bot.config.model.logger"):
            demo = validate(DemoSettings, {"count": "many", "ratio": [1], "enabled": "maybe", "$disable": True},
                            "plugins.x")
        self.assertEqual((demo.count, demo.ratio, demo.enabled), (3, 0.5, True))

    def test_comma_separated_lists_and_numbers(self):
        demo = validate(DemoSettings, {"hosts": "a, b,,c", "count": 2.0, "limit": "9"}, "plugins.x")
        self.assertEqual((demo.hosts, demo.count, demo.limit), (["a", "b", "c"], 2, 9))


if __name__ == "__main__":
    unittest.main()


class ExampleFileTests(unittest.TestCase):
    """entari.example.yml documents every setting with its default; .env.example has every secret it references."""

    ROOT = Path(__file__).resolve().parents[1]

    def models(self):
        from otae_bot.config.core import CoreSettings
        from tests.test_convert_config import KeyTableTests  # noqa: F401  (imports every plugin model)
        from plugins.arknights.config import ArknightsSettings
        from plugins.bilibilibot.config import BiliSettings
        from plugins.eft_helper.config import EftSettings
        from plugins.endfield.config import EndfieldSettings
        from plugins.grok_bot.config import GrokSettings
        from plugins.hyw.config import HywSettings
        from plugins.mcsm.config import McsmSettings
        from plugins.minecraft_plugin.config import MinecraftSettings
        from plugins.radar.config import RadarSettings
        from plugins.steamInfo.config import SteamSettings
        from plugins.tibo_radar.config import TiboRadarSettings

        plugins = {"arknights": ArknightsSettings, "bilibilibot": BiliSettings, "eft_helper": EftSettings,
                   "endfield": EndfieldSettings, "grok_bot": GrokSettings, "hyw": HywSettings, "mcsm": McsmSettings,
                   "minecraft_plugin": MinecraftSettings, "radar": RadarSettings, "steamInfo": SteamSettings,
                   "tibo_radar": TiboRadarSettings}
        return CoreSettings, plugins

    def test_every_setting_is_documented_with_its_default(self):
        core, plugins = self.models()
        text = (self.ROOT / "entari.example.yml").read_text(encoding="utf-8")
        with loaded_config_file(text, "") as _, patch("otae_bot.config.model.logger") as log:
            self.assertEqual(settings.core_settings(), core())
            for name, model in plugins.items():
                with self.subTest(plugin=name):
                    self.assertIn(f"plugins.{name}", settings.loaded_config().plugin)
                    self.assertEqual(settings.plugin_settings(name, model), model())
        log.warning.assert_not_called()

        def keys(model, indent):
            for item in dataclasses.fields(model):
                self.assertRegex(text, rf"(?m)^{' ' * indent}(# )?{item.name}:", f"{model.__name__}.{item.name}")
                default = item.default_factory() if item.default_factory is not dataclasses.MISSING else None
                if isinstance(default, Settings):
                    keys(type(default), indent + 2)

        keys(core, 2)
        for model in plugins.values():
            keys(model, 4)

    def test_every_referenced_secret_is_in_env_example(self):
        example = (self.ROOT / "entari.example.yml").read_text(encoding="utf-8")
        env = (self.ROOT / ".env.example").read_text(encoding="utf-8")
        referenced = set(re.findall(r"env\.([A-Z0-9_]+)", example))
        documented = set(re.findall(r"(?m)^(?:# )?([A-Z][A-Z0-9_]+)=", env))
        self.assertTrue(referenced)
        self.assertEqual(sorted(referenced - documented), [])
        for name in settings.PROCESS_ENV:
            if name not in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "HYW_FRONTIER_HOME", "HYW_OPENCODE_SESSION"):
                self.assertIn(name, env)

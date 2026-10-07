"""终末地交互体验改进与指引护栏测试。

涵盖：
1. 失败指引测试：
   - /ef 缺章 等子指令别名作为关键词查询失败时，精准指引到所属父命令（如 /ef 奖章 缺章）；
   - /ef 搜索 缺章 失败时，给出子指令指引与可用查询入口，绝不建议「试试 /ef 搜索 缺章」；
   - 通用查询失败（如不存在的词）在搜索流程下给出可用查询入口，而不是死胡同；
   - 各类别独立子指令（如收集、基建、刷新、抽卡同步等）均能正确指引。
2. 命令示例回归护栏测试：
   - 扫描 plugins/endfield 代码中所有面向用户文本里出现的 /ef 命令示例；
   - 用 parse_command() 解析，断言全部为非 invalid 的合法命令。
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

from plugins.endfield.catalog import commands


class EndfieldGuidanceTests(unittest.TestCase):
    """测试失败提示的路径指引与出路推荐。"""

    def test_medal_missing_alias_guidance_in_format_not_found(self):
        """用户输入 /ef 缺章（或未获得、missing）未找到时，提示其为奖章子指令。"""
        for word in ("缺章", "未获得", "missing"):
            with self.subTest(word=word):
                msg = commands.format_not_found("all", word)
                self.assertIn(f"「{word}」是「/ef 奖章」的子指令", msg)
                self.assertIn("/ef 奖章 缺章 —— 检查当前账号未解锁、未升满或未镀层的奖章", msg)
                self.assertNotIn("可尝试使用 /ef 搜索", msg)

    def test_format_not_found_in_search_action_does_not_suggest_search(self):
        """当处于搜索流程中时，format_not_found 不再建议 /ef 搜索。"""
        msg = commands.format_not_found("all", "某种不存在的条目", action="search")
        self.assertNotIn("可尝试使用 /ef 搜索", msg)
        self.assertIn("可使用的查询入口：", msg)
        for expected in ("/ef 干员", "/ef 武器", "/ef 装备", "/ef 物品", "/ef 道具", "/ef 敌人", "/ef 词条", "/ef 档案"):
            self.assertIn(expected, msg)

    def test_search_medal_missing_in_format_candidates(self):
        """用户执行 /ef 搜索 缺章 且未匹配到条目时，给出出路且指引到奖章子指令。"""
        msg = commands.format_candidates([], title="未匹配到相关条目", scope="all", query="缺章")
        self.assertIn("未匹配到相关条目：缺章。", msg)
        self.assertIn("「缺章」是「/ef 奖章」的子指令，正确写法：/ef 奖章 缺章", msg)
        self.assertIn("可使用的查询入口：", msg)
        self.assertNotIn("可尝试使用 /ef 搜索 缺章", msg)

    def test_search_empty_candidates_provides_exits(self):
        """全局搜索或分类搜索未匹配到结果时，带有明确出路，而非光秃秃一句。"""
        all_msg = commands.format_candidates([], title="未匹配到相关条目", scope="all", query="未知词")
        self.assertIn("未匹配到相关条目：未知词。", all_msg)
        self.assertIn("可使用的查询入口：/ef 干员", all_msg)

        equip_msg = commands.format_candidates([], title="未匹配到相关条目", scope="equipment", query="未知武器")
        self.assertIn("未匹配到相关条目：未知武器。", equip_msg)
        self.assertIn("可发送 /ef 装备 查阅相关条目。", equip_msg)

        item_msg = commands.format_candidates([], title="未匹配到相关条目", scope="item", query="未知材料")
        self.assertIn("未匹配到相关条目：未知材料。", item_msg)
        self.assertIn("可发送 /ef 物品 查阅物品完整目录。", item_msg)

    def test_all_subcommand_guidance_mappings(self):
        """测试统一映射中的各类子指令别名都能生成正确指路。"""
        cases = [
            ("缺章", "/ef 奖章 缺章", "奖章"),
            ("收集", "/ef 档案 收集", "档案"),
            ("进度", "/ef 档案 收集", "档案"),
            ("历史", "/ef 影拓 历史", "影拓"),
            ("刷新", "/ef 奖章 刷新", "奖章"),
            ("抽卡同步", "/ef 抽卡同步", "抽卡"),
            ("抽卡记录", "/ef 抽卡记录", "抽卡"),
            ("抽卡导入", "/ef 抽卡导入", "抽卡"),
            ("添加", "/ef 别名 添加", "别名"),
            ("基建", "/ef 账号 基建", "账号"),
        ]
        for query, expected_cmd, expected_parent in cases:
            with self.subTest(query=query):
                guide = commands.format_subcommand_guidance(query)
                self.assertIn(f"「{query}」是「/ef {expected_parent}」的子指令", guide)
                self.assertIn(f"正确写法：{expected_cmd}", guide)

    def test_original_query_preserved_when_no_subcommand(self):
        """原本不是子指令的正常查询失败时，保留原样提示。"""
        msg = commands.format_not_found("operator", "不存在的干员")
        expected = "未能检索到干员：不存在的干员。\n可尝试使用 /ef 搜索 不存在的干员 进行全局检索。"
        self.assertEqual(msg, expected)


class EndfieldCommandExamplesGuardTests(unittest.TestCase):
    """回归护栏：扫描代码中所有面向用户的 /ef 命令示例，确保均能被正确解析。"""

    def _sanitize_example_to_concrete_command(self, raw_example: str) -> str | None:
        """将包含占位符的命令示例转换成可解析的测试命令。"""
        text = raw_example.split("——")[0].strip()
        text = text.split("（")[0].strip()
        text = text.split("(")[0].strip()
        text = re.sub(r"</?(?:b|small|span|div|p|h\d)>", "", text).strip()
        if not text.startswith("/ef"):
            return None
        text = text.removeprefix("/ef").strip()
        if not text:
            return None

        text = re.sub(r"--source\s*<[^>]+>", "--source akedata", text)
        text = re.sub(r"--池\s*<[^>]+>", "--池 常驻", text)
        text = re.sub(r"\[[^\]]+\]", "", text)
        text = re.sub(r"<[^>]+>", "测试", text)
        if "|" in text:
            text = text.split("|")[0].strip()

        cleaned = " ".join(text.split())
        return cleaned if cleaned else None

    def test_all_user_facing_ef_examples_are_valid(self):
        """扫描 plugins/endfield 代码中所有字符串字面量里的 /ef 指令，断言解析结果非 invalid。"""
        root = Path(__file__).resolve().parents[1] / "plugins/endfield"
        self.assertTrue(root.exists(), f"Endfield path {root} not found")

        checked_count = 0
        failures: list[tuple[str, str, str, str]] = []

        # 匹配 /ef 指令段落
        stop_chars = set("，。！？；、\n\"'”’》")

        for py_file in root.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except Exception:
                continue

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                    if (
                        node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)
                    ):
                        continue
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    val = node.value
                    if "/ef " not in val:
                        continue
                    start = 0
                    while True:
                        idx = val.find("/ef ", start)
                        if idx == -1:
                            break
                        end = idx + 4
                        while end < len(val) and val[end] not in stop_chars:
                            end += 1
                        raw = val[idx:end].strip()
                        start = end
                        cleaned = self._sanitize_example_to_concrete_command(raw)
                        if not cleaned:
                            continue
                        parsed = commands.parse_command(cleaned)
                        checked_count += 1
                        if parsed.action == "invalid":
                            failures.append((raw, cleaned, py_file.name, parsed.error))

        self.assertGreater(checked_count, 50, f"应扫描到至少 50 个命令示例，实际仅扫描到 {checked_count} 个")
        if failures:
            details = "\n".join(
                f"文件 {fname}: 原文 {raw!r} -> 清理后 {cleaned!r} -> 错误: {err}"
                for raw, cleaned, fname, err in failures
            )
            self.fail(f"发现 {len(failures)} 个无法解析的命令示例：\n{details}")

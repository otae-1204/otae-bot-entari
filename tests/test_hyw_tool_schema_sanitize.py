"""Strict-gateway tool parameters schema sanitization (Gemini/OpenAI-compat safe)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTIER = ROOT / "vendor" / "hyw-frontier" / "hyw_frontier"

import importlib.util

def _load(name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(name, FRONTIER / file_name)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

_schema = _load("hyw_frontier_schema_sanitize", "schema_sanitize.py")
sanitize_function_parameters_schema = _schema.sanitize_function_parameters_schema
sanitize_tool_definition = _schema.sanitize_tool_definition


def _walk_issues(obj, path="$"):
    issues = []
    if isinstance(obj, dict):
        props, req = obj.get("properties"), obj.get("required")
        if isinstance(req, list) and req:
            if not isinstance(props, dict) or not props:
                issues.append((path, "required_with_empty_properties", list(req)))
            else:
                missing = [name for name in req if name not in props]
                if missing:
                    issues.append((path, "required_not_in_properties", missing))
        for key, value in obj.items():
            issues.extend(_walk_issues(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            issues.extend(_walk_issues(value, f"{path}[{index}]"))
    return issues


class SanitizeFunctionParametersTests(unittest.TestCase):
    def test_empty_properties_with_required_materializes_from_parent(self):
        raw = {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "url": {"type": "string"},
            },
            "anyOf": [
                {"type": "object", "required": ["source_id"], "not": {"required": ["url"]}},
                {"type": "object", "required": ["url"], "not": {"required": ["source_id"]}},
            ],
            "additionalProperties": False,
        }
        self.assertTrue(_walk_issues(raw))
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_walk_issues(clean), [])
        branch0 = clean["anyOf"][0]
        self.assertIn("source_id", branch0["properties"])
        self.assertEqual(branch0["required"], ["source_id"])
        # nested not:{required} also gets properties
        self.assertIn("url", branch0["not"]["properties"])
        self.assertEqual(branch0["not"]["required"], ["url"])

    def test_any_of_required_out_of_branch_is_fixed(self):
        raw = {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "pageshot_id": {"type": "string"},
                "bbox": {"type": "array", "items": {"type": "integer"}},
            },
            "anyOf": [
                {
                    "type": "object",
                    "required": ["url"],
                    "not": {"anyOf": [{"required": ["pageshot_id"]}, {"required": ["bbox"]}]},
                },
                {"type": "object", "required": ["pageshot_id", "bbox"], "not": {"required": ["url"]}},
            ],
        }
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_walk_issues(clean), [])
        self.assertEqual(set(clean["anyOf"][1]["properties"]), {"pageshot_id", "bbox"})

    def test_normal_openai_object_schema_unchanged(self):
        raw = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(clean, raw)
        self.assertIsNot(clean, raw)

    def test_required_without_any_known_property_is_dropped(self):
        raw = {"type": "object", "required": ["ghost"], "properties": {}}
        clean = sanitize_function_parameters_schema(raw)
        self.assertNotIn("required", clean)
        self.assertEqual(_walk_issues(clean), [])

    def test_sanitize_tool_definition_only_touches_parameters(self):
        tool = {
            "name": "demo",
            "label": "演示",
            "description": "d",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "anyOf": [{"type": "object", "required": ["a"]}],
            },
        }
        out = sanitize_tool_definition(tool)
        self.assertEqual(out["name"], "demo")
        self.assertEqual(out["label"], "演示")
        self.assertEqual(_walk_issues(out["parameters"]), [])
        self.assertIn("a", out["parameters"]["anyOf"][0]["properties"])


class ToolsJsonRegistryTests(unittest.TestCase):
    def test_checked_in_tools_json_has_no_poisonous_shapes(self):
        tools = json.loads((FRONTIER / "tools.json").read_text(encoding="utf-8"))
        for tool in tools:
            issues = _walk_issues(tool["parameters"])
            self.assertEqual(issues, [], msg=f"{tool['name']}: {issues}")

    def _exclusive_tools(self):
        tools = {t["name"]: t for t in json.loads((FRONTIER / "tools.json").read_text(encoding="utf-8"))}
        return {name: tools[name] for name in ("reverse_image_search", "jina_pageshot")}

    def test_exclusive_tools_use_anyof_with_local_properties(self):
        for name, tool in self._exclusive_tools().items():
            params = tool["parameters"]
            self.assertTrue(params.get("properties"), msg=name)
            self.assertIn("anyOf", params, msg=name)
            self.assertFalse(params.get("additionalProperties", True), msg=name)
            branch_props = set()
            for branch in params["anyOf"]:
                self.assertTrue(branch.get("properties"), msg=name)
                self.assertTrue(branch.get("required"), msg=name)
                self.assertLessEqual(set(branch["required"]), set(branch["properties"]), msg=name)
                self.assertFalse(branch.get("additionalProperties", True), msg=name)
                branch_props |= set(branch["properties"])
            self.assertEqual(branch_props, set(params["properties"]), msg=name)

    def test_exclusive_tools_avoid_oneof_and_not(self):
        def keys(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    yield key
                    yield from keys(value)
            elif isinstance(obj, list):
                for value in obj:
                    yield from keys(value)

        for name, tool in self._exclusive_tools().items():
            found = set(keys(tool["parameters"]))
            self.assertNotIn("oneOf", found, msg=name)
            self.assertNotIn("not", found, msg=name)

    def test_sanitizer_is_noop_on_exclusive_tools(self):
        for name, tool in self._exclusive_tools().items():
            clean = sanitize_tool_definition(tool)
            self.assertEqual(clean["parameters"], tool["parameters"], msg=name)
            self.assertEqual(_walk_issues(clean["parameters"]), [], msg=name)


if __name__ == "__main__":
    unittest.main()

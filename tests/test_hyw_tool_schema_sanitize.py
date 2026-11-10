"""Strict-gateway tool parameters schema sanitization (Gemini/OpenAI-compat safe).

Gemini behind OpenAI-compatible gateways answers HTTP 400 when ``anyOf`` shares a node
with any other field ("When using any_of, it must be the only field set"), so neither
the checked-in registry nor anything the sanitizer emits may contain that shape.
"""
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

EXCLUSIVE_TOOLS = ("reverse_image_search", "jina_pageshot")


def _registry() -> list[dict]:
    return json.loads((FRONTIER / "tools.json").read_text(encoding="utf-8"))


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


def _combinator_issues(node, path="$", *, root=True):
    """Schema-aware walk: property *names* are never mistaken for keywords."""
    issues = []
    if isinstance(node, list):
        for index, value in enumerate(node):
            issues.extend(_combinator_issues(value, f"{path}[{index}]", root=False))
        return issues
    if not isinstance(node, dict):
        return issues
    if "anyOf" in node and (root or len(node) > 1):
        issues.append((path, "anyOf_with_siblings", sorted(set(node) - {"anyOf"})))
    for keyword in ("oneOf", "allOf", "not"):
        if keyword in node:
            issues.append((path, keyword))
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            for name, decl in value.items():
                issues.extend(_combinator_issues(decl, f"{path}.properties.{name}", root=False))
        elif isinstance(value, (dict, list)):
            issues.extend(_combinator_issues(value, f"{path}.{key}", root=False))
    return issues


def _strict_issues(schema):
    return _walk_issues(schema) + _combinator_issues(schema)


class SanitizeFunctionParametersTests(unittest.TestCase):
    def test_any_of_beside_properties_is_flattened_into_a_union(self):
        raw = {
            "type": "object",
            "description": "demo",
            "properties": {"a": {"type": "string", "description": "parent"}},
            "anyOf": [
                {"type": "object", "properties": {"a": {"type": "string", "description": "branch"},
                                                   "b": {"type": "integer"}},
                 "required": ["a"], "additionalProperties": False},
                {"type": "object", "properties": {"c": {"type": "array", "items": {"type": "string"}}},
                 "required": ["c"], "additionalProperties": False},
            ],
            "additionalProperties": False,
        }
        self.assertTrue(_combinator_issues(raw))
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_strict_issues(clean), [])
        self.assertNotIn("anyOf", clean)
        self.assertEqual(list(clean["properties"]), ["a", "b", "c"])
        self.assertEqual(clean["properties"]["a"]["description"], "parent")  # parent decl wins
        self.assertEqual(clean["properties"]["b"], {"type": "integer"})
        self.assertEqual(clean["properties"]["c"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(clean["type"], "object")
        self.assertEqual(clean["description"], "demo")
        self.assertIs(clean["additionalProperties"], False)
        self.assertNotIn("required", clean)  # alternatives never become unconditionally required
        self.assertIn("anyOf", raw)  # input untouched

    def test_node_required_survives_flattening_and_gets_its_properties(self):
        raw = {
            "type": "object", "required": ["b"],
            "anyOf": [{"properties": {"a": {"type": "string"}}, "required": ["a"]},
                      {"properties": {"b": {"type": "string"}}, "required": ["b"]}],
        }
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_strict_issues(clean), [])
        self.assertEqual(clean["required"], ["b"])
        self.assertEqual(set(clean["properties"]), {"a", "b"})

    def test_root_any_of_alone_is_flattened_to_a_plain_object(self):
        raw = {"anyOf": [{"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
                         {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]}]}
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(clean, {"type": "object",
                                 "properties": {"x": {"type": "string"}, "y": {"type": "string"}}})

    def test_nested_any_of_beside_description_is_flattened(self):
        raw = {
            "type": "object",
            "properties": {
                "tag": {"description": "optional tag",
                        "anyOf": [{"type": "null"}, {"type": "string", "maxLength": 5}]},
                "deep": {"type": "array", "items": {
                    "type": "object", "properties": {"k": {"type": "string"}},
                    "anyOf": [{"required": ["k"]}, {"properties": {"v": {"type": "integer"}}, "required": ["v"]}],
                }},
            },
        }
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_strict_issues(clean), [])
        self.assertEqual(clean["properties"]["tag"], {"description": "optional tag", "type": "string", "maxLength": 5})
        self.assertEqual(clean["properties"]["deep"]["items"],
                         {"type": "object", "properties": {"k": {"type": "string"}, "v": {"type": "integer"}}})

    def test_lone_nested_any_of_is_kept_and_its_branches_are_cleaned(self):
        raw = {"type": "object", "properties": {
            "value": {"anyOf": [{"type": "string"}, {"type": "object", "required": ["ghost"]}]}}}
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_strict_issues(clean), [])
        self.assertEqual(clean["properties"]["value"], {"anyOf": [{"type": "string"}, {"type": "object"}]})

    def test_one_of_all_of_and_not_never_survive(self):
        raw = {
            "type": "object",
            "properties": {"url": {"type": "string"}, "pageshot_id": {"type": "string"},
                           "mode": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
            "allOf": [{"required": ["url"]}],
            "oneOf": [{"properties": {"bbox": {"type": "array", "items": {"type": "integer"}}},
                       "not": {"required": ["url"]}}],
            "not": {"anyOf": [{"required": ["pageshot_id"]}, {"required": ["bbox"]}]},
        }
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_strict_issues(clean), [])
        self.assertEqual(set(clean["properties"]), {"url", "pageshot_id", "mode", "bbox"})
        self.assertEqual(clean["properties"]["mode"], {"type": "string"})
        self.assertEqual(clean["required"], ["url"])  # allOf applies unconditionally

    def test_legacy_exclusive_shapes_from_v1_37_4_are_flattened(self):
        """The exact parameters shapes Gemini rejected (anyOf beside type/properties/additionalProperties)."""
        reverse = {
            "type": "object",
            "properties": {"source_id": {"type": "string"}, "url": {"type": "string"}, "page": {"type": "integer"}},
            "anyOf": [
                {"type": "object", "properties": {"source_id": {"type": "string"}, "page": {"type": "integer"}},
                 "required": ["source_id"], "additionalProperties": False},
                {"type": "object", "properties": {"url": {"type": "string"}, "page": {"type": "integer"}},
                 "required": ["url"], "additionalProperties": False},
            ],
            "additionalProperties": False,
        }
        clean = sanitize_function_parameters_schema(reverse)
        self.assertEqual(_strict_issues(clean), [])
        self.assertEqual(clean, {"type": "object", "properties": reverse["properties"], "additionalProperties": False})

    def test_empty_properties_with_required_is_still_dropped(self):
        raw = {"type": "object", "required": ["ghost"], "properties": {}}
        clean = sanitize_function_parameters_schema(raw)
        self.assertNotIn("required", clean)
        self.assertEqual(_strict_issues(clean), [])

    def test_lone_any_of_branch_required_without_properties_is_fixed(self):
        raw = {"type": "object", "properties": {"v": {"anyOf": [
            {"type": "object", "required": ["x"]},
            {"type": "object", "properties": {}, "required": ["y"]},
        ]}}}
        self.assertTrue(_walk_issues(raw))
        clean = sanitize_function_parameters_schema(raw)
        self.assertEqual(_strict_issues(clean), [])

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

    def test_sanitizer_is_idempotent(self):
        raw = {"type": "object", "properties": {"a": {"type": "string"}},
               "anyOf": [{"required": ["a"]}, {"properties": {"b": {"type": "string"}}, "required": ["b"]}]}
        once = sanitize_function_parameters_schema(raw)
        self.assertEqual(sanitize_function_parameters_schema(once), once)

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
        self.assertEqual(out["description"], "d")
        self.assertEqual(_strict_issues(out["parameters"]), [])
        self.assertEqual(out["parameters"], {"type": "object", "properties": {"a": {"type": "string"}}})
        self.assertIn("anyOf", tool["parameters"])


class ToolsJsonRegistryTests(unittest.TestCase):
    def test_checked_in_tools_json_has_no_poisonous_shapes(self):
        for tool in _registry():
            issues = _strict_issues(tool["parameters"])
            self.assertEqual(issues, [], msg=f"{tool['name']}: {issues}")

    def test_sanitized_registry_has_no_poisonous_shapes(self):
        for tool in _registry():
            clean = sanitize_tool_definition(tool)
            issues = _strict_issues(clean["parameters"])
            self.assertEqual(issues, [], msg=f"{tool['name']}: {issues}")

    def test_sanitizer_is_noop_on_checked_in_tools(self):
        for tool in _registry():
            clean = sanitize_tool_definition(tool)
            self.assertEqual(clean["parameters"], tool["parameters"], msg=tool["name"])

    def test_exclusive_tools_are_flat_objects(self):
        tools = {tool["name"]: tool for tool in _registry()}
        expected = {"reverse_image_search": {"source_id", "url", "page"},
                    "jina_pageshot": {"url", "pageshot_id", "bbox"}}
        for name in EXCLUSIVE_TOOLS:
            params = tools[name]["parameters"]
            self.assertEqual(params["type"], "object", msg=name)
            self.assertEqual(set(params["properties"]), expected[name], msg=name)
            self.assertIs(params["additionalProperties"], False, msg=name)
            # Exclusivity is enforced at runtime, never via required/anyOf/oneOf/not.
            self.assertEqual(set(params), {"type", "properties", "additionalProperties"}, msg=name)

    def test_exclusive_tools_describe_the_exclusivity(self):
        tools = {tool["name"]: tool for tool in _registry()}
        reverse = tools["reverse_image_search"]["description"]
        self.assertIn("source_id 与 url 必须二选一", reverse)
        self.assertIn("不能都不给", reverse)
        self.assertIn("不能同时给", reverse)
        self.assertIn("page 可选", reverse)
        pageshot = tools["jina_pageshot"]["description"]
        self.assertIn("互斥", pageshot)
        self.assertIn("首次抓取只传 url", pageshot)
        self.assertIn("只传 pageshot_id 与 bbox", pageshot)
        self.assertIn("不能混用", pageshot)


try:
    from hyw_frontier.tools import SEARCH_PROVIDERS, tool_definitions
except ImportError:  # pragma: no cover - the vendored package is not installed
    tool_definitions = None


@unittest.skipIf(tool_definitions is None, "hyw_frontier is not importable")
class ExportedToolDefinitionsTests(unittest.TestCase):
    """What is actually sent upstream, for every search provider."""

    def test_every_exported_schema_is_gateway_safe(self):
        for provider in (None, *SEARCH_PROVIDERS):
            for tool in tool_definitions(provider):
                with self.subTest(provider=provider, tool=tool["name"]):
                    self.assertEqual(_strict_issues(tool["parameters"]), [])
                    self.assertEqual(tool["parameters"]["type"], "object")

    def test_exclusive_tools_are_exported(self):
        names = {tool["name"] for tool in tool_definitions(None)}
        self.assertLessEqual(set(EXCLUSIVE_TOOLS), names)


if __name__ == "__main__":
    unittest.main()

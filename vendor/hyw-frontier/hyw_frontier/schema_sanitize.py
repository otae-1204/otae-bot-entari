"""Normalize tool function-parameters JSON Schema for strict OpenAI-compatible gateways.

Some upstreams (notably Gemini via OpenAI-compat proxies) reject schemas where:
- ``required`` is non-empty but the same object has empty/missing ``properties``
- an ``anyOf``/``oneOf``/``allOf`` branch ``required`` names a property not
  declared on that branch

Those shapes are legal JSON Schema when parent ``properties`` apply alongside
``anyOf``, but they break strict function_declarations validators. This module
materializes inherited property decls onto such nodes and drops impossible
``required`` entries. It is provider-agnostic: every model gets a stricter,
self-contained schema.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def sanitize_function_parameters_schema(schema: Any) -> Any:
    """Return a deep-copied parameters schema safe for strict tool gateways."""
    if not isinstance(schema, dict):
        return deepcopy(schema)
    return _sanitize_node(deepcopy(schema), inherited_properties={})


def sanitize_tool_definition(tool: dict) -> dict:
    """Copy a tool definition and sanitize its ``parameters`` object."""
    out = dict(tool)
    if "parameters" in out:
        out["parameters"] = sanitize_function_parameters_schema(out["parameters"])
    return out


def _sanitize_node(node: dict, inherited_properties: dict) -> dict:
    props = node.get("properties")
    if isinstance(props, dict):
        effective = {**inherited_properties, **props}
    else:
        effective = dict(inherited_properties)

    for key in ("anyOf", "oneOf", "allOf"):
        branches = node.get(key)
        if isinstance(branches, list):
            node[key] = [
                _sanitize_node(branch, effective) if isinstance(branch, dict) else branch
                for branch in branches
            ]

    if isinstance(node.get("not"), dict):
        node["not"] = _sanitize_node(node["not"], effective)

    if isinstance(node.get("items"), dict):
        node["items"] = _sanitize_node(node["items"], {})
    elif isinstance(node.get("items"), list):
        node["items"] = [
            _sanitize_node(item, {}) if isinstance(item, dict) else item
            for item in node["items"]
        ]

    if isinstance(props, dict):
        node["properties"] = {
            name: _sanitize_node(value, {}) if isinstance(value, dict) else value
            for name, value in props.items()
        }
        effective = {**inherited_properties, **node["properties"]}

    _materialize_required(node, effective)
    return node


def _materialize_required(node: dict, effective_properties: dict) -> None:
    required = node.get("required")
    if not isinstance(required, list) or not required:
        if required == []:
            node.pop("required", None)
        return

    props = node.get("properties")
    if not isinstance(props, dict):
        props = {}

    changed = False
    for name in required:
        if name not in props and name in effective_properties:
            props[name] = deepcopy(effective_properties[name])
            changed = True
    if changed or (props and "properties" not in node):
        node["properties"] = props

    if isinstance(node.get("properties"), dict) and node["properties"]:
        node["required"] = [name for name in required if name in node["properties"]]
        if not node["required"]:
            node.pop("required", None)
        return

    # Still empty properties with required → drop required (gateway-poisonous).
    node.pop("required", None)
    if node.get("properties") == {}:
        node.pop("properties", None)

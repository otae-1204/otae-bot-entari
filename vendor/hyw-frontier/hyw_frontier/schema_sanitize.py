"""Normalize tool function-parameters JSON Schema for strict OpenAI-compatible gateways.

Some upstreams (notably Gemini via OpenAI-compat proxies, also Aliyun / NewAPI relays)
reject schemas where:
- ``anyOf`` sits beside any other field on the same node (``type``, ``properties``,
  ``required``, ``additionalProperties``, ``description`` ...). Gemini answers HTTP 400
  "When using any_of, it must be the only field set".
- ``oneOf`` / ``allOf`` / ``not`` appear at all (outside the OpenAPI subset they accept)
- ``required`` is non-empty but the same object has empty/missing ``properties``
- an ``anyOf`` branch ``required`` names a property not declared on that branch

Strategy: never keep ``anyOf`` next to sibling fields. Such a node (and any root
``anyOf``, since function ``parameters`` must be a plain object) is flattened: branch
``properties`` are merged into the node as a union, branch-local ``required`` is dropped
(branches are alternatives, so none of them is unconditionally required), and other
branch keywords only fill fields the node does not set itself. ``oneOf`` is flattened the
same way, ``allOf`` is merged (keeping its ``required``) and ``not`` is dropped. Mutual
exclusion that the flat schema can no longer express must be enforced by the tool runtime
before doing any work (see ``tools.exclusive_arguments_error``).

The remaining rules materialize inherited property decls onto nodes whose ``required``
would otherwise point at nothing, and drop impossible ``required`` entries. The module is
provider-agnostic: every model gets the same strict, self-contained schema.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

# Branch keywords that must never be copied onto the flattened node.
_BRANCH_ONLY = frozenset({"required", "anyOf", "oneOf", "allOf", "not"})


def sanitize_function_parameters_schema(schema: Any) -> Any:
    """Return a deep-copied parameters schema safe for strict tool gateways."""
    if not isinstance(schema, dict):
        return deepcopy(schema)
    return _sanitize_node(deepcopy(schema), inherited_properties={}, root=True)


def sanitize_tool_definition(tool: dict) -> dict:
    """Copy a tool definition and sanitize its ``parameters`` object."""
    out = dict(tool)
    if "parameters" in out:
        out["parameters"] = sanitize_function_parameters_schema(out["parameters"])
    return out


def _flatten(node: dict, *, root: bool) -> None:
    """Remove combinators strict gateways reject; only a lone non-root ``anyOf`` survives."""
    node.pop("not", None)
    for key in ("allOf", "oneOf", "anyOf"):
        branches = node.get(key)
        if not isinstance(branches, list):
            continue
        if key == "anyOf" and not root and len(node) == 1:
            continue  # anyOf as the only field is what Gemini accepts
        del node[key]
        _merge_branches(node, branches, keep_required=key == "allOf")


def _merge_branches(node: dict, branches: list, *, keep_required: bool) -> None:
    """Merge branches into ``node``: properties as a union, node fields win over branch fields.

    Branch ``required`` only survives for ``allOf`` (every branch applies); alternatives of
    ``anyOf``/``oneOf`` never make a property unconditionally required.
    """
    branches = [branch for branch in branches if isinstance(branch, dict)]
    for branch in branches:
        _flatten(branch, root=True)  # nested combinators would otherwise be dropped below
    had_properties = isinstance(node.get("properties"), dict)
    merged = dict(node["properties"]) if had_properties else {}
    required = list(node["required"]) if isinstance(node.get("required"), list) else []
    # A "null" alternative must not decide the merged node's type.
    for branch in sorted(branches, key=lambda branch: branch.get("type") == "null"):
        for name, value in branch.items():
            if name == "properties" and isinstance(value, dict):
                for prop, decl in value.items():
                    merged.setdefault(prop, decl)
            elif name == "required":
                if keep_required and isinstance(value, list):
                    required += [prop for prop in value if prop not in required]
            elif name not in _BRANCH_ONLY:
                node.setdefault(name, value)
    if merged or had_properties:
        node["properties"] = merged
    if merged:
        node.setdefault("type", "object")
    if required:
        node["required"] = required


def _sanitize_node(node: dict, inherited_properties: dict, *, root: bool = False) -> dict:
    _flatten(node, root=root)

    props = node.get("properties")
    if isinstance(props, dict):
        effective = {**inherited_properties, **props}
    else:
        effective = dict(inherited_properties)

    # Only a lone anyOf (no sibling fields) can still be here.
    if isinstance(node.get("anyOf"), list):
        node["anyOf"] = [
            _sanitize_node(branch, effective) if isinstance(branch, dict) else branch
            for branch in node["anyOf"]
        ]

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

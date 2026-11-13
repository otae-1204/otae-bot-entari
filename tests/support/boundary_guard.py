"""Boundary guard: platform-protocol code outside ``otae_bot/boundary/`` and layering.

Inside the bot only Satori should exist; OneBot actions, HTTP side channels,
framework private APIs and platform-name branching belong to the boundary
(rules R1-R8). Media leave the bot only as uploads or inline data, built from
bytes; local paths in media elements and direct ``upload.create`` calls are
only allowed in the outbound pipeline and the boundary (rule R9). Imports must follow the layering in
``docs/development/architecture_conventions.md`` (rules L1-L4), and nothing may still use
the NoneBot-style compatibility layer ``otae_bot/compat`` (rule C1). The checks
are AST heuristics run over ``otae_bot/`` and ``plugins/``.

Today they are report-only: the current inventory is the checked-in baseline,
new violations fail, and fixed ones must be removed from the baseline (it may
only shrink).

    python -m tests.support.boundary_guard --report   # inventory with line numbers
    python -m tests.support.boundary_guard --write    # regenerate the baseline
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("otae_bot", "plugins")
EXEMPT = ("otae_bot/boundary/",)
BASELINE = ROOT / "tests/fixtures/boundary_guard_baseline.json"

RULES = {
    "R1": "onebot-action",
    "R2": "internal-call",
    "R3": "onebot-http",
    "R4": "framework-private",
    "R5": "numeric-id",
    "R6": "platform-name",
    "R7": "event-class-name",
    "R8": "direct-env-read",
    "R9": "local-media-path",
    "L1": "plugin-cross-import",
    "L2": "boundary-import",
    "L3": "infrastructure-upward-import",
    "L4": "core-imports-plugins",
    "C1": "compat-import",
}

# L2: only the boundary itself and the module that wires it may import these.
BOUNDARY_MODULES = ("otae_bot.boundary", "satori.adapters", "entari_plugin_server")
BOUNDARY_WIRING = "otae_bot/application.py"  # the single exception: it loads the server plugin
# L3: the bottom layers depend on the standard library and third-party code only.
LOWER_LAYERS = ("otae_bot/infrastructure/", "otae_bot/config/")
LOWER_ALLOWED = ("otae_bot.infrastructure", "otae_bot.config")
LOWER_FORBIDDEN = ("arclet.entari", "arclet.letoderea", "satori", "plugins")
# otae_bot/config is where settings bind to Entari's config API; nothing else of Entari.
CONFIG_LAYER = "otae_bot/config/"
CONFIG_FRAMEWORK_API = "arclet.entari.config"

# C1: the compatibility layer is being removed; only its own modules may import it.
COMPAT_MODULE = "otae_bot.compat"
COMPAT_LAYER = "otae_bot/compat/"

# R8: settings come from otae_bot.config (entari.yml / .env); only it reads the environment.
ENV_READERS = frozenset({"getenv", "environ", "getenvb", "environb"})
ENV_EXEMPT = ("otae_bot/config/",)

# R1: always flagged wherever they appear (outside docstrings).
KNOWN_ACTIONS = frozenset({
    "send_group_forward_msg", "send_private_forward_msg", "get_group_msg_history",
    "get_friend_msg_history", "get_forward_msg", "get_msg", "get_group_member_info",
    "get_group_member_list", "get_group_system_msg", "get_group_ignore_add_request",
    "set_group_add_request", "set_friend_add_request", "approve_group_invite",
    "set_group_invite", "upload_group_file", "upload_private_file",
})
# Anything else shaped like an action is flagged only in an "action position"
# of a module that mentions internal/onebot; otherwise names such as
# "set_default_peek" (a command) or "upload_dir" (an MCSM field) would match.
ACTION_SHAPE = re.compile(r"^(onebot11/)?(get|set|send|upload|delete|approve)_[a-z_]+$")
ACTION_CALLEE = re.compile(r"internal|action|api|onebot", re.I)

INTERNAL_METHODS = frozenset({"internal", "request_internal"})
ONEBOT_HTTP_NAMES = frozenset({
    "ONEBOT_HTTP_URL", "LLONEBOT_HTTP_URL", "ONEBOT_ACCESS_TOKEN", "onebot_url", "onebot_token",
})
ONEBOT_MODULES = ("otae_bot.adapters.onebot", "utils.onebot_api")

FRAMEWORKS = ("arclet.entari", "arclet.letoderea")
# Public-looking names that are still framework internals.
FRAMEWORK_INTERNAL_NAMES = frozenset({"global_propagators"})
SCOPE_LISTS = frozenset({"subscribers", "propagators"})

# R5 keys on the id expression: a bare name or attribute ending in one of
# these, `x.user.id`-style chains, `d["user_id"]` / `d.get("user_id")`
# lookups; and on the destination: `group_id=int(x)` / `{"user_id": int(x)}`.
ID_SUFFIXES = ("user_id", "group_id", "guild_id", "channel_id", "self_id")
ID_EXACT = frozenset({"peer", "uin", "qq"})
ID_OWNERS = frozenset({"user", "guild", "channel", "member", "self", "account", "login", "operator"})

# R9: media elements built from local paths; the outbound pipeline (and the boundary)
# are the only places that may turn bytes into uploads.
MEDIA_EXEMPT = ("otae_bot/pipeline/", "otae_bot/boundary/")
MEDIA_CLASSES = frozenset({"Image", "Audio", "Video", "File"})
MEDIA_SOURCES = frozenset({"src", "url"})
UPLOAD_NAMES = frozenset({"upload_create", "UPLOAD_CREATE"})

# R6: receivers whose string comparison means "which platform/adapter is this".
PLATFORM_ATTRS = frozenset({"platform", "adapter"})
PLATFORM_CALL = re.compile(r"adapter|platform", re.I)


class Violation(NamedTuple):
    rule: str
    file: str
    line: int
    snippet: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.file, self.snippet)


def _name(node: ast.AST) -> str:
    """Last identifier of a Name/Attribute/Call chain ('' if none)."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _strings(node: ast.AST) -> list[str]:
    """String constants of a literal or a literal collection."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return [item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    return []


def _is_platform_id(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and _name(node.func) == "str" and node.args:
        return _is_platform_id(node.args[0])
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = _name(node)
        if name.endswith(ID_SUFFIXES) or name in ID_EXACT:
            return True
        return name == "id" and isinstance(node, ast.Attribute) and _name(node.value) in ID_OWNERS
    key = None
    if isinstance(node, ast.Subscript):
        key = node.slice
    elif isinstance(node, ast.Call) and _name(node.func) == "get" and node.args:
        key = node.args[0]
    return bool(key is not None and any(s.endswith(ID_SUFFIXES) or s in ID_EXACT for s in _strings(key)))


def _is_platform_receiver(node: ast.AST) -> bool:
    # Strip normalisers such as `.casefold()`, `.lower()` and `str(...)`.
    while isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"casefold", "lower", "strip"}:
            node = node.func.value
        elif _name(node.func) == "str" and node.args:
            node = node.args[0]
        elif isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
            return bool(set(_strings(node.args[1])) & PLATFORM_ATTRS)
        else:
            return bool(PLATFORM_CALL.search(_name(node.func)))
    if isinstance(node, ast.BoolOp):
        return any(_is_platform_receiver(value) for value in node.values)
    if isinstance(node, ast.Attribute) and node.attr in PLATFORM_ATTRS:
        return not (isinstance(node.value, ast.Name) and node.value.id == "sys")  # sys.platform is the OS
    return False


def _within(name: str, prefixes: tuple[str, ...] | str) -> bool:
    """``name`` is one of ``prefixes`` or a submodule of one."""
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def _module_of(rel: str) -> list[str]:
    """Package path of a scanned file, for resolving relative imports."""
    parts = rel[:-3].split("/")
    return parts[:-1] if parts[-1] == "__init__" else parts


def _imported(rel: str, node: ast.Import | ast.ImportFrom) -> list[str]:
    """Absolute module names an import statement may load (relative ones resolved)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    module = node.module or ""
    if node.level:
        package = _module_of(rel)
        if not rel.endswith("__init__.py"):
            package = package[:-1]
        base = package[: len(package) - (node.level - 1)]
        module = ".".join([*base, module] if module else base)
    # `from pkg import sub` may load the submodule pkg.sub.
    return [module, *(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")]


def _is_class_name(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Attribute) and node.attr == "__name__"):
        return False
    owner = node.value
    return (isinstance(owner, ast.Attribute) and owner.attr == "__class__") or (
        isinstance(owner, ast.Call) and _name(owner.func) == "type")


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, source: str, protocol: bool = True):
        self.rel = rel
        # R1-R7 do not apply inside the boundary; the layering rules apply everywhere.
        self.protocol = protocol
        self.lines = source.splitlines()
        self.mentions_protocol = bool(re.search(r"internal|onebot", source, re.I))
        self.found: dict[tuple[str, int], Violation] = {}
        self.parents: dict[ast.AST, ast.AST] = {}
        self.docstrings: set[ast.AST] = set()
        self.media = not rel.startswith(MEDIA_EXEMPT)
        self.media_classes = set(MEDIA_CLASSES)  # plus aliases such as `Image as EntariImage`

    def run(self, tree: ast.AST) -> list[Violation]:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
            body = getattr(parent, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                self.docstrings.add(body[0].value)
        self.visit(tree)
        return list(self.found.values())

    def add(self, rule: str, node: ast.AST) -> None:
        if rule.startswith("R") and not self.protocol:
            return
        if rule == "R9" and not self.media:
            return
        line = node.lineno
        snippet = " ".join(self.lines[line - 1].split())[:80]
        # One entry per rule and line keeps the inventory readable.
        self.found.setdefault((rule, line), Violation(RULES[rule], self.rel, line, snippet))

    # R1, R2, R3: string literals
    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or node in self.docstrings:
            return
        value = node.value
        if value in KNOWN_ACTIONS or value.startswith("onebot11/") or (
                ACTION_SHAPE.match(value) and self.mentions_protocol and self._action_position(node)):
            self.add("R1", node)
        # Third-party URLs (e.g. bilibili's /x/internal/) are not Satori API paths.
        if "internal/" in value and not value.startswith(("http://", "https://")):
            self.add("R2", node)
        if value in ONEBOT_HTTP_NAMES:
            self.add("R3", node)

    def _action_position(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        if isinstance(parent, ast.keyword):
            return parent.arg == "action"
        if isinstance(parent, ast.Call) and node in parent.args:
            return bool(ACTION_CALLEE.search(_name(parent.func)))
        if isinstance(parent, ast.Tuple):
            # `(action, params)` pairs and `action, key = "...", "..."` unpacking.
            outer = self.parents.get(parent)
            if isinstance(outer, ast.Assign):
                return any(isinstance(t, ast.Tuple) and any(_name(e) == "action" for e in t.elts) for t in outer.targets)
            return parent.elts[0] is node
        if isinstance(parent, ast.Assign):
            return any(_name(target) == "action" for target in parent.targets)
        return False

    # R2, R4, R5: calls
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in INTERNAL_METHODS:
            self.add("R2", node)
        if isinstance(func, ast.Name) and func.id in {"getattr", "hasattr", "setattr"} and len(node.args) >= 2:
            names = set(_strings(node.args[1]))
            if names & INTERNAL_METHODS:
                self.add("R2", node)
            if "_origin" in names:
                self.add("R4", node)
        if isinstance(func, ast.Name) and func.id == "int" and node.args and (
                _is_platform_id(node.args[0]) or self._id_destination(node)):
            self.add("R5", node)
        if isinstance(func, ast.Attribute) and func.attr == "of" and any(k.arg == "path" for k in node.keywords):
            self.add("R9", node)  # Image.of(path=...) → file:// src
        if isinstance(func, ast.Name) and func.id in {"getattr", "hasattr"} and len(node.args) >= 2 \
                and set(_strings(node.args[1])) & UPLOAD_NAMES:
            self.add("R9", node)
        if self._media_call(node):
            for value in [*node.args, *(k.value for k in node.keywords if k.arg in MEDIA_SOURCES)]:
                if self._local_source(value):
                    self.add("R9", node)
        self.generic_visit(node)

    def _media_call(self, node: ast.Call) -> bool:
        """``Image(...)``, ``Image.of(...)``, ``Image.unpack(...)`` and their aliases."""
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"of", "unpack"}:
            func = func.value
        return _name(func) in self.media_classes

    @staticmethod
    def _local_source(node: ast.AST) -> bool:
        """``x.as_uri()``, or a literal / f-string starting with ``file:``."""
        if isinstance(node, ast.Call) and _name(node.func) == "as_uri":
            return True
        if isinstance(node, ast.JoinedStr) and node.values:
            node = node.values[0]
        return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower().startswith("file:")

    def _id_destination(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        if isinstance(parent, ast.keyword):
            return bool(parent.arg) and parent.arg.endswith(ID_SUFFIXES)
        if isinstance(parent, ast.Dict) and node in parent.values:
            key = parent.keys[parent.values.index(node)]
            return key is not None and any(s.endswith(ID_SUFFIXES) for s in _strings(key))
        return False

    # L1-L4: import direction
    def _layering(self, node: ast.Import | ast.ImportFrom) -> None:
        rel = self.rel
        names = _imported(rel, node)
        if isinstance(node, ast.ImportFrom) and _within(names[0], "otae_bot") and names[0].count(".") == 0:
            names = names[1:]  # `from otae_bot import x` is about x only
        if isinstance(node, ast.ImportFrom) and names and names[0] == "plugins":
            names = names[1:]  # `from plugins import x` is about plugins.x only
        if rel.startswith("plugins/"):
            own = rel.split("/")[1]
            if any(_within(n, "plugins") and n.count(".") >= 1 and n.split(".")[1] != own for n in names):
                self.add("L1", node)
        in_boundary = rel.startswith("otae_bot/boundary/")
        if not in_boundary and rel != BOUNDARY_WIRING and any(_within(n, BOUNDARY_MODULES) for n in names):
            self.add("L2", node)
        if rel.startswith(LOWER_LAYERS) and any(
                (_within(n, LOWER_FORBIDDEN)
                 and not (rel.startswith(CONFIG_LAYER) and _within(n, CONFIG_FRAMEWORK_API)))
                or (_within(n, "otae_bot") and n != "otae_bot" and not _within(n, LOWER_ALLOWED))
                for n in names):
            self.add("L3", node)
        if rel.startswith("otae_bot/") and any(_within(n, "plugins") for n in names):
            self.add("L4", node)
        if not rel.startswith(COMPAT_LAYER) and any(_within(n, COMPAT_MODULE) for n in names):
            self.add("C1", node)

    # R3, R4, R8: imports
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._layering(node)
        if (node.module or "").startswith(("arclet.entari", "satori")):
            self.media_classes.update(a.asname for a in node.names if a.name in MEDIA_CLASSES and a.asname)
        if node.module == "os" and any(a.name in ENV_READERS for a in node.names):
            self._env_read(node)
        module = node.module or ""
        if module.startswith(ONEBOT_MODULES) or any(f"{module}.{a.name}".startswith(ONEBOT_MODULES) for a in node.names):
            self.add("R3", node)
        if module.startswith(FRAMEWORKS) and (
                any(part.startswith("_") for part in module.split("."))
                or any(a.name.startswith("_") or a.name in FRAMEWORK_INTERNAL_NAMES for a in node.names)):
            self.add("R4", node)

    def visit_Import(self, node: ast.Import) -> None:
        self._layering(node)
        for alias in node.names:
            if alias.name.startswith(ONEBOT_MODULES):
                self.add("R3", node)
            if alias.name.startswith(FRAMEWORKS) and any(p.startswith("_") for p in alias.name.split(".")):
                self.add("R4", node)

    # R3: config names used as identifiers
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ONEBOT_HTTP_NAMES:
            self.add("R3", node)

    def _env_read(self, node: ast.AST) -> None:
        if not self.rel.startswith(ENV_EXEMPT):
            self.add("R8", node)

    # R4: private attributes of plugins, scopes and events; R8: os.environ / os.getenv
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in ENV_READERS and isinstance(node.value, ast.Name) and node.value.id == "os":
            self._env_read(node)
        if node.attr in ONEBOT_HTTP_NAMES:
            self.add("R3", node)
        if node.attr in UPLOAD_NAMES:
            self.add("R9", node)  # uploads happen in the outbound pipeline only
        if node.attr in {"_scope", "_origin"} or node.attr in SCOPE_LISTS:
            self.add("R4", node)
        elif node.attr == "_data" and "event" in _name(node.value).lower():
            # Only on events: `self._data` / `store._data` are the bot's own stores.
            self.add("R4", node)
        self.generic_visit(node)

    # R6, R7: comparisons
    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        has_literal = any(_strings(item) for item in operands)
        if has_literal and any(_is_platform_receiver(item) for item in operands):
            self.add("R6", node)
        if has_literal and any(_is_class_name(item) for item in operands):
            self.add("R7", node)
        self.generic_visit(node)


def scan(root: Path = ROOT) -> list[Violation]:
    found: list[Violation] = []
    for top in SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8")
            scanner = _Scanner(rel, source, protocol=not rel.startswith(EXEMPT))
            found.extend(scanner.run(ast.parse(source, filename=rel)))
    return sorted(found, key=lambda v: (v.rule, v.file, v.line))


def to_baseline(violations: list[Violation]) -> list[dict]:
    counts = Counter(v.key for v in violations)
    return [{"rule": r, "file": f, "snippet": s, "count": n} for (r, f, s), n in sorted(counts.items())]


def load_baseline(path: Path = BASELINE) -> Counter:
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return Counter({(e["rule"], e["file"], e["snippet"]): e.get("count", 1) for e in entries})


def compare(violations: list[Violation], baseline: Counter) -> tuple[list[Violation], Counter]:
    """(violations beyond the baseline, baseline entries that no longer occur)."""
    current = Counter(v.key for v in violations)
    extra = current - baseline
    new: list[Violation] = []
    for v in reversed(violations):  # report the last occurrences of a duplicated key
        if extra[v.key]:
            extra[v.key] -= 1
            new.append(v)
    return sorted(new), baseline - current


def report(violations: list[Violation]) -> str:
    out: list[str] = []
    for code, rule in RULES.items():
        group = [v for v in violations if v.rule == rule]
        out.append(f"{code} {rule}: {len(group)}")
        for v in group:
            out.append(f"  {v.file}:{v.line}  {v.snippet}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="rewrite the baseline from the current tree")
    parser.add_argument("--report", action="store_true", help="print the inventory by rule with line numbers")
    args = parser.parse_args(argv)
    violations = scan()
    if args.write:
        rows = (json.dumps(entry, ensure_ascii=False) for entry in to_baseline(violations))
        BASELINE.write_text("[\n" + ",\n".join(rows) + "\n]\n", encoding="utf-8")  # one entry per line
        print(f"wrote {len(violations)} violations to {BASELINE.relative_to(ROOT)}")
    if args.report or not args.write:
        print(report(violations))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tools described by JSON Schema, and JSON Schema -> TypeScript declarations.

Pure Python, no pydantic: shared by `AgentSandbox` (which accepts `SchemaTool`s and generates its
``.d.ts`` from them) and by `pydeno.integrations.pydantic_ai` (whose `schema_tools_to_dts`
renders pydantic-ai ``ToolDefinition``s the same way).

A schema tool takes ONE object argument, the way MCP and most LLM tool-calling APIs define tools:
the guest calls ``await lookup({city: "Paris"})`` and the host callable receives ``{"city":
"Paris"}``. The schema documents the argument for the model; it is not enforced by pydeno, so
validate the arguments inside the tool (they are untrusted guest data).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ._tools import _RESERVED_NAMES, ToolBridge

__all__ = ["SchemaTool", "js_tool_names", "schema_tools_to_dts"]

NAMESPACE = "tools"

# JavaScript words that cannot be a parameter name in a `.d.ts`; Python allows several of them.
_JS_RESERVED = frozenset(
    "break case catch class const continue debugger default delete do else enum export extends "
    "false finally for function if import in instanceof new null return super switch this throw "
    "true try typeof var void while with yield let static implements interface package private "
    "protected public await arguments eval".split()
)


def _union(parts: list[str]) -> str:
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    if "unknown" in seen:
        return "unknown"
    # `null` last reads the way people write it: `string | null`.
    if "null" in seen:
        seen = [p for p in seen if p != "null"] + ["null"]
    return " | ".join(seen)


# ---------------------------------------------------------------------------
# tool names: pydantic-ai name <-> JavaScript identifier
# ---------------------------------------------------------------------------

_NON_IDENT = re.compile(r"[^A-Za-z0-9_]")


def _js_identifier(name: str) -> str:
    ident = _NON_IDENT.sub("_", name) or "_"
    if ident[0].isdigit():
        ident = f"_{ident}"
    if ident in _JS_RESERVED or ident in _RESERVED_NAMES:
        ident = f"{ident}_"
    return ident


def js_tool_names(names: Iterable[str]) -> dict[str, str]:
    """Map tool names to the JavaScript identifiers they get on the ``tools`` object.

    Names that are not plain identifiers (``get-weather``, ``api.call``) have the offending
    characters replaced with ``_``; JavaScript reserved words and names that would shadow an
    object member (``delete``, ``constructor``) get a trailing ``_``; a collision gets a
    numeric suffix (``get_weather_2``). Deterministic for a given input order.
    """
    out: dict[str, str] = {}
    taken: set[str] = set()
    for name in names:
        base = _js_identifier(name)
        ident, n = base, 2
        while ident in taken:
            ident = f"{base}_{n}"
            n += 1
        taken.add(ident)
        out[name] = ident
    return out


# ---------------------------------------------------------------------------
# JSON schema -> TypeScript declarations
# ---------------------------------------------------------------------------

_TS_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_TS_BUILTIN_TYPES = frozenset(
    "any unknown never object string number boolean symbol bigint undefined null void "
    "Array Record Promise Set Map Date Uint8Array".split()
)
_MAX_DEPTH = 24


def _type_name(name: str) -> str:
    ident = _NON_IDENT.sub("_", name) or "_"
    if ident[0].isdigit():
        ident = f"_{ident}"
    if ident in _JS_RESERVED or ident in _TS_BUILTIN_TYPES:
        ident = f"{ident}_"
    return ident


def _literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return "unknown"


def _paren(t: str) -> str:
    return f"({t})" if (" | " in t or " & " in t) and not t.startswith("{") else t


def _arr(element: str) -> str:
    return f"{_paren(element)}[]"


def _comment_lines(parts: Sequence[str], indent: str) -> list[str]:
    text = "\n".join(p for p in parts if p).replace("*/", "*\\/").strip()
    if not text:
        return []
    lines = text.splitlines()
    if len(lines) == 1 and len(lines[0]) <= 80:
        return [f"{indent}/** {lines[0]} */"]
    return (
        [f"{indent}/**"]
        + [f"{indent} * {ln}".rstrip() for ln in lines]
        + [f"{indent} */"]
    )


def _schema_doc(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    parts: list[str] = []
    description = schema.get("description")
    if isinstance(description, str):
        parts.append(description.strip())
    fmt = schema.get("format")
    if isinstance(fmt, str):
        parts.append(f"@format {fmt}")
    if "default" in schema:
        try:
            rendered = json.dumps(schema["default"], ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = "..."
        if len(rendered) > 80:
            rendered = rendered[:77] + "..."
        parts.append(f"@default {rendered}")
    return parts


class _DtsWriter:
    """Renders tool schemas as one ``declare namespace`` block. Named schemas (``$defs``) become
    ``interface``/``type`` declarations, so recursive models stay finite."""

    def __init__(self) -> None:
        # TS name -> (canonical JSON, schema, the ref map its own refs resolve through)
        self._decls: dict[str, tuple[str, Any, dict[str, str]]] = {}

    def register(self, root: Any) -> dict[str, str]:
        """Give every ``$defs``/``definitions`` entry of `root` a TS name; return ref -> name."""
        refmap: dict[str, str] = {}
        if not isinstance(root, dict):
            return refmap
        found: list[tuple[str, str, Any]] = []
        for key in ("$defs", "definitions"):
            defs = root.get(key)
            if isinstance(defs, dict):
                for name, schema in defs.items():
                    found.append((f"#/{key}/{name}", str(name), schema))
        for ref, name, schema in found:
            base = _type_name(name)
            canonical = json.dumps(schema, sort_keys=True, default=str)
            ts, n = base, 2
            while ts in self._decls and self._decls[ts][0] != canonical:
                ts = f"{base}_{n}"
                n += 1
            refmap[ref] = ts
            if ts not in self._decls:
                self._decls[ts] = (canonical, schema, refmap)
        return refmap

    def declarations(self, indent: str) -> list[str]:
        out: list[str] = []
        for name, (_, schema, refmap) in self._decls.items():
            if out:
                out.append("")
            out.extend(_comment_lines(_schema_doc_no_default(schema), indent))
            body = self.ts(schema, refmap, indent)
            if body.startswith("{"):
                out.append(f"{indent}interface {name} {body}")
            else:
                out.append(f"{indent}type {name} = {body};")
        return out

    def ts(
        self, schema: Any, refmap: Mapping[str, str], indent: str, depth: int = 0
    ) -> str:
        if depth > _MAX_DEPTH or schema is True or schema is None:
            return "unknown"
        if schema is False:
            return "never"
        if not isinstance(schema, dict):
            return "unknown"
        rendered = self._ts(schema, refmap, indent, depth)
        if schema.get("nullable") is True:
            rendered = _union([rendered, "null"])
        return rendered

    def _ts(
        self, s: dict[str, Any], refmap: Mapping[str, str], indent: str, depth: int
    ) -> str:
        sub = depth + 1
        ref = s.get("$ref")
        if ref is not None:
            return refmap.get(ref, "unknown") if isinstance(ref, str) else "unknown"
        if "const" in s:
            return _literal(s["const"])
        enum = s.get("enum")
        if isinstance(enum, list) and enum:
            return _union([_literal(v) for v in enum])
        for key in ("anyOf", "oneOf"):
            options = s.get(key)
            if isinstance(options, list) and options:
                return _union([self.ts(o, refmap, indent, sub) for o in options])
        all_of = s.get("allOf")
        if isinstance(all_of, list) and all_of:
            parts = [self.ts(o, refmap, indent, sub) for o in all_of]
            parts = [p for p in dict.fromkeys(parts) if p != "unknown"]
            if not parts:
                return "unknown"
            return parts[0] if len(parts) == 1 else " & ".join(_paren(p) for p in parts)
        typ = s.get("type")
        if isinstance(typ, list):
            return _union(
                [self._ts({**s, "type": t}, refmap, indent, depth) for t in typ]
            )
        if typ is None:
            if any(
                k in s
                for k in ("properties", "additionalProperties", "patternProperties")
            ):
                typ = "object"
            elif "items" in s or "prefixItems" in s:
                typ = "array"
        if typ == "string":
            return "string"
        if typ in ("integer", "number"):
            return "number"
        if typ == "boolean":
            return "boolean"
        if typ == "null":
            return "null"
        if typ == "array":
            return self._array(s, refmap, indent, sub)
        if typ == "object":
            return self._object(s, refmap, indent, sub)
        return "unknown"

    def _array(
        self, s: dict[str, Any], refmap: Mapping[str, str], indent: str, depth: int
    ) -> str:
        prefix = s.get("prefixItems")
        rest = s.get("items")
        if isinstance(rest, list):  # draft 4-2019 tuples
            prefix, rest = rest, s.get("additionalItems")
        if isinstance(prefix, list):
            elements = [self.ts(p, refmap, indent, depth) for p in prefix]
            if rest is not None and rest is not False:
                elements.append(f"...{_arr(self.ts(rest, refmap, indent, depth))}")
            return "[" + ", ".join(elements) + "]"
        if rest is None or rest is True:
            return "unknown[]"
        return _arr(self.ts(rest, refmap, indent, depth))

    def _object(
        self, s: dict[str, Any], refmap: Mapping[str, str], indent: str, depth: int
    ) -> str:
        props = s.get("properties")
        props = props if isinstance(props, dict) else {}
        required = s.get("required")
        required = set(required) if isinstance(required, list) else set()
        extra = s.get("additionalProperties")
        patterns = s.get("patternProperties")
        index: str | None = None
        if isinstance(extra, dict):
            index = self.ts(extra, refmap, indent + "  ", depth)
        elif isinstance(patterns, dict) and patterns:
            index = _union(
                [self.ts(p, refmap, indent + "  ", depth) for p in patterns.values()]
            )
        if not props:
            if index is not None:
                return f"Record<string, {index}>"
            return "{}" if extra is False else "Record<string, unknown>"
        inner = indent + "  "
        lines = ["{"]
        for raw_key, prop in props.items():
            key = str(raw_key)
            lines.extend(_comment_lines(_schema_doc(prop), inner))
            shown = (
                key if _TS_IDENT.fullmatch(key) else json.dumps(key, ensure_ascii=False)
            )
            optional = "" if key in required else "?"
            lines.append(
                f"{inner}{shown}{optional}: {self.ts(prop, refmap, inner, depth)};"
            )
        if index is not None:
            lines.append(f"{inner}[key: string]: {index};")
        lines.append(f"{indent}}}")
        return "\n".join(lines)


def _schema_doc_no_default(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    description = schema.get("description")
    return [description.strip()] if isinstance(description, str) else []


def _tool_field(tool: Any, name: str, default: Any = None) -> Any:
    if isinstance(tool, Mapping):
        return tool.get(name, default)
    return getattr(tool, name, default)


def schema_tools_to_dts(tools: Iterable[Any], *, namespace: str = NAMESPACE) -> str:
    """TypeScript declarations for tools described by JSON schemas.

    Args:
        tools: `pydantic_ai.ToolDefinition` objects, or mappings with the same keys (``name``,
            ``parameters_json_schema``, and optionally ``description``, ``return_schema``,
            ``sequential``).
        namespace: The JavaScript object the tools live on.

    Returns:
        A ``declare namespace <namespace> { ... }`` block. Each tool is a function taking one
        object argument (its parameters schema) and returning ``Promise<T>``, where ``T`` comes
        from ``return_schema`` (``unknown`` without one). Tool names that are not identifiers are
        mapped as `js_tool_names` maps them. ``$ref``/``$defs`` (recursive ones too) become named
        declarations; ``anyOf``/``oneOf`` unions, ``allOf`` intersections, ``enum``/``const``
        literals, ``prefixItems`` tuples, ``additionalProperties`` index signatures; integers are
        ``number``, every string format is ``string``; descriptions become JSDoc.
    """
    tools = list(tools)
    names = js_tool_names(str(_tool_field(t, "name")) for t in tools)
    writer = _DtsWriter()
    indent = "  "
    functions: list[list[str]] = []
    for tool in tools:
        name = str(_tool_field(tool, "name"))
        params = _tool_field(tool, "parameters_json_schema") or {
            "type": "object",
            "properties": {},
        }
        returns = _tool_field(tool, "return_schema")
        params_refs = writer.register(params)
        returns_refs = writer.register(returns)
        doc: list[str] = []
        description = _tool_field(tool, "description")
        if isinstance(description, str) and description.strip():
            doc.append(description.strip())
        if names[name] != name:
            doc.append(f"(The tool `{name}`.)")
        if _tool_field(tool, "sequential", False):
            doc.append("Runs alone: other tool calls wait while it runs.")
        ptype = writer.ts(params, params_refs, indent)
        optional = (
            "?" if isinstance(params, dict) and not params.get("required") else ""
        )
        rtype = (
            "unknown" if returns is None else writer.ts(returns, returns_refs, indent)
        )
        functions.append(
            _comment_lines(doc, indent)
            + [
                f"{indent}function {names[name]}(args{optional}: {ptype}): Promise<{rtype}>;"
            ]
        )
    blocks = [writer.declarations(indent), *functions]
    body = "\n\n".join("\n".join(block) for block in blocks if block)
    if not body:
        return f"declare namespace {namespace} {{}}\n"
    return f"declare namespace {namespace} {{\n{body}\n}}\n"


# ---------------------------------------------------------------------------
# tools described by JSON Schema
# ---------------------------------------------------------------------------

_MAX_SCHEMA_BYTES = 256 * 1024


@dataclass(frozen=True)
class SchemaTool:
    """A tool described by JSON Schema (MCP style) rather than by a Python signature.

    Args:
        name: The JavaScript name (``[A-Za-z_][A-Za-z0-9_]*``, as for every pydeno tool).
        description: Shown to the model (JSDoc in `typescript_stubs`, text in `describe_tools`).
        input_schema: JSON Schema of the ONE object argument the tool takes.
        callable: Called with that argument as a ``dict`` (sync or async). It is untrusted guest
            data that pydeno does **not** validate against `input_schema`: validate it here.
        output_schema: JSON Schema of the result; the declared return type (``unknown`` without).

    A plain mapping with the same keys works wherever a `SchemaTool` does (``inputSchema`` and
    ``outputSchema``, the MCP spellings, are accepted too).
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    callable: Callable[..., Any] = field(repr=False)
    output_schema: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        ToolBridge._check_name(self.name, what="tool name")  # noqa: SLF001
        if not isinstance(self.description, str):
            raise TypeError(f"tool {self.name!r}: description must be a string")
        for label, schema in (
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
        ):
            if schema is None and label == "output_schema":
                continue
            if not isinstance(schema, Mapping):
                raise TypeError(
                    f"tool {self.name!r}: {label} must be a JSON Schema object (a mapping)"
                )
            try:
                size = len(json.dumps(schema, default=_refuse))
            except (TypeError, ValueError, RecursionError):
                raise TypeError(
                    f"tool {self.name!r}: {label} is not plain JSON"
                ) from None
            if size > _MAX_SCHEMA_BYTES:
                raise ValueError(
                    f"tool {self.name!r}: {label} is larger than {_MAX_SCHEMA_BYTES} bytes"
                )
        kind = self.input_schema.get("type")
        if kind is not None and kind != "object":
            raise ValueError(
                f"tool {self.name!r}: input_schema must describe an object (the tool takes one "
                f"object argument), not {kind!r}"
            )
        if not callable(self.callable):
            raise TypeError(f"tool {self.name!r} is not callable")

    def tool_definition(self) -> dict[str, Any]:
        """The ``name``/``parameters_json_schema``/``return_schema`` form `schema_tools_to_dts`
        and pydantic-ai use."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters_json_schema": dict(self.input_schema),
            "return_schema": None
            if self.output_schema is None
            else dict(self.output_schema),
        }


def _refuse(value: Any) -> Any:
    raise TypeError(type(value).__name__)


def as_schema_tool(tool: Any, name: str | None = None) -> SchemaTool:
    """A `SchemaTool` from a `SchemaTool` or a mapping with its keys. `name`, when given (the
    key the tool was registered under), must agree with the tool's own name if it has one."""
    if isinstance(tool, SchemaTool):
        found = tool
    elif isinstance(tool, Mapping):
        unknown = set(tool) - {
            "name",
            "description",
            "input_schema",
            "inputSchema",
            "output_schema",
            "outputSchema",
            "callable",
        }
        if unknown:
            raise TypeError(f"unknown keys in a schema tool: {sorted(unknown)}")
        own = tool.get("name", name)
        input_schema = tool.get("input_schema", tool.get("inputSchema"))
        if input_schema is None:
            input_schema = {"type": "object", "properties": {}}
        if "callable" not in tool:
            raise TypeError(f"schema tool {own!r} has no 'callable'")
        found = SchemaTool(
            name=own,  # type: ignore[arg-type]
            description=tool.get("description", ""),
            input_schema=input_schema,
            callable=tool["callable"],
            output_schema=tool.get("output_schema", tool.get("outputSchema")),
        )
    else:
        raise TypeError(
            f"expected a SchemaTool or a mapping describing one, got {type(tool).__name__}"
        )
    if name is not None and found.name != name:
        raise ValueError(
            f"schema tool registered as {name!r} is named {found.name!r}; use one name"
        )
    return found


def is_schema_tool(tool: Any) -> bool:
    return isinstance(tool, (SchemaTool, Mapping))


def schema_declarations(
    tools: Iterable[SchemaTool], *, declare: bool, indent: str = ""
) -> tuple[list[str], dict[str, tuple[list[str], str]]]:
    """TypeScript for schema tools, sharing one set of named types.

    Returns the named-type declarations (``$defs`` become ``interface``/``type``, shared by all
    the tools, so two tools defining the same model declare it once) and, per tool, its JSDoc
    lines and its signature (``name(args: {...}): Promise<T>``). `declare` prefixes ``declare``
    for top-level (global) declarations."""
    writer = _DtsWriter()
    functions: dict[str, tuple[list[str], str]] = {}
    for tool in tools:
        params = dict(tool.input_schema)
        returns = None if tool.output_schema is None else dict(tool.output_schema)
        params_refs = writer.register(params)
        returns_refs = writer.register(returns)
        ptype = writer.ts(params, params_refs, indent)
        optional = "" if params.get("required") else "?"
        rtype = (
            "unknown" if returns is None else writer.ts(returns, returns_refs, indent)
        )
        functions[tool.name] = (
            _comment_lines([tool.description], indent),
            f"{tool.name}(args{optional}: {ptype}): Promise<{rtype}>",
        )
    decls = writer.declarations(indent)
    if declare:
        decls = [
            f"declare {line}" if line.startswith(("interface ", "type ")) else line
            for line in decls
        ]
    return decls, functions

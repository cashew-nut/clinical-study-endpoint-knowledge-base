"""The syntax-template grammar: parse the authored form, emit USDM's markup.

Templates are authored in `vocab/usdm_templates.yaml` in a compact form and
emitted in the markup CDISC's own published examples use:

    authored   Change from {reference} in {measurement}[ at {timepoint}]
    emitted    <p>Change from <usdm:tag name="reference"/> in
               <usdm:tag name="measurement"/> at <usdm:tag name="timepoint"/></p>

Two things come out of one parse, and they have to agree or the document is
malformed: the `text` (tags unresolved, for `SyntaxTemplate.text`) and the
`label` (tags substituted, for the human reading). `render` returns both plus
the tags actually used, which is what the dictionary's `parameterMaps` are
built from -- so a tag can never appear in one and not the other.

This module is deliberately free of warehouse and vocabulary imports: the
`vocab validate` path imports it to check templates at load time, and the
projection imports it to render them.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

TAG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Rendered for each tag in `text`. The one place the emitted markup is decided.
TAG_MARKUP = '<usdm:tag name="{name}"/>'
#: SyntaxTemplate.text is an HTML fragment in every published example.
TEXT_WRAPPER = "<p>{body}</p>"


class TemplateError(ValueError):
    """A template that cannot be parsed. Raised at validate time, not render time."""


@dataclass(frozen=True)
class Literal:
    text: str


@dataclass(frozen=True)
class Tag:
    name: str


@dataclass(frozen=True)
class Optional:
    """A `[ ... ]` group, dropped entirely if any tag inside is unresolved."""

    parts: tuple[Literal | Tag, ...]


Part = Literal | Tag | Optional

_ESCAPABLE = frozenset("{}[]\\")


def parse_template(template: str) -> tuple[Part, ...]:
    """Parse the authored form into parts. Raises TemplateError on bad syntax."""
    parts: list[Part] = []
    group: list[Literal | Tag] | None = None
    buf: list[str] = []
    i = 0
    n = len(template)

    def flush() -> None:
        if not buf:
            return
        target = group if group is not None else parts
        target.append(Literal("".join(buf)))
        buf.clear()

    while i < n:
        ch = template[i]
        if ch == "\\":
            if i + 1 >= n or template[i + 1] not in _ESCAPABLE:
                raise TemplateError(f"dangling escape at position {i} in {template!r}")
            buf.append(template[i + 1])
            i += 2
            continue
        if ch == "{":
            end = template.find("}", i)
            if end == -1:
                raise TemplateError(f"unclosed {{ at position {i} in {template!r}")
            name = template[i + 1 : end]
            if not TAG_NAME_RE.match(name):
                raise TemplateError(f"invalid tag name {name!r} in {template!r}")
            flush()
            (group if group is not None else parts).append(Tag(name))
            i = end + 1
            continue
        if ch == "}":
            raise TemplateError(f"unmatched }} at position {i} in {template!r}")
        if ch == "[":
            if group is not None:
                raise TemplateError(f"nested optional group at position {i} in {template!r}")
            flush()
            group = []
            i += 1
            continue
        if ch == "]":
            if group is None:
                raise TemplateError(f"unmatched ] at position {i} in {template!r}")
            flush()
            if not group:
                raise TemplateError(f"empty optional group at position {i} in {template!r}")
            if not any(isinstance(p, Tag) for p in group):
                raise TemplateError(
                    f"optional group with no tag at position {i} in {template!r} -- "
                    "it would never be dropped, so it is not optional"
                )
            parts.append(Optional(tuple(group)))
            group = None
            i += 1
            continue
        buf.append(ch)
        i += 1

    if group is not None:
        raise TemplateError(f"unclosed [ in {template!r}")
    flush()
    if not parts:
        raise TemplateError("empty template")
    return tuple(parts)


def required_tags(parts: tuple[Part, ...]) -> tuple[str, ...]:
    """Tags outside any optional group. If one of these does not resolve, the
    template does not apply at all."""
    return tuple(p.name for p in parts if isinstance(p, Tag))


def all_tags(parts: tuple[Part, ...]) -> tuple[str, ...]:
    names: list[str] = []
    for part in parts:
        if isinstance(part, Tag):
            names.append(part.name)
        elif isinstance(part, Optional):
            names.extend(p.name for p in part.parts if isinstance(p, Tag))
    return tuple(names)


@dataclass(frozen=True)
class Rendered:
    text: str
    label: str
    tags: tuple[str, ...]
    dropped: tuple[str, ...]


def _resolved(values: dict[str, str | None], name: str) -> bool:
    value = values.get(name)
    return value is not None and str(value).strip() != ""


def render(parts: tuple[Part, ...], values: dict[str, str | None]) -> Rendered | None:
    """Render to (text, label, tags used). Returns None when a required tag is
    unresolved -- the caller then falls to a lower fidelity tier rather than
    emitting a half-sentence.
    """
    for name in required_tags(parts):
        if not _resolved(values, name):
            return None

    text: list[str] = []
    label: list[str] = []
    used: list[str] = []
    dropped: list[str] = []

    def emit(part: Literal | Tag) -> None:
        if isinstance(part, Literal):
            text.append(html.escape(part.text, quote=False))
            label.append(part.text)
        else:
            text.append(TAG_MARKUP.format(name=part.name))
            label.append(str(values[part.name]))
            used.append(part.name)

    for part in parts:
        if isinstance(part, Optional):
            group_tags = [p.name for p in part.parts if isinstance(p, Tag)]
            if all(_resolved(values, t) for t in group_tags):
                for inner in part.parts:
                    emit(inner)
            else:
                dropped.extend(t for t in group_tags if not _resolved(values, t))
        else:
            emit(part)

    body = "".join(text).strip()
    return Rendered(
        text=TEXT_WRAPPER.format(body=body),
        label=" ".join("".join(label).split()),
        tags=tuple(used),
        dropped=tuple(dropped),
    )

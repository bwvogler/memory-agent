"""Directory views: turning a directory's files into an ordered, grouped index.

A directory may hold a `VIEW.md` whose frontmatter *is* a display spec - see
docs/decisions/0018 for why it is its own file rather than a key in `GUIDE.md`
(the `headers` JSONB column is full-replace on write, so a spec sharing a file
with prose is one careless rewrite away from being silently deleted).

Everything here is pure: dicts in, dataclasses out, no database and no
filesystem. That is deliberate. `pytest.ini` defines three tiers and all three
are Python, while no tier executes the served JavaScript - so every decision
that can live on this side of the wire is a decision that gets tested by
default. The client is left with painting.

Two invariants are enforced rather than documented:

**A view may reorder and group; it may never drop an entry.** A filtered index
hides files that exist, which makes a liar of the one artifact whose whole
value is "the wiki says what is there" - and it would be a hiding primitive an
agent could write into the KB after ingesting a poisoned page. `build_groups`
sweeps anything it failed to place into the empty group rather than losing it.

**A bad spec never costs the reader the page.** Every parse failure degrades to
the default view plus a warning naming the problem. Nothing here raises on
malformed input, because the input is written by an agent and read by a person
who did not write it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Bumping this is a promise about the *vocabulary*, not the renderer. A spec
# declaring a version we do not know still renders - under this version's
# rules, with a warning - because refusing it would leave a reader staring at
# nothing over a key they cannot see.
SCHEMA_VERSION = 1

LAYOUTS = frozenset({"table", "list"})
DEFAULT_LAYOUT = "list"
DEFAULT_EMPTY = "Nothing here yet."

# The spec is its own file. `GUIDE.md` stays what it has always been - prose
# telling the agent how to write files here - and `VIEW.md` is config telling
# the renderer how to show them. Keeping them apart means a turn revising the
# prose cannot break the rendering, which is not a style preference: the
# `headers` column is full-replace on write, so a `view:` key sharing a file
# with prose is one incomplete rewrite away from being silently deleted.
SPEC_FILE = "VIEW.md"
GUIDE_FILE = "GUIDE.md"
SKILL_FILE = "SKILL.md"

# Files that describe the directory rather than sit in it. Each is reachable -
# the tree hides them as leaves too - but none is an entry in its own index.
NOT_ENTRIES = frozenset({SPEC_FILE, GUIDE_FILE, SKILL_FILE})


def is_entry(filename: str) -> bool:
    """Whether a child file appears as a row in its directory's index.

    Dotfiles are excluded because the store keeps bookkeeping beside the
    content - `.bootstrap-state.json` is a real child of `memory/wiki/` and is
    nobody's wiki page.
    """
    return (
        bool(filename) and not filename.startswith(".") and filename not in NOT_ENTRIES
    )


# Cap what one field can contribute to a response. A single frontmatter value
# is a heading, a date, a name - not a document. Without a bound, one pasted
# essay in one file's frontmatter is dragged into every render of its
# directory.
MAX_FIELD_CHARS = 200
MAX_EXCERPT_CHARS = 240

_VIEW_KEYS = frozenset(
    {
        "v",
        "layout",
        "fields",
        "labels",
        "title_field",
        "excerpt",
        "sort",
        "group_by",
        "group_order",
        "counts",
        "empty_labels",
        "value_labels",
        "empty",
    }
)
_PAGE_KEYS = frozenset({"header"})
_TOP_KEYS = frozenset({"view", "page"})

# The frontmatter keys TigerFS routes to dedicated columns rather than into
# `headers`. A spec writing one at the top level would find it silently
# missing, which is why the vocabulary nests everything under `view:`/`page:`.
RESERVED_COLUMNS = ("title", "author", "encoding")


@dataclass(frozen=True)
class View:
    """The normalised index spec. Every field has a usable default."""

    layout: str = DEFAULT_LAYOUT
    fields: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    title_field: str | None = None
    excerpt: bool = False
    sort: tuple[str, ...] = ()
    group_by: str | None = None
    group_order: tuple[str, ...] = ()
    counts: bool = False
    empty_labels: Mapping[str, str] = field(default_factory=dict)
    value_labels: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    empty: str = DEFAULT_EMPTY


@dataclass(frozen=True)
class Page:
    """The normalised per-file spec: what renders above a file's prose."""

    header: tuple[str, ...] = ()


@dataclass(frozen=True)
class Entry:
    path: str
    name: str
    title: str
    fields: Mapping[str, Any]
    excerpt: str


@dataclass(frozen=True)
class Group:
    """`key` is None for an ungrouped view and "" for a missing value.

    The client turns `key` into a heading via the spec's `value_labels` and
    `empty_labels`; those live on one side of the wire, not both.
    """

    key: str | None
    count: int
    entries: tuple[Entry, ...]


# ---------------------------------------------------------------------------
# Normalising a raw spec
# ---------------------------------------------------------------------------


def normalise(raw: object) -> tuple[View, Page, list[str]]:
    """Turn whatever was in `VIEW.md`'s frontmatter into a usable spec.

    Returns the defaults plus a warning for anything it could not use. Never
    raises: `raw` came out of a file an agent wrote.
    """
    warnings: list[str] = []
    if raw is None:
        return View(), Page(), warnings
    if not isinstance(raw, Mapping):
        warnings.append("the spec is not a mapping; using the default view")
        return View(), Page(), warnings

    for key in sorted(set(raw) - _TOP_KEYS):
        if key in RESERVED_COLUMNS:
            warnings.append(
                f"`{key}:` is a reserved column and never reaches the spec - "
                f"nest it under `view:` or `page:`"
            )
        else:
            warnings.append(f"unknown top-level key `{key}:` ignored")

    view = _normalise_view(raw.get("view"), warnings)
    page = _normalise_page(raw.get("page"), warnings)
    return view, page, warnings


def _normalise_view(raw: object, warnings: list[str]) -> View:
    if raw is None:
        return View()
    if not isinstance(raw, Mapping):
        warnings.append("`view:` is not a mapping; using the default view")
        return View()

    warnings.extend(
        f"unknown key `view.{key}` ignored" for key in sorted(set(raw) - _VIEW_KEYS)
    )

    version = raw.get("v")
    if version is not None and version != SCHEMA_VERSION:
        warnings.append(
            f"spec version {version!r} is not version {SCHEMA_VERSION}; "
            f"rendering with version {SCHEMA_VERSION} rules"
        )

    layout = raw.get("layout", DEFAULT_LAYOUT)
    if layout not in LAYOUTS:
        if layout != DEFAULT_LAYOUT:
            warnings.append(
                f"unknown layout {layout!r}; using {DEFAULT_LAYOUT!r} "
                f"(known: {', '.join(sorted(LAYOUTS))})"
            )
        layout = DEFAULT_LAYOUT

    group_by = _string(raw.get("group_by"), "view.group_by", warnings)
    return View(
        layout=layout,
        fields=_string_list(raw.get("fields"), "view.fields", warnings),
        labels=_string_map(raw.get("labels"), "view.labels", warnings),
        title_field=_string(raw.get("title_field"), "view.title_field", warnings),
        excerpt=bool(raw.get("excerpt", False)),
        sort=_string_list(raw.get("sort"), "view.sort", warnings),
        group_by=group_by,
        group_order=_string_list(raw.get("group_order"), "view.group_order", warnings),
        counts=bool(raw.get("counts", False)),
        empty_labels=_string_map(
            raw.get("empty_labels"), "view.empty_labels", warnings
        ),
        value_labels=_value_labels(raw.get("value_labels"), warnings),
        empty=_string(raw.get("empty"), "view.empty", warnings) or DEFAULT_EMPTY,
    )


def _normalise_page(raw: object, warnings: list[str]) -> Page:
    if raw is None:
        return Page()
    if not isinstance(raw, Mapping):
        warnings.append("`page:` is not a mapping; ignoring it")
        return Page()

    warnings.extend(
        f"unknown key `page.{key}` ignored" for key in sorted(set(raw) - _PAGE_KEYS)
    )

    return Page(header=_string_list(raw.get("header"), "page.header", warnings))


def _string(value: object, where: str, warnings: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    warnings.append(f"`{where}` should be text; ignoring {value!r}")
    return None


def _string_list(value: object, where: str, warnings: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # A single name where a list was expected is an obvious intent, and
        # refusing it would be pedantry aimed at someone who cannot see the
        # schema. Accept it and say nothing.
        return (value,)
    if not isinstance(value, Sequence):
        warnings.append(f"`{where}` should be a list; ignoring {value!r}")
        return ()

    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        else:
            warnings.append(f"`{where}` entry {item!r} is not text; ignoring it")
    return tuple(out)


def _string_map(value: object, where: str, warnings: list[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        warnings.append(f"`{where}` should be a mapping; ignoring {value!r}")
        return {}
    return {str(k): _scalar_text(v) for k, v in value.items()}


def _value_labels(value: object, warnings: list[str]) -> dict[str, dict[str, str]]:
    """`{field: {raw_value: label}}`, with every key stringified.

    JSONB has no non-string object keys, so a YAML `true:` arrives as `"true"`
    - which is also what `_group_key` produces for a boolean. Stringifying both
    sides is what makes `msc_agreed: false` findable as `"false"`.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        warnings.append(f"`view.value_labels` should be a mapping; ignoring {value!r}")
        return {}

    out: dict[str, dict[str, str]] = {}
    for fieldname, mapping in value.items():
        if not isinstance(mapping, Mapping):
            warnings.append(
                f"`view.value_labels.{fieldname}` should be a mapping; ignoring it"
            )
            continue
        out[str(fieldname)] = {
            _scalar_text(k): _scalar_text(v) for k, v in mapping.items()
        }
    return out


# ---------------------------------------------------------------------------
# Building entries out of rows
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")


def build_entries(rows: Sequence[Mapping[str, Any]], view: View) -> list[Entry]:
    """One Entry per row, in the spec's order.

    A row is what the children query returns: `path`, `filename`, `title`,
    `author`, `headers`, `body`. `headers` is already a dict - TigerFS parsed
    the frontmatter on the way in, so nothing here parses YAML.
    """
    entries = [_entry(row, view) for row in rows]
    return _sorted(entries, view)


def _entry(row: Mapping[str, Any], view: View) -> Entry:
    path = str(row.get("path") or "")
    filename = str(row.get("filename") or path.rsplit("/", 1)[-1])
    name = filename.removesuffix(".md")

    headers = row.get("headers")
    fields: dict[str, Any] = dict(headers) if isinstance(headers, Mapping) else {}
    # The dedicated columns are frontmatter too - a spec naming `title` or
    # `author` as a field means the value the writer put in the frontmatter,
    # and has no way to know the store filed it elsewhere.
    for column in ("title", "author"):
        value = row.get(column)
        if value not in (None, ""):
            fields.setdefault(column, value)

    body = row.get("body") or ""
    return Entry(
        path=path,
        name=name,
        title=_title(row, fields, body, name, view),
        fields=fields,
        excerpt=_excerpt(body) if view.excerpt else "",
    )


def _title(
    row: Mapping[str, Any],
    fields: Mapping[str, Any],
    body: str,
    name: str,
    view: View,
) -> str:
    """The spec's `title_field`, else the title column, else the first heading,
    else the filename. Every step can come up empty, so all four are tried."""
    if view.title_field:
        chosen = _scalar_text(fields.get(view.title_field))
        if chosen:
            return chosen

    column = row.get("title")
    if isinstance(column, str) and column.strip():
        return column.strip()

    for line in body.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            return match.group(1)

    return name


def _excerpt(body: str) -> str:
    """The first paragraph that is not a heading, a rule, or a list."""
    for block in re.split(r"\n\s*\n", body):
        text = " ".join(block.split())
        if not text or _HEADING.match(text) or text.startswith(("-", "*", ">", "|")):
            continue
        return _truncate(text, MAX_EXCERPT_CHARS)
    return ""


# ---------------------------------------------------------------------------
# Sorting and grouping
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"(\d+)")


def _natural_key(value: object) -> tuple[tuple[int, int, str], ...]:
    """Collate "Card 2" before "Card 10".

    Each part becomes a same-shaped triple so a number never has to be
    compared against a string. The tree's bare `.sort()` does not do this and
    it shows the moment a directory holds more than nine numbered files.
    """
    text = _scalar_text(value).casefold()
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in _DIGITS.split(text)
        if part != ""
    )


def _sort_key(
    entry: Entry, name: str, *, descending: bool
) -> tuple[int, tuple[tuple[int, int, str], ...]]:
    """Natural order within a presence flag that survives `reverse`.

    An absent value is not a small value - it is no value, and it belongs at
    the bottom of the list whichever way the column is pointing. `reverse=True`
    flips the whole key, so the flag has to be pre-flipped to come back the
    right way up; a flag that simply said "absent = 1" would put every blank
    row at the *top* of a descending sort and bury everything that has a value.
    """
    value = entry.title if name == "title" else entry.fields.get(name)
    absent = value in (None, "", [], {})
    present_first = (0, 1) if descending else (1, 0)
    if absent:
        return (present_first[0], ())
    return (present_first[1], _natural_key(value))


def _sorted(entries: list[Entry], view: View) -> list[Entry]:
    """Least-significant key first, so Python's stable sort composes them."""
    out = sorted(entries, key=lambda e: _natural_key(e.title))
    for name in reversed(view.sort):
        descending = name.startswith("-")
        key = name[1:] if descending else name
        if not key:
            continue
        out.sort(
            key=lambda e, k=key, d=descending: _sort_key(e, k, descending=d),
            reverse=descending,
        )
    return out


def _group_values(entry: Entry, view: View) -> list[str]:
    """Which groups this entry belongs to. A list-valued field means several.

    An empty list is not "no group" - it is the empty group, which is how
    `holder: []` stays visible as *undealt* rather than vanishing into a
    heading nobody reads. Returning [""] rather than [] is the whole point.
    """
    if not view.group_by:
        return [""]
    value = entry.fields.get(view.group_by)
    if isinstance(value, (list, tuple)):
        keys = [_scalar_text(v) for v in value if _scalar_text(v)]
        return keys or [""]
    text = _scalar_text(value)
    return [text] if text else [""]


def build_groups(entries: Sequence[Entry], view: View) -> list[Group]:
    """Group in the spec's order, then whatever is left, alphabetically.

    Enforces this module's first invariant: every entry handed in comes back
    out at least once. A view reorders and groups; it never filters.
    """
    if not view.group_by:
        return [Group(key=None, count=len(entries), entries=tuple(entries))]

    buckets: dict[str, list[Entry]] = {}
    for entry in entries:
        for key in _group_values(entry, view):
            buckets.setdefault(key, []).append(entry)

    listed = [k for k in view.group_order if k in buckets]
    rest = sorted(set(buckets) - set(listed), key=_natural_key)

    # Repair rather than assert. `assert` is stripped under -O, and the point
    # of this invariant is that an index which quietly omits a file is worse
    # than no index - so anything `_group_values` failed to place is swept into
    # the empty group and logged, which is visible in the page and in the log
    # rather than only in a crash nobody is watching for.
    placed = {id(e) for bucket in buckets.values() for e in bucket}
    orphans = [e for e in entries if id(e) not in placed]
    if orphans:
        log.error("view grouping dropped %d entries; recovering", len(orphans))
        buckets.setdefault("", []).extend(orphans)
        if "" not in listed and "" not in rest:
            rest = [*rest, ""]

    return [
        Group(key=key, count=len(buckets[key]), entries=tuple(buckets[key]))
        for key in [*listed, *rest]
    ]


# ---------------------------------------------------------------------------
# Shared value handling
# ---------------------------------------------------------------------------


def _scalar_text(value: object) -> str:
    """One rendering of a frontmatter value, used for keys and collation.

    Booleans are lowercased so `false` matches a `value_labels` key written in
    YAML; a nested mapping has no sensible one-line form and becomes empty
    rather than `{'a': 1}`.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _truncate(value.strip(), MAX_FIELD_CHARS)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return _truncate(
            ", ".join(t for t in (_scalar_text(v) for v in value) if t),
            MAX_FIELD_CHARS,
        )
    return ""


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

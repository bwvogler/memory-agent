"""What a directory view does with a spec, and what it does with a bad one.

Two things this file is really about. A spec is written by an agent and read
by a person who did not write it, so *every* malformed shape has to degrade to
something readable plus a warning that names the problem - never an exception,
never a blank page. And a view may reorder and group but must never drop an
entry, because an index that hides a file is worse than no index at all.
"""

from __future__ import annotations

from app import kbview
from app.kbview import Entry, Page, View


def row(path, *, headers=None, body="", title=None, author=None):
    return {
        "path": path,
        "filename": path.rsplit("/", 1)[-1],
        "title": title,
        "author": author,
        "headers": headers or {},
        "body": body,
    }


def spec(**view):
    return {"view": view}


def names(entries):
    return [e.name for e in entries]


# --- normalising -----------------------------------------------------------


def test_an_absent_spec_is_the_default_view():
    view, page, warnings = kbview.normalise(None)

    assert view == View()
    assert page == Page()
    assert warnings == []


def test_a_full_spec_normalises_to_exactly_what_it_said():
    view, page, warnings = kbview.normalise(
        {
            "view": {
                "v": 1,
                "layout": "table",
                "fields": ["cuisine", "time"],
                "labels": {"time": "Cook time"},
                "sort": ["-time", "title"],
                "group_by": "cuisine",
                "group_order": ["italian"],
                "counts": True,
                "excerpt": True,
                "title_field": "dish",
                "empty_labels": {"holder": "Undealt"},
                "value_labels": {"msc_agreed": {False: "not yet"}},
                "empty": "No recipes yet.",
            },
            "page": {"header": ["cuisine"]},
        }
    )

    assert warnings == []
    assert view.layout == "table"
    assert view.fields == ("cuisine", "time")
    assert view.sort == ("-time", "title")
    assert view.group_order == ("italian",)
    assert view.counts is True
    assert view.title_field == "dish"
    assert view.empty == "No recipes yet."
    assert page.header == ("cuisine",)


def test_an_unknown_layout_falls_back_and_says_so():
    view, _, warnings = kbview.normalise(spec(layout="kanban"))

    assert view.layout == kbview.DEFAULT_LAYOUT
    assert any("kanban" in w for w in warnings), warnings


def test_an_unknown_version_still_renders():
    """Refusing would leave a reader staring at nothing over a key they cannot
    see. Render under this version's rules, and warn."""
    view, _, warnings = kbview.normalise(spec(v=99, layout="table"))

    assert view.layout == "table"
    assert any("99" in w for w in warnings), warnings


def test_unknown_keys_are_named_rather_than_swallowed():
    _, _, warnings = kbview.normalise({"view": {"colums": ["a"]}, "pages": {}})

    assert any("view.colums" in w for w in warnings), warnings
    assert any("pages" in w for w in warnings), warnings


def test_a_reserved_column_at_the_top_level_is_called_out_specifically():
    """`title:` lands in the store's own column and never reaches `headers`,
    so an author who writes it there sees their key vanish with no explanation."""
    _, _, warnings = kbview.normalise({"title": "Recipes", "view": {}})

    assert any("reserved column" in w and "title" in w for w in warnings), warnings


def test_a_spec_that_is_not_a_mapping_degrades():
    view, _, warnings = kbview.normalise(["layout: table"])

    assert view == View()
    assert warnings


def test_a_lone_string_where_a_list_belongs_is_accepted():
    view, _, warnings = kbview.normalise(spec(fields="cuisine"))

    assert view.fields == ("cuisine",)
    assert warnings == []


def test_a_non_text_list_entry_is_dropped_with_a_warning():
    view, _, warnings = kbview.normalise(spec(fields=["cuisine", 7]))

    assert view.fields == ("cuisine",)
    assert any("7" in w for w in warnings), warnings


def test_value_label_keys_are_stringified_to_match_group_keys():
    """YAML `false:` and a boolean field value have to meet somewhere, and
    JSONB has no non-string object keys - so both sides become "false"."""
    view, _, _ = kbview.normalise(
        spec(value_labels={"msc_agreed": {False: "not yet", True: "agreed"}})
    )

    assert view.value_labels == {"msc_agreed": {"false": "not yet", "true": "agreed"}}


# --- building entries ------------------------------------------------------


def test_the_title_falls_back_through_four_sources():
    view, _, _ = kbview.normalise(spec(title_field="dish"))

    from_field = kbview.build_entries([row("w/a.md", headers={"dish": "Ragu"})], view)
    from_column = kbview.build_entries([row("w/b.md", title="Column")], view)
    from_heading = kbview.build_entries([row("w/c.md", body="# Heading\n\nx")], view)
    from_name = kbview.build_entries([row("w/lentil-soup.md")], view)

    assert from_field[0].title == "Ragu"
    assert from_column[0].title == "Column"
    assert from_heading[0].title == "Heading"
    assert from_name[0].title == "lentil-soup"


def test_the_dedicated_columns_are_readable_as_ordinary_fields():
    """A writer put `author:` in frontmatter; that the store filed it in its own
    column is an implementation detail they have no way to know about."""
    view, _, _ = kbview.normalise(spec(fields=["author"]))

    entry = kbview.build_entries([row("w/a.md", author="Brian")], view)[0]

    assert entry.fields["author"] == "Brian"


def test_an_excerpt_skips_headings_and_lists_and_is_only_built_when_asked():
    body = "# Title\n\n- a bullet\n\nThe real first paragraph.\n"
    asked, _, _ = kbview.normalise(spec(excerpt=True))
    not_asked, _, _ = kbview.normalise(spec())

    assert kbview.build_entries([row("w/a.md", body=body)], asked)[0].excerpt == (
        "The real first paragraph."
    )
    assert kbview.build_entries([row("w/a.md", body=body)], not_asked)[0].excerpt == ""


def test_one_enormous_field_cannot_be_dragged_into_every_render():
    view, _, _ = kbview.normalise(spec(group_by="note"))
    entries = kbview.build_entries([row("w/a.md", headers={"note": "x" * 5000})], view)

    key = kbview.build_groups(entries, view)[0].key

    assert key is not None
    assert len(key) <= kbview.MAX_FIELD_CHARS


# --- sorting ---------------------------------------------------------------


def test_sorting_is_numeric_aware():
    """A bare .sort() puts "Card 10" before "Card 2", which looks broken the
    moment a directory holds more than nine numbered files."""
    view, _, _ = kbview.normalise(spec())
    rows = [row(f"w/Card {n}.md") for n in (10, 2, 1)]

    assert names(kbview.build_entries(rows, view)) == ["Card 1", "Card 2", "Card 10"]


def test_sorting_by_a_field_descending():
    view, _, _ = kbview.normalise(spec(sort=["-time"]))
    rows = [
        row("w/a.md", headers={"time": 30}),
        row("w/b.md", headers={"time": 180}),
        row("w/c.md", headers={"time": 90}),
    ]

    assert names(kbview.build_entries(rows, view)) == ["b", "c", "a"]


def test_a_missing_value_sorts_last_in_both_directions():
    """Not merely "reverse of ascending": an absent value is not a small value,
    and flipping it to the top would bury everything that has one."""
    rows = [row("w/a.md"), row("w/b.md", headers={"time": 5})]
    up, _, _ = kbview.normalise(spec(sort=["time"]))
    down, _, _ = kbview.normalise(spec(sort=["-time"]))

    assert names(kbview.build_entries(rows, up)) == ["b", "a"]
    assert names(kbview.build_entries(rows, down)) == ["b", "a"]


def test_sort_keys_compose_least_significant_last():
    view, _, _ = kbview.normalise(spec(sort=["cuisine", "title"]))
    rows = [
        row("w/z.md", headers={"cuisine": "italian"}),
        row("w/a.md", headers={"cuisine": "italian"}),
        row("w/m.md", headers={"cuisine": "french"}),
    ]

    assert names(kbview.build_entries(rows, view)) == ["m", "a", "z"]


# --- grouping --------------------------------------------------------------


def test_no_group_by_is_one_group_keyed_none():
    view, _, _ = kbview.normalise(spec())
    entries = kbview.build_entries([row("w/a.md")], view)

    groups = kbview.build_groups(entries, view)

    assert len(groups) == 1
    assert groups[0].key is None


def test_a_list_valued_field_puts_an_entry_in_every_one_of_its_groups():
    view, _, _ = kbview.normalise(spec(group_by="holder"))
    entries = kbview.build_entries(
        [row("w/a.md", headers={"holder": ["brian", "laura"]})], view
    )

    groups = {g.key: names(g.entries) for g in kbview.build_groups(entries, view)}

    assert groups == {"brian": ["a"], "laura": ["a"]}


def test_an_empty_list_is_the_empty_group_not_no_group():
    """ADR 0011: "Undealt is data, not absence." `holder: []` has to stay
    visible and countable, or the deck's most useful number disappears."""
    view, _, _ = kbview.normalise(spec(group_by="holder"))
    entries = kbview.build_entries(
        [row("w/a.md", headers={"holder": []}), row("w/b.md")], view
    )

    groups = kbview.build_groups(entries, view)

    assert [g.key for g in groups] == [""]
    assert groups[0].count == 2


def test_group_order_is_honoured_and_unlisted_groups_append():
    view, _, _ = kbview.normalise(
        spec(group_by="cat", group_order=["home", "out", "caregiving"])
    )
    entries = kbview.build_entries(
        [row(f"w/{c}.md", headers={"cat": c}) for c in ("wild", "out", "home")], view
    )

    assert [g.key for g in kbview.build_groups(entries, view)] == [
        "home",
        "out",
        "wild",
    ]


def test_counts_are_per_group():
    view, _, _ = kbview.normalise(spec(group_by="cat"))
    entries = kbview.build_entries(
        [
            row("w/a.md", headers={"cat": "home"}),
            row("w/b.md", headers={"cat": "home"}),
            row("w/c.md", headers={"cat": "out"}),
        ],
        view,
    )

    assert {g.key: g.count for g in kbview.build_groups(entries, view)} == {
        "home": 2,
        "out": 1,
    }


def test_a_view_never_drops_an_entry():
    """The invariant, stated as a test because it is a security property as
    much as a correctness one: there is no filter in the vocabulary, so no
    spec an agent can write is able to hide a file that exists."""
    view, _, _ = kbview.normalise(
        spec(group_by="cat", group_order=["only-this-one"], sort=["-nope"])
    )
    entries = kbview.build_entries(
        [
            row("w/a.md", headers={"cat": "home"}),
            row("w/b.md", headers={"cat": None}),
            row("w/c.md", headers={"cat": ["x", "y"]}),
            row("w/d.md"),
        ],
        view,
    )

    groups = kbview.build_groups(entries, view)
    seen = {e.path for g in groups for e in g.entries}

    assert seen == {"w/a.md", "w/b.md", "w/c.md", "w/d.md"}


def test_entries_survive_a_spec_that_is_nonsense_end_to_end():
    view, _, warnings = kbview.normalise({"view": "table"})
    entries = kbview.build_entries([row("w/a.md"), row("w/b.md")], view)

    assert warnings
    assert names(kbview.build_groups(entries, view)[0].entries) == ["a", "b"]


def test_a_hostile_field_name_cannot_reach_a_lookup_as_a_prototype_key():
    """`__proto__` is a legal YAML key and a legal JSONB key. It must arrive as
    ordinary data - the client's own lookups guard themselves, but nothing here
    may hand it out as anything other than a string field name."""
    view, _, _ = kbview.normalise(spec(group_by="__proto__"))
    entries = kbview.build_entries([row("w/a.md", headers={"__proto__": "x"})], view)

    groups = kbview.build_groups(entries, view)

    assert [g.key for g in groups] == ["x"]
    assert isinstance(entries[0], Entry)

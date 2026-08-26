"""Silent truncation is the worst failure mode for a discovery tool: a short
template set produces a short manifest, and nothing in the output says so. The
omission would not surface until someone opened an app with a missing layer.
"""

import pytest

from agol_provision.discovery import TruncatedError, collect, count_matches


def _id(n: int) -> str:
    return f"{n:032x}"


class FakeItem:
    def __init__(self, itemid):
        self.itemid = itemid
        self.title = f"Item {itemid[-2:]}"
        self.type = "Web Map"
        self.typeKeywords = []

    def get_data(self):
        return {}


class FakeContent:
    def __init__(self, items, count=None, count_raises=False):
        self._items = items
        self._count = count
        self._count_raises = count_raises

    def search(self, query, max_items=None, **kw):
        return self._items[:max_items] if max_items else self._items[:10]

    def advanced_search(self, query, return_count=False, **kw):
        if self._count_raises:
            raise RuntimeError("not supported on this portal")
        return self._count


class FakeGroup:
    def __init__(self, items, title="Templates"):
        self._items = items
        self.title = title

    def content(self, max_items=1000):
        return self._items[:max_items]


class FakeGroups:
    def __init__(self, group):
        self._group = group

    def get(self, name):
        return self._group


class FakeGIS:
    def __init__(self, content=None, group=None):
        self.content = content
        self.groups = FakeGroups(group)


def items(n):
    return [FakeItem(_id(i)) for i in range(1, n + 1)]


class TestCountMatches:
    @pytest.mark.parametrize(
        "returned,expected",
        [(52, 52), ("52", 52), ({"total": 52}, 52), ({"num": 52}, 52)],
    )
    def test_reads_a_count(self, returned, expected):
        gis = FakeGIS(content=FakeContent([], count=returned))
        assert count_matches(gis, "q") == expected

    def test_unsupported_portal_yields_none_not_an_error(self):
        gis = FakeGIS(content=FakeContent([], count_raises=True))
        assert count_matches(gis, "q") is None

    def test_a_boolean_is_not_mistaken_for_a_count(self):
        """`True` would otherwise int() to 1 and read as "one match"."""
        gis = FakeGIS(content=FakeContent([], count=True))
        assert count_matches(gis, "q") is None

    def test_unrecognized_shape_yields_none(self):
        gis = FakeGIS(content=FakeContent([], count={"unexpected": "shape"}))
        assert count_matches(gis, "q") is None


class TestQueryTruncation:
    def test_raises_when_more_match_than_were_fetched(self):
        """The reported case: 52 items in AGOL, 50 returned."""
        gis = FakeGIS(content=FakeContent(items(52), count=52))
        with pytest.raises(TruncatedError, match="matches 52 items but only 50"):
            collect(gis, query="title:VSCLR", limit=50)

    def test_error_names_a_limit_that_would_work(self):
        gis = FakeGIS(content=FakeContent(items(52), count=52))
        with pytest.raises(TruncatedError, match=r"--limit 62"):
            collect(gis, query="title:VSCLR", limit=50)

    def test_no_truncation_when_everything_fits(self):
        gis = FakeGIS(content=FakeContent(items(21), count=21))
        assert len(collect(gis, query="title:VSCLR", limit=1000)) == 21

    def test_falls_back_to_the_limit_heuristic_without_a_count(self):
        """Without an exact count, landing exactly on the limit is the tell."""
        gis = FakeGIS(content=FakeContent(items(80), count_raises=True))
        with pytest.raises(TruncatedError, match="probably more"):
            collect(gis, query="title:VSCLR", limit=50)

    def test_heuristic_does_not_fire_below_the_limit(self):
        gis = FakeGIS(content=FakeContent(items(21), count_raises=True))
        assert len(collect(gis, query="title:VSCLR", limit=50)) == 21

    def test_rejects_a_limit_above_the_agol_ceiling(self):
        gis = FakeGIS(content=FakeContent(items(5), count=5))
        with pytest.raises(ValueError, match="caps search"):
            collect(gis, query="q", limit=20_000)


class TestGroupTruncation:
    def test_raises_when_a_group_fills_the_limit(self):
        gis = FakeGIS(group=FakeGroup(items(60)))
        with pytest.raises(TruncatedError, match="probably more"):
            collect(gis, group="Templates", limit=50)

    def test_names_the_group_in_the_error(self):
        gis = FakeGIS(group=FakeGroup(items(60), title="AGOL Templates"))
        with pytest.raises(TruncatedError, match="AGOL Templates"):
            collect(gis, group="AGOL Templates", limit=50)

    def test_no_truncation_for_a_normal_template_group(self):
        gis = FakeGIS(group=FakeGroup(items(21)))
        assert len(collect(gis, group="Templates", limit=1000)) == 21


class TestDefaults:
    def test_default_limit_is_far_above_a_real_template_set(self):
        """The reported bug was a 50-item default against a 52-item set."""
        from agol_provision.discovery import DEFAULT_SEARCH_LIMIT

        assert DEFAULT_SEARCH_LIMIT >= 1000

    def test_default_does_not_truncate_a_52_item_org(self):
        gis = FakeGIS(content=FakeContent(items(52), count=52))
        assert len(collect(gis, query="title:VSCLR")) == 52


class FakeMultiGroups:
    """Resolves several groups by title, the way GroupManager.get does."""

    def __init__(self, groups_by_name):
        self._by_name = groups_by_name

    def get(self, name):
        return self._by_name.get(name)

    def search(self, query):
        return []


class TestMultipleGroups:
    """A template set is commonly spread across several groups -- one per consuming
    role -- so the union of them is the real template list."""

    def _gis(self, mapping):
        gis = FakeGIS()
        gis.groups = FakeMultiGroups(mapping)
        return gis

    def test_unions_items_across_groups(self):
        gis = self._gis({
            "Contractor": FakeGroup(items(3)[:3], "Contractor"),
            "QC": FakeGroup([FakeItem(_id(10)), FakeItem(_id(11))], "QC"),
        })
        got = collect(gis, group=["Contractor", "QC"], limit=1000)
        assert len(got) == 5

    def test_deduplicates_items_shared_to_several_groups(self):
        """The Design view is shared to both Contractor and QC; it is one item."""
        shared = FakeItem(_id(1))
        gis = self._gis({
            "Contractor": FakeGroup([shared, FakeItem(_id(2))], "Contractor"),
            "QC": FakeGroup([shared, FakeItem(_id(3))], "QC"),
        })
        got = collect(gis, group=["Contractor", "QC"], limit=1000)
        assert [i.itemid for i in got] == [_id(1), _id(2), _id(3)]

    def test_a_single_group_string_still_works(self):
        gis = self._gis({"Templates": FakeGroup(items(21), "Templates")})
        assert len(collect(gis, group="Templates", limit=1000)) == 21

    def test_unknown_group_names_the_offender(self):
        gis = self._gis({"Contractor": FakeGroup(items(2), "Contractor")})
        with pytest.raises(ValueError, match="Typo"):
            collect(gis, group=["Contractor", "Typo"], limit=1000)

    def test_truncation_in_any_one_group_still_raises(self):
        gis = self._gis({
            "Contractor": FakeGroup(items(2), "Contractor"),
            "QC": FakeGroup(items(60), "QC"),
        })
        with pytest.raises(TruncatedError, match="QC"):
            collect(gis, group=["Contractor", "QC"], limit=50)

"""Discovery's pure logic, exercised without touching AGOL.

The dependency scanner matters most: it is what derives the graph the whole
manifest is built on, and it has to work across item types whose JSON shapes are
completely different.
"""

import pytest

from agol_provision.discovery import (
    _common_title_prefix,
    _service_name_pattern,
    _title_pattern,
    classify,
    find_dependencies,
    item_json_text,
    suggest_key,
)


class FakeItem:
    """Stands in for arcgis.gis.Item -- only the attributes discovery reads."""

    def __init__(self, itemid="a" * 32, title="Item", type="Web Map",
                 typeKeywords=None, data=None, raises=False):
        self.itemid = itemid
        self.title = title
        self.type = type
        self.typeKeywords = typeKeywords or []
        self._data = data
        self._raises = raises

    def get_data(self):
        if self._raises:
            raise RuntimeError("no access")
        return self._data


def _id(n: int) -> str:
    return f"{n:032x}"


class TestClassify:
    def test_feature_service_without_view_keyword_is_the_master(self):
        assert classify(FakeItem(type="Feature Service", typeKeywords=["Hosted Service"])) == "master"

    def test_feature_service_with_view_keyword_is_a_view(self):
        item = FakeItem(type="Feature Service", typeKeywords=["Hosted Service", "View Service"])
        assert classify(item) == "view"

    def test_tolerates_whitespace_in_keywords(self):
        item = FakeItem(type="Feature Service", typeKeywords=[" View Service "])
        assert classify(item) == "view"

    @pytest.mark.parametrize(
        "type_,expected",
        [("Web Map", "map"), ("Dashboard", "app"), ("Web Experience", "app"),
         ("Web Mapping Application", "app"), ("PDF", "unknown")],
    )
    def test_maps_types_to_roles(self, type_, expected):
        assert classify(FakeItem(type=type_)) == expected


class TestFindDependencies:
    def test_finds_ids_nested_anywhere_in_the_json(self):
        """A dashboard buries layer ids in per-widget datasources, not a flat list."""
        target = _id(2)
        item = FakeItem(
            itemid=_id(1),
            data={"widgets": [{"datasets": [{"dataSource": {"itemId": target}}]}]},
        )
        assert find_dependencies(item, {_id(1), target}) == [target]

    def test_finds_ids_in_a_web_map_operational_layer(self):
        a, b = _id(2), _id(3)
        item = FakeItem(
            itemid=_id(1),
            data={"operationalLayers": [{"itemId": a}, {"itemId": b}]},
        )
        assert find_dependencies(item, {_id(1), a, b}) == sorted([a, b])

    def test_ignores_ids_that_are_not_templates(self):
        """Basemaps and unrelated org content reference ids too."""
        stranger = _id(99)
        item = FakeItem(itemid=_id(1), data={"baseMap": {"itemId": stranger}})
        assert find_dependencies(item, {_id(1), _id(2)}) == []

    def test_never_reports_itself(self):
        me = _id(1)
        item = FakeItem(itemid=me, data={"self": me, "other": _id(2)})
        assert me not in find_dependencies(item, {me, _id(2)})

    def test_deduplicates_repeated_references(self):
        target = _id(2)
        item = FakeItem(itemid=_id(1), data={"a": target, "b": target, "c": target})
        assert find_dependencies(item, {_id(1), target}) == [target]

    def test_unreadable_item_yields_no_dependencies_rather_than_crashing(self):
        item = FakeItem(itemid=_id(1), raises=True)
        assert find_dependencies(item, {_id(1), _id(2)}) == []

    def test_empty_data_yields_no_dependencies(self):
        assert find_dependencies(FakeItem(itemid=_id(1), data=None), {_id(2)}) == []


class TestItemJsonText:
    def test_serializes_dicts(self):
        assert "hello" in item_json_text(FakeItem(data={"k": "hello"}))

    def test_decodes_bytes(self):
        assert "hello" in item_json_text(FakeItem(data=b'{"k": "hello"}'))

    def test_swallows_read_errors(self):
        assert item_json_text(FakeItem(raises=True)) == ""


class TestTitlePatterns:
    def test_detects_the_shared_template_stem(self):
        titles = [
            "VSCLR Template - Design View",
            "VSCLR Template - QC View",
            "VSCLR Template - Testing View",
        ]
        assert _common_title_prefix(titles) == "VSCLR Template - "

    def test_no_shared_stem_returns_empty(self):
        assert _common_title_prefix(["Alpha", "Beta"]) == ""

    def test_single_title_has_no_inferable_stem(self):
        assert _common_title_prefix(["Only One"]) == ""

    def test_builds_a_title_pattern(self):
        pattern = _title_pattern("VSCLR Template - Design View", "VSCLR Template - ")
        assert pattern == "{base} - Design View"

    @pytest.mark.parametrize(
        "stem,sep",
        [("VSCLR Template", " - "), ("VSCLR Template", ": "), ("VSCLR Template", " | ")],
    )
    def test_reattaches_whichever_separator_the_stem_absorbed(self, stem, sep):
        """The shared stem swallows the separator; the pattern must put it back."""
        assert _title_pattern(f"{stem}{sep}Design View", f"{stem}{sep}") == (
            f"{{base}}{sep}Design View"
        )

    def test_title_equal_to_the_stem_becomes_bare_base(self):
        assert _title_pattern("VSCLR Template", "VSCLR Template") == "{base}"

    def test_builds_a_service_name_pattern(self):
        pattern = _service_name_pattern("VSCLR Template - Design View", "VSCLR Template - ")
        assert pattern == "{base_sn}_Design_View"

    def test_service_pattern_falls_back_to_bare_stem(self):
        assert _service_name_pattern("VSCLR Template", "VSCLR Template") == "{base_sn}"


class TestSuggestKey:
    def test_slugifies_a_title(self):
        assert suggest_key(FakeItem(title="Contractor / CX Redline View"), set()) == (
            "contractor_cx_redline_view"
        )

    def test_disambiguates_collisions(self):
        taken = {"design_view"}
        assert suggest_key(FakeItem(title="Design View"), taken) == "design_view_2"

    def test_untitled_item_still_gets_a_key(self):
        assert suggest_key(FakeItem(title="!!!"), set()) == "item"


class TestIdFileParsing:
    """`list-content --save-ids` writes "<id>  # <title>" so the file stays
    reviewable, and it is meant to be hand-edited before use. collect() has to
    tolerate what that produces."""

    class FakeGIS:
        def __init__(self, known):
            self.content = self
            self._known = known

        def get(self, iid):
            return self._known.get(iid)

    def _collect(self, lines, known):
        from agol_provision.discovery import collect

        return collect(self.FakeGIS(known), item_ids=lines)

    def test_strips_trailing_title_comments(self):
        item = FakeItem(itemid=_id(1))
        got = self._collect([f"{_id(1)}  # CompanyA Moline"], {_id(1): item})
        assert got == [item]

    def test_skips_blank_and_comment_only_lines(self):
        item = FakeItem(itemid=_id(1))
        got = self._collect(
            ["", "   ", "# everything below is a view", f"{_id(1)}"], {_id(1): item}
        )
        assert got == [item]

    def test_tolerates_surrounding_whitespace(self):
        item = FakeItem(itemid=_id(1))
        got = self._collect([f"   {_id(1)}   "], {_id(1): item})
        assert got == [item]

    def test_unknown_id_names_the_offending_value(self):
        with pytest.raises(ValueError, match=_id(2)):
            self._collect([f"{_id(2)}  # deleted template"], {})

    def test_error_does_not_leak_the_comment_into_the_id(self):
        """A comment surviving into the lookup would produce a confusing message."""
        with pytest.raises(ValueError) as exc:
            self._collect([f"{_id(2)}  # some title"], {})
        assert "some title" not in str(exc.value)

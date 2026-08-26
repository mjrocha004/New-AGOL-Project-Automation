"""Discovery proposes the groups section from how the templates are actually
shared. The template groups are not a project's groups, but their shape is: if
the Design template view sits in a contractor group, a project's Design view
belongs in that project's contractor group.
"""

import pytest

from agol_provision.discovery import TemplateItem, _derive_groups, _group_key, item_groups


class FakeGroupObj:
    def __init__(self, groupid, title):
        self.groupid, self.title = groupid, title


class FakeSharingGroups:
    def __init__(self, groups, raises=False):
        self._groups, self._raises = groups, raises

    def list(self):
        if self._raises:
            raise RuntimeError("no access")
        return self._groups


class FakeSharing:
    def __init__(self, groups, raises=False):
        self.groups = FakeSharingGroups(groups, raises)


class FakeItem:
    def __init__(self, title="Item", groups=None, raises=False):
        self.title, self.itemid, self.type, self.typeKeywords = title, "a" * 32, "Web Map", []
        self.sharing = FakeSharing(groups or [], raises)


def _template(key, role, groups):
    return TemplateItem(item=FakeItem(), role=role, key=key, groups=groups)


class TestItemGroups:
    def test_reads_group_id_and_title(self):
        item = FakeItem(groups=[FakeGroupObj("g1", "Contractor")])
        assert item_groups(item) == [("g1", "Contractor")]

    def test_sorted_for_stable_manifest_output(self):
        item = FakeItem(groups=[FakeGroupObj("g2", "QC"), FakeGroupObj("g1", "Contractor")])
        assert [t for _, t in item_groups(item)] == ["Contractor", "QC"]

    def test_unreadable_sharing_yields_empty_not_an_error(self):
        assert item_groups(FakeItem(raises=True)) == []

    def test_private_item_yields_empty(self):
        """The master is often shared to nothing at all."""
        assert item_groups(FakeItem(groups=[])) == []


class TestGroupKey:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Contractor", "contractor"),
            ("QC / Review", "qc_review"),
            ("VSCLR Template - Data Manager", "vsclr_template_data_manager"),
        ],
    )
    def test_slugifies(self, title, expected):
        assert _group_key(title) == expected

    def test_unusable_title_still_yields_a_key(self):
        assert _group_key("!!!") == "group"


class TestDeriveGroups:
    def test_one_entry_per_distinct_group(self):
        items = [
            _template("design", "view", [("g1", "Contractor"), ("g2", "QC")]),
            _template("qc", "view", [("g2", "QC")]),
        ]
        assert {g["key"] for g in _derive_groups(items, "")} == {"contractor", "qc"}

    def test_records_which_views_each_group_carries(self):
        items = [
            _template("design", "view", [("g1", "Contractor")]),
            _template("redline", "view", [("g1", "Contractor")]),
            _template("qc", "view", [("g2", "QC")]),
        ]
        by_key = {g["key"]: g for g in _derive_groups(items, "")}
        assert sorted(by_key["contractor"]["consumes"]) == ["design", "redline"]
        assert by_key["qc"]["consumes"] == ["qc"]

    def test_only_views_are_listed_as_consumed(self):
        """`consumes` on a group documents the views it carries, not the apps."""
        items = [
            _template("design", "view", [("g1", "Contractor")]),
            _template("field_map", "map", [("g1", "Contractor")]),
        ]
        assert _derive_groups(items, "")[0]["consumes"] == ["design"]

    def test_titles_become_project_patterns(self):
        items = [_template("design", "view", [("g1", "VSCLR Template - Contractor")])]
        assert _derive_groups(items, "VSCLR Template - ")[0]["title"] == "{base} - Contractor"

    def test_groups_default_to_private(self):
        items = [_template("design", "view", [("g1", "Contractor")])]
        assert _derive_groups(items, "")[0]["access"] == "private"

    def test_no_sharing_yields_no_groups(self):
        assert _derive_groups([_template("design", "view", [])], "") == []

    def test_output_is_deterministic(self):
        items = [_template("design", "view", [("g2", "QC"), ("g1", "Contractor")])]
        assert [g["key"] for g in _derive_groups(items, "")] == ["contractor", "qc"]

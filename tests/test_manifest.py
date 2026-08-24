"""Manifest validation exists to fail before any AGOL call, not during one."""

import pytest
import yaml

from agol_provision.manifest import Manifest, ManifestError, load_manifest

ID = "0123456789abcdef0123456789abcdef"


def _id(n: int) -> str:
    return f"{n:032x}"


def minimal(**overrides) -> dict:
    base = {
        "name": "test-standard",
        "version": 1,
        "source_org": "https://example.maps.arcgis.com",
        "master": {"template_item_id": _id(1)},
        "views": [
            {"key": "design", "template_item_id": _id(2),
             "title": "{base} - Design View", "service_name": "{base_sn}_Design"},
            {"key": "qc", "template_item_id": _id(3),
             "title": "{base} - QC View", "service_name": "{base_sn}_QC"},
        ],
        "groups": [{"key": "qc_group", "title": "{base} - QC", "consumes": ["design", "qc"]}],
        "maps": [
            {"key": "qc_map", "template_item_id": _id(4), "item_type": "Web Map",
             "title": "{base} - QC Review Map", "consumes": ["design", "qc"],
             "share_to": ["qc_group"]}
        ],
        "apps": [
            {"key": "exb_qc", "template_item_id": _id(5), "item_type": "Web Experience",
             "title": "{base} - QC Review", "consumes": ["qc_map"], "share_to": ["qc_group"]}
        ],
    }
    base.update(overrides)
    return base


class TestValidManifest:
    def test_parses(self):
        m = Manifest(**minimal())
        assert m.name == "test-standard"
        assert len(m.views) == 2

    def test_maps_are_ordered_before_apps(self):
        """Apps reference maps, so maps must be created first."""
        m = Manifest(**minimal())
        keys = [i.key for i in m.cloned_items]
        assert keys.index("qc_map") < keys.index("exb_qc")

    def test_counts_every_item_including_master_and_groups(self):
        m = Manifest(**minimal())
        assert m.total_items() == 6  # master + 2 views + 1 group + 1 map + 1 app

    def test_template_ids_exclude_groups(self):
        """Groups are created, not cloned, so no cloned JSON references a group id."""
        m = Manifest(**minimal())
        ids = m.template_item_ids()
        assert "qc_group" not in ids
        assert set(ids) == {"master", "design", "qc", "qc_map", "exb_qc"}


class TestItemIdValidation:
    @pytest.mark.parametrize("bad", ["", "abc", ID[:31], ID.upper(), "not-hex-" + "0" * 24])
    def test_rejects_malformed_item_ids(self, bad):
        with pytest.raises(Exception, match="hex AGOL item id"):
            Manifest(**minimal(master={"template_item_id": bad}))

    def test_tolerates_surrounding_whitespace(self):
        m = Manifest(**minimal(master={"template_item_id": f"  {_id(1)}  "}))
        assert m.master.template_item_id == _id(1)


class TestGraphValidation:
    def test_rejects_duplicate_keys_across_sections(self):
        """Keys index run state -- a collision would silently overwrite an item."""
        bad = minimal()
        bad["maps"][0]["key"] = "design"  # collides with a view
        with pytest.raises(Exception, match="Duplicate key 'design'"):
            Manifest(**bad)

    def test_rejects_consumes_pointing_at_nothing(self):
        bad = minimal()
        bad["maps"][0]["consumes"] = ["desgin"]  # typo
        with pytest.raises(Exception, match="consumes 'desgin'"):
            Manifest(**bad)

    def test_rejects_share_to_a_non_group(self):
        bad = minimal()
        bad["maps"][0]["share_to"] = ["design"]  # a view, not a group
        with pytest.raises(Exception, match="not a group"):
            Manifest(**bad)

    def test_rejects_unknown_fields(self):
        """extra=forbid catches a misspelled key rather than ignoring it."""
        bad = minimal()
        bad["maps"][0]["shares_to"] = ["qc_group"]
        with pytest.raises(Exception):
            Manifest(**bad)


class TestLoading:
    def test_round_trips_through_yaml(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text(yaml.safe_dump(minimal()))
        assert load_manifest(p).name == "test-standard"

    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(ManifestError, match="No manifest at"):
            load_manifest(tmp_path / "nope.yaml")

    def test_invalid_yaml_is_reported_clearly(self, tmp_path):
        p = tmp_path / "m.yaml"
        p.write_text("name: [unclosed")
        with pytest.raises(ManifestError, match="not valid YAML"):
            load_manifest(p)

    def test_validation_error_names_the_file(self, tmp_path):
        p = tmp_path / "m.yaml"
        bad = minimal()
        bad["maps"][0]["consumes"] = ["ghost"]
        p.write_text(yaml.safe_dump(bad))
        with pytest.raises(ManifestError, match="is invalid"):
            load_manifest(p)

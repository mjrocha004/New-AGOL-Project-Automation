"""The schema comparison decides whether the master-copy foundation is sound,
so its failure detection is worth pinning down precisely.
"""

import pytest

from agol_provision.schema_diff import (
    diff_fingerprints,
    fingerprint_layer,
    fingerprint_service,
    summarize,
)


def layer(name="Poles", geometry="esriGeometryPoint", fields=None,
          relationships=None, attachments=False, subtypes=0, indexes=None):
    return {
        "name": name,
        "geometryType": geometry,
        "fields": fields if fields is not None else [
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "nullable": False},
            {"name": "Status", "type": "esriFieldTypeString", "nullable": True,
             "domain": {"name": "StatusDomain", "type": "codedValue"}},
        ],
        "relationships": relationships or [],
        "hasAttachments": attachments,
        "subtypes": [{}] * subtypes,
        "indexes": indexes or [],
    }


class FakeLayer:
    def __init__(self, props):
        self.properties = props


class FakeFLC:
    def __init__(self, layers=None, tables=None, capabilities="Query,Editing",
                 editor_tracking=True):
        self.layers = [FakeLayer(p) for p in (layers or [])]
        self.tables = [FakeLayer(p) for p in (tables or [])]
        self.properties = {
            "capabilities": capabilities,
            "editorTrackingInfo": {"enableEditorTracking": editor_tracking},
        }


REL = {"name": "PoleToInspection", "cardinality": "esriRelCardinalityOneToMany",
       "role": "esriRelRoleOrigin", "keyField": "GlobalID"}


class TestFingerprint:
    def test_captures_fields_with_domains(self):
        fp = fingerprint_layer(layer())
        assert fp["fields"]["Status"]["domain"] == "StatusDomain"
        assert fp["fields"]["OBJECTID"]["domain"] is None

    def test_captures_relationships(self):
        fp = fingerprint_layer(layer(relationships=[REL]))
        assert fp["relationships"][0]["name"] == "PoleToInspection"

    def test_relationship_order_does_not_matter(self):
        r2 = {**REL, "name": "AnotherRel"}
        a = fingerprint_layer(layer(relationships=[REL, r2]))
        b = fingerprint_layer(layer(relationships=[r2, REL]))
        assert a["relationships"] == b["relationships"]

    def test_service_fingerprint_keys_by_layer_name(self):
        fp = fingerprint_service(FakeFLC(layers=[layer("Poles"), layer("Spans")]))
        assert set(fp["layers"]) == {"Poles", "Spans"}


class TestNoDifferences:
    def test_identical_services_produce_no_diffs(self):
        a = fingerprint_service(FakeFLC(layers=[layer(relationships=[REL])]))
        b = fingerprint_service(FakeFLC(layers=[layer(relationships=[REL])]))
        assert diff_fingerprints(a, b) == []

    def test_summary_reports_identical(self):
        assert summarize([]).startswith("IDENTICAL")


class TestCriticalDifferences:
    def test_dropped_relationship_is_critical(self):
        """The specific failure this spike exists to catch."""
        src = fingerprint_service(FakeFLC(layers=[layer(relationships=[REL])]))
        cpy = fingerprint_service(FakeFLC(layers=[layer(relationships=[])]))

        diffs = diff_fingerprints(src, cpy)
        assert len(diffs) == 1
        assert diffs[0].kind == "relationship"
        assert diffs[0].severity == "critical"
        assert "PoleToInspection" in diffs[0].detail
        assert summarize(diffs).startswith("NOT USABLE")

    def test_missing_layer_is_critical(self):
        src = fingerprint_service(FakeFLC(layers=[layer("Poles"), layer("Spans")]))
        cpy = fingerprint_service(FakeFLC(layers=[layer("Poles")]))
        diffs = diff_fingerprints(src, cpy)
        assert [d.where for d in diffs] == ["Spans"]
        assert diffs[0].severity == "critical"

    def test_missing_table_is_critical(self):
        """Related tables are the usual casualty of a lossy copy."""
        src = fingerprint_service(FakeFLC(tables=[layer("Inspections", geometry=None)]))
        cpy = fingerprint_service(FakeFLC(tables=[]))
        diffs = diff_fingerprints(src, cpy)
        assert diffs[0].kind == "table" and diffs[0].severity == "critical"

    def test_missing_field_is_critical(self):
        src = fingerprint_service(FakeFLC(layers=[layer()]))
        cpy = fingerprint_service(FakeFLC(layers=[layer(fields=[
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "nullable": False}])]))
        diffs = diff_fingerprints(src, cpy)
        assert any(d.kind == "field" and d.where == "Poles.Status" for d in diffs)

    def test_changed_field_type_is_critical(self):
        src = fingerprint_service(FakeFLC(layers=[layer()]))
        cpy = fingerprint_service(FakeFLC(layers=[layer(fields=[
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "nullable": False},
            {"name": "Status", "type": "esriFieldTypeInteger", "nullable": True}])]))
        kinds = {d.kind for d in diff_fingerprints(src, cpy)}
        assert "field_type" in kinds

    def test_changed_cardinality_is_critical(self):
        src = fingerprint_service(FakeFLC(layers=[layer(relationships=[REL])]))
        cpy = fingerprint_service(FakeFLC(layers=[layer(relationships=[
            {**REL, "cardinality": "esriRelCardinalityOneToOne"}])]))
        diffs = diff_fingerprints(src, cpy)
        assert diffs[0].kind == "relationship" and "cardinality" in diffs[0].detail


class TestNonCriticalDifferences:
    def test_dropped_domain_is_a_warning_not_a_blocker(self):
        src = fingerprint_service(FakeFLC(layers=[layer()]))
        cpy = fingerprint_service(FakeFLC(layers=[layer(fields=[
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "nullable": False},
            {"name": "Status", "type": "esriFieldTypeString", "nullable": True}])]))
        diffs = diff_fingerprints(src, cpy)
        assert [d.kind for d in diffs] == ["domain"]
        assert summarize(diffs).startswith("USABLE WITH FIXUPS")

    @pytest.mark.parametrize(
        "kwargs,kind",
        [
            ({"attachments": True}, "attachments"),
            ({"subtypes": 3}, "subtype"),
            ({"indexes": [{"name": "idx_status"}]}, "index"),
        ],
    )
    def test_settings_differences_are_warnings(self, kwargs, kind):
        src = fingerprint_service(FakeFLC(layers=[layer(**kwargs)]))
        cpy = fingerprint_service(FakeFLC(layers=[layer()]))
        diffs = diff_fingerprints(src, cpy)
        assert [d.kind for d in diffs] == [kind]
        assert diffs[0].severity == "warning"

    def test_capabilities_difference_is_informational(self):
        src = fingerprint_service(FakeFLC(layers=[layer()], capabilities="Query,Editing"))
        cpy = fingerprint_service(FakeFLC(layers=[layer()], capabilities="Query"))
        diffs = diff_fingerprints(src, cpy)
        assert diffs[0].severity == "info"
        assert summarize(diffs).startswith("USABLE WITH FIXUPS")

    def test_editor_tracking_difference_is_flagged(self):
        src = fingerprint_service(FakeFLC(layers=[layer()], editor_tracking=True))
        cpy = fingerprint_service(FakeFLC(layers=[layer()], editor_tracking=False))
        assert any(d.kind == "editor_tracking" for d in diff_fingerprints(src, cpy))


class TestSeverityRanking:
    def test_critical_dominates_the_verdict(self):
        """One dropped relationship outweighs any number of cosmetic differences."""
        src = fingerprint_service(FakeFLC(layers=[layer(relationships=[REL], attachments=True)]))
        cpy = fingerprint_service(FakeFLC(layers=[layer()]))
        assert summarize(diff_fingerprints(src, cpy)).startswith("NOT USABLE")

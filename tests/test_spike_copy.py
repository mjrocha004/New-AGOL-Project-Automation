"""How the spike copies the master.

`Item.copy_feature_layer_collection()` selects a *subset* of a service. Left to
its defaults it copies nothing and raises, and the values it selects with are
positional indexes rather than layer ids. Both are easy to get wrong and neither
fails until a live run, so they are pinned here.
"""

import pytest

from agol_provision.cli import copy_whole_service


class FakeLayer:
    def __init__(self, layer_id):
        self.properties = {"id": layer_id}


class FakeService:
    """Mimics the arcgis contract this code depends on.

    Two behaviours are reproduced from `arcgis.gis.Item`: it raises when given no
    selection at all, and it resolves a selection by `self.layers[idx]` -- a
    positional index, not a layer id.
    """

    def __init__(self, layer_ids, table_ids):
        self.layers = [FakeLayer(i) for i in layer_ids]
        self.tables = [FakeLayer(i) for i in table_ids]
        self.call = None

    def copy_feature_layer_collection(self, service_name, layers=None, tables=None):
        if layers is None and tables is None:
            raise ValueError("An index of layers or tables must be provided")
        self.call = {
            "service_name": service_name,
            "layers": [self.layers[i] for i in layers or []],
            "tables": [self.tables[i] for i in tables or []],
        }
        return "copied-item"


class TestCopyWholeService:
    def test_selects_every_layer_and_table(self):
        """The default of None copies nothing, so the selection must be explicit."""
        svc = FakeService(layer_ids=range(17), table_ids=[17])
        copy_whole_service(svc, "ZZZ_SPIKE_TEST_abcd1234")

        assert len(svc.call["layers"]) == 17
        assert len(svc.call["tables"]) == 1

    def test_indexes_positionally_rather_than_by_layer_id(self):
        """The real master's layer ids start at 11. Passing ids as indexes would
        run off the end of the list -- which is the failure this pins."""
        svc = FakeService(layer_ids=[11, 12, 13, 14, 15], table_ids=[])
        copy_whole_service(svc, "ZZZ_SPIKE_TEST_abcd1234")

        assert [lyr.properties["id"] for lyr in svc.call["layers"]] == [11, 12, 13, 14, 15]

    def test_passes_the_service_name_through(self):
        svc = FakeService(layer_ids=[0], table_ids=[])
        copy_whole_service(svc, "ZZZ_SPIKE_TEST_abcd1234")

        assert svc.call["service_name"] == "ZZZ_SPIKE_TEST_abcd1234"

    def test_a_service_with_no_tables_still_copies(self):
        """Most masters have none; an empty list must not read as 'unspecified'."""
        svc = FakeService(layer_ids=[0, 1], table_ids=[])
        assert copy_whole_service(svc, "ZZZ_SPIKE_TEST_abcd1234") == "copied-item"
        assert svc.call["tables"] == []


class TestDiagnoseFailedCopy:
    """The library creates the service, then posts the layers; when AGOL refuses
    the layers the item is lost inside the exception. The first time this
    happened the message named a .NET type and an empty service was left in the
    org. The diagnosis recovers the service (so the guarded delete can run) and
    asks AGOL layer by layer what it refused."""

    def _template(self):
        class Mgr:
            def __init__(self, props):
                self.properties = props

        class Lyr:
            def __init__(self, props):
                self.manager = Mgr(props)

        tmpl = type("Tmpl", (), {})()
        tmpl.title, tmpl.itemid = "Kinetic Master FS", "8a80d371" + "0" * 24
        tmpl.layers = [Lyr({"id": 0, "name": "Poles", "fields": []}),
                       Lyr({"id": 1, "name": "Spans", "fields": [], "subtypes": [{"code": 1}]})]
        tmpl.tables = [Lyr({"id": 2, "name": "Notes", "fields": []})]
        return tmpl

    def _gis(self, items):
        class Content:
            def search(self_, query, max_items=None):
                return items

        gis = type("GIS", (), {})()
        gis.content = Content()
        gis.users = type("U", (), {"me": type("Me", (), {"username": "martin"})()})()
        return gis

    def _orphan(self, spike_name, created):
        o = type("Item", (), {})()
        o.itemid, o.title, o.name, o.url = "orphan1", spike_name, spike_name, f"https://x/{spike_name}"
        o.owner, o.type, o.created = "martin", "Feature Service", created
        return o

    def test_recovers_the_orphan_and_names_the_layer_and_property(self, tmp_path):
        from agol_provision.cli import _diagnose_failed_copy
        from agol_provision.safety import spike_service_name

        spike_name = spike_service_name("8a80d371" + "0" * 24)
        orphan = self._orphan(spike_name, created=2_000_000)
        posted = []

        def add(json_dict):
            posted.append(json_dict)
            for entry in json_dict.get("layers", []) + json_dict.get("tables", []):
                if "subtypes" in entry:
                    raise Exception("Invalid definition for LayerCoreInfo")
            return {"success": True}

        report = tmp_path / "spike.md"
        got = _diagnose_failed_copy(
            self._gis([orphan]), self._template(), spike_name, Exception("boom"),
            since_ms=1_000_000, add_for=lambda item: add, report=report,
        )

        assert got is orphan
        text = report.read_text()
        assert "NOT USABLE" in text
        assert "**Spans** (layer): rejected" in text
        assert "**subtypes** were removed" in text
        assert "- Poles (layer): accepted" in text
        assert "- Notes (table): accepted" in text
        # The rejected definition is in the report, so the property can be read.
        assert '"name": "Spans"' in text

    def test_returns_none_when_the_orphan_cannot_be_found(self, tmp_path, monkeypatch):
        import agol_provision.cli as cli

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        got = cli._diagnose_failed_copy(
            self._gis([]), self._template(), "ZZZ_SPIKE_TEST_8a80d371", Exception("boom"),
            since_ms=1_000_000, add_for=lambda item: None, report=tmp_path / "spike.md",
        )
        assert got is None
        assert not (tmp_path / "spike.md").exists()

    def test_a_leftover_from_an_earlier_run_is_not_touched(self, tmp_path, monkeypatch):
        """Created before this run started, so not this run's to delete --
        even with the exact spike name."""
        import agol_provision.cli as cli
        from agol_provision.safety import spike_service_name

        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        spike_name = spike_service_name("8a80d371" + "0" * 24)
        old = self._orphan(spike_name, created=500_000)
        got = cli._diagnose_failed_copy(
            self._gis([old]), self._template(), spike_name, Exception("boom"),
            since_ms=1_000_000, add_for=lambda item: None, report=tmp_path / "spike.md",
        )
        assert got is None

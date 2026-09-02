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

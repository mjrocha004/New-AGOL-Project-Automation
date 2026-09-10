"""How stage 1 copies the master, now that the copy is ours.

`Item.copy_feature_layer_collection()` posts every layer definition verbatim,
`relationships` included -- and a relationship names its related layer by the
template's layer id, which AGOL renumbers on the way in. The Kinetic master
(2 relationships) was refused outright with a 400 that named a .NET type. The
library cannot be told to strip them, so the copy is done here the way Esri's
own `clone_items` does it: create the service, post the layers without their
relationships, then add the relationships back with the ids remapped.
"""

import copy

import pytest

from agol_provision.master import (
    ALREADY_PRESENT,
    APPLIED,
    FAILED,
    LAYER_STRIPS,
    MasterError,
    add_layer_definitions,
    create_empty_service,
    layer_definitions,
    reapply_relationships,
    service_params,
)


class FakeManager:
    def __init__(self, props=None, reject=None):
        self.properties = props if props is not None else {}
        self.calls = []
        self._reject = reject

    def add_to_definition(self, json_dict):
        self.calls.append(copy.deepcopy(json_dict))
        if self._reject:
            msg = self._reject(json_dict)
            if msg:
                raise RuntimeError(msg)
        return {"success": True}


class FakeLayer:
    def __init__(self, props):
        self.properties = props
        self.manager = FakeManager(props)


class FakeFLC:
    def __init__(self, layers=(), tables=(), properties=None, reject=None):
        self.layers = list(layers)
        self.tables = list(tables)
        self.properties = properties or {}
        self.manager = FakeManager(reject=reject)


def rel(name, related_id, role, key="GlobalID"):
    return {"id": 0, "name": name, "relatedTableId": related_id,
            "cardinality": "esriRelCardinalityOneToMany", "role": role,
            "keyField": key, "composite": False}


def layer(layer_id, name, **extra):
    return FakeLayer({"id": layer_id, "name": name, "type": "Feature Layer",
                      "fields": [{"name": "GlobalID", "type": "esriFieldTypeGlobalID"}],
                      **extra})


# The Kinetic shape: Equipment Redline (15) is the origin, Test Results Redline
# (20) the destination, with ids that will not survive the copy.
def kinetic_template():
    return FakeFLC(
        layers=[
            layer(11, "Redline Details"),
            layer(15, "Equipment Redline",
                  relationships=[rel("Test_Results_Redline", 20, "esriRelRoleOrigin")]),
            layer(20, "Test Results Redline",
                  relationships=[rel("Equipment_Redline", 15, "esriRelRoleDestination",
                                     key="parentguid")]),
        ],
        tables=[layer(22, "RateCodeApplied")],
        properties={"capabilities": "Query,Editing", "name": "Kinetic_Master_FS",
                    "editorTrackingInfo": {"enableEditorTracking": True},
                    "somethingAgolWillNotTake": True},
    )


def renumbered_copy():
    """What AGOL hands back: the same layers in order, ids from 0."""
    return FakeFLC(
        layers=[layer(0, "Redline Details"), layer(1, "Equipment Redline"),
                layer(2, "Test Results Redline")],
        tables=[layer(3, "RateCodeApplied")],
    )


class TestServiceParams:
    def test_carries_the_service_settings_the_library_carries(self):
        params = service_params(kinetic_template().properties, "TestCompany_DeWitt")
        assert params["capabilities"] == "Query,Editing"
        assert params["editorTrackingInfo"] == {"enableEditorTracking": True}

    def test_names_the_new_service_and_drops_what_agol_would_refuse(self):
        params = service_params(kinetic_template().properties, "TestCompany_DeWitt")
        assert params["name"] == "TestCompany_DeWitt"
        assert params["_ssl"] is False
        assert "somethingAgolWillNotTake" not in params


class TestLayerDefinitions:
    def test_posts_layers_and_tables_in_template_order(self):
        defs = layer_definitions(kinetic_template())
        assert [d["name"] for d in defs["layers"]] == [
            "Redline Details", "Equipment Redline", "Test Results Redline"]
        assert [d["name"] for d in defs["tables"]] == ["RateCodeApplied"]

    def test_strips_relationships_as_well_as_what_the_library_strips(self):
        """Relationships name the related layer by an id the copy renumbers.
        Posted as-is they are refused; posted stripped they go back afterwards."""
        assert set(LAYER_STRIPS) == {"indexes", "adminLayerInfo", "relationships"}
        template = kinetic_template()
        template.layers[1].properties["indexes"] = [{"name": "i"}]
        template.layers[1].properties["adminLayerInfo"] = {"x": 1}
        defs = layer_definitions(template)
        equipment = defs["layers"][1]
        assert "relationships" not in equipment
        assert "indexes" not in equipment
        assert "adminLayerInfo" not in equipment

    def test_never_touches_the_template(self):
        template = kinetic_template()
        layer_definitions(template)
        assert "relationships" in template.layers[1].properties


class TestCreateEmptyService:
    class FakeContent:
        def __init__(self, returns="item"):
            self.calls = []
            self._returns = returns

        def create_service(self, name, create_params=None, item_properties=None, **kw):
            self.calls.append({"name": name, "create_params": create_params,
                               "item_properties": item_properties})
            return self._returns

    def test_creates_under_the_given_name_with_the_template_settings(self):
        content = self.FakeContent()
        gis = type("GIS", (), {"content": content})()
        item = create_empty_service(gis, kinetic_template(), "TestCompany_DeWitt",
                                    title="TestCompany DeWitt", tags=["a"])
        assert item == "item"
        call = content.calls[0]
        assert call["name"] == "TestCompany_DeWitt"
        assert call["create_params"]["name"] == "TestCompany_DeWitt"
        assert call["create_params"]["capabilities"] == "Query,Editing"
        assert call["item_properties"]["title"] == "TestCompany DeWitt"
        assert call["item_properties"]["tags"] == ["a"]

    def test_a_none_result_is_an_error_not_a_none(self):
        gis = type("GIS", (), {"content": self.FakeContent(returns=None)})()
        with pytest.raises(MasterError, match="create_service"):
            create_empty_service(gis, kinetic_template(), "X", title="X")


class TestAddLayerDefinitions:
    def test_posts_every_layer_and_table_in_one_call(self):
        new = FakeFLC()
        posted = add_layer_definitions(kinetic_template(), new)
        assert len(new.manager.calls) == 1
        assert new.manager.calls[0] == posted
        assert len(posted["layers"]) == 3 and len(posted["tables"]) == 1

    def test_agols_refusal_becomes_a_master_error_with_its_message(self):
        new = FakeFLC(reject=lambda d: "Invalid definition for LayerCoreInfo (Error Code: 400)")
        with pytest.raises(MasterError, match="LayerCoreInfo"):
            add_layer_definitions(kinetic_template(), new)


class TestReapplyRelationships:
    def test_remaps_related_table_ids_by_layer_name_and_posts_once(self):
        """Template ids 15/20 become whatever the copy gave those layers."""
        new = renumbered_copy()
        outcomes = reapply_relationships(kinetic_template(), new)

        assert [(o.layer, o.relationship, o.status) for o in outcomes] == [
            ("Equipment Redline", "Test_Results_Redline", APPLIED),
            ("Test Results Redline", "Equipment_Redline", APPLIED),
        ]
        assert len(new.manager.calls) == 1
        posted = {l["id"]: l["relationships"] for l in new.manager.calls[0]["layers"]}
        assert posted[1][0]["relatedTableId"] == 2   # Equipment -> Test Results
        assert posted[2][0]["relatedTableId"] == 1   # Test Results -> Equipment
        # Everything else about the relationship is carried as-is.
        assert posted[2][0]["keyField"] == "parentguid"
        assert posted[2][0]["role"] == "esriRelRoleDestination"

    def test_posts_only_id_and_relationships_per_layer(self):
        """The service-level call clone uses carries nothing else -- a full
        layer definition here would be an attempt to add a second layer."""
        new = renumbered_copy()
        reapply_relationships(kinetic_template(), new)
        for entry in new.manager.calls[0]["layers"]:
            assert set(entry) == {"id", "relationships"}

    def test_a_template_without_relationships_posts_nothing(self):
        new = renumbered_copy()
        template = kinetic_template()
        for lyr in template.layers:
            lyr.properties.pop("relationships", None)
        assert reapply_relationships(template, new) == []
        assert new.manager.calls == []

    def test_already_present_on_a_rerun(self):
        """Re-running provision repairs in place; a relationship that is already
        there must be recognised, not posted again."""
        new = renumbered_copy()
        new.layers[1].properties["relationships"] = [rel("Test_Results_Redline", 2, "esriRelRoleOrigin")]
        new.layers[2].properties["relationships"] = [rel("Equipment_Redline", 1, "esriRelRoleDestination")]
        outcomes = reapply_relationships(kinetic_template(), new)
        assert {o.status for o in outcomes} == {ALREADY_PRESENT}
        assert new.manager.calls == []

    def test_agols_refusal_fails_every_pending_relationship_with_its_message(self):
        new = renumbered_copy()
        new.manager._reject = lambda d: "Unable to add feature service definition. (Error Code: 400)"
        outcomes = reapply_relationships(kinetic_template(), new)
        assert {o.status for o in outcomes} == {FAILED}
        assert all("400" in o.detail for o in outcomes)

    def test_a_related_layer_missing_from_the_copy_is_named_not_guessed(self):
        new = renumbered_copy()
        del new.layers[2]  # Test Results Redline never arrived
        outcomes = reapply_relationships(kinetic_template(), new)
        by_layer = {o.layer: o for o in outcomes}
        assert by_layer["Equipment Redline"].status == FAILED
        assert "Test Results Redline" in by_layer["Equipment Redline"].detail
        assert by_layer["Test Results Redline"].status == FAILED
        assert new.manager.calls == []

    def test_does_not_mutate_the_template(self):
        template = kinetic_template()
        reapply_relationships(template, renumbered_copy())
        assert template.layers[1].properties["relationships"][0]["relatedTableId"] == 20

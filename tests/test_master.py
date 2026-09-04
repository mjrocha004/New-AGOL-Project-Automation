"""Stage 1: the master service, and the indexes the copy drops.

`copy_feature_layer_collection()` strips each layer's `indexes` before applying
the definition, so a faithful copy still arrives without them. The spike measured
83 missing index entries; 73 of those are system-generated names that could never
have matched, and 10 are real. `build_status_Index` is the one that matters --
`build_status` is the field every view's definition query filters on, so a master
without it makes all seven views table-scan.

Classification is therefore by *fields*, never by name: the system names carry
random suffixes and owner ids, and `I25bore_depth` shows the `I##` prefix appears
on user indexes too.
"""

import pytest

from agol_provision.master import (
    MasterError,
    index_fields,
    is_user_defined,
    reapply_user_indexes,
    system_field_names,
    user_defined_indexes,
)


def idx(name, fields):
    """One index as AGOL reports it -- `fields` is a comma-delimited string."""
    return {"name": name, "fields": fields, "isUnique": False, "isAscending": True}


def layer_props(name="redline", indexes=(), extra_fields=(), layer_id=0):
    """A layer definition carrying the system fields every hosted layer has."""
    fields = [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "GlobalID", "type": "esriFieldTypeGlobalID"},
        {"name": "Shape", "type": "esriFieldTypeGeometry"},
        {"name": "CreationDate", "type": "esriFieldTypeDate"},
        {"name": "Creator", "type": "esriFieldTypeString"},
        {"name": "EditDate", "type": "esriFieldTypeDate"},
        {"name": "Editor", "type": "esriFieldTypeString"},
        *({"name": f, "type": "esriFieldTypeString"} for f in extra_fields),
    ]
    return {
        "id": layer_id,
        "name": name,
        "objectIdField": "OBJECTID",
        "globalIdField": "GlobalID",
        "editFieldsInfo": {
            "creationDateField": "CreationDate",
            "creatorField": "Creator",
            "editDateField": "EditDate",
            "editorField": "Editor",
        },
        "fields": fields,
        "indexes": list(indexes),
    }


# ---------------------------------------------------------------- fakes


class FakeManager:
    def __init__(self, raises=None, rejects=(), props=None):
        self.calls = []
        self._raises = raises
        self._rejects = set(rejects)
        self._props = props if props is not None else {}

    def update_definition(self, json_dict):
        self.calls.append(json_dict)
        if self._raises:
            raise self._raises
        return {"success": True}

    def add_to_definition(self, json_dict):
        self.calls.append(json_dict)
        if self._raises:
            raise self._raises
        names = {i["name"] for i in json_dict.get("indexes", [])}
        if names & self._rejects:
            # AGOL rejects the whole call if any one index in it is invalid.
            raise RuntimeError("Unable to add feature service layer definition.\n"
                               "Invalid definition for FieldIndex\n(Error Code: 400)")
        # An applied index is really there afterwards, so a second run sees it.
        self._props.setdefault("indexes", []).extend(
            dict(i) for i in json_dict.get("indexes", [])
        )
        return {"success": True}


class FakeLayer:
    def __init__(self, props, raises=None, rejects=()):
        self.properties = props
        self.contingent_values = None
        self.manager = FakeManager(raises, rejects, props)


class FakeFLC:
    def __init__(self, layers=(), tables=()):
        self.layers = list(layers)
        self.tables = list(tables)


# ---------------------------------------------------------------- classification


class TestSystemFieldNames:
    def test_collects_objectid_globalid_shape_and_editor_tracking(self):
        assert system_field_names(layer_props()) == {
            "objectid", "globalid", "shape", "creationdate", "creator", "editdate", "editor",
        }

    def test_a_plain_attribute_is_not_a_system_field(self):
        assert "build_status" not in system_field_names(
            layer_props(extra_fields=["build_status"])
        )

    def test_tolerates_a_layer_with_no_editor_tracking(self):
        props = layer_props()
        del props["editFieldsInfo"]
        assert "objectid" in system_field_names(props)

    def test_finds_a_shape_field_under_any_name(self):
        """The geometry field is not always called Shape."""
        props = layer_props()
        props["fields"][2] = {"name": "SHAPE_1", "type": "esriFieldTypeGeometry"}
        assert "shape_1" in system_field_names(props)


class TestIndexFields:
    def test_splits_the_comma_delimited_string(self):
        assert index_fields(idx("i", "build_status,route_id")) == ["build_status", "route_id"]

    def test_lowercases_and_strips(self):
        assert index_fields(idx("i", " Build_Status , Route_ID ")) == ["build_status", "route_id"]

    def test_tolerates_a_list_instead_of_a_string(self):
        assert index_fields(idx("i", ["build_status"])) == ["build_status"]


class TestIsUserDefined:
    """The spec's own classification table, case by case."""

    @pytest.fixture
    def props(self):
        return layer_props(extra_fields=["build_status", "bore_depth"])

    @pytest.mark.parametrize("name,fields", [
        ("build_status_Index", "build_status"),
        ("I25bore_depth", "bore_depth"),
    ])
    def test_keeps_the_ten_real_ones(self, name, fields, props):
        assert is_user_defined(idx(name, fields), props)

    @pytest.mark.parametrize("name,fields", [
        ("PK__ZAYO_CHI__F4B70D85A1B2C3D4", "OBJECTID"),
        ("user_57996.ZAYO_CHICAGO_REDLINE_Shape_sidx", "Shape"),
        ("FDO_GlobalID", "GlobalID"),
        ("GlobalID_Index", "GlobalID"),
        ("I13Creator", "Creator"),
        ("I14CreationDate", "CreationDate"),
        ("I15Editor", "Editor"),
        ("I16EditDate", "EditDate"),
    ])
    def test_drops_the_system_generated_ones(self, name, fields, props):
        assert not is_user_defined(idx(name, fields), props)

    def test_classifies_by_fields_not_by_name(self):
        """The decisive case: a user-looking name over a system field is still system.

        Names carry random suffixes and owner ids, and `I25bore_depth` proves the
        `I##` prefix appears on user indexes too, so the name cannot be trusted
        either way.
        """
        assert not is_user_defined(idx("build_status_Index", "OBJECTID"), layer_props())

    def test_keeps_a_composite_index_that_includes_a_system_field(self):
        """Only an index that is *exactly* one system field is system-generated."""
        props = layer_props(extra_fields=["build_status"])
        assert is_user_defined(idx("compound", "build_status,OBJECTID"), props)

    def test_drops_a_spatial_index_when_the_layer_omits_its_geometry_field(self):
        """The bug that failed the first live run.

        AGOL does not list the geometry field in a layer's `fields`, so looking
        for `esriFieldTypeGeometry` finds nothing and `..._Shape_sidx` reads as a
        user index. Reapplying it is rejected, and -- batched -- it took all ten
        real indexes down with it.
        """
        props = layer_props(extra_fields=["build_status"])
        props["fields"] = [f for f in props["fields"] if f["name"] != "Shape"]
        assert not is_user_defined(idx("user_57996.ZAYO_X_Shape_sidx", "Shape"), props)

    def test_still_keeps_a_real_index_on_a_layer_with_no_geometry_field(self):
        props = layer_props(extra_fields=["build_status"])
        props["fields"] = [f for f in props["fields"] if f["name"] != "Shape"]
        assert is_user_defined(idx("build_status_Index", "build_status"), props)

    def test_falls_back_to_the_system_rule_when_a_layer_lists_no_fields(self):
        """A layer definition without `fields` must not silently drop everything."""
        props = layer_props()
        props["fields"] = []
        assert is_user_defined(idx("build_status_Index", "build_status"), props)
        assert not is_user_defined(idx("PK__X__99", "OBJECTID"), props)


class TestUserDefinedIndexes:
    def test_returns_only_the_user_indexes_of_one_layer(self):
        props = layer_props(
            extra_fields=["build_status"],
            indexes=[
                idx("PK__ZAYO_CHI__99", "OBJECTID"),
                idx("build_status_Index", "build_status"),
                idx("FDO_GlobalID", "GlobalID"),
            ],
        )
        assert [i["name"] for i in user_defined_indexes(props)] == ["build_status_Index"]

    def test_a_layer_with_no_indexes_yields_nothing(self):
        assert user_defined_indexes(layer_props()) == []


class TestTheLiveMaster:
    """The shape of the real template, as the first live run revealed it.

    17 layers and 1 table. Every layer carries a spatial index over `Shape`, a
    primary key over OBJECTID, a GlobalID index and four editor-tracking indexes
    -- and none of them lists `Shape` in `fields`. Nine redline layers carry
    `build_status_Index`; `Redline_Details` also carries `I25bore_depth`.

    Exactly ten indexes should survive classification. The first live run kept 31
    and applied none.
    """

    REDLINE = ["Redline_Details", "Splice_Point_Redline", "Slack_Loop_Redline",
               "Pole_Redline", "Equipment_Redline", "Building_Redline",
               "Access_Point_Redline", "Span_Redline", "Fiber_Cable_Redline"]
    DESIGN = ["Splice_Closures_Design", "Slack_Loop_Design", "Pole_Design",
              "Equipment_Design", "Building_Design", "Access_Point_Design",
              "Fiber_Cable_Design", "Span_Design"]

    def layer(self, name):
        extra = ["build_status", "bore_depth"] if name in self.REDLINE else ["notes"]
        props = layer_props(name, extra_fields=extra)
        # AGOL does not list the geometry field. This is the whole bug.
        props["fields"] = [f for f in props["fields"] if f["name"] != "Shape"]
        props["indexes"] = [
            idx(f"user_57996.ZAYO_CHICAGO_{name.upper()}_Shape_sidx", "Shape"),
            idx("PK__ZAYO_CHI__F4B70D85A1B2C3D4", "OBJECTID"),
            idx("FDO_GlobalID", "GlobalID"),
            idx("I13Creator", "Creator"),
            idx("I14CreationDate", "CreationDate"),
            idx("I15Editor", "Editor"),
            idx("I16EditDate", "EditDate"),
        ]
        if name in self.REDLINE:
            props["indexes"].append(idx("build_status_Index", "build_status"))
        if name == "Redline_Details":
            props["indexes"].append(idx("I25bore_depth", "bore_depth"))
        return props

    def test_exactly_ten_indexes_survive_across_the_whole_service(self):
        kept = [
            i["name"]
            for name in self.REDLINE + self.DESIGN
            for i in user_defined_indexes(self.layer(name))
        ]
        assert len(kept) == 10
        assert kept.count("build_status_Index") == 9
        assert kept.count("I25bore_depth") == 1

    def test_no_spatial_index_is_ever_attempted(self):
        """Reapplying one is rejected, and batched it took the ten real ones down."""
        kept = [
            i["name"]
            for name in self.REDLINE + self.DESIGN
            for i in user_defined_indexes(self.layer(name))
        ]
        assert not [n for n in kept if n.endswith("_sidx")]


# ---------------------------------------------------------------- reapplication


def template_and_copy(indexes, copy_indexes=(), name="redline", raises=None, rejects=()):
    """A template layer carrying `indexes`, and the freshly copied layer."""
    template = FakeLayer(layer_props(name, indexes, extra_fields=["build_status", "bore_depth"]))
    copy = FakeLayer(
        layer_props(name, copy_indexes, extra_fields=["build_status", "bore_depth"]),
        raises=raises, rejects=rejects,
    )
    return FakeFLC([template]), FakeFLC([copy]), copy


class TestReapplyUserIndexes:
    def test_applies_the_user_indexes_and_nothing_else(self):
        tpl, new, copy = template_and_copy([
            idx("PK__ZAYO_CHI__99", "OBJECTID"),
            idx("build_status_Index", "build_status"),
        ])
        outcomes = reapply_user_indexes(tpl, new)
        assert copy.manager.calls == [{"indexes": [idx("build_status_Index", "build_status")]}]
        assert [(o.index, o.status) for o in outcomes] == [("build_status_Index", "applied")]

    def test_batches_one_call_per_layer(self):
        tpl, new, copy = template_and_copy([
            idx("build_status_Index", "build_status"),
            idx("I25bore_depth", "bore_depth"),
        ])
        reapply_user_indexes(tpl, new)
        assert len(copy.manager.calls) == 1
        assert len(copy.manager.calls[0]["indexes"]) == 2

    def test_makes_no_call_for_a_layer_with_no_user_indexes(self):
        tpl, new, copy = template_and_copy([idx("FDO_GlobalID", "GlobalID")])
        assert reapply_user_indexes(tpl, new) == []
        assert copy.manager.calls == []

    def test_skips_an_index_the_copy_already_carries(self):
        tpl, new, copy = template_and_copy(
            [idx("build_status_Index", "build_status")],
            copy_indexes=[idx("build_status_Index", "build_status")],
        )
        outcomes = reapply_user_indexes(tpl, new)
        assert copy.manager.calls == []
        assert [o.status for o in outcomes] == ["already present"]

    def test_tolerates_a_duplicate_error_from_agol(self):
        """The spec calls these tolerated rather than fatal."""
        tpl, new, copy = template_and_copy(
            [idx("build_status_Index", "build_status")],
            raises=RuntimeError("Duplicate index name build_status_Index"),
        )
        outcomes = reapply_user_indexes(tpl, new)
        assert [o.status for o in outcomes] == ["already present"]

    def test_reports_a_genuine_failure_without_raising(self):
        """One bad layer must not abandon the other seventeen."""
        tpl, new, copy = template_and_copy(
            [idx("build_status_Index", "build_status")],
            raises=RuntimeError("Invalid definition"),
        )
        outcomes = reapply_user_indexes(tpl, new)
        assert [o.status for o in outcomes] == ["failed"]
        assert "Invalid definition" in outcomes[0].detail

    def test_one_rejected_index_does_not_take_the_others_down(self):
        """The second fault in the first live run.

        AGOL rejects the entire add_to_definition call if any index in it is
        invalid, so batching per layer meant one bad index lost every good one on
        that layer. On a batch failure each index is retried alone.
        """
        tpl, new, copy = template_and_copy(
            [idx("bad_index", "build_status"), idx("I25bore_depth", "bore_depth")],
            rejects={"bad_index"},
        )
        outcomes = reapply_user_indexes(tpl, new)
        by_index = {o.index: o.status for o in outcomes}
        assert by_index == {"bad_index": "failed", "I25bore_depth": "applied"}

    def test_a_single_index_is_not_retried_after_it_fails(self):
        """Nothing to salvage, and a second rejected call is a wasted request."""
        tpl, new, copy = template_and_copy(
            [idx("bad_index", "build_status")], rejects={"bad_index"}
        )
        reapply_user_indexes(tpl, new)
        assert len(copy.manager.calls) == 1

    def test_the_error_detail_is_collapsed_to_one_line(self):
        """AGOL returns four lines per rejection; 27 of those buried the summary."""
        tpl, new, copy = template_and_copy(
            [idx("bad_index", "build_status")], rejects={"bad_index"}
        )
        assert "\n" not in reapply_user_indexes(tpl, new)[0].detail

    def test_covers_tables_as_well_as_layers(self):
        tpl_layer = FakeLayer(layer_props("redline", [], extra_fields=["build_status"]))
        tpl_table = FakeLayer(
            layer_props("notes", [idx("note_idx", "build_status")], extra_fields=["build_status"])
        )
        copy_layer = FakeLayer(layer_props("redline", [], extra_fields=["build_status"]))
        copy_table = FakeLayer(layer_props("notes", [], extra_fields=["build_status"]))
        outcomes = reapply_user_indexes(
            FakeFLC([tpl_layer], [tpl_table]), FakeFLC([copy_layer], [copy_table])
        )
        assert [o.index for o in outcomes] == ["note_idx"]
        assert len(copy_table.manager.calls) == 1

    def test_refuses_to_apply_anything_if_the_layers_do_not_line_up(self):
        """Applying an index to the wrong layer is a silent wrong answer.

        The copy is made with positional indexes into `Item.layers`, so position
        is the correspondence -- but it is checked, not assumed.
        """
        tpl = FakeFLC([FakeLayer(layer_props("redline", [idx("i", "build_status")]))])
        copy = FakeLayer(layer_props("something_else"))
        with pytest.raises(MasterError, match="do not line up"):
            reapply_user_indexes(tpl, FakeFLC([copy]))
        assert copy.manager.calls == []

    def test_refuses_when_the_copy_has_a_different_layer_count(self):
        tpl = FakeFLC([FakeLayer(layer_props("a")), FakeLayer(layer_props("b"))])
        with pytest.raises(MasterError):
            reapply_user_indexes(tpl, FakeFLC([FakeLayer(layer_props("a"))]))


# ---------------------------------------------------------------- the command

import json

import pytest as _pytest  # noqa: F811  (already imported; kept local to this section)

from agol_provision.auth import REQUIRED_PRIVILEGES

TEMPLATE_MASTER = "1" * 32
TEMPLATE_VIEW = "2" * 32


class FakeService:
    """A Feature Service item: enough of it for stage 1 and for --destroy."""

    def __init__(self, itemid, title, layer_names=("redline",), registry=None,
                 typeKeywords=("Hosted Service",), indexes=None, copy_returns=True,
                 capabilities="Query", queries=None, layer_ids=None,
                 snippet="", description="", tags=None, contingent=None):
        self.itemid = itemid
        self.title = title
        self.snippet = snippet
        self.description = description
        self.tags = list(tags or [])
        self.type = "Feature Service"
        self.typeKeywords = list(typeKeywords)
        # Layer ids deliberately do not start at 0 and are not contiguous: the
        # real master's 17 layers carry ids running to 19.
        ids = layer_ids or [11 + i for i in range(len(layer_names))]
        self.layers = []
        for layer_id, n in zip(ids, layer_names):
            props = layer_props(n, indexes or [], extra_fields=["build_status"],
                                layer_id=layer_id)
            if queries and n in queries:
                props["viewDefinitionQuery"] = queries[n]
            layer = FakeLayer(props)
            layer.contingent_values = contingent
            self.layers.append(layer)
        self.tables = []
        self.url = (
            f"https://services.arcgis.com/org/arcgis/rest/services/{title}/FeatureServer"
        )
        self.properties = {"capabilities": capabilities}
        self.manager = FakeServiceManager(self)
        self.updates = []
        self.deleted = False
        self.copy_calls = []
        self._registry = registry if registry is not None else {}
        self._copy_returns = copy_returns
        self._registry[itemid] = self

    @property
    def _next_id(self):
        return f"{len(self._registry):032x}"

    def copy_feature_layer_collection(self, service_name, layers=None, tables=None):
        self.copy_calls.append({"service_name": service_name, "layers": layers, "tables": tables})
        if not self._copy_returns:
            return None
        # The copy is named after the *service*, arrives with no indexes, and
        # RENUMBERS the layers: ids do not survive copy_feature_layer_collection.
        # Names do, which is why the views stage matches on them.
        copy = FakeService(
            itemid="c" * 32, title=service_name,
            layer_names=[layer.properties["name"] for layer in self.layers],
            layer_ids=list(range(len(self.layers))),
            registry=self._registry,
        )
        # AGOL recreates the system indexes under its own names -- except any
        # over GlobalID, which the live copy carries on none of its 18 layers.
        # The user-defined ones are the copy's real loss, which is why stage 1
        # exists.
        for src, dst in zip(self.layers, copy.layers):
            user = {i["name"] for i in user_defined_indexes(src.properties)}
            dst.properties["indexes"] = [
                dict(i) for i in src.properties.get("indexes", []) or []
                if i["name"] not in user and index_fields(i) != ["globalid"]
            ]
        return copy

    def update(self, item_properties=None, **kwargs):
        props = item_properties or kwargs
        self.updates.append(props)
        self.title = props.get("title", self.title)
        self.snippet = props.get("snippet", self.snippet)
        self.description = props.get("description", self.description)
        self.tags = props.get("tags", self.tags)
        return True

    def delete(self):
        self.deleted = True
        return True


class FakeServiceManager:
    """The FeatureLayerCollection manager: create_view lives here."""

    def __init__(self, service):
        self.service = service
        self.create_view_calls = []

    def create_view(self, **kwargs):
        self.create_view_calls.append(kwargs)
        names = [lyr.properties["name"] for lyr in kwargs.get("view_layers") or []]
        ids = [lyr.properties["id"] for lyr in kwargs.get("view_layers") or []]
        view = FakeService(
            self.service._next_id, kwargs["name"], layer_names=names,
            registry=self.service._registry, layer_ids=ids,
            typeKeywords=["View Service"],
        )
        return view


class FakeContent:
    def __init__(self, registry, taken=()):
        self._registry = registry
        self._taken = set(taken)
        self.queries = []

    def get(self, item_id):
        return self._registry.get(item_id)

    def is_service_name_available(self, service_name, service_type):
        self.queries.append(service_name)
        return service_name not in self._taken


class FakeGIS:
    def __init__(self, registry, taken=()):
        self.content = FakeContent(registry, taken)
        self.users = type("U", (), {"me": type("M", (), {
            "privileges": list(REQUIRED_PRIVILEGES),
            "username": "tester", "role": "org_admin"})()})()
        self.url = "https://example.maps.arcgis.com"


class TestProvisionStage1:
    """Stage 1 creates one service. Everything about it should be checkable."""

    @_pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")

    @_pytest.fixture(autouse=True)
    def fakes_are_their_own_service(self, monkeypatch):
        """A provision run continues into stage 2, whose one arcgis call is this."""
        from agol_provision import views

        monkeypatch.setattr(views, "service_of", lambda item: item)

    @_pytest.fixture
    def registry(self):
        return {}

    @_pytest.fixture
    def template(self, registry):
        FakeService(TEMPLATE_VIEW, "Design View", registry=registry,
                    typeKeywords=["View Service"])
        return FakeService(
            TEMPLATE_MASTER, "Zayo Chicago", layer_names=["redline", "bores"],
            registry=registry,
            indexes=[idx("build_status_Index", "build_status"),
                     idx("PK__ZAYO__99", "OBJECTID")],
        )

    @_pytest.fixture
    def manifest_file(self, tmp_path):
        import yaml

        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump({
            "name": "test", "version": 1, "source_org": "https://example.maps.arcgis.com",
            "master": {"key": "master", "template_item_id": TEMPLATE_MASTER,
                       "title": "{base}", "service_name": "{base_sn}"},
            "views": [{"key": "design", "template_item_id": TEMPLATE_VIEW,
                       "title": "{base} - Design View", "service_name": "{base_sn}_Design"}],
        }))
        return path

    @_pytest.fixture
    def state_dir(self, tmp_path, monkeypatch):
        from agol_provision import cli

        d = tmp_path / "state"
        monkeypatch.setattr(cli, "STATE_DIR", d)
        return d

    def invoke(self, monkeypatch, manifest_file, state_dir, gis, extra=()):
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        monkeypatch.setattr(auth, "connect", lambda profile: gis)
        return CliRunner().invoke(main, [
            "provision", "--company", "CompanyA", "--location", "Moline",
            "--manifest", str(manifest_file), *extra,
        ])

    def state(self, state_dir):
        return json.loads((state_dir / "companya-moline.json").read_text())

    def test_creates_the_master_under_the_preflight_approved_name(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert result.exit_code == 0, result.output
        assert template.copy_calls[0]["service_name"] == "CompanyA_Moline"

    def test_copies_every_layer_and_table_by_positional_index(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        """`copy_feature_layer_collection` copies a subset and raises on None."""
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert template.copy_calls[0]["layers"] == [0, 1]
        assert template.copy_calls[0]["tables"] == []

    def test_sets_the_item_title_to_the_display_name(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        """The copy names the item after the service, so this is not cosmetic."""
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        copy = registry["c" * 32]
        assert copy.updates[0]["title"] == "CompanyA Moline"

    def test_reapplies_the_user_index_and_not_the_system_one(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        applied = [
            i["name"]
            for layer in registry["c" * 32].layers
            for call in layer.manager.calls
            for i in call["indexes"]
        ]
        assert applied == ["build_status_Index", "build_status_Index"]

    def test_records_the_master_and_completes_the_stage(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        recorded = self.state(state_dir)
        assert recorded["stages_completed"] == ["preflight", "master", "views"]
        assert recorded["items"]["master"]["item_id"] == "c" * 32
        assert recorded["items"]["master"]["service_name"] == "CompanyA_Moline"

    def test_a_dry_run_creates_nothing(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry), ["--dry-run"])
        assert template.copy_calls == []

    def test_a_failed_preflight_creates_nothing(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        gis = FakeGIS(registry, taken=["CompanyA_Moline"])
        result = self.invoke(monkeypatch, manifest_file, state_dir, gis)
        assert result.exit_code == 1
        assert template.copy_calls == []

    def test_a_resume_does_not_create_the_master_twice(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        """The one failure mode that leaks an orphaned service."""
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert len(template.copy_calls) == 1

    def test_a_resume_reattempts_the_indexes_on_the_existing_master(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        """The repair path.

        A run whose indexes failed must be fixable by re-running, without
        creating a second service -- the first one's name is already burned.
        """
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert "rechecking indexes" in result.output
        assert "2 already present" in result.output

    def test_a_resume_whose_master_has_vanished_fails_clearly(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        """Deleted by hand in AGOL, still recorded here -- say so, do not crash."""
        self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        del registry["c" * 32]
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert result.exit_code == 1
        assert "c" * 32 in result.output

    def test_repeated_failures_report_the_error_once(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        """27 copies of the same four-line AGOL error buried the summary."""
        from agol_provision import master as master_mod

        monkeypatch.setattr(
            master_mod, "reapply_user_indexes",
            lambda t, n: [
                master_mod.IndexOutcome(f"layer_{i}", "build_status_Index",
                                        master_mod.FAILED, "Invalid definition (400)")
                for i in range(9)
            ],
        )
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert result.output.count("Invalid definition (400)") == 1
        assert "layer_8" in result.output

    def test_restores_the_globalid_index_agol_does_not_recreate(
        self, monkeypatch, manifest_file, state_dir, registry
    ):
        """The one index AGOL creates nothing equivalent to, on any layer."""
        FakeService(TEMPLATE_VIEW, "Design View", registry=registry,
                    typeKeywords=["View Service"])
        FakeService(TEMPLATE_MASTER, "Zayo Chicago", layer_names=["redline"],
                    registry=registry,
                    indexes=[idx("build_status_Index", "build_status"),
                             dict(idx("FDO_GlobalID", "GlobalID"), isUnique=True)])
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert result.exit_code == 0, result.output
        applied = [
            i["name"] for layer in registry["c" * 32].layers
            for call in layer.manager.calls for i in call["indexes"]
        ]
        assert "FDO_GlobalID" in applied
        assert "Not carried over" not in result.output

    def test_reports_contingent_values_the_copy_lost(
        self, monkeypatch, manifest_file, state_dir, registry
    ):
        """Not repaired -- arcgis ships no writer -- but named rather than silent."""
        FakeService(TEMPLATE_VIEW, "Design View", registry=registry,
                    typeKeywords=["View Service"])
        FakeService(TEMPLATE_MASTER, "Zayo Chicago", layer_names=["redline"],
                    registry=registry, contingent={"contingentValues": [{"id": 1}]})
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert result.exit_code == 0, result.output
        assert "contingent values" in result.output
        assert "redline" in result.output

    def test_a_clean_copy_reports_no_gaps(
        self, monkeypatch, manifest_file, state_dir, registry, template
    ):
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert "Not carried over" not in result.output

    def test_a_copy_returning_none_fails_loudly(
        self, monkeypatch, manifest_file, state_dir, registry
    ):
        FakeService(TEMPLATE_VIEW, "Design View", registry=registry,
                    typeKeywords=["View Service"])
        FakeService(TEMPLATE_MASTER, "Zayo Chicago", registry=registry, copy_returns=False)
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert result.exit_code == 1
        assert "returned None" in result.output

    def test_index_failures_are_reported_without_losing_the_master(
        self, monkeypatch, manifest_file, state_dir, registry, template, monkeypatch_fail=None
    ):
        """A master that exists with a missing index beats no record of a master."""
        from agol_provision import master as master_mod

        monkeypatch.setattr(
            master_mod, "reapply_user_indexes",
            lambda t, n: [master_mod.IndexOutcome("redline", "build_status_Index",
                                                  master_mod.FAILED, "boom")],
        )
        result = self.invoke(monkeypatch, manifest_file, state_dir, FakeGIS(registry))
        assert self.state(state_dir)["items"]["master"]["item_id"] == "c" * 32
        assert "build_status_Index" in result.output


class TestDestroy:
    """Rollback deletes what this tool recorded, in reverse, and nothing else."""

    @_pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")

    @_pytest.fixture
    def state_dir(self, tmp_path, monkeypatch):
        from agol_provision import cli

        d = tmp_path / "state"
        monkeypatch.setattr(cli, "STATE_DIR", d)
        return d

    @_pytest.fixture
    def built(self, state_dir):
        """A recorded project: a master, then a view created after it."""
        from agol_provision.state import CreatedItem, ProjectState

        registry = {}
        master = FakeService("a" * 32, "CompanyA Moline", registry=registry)
        view = FakeService("b" * 32, "CompanyA Moline - Design View", registry=registry)
        state = ProjectState.load_or_create(
            state_dir=state_dir, slug="companya-moline", company="CompanyA",
            location="Moline", manifest_name="test", manifest_version=1,
        )
        for key, item in (("master", master), ("design", view)):
            state.record(CreatedItem(key=key, item_id=item.itemid,
                                     item_type="Feature Service", title=item.title))
        return registry, master, view

    def destroy(self, monkeypatch, registry, extra=()):
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        monkeypatch.setattr(auth, "connect", lambda profile: FakeGIS(registry))
        return CliRunner().invoke(
            main, ["provision", "--destroy", "companya-moline", *extra], input="y\n"
        )

    def test_deletes_every_recorded_item(self, monkeypatch, built):
        registry, master, view = built
        result = self.destroy(monkeypatch, registry, ["--yes"])
        assert result.exit_code == 0, result.output
        assert master.deleted and view.deleted

    def test_deletes_views_before_the_master_they_depend_on(self, monkeypatch, built):
        """AGOL refuses to delete a feature service while its views still exist."""
        registry, master, view = built
        order = []
        for item in (master, view):
            item.delete = (lambda i=item: (order.append(i.itemid), True)[1])
        self.destroy(monkeypatch, registry, ["--yes"])
        assert order == ["b" * 32, "a" * 32]

    def test_leaves_nothing_recorded_afterwards(self, monkeypatch, built, state_dir):
        registry, _, _ = built
        self.destroy(monkeypatch, registry, ["--yes"])
        recorded = json.loads((state_dir / "companya-moline.json").read_text())
        assert recorded["items"] == {} and recorded["stages_completed"] == []

    def test_never_touches_an_item_it_did_not_record(self, monkeypatch, built):
        registry, _, _ = built
        bystander = FakeService("f" * 32, "Someone else's service", registry=registry)
        self.destroy(monkeypatch, registry, ["--yes"])
        assert not bystander.deleted

    def test_asks_before_deleting_anything(self, monkeypatch, built):
        registry, master, view = built
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        monkeypatch.setattr(auth, "connect", lambda profile: FakeGIS(registry))
        result = CliRunner().invoke(
            main, ["provision", "--destroy", "companya-moline"], input="n\n"
        )
        assert not master.deleted and not view.deleted
        assert result.exit_code != 0

    def test_an_unknown_slug_fails_rather_than_doing_nothing_quietly(
        self, monkeypatch, state_dir
    ):
        result = self.destroy(monkeypatch, {}, ["--yes"])
        assert result.exit_code == 1
        assert "companya-moline" in result.output

    def test_a_failed_delete_keeps_the_item_recorded(self, monkeypatch, built):
        """Forgetting an item that still exists would orphan it silently."""
        registry, master, view = built

        def boom():
            raise RuntimeError("still has dependencies")

        master.delete = boom
        result = self.destroy(monkeypatch, registry, ["--yes"])
        assert result.exit_code == 1
        assert "still has dependencies" in result.output


class TestInspectIndexes:
    """A read-only view of what the classifier keeps and what the copy is missing.

    This is the spec's live-verification step -- "build_status_Index present on
    all 9 redline layers" -- made runnable instead of done by eye in the AGOL UI.
    """

    @_pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")

    @_pytest.fixture
    def state_dir(self, tmp_path, monkeypatch):
        from agol_provision import cli

        d = tmp_path / "state"
        monkeypatch.setattr(cli, "STATE_DIR", d)
        return d

    @_pytest.fixture
    def manifest_file(self, tmp_path):
        import yaml

        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump({
            "name": "test", "version": 1, "source_org": "https://example.maps.arcgis.com",
            "master": {"key": "master", "template_item_id": TEMPLATE_MASTER,
                       "title": "{base}", "service_name": "{base_sn}"},
            "views": [],
        }))
        return path

    @_pytest.fixture
    def org(self, state_dir):
        """A template carrying two user indexes, and a copy missing one of them."""
        from agol_provision.state import CreatedItem, ProjectState

        registry = {}
        FakeService(
            TEMPLATE_MASTER, "Zayo Chicago", layer_names=["Pole_Redline", "Span_Redline"],
            registry=registry,
            indexes=[idx("build_status_Index", "build_status"),
                     idx("PK__ZAYO__99", "OBJECTID")],
        )
        copy = FakeService("c" * 32, "TestCompany Silvis",
                           layer_names=["Pole_Redline", "Span_Redline"], registry=registry)
        # Span_Redline got its index; Pole_Redline did not -- the live symptom.
        copy.layers[1].properties["indexes"] = [idx("build_status_Index", "build_status")]

        state = ProjectState.load_or_create(
            state_dir=state_dir, slug="testcompany-silvis", company="TestCompany",
            location="Silvis", manifest_name="test", manifest_version=1,
        )
        state.record(CreatedItem(key="master", item_id="c" * 32,
                                 item_type="Feature Service", title="TestCompany Silvis",
                                 service_name="TestCompany_Silvis"))
        return registry, copy

    def invoke(self, monkeypatch, manifest_file, registry, extra=()):
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        monkeypatch.setattr(auth, "connect", lambda profile: FakeGIS(registry))
        return CliRunner().invoke(main, [
            "inspect-indexes", "--slug", "testcompany-silvis",
            "--manifest", str(manifest_file), *extra,
        ])

    def test_command_exists(self):
        from agol_provision.cli import main

        assert "inspect-indexes" in main.commands

    def test_lists_what_the_classifier_keeps_and_drops_the_rest(
        self, monkeypatch, manifest_file, org
    ):
        registry, _ = org
        result = self.invoke(monkeypatch, manifest_file, registry)
        assert result.exit_code == 0, result.output
        assert "build_status_Index" in result.output
        assert "PK__ZAYO__99" not in result.output

    def test_names_the_layers_whose_index_is_missing_from_the_copy(
        self, monkeypatch, manifest_file, org
    ):
        registry, _ = org
        result = self.invoke(monkeypatch, manifest_file, registry)
        assert "Pole_Redline" in result.output
        assert "MISSING" in result.output

    def test_says_so_when_nothing_is_missing(self, monkeypatch, manifest_file, org):
        registry, copy = org
        copy.layers[0].properties["indexes"] = [idx("build_status_Index", "build_status")]
        result = self.invoke(monkeypatch, manifest_file, registry)
        assert "MISSING" not in result.output

    def test_writes_nothing(self, monkeypatch, manifest_file, org):
        """Read-only: it must be safe to run against a live project any time."""
        registry, copy = org
        self.invoke(monkeypatch, manifest_file, registry)
        assert all(layer.manager.calls == [] for layer in copy.layers)
        assert not copy.updates and not copy.deleted

    def test_focusing_one_layer_shows_the_fields_the_decision_used(
        self, monkeypatch, manifest_file, org
    ):
        """The classification is by fields, so the fields are what to inspect."""
        registry, _ = org
        result = self.invoke(monkeypatch, manifest_file, registry,
                             ["--layer", "Pole_Redline"])
        assert "build_status" in result.output
        assert "objectid" in result.output.lower()

    def test_an_unknown_slug_fails_clearly(self, monkeypatch, manifest_file, org):
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        registry, _ = org
        monkeypatch.setattr(auth, "connect", lambda profile: FakeGIS(registry))
        result = CliRunner().invoke(main, [
            "inspect-indexes", "--slug", "nope", "--manifest", str(manifest_file),
        ])
        assert result.exit_code == 1
        assert "nope" in result.output


# ---------------------------------------------------------------- silent losses


class TestMissingIndexCoverage:
    """Compare index coverage by FIELDS, since AGOL renames what it recreates.

    The first live run of stage 1 showed the copy carrying CreationDateIndex,
    CreatorIndex, EditDateIndex, EditorIndex and a PK -- all created by AGOL
    itself, under its own names -- but **no GlobalID index at all**, where the
    template has `FDO_GlobalID`. Comparing names would report all five as lost;
    comparing fields reports only the one that really is.
    """

    def test_reports_a_field_the_copy_indexes_nowhere(self):
        from agol_provision.master import missing_index_coverage

        template = layer_props(indexes=[idx("FDO_GlobalID", "GlobalID")])
        copy = layer_props(indexes=[idx("CreatorIndex", "Creator")])
        assert [i["name"] for i in missing_index_coverage(template, copy)] == [
            "FDO_GlobalID"
        ]

    def test_ignores_an_index_agol_recreated_under_another_name(self):
        from agol_provision.master import missing_index_coverage

        template = layer_props(indexes=[idx("I13Creator", "Creator")])
        copy = layer_props(indexes=[idx("CreatorIndex", "Creator")])
        assert missing_index_coverage(template, copy) == []

    def test_ignores_a_primary_key_with_a_different_random_suffix(self):
        from agol_provision.master import missing_index_coverage

        template = layer_props(indexes=[idx("PK__ZAYO_CHI__AAAA", "OBJECTID")])
        copy = layer_props(indexes=[idx("PK__TestComp__BBBB", "OBJECTID")])
        assert missing_index_coverage(template, copy) == []

    def test_a_composite_index_is_compared_as_a_whole(self):
        from agol_provision.master import missing_index_coverage

        template = layer_props(indexes=[idx("compound", "build_status,route_id")])
        copy = layer_props(indexes=[idx("build_status_Index", "build_status")])
        assert [i["name"] for i in missing_index_coverage(template, copy)] == ["compound"]

    def test_field_order_does_not_matter(self):
        from agol_provision.master import missing_index_coverage

        template = layer_props(indexes=[idx("a", "x,y")])
        copy = layer_props(indexes=[idx("b", "y,x")])
        assert missing_index_coverage(template, copy) == []


class TestReapplyMissingCoverage:
    """A second pass for what the copy indexes nowhere -- in practice, GlobalID.

    AGOL recreates the editor-tracking indexes and the primary key under its own
    names, but creates nothing covering GlobalID: the live copy has no such index
    on any of its 18 layers, where the template has `FDO_GlobalID` on some and
    `GlobalID_Index` on others. Several template views carry `Sync`, and offline
    sync keys on GlobalID, so this one is worth attempting rather than only
    reporting. Attempting is cheap now that a rejected batch retries per index.
    """

    def test_applies_an_index_the_copy_covers_nowhere(self):
        from agol_provision.master import reapply_missing_coverage

        tpl, new, copy = template_and_copy([idx("FDO_GlobalID", "GlobalID")])
        outcomes = reapply_missing_coverage(tpl, new)
        assert [(o.index, o.status) for o in outcomes] == [("FDO_GlobalID", "applied")]

    def test_leaves_alone_what_agol_recreated_under_another_name(self):
        from agol_provision.master import reapply_missing_coverage

        tpl, new, copy = template_and_copy(
            [idx("I13Creator", "Creator")],
            copy_indexes=[idx("CreatorIndex", "Creator")],
        )
        assert reapply_missing_coverage(tpl, new) == []
        assert copy.manager.calls == []

    def test_does_not_retry_the_spatial_index(self):
        """The copy has its own `_Shape_sidx`, so the fields are covered."""
        from agol_provision.master import reapply_missing_coverage

        tpl, new, copy = template_and_copy(
            [idx("user_57996.X_Shape_sidx", "Shape")],
            copy_indexes=[idx("TestCompany_Shape_sidx", "Shape")],
        )
        assert reapply_missing_coverage(tpl, new) == []

    def test_a_rejection_is_reported_not_raised(self):
        """AGOL may decline to recreate a GlobalID index, and that is an answer."""
        from agol_provision.master import reapply_missing_coverage

        tpl, new, copy = template_and_copy(
            [idx("FDO_GlobalID", "GlobalID")], rejects={"FDO_GlobalID"}
        )
        assert [o.status for o in reapply_missing_coverage(tpl, new)] == ["failed"]

    def test_keeps_the_templates_uniqueness(self):
        """`FDO_GlobalID` is unique on the template; a non-unique copy is not it."""
        from agol_provision.master import reapply_missing_coverage

        unique = dict(idx("FDO_GlobalID", "GlobalID"), isUnique=True)
        tpl, new, copy = template_and_copy([unique])
        reapply_missing_coverage(tpl, new)
        assert copy.manager.calls[0]["indexes"][0]["isUnique"] is True


class TestSchemaGaps:
    """What the copy loses and nothing puts back. Reported, never guessed at."""

    def service(self, name, indexes=(), contingent=None):
        layer = FakeLayer(layer_props(name, indexes, extra_fields=["build_status"]))
        layer.contingent_values = contingent
        return FakeFLC([layer])

    def test_reports_a_layer_whose_contingent_values_did_not_survive(self):
        from agol_provision.master import schema_gaps

        template = self.service("redline", contingent={"contingentValues": [{"id": 1}]})
        copy = self.service("redline", contingent={})
        gaps = schema_gaps(template, copy)
        assert [(g.layer, g.kind) for g in gaps] == [("redline", "contingent values")]

    def test_says_nothing_when_the_template_defines_none(self):
        from agol_provision.master import schema_gaps

        assert schema_gaps(self.service("redline"), self.service("redline")) == []

    def test_says_nothing_when_the_copy_kept_them(self):
        from agol_provision.master import schema_gaps

        cv = {"contingentValues": [{"id": 1}]}
        assert schema_gaps(self.service("a", contingent=cv),
                           self.service("a", contingent=cv)) == []

    def test_reports_an_index_field_the_copy_covers_nowhere(self):
        from agol_provision.master import schema_gaps

        template = self.service("redline", indexes=[idx("FDO_GlobalID", "GlobalID")])
        copy = self.service("redline", indexes=[idx("CreatorIndex", "Creator")])
        gaps = schema_gaps(template, copy)
        assert [(g.layer, g.kind) for g in gaps] == [("redline", "index")]
        assert "GlobalID" in gaps[0].detail

    def test_a_layer_that_cannot_be_read_is_not_reported_as_a_loss(self):
        """`FeatureLayer.contingent_values` swallows errors and returns {}."""
        from agol_provision.master import schema_gaps

        template = self.service("redline", contingent=None)
        assert schema_gaps(template, self.service("redline", contingent=None)) == []

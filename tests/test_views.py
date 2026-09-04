"""Stage 2: views, created natively from the new master and never cloned.

Two constraints shape this module.

Cloning a hosted feature layer view within one org is unreliable -- it can
produce an empty service or silently re-point at the source -- so views are
created with `create_view()` and the template's configuration is replayed.

And `create_view()`'s own service-level `query` argument cannot be used. It
applies the query to `flc.layers[0]` alone (and rewrites that layer's field
visibility as a side effect), so on a view spanning eighteen layers the other
seventeen would be left unfiltered. These views are shared to subcontractors;
an unfiltered layer is a data-exposure bug, not a cosmetic one. Every definition
query is therefore applied per layer, for every view, uniform or not.
"""

import pytest

from agol_provision.views import (
    LayerView,
    ViewError,
    apply_definition_queries,
    read_template_view,
    add_view_layers,
    build_view_definition,
    source_layers,
    wait_for_layers,
)


class FakeManager:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def update_definition(self, json_dict):
        self.calls.append(json_dict)
        if self._raises:
            raise self._raises
        return {"success": True}


class FakeLayer:
    def __init__(self, id, name, query=None, raises=None):
        self.properties = {"id": id, "name": name}
        if query is not None:
            self.properties["viewDefinitionQuery"] = query
        self.manager = FakeManager(raises)


class FakeService:
    """A feature service: the template view, the new master, or a new view."""

    def __init__(self, layers=(), tables=(), capabilities="Query"):
        self.properties = {"capabilities": capabilities}
        self.layers = list(layers)
        self.tables = list(tables)


def master(*ids_and_names, tables=()):
    return FakeService(
        [FakeLayer(i, n) for i, n in ids_and_names],
        [FakeLayer(i, n) for i, n in tables],
    )


# ---------------------------------------------------------------- reading


class TestReadTemplateView:
    def test_captures_the_service_capabilities(self):
        view = FakeService([FakeLayer(0, "a")], capabilities="Query,Update,Editing")
        assert read_template_view(view).capabilities == "Query,Update,Editing"

    def test_captures_each_layer_id_name_and_query(self):
        view = FakeService([
            FakeLayer(3, "Redline", "build_status = 'Complete'"),
            FakeLayer(11, "Bores", "build_status = 'Pending'"),
        ])
        assert read_template_view(view).layers == [
            LayerView(3, "Redline", "build_status = 'Complete'"),
            LayerView(11, "Bores", "build_status = 'Pending'"),
        ]

    def test_an_unfiltered_layer_reads_as_an_empty_query(self):
        view = FakeService([FakeLayer(0, "a")])
        assert read_template_view(view).layers[0].query == ""

    def test_keeps_tables_separate_from_layers(self):
        """create_view() takes them as separate arguments."""
        view = FakeService([FakeLayer(0, "a")], [FakeLayer(19, "Notes", "x = 1")])
        plan = read_template_view(view)
        assert [lv.name for lv in plan.layers] == ["a"]
        assert plan.tables == [LayerView(19, "Notes", "x = 1")]

    def test_reports_a_uniform_query(self):
        q = "build_status = 'Complete'"
        view = FakeService([FakeLayer(0, "a", q), FakeLayer(1, "b", q)])
        assert read_template_view(view).uniform_query

    def test_reports_a_non_uniform_query(self):
        """Two of the seven template views filter differently per layer."""
        view = FakeService([FakeLayer(0, "a", "x = 1"), FakeLayer(1, "b", "x = 2")])
        assert not read_template_view(view).uniform_query


# ---------------------------------------------------------------- layer subset


class TestSourceLayers:
    def test_returns_the_master_layers_the_view_exposes(self):
        """The seven views span 2 to 18 of the master's layers."""
        flc = master((0, "a"), (1, "b"), (2, "c"))
        plan = read_template_view(FakeService([FakeLayer(11, "c"), FakeLayer(19, "a")]))
        layers, tables = source_layers(flc, plan)
        assert [lyr.properties["name"] for lyr in layers] == ["c", "a"]
        assert tables == []

    def test_matches_by_name_because_ids_do_not_survive_the_copy(self):
        """The first live run of stage 2 failed on exactly this.

        `copy_feature_layer_collection()` renumbers the master's layers, so a
        template view's layer id names a different layer on the copy -- id 11 was
        `Redline_Details` on the template and `Pole_Design` on the new master.
        Names survive the copy; stage 1's index pairing checks that on all 18.
        """
        flc = master((0, "Redline_Details"), (11, "Pole_Design"))
        plan = read_template_view(FakeService([FakeLayer(11, "Redline_Details")]))
        layers, _ = source_layers(flc, plan)
        assert layers[0].properties["id"] == 0

    def test_returns_tables_separately(self):
        flc = master((0, "a"), tables=[(1, "Notes")])
        plan = read_template_view(FakeService([FakeLayer(0, "a")], [FakeLayer(19, "Notes")]))
        layers, tables = source_layers(flc, plan)
        assert [t.properties["name"] for t in tables] == ["Notes"]
        assert [lyr.properties["name"] for lyr in layers] == ["a"]

    def test_refuses_when_the_master_lacks_a_layer_the_view_needs(self):
        """A view silently missing a layer is worse than a stopped run."""
        flc = master((0, "a"))
        plan = read_template_view(FakeService([FakeLayer(7, "missing")]))
        with pytest.raises(ViewError, match="missing"):
            source_layers(flc, plan)

    def test_names_the_layers_the_master_does_have(self):
        """A rename in a template view is the likely cause; say what is there."""
        flc = master((0, "Redline_Details"), (1, "Pole_Design"))
        plan = read_template_view(FakeService([FakeLayer(0, "Renamed_In_View")]))
        with pytest.raises(ViewError, match="Pole_Design"):
            source_layers(flc, plan)

    def test_refuses_when_the_master_has_two_layers_of_the_same_name(self):
        """Name is the key, so an ambiguous one must stop rather than pick."""
        flc = master((0, "Redline"), (1, "Redline"))
        plan = read_template_view(FakeService([FakeLayer(0, "Redline")]))
        with pytest.raises(ViewError, match="twice"):
            source_layers(flc, plan)


# ---------------------------------------------------------------- the async gap


class TestWaitForLayers:
    """`create_view()` returns before the view's layers exist.

    On ArcGIS Online it calls `add_to_definition(..., future=True)` and discards
    the Future, so the item comes back describing a service whose layers are
    still being committed. The first live run applied definition queries to
    nothing at all on four views, and to 5 of 7 layers on another -- a race it
    partly won. Every layer reported "missing" was really there moments later.
    """

    def fetcher(self, *snapshots):
        """Successive views of the service, as separate objects.

        A retained FeatureLayerCollection cannot show the change: `.layers` is a
        plain attribute assigned once in `_populate_layers`, so each poll has to
        build a new one.
        """
        seen = iter(snapshots)
        return lambda: next(seen)

    def test_returns_at_once_when_the_layers_are_already_there(self):
        ready = FakeService([FakeLayer(0, "a"), FakeLayer(1, "b")])
        slept = []
        got = wait_for_layers(self.fetcher(ready), {"a", "b"}, sleep=slept.append)
        assert got is ready
        assert slept == []

    def test_polls_until_the_layers_appear(self):
        """The live case: the first look showed an empty service."""
        empty = FakeService([])
        partial = FakeService([FakeLayer(0, "a")])
        ready = FakeService([FakeLayer(0, "a"), FakeLayer(1, "b")])
        slept = []
        got = wait_for_layers(
            self.fetcher(empty, partial, ready), {"a", "b"}, sleep=slept.append
        )
        assert got is ready
        assert len(slept) == 2

    def test_waits_for_tables_too(self):
        ready = FakeService([FakeLayer(0, "a")], [FakeLayer(1, "Notes")])
        got = wait_for_layers(self.fetcher(ready), {"a", "Notes"}, sleep=lambda _: None)
        assert got is ready

    def test_gives_up_and_says_what_it_saw(self):
        """A timeout must not read as a network hang."""
        empty = FakeService([FakeLayer(0, "a")])
        with pytest.raises(ViewError, match="'b'"):
            wait_for_layers(
                lambda: empty, {"a", "b"}, sleep=lambda _: None, backoff=(0.0, 1.0)
            )

    def test_never_sleeps_before_looking(self):
        """A fast org should pay nothing for the poll."""
        slept = []
        wait_for_layers(
            self.fetcher(FakeService([FakeLayer(0, "a")])), {"a"}, sleep=slept.append
        )
        assert slept == []

    def test_tolerates_a_fetch_that_throws_while_the_service_settles(self):
        """A half-built service can 400 rather than return an empty list."""
        ready = FakeService([FakeLayer(0, "a")])
        calls = iter([RuntimeError("Invalid URL"), ready])

        def fetch():
            got = next(calls)
            if isinstance(got, Exception):
                raise got
            return got

        assert wait_for_layers(fetch, {"a"}, sleep=lambda _: None) is ready


class TestBuildViewDefinition:
    """The layer payload `create_view()` posts asynchronously, built ourselves.

    Rebuilt rather than borrowed because `create_view()` discards the Future that
    tracks its own POST, so a job that fails server-side is invisible: the client
    sees an item, and the service stays empty forever. Posting the same definition
    synchronously turns that into either a working view or a readable error.
    """

    def master_at(self, url, *ids_and_names, tables=()):
        svc = master(*ids_and_names, tables=tables)
        svc.url = url
        return svc

    URL = "https://services.arcgis.com/abc/arcgis/rest/services/TestCompany_Moline/FeatureServer"

    def test_names_the_source_service_from_the_master_url(self):
        """`sourceServiceName` is the master's service name, not the view's."""
        flc = self.master_at(self.URL, (0, "Redline"))
        plan = read_template_view(FakeService([FakeLayer(11, "Redline")]))
        payload = build_view_definition(flc, plan)
        source = payload["layers"][0]["adminLayerInfo"]["viewLayerDefinition"]
        assert source["sourceServiceName"] == "TestCompany_Moline"

    def test_points_each_view_layer_at_the_masters_id_not_the_templates(self):
        flc = self.master_at(self.URL, (0, "Redline"), (1, "Bores"))
        plan = read_template_view(FakeService([FakeLayer(19, "Bores")]))
        entry = build_view_definition(flc, plan)["layers"][0]
        assert entry["id"] == 1
        assert entry["adminLayerInfo"]["viewLayerDefinition"]["sourceLayerId"] == 1
        assert entry["name"] == "Bores"

    def test_takes_every_field_of_the_source_layer(self):
        """No visible_fields path is built; a view exposes the master's fields."""
        flc = self.master_at(self.URL, (0, "Redline"))
        plan = read_template_view(FakeService([FakeLayer(0, "Redline")]))
        source = build_view_definition(flc, plan)["layers"][0]["adminLayerInfo"]
        assert source["viewLayerDefinition"]["sourceLayerFields"] == "*"

    def test_marks_tables_as_tables(self):
        """Both 18-layer views include the master's one table."""
        flc = self.master_at(self.URL, (0, "Redline"), tables=[(17, "RateCodeApplied")])
        plan = read_template_view(
            FakeService([FakeLayer(0, "Redline")], [FakeLayer(20, "RateCodeApplied")])
        )
        payload = build_view_definition(flc, plan)
        assert [t["name"] for t in payload["tables"]] == ["RateCodeApplied"]
        assert payload["tables"][0]["type"] == "Table"
        assert [lyr["name"] for lyr in payload["layers"]] == ["Redline"]

    def test_sends_no_null_popupinfo(self):
        """create_view sends `popupInfo: null`; omitting it is one less thing to reject."""
        flc = self.master_at(self.URL, (0, "Redline"))
        plan = read_template_view(FakeService([FakeLayer(0, "Redline")]))
        assert "popupInfo" not in build_view_definition(flc, plan)["layers"][0]["adminLayerInfo"]


class TestAddViewLayers:
    def test_posts_the_definition_synchronously(self):
        """Synchronous is the whole point: the error comes back to us."""
        view = FakeService([])
        view.manager = FakeServiceManager()
        add_view_layers(view, {"layers": [{"id": 0}], "tables": []})
        assert view.manager.added == [{"layers": [{"id": 0}], "tables": []}]
        assert view.manager.futures == [False]

    def test_surfaces_the_agol_error(self):
        view = FakeService([])
        view.manager = FakeServiceManager(raises=RuntimeError("Invalid definition"))
        with pytest.raises(ViewError, match="Invalid definition"):
            add_view_layers(view, {"layers": [], "tables": []})

    def test_reports_a_failure_response_that_does_not_raise(self):
        """AGOL can answer 200 with success=False."""
        view = FakeService([])
        view.manager = FakeServiceManager(result={"success": False, "error": "nope"})
        with pytest.raises(ViewError, match="nope"):
            add_view_layers(view, {"layers": [], "tables": []})


class FakeServiceManager:
    """The collection manager, for the synchronous add path."""

    def __init__(self, raises=None, result=None):
        self.added = []
        self.futures = []
        self._raises = raises
        self._result = result if result is not None else {"success": True}

    def add_to_definition(self, json_dict, future=False):
        self.added.append(json_dict)
        self.futures.append(future)
        if self._raises:
            raise self._raises
        return self._result


# ---------------------------------------------------------------- queries


class TestApplyDefinitionQueries:
    def test_sets_the_query_on_every_layer_even_when_uniform(self):
        """The regression against create_view()'s own `query` argument.

        That argument sets the query on the first layer only. Relying on it for a
        uniform view would leave every other layer unfiltered.
        """
        q = "build_status = 'Complete'"
        plan = read_template_view(FakeService([FakeLayer(0, "a", q), FakeLayer(1, "b", q)]))
        new_view = FakeService([FakeLayer(0, "a"), FakeLayer(1, "b")])
        apply_definition_queries(new_view, plan)
        assert [lyr.manager.calls for lyr in new_view.layers] == [
            [{"viewDefinitionQuery": q}], [{"viewDefinitionQuery": q}]
        ]

    def test_applies_each_layers_own_query_when_they_differ(self):
        plan = read_template_view(
            FakeService([FakeLayer(0, "a", "x = 1"), FakeLayer(1, "b", "x = 2")])
        )
        new_view = FakeService([FakeLayer(0, "a"), FakeLayer(1, "b")])
        apply_definition_queries(new_view, plan)
        assert new_view.layers[0].manager.calls == [{"viewDefinitionQuery": "x = 1"}]
        assert new_view.layers[1].manager.calls == [{"viewDefinitionQuery": "x = 2"}]

    def test_leaves_an_unfiltered_layer_alone(self):
        plan = read_template_view(FakeService([FakeLayer(0, "a"), FakeLayer(1, "b", "x = 1")]))
        new_view = FakeService([FakeLayer(0, "a"), FakeLayer(1, "b")])
        apply_definition_queries(new_view, plan)
        assert new_view.layers[0].manager.calls == []

    def test_matches_the_new_view_layers_by_name(self):
        """The new view's ids come from the new master's, not the template's."""
        plan = read_template_view(FakeService([FakeLayer(11, "a", "x = 1")]))
        new_view = FakeService([FakeLayer(0, "other"), FakeLayer(1, "a")])
        apply_definition_queries(new_view, plan)
        assert new_view.layers[0].manager.calls == []
        assert new_view.layers[1].manager.calls == [{"viewDefinitionQuery": "x = 1"}]

    def test_covers_tables(self):
        plan = read_template_view(FakeService([], [FakeLayer(19, "Notes", "x = 1")]))
        new_view = FakeService([], [FakeLayer(19, "Notes")])
        apply_definition_queries(new_view, plan)
        assert new_view.tables[0].manager.calls == [{"viewDefinitionQuery": "x = 1"}]

    def test_reports_a_layer_the_new_view_does_not_have(self):
        """Every filtered layer must end up filtered, or the run has to say so."""
        plan = read_template_view(FakeService([FakeLayer(11, "a", "x = 1")]))
        outcomes = apply_definition_queries(FakeService([FakeLayer(0, "zzz")]), plan)
        assert [(o.layer, o.status) for o in outcomes] == [("a", "missing")]

    def test_reports_a_failure_without_raising(self):
        plan = read_template_view(FakeService([FakeLayer(0, "a", "x = 1")]))
        new_view = FakeService([FakeLayer(0, "a", raises=RuntimeError("nope"))])
        outcomes = apply_definition_queries(new_view, plan)
        assert [(o.layer, o.status) for o in outcomes] == [("a", "failed")]
        assert "nope" in outcomes[0].detail

    def test_reports_each_applied_layer(self):
        plan = read_template_view(FakeService([FakeLayer(0, "a", "x = 1")]))
        outcomes = apply_definition_queries(FakeService([FakeLayer(0, "a")]), plan)
        assert [(o.layer, o.status) for o in outcomes] == [("a", "applied")]


# ---------------------------------------------------------------- the command

import json

# The fake AGOL lives in test_master; stage 2 needs the same one, plus a master
# that stage 1 has already created. tests/ is on sys.path. Aliased so it does not
# shadow this module's own smaller fakes above.
from test_master import FakeGIS as AgolGIS  # noqa: E402
from test_master import FakeService as AgolService  # noqa: E402

TEMPLATE_MASTER = "1" * 32
UNIFORM_VIEW = "2" * 32
PER_LAYER_VIEW = "3" * 32
UNIFORM_Q = "build_status = 'Complete'"


class TestProvisionStage2:
    """Views end to end: created from the new master, never cloned."""

    @pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")

    @pytest.fixture(autouse=True)
    def fakes_are_their_own_service(self, monkeypatch):
        """`service_of` is the only arcgis call in stage 2; the fakes double as both."""
        from agol_provision import views

        monkeypatch.setattr(views, "service_of", lambda item: item)

    @pytest.fixture
    def registry(self):
        return {}

    @pytest.fixture
    def templates(self, registry):
        master = AgolService(
            TEMPLATE_MASTER, "Zayo Chicago", registry=registry,
            layer_names=["Redline", "Bores", "Splice"], layer_ids=[11, 12, 19],
            snippet="Master snippet", description="Master description",
            tags=["Zayo", "Chicago"],
        )
        # Two of the seven real views filter differently per layer.
        AgolService(
            UNIFORM_VIEW, "Read Only", registry=registry, typeKeywords=["View Service"],
            layer_names=["Redline", "Bores"], layer_ids=[11, 12],
            capabilities="Query", queries={"Redline": UNIFORM_Q, "Bores": UNIFORM_Q},
            snippet="Read-only access", description="What the viewer group sees",
            tags=["viewer", "read-only"],
        )
        AgolService(
            PER_LAYER_VIEW, "Redline QC", registry=registry, typeKeywords=["View Service"],
            layer_names=["Redline", "Splice"], layer_ids=[11, 19],
            capabilities="Query,Update,Editing",
            queries={"Redline": "x = 1", "Splice": "x = 2"},
        )
        return master

    @pytest.fixture
    def manifest_file(self, tmp_path):
        import yaml

        path = tmp_path / "m.yaml"
        path.write_text(yaml.safe_dump({
            "name": "test", "version": 1, "source_org": "https://example.maps.arcgis.com",
            "master": {"key": "master", "template_item_id": TEMPLATE_MASTER,
                       "title": "{base}", "service_name": "{base_sn}"},
            "views": [
                {"key": "read_only", "template_item_id": UNIFORM_VIEW,
                 "title": "{base} (Read-Only)", "service_name": "{base_sn}_Read_Only"},
                {"key": "qc", "template_item_id": PER_LAYER_VIEW,
                 "title": "{base} - QC View", "service_name": "{base_sn}_QC"},
            ],
        }))
        return path

    @pytest.fixture(autouse=True)
    def no_real_sleeping(self, monkeypatch):
        """The wait itself is covered by TestWaitForLayers; do not pay for it here."""
        from agol_provision import cli

        monkeypatch.setattr(cli, "SLEEP", lambda _: None)

    @pytest.fixture
    def state_dir(self, tmp_path, monkeypatch):
        from agol_provision import cli

        d = tmp_path / "state"
        monkeypatch.setattr(cli, "STATE_DIR", d)
        return d

    def invoke(self, monkeypatch, manifest_file, state_dir, registry, extra=()):
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        monkeypatch.setattr(auth, "connect", lambda profile: AgolGIS(registry))
        return CliRunner().invoke(main, [
            "provision", "--company", "CompanyA", "--location", "Moline",
            "--manifest", str(manifest_file), *extra,
        ])

    def new_master(self, registry):
        return next(s for s in registry.values() if s.title == "CompanyA Moline")

    def created_views(self, registry):
        return {s.title: s for s in registry.values() if "View Service" in s.typeKeywords
                and s.itemid not in (UNIFORM_VIEW, PER_LAYER_VIEW)}

    def calls(self, registry):
        return {c["name"]: c for c in self.new_master(registry).manager.create_view_calls}

    def test_creates_a_view_for_every_manifest_entry(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 0, result.output
        assert set(self.calls(registry)) == {
            "CompanyA_Moline_Read_Only", "CompanyA_Moline_QC"
        }

    def test_passes_the_templates_capabilities_through(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """Two template views changed capabilities between discovery runs; they
        are read live so edits propagate with no manifest change."""
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        calls = self.calls(registry)
        assert calls["CompanyA_Moline_Read_Only"]["capabilities"] == "Query"
        assert calls["CompanyA_Moline_QC"]["capabilities"] == "Query,Update,Editing"

    def test_passes_only_the_layers_the_template_view_exposes(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """The seven real views span 2 to 18 of the master's layers."""
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        picked = {
            name: [lyr.properties["name"] for lyr in call["view_layers"]]
            for name, call in self.calls(registry).items()
        }
        assert picked == {
            "CompanyA_Moline_Read_Only": ["Redline", "Bores"],
            "CompanyA_Moline_QC": ["Redline", "Splice"],
        }

    def test_preserves_layer_ids(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """Maps reference layers by URL with the layer index in it."""
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert all(c["preserve_layer_ids"] is True for c in self.calls(registry).values())

    def test_never_uses_the_service_level_query_argument(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """create_view()'s `query` reaches layers[0] only.

        Using it on a uniform view would create every other layer unfiltered --
        these views are shared to subcontractors, so that leaks data.
        """
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert all(c.get("query") is None for c in self.calls(registry).values())

    def test_applies_the_query_per_layer_even_for_a_uniform_view(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        view = self.created_views(registry)["CompanyA Moline (Read-Only)"]
        assert [lyr.manager.calls for lyr in view.layers] == [
            [{"viewDefinitionQuery": UNIFORM_Q}], [{"viewDefinitionQuery": UNIFORM_Q}]
        ]

    def test_applies_each_layers_own_query_for_a_per_layer_view(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        view = self.created_views(registry)["CompanyA Moline - QC View"]
        assert [lyr.manager.calls for lyr in view.layers] == [
            [{"viewDefinitionQuery": "x = 1"}], [{"viewDefinitionQuery": "x = 2"}]
        ]

    def test_sets_each_view_item_title(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert set(self.created_views(registry)) == {
            "CompanyA Moline (Read-Only)", "CompanyA Moline - QC View"
        }

    def test_records_every_view_and_completes_the_stage(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        recorded = json.loads((state_dir / "companya-moline.json").read_text())
        assert recorded["stages_completed"] == ["preflight", "master", "views"]
        assert set(recorded["items"]) == {"master", "read_only", "qc"}

    def test_records_views_after_the_master_so_rollback_removes_them_first(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """AGOL refuses to delete a feature service while its views still exist."""
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        recorded = json.loads((state_dir / "companya-moline.json").read_text())
        assert list(recorded["items"]) == ["master", "read_only", "qc"]

    def test_a_resume_does_not_create_a_view_twice(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert len(self.new_master(registry).manager.create_view_calls) == 2

    def test_a_dry_run_creates_no_views(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        self.invoke(monkeypatch, manifest_file, state_dir, registry, ["--dry-run"])
        assert templates.manager.create_view_calls == []

    def test_survives_a_view_whose_layers_are_not_there_yet(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """The live failure: create_view() returns before AGOL adds the layers.

        The first look at each new view showed an empty service, so every
        definition query was reported missing and none was applied.
        """
        from agol_provision import views

        class NotReadyYet:
            """The same service, before its layers have been committed."""

            def __init__(self, item):
                self.properties = item.properties
                self.layers = []
                self.tables = []

        first_look = set()
        templates_ids = {TEMPLATE_MASTER, UNIFORM_VIEW, PER_LAYER_VIEW}

        def service_of(item):
            new_view = item.itemid not in templates_ids and "View Service" in item.typeKeywords
            if new_view and item.itemid not in first_look:
                first_look.add(item.itemid)
                return NotReadyYet(item)
            return item

        monkeypatch.setattr(views, "service_of", service_of)
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 0, result.output
        view = self.created_views(registry)["CompanyA Moline (Read-Only)"]
        assert [lyr.manager.calls for lyr in view.layers] == [
            [{"viewDefinitionQuery": UNIFORM_Q}], [{"viewDefinitionQuery": UNIFORM_Q}]
        ]

    def test_posts_the_layers_itself_when_the_async_job_never_lands(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """The live dead end: an 18-layer view came back empty and stayed empty.

        create_view() discards the Future for its own POST, so a job that failed
        server-side leaves an item with no layers and no error anywhere. Posting
        the same definition synchronously either works or says why.
        """
        from agol_provision import views

        class EmptyUntilPosted:
            """A view service whose layers appear only once posted synchronously."""

            def __init__(self, item):
                self.properties = item.properties
                self.url = item.url
                self._item = item
                self.posted = None
                self.manager = self

            def add_to_definition(self, json_dict, future=False):
                self.posted = json_dict
                return {"success": True}

            @property
            def layers(self):
                return self._item.layers if self.posted else []

            @property
            def tables(self):
                return self._item.tables if self.posted else []

        template_ids = {TEMPLATE_MASTER, UNIFORM_VIEW, PER_LAYER_VIEW}
        wrappers = {}

        def service_of(item):
            new_view = (item.itemid not in template_ids
                        and "View Service" in item.typeKeywords)
            if not new_view:
                return item
            return wrappers.setdefault(item.itemid, EmptyUntilPosted(item))

        monkeypatch.setattr(views, "service_of", service_of)
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 0, result.output

        posted = [w.posted for w in wrappers.values()]
        assert all(p is not None for p in posted)
        assert [lyr["name"] for lyr in posted[0]["layers"]] == ["Redline", "Bores"]
        assert "posting them" in result.output.lower()

    def test_a_synchronous_post_that_agol_rejects_fails_the_run(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """The point of the fallback is the error message, so it must surface."""
        from agol_provision import views

        class NeverReady:
            def __init__(self, item):
                self.properties = item.properties
                self.url = item.url
                self.layers = []
                self.tables = []
                self.manager = self

            def add_to_definition(self, json_dict, future=False):
                raise RuntimeError("Invalid definition for viewLayerDefinition")

        template_ids = {TEMPLATE_MASTER, UNIFORM_VIEW, PER_LAYER_VIEW}

        def service_of(item):
            new_view = (item.itemid not in template_ids
                        and "View Service" in item.typeKeywords)
            return NeverReady(item) if new_view else item

        monkeypatch.setattr(views, "service_of", service_of)
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 1
        assert "Invalid definition for viewLayerDefinition" in result.output

    def test_an_unapplied_definition_query_fails_the_run(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """A view whose filter never applied is unfiltered, not merely imperfect.

        These are shared to subcontractors, so it leaks rows. Better to stop and
        let --destroy clean up than to report Created and move on.
        """
        from agol_provision import views

        real = views.apply_definition_queries

        def failing(new_view, plan):
            outcomes = real(new_view, plan)
            return [
                views.QueryOutcome(o.layer, views.FAILED, "Invalid URL (400)")
                if i == 0 else o
                for i, o in enumerate(outcomes)
            ]

        monkeypatch.setattr(views, "apply_definition_queries", failing)
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 1
        assert "Invalid URL (400)" in result.output

    def test_copies_the_template_views_own_snippet_description_and_tags(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """create_view() falls back to the SOURCE service's item for these.

        The source is the new master, so every view inherited the master's
        description and tags instead of the template view's.
        """
        self.invoke(monkeypatch, manifest_file, state_dir, registry)
        view = self.created_views(registry)["CompanyA Moline (Read-Only)"]
        assert view.snippet == "Read-only access"
        assert view.description == "What the viewer group sees"
        assert view.tags == ["viewer", "read-only"]

    def test_a_master_missing_a_layer_the_view_needs_stops_the_run(
        self, monkeypatch, manifest_file, state_dir, registry
    ):
        """Better a stopped run than a view silently missing a layer."""
        AgolService(TEMPLATE_MASTER, "Zayo Chicago", registry=registry,
                    layer_names=["Redline"], layer_ids=[11])
        AgolService(UNIFORM_VIEW, "Read Only", registry=registry,
                    typeKeywords=["View Service"], layer_names=["Redline", "Gone"],
                    layer_ids=[11, 99])
        AgolService(PER_LAYER_VIEW, "QC", registry=registry,
                    typeKeywords=["View Service"], layer_names=["Redline"],
                    layer_ids=[11])
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 1
        assert "Gone" in result.output

    def test_the_master_renumbering_its_layers_does_not_break_the_views(
        self, monkeypatch, manifest_file, state_dir, registry, templates
    ):
        """The regression: the copy renumbers layers, the template view does not.

        Guards the guard -- if the fake ever stopped renumbering, matching by id
        would pass again and this whole class would stop testing the real case.
        """
        result = self.invoke(monkeypatch, manifest_file, state_dir, registry)
        assert result.exit_code == 0, result.output

        template_ids = sorted(lyr.properties["id"] for lyr in templates.layers)
        master_ids = sorted(
            lyr.properties["id"] for lyr in self.new_master(registry).layers
        )
        assert template_ids == [11, 12, 19] and master_ids == [0, 1, 2]

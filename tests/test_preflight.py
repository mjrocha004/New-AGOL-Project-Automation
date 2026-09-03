"""Stage 0: everything that can fail must fail here.

Preflight is the only stage that runs before anything is created, and a hosted
service name is reserved org-wide *permanently*. So the assertions that matter
most are the negative ones: that a taken name stops the run, and that nothing
anywhere quietly renames it.

No network. Fakes follow the pattern in `test_discovery.py` -- only the
attributes the code actually reads.
"""

import pytest

from agol_provision.auth import REQUIRED_PRIVILEGES
from agol_provision.manifest import Manifest
from agol_provision.naming import NameContext
from agol_provision.preflight import (
    STATUS_AVAILABLE,
    STATUS_EXISTS,
    STATUS_OUT_OF_SCOPE,
    STATUS_TAKEN,
    run_preflight,
)
from agol_provision.state import CreatedItem, ProjectState

ALL_PRIVILEGES = list(REQUIRED_PRIVILEGES)


def _id(n: int) -> str:
    return f"{n:032x}"


MASTER_ID, DESIGN_ID, QC_ID, MAP_ID, APP_ID = (_id(n) for n in range(1, 6))


# ---------------------------------------------------------------- fakes


class FakeItem:
    """Stands in for arcgis.gis.Item -- only what preflight and classify() read."""

    def __init__(self, itemid, title="Template", type="Feature Service", typeKeywords=None):
        self.itemid = itemid
        self.title = title
        self.type = type
        self.typeKeywords = typeKeywords or []


class FakeContent:
    def __init__(self, items, taken):
        self._items = items
        self._taken = {name.lower() for name in taken}
        # Recording every query is what lets a test prove preflight never went
        # looking for a free variant of a taken name.
        self.queries = []

    def get(self, item_id):
        return self._items.get(item_id)

    def is_service_name_available(self, service_name, service_type):
        assert service_type == "featureService", service_type
        self.queries.append(service_name)
        return service_name.lower() not in self._taken


class FakeUser:
    def __init__(self, privileges):
        self.privileges = list(privileges)
        self.username = "tester"
        self.role = "org_admin"


class FakeGIS:
    def __init__(self, items=None, taken=(), privileges=None):
        self.content = FakeContent(items if items is not None else default_items(), taken)
        self.users = type("Users", (), {})()
        self.users.me = FakeUser(ALL_PRIVILEGES if privileges is None else privileges)
        self.url = "https://example.maps.arcgis.com"


def default_items():
    """A template inventory where every id resolves to the right type."""
    return {
        MASTER_ID: FakeItem(MASTER_ID, "Zayo Chicago", typeKeywords=["Hosted Service"]),
        DESIGN_ID: FakeItem(DESIGN_ID, "Design View", typeKeywords=["View Service"]),
        QC_ID: FakeItem(QC_ID, "QC View", typeKeywords=["View Service"]),
        MAP_ID: FakeItem(MAP_ID, "Field Map", type="Web Map"),
        APP_ID: FakeItem(APP_ID, "Viewer", type="Web Experience"),
    }


def make_manifest(**overrides) -> Manifest:
    data = {
        "name": "test",
        "version": 1,
        "source_org": "https://example.maps.arcgis.com",
        "master": {
            "key": "master",
            "template_item_id": MASTER_ID,
            "title": "{base}",
            "service_name": "{base_sn}",
        },
        "views": [
            {
                "key": "design",
                "template_item_id": DESIGN_ID,
                "title": "{base} - Design View",
                "service_name": "{base_sn}_Design",
            },
            {
                "key": "qc",
                "template_item_id": QC_ID,
                "title": "{base} - QC View",
                "service_name": "{base_sn}_QC",
            },
        ],
    }
    data.update(overrides)
    return Manifest(**data)


def with_out_of_scope() -> Manifest:
    """The same manifest plus the items stages 3-6 would create."""
    return make_manifest(
        groups=[{"key": "viewer_group", "title": "{base} - Viewer"}],
        maps=[
            {
                "key": "field_map",
                "template_item_id": MAP_ID,
                "item_type": "Web Map",
                "title": "{base} - Field Map",
            }
        ],
        apps=[
            {
                "key": "viewer_app",
                "template_item_id": APP_ID,
                "item_type": "Web Experience",
                "title": "{base} - Viewer",
            }
        ],
    )


CTX = NameContext(company="CompanyA", location="Moline")


def run(gis=None, manifest=None, ctx=CTX, state=None):
    return run_preflight(gis or FakeGIS(), manifest or make_manifest(), ctx, state=state)


def messages(report):
    return " ".join(p.message for p in report.problems)


# ---------------------------------------------------------------- the headline check


class TestTakenServiceName:
    """The check the whole stage exists for."""

    def test_a_taken_name_fails_preflight(self):
        report = run(FakeGIS(taken=["CompanyA_Moline_Design"]))
        assert not report.ok
        assert "CompanyA_Moline_Design" in messages(report)

    def test_the_planned_name_is_reported_unchanged(self):
        """A taken name must still be shown as the name the manifest asked for."""
        report = run(FakeGIS(taken=["CompanyA_Moline_Design"]))
        design = next(p for p in report.plan if p.key == "design")
        assert design.service_name == "CompanyA_Moline_Design"
        assert design.status == STATUS_TAKEN

    def test_never_probes_for_a_free_variant(self):
        """The one behaviour that proves preflight does not auto-suffix.

        An implementation that renamed on collision would have to ask AGOL
        whether the renamed form were free. Nothing may be queried but the names
        the manifest derives.
        """
        gis = FakeGIS(taken=["CompanyA_Moline", "CompanyA_Moline_Design"])
        run(gis)
        assert gis.content.queries == [
            "CompanyA_Moline",
            "CompanyA_Moline_Design",
            "CompanyA_Moline_QC",
        ]

    def test_reports_every_collision_not_only_the_first(self):
        """You run this against the real org; one collision per run is a bad loop."""
        gis = FakeGIS(taken=["CompanyA_Moline", "CompanyA_Moline_QC"])
        report = run(gis)
        taken = {p.key for p in report.plan if p.status == STATUS_TAKEN}
        assert taken == {"master", "qc"}

    def test_the_failure_explains_that_the_name_is_permanent(self):
        """A collision is unrecoverable by waiting, so the message has to say so."""
        report = run(FakeGIS(taken=["CompanyA_Moline"]))
        assert "permanent" in messages(report).lower()


# ---------------------------------------------------------------- template items


class TestTemplateResolution:
    def test_an_unresolvable_master_id_fails(self):
        items = default_items()
        del items[MASTER_ID]
        report = run(FakeGIS(items=items))
        assert not report.ok
        assert MASTER_ID in messages(report) and "master" in messages(report)

    def test_an_unresolvable_view_id_fails(self):
        items = default_items()
        del items[DESIGN_ID]
        report = run(FakeGIS(items=items))
        assert not report.ok
        assert "design" in messages(report)

    def test_a_master_pointing_at_a_view_fails(self):
        items = default_items()
        items[MASTER_ID] = FakeItem(MASTER_ID, "Oops", typeKeywords=["View Service"])
        report = run(FakeGIS(items=items))
        assert not report.ok
        assert "master" in messages(report)

    def test_a_view_pointing_at_the_master_fails(self):
        items = default_items()
        items[DESIGN_ID] = FakeItem(DESIGN_ID, "Oops", typeKeywords=["Hosted Service"])
        report = run(FakeGIS(items=items))
        assert not report.ok
        assert "design" in messages(report)

    def test_an_unresolvable_map_template_warns_rather_than_failing(self):
        """Stages 3-6 are out of scope; a stale app template must not block views."""
        items = default_items()
        del items[MAP_ID]
        report = run(FakeGIS(items=items), with_out_of_scope())
        assert report.ok
        assert [p.key for p in report.warnings] == ["field_map"]


# ---------------------------------------------------------------- the rest of stage 0


class TestPrivileges:
    def test_missing_privileges_fail_and_are_named(self):
        held = [p for p in ALL_PRIVILEGES if p != "portal:user:createGroup"]
        report = run(FakeGIS(privileges=held))
        assert not report.ok
        assert "create groups" in messages(report)


class TestDuplicateNames:
    def test_duplicates_fail_even_when_every_name_is_available(self):
        """The second create would collide with the name the first just took."""
        manifest = make_manifest(
            views=[
                {"key": "design", "template_item_id": DESIGN_ID,
                 "title": "{base} - A", "service_name": "{base_sn}_Same"},
                {"key": "qc", "template_item_id": QC_ID,
                 "title": "{base} - B", "service_name": "{base_sn}_Same"},
            ]
        )
        report = run(FakeGIS(taken=[]), manifest)
        assert not report.ok
        assert "CompanyA_Moline_Same" in messages(report)


class TestUnrenderableNames:
    def test_a_company_name_that_cannot_start_a_service_name_is_reported(self):
        report = run(ctx=NameContext(company="3M", location="Moline"))
        assert not report.ok
        assert "service_name_override" in messages(report)

    def test_an_unknown_pattern_placeholder_is_reported_not_raised(self):
        manifest = make_manifest(
            master={"key": "master", "template_item_id": MASTER_ID,
                    "title": "{nonsense}", "service_name": "{base_sn}"}
        )
        report = run(manifest=manifest)
        assert not report.ok
        assert "master" in messages(report)


class TestCleanRun:
    def test_a_clean_manifest_passes(self):
        report = run()
        assert report.ok
        assert not report.problems

    def test_every_in_scope_name_is_marked_available(self):
        report = run()
        assert [p.status for p in report.plan] == [STATUS_AVAILABLE] * 3

    def test_the_plan_is_ordered_master_then_views(self):
        """The printed plan is the creation order, so it has to read as one."""
        report = run()
        assert [p.key for p in report.plan] == ["master", "design", "qc"]

    def test_only_master_and_view_names_are_checked(self):
        """Groups, maps and apps reserve no service name and are out of scope."""
        gis = FakeGIS()
        run(gis, with_out_of_scope())
        assert gis.content.queries == [
            "CompanyA_Moline",
            "CompanyA_Moline_Design",
            "CompanyA_Moline_QC",
        ]

    def test_out_of_scope_items_appear_in_the_plan_marked_as_such(self):
        report = run(manifest=with_out_of_scope())
        out = {p.key: p for p in report.plan if p.status == STATUS_OUT_OF_SCOPE}
        assert set(out) == {"viewer_group", "field_map", "viewer_app"}
        assert all(p.service_name is None for p in out.values())


class TestResume:
    """Stage 1 takes the master's name. Preflight must not then reject it."""

    @pytest.fixture
    def state_with_master(self, tmp_path):
        state = ProjectState.load_or_create(
            state_dir=tmp_path, slug="companya-moline", company="CompanyA",
            location="Moline", manifest_name="test", manifest_version=1,
        )
        state.record(CreatedItem(
            key="master", item_id=_id(50), item_type="Feature Service",
            title="CompanyA Moline", service_name="CompanyA_Moline",
        ))
        return state

    def test_a_name_this_run_already_created_is_not_re_checked(self, state_with_master):
        gis = FakeGIS(taken=["CompanyA_Moline"])
        report = run(gis, state=state_with_master)
        assert gis.content.queries == ["CompanyA_Moline_Design", "CompanyA_Moline_QC"]
        assert report.ok

    def test_the_recorded_item_is_shown_as_already_existing(self, state_with_master):
        report = run(FakeGIS(taken=["CompanyA_Moline"]), state=state_with_master)
        master = next(p for p in report.plan if p.key == "master")
        assert master.status == STATUS_EXISTS


class TestCollectsEverything:
    def test_unrelated_problems_are_all_reported_in_one_run(self):
        """Fail-fast would mean a round trip to Windows per problem."""
        items = default_items()
        del items[QC_ID]
        gis = FakeGIS(
            items=items,
            taken=["CompanyA_Moline_Design"],
            privileges=[p for p in ALL_PRIVILEGES if p != "portal:user:createItem"],
        )
        report = run(gis)
        keys = {p.key for p in report.errors}
        assert {"privileges", "qc", "design"} <= keys


class TestAgolErrors:
    def test_a_failed_availability_check_is_reported_not_raised(self):
        """A transient AGOL error should not cost a round trip to read a traceback."""

        class Exploding(FakeContent):
            def is_service_name_available(self, service_name, service_type):
                raise RuntimeError("connection reset")

        gis = FakeGIS()
        gis.content = Exploding(default_items(), set())
        report = run(gis)
        assert not report.ok
        assert "connection reset" in messages(report)


class TestOutOfScopeNeverBlocks:
    """Stages 3-6 are not built. Nothing about them may stop stages 0-2."""

    def test_a_broken_group_title_pattern_warns_rather_than_failing(self):
        manifest = with_out_of_scope().model_copy(
            update={"groups": [type(with_out_of_scope().groups[0])(
                key="viewer_group", title="{nonsense}")]}
        )
        report = run(manifest=manifest)
        assert report.ok
        assert [p.key for p in report.warnings] == ["viewer_group"]

    def test_a_broken_map_title_pattern_warns_rather_than_failing(self):
        manifest = with_out_of_scope().model_copy(
            update={"maps": [type(with_out_of_scope().maps[0])(
                key="field_map", template_item_id=MAP_ID, item_type="Web Map",
                title="{nonsense}")]}
        )
        report = run(manifest=manifest)
        assert report.ok
        assert [p.key for p in report.warnings] == ["field_map"]


# ---------------------------------------------------------------- the command


class TestProvisionCommand:
    """`provision` end to end, with AGOL replaced by the fakes above.

    In this build the command runs stage 0 and stops, so it writes nothing to
    ArcGIS Online at all -- which is what makes it safe to re-run against the
    real org while the naming is still being settled.
    """

    @pytest.fixture(autouse=True)
    def wide_terminal(self, monkeypatch):
        # rich wraps table cells to the terminal width, splitting the names asserted on.
        monkeypatch.setenv("COLUMNS", "200")

    @pytest.fixture
    def manifest_file(self, tmp_path):
        import yaml

        path = tmp_path / "test-manifest.yaml"
        path.write_text(yaml.safe_dump(make_manifest().model_dump()))
        return path

    @pytest.fixture
    def state_dir(self, tmp_path, monkeypatch):
        from agol_provision import cli

        d = tmp_path / "state"
        monkeypatch.setattr(cli, "STATE_DIR", d)
        return d

    def invoke(self, monkeypatch, manifest_file, state_dir, gis=None, extra=(),
               dry_run=True):
        """Preflight-scoped by default: --dry-run stops before stage 1.

        Stage 1 is covered in `test_master.py`, with fakes that can be copied.
        """
        from click.testing import CliRunner

        from agol_provision import auth
        from agol_provision.cli import main

        monkeypatch.setattr(auth, "connect", lambda profile: gis or FakeGIS())
        return CliRunner().invoke(main, [
            "provision", "--company", "CompanyA", "--location", "Moline",
            "--manifest", str(manifest_file), *(["--dry-run"] if dry_run else []), *extra,
        ])

    def test_a_clean_run_reports_the_plan_and_passes(self, monkeypatch, manifest_file, state_dir):
        result = self.invoke(monkeypatch, manifest_file, state_dir)
        assert result.exit_code == 0, result.output
        assert "CompanyA_Moline_Design" in result.output

    def test_a_taken_name_exits_non_zero(self, monkeypatch, manifest_file, state_dir):
        gis = FakeGIS(taken=["CompanyA_Moline_Design"])
        result = self.invoke(monkeypatch, manifest_file, state_dir, gis)
        assert result.exit_code == 1
        assert "CompanyA_Moline_Design" in result.output

    def test_a_dry_run_writes_no_state_file(self, monkeypatch, manifest_file, state_dir):
        result = self.invoke(monkeypatch, manifest_file, state_dir)
        assert result.exit_code == 0, result.output
        assert not state_dir.exists()

    def test_a_failed_preflight_writes_no_state(self, monkeypatch, manifest_file, state_dir):
        """A real run, not a dry one: preflight must gate stage 1's first write."""
        gis = FakeGIS(taken=["CompanyA_Moline"])
        self.invoke(monkeypatch, manifest_file, state_dir, gis, dry_run=False)
        assert not state_dir.exists()

    def test_company_and_location_are_required_together(self, monkeypatch, state_dir):
        from click.testing import CliRunner

        from agol_provision.cli import main

        result = CliRunner().invoke(main, ["provision", "--company", "CompanyA"])
        assert result.exit_code == 1
        assert "--location" in result.output

    def test_a_dry_run_says_plainly_that_nothing_was_created(
        self, monkeypatch, manifest_file, state_dir
    ):
        result = self.invoke(monkeypatch, manifest_file, state_dir)
        assert "Nothing was created" in result.output

    def test_the_remedy_is_explained_once_not_per_collision(
        self, monkeypatch, manifest_file, state_dir
    ):
        """Seven taken names should not print the same paragraph seven times."""
        gis = FakeGIS(taken=["CompanyA_Moline", "CompanyA_Moline_Design",
                             "CompanyA_Moline_QC"])
        result = self.invoke(monkeypatch, manifest_file, state_dir, gis)
        assert result.output.count("--service-name-override") == 1

    def test_every_taken_name_is_still_listed(self, monkeypatch, manifest_file, state_dir):
        gis = FakeGIS(taken=["CompanyA_Moline", "CompanyA_Moline_QC"])
        result = self.invoke(monkeypatch, manifest_file, state_dir, gis)
        assert result.output.count("is already taken") == 2

    def test_the_override_changes_the_stem_of_every_name(
        self, monkeypatch, manifest_file, state_dir
    ):
        """The escape hatch naming.py's own error messages point at."""
        gis = FakeGIS()
        self.invoke(monkeypatch, manifest_file, state_dir, gis,
                    extra=["--service-name-override", "CompA_MO"])
        assert gis.content.queries == ["CompA_MO", "CompA_MO_Design", "CompA_MO_QC"]

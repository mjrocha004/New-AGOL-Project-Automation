"""State is what makes a failed run recoverable rather than a cleanup job."""

import json

import pytest

from agol_provision.state import (
    CreatedItem,
    ProjectState,
    StateError,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def fresh(state_dir):
    return ProjectState.load_or_create(
        state_dir=state_dir,
        slug="companya-moline",
        company="CompanyA",
        location="Moline",
        manifest_name="vsclr-standard",
        manifest_version=1,
    )


def _item(key, item_id, item_type="Feature Service"):
    return CreatedItem(key=key, item_id=item_id, item_type=item_type, title=key)


class TestRoundTrip:
    def test_new_project_starts_empty(self, fresh):
        assert fresh.items == {}
        assert fresh.stages_completed == []

    def test_recorded_items_survive_reload(self, fresh, state_dir):
        fresh.record(_item("master", "aaa111"))
        fresh.complete_stage("master")

        reloaded = ProjectState.load(state_dir / "companya-moline.json")
        assert reloaded.get("master").item_id == "aaa111"
        assert reloaded.is_stage_complete("master")

    def test_record_flushes_immediately(self, fresh, state_dir):
        """An item must be on disk before the next API call, or a crash orphans it."""
        fresh.record(_item("master", "aaa111"))
        on_disk = json.loads((state_dir / "companya-moline.json").read_text())
        assert on_disk["items"]["master"]["item_id"] == "aaa111"

    def test_save_leaves_no_temp_file(self, fresh, state_dir):
        fresh.record(_item("master", "aaa111"))
        assert list(state_dir.glob("*.tmp")) == []


class TestResume:
    def test_load_or_create_resumes_existing(self, state_dir, fresh):
        fresh.record(_item("master", "aaa111"))

        resumed = ProjectState.load_or_create(
            state_dir=state_dir,
            slug="companya-moline",
            company="CompanyA",
            location="Moline",
            manifest_name="vsclr-standard",
            manifest_version=1,
        )
        assert resumed.has("master")

    def test_refuses_resume_across_manifest_version_change(self, state_dir, fresh):
        fresh.record(_item("master", "aaa111"))

        with pytest.raises(StateError, match="v2 was requested"):
            ProjectState.load_or_create(
                state_dir=state_dir,
                slug="companya-moline",
                company="CompanyA",
                location="Moline",
                manifest_name="vsclr-standard",
                manifest_version=2,
            )

    def test_refuses_to_mix_two_projects_in_one_file(self, state_dir, fresh):
        fresh.record(_item("master", "aaa111"))

        with pytest.raises(StateError, match="Refusing to mix"):
            ProjectState.load_or_create(
                state_dir=state_dir,
                slug="companya-moline",
                company="CompanyB",
                location="Peoria",
                manifest_name="vsclr-standard",
                manifest_version=1,
            )

    def test_truncated_state_gives_actionable_error(self, state_dir):
        bad = state_dir / "broken.json"
        bad.write_text('{"slug": "broken", "items": {')  # hard-kill mid-write
        with pytest.raises(StateError, match="already exist in AGOL"):
            ProjectState.load(bad)


class TestGuards:
    def test_duplicate_record_is_refused(self, fresh):
        fresh.record(_item("master", "aaa111"))
        with pytest.raises(StateError, match="would orphan the original"):
            fresh.record(_item("master", "bbb222"))

    def test_unknown_stage_is_refused(self, fresh):
        with pytest.raises(StateError, match="Unknown stage"):
            fresh.complete_stage("not-a-stage")

    def test_completing_a_stage_twice_is_harmless(self, fresh):
        fresh.complete_stage("master")
        fresh.complete_stage("master")
        assert fresh.stages_completed == ["master"]


class TestRollback:
    def test_destroy_order_is_reverse_creation(self, fresh):
        """AGOL refuses to delete a master while its views still exist."""
        fresh.record(_item("master", "aaa111"))
        fresh.record(_item("design_view", "bbb222"))
        fresh.record(_item("qc_view", "ccc333"))
        fresh.record(_item("field_map", "ddd444", "Web Map"))

        keys = [i.key for i in fresh.destroy_order()]
        assert keys == ["field_map", "qc_view", "design_view", "master"]
        assert keys.index("design_view") < keys.index("master")

    def test_destroy_order_survives_reload(self, fresh, state_dir):
        """JSON object order must round-trip, or rollback deletes in the wrong order."""
        for k, i in [("master", "a"), ("view", "b"), ("map", "c")]:
            fresh.record(_item(k, i))

        reloaded = ProjectState.load(state_dir / "companya-moline.json")
        assert [i.key for i in reloaded.destroy_order()] == ["map", "view", "master"]

    def test_forget_clears_resumable_stages(self, fresh):
        fresh.record(_item("master", "aaa111"))
        fresh.complete_stage("master")
        fresh.forget("master")

        assert not fresh.has("master")
        assert fresh.stages_completed == []

    def test_item_ids_collects_everything_created(self, fresh):
        fresh.record(_item("master", "aaa111"))
        fresh.record(_item("view", "bbb222"))
        assert fresh.item_ids() == {"aaa111", "bbb222"}

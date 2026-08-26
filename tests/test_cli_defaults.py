"""The connection default is what makes the tool usable without any credential
setup on a machine with ArcGIS Pro. It is worth pinning.
"""

import pytest

from agol_provision.cli import main


def _profile_option(command_name: str):
    cmd = main.commands[command_name]
    return next(p for p in cmd.params if p.name == "profile")


@pytest.mark.parametrize("command", ["discover", "spike-master", "doctor"])
def test_every_command_defaults_to_the_pro_connection(command):
    """No --profile flag should be needed inside ArcGIS Pro's Python."""
    assert _profile_option(command).default == "home"


@pytest.mark.parametrize("command", ["discover", "spike-master", "doctor"])
def test_profile_is_optional(command):
    assert not _profile_option(command).required


def test_the_default_routes_through_the_arcgis_pro_path():
    from agol_provision.auth import uses_arcgis_pro

    assert uses_arcgis_pro(_profile_option("discover").default)


def test_setup_profile_is_the_only_command_needing_credentials():
    """Everything else borrows Pro's sign-in, so nothing else should ask."""
    needs_username = {
        name for name, cmd in main.commands.items()
        if any(p.name == "username" for p in cmd.params)
    }
    assert needs_username == {"setup-profile"}


def test_spike_master_requires_an_explicit_target():
    """It writes to AGOL, so it must never guess which service to copy."""
    cmd = main.commands["spike-master"]
    master = next(p for p in cmd.params if p.name == "master_id")
    assert master.required


class TestSelectorHelpers:
    """`discover` needs to be told which items are the templates. These commands
    exist so that selector is discoverable rather than guessed at.
    """

    @pytest.mark.parametrize("command", ["list-groups", "list-content"])
    def test_helper_commands_exist(self, command):
        assert command in main.commands

    @pytest.mark.parametrize("command", ["list-groups", "list-content"])
    def test_helpers_default_to_the_pro_connection(self, command):
        assert _profile_option(command).default == "home"

    def test_list_content_requires_a_query(self):
        """Listing the whole org by accident would be slow and useless."""
        cmd = main.commands["list-content"]
        assert next(p for p in cmd.params if p.name == "query").required

    def test_discover_offers_a_dry_run(self):
        cmd = main.commands["discover"]
        dry = next(p for p in cmd.params if p.name == "dry_run")
        assert dry.is_flag and dry.default is False

    def test_all_three_selectors_are_optional_individually(self):
        """Group, ids, and query are alternatives; collect() enforces that one is
        given, so click must not mark any of them required."""
        cmd = main.commands["discover"]
        for name in ("group", "ids_file", "query"):
            assert not next(p for p in cmd.params if p.name == name).required


class TestReadOnlyCommands:
    """Only spike-master writes to AGOL. Everything else in Phase 0 is read-only,
    which is what makes it safe to iterate on a selector."""

    @pytest.mark.parametrize("command", ["doctor", "list-groups", "list-content"])
    def test_read_only_commands_need_no_confirmation(self, command):
        cmd = main.commands[command]
        assert not any(p.name == "yes" for p in cmd.params)

    def test_spike_master_has_a_confirmation_escape_hatch(self):
        cmd = main.commands["spike-master"]
        assert any(p.name == "yes" for p in cmd.params)


class TestSingleIdOption:
    """--ids needs a file that must already exist, which turned "add the master"
    into "create a one-line file first". --id takes the id directly.
    """

    def test_discover_accepts_a_bare_item_id(self):
        cmd = main.commands["discover"]
        opt = next(p for p in cmd.params if p.name == "extra_ids")
        assert opt.multiple and "--id" in opt.opts

    def test_id_and_ids_are_distinct_options(self):
        cmd = main.commands["discover"]
        names = {p.name for p in cmd.params}
        assert {"extra_ids", "ids_file"} <= names

    def test_id_requires_no_pre_existing_file(self):
        """--ids uses click.Path(exists=True); --id must not."""
        import click

        cmd = main.commands["discover"]
        opt = next(p for p in cmd.params if p.name == "extra_ids")
        assert not isinstance(opt.type, click.Path)

    def test_ids_file_still_fails_fast_on_a_bad_path(self):
        """Failing on a typo'd path is right -- silently reading nothing is not."""
        import click

        cmd = main.commands["discover"]
        opt = next(p for p in cmd.params if p.name == "ids_file")
        assert isinstance(opt.type, click.Path) and opt.type.exists


class TestPreview:
    """Service names are permanent org-wide, so they get one chance to be right.
    preview renders them locally, before anything is created."""

    def test_command_exists(self):
        assert "preview" in main.commands

    def test_needs_no_connection(self):
        """Pure local computation -- no --profile, so it cannot touch AGOL."""
        cmd = main.commands["preview"]
        assert not any(p.name == "profile" for p in cmd.params)

    def test_renders_the_example_manifest(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        # rich wraps table cells to the terminal width, which would split the
        # names this asserts on.
        monkeypatch.setenv("COLUMNS", "200")
        result = CliRunner().invoke(main, [
            "preview", "--manifest", "agol_provision/templates/EXAMPLE.yaml",
            "--company", "CompanyA", "--location", "Moline",
        ])
        assert result.exit_code == 0, result.output
        assert "CompanyA_Moline_Design" in result.output

    def test_flags_duplicate_service_names(self, tmp_path):
        """A collision would fail the run partway through, leaving debris."""
        import yaml
        from click.testing import CliRunner

        src = yaml.safe_load(
            open("agol_provision/templates/EXAMPLE.yaml").read().split("\n\n", 1)[-1]
        )
        src["views"][1]["service_name"] = src["views"][0]["service_name"]
        path = tmp_path / "dupe.yaml"
        path.write_text(yaml.safe_dump(src))

        result = CliRunner().invoke(main, ["preview", "--manifest", str(path)])
        assert "Duplicate service names" in result.output.replace("\n", " ")

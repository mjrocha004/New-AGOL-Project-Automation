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

"""The shipped example manifest must stay valid and must render VSCLR's naming
convention exactly. This is the end-to-end check on manifest + naming together.
"""

from pathlib import Path

import pytest

from agol_provision.manifest import load_manifest
from agol_provision.naming import NameContext

EXAMPLE = Path(__file__).resolve().parent.parent / "agol_provision" / "templates" / "EXAMPLE.yaml"


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(EXAMPLE)


@pytest.fixture
def ctx():
    return NameContext(company="CompanyA", location="Moline")


def test_example_manifest_is_valid(manifest):
    assert manifest.name == "example"


def test_covers_the_full_documented_inventory(manifest):
    assert len(manifest.views) == 7
    assert len(manifest.groups) == 4
    assert len(manifest.maps) == 4
    assert len(manifest.apps) == 5
    assert manifest.total_items() == 21


def test_master_renders_to_the_documented_convention(manifest, ctx):
    assert ctx.render_title(manifest.master.title) == "CompanyA Moline"
    assert ctx.render_service_name(manifest.master.service_name) == "CompanyA_Moline"


def test_design_view_renders_to_the_documented_convention(manifest, ctx):
    design = next(v for v in manifest.views if v.key == "design")
    assert ctx.render_title(design.title) == "CompanyA Moline - Design View"
    assert ctx.render_service_name(design.service_name) == "CompanyA_Moline_Design"


def test_punctuated_title_survives_but_service_name_is_sanitized(manifest, ctx):
    """"Contractor / CX Redline View" is a legal title but an illegal service name."""
    redline = next(v for v in manifest.views if v.key == "redline")
    assert ctx.render_title(redline.title) == "CompanyA Moline - Contractor / CX Redline View"
    assert ctx.render_service_name(redline.service_name) == "CompanyA_Moline_Redline"


def test_every_service_name_is_url_safe(manifest, ctx):
    names = [ctx.render_service_name(manifest.master.service_name)] + [
        ctx.render_service_name(v.service_name) for v in manifest.views
    ]
    for n in names:
        assert n.replace("_", "").isalnum(), n
        assert n[0].isalpha(), n


def test_service_names_are_unique(manifest, ctx):
    """AGOL reserves service names org-wide, permanently. A collision within one
    project would fail the run partway through."""
    names = [ctx.render_service_name(manifest.master.service_name)] + [
        ctx.render_service_name(v.service_name) for v in manifest.views
    ]
    assert len(names) == len(set(names))


def test_maps_precede_apps_in_creation_order(manifest):
    keys = [i.key for i in manifest.cloned_items]
    assert keys.index("viewer_map") < keys.index("exb_project_viewer")


def test_every_view_is_consumed_by_something(manifest):
    """A view nothing consumes is either dead weight or a missing edge."""
    consumed = {ref for spec in [*manifest.groups, *manifest.cloned_items]
                for ref in spec.consumes}
    orphans = {v.key for v in manifest.views} - consumed
    assert orphans == set(), f"views consumed by nothing: {orphans}"


def test_every_group_receives_at_least_one_item(manifest):
    """A group nothing is shared to would be created empty."""
    shared_to = {g for spec in manifest.cloned_items for g in spec.share_to}
    empty = manifest.group_keys - shared_to
    assert empty == set(), f"groups nothing is shared to: {empty}"

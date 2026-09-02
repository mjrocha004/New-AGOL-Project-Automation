# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI that provisions a complete ArcGIS Online project from templates —
one master feature service, 7 views, 4 groups, 4 web maps, 5 Experience Builder
apps, and a dashboard (21 items), all named from `COMPANY LOCATION` and wired to
each other. See `README.md` for the user-facing setup and `docs/implementation-plan.md`
for the phased plan and the reasoning behind the architecture.

## Status and what is blocked

Phase 0 (discovery + master-copy spike) and the core modules are built. **The
provisioning stages are deliberately not started.** Two design forks depend on
artifacts that only a run against the real org can produce:

| Artifact | Decides |
| --- | --- |
| `docs/spike-master-copy.md` | Whether the master copies faithfully, or needs a publish-from-FGDB fallback |
| `docs/discovery-report.md` | Whether views filter uniformly across layers, or need per-layer `update_definition()` |

If those files do not exist yet, they have not been generated. Do not build the
master or views stages by guessing which fork applies — ask.

## Commands

```bash
uv sync --group dev          # build the environment
uv run pytest                # all tests (~290, no network required)
uv run pytest tests/test_naming.py::TestSanitizeServiceName -x
uv run agol-provision --help
```

`--profile` defaults to `home`, which borrows ArcGIS Pro's sign-in. Pass a stored
profile name instead on a machine without Pro. (`pro` is accepted as a synonym;
outside a hosted notebook the ArcGIS API rewrites `home` to `pro` anyway.)

**Two test files assert that the docs match the code**, and will fail a commit
that drifts: `test_readme_accuracy.py` checks every
`python -m agol_provision.cli ...` invocation in `README.md` against the real CLI,
and `test_no_dead_options.py` fails any click option that is declared but never
read. Update the README in the same commit as a flag rename.

`pyproject.toml` already sets `addopts = "-q"`. Passing `-q` again makes it `-qq`,
which suppresses the pass/fail summary line — run bare `uv run pytest` when you
need the count.

Inside ArcGIS Pro's Python environment there is no console script; run the module
directly, which needs no install step:

```bat
python -m agol_provision.cli doctor --profile home
```

Tests never touch the network. Anything requiring AGOL is exercised through fakes
(`tests/test_discovery.py` has the pattern). Keep it that way — the user runs the
live commands, this environment has no credentials.

## Architecture

**The template inventory is data, not code.** `agol_provision/templates/*.yaml`
declares the items and how they reference each other; the code is a generic
walker. Adding or dropping an item for a client is a manifest edit. `manifest.py`
validates the cross-references (`consumes`, `share_to`) at load time so a typo
fails before any AGOL call rather than at stage 4 of a live run.

**Creation order is the dependency graph**, and rollback is its reverse:
`master → views → groups → maps → apps → sharing`. `state.py` records items in
creation order and derives `destroy_order()` by reversing what was *actually*
recorded, not an assumed sequence — AGOL refuses to delete a feature service
while its views still exist.

**`naming.py` handles two namespaces with different rules.** Item titles are
display strings (`CompanyA Moline - Contractor / CX Redline View`). Service names
form part of a REST URL, must start with a letter, and are reserved org-wide
*permanently* — even after deletion. Preflight checks every derived name before
creating anything.

**`safety.py` guards the only destructive operation.** `spike-master` creates a
temporary service and deletes it. The code path makes deleting anything else
structurally impossible, but that is checked rather than assumed: the delete is
refused unless the item carries the spike's `ZZZ_SPIKE_TEST_` prefix. An orphaned
test service is a nuisance; deleting the wrong thing is not.

**Discovery derives the dependency graph by scanning serialized item JSON** for
the ids of other template items, rather than parsing each item type's schema.
Web maps, dashboards, and Experience Builder apps all nest their references
differently; one scan finds them all. Same principle `remap_data()` works on.

## AGOL constraints that fail silently

These are not discoverable from the code and each produces a broken project
rather than an error:

- **`clone_items(search_existing_items=False)` is mandatory.** It defaults to
  `True` and matches on a `source-<item_id>` type keyword. At the default,
  provisioning a second project in the same org silently *reuses the first
  project's* cloned maps and apps.
- **Never clone hosted feature layer views.** Cloning a view within one org can
  produce an empty service or silently re-point at the source. Create views
  natively with `create_view()` and replay the template view's configuration.
- **Use `remap_data()`, not `item_mapping`.** `item_mapping` does not work with
  dashboards. `remap_data()` (arcgis ≥ 2.4, enforced in `auth.py`) rewrites ids
  throughout an item's JSON.
- **Experience Builder reads its draft.** A remapped experience must be
  republished before end users see corrected data sources.
- **`--profile home` requires ArcGIS Pro's Python**, because it reads the sign-in
  token through `arcpy`. It cannot work from a standalone venv — use a stored
  profile there.

## Verifying app rewiring

The check that catches a silent `remap_data()` failure: after provisioning, no
cloned item's JSON may still contain a *template* item id. Without it, a broken
app looks fine until someone opens it.

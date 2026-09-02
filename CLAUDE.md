# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI that provisions a complete ArcGIS Online project from templates —
one master feature service, 7 views, 4 groups, 4 web maps, 3 Experience Builder
apps, and a dashboard (20 items), all named from `COMPANY LOCATION` and wired to
each other. See `README.md` for the user-facing setup and `docs/implementation-plan.md`
for the phased plan and the reasoning behind the architecture.

## Status and what is blocked

Phase 0 (discovery + master-copy spike) and the core modules are built.
`docs/implementation-plan.md` holds the phased plan and the reasoning.

**Phase 2 is being built in stage order. Stage 0 (preflight) is done** —
`preflight.py` plus `provision`, which resolves the templates, renders every
name, and hard-fails on a taken service name. It writes nothing to AGOL, so it
is safe to re-run. **Stages 1 (master) and 2 (views) are next.** Stages 3-6
(groups, maps, apps, sharing) are deferred at the user's request: creating four
groups by hand takes a minute, creating seven views takes a day, so the views
stage carries nearly all the value and none of the clone/remap risk. The plan
anticipated this split in its Scope note. `share_to` stays in the manifest,
validated but unused, until the groups stage exists. Provisioned items land at
the org root; moving them into a folder is a manual step by choice.

**Both design forks are now closed** by runs against the real org:

| Artifact | Decided |
| --- | --- |
| `docs/discovery-report.md` | 2 of the 7 views (`redline_qc_view_editable`, `cx_redline_edit_view`) use a different query per layer, so per-layer `update_definition()` after `create_view()` is required. Field visibility is uniform across all seven and no view hides a field, so `visible_fields` is not needed at all. |
| `docs/spike-master-copy.md` | `USABLE WITH FIXUPS`, **no critical differences**. All 17 layers, 1 table, every field and type, 47 domains, attachments, and editor tracking survive `copy_feature_layer_collection()`. No FGDB fallback is needed. The only real loss is 10 user-defined indexes — `build_status_Index` on 9 layers and `I25bore_depth` on 1 — which stage 1 must reapply. The other 73 reported differences are system-generated index names that could never match. |

`docs/phase-2-master-and-views.md` is the spec for the next work. Neither
generated report is committed; both are produced on the Windows machine.

## Commands

Local development (macOS, no arcpy) uses uv. Live runs against ArcGIS Online
happen on Windows in ArcGIS Pro's own Python, which has no uv and no venv — see
below.

```bash
uv sync --group dev          # build the environment
uv run pytest                # all tests (288, no network required)
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
- **`copy_feature_layer_collection()` copies nothing by default.** It selects a
  *subset*, so called with both `layers` and `tables` left as `None` it raises
  rather than copying everything. The values it selects with are **positional
  indexes** into `Item.layers` / `Item.tables` — it evaluates `self.layers[idx]`
  — not layer ids. The master's 17 layers carry ids running to 19, so passing ids
  indexes off the end of the list: a plausible-looking fix that fails differently.
  `copy_whole_service()` in `cli.py` wraps this and `tests/test_spike_copy.py`
  pins both behaviours. The method also strips each layer's `indexes` before
  applying the definition, so a schema diff is *expected* to report index
  differences — that is the method's doing, not the template's, and should not be
  read as the master failing to copy.

## Known gaps

Real, understood, not yet fixed. Each has already produced a wrong answer once.

- **Discovery silently discards references to items outside the inventory.**
  `find_dependencies()` filters the ids it finds down to `known_ids`, so an app
  pointing at a map that is not in `ids.txt` is reported as having *no*
  dependencies — indistinguishable from genuinely standing alone. Two Experience
  Builder apps were flagged this way and only one truly stood alone. Referenced
  ids that are not in the inventory should be reported, not dropped.
- **`_common_title_prefix()` can leak the template's location into every
  pattern.** It takes the longest prefix shared by *all* titles, so a single
  oddly-named template collapses the stem: templates titled `Zayo Chicago …` plus
  one `Zayo CX Field Map` yield the stem `Zayo `, and every generated title and
  service name keeps the word `Chicago`. Always read `preview` output before a
  run — service names are reserved org-wide permanently.
- **The working manifest is not in version control.**
  `agol_provision/templates/vsclr-standard.yaml` is generated by `discover` and
  then hand-edited, and `discover` overwrites it. Until it is committed, a re-run
  silently destroys the hand edits. `EXAMPLE.yaml` is tracked; the real one is not.
- **`Manifest.cloned_items` is `[*maps, *apps]` with no topological sort.** An app
  that consumes another app — the Project Viewer ExB consumes the Summary
  dashboard — can be ordered ahead of its own dependency. Must be fixed before
  stage 4.

## Not in the manifest, on purpose

Some feature services and views are shared across *all* projects rather than
recreated per project. They belong in neither `views:` nor `maps:`: they are
created once, by hand, and reused. `remap_data()` only rewrites ids present in
its mapping, so leaving them out is what makes cloned maps keep pointing at them.
The manifest describes what a project **creates**, not everything it touches.

## Verifying app rewiring

The check that catches a silent `remap_data()` failure: after provisioning, no
cloned item's JSON may still contain a *template* item id. Without it, a broken
app looks fine until someone opens it.

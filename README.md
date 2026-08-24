# AGOL Project Provisioning

Stands up a complete ArcGIS Online project from templates: a master feature
service, its views, groups, maps, dashboards, and Experience Builder apps —
correctly named, wired, and shared.

Given a company and a location, it derives every item name from the standard
convention (`CompanyA Moline`, `CompanyA Moline - Design View`) and builds the
whole dependency graph in order.

## Status

Phase 0 (discovery and the master-copy spike) and the core modules are built and
tested. The provisioning stages are next, and are deliberately blocked on what
Phase 0 reports about the real templates.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
```

Store credentials once. The password goes into the OS keyring via the ArcGIS
API's profile mechanism — never into this repo, shell history, or command output.

```bash
uv run agol-provision setup-profile --name vsclr --org https://YOURORG.maps.arcgis.com --username YOURUSER
```

## Phase 0 — run these first

### 1. Discover the templates (read-only)

Audits every template item and writes three things: a manifest with real item
ids, a JSON snapshot of each template, and a findings report.

```bash
uv run agol-provision discover --profile vsclr --group "Templates"
```

Locate the templates by group, by explicit ids (`--ids ids.txt`), or by search
(`--query 'title:VSCLR Template'`).

This writes:

| Path | What it is |
| --- | --- |
| `agol_provision/templates/vsclr-standard.yaml` | The manifest. Review and edit it. |
| `snapshots/*.json` | Per-template JSON. **Commit these** — they are the version history. |
| `docs/discovery-report.md` | Findings to read before building. |

The dependency graph is derived by scanning each item's JSON for the ids of other
template items, so it finds edges regardless of item type — including the ones
buried in dashboard widget datasources and Experience Builder draft configs.

After running, edit the generated manifest: shorten the suggested keys, check the
title and `service_name` patterns, and fill in `share_to` for each map and app
(sharing cannot be derived, because the templates' own sharing points at template
groups rather than the per-project groups this tool creates).

### 2. Spike the master copy (writes one temporary service)

Answers the one question the whole tool rests on: does copying the master
preserve its relationship classes, domains, subtypes, and indexes?

```bash
uv run agol-provision spike-master --profile vsclr --master-id <TEMPLATE_MASTER_ID>
```

Creates one service named `ZZZ_SPIKE_TEST_<prefix>`, diffs its schema against the
template, writes `docs/spike-master-copy.md`, and deletes it. Pass `--keep` to
leave it for inspection.

Three verdicts:

- **IDENTICAL** — `copy_feature_layer_collection()` is the master strategy.
- **USABLE WITH FIXUPS** — same, plus explicit reapplication of the listed settings.
- **NOT USABLE** — critical structure is lost; fall back to publishing from the
  file geodatabase.

> AGOL reserves a hosted service name permanently, even after the service is
> deleted. The spike reuses one fixed name so it only ever burns that one.

## Where the templates live

**ArcGIS Online is the source of truth.** Web maps, dashboards, and Experience
Builder apps have no meaningful off-platform representation — a SharePoint copy
would be exported JSON you could only use by pushing it back through this same
API. `clone_items()` and `remap_data()` read live AGOL items.

Version history is handled by `snapshots/` in this repo: `discover` exports each
template's JSON, and committing that gives real, diffable history. Protect the
templates themselves by having a service account own them in a read-only group.

SharePoint keeps the file geodatabase, the XML workspace document, and human
documentation — the artifacts the Pro desktop side actually needs.

## Design

The template inventory is **data, not code**. `agol_provision/templates/*.yaml`
declares what to build and how the pieces reference each other; the code walks
that graph. Dropping the Permit Management app for a smaller client is a config
edit, not a rewrite. See `templates/EXAMPLE.yaml` for the full 21-item shape.

| Module | Responsibility |
| --- | --- |
| `naming.py` | Titles and service names. Separate namespaces with different rules. |
| `manifest.py` | Manifest schema and cross-reference validation. |
| `state.py` | What a run created. Idempotent resume and rollback. |
| `discovery.py` | Read-only template audit. |
| `schema_diff.py` | Structural comparison of two feature services. |
| `auth.py` | Profile-based connection and privilege checks. |

### Constraints worth knowing

- **Service names are permanent.** AGOL reserves a hosted service name org-wide
  forever, even after deletion. Preflight checks every derived name before
  anything is created, so a collision fails before it leaves debris.
- **Views are created, never cloned.** Cloning a view within one org is
  unreliable — it can produce an empty service or silently re-point at the
  source. Views are made natively from the new master with the template view's
  configuration replayed onto them.
- **`clone_items(search_existing_items=False)` is mandatory.** It defaults to
  `True` and matches on a `source-<item_id>` type keyword. At the default,
  provisioning a second project would silently reuse the first project's cloned
  maps and apps.
- **`remap_data()`, not `item_mapping`.** `item_mapping` does not work with
  dashboards. `remap_data()` (ArcGIS API 2.4+) rewrites ids throughout an item's
  JSON and works across web maps, dashboards, and web experiences.
- **Experience Builder reads its draft.** A remapped experience must be
  republished before end users see corrected data sources.

## Tests

```bash
uv run pytest
```

119 tests, no network access required — naming, manifest validation, state
transitions, dependency scanning, and schema comparison all run against fakes.

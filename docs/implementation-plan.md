<!-- Copied from the plan approved in the session that started this repo. Kept in
     version control so the reasoning travels with the code. Phases 0 and 1 are
     built. Phase 2 is being built in stage order, starting with stages 0-2
     (preflight, master, views); stages 3-6 are deferred. See CLAUDE.md. -->

# AGOL Project Provisioning Automation

> ## STATUS — updated after Phase 0 was built
>
> **Phase 0 and Phase 1 are built and committed.** 288 tests pass. `discover` has
> now been run against the real organization; `spike-master` has not yet
> completed one.
>
> **Phase 2 is being built in stage order, starting with stages 0-2** — preflight,
> master, views — and stopping there. Stages 3-6 (groups, maps, apps, sharing) are
> deferred: creating the four groups by hand takes a minute, creating the seven
> views takes a day, so stage 2 carries nearly all the time saving and none of the
> clone/remap risk. This is the split the Scope note at the foot of this document
> already anticipated, taken deliberately rather than by drift.
>
> Of the two design forks, one is resolved:
>
> | Artifact | Fork | State |
> | --- | --- | --- |
> | `docs/discovery-report.md` | Uniform vs per-layer view configuration | **Resolved.** 2 of the 7 views use a different definition query per layer, so per-layer `update_definition()` after `create_view()` is required, not a contingency. |
> | `docs/spike-master-copy.md` | `copy_feature_layer_collection()` vs publish-from-FGDB | **Open.** The command raised `ValueError: An index of layers or tables must be provided` on every run; fixed, but not yet re-run against the org. |
>
> ### Decisions that changed since this plan was written
>
> - **Runtime is ArcGIS Pro's Python on Windows, not a standalone uv venv.**
>   Auth is `GIS("home")`, borrowing Pro's sign-in — no credentials, and it works
>   with SSO. Requires `arcpy`. Invoked as `python -m agol_provision.cli ...`.
>   The uv/profile/OAuth path in "Phase 1" below was built and then stripped as
>   over-engineered for this environment.
> - **Python floor is 3.11 with no ceiling.** Current ArcGIS Pro ships 3.13.
> - **Discovery grew well beyond the plan's sketch**, because the templates are
>   spread across several groups that also hold real project content, and the
>   master is shared to no group. Selectors union (`--group` repeatable, `--id`,
>   `--ids`, `--query`), `--dry-run` and `--save-ids` support prune-then-run, and
>   truncated searches are a hard error rather than a silent short set.
> - **Discovery now proposes the `groups:` section and all `share_to` lists** by
>   reading each template's real group membership. The manifest rejects a
>   `group.consumes` that disagrees with an item's `share_to`.
> - **Added beyond the plan:** `doctor`, `list-groups`, `list-content`,
>   `preview`, and `safety.py` (guards on the one destructive call).
> - **`copy_feature_layer_collection()` needs an explicit layer and table
>   selection.** It copies a *subset*, and with both arguments left as `None` it
>   raises rather than copying everything. The selection is by positional index
>   into `Item.layers` / `Item.tables`, not by layer id. It also strips each
>   layer's `indexes`, so the spike is expected to report index differences that
>   belong to the method rather than to the template. Wrapped as
>   `copy_whole_service()` and pinned by `tests/test_spike_copy.py`.
> - **Preflight hard-fails on a taken service name.** It will not auto-suffix. A
>   silently renamed service is worse than a stopped run, because the name is
>   reserved org-wide permanently and the project then carries a name nobody
>   chose. (Note `copy_feature_layer_collection()` *does* auto-suffix internally,
>   which is another reason preflight must catch collisions first.)
> - **Cross-project shared services stay out of the manifest.** Some views and
>   feature services are created once and reused by every project. The manifest
>   describes what a project *creates*, and `remap_data()` only rewrites ids
>   present in its mapping, so omitting them is what keeps cloned maps pointing at
>   them. Discovery must still *report* them — see the gap below.
> - **Discovery discards references to items outside the inventory**, which made
>   two Experience Builder apps look dependency-free when only one was. Reporting
>   referenced-but-unknown ids is folded into the stage 0-2 work.
> - **Not built from the Phase 1 file tree below:** `agol_provision/steps/*.py` are
>   still empty and `config/projects/` holds no project files — `--company` and
>   `--location` are passed on the command line instead. Both land with Phase 2.
>
> Read the repo README and `git log` before continuing — commit messages carry the
> reasoning. Everything below is the original plan, still accurate for Phase 2+.


## Context

Standing up a new client project in ArcGIS Online currently means manually creating ~20 interdependent items: a master feature service, 7 views derived from it, 4 groups, 4 web maps, 4 Experience Builder apps, and a dashboard — then wiring sharing between all of them. This takes significant time per project, and manual execution means the "standard" drifts between clients and between the people doing the work.

Templates of every item already exist in ArcGIS Online, fully configured (layers, filters, groups, forms). The goal is a repeatable, version-controlled tool that provisions a complete, correctly-wired project from those templates given only a company name and location.

**Decisions already made** (from planning discussion):
- Single AGOL org for all clients
- Python CLI in a git repo (not an AGOL Notebook)
- Experience Builder apps are AGOL-hosted (`Web Experience` items)
- Every project gets the identical item set for now
- Field Maps offline map areas: manual for v1
- User assignment to groups: manual for v1 (groups still created and shared correctly)

**Answers to the framing questions:**

*Templates on AGOL or SharePoint?* **AGOL is the source of truth.** Web maps, dashboards, and ExB apps have no meaningful off-platform representation — storing them in SharePoint means storing exported JSON you can only use by pushing it back through the same API. `clone_items()` and `remap_data()` read from live AGOL items. The legitimate concern behind the question (drift, no version history) is solved instead by locking templates to a service account in a read-only `_TEMPLATES` group, and by snapshotting every template's JSON into this git repo. That yields real diffs, which is better than SharePoint versioning. SharePoint retains the FGDB, XML workspace doc, and human documentation.

*Local script or AGOL Notebook?* **Local CLI in git.** Version control is the standardization being asked for; a Notebook is an AGOL item with no diffs, no PRs, and no code review. Notebooks also idle-timeout at 20 minutes and consume credits when scheduled. A thin Notebook wrapper that `pip install`s this package can be added later if non-developers need a button.

---

## Core design principle

**The template inventory is data, not code.** A declarative YAML manifest describes the items and their dependency edges. The code is a generic dependency-graph walker with one handler per item type. "Identical every time" is one manifest; future per-client variation is a manifest edit, not a rewrite. The manifest also doubles as living documentation of the dependency graph.

---

## Phase 0 — Discovery + Spike (do this first)

Two throwaway scripts that de-risk everything downstream. Discovery runs first because its output informs the spike.

> **As built:** neither stayed a script. Both became first-class CLI commands — `discover` (backed by `discovery.py`) and `spike-master` (backed by `schema_diff.py` and `safety.py`) — because both are re-run whenever the templates change, not once.

### 0a. Discovery (read-only, no writes)

`scripts/discover.py` — connects to the org, walks every template item, and emits:

1. **`agol_provision/templates/vsclr-standard.yaml`** — manifest skeleton with real item IDs and the derived dependency graph (which map each app consumes, which views each map consumes). This resolves the gap where the Permit Management app, Data Management app, and Project Metrics dashboard have no declared map/view dependency.
2. **`snapshots/<key>.json`** — full item JSON for each template. Committed to git; this is the version-history mechanism.
3. **`docs/discovery-report.md`** — findings: per-view field visibility and `viewDefinitionQuery` (per *layer*, not just service-level), per-view `capabilities`, ExB datasource types, master schema inventory (relationship classes, domains, subtypes, attribute rules, attachments, editor tracking).

Key APIs: `gis.content.get()`, `item.get_data()`, `FeatureLayerCollection.properties`, `layer.properties.viewDefinitionQuery`, and `WebExperience(item).datasources` (`from arcgis.apps.expbuilder import WebExperience`) for ExB datasource inspection.

### 0b. Spike — master copy fidelity

`scripts/spike_master_copy.py` — the one genuinely uncertain thing.

- Run `template_master.copy_feature_layer_collection(service_name="_SPIKE_TEST")`
- Diff the resulting schema against the template: relationship classes, domains, subtypes, indexes, attachment settings, editor tracking, field order
- If relationships are dropped, test publish-from-FGDB as the fallback path and diff again
- Delete the spike service

**Exit criterion:** a documented, proven method for producing an empty schema-identical master. Everything else sits on this.

---

## Phase 1 — Package skeleton

Repo root: `/Users/martinrocha/Development/VSCLR/New AGOL Project Automation`

```
pyproject.toml            # uv-managed, matches Gridiron OS convention; Python 3.11
agol_provision/
  cli.py                  # discover | provision | verify | destroy
  auth.py                 # GIS(profile=...) — creds in OS keyring, never in repo
  naming.py               # company+location -> titles AND sanitized service names
  manifest.py             # load/validate manifest (pydantic)
  state.py                # per-project record of created items; idempotency + rollback
  discovery.py            # promoted from scripts/discover.py
  steps/
    master.py  views.py  groups.py  apps.py  sharing.py  verify.py
  templates/vsclr-standard.yaml
config/projects/          # per-project inputs, e.g. companya-moline.yaml
snapshots/                # git-tracked template JSON
state/                    # per-project manifests of what was created
tests/
```

`naming.py` is small but load-bearing. `"CompanyA Moline"` is a valid item **title** but an invalid **service name** — service names must be sanitized (`CompanyA_Moline_Design`) and are unique org-wide *permanently*, even after deletion. Titles and service names are derived separately from the same inputs.

Auth: ArcGIS Python API profiles (`GIS(profile="vsclr")`), credentials in the OS keyring. No secrets in the repo.

---

## Phase 2 — Provisioning pipeline

Six ordered stages. Each writes to state on completion so a failed run resumes rather than duplicates.

### Stage 0 — Preflight (read-only, fails fast)
Verify privileges, confirm every template item ID resolves, and **check every derived service name for collisions before creating anything**. In a single-org setup this is critical: service names are globally unique forever, and a collision discovered at item 12 leaves debris that must be hand-cleaned.

### Stage 1 — Master
`copy_feature_layer_collection(service_name=..., folder=...)` (or the Phase 0b fallback), then set title, description, tags, thumbnail.

### Stage 2 — Views *(highest-value stage)*
For each of the 7 views, read the template view's real configuration and replay it against the new master:

```python
new_master.manager.create_view(
    name=..., capabilities=..., view_layers=..., view_tables=...,
    visible_fields=..., query=..., preserve_layer_ids=True, folder=...,
)
```

Then per-layer `layer.manager.update_definition({...})` for anything the service-level parameters can't express.

**Discovery has now measured this, and the guess was half right.** Definition queries *do* vary per layer: `redline_qc_view_editable` and `cx_redline_edit_view` filter differently across their layers, so `create_view`'s service-level `query` cannot express them and per-layer `update_definition()` is mandatory for those two. Field visibility, however, is uniform across every one of the seven views — none hides a single field — so `visible_fields` is not needed at all until a template starts hiding some. Build the per-layer query path; do not build a per-layer field path on spec.

Layer *subsetting* is the other per-view axis: the seven views span 2 to 18 layers of the master's 17 layers and 1 table, which `view_layers` / `view_tables` handle at creation. `preserve_layer_ids` defaults to `True` in arcgis 2.4 and must stay that way — maps reference layers by URL with the layer index in it.

**Do not clone views.** Cloning a view within the same org is [documented as unreliable](https://community.esri.com/t5/arcgis-api-for-python-questions/non-buggy-way-to-duplicate-hosted-feature-service/td-p/1355735) — it can produce an empty service or silently re-point at the source. Native creation is deterministic.

### Stage 3 — Groups
`gis.groups.create(...)`, copying settings from the template groups.

### Stage 4 — Maps, then Dashboard and ExB apps
Maps first (apps reference maps). For each:

```python
cloned = gis.content.clone_items(items=[tpl], folder=..., search_existing_items=False)
cloned[0].remap_data({old_id: new_id, ...})
```

Two non-obvious requirements:

- **`search_existing_items=False` is mandatory.** It defaults to `True` and matches on a `source-<item_id>` typeKeyword. Left at default, provisioning CompanyB would silently *reuse CompanyA's* cloned maps and apps. This is the single most damaging footgun in a single-org setup.
- **Use `remap_data()`, not `item_mapping`.** `item_mapping` [does not work with Dashboards](https://developers.arcgis.com/python/latest/guide/cloning-complex-apps/). `remap_data()` (API 2.4+) rewrites IDs throughout an item's entire JSON and works on web maps, dashboards, and Web Experiences alike.

**Experience Builder gets a dedicated path.** ExB apps are cloneable as of API 2.2, but rather than generic JSON rewriting, use the purpose-built class:

```python
from arcgis.apps.expbuilder import WebExperience
exp = WebExperience(cloned_item)
exp.datasources          # inspect what the experience points at
```

`datasources` exposes the experience's data sources and supports remapping them to portal items directly, manually or automatically. Note it reads the experience's **draft** — the draft/published split means a remapped experience must be republished before end users see corrected data sources. Verify both states.

### Stage 5 — Sharing
`item.share(groups=[...])` per the manifest. Group *membership* is deliberately out of scope for v1.

### Stage 6 — Verify
Re-read every created item and assert:
- View layer/table counts match the template
- Per-layer def queries and field visibility applied
- Capabilities correct per view (Redline editable, QC read-only, etc.)
- **No app or map JSON still contains a template item ID** — this is the check that catches silent `remap_data` failures, which are otherwise invisible until a user opens a broken app

Emit a pass/fail report.

---

## Phase 3 — State, rollback, testing

`state/<project>.json` records every created item ID and stage completion.
- Re-running resumes from the last completed stage
- `provision --destroy <project>` deletes items in reverse dependency order, and **only** items recorded in state
- `provision --dry-run` executes Stage 0 and prints the full plan without writing

Tests:
- Unit, no network: `naming.py`, manifest parsing/validation, state transitions, ID-remap map construction
- Integration: full end-to-end into a throwaway project name, then `--destroy`

---

## Files to create

| File | Purpose |
|---|---|
| `scripts/discover.py` | Phase 0a — read-only audit, generates manifest + snapshots |
| `scripts/spike_master_copy.py` | Phase 0b — proves master copy fidelity |
| `agol_provision/templates/vsclr-standard.yaml` | The manifest — central artifact |
| `agol_provision/naming.py` | Title and service-name derivation |
| `agol_provision/steps/views.py` | Highest-value logic; per-layer definition replay |
| `agol_provision/steps/apps.py` | clone + remap; holds both footguns above |
| `agol_provision/state.py` | Idempotency and rollback |

---

## Verification

1. **Phase 0 gate:** discovery report reviewed by a human; spike proves schema fidelity or documents the fallback.
2. **Dry run:** `provision --dry-run --company TestCo --location Sandbox` prints all ~20 planned items with derived names, no writes.
3. **Live end-to-end:** provision into a sandbox name. Manually confirm in AGOL: master empty with correct schema; all 7 views present with correct field visibility, filters, and edit capabilities; 4 groups exist; every map opens with working layers; all 5 ExB apps and the dashboard load without broken data sources.
4. **Rollback:** `provision --destroy TestCo Sandbox` removes everything; org is clean.
5. **Re-runnability:** kill a run mid-Stage-4, re-run, confirm it resumes without creating duplicates.
6. **Second project:** provision a *second* sandbox project. Confirm it does not reuse the first project's items — this specifically exercises the `search_existing_items=False` fix.

---

## Scope note

This is a real project — roughly 1,500–2,500 lines including tests. Phase 0 is 1–2 hours and is worth completing and reviewing before committing to the rest. The views stage (Phase 2, Stage 2) delivers the most time savings and is provable on its own if you want value before the full pipeline lands.


---

## Addendum: authentication (added after the plan was approved)

The plan assumed a stored ArcGIS profile. Two additions came out of setting the
tool up on a second machine:

- **`--profile home`** (the default) borrows whichever portal ArcGIS Pro is signed in to. No
  credentials are stored or typed, and it works with SAML/SSO accounts, which
  cannot accept a password from a script. It reads the token through `arcpy`, so
  it only works inside ArcGIS Pro's own Python environment. Outside a hosted
  notebook the ArcGIS API rewrites `home` to `pro`, so both names work.
- **`setup-profile`** stores a username/password profile, for machines without
  ArcGIS Pro. An OAuth `--client-id` path was added and then removed: on a machine
  with Pro, `home` already solves SSO, and the OAuth flow was a second
  under-exercised code path for the same problem.
- **`doctor`** checks Python and `arcgis` versions, keyring round-trip, the
  connection, and account privileges. The `arcgis >= 2.4` floor is enforced at
  runtime rather than assumed, because a machine using Pro's bundled Python gets
  whichever version Pro shipped.

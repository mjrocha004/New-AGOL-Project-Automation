# AGOL Project Provisioning

Stands up a complete ArcGIS Online project from templates: a master feature
service, its views, groups, maps, dashboards, and Experience Builder apps —
correctly named, wired, and shared.

Given a company and a location, it derives every item name from the standard
convention (`CompanyA Moline`, `CompanyA Moline - Design View`) and builds the
whole dependency graph in order.

Targets Windows with ArcGIS Pro installed.

## Status

Phase 0 is built: `discover` audits the templates, `spike-master` proves whether
the master schema copies faithfully. 430 tests pass, none needing network access.

Phase 0 has now run against the real organization and both questions it existed
to answer are settled:

- **The master copies faithfully.** `spike-master` returned `USABLE WITH FIXUPS`
  with no critical differences — every layer, table, field, field type, domain,
  attachment setting, and editor-tracking setting survived. The only real loss is
  10 user-defined indexes, which the master stage reapplies. No publish-from-FGDB
  fallback is needed.
- **Two of the seven views need per-layer configuration**, because their
  definition queries differ per layer. Field visibility is uniform across all
  seven, so that path is not needed at all.

Provisioning is being built in stage order against
`docs/phase-2-master-and-views.md`. **Stages 0-2 are in** — `provision`
resolves the templates, renders every name, refuses to go on if a service name is
already taken, copies the master, reapplies the indexes the copy drops, and
creates all seven views from it. `--destroy` rolls the whole thing back. Groups,
maps, apps and sharing stay manual.

## Setup

You need ArcGIS Pro installed on the machine and signed in to your organization.
Pro does **not** need to be running — the sign-in token persists. No credentials
are stored or typed anywhere; the tool borrows Pro's connection via `GIS("home")`.

**1. Get the code onto the machine.** Clone it from wherever your team keeps code,
or copy the folder across.

**2. Point VS Code at ArcGIS Pro's Python.** `Ctrl+Shift+P` → *Python: Select
Interpreter* → *Enter interpreter path*:

```
C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe
```

The conda environment *is* the virtual environment. Do not create another one
inside it.

**3. Check what that environment already has.** In VS Code's terminal:

```bat
python -c "import importlib.util as u; print({m: u.find_spec(m) is not None for m in ['arcpy','arcgis','pydantic','keyring','yaml','click','rich']})"
```

Stdlib only, deliberately — `doctor` cannot answer this, because `doctor` is
itself built on `click` and `rich`.

- **All `True`** → skip to step 5. No clone, no install.
- **Any `False`** → step 4.

**4. Only if something was missing.** Pro's default environment is read-only, so
install nothing into it — clone it first. In ArcGIS Pro: **Project → Package
Manager →** the gear icon beside *Active Environment* **→ Clone**. Package Manager
shows the clone's path when it finishes. Point VS Code at that clone's
`python.exe` instead, then install only what was missing:

```bat
pip install click rich pyyaml
```

You do not need to set the clone active in Pro. That setting only controls which
interpreter Pro itself uses; VS Code selects one by path, and the portal sign-in
lives in your Windows user profile either way.

**5. Confirm the machine is ready.**

```bat
python -m agol_provision.cli doctor
```

Checks that `arcpy` is importable, that Pro's `arcgis` is 2.4 or newer, that the
connection works, and that your account holds the privileges provisioning needs.
Exits non-zero on failure.

> If it reports `arcgis` below 2.4, Pro shipped an older API than this needs —
> `remap_data()`, which rewires cloned maps and apps, arrived in 2.4.

## Commands

All read-only except `spike-master`, which creates and deletes one service and
asks first, and `provision`, which creates the master feature service (and,
with `--destroy`, deletes what it recorded creating). `provision --dry-run` is
read-only.

| Command | Purpose |
| --- | --- |
| `doctor` | Check this machine can run the tool. |
| `list-groups` | Groups you belong to, with item counts. |
| `list-content` | Search org content by title, owner, or type. |
| `discover` | Audit the templates; write the manifest, snapshots, and report. |
| `preview` | Show every name a manifest would produce. No connection needed. |
| `spike-master` | Test whether the master schema copies faithfully. |
| `provision` | Provision a project: preflight, master service, and views. |
| `inspect-indexes` | Compare a provisioned master's indexes against its template. |
| `setup-profile` | Store credentials, for a machine without ArcGIS Pro. |

## Phase 0 — run these, then send back the output

### 1. Discover the templates (read-only)

First tell the tool which items are the templates. It cannot infer this — nothing
distinguishes a template from a real project except your intent.

Three selectors, and **they combine**, deduplicated by item id:

| Selector | Notes |
| --- | --- |
| `--group "Title or id"` | Repeatable — pass it as many times as you need. |
| `--id <item id>` | A single item id. Repeatable. No file needed. |
| `--ids file.txt` | A file of item ids, one per line. Must already exist. |
| `--query "title:VSCLR"` | AGOL search syntax. |

Combining matters in practice: the master feature service is often shared to no
group at all, so the real selector is usually *these groups, plus the master* —
which is what `--id` is for.

**Finding what to pass.** If you do not already know:

```bat
python -m agol_provision.cli list-groups
```

```bat
python -m agol_provision.cli list-content --query "owner:TEMPLATE_OWNER"
```

Both print a role breakdown (master / view / map / app) so you can judge whether a
selector is catching the right things.

**Then work down to the real set.** Groups that hold templates usually hold real
project content too, so expect the first pass to be a superset:

```bat
python -m agol_provision.cli discover --group "A" --group "B" --dry-run --save-ids ids.txt
```

`--dry-run` lists what matched, counts the roles, and stops without writing a
manifest. Check it reports **master: 1** — none means something is missing, more
than one means the selector is catching project content.

`--save-ids` writes `ids.txt`: one id per line, sorted by role, each annotated
`# [role   ] Title`, under a header reminding you of the next command. Delete the
lines that are not templates — comments and blanks are ignored, so annotate
freely. Add any item the groups missed by pasting its id on its own line; the
master usually needs this.

Count what you expect to be left with, and note that groups are **not** in this
file — they are created, never cloned, so `discover` derives them from how the
templates are shared. For the current VSCLR set that is 16 lines: 1 master,
7 views, 4 maps, and 4 apps.

Confirm the pruned list, then run for real:

```bat
python -m agol_provision.cli discover --ids ids.txt --dry-run
```

```bat
python -m agol_provision.cli discover --ids ids.txt
```

**What it writes:**

| Path | What it is |
| --- | --- |
| `agol_provision/templates/vsclr-standard.yaml` | The manifest. Review and edit it. |
| `snapshots/*.json` | Per-template JSON. **Commit these** — they are the version history. |
| `docs/discovery-report.md` | Findings to read before building. |

The dependency graph is derived by scanning each item's JSON for the ids of other
template items, so it finds edges regardless of item type — including those buried
in dashboard widget datasources and Experience Builder draft configs.

### Reviewing the generated manifest

Discovery fills in everything it can read. What needs your eyes:

**Titles and service names.** Render them for a sample project rather than
reasoning about the patterns:

```bat
python -m agol_provision.cli preview --company CompanyA --location Moline
```

`{base}` expands to `CompanyA Moline`, `{base_sn}` to `CompanyA_Moline`. Titles
are cosmetic and can be changed later. **Service names cannot** — AGOL reserves a
hosted service name org-wide permanently, even after the service is deleted, so
they get one chance to be right. Discovery derives them from each template's
title, which can produce long ones like `{base_sn}_Contractor_CX_Redline_View`;
shortening to `{base_sn}_Redline` is worth doing now. `preview` reports the
longest name and flags any duplicates.

**Groups and `share_to`.** `share_to` lists the group keys from the manifest's own
`groups:` section — which discovery proposes by reading which groups each template
is actually shared to. The template groups are not a project's groups, but their
shape is right: a view sitting in a contractor template group belongs in that
project's contractor group. So the membership is real; edit the group *titles* to
your project naming pattern.

Views carry `share_to` too, and usually matter more than the maps and apps do —
groups consume views, so an unshared view means a group whose members can see
nothing. `preview` warns about anything with no `share_to`, and the manifest
refuses to load if a group `consumes` an item that is not shared back to it.

**Keys** are internal identifiers used in `consumes` and `share_to` and in run
state. They only need to be unique and readable. Shorten them if you will be
typing them; otherwise leave them.

> **Worth doing once:** put every template in a single AGOL group. The selector
> then becomes `--group "AGOL Templates"` permanently, adding a template is one
> share action rather than a config edit, and the group doubles as the access
> control that stops anyone editing a template by accident. Keep `ids.txt`
> committed as a stand-in until then.

> **TRUNCATED** in any output means more items matched than were retrieved —
> re-run with the `--limit` it names. `discover` treats truncation as a hard error
> rather than a warning: a short template set yields a short manifest, and the
> omission would not surface until an app opened with a missing layer.

### 2. Spike the master copy (creates and deletes one service)

```bat
python -m agol_provision.cli spike-master --master-id <TEMPLATE_MASTER_ID>
```

Answers the one question everything else rests on: does copying the master
preserve its relationship classes, domains, subtypes, and indexes? Creates a
service named `ZZZ_SPIKE_TEST_<prefix>`, diffs its schema against the template,
writes `docs/spike-master-copy.md`, and deletes it. `--keep` leaves it for
inspection.

- **IDENTICAL** — `copy_feature_layer_collection()` is the master strategy.
- **USABLE WITH FIXUPS** — same, plus reapplying the listed settings.
- **NOT USABLE** — critical structure is lost; fall back to publishing from the
  file geodatabase.

> AGOL reserves a hosted service name permanently, even after deletion. The spike
> reuses one fixed name so it only ever burns that one.

**What it touches.** The template is only ever read. The command creates exactly
one new feature service, and its `delete()` targets that newly created item and
nothing else. That is checked rather than assumed:
`safety.py` refuses the delete if the item is the template, lacks the
`ZZZ_SPIKE_TEST_` prefix, or is not the service the copy returned — leaving it in
place instead. A leftover test service is a ten-second cleanup; a wrongly deleted
service is not. Pass `--keep` to skip the delete entirely.

> The codebase has exactly two `delete()` calls — this one and `provision
> --destroy` — and a test fails the build if a third appears anywhere. Each is
> guarded differently: this one by `safety.py`, the other by run state, which can
> only name items the tool recorded creating.

## Phase 2 — provisioning

Built in stage order. **Stages 0 (preflight), 1 (the master feature service) and
2 (its views) are in.** Stages 3–6 (groups, maps, apps, sharing) stay manual for
now — creating four groups by hand takes a minute, where creating seven views
took a day.

```bat
python -m agol_provision.cli provision --company CompanyA --location Moline
```

Runs preflight, creates the master, then creates its seven views. Add
`--dry-run` to stop after preflight without writing anything.

### Preflight

```bat
python -m agol_provision.cli provision --company CompanyA --location Moline --dry-run
```

Prints the full plan — every item, its title, and its service name — and checks
everything that can be checked without writing:

- the account holds the privileges provisioning needs
- every `template_item_id` resolves, is visible, and is the kind of item the
  manifest claims (a view where a view is expected, the master where the master
  is)
- every title and service name renders
- no two items derive the same service name
- **every derived service name is still free in the organization**

That last check is the reason this stage exists. A hosted service name is
reserved org-wide **permanently** — deleting the service does not release it — so
a collision found at stage 1 has already burned the name.

**A taken name stops the run, and nothing is ever renamed automatically.** A
silently suffixed service (`CompanyA_Moline_2`) is worse than a stopped run: the
project carries a name nobody chose and every downstream reference points at it.
Note that `copy_feature_layer_collection()` *does* auto-suffix internally, which
is the second reason the collision has to be caught here rather than at stage 1.

Problems are collected rather than reported one at a time, so a run tells you
about all four collisions at once instead of one per attempt.

If a name is taken, fix it in one of two places. Change that item's
`service_name` in the manifest when a single name collides; pass
`--service-name-override` when the whole stem does, which is the usual case when
re-provisioning a project name that was already used:

```bat
python -m agol_provision.cli provision --company CompanyA --location Moline --service-name-override CompanyA_Moline_B
```

Drop `--dry-run` to record that preflight passed in `state/<slug>.json`. Nothing
is created in ArcGIS Online either way until stage 1 exists. Groups, maps, and
apps appear in the plan marked `not in this build`; their `share_to` entries are
still validated when the manifest loads, just not acted on.

### The master service

Stage 1 copies the template master with `copy_feature_layer_collection()`, which
Phase 0b proved carries every layer, table, field, field type, domain, attachment
setting, and editor-tracking setting intact. Two things it does not do on its
own, which this stage handles:

**The item title.** The copy names the item after the *service*, so without a
follow-up the master's title reads `CompanyA_Moline` rather than `CompanyA
Moline`. Stage 1 sets the title, a description naming the template it came from,
and tags.

**The indexes.** `copy_feature_layer_collection()` strips each layer's `indexes`
before applying the definition, so even a perfect copy arrives without them. The
spike reported 83 missing index entries; 73 are system-generated names that could
never have matched, and 10 are real — `build_status_Index` on 9 layers and
`I25bore_depth` on 1. `build_status_Index` is the one that matters: `build_status`
is the field every view's definition query filters on, so a master without it
makes all seven views table-scan.

Indexes are classified by their **fields**, never by their names. The system
names carry random suffixes and owner ids, and `I25bore_depth` shows the `I##`
prefix appears on user indexes too, so a name proves nothing either way. An index
is treated as system-generated when either:

- its fields are exactly one system field — the object id, the global id, the
  geometry field, or an editor-tracking field; or
- any of its fields is one the layer does not expose in `fields`. AGOL omits the
  geometry field there, so this is what catches a spatial index
  (`user_<id>.<LAYER>_Shape_sidx`) — and an index over a field that is not there
  would be rejected anyway.

A duplicate-index error from AGOL is tolerated rather than fatal. Indexes are
applied one call per layer, but AGOL rejects the whole call if any single index
in it is invalid, so a failed batch is retried one index at a time — otherwise
one bad index loses every good one on that layer.

The new master is **schema only, with no features** — nothing copies data.

A failed index is reported but does not lose the master: the item is recorded in
state before anything else can fail, because an item that exists in AGOL but not
in state is the one failure mode that leaks an orphan. A missing index is a
performance problem, not a correctness one.

**Re-running repairs.** A second `provision` for the same project does not create
a second service — it finds the recorded master and rechecks its indexes,
applying any that are missing. That matters because the service name is burned
the moment the first one is created, so a run that lost its indexes has to be
fixable in place rather than by rolling back and picking a new name.

### Checking the indexes landed

```bat
python -m agol_provision.cli inspect-indexes --slug companya-moline
```

Read-only. Lists every index the tool classifies as user-defined and whether the
copy has it, so the spec's verification step — `build_status_Index` on all nine
redline layers — is a command rather than a click-through in the AGOL UI. Run it
after provisioning, and first whenever an index fails to reapply.

Classification is by fields, so `--layer NAME` prints the fields the decision was
made from: which the layer treats as system, which it exposes at all, every index
on it, and what the copy currently has.

Anything reported `MISSING` is fixed by re-running `provision` for that project —
it repairs in place rather than creating a second service.

### The views

This is the stage that carries the time saving, and the only one that touches
none of the risky machinery — no `clone_items()`, no `remap_data()`, no
Experience Builder republish.

Each view is **created, never cloned**. Cloning a hosted feature layer view
within one organization is unreliable: it can produce an empty service or
silently re-point at the source. So `create_view()` builds it from the new
master, and the template view's real configuration is read **live at provision
time** and replayed — capabilities, which of the master's layers it exposes, and
each layer's definition query. Reading it live rather than storing it in the
manifest means editing a template view in AGOL propagates to the next project
with no manifest edit. Two template views changed capabilities between two
discovery runs; nothing had to be done about it.

Layers are matched to the master **by layer id**, not by position — the master's
17 layers carry ids running past 17, and the seven views each expose a different
subset, from 2 layers to 18. The name is checked against the id rather than
trusted: a view built over the wrong layer shows the wrong data to whoever the
view is shared with.

**`create_view()`'s own `query` argument is never used.** It applies the query to
the first layer of the view only, so a view spanning eighteen layers would be
created with seventeen of them unfiltered — and these views are shared to
subcontractors, so that leaks data rather than merely showing the wrong rows. It
also rewrites that first layer's field visibility as a side effect. Every
definition query is applied per layer instead, for every view, whether or not the
template's queries are uniform. Two of the seven templates filter differently per
layer; the other five happen to be uniform, and are treated identically.

No `visible_fields` handling exists. Field visibility is uniform across all seven
template views and none hides a field, so that path is not built on spec. If a
template starts hiding fields, `discover` reports `Uniform fields: False`.

Views are recorded in state after the master, which is what makes `--destroy`
delete them first — AGOL refuses to delete a feature service while its views
still exist.

### Rolling a project back

```bat
python -m agol_provision.cli provision --destroy companya-moline
```

Deletes every item recorded in `state/<slug>.json`, in **reverse recorded
creation order** — not an assumed order, which matters because AGOL refuses to
delete a feature service while its views still exist. It lists what it will
delete and asks first; `--yes` skips the prompt.

Only recorded items are reachable, so nothing this tool merely found can be
touched. A delete that fails stops the run with the remaining items still
recorded, so re-running resumes from there rather than orphaning them.

## Signing in

The default is `GIS("home")`, which borrows ArcGIS Pro's sign-in. Outside a hosted
ArcGIS Notebook the ArcGIS API rewrites `home` to `pro`, so both mean the same
thing on a desktop; inside a notebook, `home` uses the notebook's identity.

For a machine without ArcGIS Pro, or an unattended run, store a profile instead —
username and org URL in `~/.arcgisprofile`, password in the OS keyring:

```bat
python -m agol_provision.cli setup-profile --name vsclr --org https://YOURORG.maps.arcgis.com --username YOURUSER
python -m agol_provision.cli discover --profile vsclr --group "Templates"
```

SSO accounts cannot sign in that way — a SAML login cannot accept a password from
a script. On those, use the Pro connection.

## Where the templates live

**ArcGIS Online is the source of truth.** Web maps, dashboards, and Experience
Builder apps have no meaningful off-platform representation — a SharePoint copy
would be exported JSON you could only use by pushing it back through this same
API. `clone_items()` and `remap_data()` read live AGOL items.

Version history is `snapshots/` in this repo: `discover` exports each template's
JSON, and committing that gives real, diffable history. Protect the templates
themselves by having a service account own them in a read-only group.

SharePoint keeps the file geodatabase, the XML workspace document, and human
documentation — the artifacts the Pro desktop side actually needs.

## Design

The template inventory is **data, not code**. `agol_provision/templates/*.yaml`
declares what to build and how the pieces reference each other; the code walks
that graph. Dropping the Permit Management app for a smaller client is a config
edit, not a rewrite. See `templates/EXAMPLE.yaml` for the full 21-item shape.

| Module | Responsibility | Used in |
| --- | --- | --- |
| `auth.py` | Connection, version and privilege checks. | Phase 0 |
| `discovery.py` | Read-only template audit. | Phase 0 |
| `schema_diff.py` | Structural comparison of two feature services. | Phase 0 |
| `safety.py` | Guards on the only destructive operation. | Phase 0 |
| `manifest.py` | Manifest schema and cross-reference validation. | Phase 0 → |
| `naming.py` | Titles and service names. Separate namespaces, different rules. | Phase 2 |
| `state.py` | What a run created. Idempotent resume and rollback. | Phase 2 |

`naming.py` and `state.py` are built and tested but nothing in Phase 0 exercises
them — they exist for provisioning.

### Constraints worth knowing

- **`GIS("home")` needs arcpy.** It reads the sign-in token through
  `arcpy.GetSigninToken()`, so it cannot work outside ArcGIS Pro's Python
  environment. The underlying error only says arcpy is missing, so `connect()`
  rewrites it to explain the cause and name the alternative.
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

## Development

The test suite runs anywhere — it uses fakes and needs neither network access nor
arcpy. In ArcGIS Pro's environment:

```bat
pip install pytest
python -m pytest
```

`pyproject.toml` and `uv.lock` support a standalone [uv](https://docs.astral.sh/uv/)
environment used for development on non-Windows machines. You do not need uv to
run this tool.

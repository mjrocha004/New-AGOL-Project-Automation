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
the master schema copies faithfully. 200 tests pass, none needing network access.

Nothing has run against a real ArcGIS Online organization yet. The provisioning
stages come next and are deliberately blocked on what Phase 0 reports.

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

## Phase 0 — run these two, then send back the output

### 1. Discover the templates (read-only)

First tell the tool which items are the templates. It cannot infer this — nothing
distinguishes a template from a real project except your intent.

Three selectors: `--group` (id or title), `--query` (AGOL search), or `--ids`
(explicit list, one item id per line).

`--group` is repeatable, and the union is used with duplicates removed. Template
sets are commonly spread across several groups — one per consuming role — so this
works today without consolidating them first:

```bat
python -m agol_provision.cli discover --group "Contractor Templates" --group "QC Templates" --dry-run
```

**Recommended: keep all templates in one AGOL group.** Then the selector is
`--group "AGOL Templates"` forever, adding a template is one share action, and the
group doubles as the access control that stops anyone editing a template by
accident. If no such group exists yet, these read-only commands help you find or
build the list:

```bat
python -m agol_provision.cli list-groups
python -m agol_provision.cli list-content --query "owner:TEMPLATE_OWNER"
python -m agol_provision.cli list-content --query "owner:TEMPLATE_OWNER" --save-ids ids.txt
```

`--save-ids` writes `<id>  # <title>` per line. Edit that file freely — comments
and blank lines are ignored — then feed it in with `--ids`.

If a command reports **TRUNCATED**, more items matched than were retrieved; re-run
with the `--limit` it suggests. `discover` treats truncation as a hard error rather
than a warning: a short template set would yield a short manifest, and the omission
would not surface until an app opened with a missing layer.

Always confirm the selector before it writes anything:

```bat
python -m agol_provision.cli discover --group "AGOL Templates" --dry-run
```

`--dry-run` lists what matched, counts the roles, warns if it did not find exactly
one master feature service, and stops. Drop the flag when the list looks right:

```bat
python -m agol_provision.cli discover --group "AGOL Templates"
```

Writes three things:

| Path | What it is |
| --- | --- |
| `agol_provision/templates/vsclr-standard.yaml` | The manifest. Review and edit it. |
| `snapshots/*.json` | Per-template JSON. **Commit these** — they are the version history. |
| `docs/discovery-report.md` | Findings to read before building. |

The dependency graph is derived by scanning each item's JSON for the ids of other
template items, so it finds edges regardless of item type — including those buried
in dashboard widget datasources and Experience Builder draft configs.

Afterwards, edit the generated manifest: shorten the suggested keys, check the
title and `service_name` patterns, and fill in `share_to` for each map and app.
Sharing cannot be derived — the templates' own sharing points at template groups,
not the per-project groups this tool creates.

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

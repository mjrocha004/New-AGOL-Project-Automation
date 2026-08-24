# AGOL Project Provisioning

Stands up a complete ArcGIS Online project from templates: a master feature
service, its views, groups, maps, dashboards, and Experience Builder apps —
correctly named, wired, and shared.

Given a company and a location, it derives every item name from the standard
convention (`CompanyA Moline`, `CompanyA Moline - Design View`) and builds the
whole dependency graph in order.

## Status

Phase 0 (discovery and the master-copy spike) and the core modules are built,
with 142 tests passing on macOS. Nothing has yet been run against a real ArcGIS
Online organization, and the Windows and ArcGIS Pro paths are written but
unexercised — `doctor` exists to surface problems there quickly.

The provisioning stages are next, and are deliberately blocked on what Phase 0
reports about the real templates.

## Setup

Requires [uv](https://docs.astral.sh/uv/). On Windows, see
[Setting up on Windows 11](#setting-up-on-windows-11) instead.

```bash
uv sync --group dev
```

Store credentials once. The password goes into the OS keyring via the ArcGIS
API's profile mechanism — never into this repo, shell history, or command output.

```bash
uv run agol-provision setup-profile --name vsclr --org https://YOURORG.maps.arcgis.com --username YOURUSER
```

If your organization uses single sign-on, use `--client-id` instead — SAML
accounts cannot accept a password from a script. Then confirm the machine is
ready:

```bash
uv run agol-provision doctor --profile vsclr
```

## Setting up on Windows 11

Two routes, differing in how you authenticate. **If ArcGIS Pro is on the machine,
take Route A** — it stores no credentials at all and works with single sign-on.

| | Route A: ArcGIS Pro's Python | Route B: standalone (uv) |
| --- | --- | --- |
| Credentials | None. Borrows Pro's sign-in. | Stored profile in Credential Manager. |
| SSO / SAML | Works. | Needs an OAuth client id. |
| `arcgis` version | Whatever Pro ships. | Pinned by `uv.lock`. |
| Needs ArcGIS Pro | Yes. | No. |
| Unattended / scheduled | No — depends on a signed-in Pro. | Yes. |

Either way, first get the code onto the machine: push this repo to wherever your
team keeps code and clone it there, or copy the folder across — but if you copy,
**exclude `.venv`**. A virtual environment is not portable between machines.

---

### Route A — use ArcGIS Pro's Python

`--profile pro` borrows whichever portal ArcGIS Pro is signed in to. Nothing is
typed, prompted for, or stored, and it works with SAML accounts that cannot accept
a password from a script. Pro does not need to be *running* — the sign-in token
persists — but it must be installed and signed in to the right organization.

The catch: this reads the token through `arcpy`, so the commands must run inside
ArcGIS Pro's own Python environment.

**1. Clone Pro's environment.** Pro's default environment (`arcgispro-py3`) is
read-only, so install nothing into it. In ArcGIS Pro: **Project → Package Manager
→** the gear icon beside *Active Environment* **→ Clone**. Name it
`agol-provision`, let it finish, then set it active and restart Pro.

**2. Open Pro's Python prompt.** Start menu → **Python Command Prompt** (installed
with ArcGIS Pro). It opens with the active cloned environment already applied.

**3. Add the few packages Pro does not ship.** `arcgis` already requires `pydantic`
and `keyring`, so only these may be missing — pip skips whatever is already there:

```bat
cd "C:\path\to\New AGOL Project Automation"
pip install click rich pyyaml
```

**4. Check the machine.** No install step is needed — run the module in place:

```bat
python -m agol_provision.cli doctor --profile pro
```

This verifies `arcpy` is importable, that Pro's `arcgis` is 2.4 or newer, that the
connection works, and that the account holds the privileges provisioning needs. It
exits non-zero on failure.

> If it reports `arcgis` below 2.4, Pro shipped an older API than this tool needs
> — `remap_data()`, which rewires cloned maps and apps, was added in 2.4. Either
> update Pro or take Route B.

**5. Run Phase 0**, substituting `python -m agol_provision.cli` for
`agol-provision` in the commands below:

```bat
python -m agol_provision.cli discover --profile pro --group "Templates"
python -m agol_provision.cli spike-master --profile pro --master-id <TEMPLATE_MASTER_ID>
```

---

### Route B — standalone with uv

Independent of ArcGIS Pro, and reproducible from `uv.lock`. Use this if the tool
ever needs to run on a machine without Pro, or unattended.

**1. Install uv** in PowerShell, then reopen PowerShell so it lands on `PATH`. You
do not need to install Python separately — `uv` fetches the right version.

```powershell
winget install --id=astral-sh.uv -e
```

**2. Build the environment.** Quote the path; it has spaces.

```powershell
cd "C:\path\to\New AGOL Project Automation"
uv sync --group dev
```

This downloads the ArcGIS API and its dependencies — a few hundred megabytes, and
slower on a corporate network. If it cannot reach the package index, you are
likely behind a proxy; set `HTTPS_PROXY` and retry.

**3. Sign in.** Which command depends on how your organization authenticates.

Built-in ArcGIS account — you type a username and password directly into AGOL:

```powershell
uv run agol-provision setup-profile --name vsclr --org https://YOURORG.maps.arcgis.com --username YOURUSER
```

SSO / enterprise login — signing in sends you to your company's login page:

```powershell
uv run agol-provision setup-profile --name vsclr --org https://YOURORG.maps.arcgis.com --client-id YOUR_CLIENT_ID
```

SAML accounts cannot accept a password from a script, so the username path will
fail for them. The OAuth path opens a browser, you sign in normally, and paste the
resulting code back. Register an OAuth app in AGOL to get a client id: **Content →
New item → Application → Other application**, then copy its Client ID.

The password or token goes into Windows Credential Manager via `keyring`, never
into this repo.

**4. Check the machine**, then run Phase 0 with `--profile vsclr`:

```powershell
uv run agol-provision doctor --profile vsclr
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

`--profile` takes either a stored profile name or `pro`, which borrows ArcGIS
Pro's signed-in connection instead of storing anything. See
[Route A](#route-a--use-arcgis-pros-python).

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
| `auth.py` | Connection (stored profile or ArcGIS Pro), version and privilege checks. |

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
- **`--profile pro` requires ArcGIS Pro's Python.** It reads the sign-in token
  through `arcpy`, so it cannot work from a standalone virtual environment. The
  underlying error only says arcpy is missing, so `connect()` rewrites it to
  explain the cause and name the alternative.
- **Experience Builder reads its draft.** A remapped experience must be
  republished before end users see corrected data sources.

## Tests

```bash
uv run pytest
```

142 tests, no network access required — naming, manifest validation, state
transitions, dependency scanning, and schema comparison all run against fakes.

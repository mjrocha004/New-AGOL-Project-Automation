# Phase 2, Stages 0-2 — preflight, master, views

Implements the first three stages of the pipeline in `implementation-plan.md`.
Stages 3-6 (groups, maps, apps, sharing) are deliberately excluded; see Scope.

Status: specified, not built. Phase 0 is complete and both design forks are
closed.

## Why this slice

Creating the four groups by hand takes about a minute. Creating the seven views
by hand takes about a day. Stage 2 therefore carries nearly all of the time
saving, and it is the only stage that touches none of the risky machinery --
no `clone_items()`, no `remap_data()`, no Experience Builder republish. It can
be run, verified, and trusted before any of that exists.

## What Phase 0 settled

**The master copies faithfully.** `spike-master` against the real org returned
`USABLE WITH FIXUPS` with **no critical differences**. All 17 layers, 1 table,
every field, every field type, all 47 domains, attachments, and editor tracking
survived `copy_feature_layer_collection()` intact. There is no FGDB fallback to
build.

**The only losses are indexes, and most of that is measurement noise.** The
report listed 83 missing index entries across 18 layers. Classified:

| Count | Kind | Real loss? |
| --- | --- | --- |
| 18 | `PK__ZAYO_CHI__…` SQL Server primary key | No -- random suffix, never name-matchable |
| 17 | `user_57996.…_Shape_sidx` spatial index | No -- name is scoped to the owning account |
| 20 | `I##CreationDate` / `Creator` / `EditDate` / `Editor` | No -- editor-tracking, recreated with new numbering |
| 18 | `FDO_GlobalID` / `GlobalID_Index` | No -- recreated with the GlobalID field |
| **10** | **`build_status_Index` (9 layers), `I25bore_depth` (1)** | **Yes** |

`schema_diff` compares indexes by name, so AGOL recreating an equivalent index
under a different generated name reads as "missing". The ten user-defined ones
are the real finding, and `build_status_Index` is the one that matters:
`build_status` is the field that **every** view's definition query filters on. A
master without it makes all seven views table-scan.

**Views need a per-layer query path and no per-layer field path.** Two of seven
views (`redline_qc_view_editable`, `cx_redline_edit_view`) use different
definition queries per layer, so `create_view()`'s service-level `query` cannot
express them. Field visibility is uniform across all seven and no view hides a
single field, so `visible_fields` is not needed at all.

## Scope

In:

- `provision --company X --location Y` running stages 0, 1, 2 and stopping.
- Preflight, master creation with index repair, view creation with per-layer
  configuration replay.
- State recording and resume for those three stages.
- `provision --destroy` for what those three stages created.

Out, and manual for now:

- Groups, sharing, maps, apps, verify (stages 3-6).
- Group *membership* -- already out of scope for v1 in the plan.
- `share_to` stays in the manifest, validated but unused, until stage 5 exists.

## Stage 0 -- Preflight

Read-only. Everything that can fail must fail here, because a failure after
stage 1 leaves a permanently reserved service name behind.

1. Load and validate the manifest (`manifest.py` already does cross-reference
   validation at load time).
2. Confirm the account holds the create/share privileges (`auth.check_privileges`).
3. Resolve every `template_item_id` and confirm each is visible and of the
   expected type.
4. Render every title and service name through `naming.py`.
5. **Check every derived service name against `gis.content.is_service_name_available()`.**

**A taken service name is a hard failure.** Preflight does not auto-suffix and
does not offer to. AGOL reserves a hosted service name org-wide permanently, so
a silent rename leaves the project carrying a name nobody chose, and every
downstream reference pointing at it. Stopping is recoverable; a wrong permanent
name is not.

Note that `copy_feature_layer_collection()` *does* auto-suffix internally
(`name`, `name_2`, `name_3`…) when a name is taken. That is a second reason
preflight must catch collisions first: by the time the copy runs, the rename has
already happened silently.

Preflight prints the full plan -- every item, title, and service name -- and with
`--dry-run` stops there without writing.

## Stage 1 -- Master

1. `copy_whole_service(template_master, service_name)` -- the wrapper already in
   `cli.py`, which passes explicit positional layer and table indexes.
2. Set the item title to the rendered `{base}`, plus description and tags. The
   copy names the item after the *service*, so this is not cosmetic: without it
   the master's item title is `CompanyA_Moline` rather than `CompanyA Moline`.
3. **Reapply user-defined indexes.** For each layer, read the template's
   `indexes`, drop the system-generated ones, and apply the rest via
   `layer.manager.add_to_definition({"indexes": [...]})`.

   An index is treated as system-generated when its fields are exactly one
   system field -- `objectid`, `globalid`, the shape field, or an editor-tracking
   field. Classifying by *fields* rather than by name is deliberate: the names
   carry random suffixes and owner ids, and `I25bore_depth` shows that the `I##`
   prefix appears on user indexes too. Duplicate-index errors from AGOL are
   tolerated rather than fatal.
4. Record the master in state.

The new master is **schema only, with no features**. `copy_feature_layer_collection()`
calls `create_service` then `add_to_definition`; nothing copies data.

## Stage 2 -- Views

For each view in the manifest, in order:

1. Read the **template** view live -- capabilities, layer subset, and per-layer
   definition queries. Reading at provision time rather than storing this in the
   manifest means editing a template view in AGOL propagates to the next project
   with no manifest edit. That is the README's stated intent.
2. Create it from the **new** master:

   ```python
   new_master_flc.manager.create_view(
       name=service_name,
       capabilities=template_capabilities,
       view_layers=[...], view_tables=[...],
       preserve_layer_ids=True,
       query=uniform_query_or_None,
       folder=...,
   )
   ```

   `preserve_layer_ids` defaults to `True` in arcgis 2.4 and must stay that way:
   maps reference layers by URL with the layer index in it. `view_layers` takes
   layer *objects* (`flc.layers[0]`), which is how the seven views span 2 to 18
   of the master's layers.
3. Where the template's queries are **not** uniform across layers, apply each
   layer's own query with `layer.manager.update_definition({"viewDefinitionQuery": ...})`.
   Required for `redline_qc_view_editable` and `cx_redline_edit_view`.
4. Set the item title to the rendered pattern, then record in state.

Views are **created, never cloned**. Cloning a view within one org can produce an
empty service or silently re-point at the source.

No `visible_fields` handling is built. If a template later hides a field,
discovery reports `Uniform fields: False` and this spec gets revisited -- that is
what the report's column is for.

## State, resume, rollback

`state.py` is already built and tested. Each stage calls `complete_stage()`;
each created item is recorded by manifest key as it is made, so a crash between
the AGOL call and the record is the only gap.

Resume skips completed stages. `--destroy` walks `destroy_order()` -- reverse
recorded creation order, not an assumed one -- which matters because AGOL refuses
to delete a feature service while its views still exist.

## CLI surface

```
provision --company X --location Y [--manifest PATH] [--dry-run] [--profile P]
provision --destroy <slug> [--profile P]
```

`--dry-run` runs stage 0 and prints the plan. `test_no_dead_options.py` fails any
declared-but-unread option, and `test_readme_accuracy.py` checks every README
invocation against the real CLI, so the README changes in the same commit.

## Testing

Unit, no network, following the fakes in `tests/test_discovery.py`:

- Preflight rejects a taken service name, and does not rename.
- Preflight rejects a manifest whose template ids do not resolve.
- Index classification: `build_status_Index` and `I25bore_depth` are kept;
  `PK__…`, `…_sidx`, `FDO_GlobalID`, and `I##Creator` are dropped.
- View creation passes the template's capabilities and layer subset through.
- A non-uniform view gets per-layer `update_definition()` calls; a uniform one
  gets none.
- State records each item as created and resumes from the last complete stage.
- Rollback order is the reverse of recorded creation order.

Live verification, run by hand on a sandbox project:

1. Master exists, empty, 17 layers and 1 table, domains and attachments intact.
2. `build_status_Index` present on all 9 redline layers.
3. All 7 views present with the right layer counts (2, 5, 7, 7, 9, 18, 18).
4. The two non-uniform views filter differently per layer, matching the template.
5. Capabilities match per view.
6. `--destroy` leaves the org clean.
7. A second sandbox project provisions without reusing the first's items.

## Follow-ups, not blocking

- **Report referenced-but-unknown item ids in discovery.** `find_dependencies()`
  filters to the known inventory, so an app referencing an item outside `ids.txt`
  reads as standing alone. Small, and it removes a class of silent wrong answers.
- **Classify indexes in `schema_diff`.** Reporting 10 real differences instead of
  83 noisy ones makes the next spike readable.
- **Topologically sort `Manifest.cloned_items`.** Needed before stage 4, not
  before this.

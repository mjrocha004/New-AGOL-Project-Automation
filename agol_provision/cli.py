"""Command-line entry point.

Phase 0 commands (`discover`, `spike-master`) come first: they establish what the
templates actually contain and whether the master schema copies faithfully. The
provisioning commands are built on top of what those two report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from agol_provision import __version__

console = Console()
err = Console(stderr=True, style="bold red")

REPO_ROOT = Path(__file__).resolve().parent.parent
# Per-project run state. Git-ignored: it holds live AGOL item ids for real client
# projects, and rollback needs it while version control does not.
STATE_DIR = REPO_ROOT / "state"


def _fail(message: str) -> None:
    err.print(message)
    sys.exit(1)


def copy_whole_service(template: Any, service_name: str) -> Any:
    """Copy every layer and table of a feature service into a new one.

    `copy_feature_layer_collection()` selects a *subset*, and its defaults are not
    "everything" -- with both `layers` and `tables` left as None it raises. The
    values it selects with are positional indexes into `Item.layers` /
    `Item.tables` (it evaluates `self.layers[idx]`), not layer ids, so a master
    whose layer ids start at 11 needs 0..n-1 here.

    Note that the copy drops each layer's `indexes` before applying the
    definition, so the spike is expected to report index differences. That is a
    property of this method, not of the template.
    """
    return template.copy_feature_layer_collection(
        service_name=service_name,
        layers=list(range(len(template.layers))),
        tables=list(range(len(template.tables))),
    )


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Provision a complete ArcGIS Online project from templates."""


# ---------------------------------------------------------------- setup


@main.command("setup-profile")
@click.option("--name", required=True, help="Local profile name, e.g. vsclr.")
@click.option("--org", required=True, help="https://yourorg.maps.arcgis.com")
@click.option("--username", required=True, help="AGOL username.")
def setup_profile(name: str, org: str, username: str) -> None:
    """Store credentials for a machine without ArcGIS Pro.

    Not needed for normal use: the default connection borrows ArcGIS Pro's
    sign-in and stores nothing. This exists for a machine without Pro, or an
    unattended run where no one is signed in.

    The password is prompted for, handed to the ArcGIS API, and stored in the OS
    keyring -- never in this repository, shell history, or command output. Note
    that SSO accounts cannot sign in this way; on those, use the Pro connection.
    """
    from arcgis.gis import GIS

    password = click.prompt("AGOL password", hide_input=True)
    try:
        gis = GIS(url=org, username=username, password=password, profile=name)
    except Exception as exc:
        _fail(
            f"Could not sign in: {exc}\n\n"
            "If your organization uses single sign-on, a username and password will "
            "not work. Run from ArcGIS Pro's Python environment instead, where the "
            "default connection borrows Pro's existing sign-in."
        )

    console.print(f"[green]Saved profile[/green] {name!r} for {gis.users.me.username}.")
    console.print(f"Use it with: [cyan]--profile {name}[/cyan]")


# ---------------------------------------------------------------- environment


@main.command()
@click.option("--profile", default="home", show_default=True,
              help="'home' borrows ArcGIS Pro's sign-in. Or a stored profile name.")
def doctor(profile: str) -> None:
    """Check that this machine can run the tool.

    Verifies the Python and ArcGIS API versions, that the OS keyring actually
    stores secrets, and -- with --profile -- that the profile connects and the
    account holds the privileges provisioning needs. Intended for a freshly set-up
    machine, where the failures are environmental rather than in the code.
    """
    import platform
    import sys

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        """A requirement. Failing one means this machine cannot run the tool."""
        nonlocal ok
        mark = "[green]PASS[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"  {mark}  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
        ok = ok and passed

    def note(label: str, available: bool, detail: str = "") -> None:
        """An option, not a requirement. Two sign-in paths exist and either is
        sufficient, so an unavailable one is information rather than a failure."""
        mark = "[green] yes[/green]" if available else "[dim]  no[/dim]"
        console.print(f"  {mark}  {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))

    console.print(f"[bold]Platform[/bold]  {platform.system()} {platform.release()} "
                  f"({platform.machine()})")
    console.print()

    console.print("[bold]Install[/bold]")
    from agol_provision.auth import MIN_PYTHON_VERSION, check_python_version

    py = sys.version_info
    min_py = ".".join(str(n) for n in MIN_PYTHON_VERSION)
    check(f"Python {min_py} or newer", check_python_version(strict=False) is None,
          f"{py.major}.{py.minor}.{py.micro}")

    try:
        import arcgis

        from agol_provision.auth import MIN_ARCGIS_VERSION, check_arcgis_version

        problem = check_arcgis_version(strict=False)
        want = ".".join(str(n) for n in MIN_ARCGIS_VERSION)
        check(f"arcgis >= {want}", problem is None, arcgis.__version__)
        if problem:
            console.print(f"        [yellow]{problem}[/yellow]")
    except ImportError as exc:
        check("arcgis importable", False, str(exc))

    from agol_provision.auth import arcpy_available, uses_arcgis_pro

    pro_mode = uses_arcgis_pro(profile)
    has_arcpy = arcpy_available()

    console.print()
    console.print("[bold]Sign-in options available here[/bold]")
    if pro_mode:
        # The default connection, so arcpy is a requirement rather than an option.
        check(f"arcpy importable (needed by --profile {profile})", has_arcpy,
              "present" if has_arcpy else "not importable")
        if not has_arcpy:
            console.print()
            console.print("[bold red]Not ready.[/bold red] This is not ArcGIS Pro's "
                          "Python environment.")
            console.print("Point VS Code at Pro's interpreter (or a clone of it), or "
                          "use a stored profile with --profile NAME.")
            # Attempting the connection would fail with the same cause, reported twice.
            sys.exit(1)
    else:
        note("ArcGIS Pro connection available", has_arcpy,
             "arcpy present" if has_arcpy else "needs ArcGIS Pro's Python environment")

    # A stored profile keeps its password in the OS keyring. The Pro connection
    # stores nothing, so this only matters when a stored profile is in play.
    if not pro_mode:
        try:
            import keyring

            backend = type(keyring.get_keyring()).__name__
            # A backend can be present and still not persist anything -- a no-op
            # backend registers itself when no real one is available. Round-tripping
            # a dummy value is the only way to know it works.
            keyring.set_password("agol-provision-doctor", "test", "ok")
            stored = keyring.get_password("agol-provision-doctor", "test")
            check("keyring stores and returns a secret", stored == "ok", backend)
            try:
                keyring.delete_password("agol-provision-doctor", "test")
            except Exception:
                pass
        except Exception as exc:
            check("keyring usable", False, str(exc))
            console.print("        [yellow]Without a keyring the ArcGIS API cannot store "
                          "a profile password securely. --profile pro avoids the "
                          "question entirely.[/yellow]")

    console.print()
    label = "ArcGIS Pro connection" if pro_mode else f"Profile {profile}"
    console.print(f"[bold]{label}[/bold]")
    from agol_provision.auth import AuthError, check_privileges, connect, describe

    try:
        gis = connect(profile)
        check("connects to ArcGIS Online", True, describe(gis))
    except AuthError as exc:
        check("connects to ArcGIS Online", False, str(exc).splitlines()[0])
        sys.exit(1)

    missing = check_privileges(gis, strict=False)
    check("account can create items, groups, views, and share", not missing,
          f"missing: {', '.join(missing)}" if missing else "all present")

    console.print()
    if ok:
        console.print("[bold green]Ready.[/bold green] Next: "
                      "python -m agol_provision.cli discover --group \"Templates\"")
    else:
        console.print("[bold red]Not ready.[/bold red] Fix the failures above first.")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- finding things


@main.command("list-groups")
@click.option("--profile", default="home", show_default=True,
              help="'home' borrows ArcGIS Pro's sign-in. Or a stored profile name.")
@click.option("--search", help="Filter by title substring.")
def list_groups(profile: str, search: str | None) -> None:
    """List the groups this account belongs to, with item counts.

    Use it to find the value for `discover --group`. Read-only.
    """
    from agol_provision.auth import AuthError, connect, describe

    try:
        gis = connect(profile)
    except AuthError as exc:
        _fail(str(exc))

    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]\n")
    groups = list(gis.users.me.groups)
    if search:
        needle = search.lower()
        groups = [g for g in groups if needle in (g.title or "").lower()]

    if not groups:
        _fail("No groups matched. Try `list-content` to search by title instead.")

    table = Table(title=f"Groups ({len(groups)})")
    for col in ("Title", "Items", "Id"):
        table.add_column(col, overflow="fold")
    for g in sorted(groups, key=lambda g: (g.title or "").lower()):
        try:
            count = str(len(g.content()))
        except Exception:
            count = "?"
        table.add_row(g.title, count, g.groupid)
    console.print(table)
    console.print("\n[dim]Then: python -m agol_provision.cli discover "
                  "--group \"<Title>\" --dry-run[/dim]")


@main.command("list-content")
@click.option("--profile", default="home", show_default=True,
              help="'home' borrows ArcGIS Pro's sign-in. Or a stored profile name.")
@click.option("--query", required=True,
              help='AGOL search, e.g. "title:VSCLR Template" or "owner:jsmith".')
@click.option("--limit", default=200, show_default=True,
              help="Maximum items to retrieve.")
@click.option("--save-ids", type=click.Path(), default=None,
              help="Write the matched item ids to a file for `discover --ids`.")
def list_content(profile: str, query: str, limit: int, save_ids: str | None) -> None:
    """Search org content by title, owner, or type. Read-only.

    Use it to work out a `discover --query`, or with --save-ids to capture an
    explicit item list when no single search or group covers all the templates.
    """
    from agol_provision.auth import AuthError, connect, describe
    from agol_provision.discovery import classify, count_matches

    try:
        gis = connect(profile)
    except AuthError as exc:
        _fail(str(exc))

    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]\n")
    items = list(gis.content.search(query, max_items=limit))
    if not items:
        _fail(f"Nothing matched {query!r}.")

    # Showing the first N of M without saying so reads as "these are all your
    # matches", which is how an incomplete template list gets built.
    total = count_matches(gis, query)
    truncated = (total is not None and total > len(items)) or (
        total is None and len(items) >= limit
    )

    table = Table(title=f"Matches ({len(items)})")
    for col in ("Title", "Type", "Role", "Owner", "Id"):
        table.add_column(col, overflow="fold")
    for it in items:
        table.add_row(it.title, it.type, classify(it), it.owner, it.itemid)
    console.print(table)

    roles = [classify(it) for it in items]
    console.print(f"\nmaster: {roles.count('master')}  views: {roles.count('view')}  "
                  f"maps: {roles.count('map')}  apps: {roles.count('app')}  "
                  f"unknown: {roles.count('unknown')}")

    if truncated:
        shortfall = f"{total} match" if total is not None else "more"
        console.print()
        err.print(
            f"TRUNCATED: showing {len(items)} items, but {shortfall}. "
            f"Re-run with --limit {(total + 10) if total is not None else limit * 4}."
        )
        if save_ids:
            _fail("Refusing to write a partial id list.")

    if save_ids:
        from agol_provision.discovery import write_id_file

        write_id_file(items, Path(save_ids))
        console.print(f"\n[green]Wrote {len(items)} id(s)[/green] to {save_ids}")
        console.print("[dim]Edit the file to add or remove items, then:\n"
                      f"  python -m agol_provision.cli discover --ids {save_ids} "
                      f"--dry-run[/dim]")


# ---------------------------------------------------------------- phase 0a


@main.command()
@click.option("--profile", default="home", show_default=True,
              help="'home' borrows ArcGIS Pro's sign-in. Or a stored profile name.")
@click.option("--group", multiple=True,
              help="Template group id or title. Repeatable; the union is used.")
@click.option("--ids", "ids_file", type=click.Path(exists=True),
              help="File of item ids, one per line. Combines with --group.")
@click.option("--id", "extra_ids", multiple=True, metavar="ITEM_ID",
              help="A single item id. Repeatable. For adding the master, which is "
                   "often shared to no group.")
@click.option("--query", help="AGOL search query, e.g. 'title:VSCLR Template'.")
@click.option("--name", "manifest_name", default="vsclr-standard",
              help="Name for the generated manifest.")
@click.option("--out", type=click.Path(), default=None,
              help="Manifest output path. Defaults to the templates directory.")
@click.option("--limit", default=None, type=int,
              help="Maximum items to retrieve. Raise it if a run reports truncation.")
@click.option("--dry-run", is_flag=True,
              help="Show which items matched, then stop. Writes no manifest.")
@click.option("--save-ids", type=click.Path(), default=None,
              help="Write the matched ids to a reviewable file for pruning.")
def discover(profile, group, ids_file, extra_ids, query, manifest_name, out, limit,
             dry_run, save_ids) -> None:
    """Audit the template items and generate a manifest, snapshots, and a report.

    Read-only: this command never writes to ArcGIS Online.
    """
    import yaml

    from agol_provision import discovery
    from agol_provision.auth import AuthError, check_privileges, connect, describe

    try:
        gis = connect(profile)
    except AuthError as exc:
        _fail(str(exc))

    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]")
    missing = check_privileges(gis, strict=False)
    if missing:
        console.print(f"[yellow]Note:[/yellow] account cannot {', '.join(missing)}. "
                      f"Discovery is read-only, but provisioning will need these.")

    # --ids reads a file, --id takes ids directly; both feed the same union, so
    # adding one stray item needs no file.
    item_ids: list[str] = list(extra_ids)
    if ids_file:
        item_ids += [ln.strip() for ln in Path(ids_file).read_text().splitlines() if ln.strip()]

    try:
        kwargs = {"limit": limit} if limit else {}
        items = discovery.collect(
            gis, group=group, item_ids=item_ids or None, query=query, **kwargs
        )
    except discovery.TruncatedError as exc:
        # Not a warning: a short template set yields a short manifest, and the
        # omission would not surface until an app opened with a missing layer.
        _fail(f"{exc}\n\nRefusing to build a manifest from an incomplete set.")
    except ValueError as exc:
        _fail(str(exc))

    if not items:
        _fail("No template items found. Widen the search, or check sharing on the templates.")

    if dry_run:
        # Confirming the selector is cheap; inspecting every item is not. Stop here
        # so the selector can be tuned without reading 21 items' worth of JSON.
        title = f"Would inspect {len(items)} item(s)"
        if len(group) > 1:
            title += f" (union of {len(group)} groups)"
        table = Table(title=title)
        for col in ("Title", "Type", "Role", "Id"):
            table.add_column(col, overflow="fold")
        for it in items:
            table.add_row(it.title, it.type, discovery.classify(it), it.itemid)
        console.print(table)

        roles = [discovery.classify(i) for i in items]
        console.print(f"\nmaster: {roles.count('master')}  views: {roles.count('view')}  "
                      f"maps: {roles.count('map')}  apps: {roles.count('app')}  "
                      f"unknown: {roles.count('unknown')}")
        masters = roles.count("master")
        if masters == 0:
            console.print(
                "\n[yellow]No master feature service in this set.[/yellow] The master "
                "is often shared to no group at all, so a group-based selector misses "
                "it. Selectors combine -- add its id alongside the groups:\n"
                "  [cyan]--id <32-character item id>[/cyan]"
            )
        elif masters > 1:
            console.print(
                f"\n[yellow]{masters} candidate master feature services.[/yellow] "
                "Expected exactly one -- a Feature Service without the 'View Service' "
                "type keyword. The selector is probably catching real project content."
            )

        if save_ids:
            written = discovery.write_id_file(items, Path(save_ids))
            console.print(f"\n[green]Wrote {written} id(s)[/green] to {save_ids}")
            console.print("[dim]Delete the lines that are not templates, then:\n"
                          f"  python -m agol_provision.cli discover --ids {save_ids} "
                          f"--dry-run[/dim]")
        else:
            console.print("\n[dim]Looks right? Re-run without --dry-run. "
                          "Too many items? Add --save-ids ids.txt to prune.[/dim]")
        return

    if len(group) > 1:
        console.print(f"Union of {len(group)} groups: {len(items)} distinct item(s).")

    console.print(f"Inspecting {len(items)} item(s)...")
    inspected = discovery.inspect_all(gis, items)

    table = Table(title="Discovered templates")
    for col in ("Key", "Title", "Type", "Role", "Depends on"):
        table.add_column(col, overflow="fold")
    by_id = {t.item_id: t for t in inspected}
    for t in inspected:
        deps = ", ".join(by_id[d].key for d in t.depends_on if d in by_id) or "-"
        table.add_row(t.key, t.title, t.item.type, t.role, deps)
    console.print(table)

    try:
        manifest = discovery.build_manifest_dict(gis, inspected, manifest_name)
    except ValueError as exc:
        _fail(str(exc))

    manifest_path = Path(out) if out else (
        REPO_ROOT / "agol_provision" / "templates" / f"{manifest_name}.yaml"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "# Generated by `agol-provision discover`. Review before use:\n"
        "#   - shorten the suggested keys\n"
        "#   - check the title and service_name patterns\n"
        "#   - fill in `share_to` for each map and app (it cannot be derived)\n\n"
        + yaml.safe_dump(manifest, sort_keys=False, width=100)
    )

    snap_dir = REPO_ROOT / "snapshots"
    count = discovery.write_snapshots(inspected, snap_dir)
    report_path = REPO_ROOT / "docs" / "discovery-report.md"
    discovery.write_report(gis, inspected, report_path)

    console.print()
    console.print(f"[green]Manifest[/green]  {manifest_path.relative_to(REPO_ROOT)}")
    console.print(f"[green]Snapshots[/green] {count} files in snapshots/")
    console.print(f"[green]Report[/green]    {report_path.relative_to(REPO_ROOT)}")

    warned = [t for t in inspected if t.warnings]
    if warned:
        console.print(f"\n[yellow]{len(warned)} item(s) raised warnings.[/yellow] "
                      f"See the report before building.")


# ---------------------------------------------------------------- reviewing


@main.command()
@click.option("--manifest", "manifest_path", type=click.Path(exists=True), default=None,
              help="Manifest to render. Defaults to the generated vsclr-standard.yaml.")
@click.option("--company", default="CompanyA", show_default=True,
              help="Sample company name to render with.")
@click.option("--location", default="Moline", show_default=True,
              help="Sample location to render with.")
def preview(manifest_path: str | None, company: str, location: str) -> None:
    """Show every name a manifest would produce, for a sample project.

    Nothing is created and no connection is made -- this is pure local
    computation. Use it to check the title and service_name patterns before they
    become permanent: AGOL reserves a hosted service name org-wide forever, so a
    name only gets one chance to be right.
    """
    from agol_provision.manifest import ManifestError, load_manifest
    from agol_provision.naming import NamingError, NameContext

    path = Path(manifest_path) if manifest_path else (
        REPO_ROOT / "agol_provision" / "templates" / "vsclr-standard.yaml"
    )
    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        _fail(str(exc))

    ctx = NameContext(company=company, location=location)
    console.print(f"Manifest [cyan]{manifest.name}[/cyan] v{manifest.version} "
                  f"-- {manifest.total_items()} items")
    console.print(f"Rendered for [cyan]{company} / {location}[/cyan]\n")

    table = Table(title="Item titles and service names")
    for col in ("Kind", "Key", "Title", "Service name"):
        table.add_column(col, overflow="fold")

    def row(kind: str, key: str, title_pat: str, sn_pat: str | None) -> None:
        try:
            title = ctx.render_title(title_pat)
        except (NamingError, KeyError, IndexError) as exc:
            title = f"[red]{exc}[/red]"
        if sn_pat is None:
            service = "[dim]n/a[/dim]"
        else:
            try:
                service = ctx.render_service_name(sn_pat)
            except (NamingError, KeyError, IndexError) as exc:
                service = f"[red]{exc}[/red]"
        table.add_row(kind, key, title, service)

    row("master", manifest.master.key, manifest.master.title, manifest.master.service_name)
    for v in manifest.views:
        row("view", v.key, v.title, v.service_name)
    for g in manifest.groups:
        row("group", g.key, g.title, None)
    for i in manifest.cloned_items:
        row("map" if i in manifest.maps else "app", i.key, i.title, None)
    console.print(table)

    # Service names are the ones worth scrutinising: titles can be renamed later,
    # a hosted service name is reserved org-wide permanently.
    names = [ctx.render_service_name(manifest.master.service_name)]
    names += [ctx.render_service_name(v.service_name) for v in manifest.views]
    dupes = {n for n in names if names.count(n) > 1}
    console.print()
    if dupes:
        err.print(f"Duplicate service names: {', '.join(sorted(dupes))}. "
                  f"Each must be unique -- the run would fail partway through.")
    longest = max(names, key=len)
    console.print(f"[dim]{len(names)} service names, longest {len(longest)} chars "
                  f"({longest}).[/dim]")
    console.print("[dim]Service names are permanent org-wide, even after deletion. "
                  "Titles can be changed later; these cannot.[/dim]")

    # Views are what groups consume, so an unshared view is the more consequential
    # omission -- the group exists and its members see nothing.
    ungrouped = [v.key for v in manifest.views if not v.share_to]
    ungrouped += [i.key for i in manifest.cloned_items if not i.share_to]
    if ungrouped:
        console.print(f"\n[yellow]No share_to set for: {', '.join(ungrouped)}[/yellow] "
                      f"-- these would be created but shared to nothing.")


# ---------------------------------------------------------------- phase 0b


@main.command("spike-master")
@click.option("--profile", default="home", show_default=True,
              help="'home' borrows ArcGIS Pro's sign-in. Or a stored profile name.")
@click.option("--master-id", required=True, help="Template master feature service item id.")
@click.option("--keep", is_flag=True, help="Leave the test service in place for inspection.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def spike_master(profile, master_id, keep, yes) -> None:
    """Test whether the master template copies with its schema intact.

    Creates one temporary feature service, compares its schema against the
    template, and deletes it again. This is the only Phase 0 command that writes
    to ArcGIS Online.
    """
    from arcgis.features import FeatureLayerCollection

    from agol_provision.auth import AuthError, connect, describe
    from agol_provision.safety import refuse_delete_reason, spike_service_name
    from agol_provision.schema_diff import diff_fingerprints, fingerprint_service, summarize

    try:
        gis = connect(profile)
    except AuthError as exc:
        _fail(str(exc))

    template = gis.content.get(master_id)
    if template is None:
        _fail(f"No item with id {master_id!r} is visible to this account.")
    if template.type != "Feature Service":
        _fail(f"{template.title!r} is a {template.type}, not a Feature Service.")

    spike_name = spike_service_name(master_id)

    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]")
    console.print(f"Template: [cyan]{template.title}[/cyan] ({master_id})")
    console.print()
    console.print("This will:")
    console.print(f"  1. create ONE new feature service named [yellow]{spike_name}[/yellow]")
    console.print("  2. compare its schema against the template (read-only)")
    console.print(f"  3. {'leave it in place (--keep)' if keep else 'delete that new service'}")
    console.print()
    console.print("[green]It does not modify or delete anything that already exists.[/green]")
    console.print("[dim]The template is only read. The only delete targets the service "
                  "created in step 1, and is refused if that item is not what step 1 "
                  "returned.[/dim]")
    console.print()
    console.print("[dim]Note: AGOL reserves a service name permanently, even after "
                  "deletion. This name is used every run so it only ever burns one.[/dim]")
    if not yes:
        click.confirm("Proceed?", abort=True)

    source_fp = fingerprint_service(FeatureLayerCollection.fromitem(template))
    console.print(f"Template schema: {len(source_fp['layers'])} layer(s), "
                  f"{len(source_fp['tables'])} table(s)")

    copy_item = None
    try:
        console.print(f"Creating {spike_name}...")
        copy_item = copy_whole_service(template, spike_name)
        if copy_item is None:
            _fail("copy_feature_layer_collection() returned None -- the copy failed. "
                  "Fall back to publishing from the file geodatabase.")

        copy_fp = fingerprint_service(FeatureLayerCollection.fromitem(copy_item))
        diffs = diff_fingerprints(source_fp, copy_fp)

        console.print()
        console.print(f"Copy schema: {len(copy_fp['layers'])} layer(s), "
                      f"{len(copy_fp['tables'])} table(s)")
        console.print()

        if diffs:
            t = Table(title="Schema differences")
            for col in ("Severity", "Kind", "Where", "Detail"):
                t.add_column(col, overflow="fold")
            colors = {"critical": "red", "warning": "yellow", "info": "dim"}
            for d in diffs:
                t.add_row(f"[{colors[d.severity]}]{d.severity}[/]", d.kind, d.where, d.detail)
            console.print(t)

        verdict = summarize(diffs)
        style = "green" if verdict.startswith("IDENTICAL") else (
            "red" if verdict.startswith("NOT USABLE") else "yellow"
        )
        console.print(f"\n[bold {style}]{verdict}[/bold {style}]")

        report = REPO_ROOT / "docs" / "spike-master-copy.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            "# Phase 0b: master copy fidelity\n\n"
            f"Template: {template.title} (`{master_id}`)\n\n"
            f"Method: `Item.copy_feature_layer_collection()`\n\n"
            f"## Verdict\n\n{verdict}\n\n"
            f"## Differences ({len(diffs)})\n\n"
            + ("\n".join(f"- {d}" for d in diffs) if diffs else "None.\n")
            + "\n\n## Raw fingerprints\n\n```json\n"
            + json.dumps({"template": source_fp, "copy": copy_fp}, indent=2, default=str)
            + "\n```\n"
        )
        console.print(f"\nWritten to {report.relative_to(REPO_ROOT)}")

    finally:
        # Always clean up, including on an exception partway through the comparison
        # -- an abandoned test service would otherwise sit in the org.
        if copy_item is not None and not keep:
            refusal = refuse_delete_reason(
                copy_item, template_id=master_id, expected_name=spike_name
            )
            if refusal:
                # Asymmetric costs: a leftover test service is a ten-second
                # cleanup, a wrongly deleted service is not.
                err.print(
                    f"REFUSING to delete item {getattr(copy_item, 'itemid', '?')} "
                    f"because {refusal}.\nLeft in place. Review it before removing "
                    f"anything by hand."
                )
            else:
                try:
                    copy_item.delete()
                    console.print(f"[dim]Deleted {spike_name}.[/dim]")
                except Exception as exc:
                    err.print(f"Could not delete {spike_name}: {exc}\nDelete it by hand.")
        elif copy_item is not None:
            console.print(f"[yellow]Left {spike_name} in place (--keep).[/yellow]")


# ---------------------------------------------------------------- phase 2


@main.command()
@click.option("--company", default=None, help="Client company name, e.g. CompanyA.")
@click.option("--location", default=None, help="Project location, e.g. Moline.")
@click.option("--manifest", "manifest_path", type=click.Path(exists=True), default=None,
              help="Manifest to provision from. Defaults to the generated vsclr-standard.yaml.")
@click.option("--service-name-override", default=None, metavar="STEM",
              help="Replace the derived service-name stem, when the standard one is taken.")
@click.option("--destroy", "destroy_slug", default=None, metavar="SLUG",
              help="Roll back a project: delete every item this tool recorded creating.")
@click.option("--dry-run", is_flag=True,
              help="Print the plan and write nothing at all, not even run state.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt for --destroy.")
@click.option("--profile", default="home", show_default=True,
              help="'home' borrows ArcGIS Pro's sign-in. Or a stored profile name.")
def provision(company, location, manifest_path, service_name_override, destroy_slug,
              dry_run, yes, profile) -> None:
    """Provision a project from a manifest.

    Runs stage 0 (preflight) and stage 1 (the master feature service), then
    stops. Stage 2, the views, is not built yet.

    Preflight is the gate on the one irreversible thing here: a hosted service
    name is reserved org-wide permanently, even after the service is deleted. A
    name that is already taken stops the run. Nothing is ever auto-renamed.

    `--destroy SLUG` rolls a project back, deleting only the items recorded in
    state, in reverse creation order.
    """
    from agol_provision.auth import AuthError, connect, describe
    from agol_provision.manifest import ManifestError, load_manifest
    from agol_provision.naming import NameContext, NamingError
    from agol_provision.preflight import (
        STATUS_AVAILABLE,
        STATUS_EXISTS,
        STATUS_OUT_OF_SCOPE,
        STATUS_TAKEN,
        run_preflight,
    )
    from agol_provision.state import ProjectState, StateError

    if destroy_slug:
        _destroy(destroy_slug, profile=profile, yes=yes)
        return

    if not company or not location:
        _fail("Both --company and --location are required, e.g. "
              "`--company CompanyA --location Moline`.")

    path = Path(manifest_path) if manifest_path else (
        REPO_ROOT / "agol_provision" / "templates" / "vsclr-standard.yaml"
    )
    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        _fail(str(exc))

    try:
        ctx = NameContext(
            company=company, location=location, service_name_override=service_name_override
        )
        slug = ctx.slug()
    except NamingError as exc:
        _fail(str(exc))

    try:
        gis = connect(profile)
    except AuthError as exc:
        _fail(str(exc))

    # Reading state is not writing it -- load_or_create only constructs, so this
    # stays safe under --dry-run.
    try:
        state = ProjectState.load_or_create(
            state_dir=STATE_DIR, slug=slug, company=company, location=location,
            manifest_name=manifest.name, manifest_version=manifest.version,
        )
    except StateError as exc:
        _fail(str(exc))

    mode = "dry run, nothing will be written" if dry_run else "live run"
    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]")
    console.print(f"Manifest [cyan]{manifest.name}[/cyan] v{manifest.version} -- {path}")
    console.print(f"Project [cyan]{company} / {location}[/cyan]  ({slug}, {mode})\n")

    report = run_preflight(gis, manifest, ctx, state=state)

    styles = {
        STATUS_AVAILABLE: "green",
        STATUS_TAKEN: "bold red",
        STATUS_EXISTS: "cyan",
        STATUS_OUT_OF_SCOPE: "dim",
    }
    table = Table(title="Plan")
    for col in ("Kind", "Key", "Title", "Service name", "Status"):
        table.add_column(col, overflow="fold")
    for row in report.plan:
        style = styles.get(row.status, "yellow")
        table.add_row(
            row.kind, row.key, row.title,
            row.service_name or "[dim]n/a[/dim]",
            f"[{style}]{row.status}[/{style}]",
        )
    console.print(table)

    if report.warnings:
        console.print()
        for problem in report.warnings:
            console.print(f"[yellow]warning[/yellow]  {problem.message}")

    if report.errors:
        console.print()
        for problem in report.errors:
            err.print(f"FAIL  {problem.message}")
        if any(row.status == STATUS_TAKEN for row in report.plan):
            # Said once, after the list, rather than repeated per collision.
            console.print(
                "\n[yellow]To fix a taken name:[/yellow] change that item's "
                "`service_name` in the manifest for a single collision, or pass "
                "--service-name-override to change the stem for the whole project. "
                "Nothing is renamed automatically -- a service carrying a name nobody "
                "chose is worse than a stopped run."
            )
        _fail(f"\nPREFLIGHT FAILED -- {len(report.errors)} problem(s). "
              f"Nothing was created.")

    console.print("\n[bold green]PREFLIGHT PASSED[/bold green]")
    if dry_run:
        console.print("[dim]--dry-run: nothing was written, not even run state.[/dim]")
        console.print("Nothing was created in ArcGIS Online.")
        return

    state.complete_stage("preflight")
    _create_master(gis, manifest, ctx, state)
    state.complete_stage("master")

    console.print(
        "\nStage 2 (views) is not built yet. The master exists; nothing else "
        "was created."
    )


def _create_master(gis: Any, manifest: Any, ctx: Any, state: Any) -> None:
    """Stage 1: copy the template master, name it, and put its indexes back."""
    from agol_provision.master import APPLIED, FAILED, MasterError, reapply_user_indexes
    from agol_provision.state import CreatedItem

    spec = manifest.master
    title = ctx.render_title(spec.title)
    service_name = ctx.render_service_name(spec.service_name)

    console.print("\n[bold]Stage 1 -- master[/bold]")
    if state.has(spec.key):
        existing = state.get(spec.key)
        console.print(f"[dim]Already created as {existing.item_id}; skipping.[/dim]")
        return

    template = gis.content.get(spec.template_item_id)
    console.print(f"Copying [cyan]{template.title}[/cyan] to [cyan]{service_name}[/cyan] "
                  f"({len(template.layers)} layer(s), {len(template.tables)} table(s))...")
    copy_item = copy_whole_service(template, service_name)
    if copy_item is None:
        _fail(f"copy_feature_layer_collection() returned None -- the copy failed. "
              f"Nothing was recorded, so check the org for a partial service named "
              f"{service_name} before re-running.")

    # Recorded before anything else can fail. An item that exists in AGOL but not
    # in state is the one failure mode that leaks an orphan.
    state.record(CreatedItem(
        key=spec.key, item_id=copy_item.itemid, item_type="Feature Service",
        title=title, service_name=service_name,
    ))
    console.print(f"[green]Created[/green] {service_name} ({copy_item.itemid})")

    # The copy names the item after the *service*, so without this the master's
    # item title reads "CompanyA_Moline" rather than "CompanyA Moline".
    copy_item.update(item_properties={
        "title": title,
        "description": (
            f"Master feature service for {ctx.company} {ctx.location}, provisioned "
            f"from template {template.title} ({spec.template_item_id})."
        ),
        "tags": [ctx.company, ctx.location],
    })
    console.print(f"Item title set to [cyan]{title}[/cyan]")

    # The copy strips every index, so the ten user-defined ones go back by hand.
    # `build_status_Index` is the one that matters: build_status is the field
    # every view's definition query filters on.
    try:
        outcomes = reapply_user_indexes(template, copy_item)
    except MasterError as exc:
        err.print(f"Indexes were NOT reapplied: {exc}\nThe master exists and is "
                  f"recorded. Fix the cause and reapply by hand, or --destroy and "
                  f"re-run.")
        return

    if not outcomes:
        console.print("[dim]No user-defined indexes to reapply.[/dim]")
        return

    failed = [o for o in outcomes if o.status == FAILED]
    applied = [o for o in outcomes if o.status == APPLIED]
    console.print(
        f"Indexes: [green]{len(applied)} applied[/green], "
        f"{len(outcomes) - len(applied) - len(failed)} already present, "
        f"{len(failed)} failed"
    )
    for outcome in failed:
        err.print(f"  index {outcome.index} on {outcome.layer} failed: {outcome.detail}")
    if failed:
        err.print("The master exists and is recorded. A missing index is a "
                  "performance problem, not a correctness one -- every view's "
                  "definition query still works.")


def _destroy(slug: str, *, profile: str, yes: bool) -> None:
    """Delete every item recorded for a project, in reverse creation order.

    Reverse *recorded* order, not an assumed one: AGOL refuses to delete a
    feature service while its views still exist. Only recorded items are ever
    touched -- anything this tool merely found is left alone.
    """
    from agol_provision.auth import AuthError, connect, describe
    from agol_provision.state import ProjectState, StateError

    path = STATE_DIR / f"{slug}.json"
    if not path.exists():
        _fail(f"No run state for {slug!r} at {path}. --destroy only deletes items this "
              f"tool recorded creating, so there is nothing it can safely remove.")
    try:
        state = ProjectState.load(path)
    except StateError as exc:
        _fail(str(exc))

    items = state.destroy_order()
    if not items:
        console.print(f"{slug} has no recorded items. Nothing to delete.")
        return

    try:
        gis = connect(profile)
    except AuthError as exc:
        _fail(str(exc))

    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]")
    console.print(f"\nThis deletes {len(items)} item(s) recorded for [cyan]{slug}[/cyan], "
                  f"in reverse creation order:")
    for item in items:
        console.print(f"  {item.item_type:<16} {item.title}  [dim]({item.item_id})[/dim]")
    console.print("\n[dim]Only these are touched. Anything this tool did not create is "
                  "left alone.[/dim]")
    if not yes:
        click.confirm("Delete these?", abort=True)

    for item in items:
        obj = gis.content.get(item.item_id)
        if obj is None:
            console.print(f"[dim]{item.title} ({item.item_id}) is already gone.[/dim]")
            state.forget(item.key)
            continue
        try:
            obj.delete()
        except Exception as exc:
            err.print(f"Could not delete {item.title} ({item.item_id}): {exc}")
            _fail("Stopped. Everything not yet deleted is still recorded, so re-running "
                  "--destroy resumes from here.")
        # Forgotten only after AGOL confirms the delete.
        state.forget(item.key)
        console.print(f"[green]Deleted[/green] {item.title}")

    console.print(f"\n[green]{slug} rolled back.[/green]")


if __name__ == "__main__":
    main()

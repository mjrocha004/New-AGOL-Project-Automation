"""Command-line entry point.

Phase 0 commands (`discover`, `spike-master`) come first: they establish what the
templates actually contain and whether the master schema copies faithfully. The
provisioning commands are built on top of what those two report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agol_provision import __version__

console = Console()
err = Console(stderr=True, style="bold red")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fail(message: str) -> None:
    err.print(message)
    sys.exit(1)


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Provision a complete ArcGIS Online project from templates."""


# ---------------------------------------------------------------- setup


@main.command("setup-profile")
@click.option("--name", required=True, help="Local profile name, e.g. vsclr.")
@click.option("--org", required=True, help="https://yourorg.maps.arcgis.com")
@click.option("--username", help="AGOL username. Use for built-in ArcGIS accounts.")
@click.option("--client-id", help="OAuth app client id. Use for SSO / enterprise logins.")
def setup_profile(name: str, org: str, username: str | None, client_id: str | None) -> None:
    """Store AGOL credentials in a local profile.

    Two sign-in paths, depending on how your organization authenticates:

    \b
      --username    Built-in ArcGIS account. Prompts for a password, which is
                    handed to the ArcGIS API and stored in the OS keyring.
      --client-id   SSO / SAML / enterprise login. Opens a browser to sign in and
                    asks for the resulting authorization code. SAML accounts
                    cannot accept a password directly, so this is the only path
                    that works for them.

    Either way, nothing is written to this repository, shell history, or command
    output.
    """
    from arcgis.gis import GIS

    if bool(username) == bool(client_id):
        _fail(
            "Pass exactly one of --username (built-in account) or --client-id (SSO).\n"
            "If sign-in normally sends you to your company's login page, you need "
            "--client-id.\n"
            "Register an OAuth app in AGOL: Content > New item > Application > Other "
            "application, then copy its Client ID."
        )

    try:
        if client_id:
            console.print("A browser window will open. Sign in, then paste the code here.")
            gis = GIS(url=org, client_id=client_id, profile=name)
        else:
            password = click.prompt("AGOL password", hide_input=True)
            gis = GIS(url=org, username=username, password=password, profile=name)
    except Exception as exc:
        _fail(
            f"Could not sign in: {exc}\n\n"
            "If your organization uses single sign-on, a username and password will "
            "not work -- re-run with --client-id instead."
        )

    console.print(f"[green]Saved profile[/green] {name!r} for {gis.users.me.username}.")
    console.print("Credentials are in the OS keyring, not in this repo.")
    console.print("Verify the machine is ready with: [cyan]agol-provision doctor "
                  f"--profile {name}[/cyan]")


# ---------------------------------------------------------------- environment


@main.command()
@click.option("--profile", help="Profile to test, or 'pro' for the ArcGIS Pro "
                                "connection. Omit to check the install only.")
def doctor(profile: str | None) -> None:
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
    py = sys.version_info
    check("Python 3.11 or 3.12", (3, 11) <= (py.major, py.minor) < (3, 13),
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

    pro_mode = bool(profile) and uses_arcgis_pro(profile)
    has_arcpy = arcpy_available()

    console.print()
    console.print("[bold]Sign-in options available here[/bold]")
    if pro_mode:
        # Requested explicitly, so now it is a requirement rather than an option.
        check("--profile pro (borrows ArcGIS Pro's connection)", has_arcpy,
              "arcpy present" if has_arcpy else "arcpy not importable")
        if not has_arcpy:
            console.print("        [yellow]Run from a clone of ArcGIS Pro's conda "
                          "environment, or use a stored profile instead.[/yellow]")
    else:
        note("--profile pro (borrows ArcGIS Pro's connection)", has_arcpy,
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
            if profile:
                check("keyring stores and returns a secret", stored == "ok", backend)
            else:
                note("stored profile (keyring holds the password)", stored == "ok", backend)
            try:
                keyring.delete_password("agol-provision-doctor", "test")
            except Exception:
                pass
        except Exception as exc:
            check("keyring usable", False, str(exc))
            console.print("        [yellow]Without a keyring the ArcGIS API cannot store "
                          "a profile password securely. --profile pro avoids the "
                          "question entirely.[/yellow]")

    if not profile:
        console.print()
        console.print("[dim]Pass --profile NAME to also test the AGOL connection.[/dim]")
        sys.exit(0 if ok else 1)

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
        console.print("[bold green]Ready.[/bold green] Next: agol-provision discover "
                      f"--profile {profile} --group \"Templates\"")
    else:
        console.print("[bold red]Not ready.[/bold red] Fix the failures above first.")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- phase 0a


@main.command()
@click.option("--profile", required=True, help="Stored AGOL profile name.")
@click.option("--group", help="Template group id or title to read items from.")
@click.option("--ids", "ids_file", type=click.Path(exists=True),
              help="File with one template item id per line.")
@click.option("--query", help="AGOL search query, e.g. 'title:VSCLR Template'.")
@click.option("--name", "manifest_name", default="vsclr-standard",
              help="Name for the generated manifest.")
@click.option("--out", type=click.Path(), default=None,
              help="Manifest output path. Defaults to the templates directory.")
def discover(profile, group, ids_file, query, manifest_name, out) -> None:
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

    item_ids = None
    if ids_file:
        item_ids = [ln.strip() for ln in Path(ids_file).read_text().splitlines() if ln.strip()]

    try:
        items = discovery.collect(gis, group=group, item_ids=item_ids, query=query)
    except ValueError as exc:
        _fail(str(exc))

    if not items:
        _fail("No template items found. Widen the search, or check sharing on the templates.")

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


# ---------------------------------------------------------------- phase 0b


@main.command("spike-master")
@click.option("--profile", required=True, help="Stored AGOL profile name.")
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

    spike_name = f"ZZZ_SPIKE_TEST_{master_id[:8]}"

    console.print(f"Connected as [cyan]{describe(gis)}[/cyan]")
    console.print(f"Template: [cyan]{template.title}[/cyan] ({master_id})")
    console.print()
    console.print("This will:")
    console.print(f"  1. create a feature service named [yellow]{spike_name}[/yellow]")
    console.print("  2. compare its schema against the template")
    console.print(f"  3. {'leave it in place (--keep)' if keep else 'delete it'}")
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
        copy_item = template.copy_feature_layer_collection(service_name=spike_name)
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
            try:
                copy_item.delete()
                console.print(f"[dim]Deleted {spike_name}.[/dim]")
            except Exception as exc:
                err.print(f"Could not delete {spike_name}: {exc}\nDelete it by hand.")
        elif copy_item is not None:
            console.print(f"[yellow]Left {spike_name} in place (--keep).[/yellow]")


if __name__ == "__main__":
    main()

"""Stage 0: the read-only check that runs before anything is created.

Preflight exists because of one asymmetry. Every other failure in a provisioning
run can be rolled back, but a hosted feature service name is reserved org-wide
**permanently** -- deleting the service does not release it. A collision found at
stage 1 has already burned the name.

So this stage does everything that can be done without writing: resolve the
template items, render every title and service name, and ask AGOL whether each
name is free. **A taken name is a hard failure and is never auto-suffixed.** A
silently renamed service is worse than a stopped run: the project carries a name
nobody chose, and every downstream reference points at it. Note that
`copy_feature_layer_collection()` *does* auto-suffix internally, which is the
second reason the collision has to be caught here.

Problems are collected rather than raised one at a time. This is run from a
different machine than it is written on, so reporting four collisions in one run
beats four runs reporting one each.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agol_provision.auth import check_privileges
from agol_provision.discovery import classify
from agol_provision.manifest import Manifest
from agol_provision.naming import NameContext, NamingError
from agol_provision.state import ProjectState

# The only service type this tool creates.
SERVICE_TYPE = "featureService"

STATUS_AVAILABLE = "available"
STATUS_TAKEN = "TAKEN"
STATUS_EXISTS = "exists"
STATUS_UNCHECKED = "unchecked"
# Stages 3-6 are deliberately not built. These items are shown so the plan reads
# as the whole project, but nothing about them gates the run.
STATUS_OUT_OF_SCOPE = "not in this build"

ERROR, WARNING = "error", "warning"

# Rendering a pattern can fail three ways: an unusable company name (NamingError),
# an unknown placeholder (KeyError), or a bare `{}` (IndexError).
_RENDER_ERRORS = (NamingError, KeyError, IndexError)


@dataclass(frozen=True)
class PlannedItem:
    """One row of the plan: what would be created, under what name."""

    kind: str  # master | view | group | map | app
    key: str
    title: str
    service_name: str | None
    status: str


@dataclass(frozen=True)
class Problem:
    severity: str  # error | warning
    key: str
    message: str


@dataclass
class PreflightReport:
    plan: list[PlannedItem] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == ERROR]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == WARNING]

    @property
    def ok(self) -> bool:
        """Whether provisioning may proceed. Warnings do not block it."""
        return not self.errors


def run_preflight(
    gis: Any,
    manifest: Manifest,
    ctx: NameContext,
    *,
    state: ProjectState | None = None,
) -> PreflightReport:
    """Check everything checkable without writing to AGOL.

    ``state`` is an in-progress run, if there is one. Items it has already
    recorded are skipped by the availability check -- on a resume the master's
    name is taken *by this project*, and re-checking it would make resuming
    impossible.
    """
    report = PreflightReport()
    already_created = set(state.items) if state is not None else set()

    _check_privileges(gis, report)

    # Master then views: the plan doubles as the creation order.
    in_scope: list[tuple[str, Any, str | None]] = []
    for kind, spec in [("master", manifest.master), *(("view", v) for v in manifest.views)]:
        title = _render(ctx.render_title, spec.title, spec.key, "title", report)
        service = _render(
            ctx.render_service_name, spec.service_name, spec.key, "service name", report
        )
        _resolve_template(gis, spec, expected=kind, severity=ERROR, report=report)
        in_scope.append((kind, spec, service))
        # A placeholder row; the status is filled in below, once duplicates are known.
        report.plan.append(
            PlannedItem(kind, spec.key, title or "", service, STATUS_UNCHECKED)
        )

    _reject_duplicate_service_names(in_scope, report)

    for index, (kind, spec, service) in enumerate(in_scope):
        status = _availability(gis, spec.key, service, already_created, report)
        row = report.plan[index]
        report.plan[index] = PlannedItem(row.kind, row.key, row.title, row.service_name, status)

    _describe_out_of_scope(gis, manifest, ctx, report)
    return report


# ---------------------------------------------------------------- checks


def _check_privileges(gis: Any, report: PreflightReport) -> None:
    """Report missing privileges rather than raising, so the run reports everything."""
    missing = check_privileges(gis, strict=False)
    if missing:
        report.problems.append(
            Problem(
                ERROR,
                "privileges",
                f"{gis.users.me.username} cannot: {', '.join(missing)}. Provisioning "
                f"would fail partway through and leave a partial project. Ask an org "
                f"administrator to grant these.",
            )
        )


def _render(
    renderer: Any,
    pattern: str,
    key: str,
    what: str,
    report: PreflightReport,
    severity: str = ERROR,
) -> str | None:
    """Fill one pattern, recording a problem instead of raising."""
    try:
        return renderer(pattern)
    except _RENDER_ERRORS as exc:
        report.problems.append(
            Problem(severity, key, f"{key!r} {what} pattern {pattern!r} cannot be rendered: {exc}")
        )
        return None


def _resolve_template(
    gis: Any, spec: Any, *, expected: str, severity: str, report: PreflightReport
) -> Any:
    """Confirm a template item is visible and is the kind of thing the manifest says."""
    try:
        item = gis.content.get(spec.template_item_id)
    except Exception as exc:  # a search failure reads the same as a missing item
        report.problems.append(
            Problem(severity, spec.key, f"Could not look up {spec.key!r}: {exc}")
        )
        return None

    if item is None:
        report.problems.append(
            Problem(
                severity,
                spec.key,
                f"Template item {spec.template_item_id} for {spec.key!r} is not visible to "
                f"this account. It may have been deleted, or shared to a group this "
                f"account is not in.",
            )
        )
        return None

    role = classify(item)
    if role != expected:
        report.problems.append(
            Problem(
                severity,
                spec.key,
                f"{spec.key!r} expects a {expected}, but template item "
                f"{spec.template_item_id} ({item.title!r}) is a {role}. The manifest "
                f"points at the wrong item.",
            )
        )
    return item


def _reject_duplicate_service_names(
    in_scope: list[tuple[str, Any, str | None]], report: PreflightReport
) -> None:
    """Two items deriving the same name would collide mid-run, after the first is made.

    AGOL reports the name as available right up until the first of the pair takes
    it, so this cannot be caught by the availability check.
    """
    names = [service for _, _, service in in_scope if service]
    for name in sorted({n for n in names if names.count(n) > 1}):
        keys = [spec.key for _, spec, service in in_scope if service == name]
        report.problems.append(
            Problem(
                ERROR,
                "service_names",
                f"{', '.join(keys)} all derive the service name {name!r}. Each hosted "
                f"service name must be unique; the run would fail partway through.",
            )
        )


def _availability(
    gis: Any,
    key: str,
    service: str | None,
    already_created: set[str],
    report: PreflightReport,
) -> str:
    """Ask AGOL whether one derived name is free. Never proposes an alternative."""
    if service is None:
        return STATUS_UNCHECKED
    if key in already_created:
        # This run took the name itself. Re-checking it would break every resume.
        return STATUS_EXISTS

    try:
        available = gis.content.is_service_name_available(service, SERVICE_TYPE)
    except Exception as exc:
        report.problems.append(
            Problem(ERROR, key, f"Could not check whether {service!r} is available: {exc}")
        )
        return STATUS_UNCHECKED

    if available:
        return STATUS_AVAILABLE

    # Deliberately short: with several collisions the remedy would otherwise be
    # repeated once per name. The caller states it once, after the list.
    report.problems.append(
        Problem(
            ERROR,
            key,
            f"Service name {service!r} (for {key!r}) is already taken in this "
            f"organization. AGOL reserves hosted service names permanently, even after "
            f"the service is deleted, so this one cannot be freed.",
        )
    )
    return STATUS_TAKEN


def _describe_out_of_scope(
    gis: Any, manifest: Manifest, ctx: NameContext, report: PreflightReport
) -> None:
    """Add the groups, maps and apps to the plan without gating the run on them.

    Their templates are still resolved -- it is read-only and catches a stale
    manifest early -- but a broken stage-4 template must not block a stage-2 run.
    """
    for group in manifest.groups:
        title = _render(ctx.render_title, group.title, group.key, "title", report, WARNING)
        report.plan.append(
            PlannedItem("group", group.key, title or "", None, STATUS_OUT_OF_SCOPE)
        )

    for kind, specs in (("map", manifest.maps), ("app", manifest.apps)):
        for spec in specs:
            title = _render(ctx.render_title, spec.title, spec.key, "title", report, WARNING)
            _resolve_template(gis, spec, expected=kind, severity=WARNING, report=report)
            report.plan.append(
                PlannedItem(kind, spec.key, title or "", None, STATUS_OUT_OF_SCOPE)
            )

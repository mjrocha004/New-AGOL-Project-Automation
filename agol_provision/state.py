"""Durable record of what a provisioning run actually created.

This is what makes a run recoverable. A failure at item 14 of 20 should be a
resume, not a cleanup job, and a rollback should delete exactly what this tool
created -- never anything it merely found.

Two invariants matter:

1. **Writes are atomic.** State is flushed after every item, so a crash between
   "AGOL created the item" and "we recorded it" is the one gap that leaks. Writing
   via temp-file-then-rename keeps a crash *during* the write from also corrupting
   the record of the previous 13 items.
2. **Deletion order is reverse creation order**, taken from the recorded order
   rather than an assumed one. This matters concretely: AGOL refuses to delete a
   feature service while views still depend on it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_VERSION = 1

# Creation order. Rollback walks the recorded items backwards, not this list, but
# preflight uses it to decide what to skip on resume.
STAGES: tuple[str, ...] = (
    "preflight",
    "master",
    "views",
    "groups",
    "maps",
    "apps",
    "sharing",
    "verify",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CreatedItem:
    """One AGOL item this tool created."""

    key: str  # manifest key, e.g. "design_view"
    item_id: str
    item_type: str  # "Feature Service", "Web Map", "Dashboard", "Web Experience", "Group"
    title: str
    service_name: str | None = None
    created_at: str = field(default_factory=_utc_now)

    @property
    def is_group(self) -> bool:
        return self.item_type == "Group"


class StateError(RuntimeError):
    """State on disk is unusable or inconsistent with the requested run."""


@dataclass
class ProjectState:
    """Per-project run record, persisted as ``state/<slug>.json``."""

    slug: str
    company: str
    location: str
    manifest_name: str
    manifest_version: int
    path: Path
    stages_completed: list[str] = field(default_factory=list)
    # Insertion-ordered: this ordering *is* the rollback plan.
    items: dict[str, CreatedItem] = field(default_factory=dict)
    started_at: str = field(default_factory=_utc_now)
    state_version: int = STATE_VERSION

    # ---------- lifecycle ----------

    @classmethod
    def load_or_create(
        cls,
        state_dir: Path,
        slug: str,
        company: str,
        location: str,
        manifest_name: str,
        manifest_version: int,
    ) -> ProjectState:
        """Resume an existing run, or start a new one.

        Refuses to resume across a manifest version change: the half-built project
        was assembled against different rules, and silently continuing would
        produce a project matching neither version.
        """
        path = state_dir / f"{slug}.json"
        if not path.exists():
            return cls(
                slug=slug,
                company=company,
                location=location,
                manifest_name=manifest_name,
                manifest_version=manifest_version,
                path=path,
            )

        existing = cls.load(path)
        if existing.manifest_version != manifest_version:
            raise StateError(
                f"{path} was created with manifest {existing.manifest_name} "
                f"v{existing.manifest_version}, but v{manifest_version} was requested. "
                f"Roll back the partial project (`--destroy {slug}`) before re-running, "
                f"or pin the manifest version."
            )
        if (existing.company, existing.location) != (company, location):
            raise StateError(
                f"{path} belongs to {existing.company} / {existing.location}, not "
                f"{company} / {location}. Refusing to mix two projects in one state file."
            )
        return existing

    @classmethod
    def load(cls, path: Path) -> ProjectState:
        try:
            raw: dict[str, Any] = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise StateError(
                f"{path} is not valid JSON ({exc}). It may have been truncated by a "
                f"hard kill. Inspect it by hand before re-running -- it lists the items "
                f"that already exist in AGOL."
            ) from exc

        if raw.get("state_version") != STATE_VERSION:
            raise StateError(
                f"{path} has state_version {raw.get('state_version')}, expected "
                f"{STATE_VERSION}."
            )

        return cls(
            slug=raw["slug"],
            company=raw["company"],
            location=raw["location"],
            manifest_name=raw["manifest_name"],
            manifest_version=raw["manifest_version"],
            path=path,
            stages_completed=list(raw.get("stages_completed", [])),
            items={k: CreatedItem(**v) for k, v in raw.get("items", {}).items()},
            started_at=raw.get("started_at", _utc_now()),
        )

    def save(self) -> None:
        """Persist atomically: write a sibling temp file, then rename over the target.

        ``os.replace`` is atomic within a filesystem, so a crash leaves either the
        old complete file or the new one -- never a half-written mix.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state_version": self.state_version,
            "slug": self.slug,
            "company": self.company,
            "location": self.location,
            "manifest_name": self.manifest_name,
            "manifest_version": self.manifest_version,
            "started_at": self.started_at,
            "stages_completed": self.stages_completed,
            "items": {k: asdict(v) for k, v in self.items.items()},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)

    # ---------- recording ----------

    def record(self, item: CreatedItem) -> None:
        """Record a created item and flush immediately.

        Flushing per item rather than per stage narrows the window in which an item
        exists in AGOL but not in state -- the one failure mode that leaks orphans.
        """
        if item.key in self.items:
            raise StateError(
                f"{item.key!r} is already recorded as item {self.items[item.key].item_id}. "
                f"Re-creating it would orphan the original."
            )
        self.items[item.key] = item
        self.save()

    def get(self, key: str) -> CreatedItem | None:
        return self.items.get(key)

    def has(self, key: str) -> bool:
        return key in self.items

    def complete_stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise StateError(f"Unknown stage {stage!r}. Known stages: {', '.join(STAGES)}")
        if stage not in self.stages_completed:
            self.stages_completed.append(stage)
            self.save()

    def is_stage_complete(self, stage: str) -> bool:
        return stage in self.stages_completed

    # ---------- rollback ----------

    def destroy_order(self) -> list[CreatedItem]:
        """Items in the order they must be deleted: reverse of creation.

        Derived from what was actually recorded, not from an assumed stage order, so
        it stays correct even if a run was interrupted mid-stage. Views therefore
        always precede the master service they depend on.
        """
        return list(reversed(list(self.items.values())))

    def forget(self, key: str) -> None:
        """Drop an item from state after it has been deleted from AGOL."""
        self.items.pop(key, None)
        # A rolled-back project has no completed stages to resume from.
        self.stages_completed = []
        self.save()

    def item_ids(self) -> set[str]:
        """Every AGOL id this run created. Used by verify to assert no leakage."""
        return {i.item_id for i in self.items.values()}

"""The template manifest: a declarative description of one project's item set.

The manifest is the central artifact of this tool. It holds *what* to build and
how the pieces reference each other; the code holds *how* to build each type. That
split is what makes "some clients skip the Permit app" a config edit instead of a
rewrite.

Cross-references are validated up front. A typo in a `consumes` or `share_to` key
fails here, before a single AGOL call, rather than at stage 4 of a live run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# AGOL item ids are 32 lowercase hex characters. Validating the shape catches the
# common failure -- a truncated copy-paste -- before it becomes a 404 mid-run.
ITEM_ID_RE = re.compile(r"^[0-9a-f]{32}$")

AppType = Literal["Web Map", "Dashboard", "Web Experience", "Web Mapping Application"]


class ManifestError(ValueError):
    """The manifest is malformed or internally inconsistent."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MasterSpec(_Base):
    """The authoritative feature service every view derives from."""

    template_item_id: str
    title: str = "{base}"
    service_name: str = "{base_sn}"
    key: str = "master"

    @field_validator("template_item_id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        return _check_item_id(v, "master")


class ViewSpec(_Base):
    """A hosted feature layer view created natively from the new master.

    Views are never cloned -- cloning a view within a single org is unreliable and
    can silently re-point at the source service. Instead the template view's real
    configuration (visible fields, definition queries, capabilities) is read at
    provision time and replayed against the new master.
    """

    key: str
    template_item_id: str
    title: str
    service_name: str

    @field_validator("template_item_id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        return _check_item_id(v, "view")


class GroupSpec(_Base):
    """An AGOL group. Groups are created, never cloned."""

    key: str
    title: str
    snippet: str = ""
    description: str = ""
    access: Literal["private", "org", "public"] = "private"
    template_group_id: str | None = None
    # Which views this group is meant to consume. Documentation and a verify
    # target; sharing itself is driven by each item's `share_to`.
    consumes: list[str] = Field(default_factory=list)


class ItemSpec(_Base):
    """A cloned item: web map, dashboard, or Experience Builder app."""

    key: str
    template_item_id: str
    item_type: AppType
    title: str
    consumes: list[str] = Field(default_factory=list)
    share_to: list[str] = Field(default_factory=list)

    @field_validator("template_item_id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        return _check_item_id(v, "item")


def _check_item_id(v: str, what: str) -> str:
    v = v.strip()
    if not ITEM_ID_RE.match(v):
        raise ValueError(
            f"{what} template_item_id {v!r} is not a 32-character hex AGOL item id. "
            f"Check for a truncated paste."
        )
    return v


class Manifest(_Base):
    """A complete project template definition."""

    name: str
    version: int
    source_org: str
    master: MasterSpec
    views: list[ViewSpec] = Field(default_factory=list)
    groups: list[GroupSpec] = Field(default_factory=list)
    maps: list[ItemSpec] = Field(default_factory=list)
    apps: list[ItemSpec] = Field(default_factory=list)

    # ---------- derived views over the data ----------

    @property
    def cloned_items(self) -> list[ItemSpec]:
        """Maps and apps, in creation order. Maps first -- apps reference maps."""
        return [*self.maps, *self.apps]

    @property
    def all_keys(self) -> set[str]:
        return (
            {self.master.key}
            | {v.key for v in self.views}
            | {g.key for g in self.groups}
            | {i.key for i in self.cloned_items}
        )

    @property
    def group_keys(self) -> set[str]:
        return {g.key for g in self.groups}

    def template_item_ids(self) -> dict[str, str]:
        """Every template key -> template AGOL item id.

        Used to build the global remap table. Groups are excluded: they are not
        cloned, so no cloned JSON ever references a template group id.
        """
        ids = {self.master.key: self.master.template_item_id}
        ids.update({v.key: v.template_item_id for v in self.views})
        ids.update({i.key: i.template_item_id for i in self.cloned_items})
        return ids

    def total_items(self) -> int:
        return len(self.all_keys)

    # ---------- validation ----------

    @model_validator(mode="after")
    def _validate_graph(self) -> Manifest:
        self._reject_duplicate_keys()
        self._reject_dangling_references()
        return self

    def _reject_duplicate_keys(self) -> None:
        """Keys index run state, so a collision would silently overwrite an item."""
        seen: dict[str, str] = {}
        groups: list[tuple[str, list[Any]]] = [
            ("master", [self.master]),
            ("views", self.views),
            ("groups", self.groups),
            ("maps", self.maps),
            ("apps", self.apps),
        ]
        for section, specs in groups:
            for spec in specs:
                if spec.key in seen:
                    raise ValueError(
                        f"Duplicate key {spec.key!r} in `{section}` -- already used in "
                        f"`{seen[spec.key]}`. Keys index run state and must be unique."
                    )
                seen[spec.key] = section

    def _reject_dangling_references(self) -> None:
        """A typo in `consumes` or `share_to` fails here, not at stage 4 of a run."""
        known = self.all_keys
        groups = self.group_keys

        for spec in [*self.groups, *self.cloned_items]:
            for ref in getattr(spec, "consumes", []):
                if ref not in known:
                    raise ValueError(
                        f"{spec.key!r} consumes {ref!r}, which is not defined in this "
                        f"manifest. Known keys: {', '.join(sorted(known))}"
                    )
            for ref in getattr(spec, "share_to", []):
                if ref not in groups:
                    raise ValueError(
                        f"{spec.key!r} shares to {ref!r}, which is not a group in this "
                        f"manifest. Known groups: {', '.join(sorted(groups)) or '(none)'}"
                    )


def load_manifest(path: str | Path) -> Manifest:
    """Read and validate a manifest YAML file."""
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"No manifest at {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{path} must contain a YAML mapping at the top level.")
    try:
        return Manifest(**raw)
    except Exception as exc:
        raise ManifestError(f"{path} is invalid:\n{exc}") from exc

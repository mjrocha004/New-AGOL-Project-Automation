"""Read-only audit of the template items in ArcGIS Online.

Produces three artifacts:

1. A manifest skeleton with real item ids and a derived dependency graph.
2. A JSON snapshot of every template item -- the version-history mechanism that
   replaces "keep a copy in SharePoint". Committed to git, these give real diffs.
3. A findings report for a human to read before committing to the build.

Nothing here writes to AGOL.

The dependency graph is derived by scanning each item's serialized JSON for the
ids of *other* template items, rather than by parsing each item type's schema
individually. A web map keeps layer ids in `operationalLayers`, a dashboard buries
them in per-widget datasources, and an Experience Builder app keeps them in a
draft config -- but all three are JSON containing the id somewhere. Scanning finds
every edge without needing to know each format, which is the same principle
`remap_data()` works on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcgis.features import FeatureLayerCollection
from arcgis.gis import GIS, Item

ITEM_ID_RE = re.compile(r"[0-9a-f]{32}")

# Item type -> manifest section.
_SECTION_BY_TYPE = {
    "Web Map": "maps",
    "Dashboard": "apps",
    "Web Experience": "apps",
    "Web Mapping Application": "apps",
}


@dataclass
class TemplateItem:
    """One discovered template item, plus everything the report needs about it."""

    item: Item
    role: str  # master | view | map | app | unknown
    key: str
    depends_on: list[str] = field(default_factory=list)  # item ids
    detail: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def item_id(self) -> str:
        return self.item.itemid

    @property
    def title(self) -> str:
        return self.item.title

    @property
    def section(self) -> str:
        if self.role == "master":
            return "master"
        if self.role == "view":
            return "views"
        return _SECTION_BY_TYPE.get(self.item.type, "apps")


# ---------------------------------------------------------------- collection


def collect(
    gis: GIS,
    *,
    group: str | None = None,
    item_ids: list[str] | None = None,
    query: str | None = None,
) -> list[Item]:
    """Gather the template items by group, explicit id list, or search query."""
    if item_ids:
        found = []
        for iid in item_ids:
            it = gis.content.get(iid.strip())
            if it is None:
                raise ValueError(f"No item with id {iid!r} is visible to this account.")
            found.append(it)
        return found

    if group:
        grp = gis.groups.get(group)
        if grp is None:
            matches = gis.groups.search(f'title:"{group}"')
            if not matches:
                raise ValueError(f"No group matching {group!r} is visible to this account.")
            grp = matches[0]
        return list(grp.content())

    if query:
        return list(gis.content.search(query, max_items=200))

    raise ValueError("Provide one of --group, --ids, or --query to locate the templates.")


def classify(item: Item) -> str:
    """Determine an item's role from its type and type keywords."""
    if item.type == "Feature Service":
        # AGOL marks hosted feature layer views with this keyword; the service's
        # own `isView` property agrees, but the keyword avoids a second request.
        keywords = {k.strip() for k in (item.typeKeywords or [])}
        return "view" if "View Service" in keywords else "master"
    if item.type == "Web Map":
        return "map"
    if item.type in _SECTION_BY_TYPE:
        return "app"
    return "unknown"


def suggest_key(item: Item, taken: set[str]) -> str:
    """Propose a stable manifest key from the item title.

    A suggestion only -- keys index run state, so the intent is that a human reads
    and shortens them in the generated manifest.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", item.title.lower()).strip("_") or "item"
    key, n = slug, 2
    while key in taken:
        key, n = f"{slug}_{n}", n + 1
    return key


# ---------------------------------------------------------------- inspection


def item_json_text(item: Item) -> str:
    """Serialized item data, for dependency scanning. Empty string if unreadable."""
    try:
        data = item.get_data()
    except Exception:
        return ""
    if data is None:
        return ""
    if isinstance(data, (dict, list)):
        return json.dumps(data)
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="ignore")
    return str(data)


def find_dependencies(item: Item, known_ids: set[str]) -> list[str]:
    """Template item ids referenced anywhere in this item's JSON."""
    text = item_json_text(item)
    if not text:
        return []
    found = {m for m in ITEM_ID_RE.findall(text) if m in known_ids}
    found.discard(item.itemid)
    return sorted(found)


def describe_master(item: Item) -> tuple[dict[str, Any], list[str]]:
    """Inventory the master's schema, flagging anything a schema copy may not carry."""
    detail: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        flc = FeatureLayerCollection.fromitem(item)
    except Exception as exc:
        return {"error": str(exc)}, [f"Could not read feature service definition: {exc}"]

    props = flc.properties
    detail["layer_count"] = len(props.get("layers", []))
    detail["table_count"] = len(props.get("tables", []))
    detail["capabilities"] = props.get("capabilities", "")
    detail["has_attachments"] = False
    detail["editor_tracking"] = bool(
        props.get("editorTrackingInfo", {}).get("enableEditorTracking", False)
    )

    relationships, domains, subtypes, attribute_rules = [], set(), 0, 0
    for layer in list(flc.layers) + list(flc.tables):
        lp = layer.properties
        name = lp.get("name", f"layer {lp.get('id')}")
        for rel in lp.get("relationships", []) or []:
            relationships.append(
                {"layer": name, "name": rel.get("name"), "related_id": rel.get("relatedTableId"),
                 "cardinality": rel.get("cardinality"), "role": rel.get("role")}
            )
        for fld in lp.get("fields", []) or []:
            if fld.get("domain"):
                domains.add(f"{name}.{fld.get('name')}")
        if lp.get("subtypes"):
            subtypes += len(lp["subtypes"])
        if lp.get("hasAttachments"):
            detail["has_attachments"] = True
        # Attribute rules do not appear in the standard layer definition; their
        # presence is worth flagging because they are the most likely thing a
        # schema copy silently drops.
        if lp.get("hasAttributeRules") or lp.get("attributeRules"):
            attribute_rules += 1

    detail["relationships"] = relationships
    detail["fields_with_domains"] = sorted(domains)
    detail["subtype_count"] = subtypes
    detail["attribute_rule_layers"] = attribute_rules

    if relationships:
        warnings.append(
            f"{len(relationships)} relationship(s) present -- the Phase 0b spike must "
            f"confirm a schema copy preserves them."
        )
    if attribute_rules:
        warnings.append(
            f"{attribute_rules} layer(s) report attribute rules. These are the most "
            f"likely thing a schema copy drops; verify explicitly."
        )
    if detail["has_attachments"]:
        warnings.append("Attachments are enabled; confirm the copy preserves the setting.")
    return detail, warnings


def describe_view(item: Item) -> tuple[dict[str, Any], list[str]]:
    """Capture a view's real configuration, per layer.

    Per-layer detail matters: `create_view()` takes service-level `query` and
    `visible_fields` conveniences, but a view whose layers filter differently
    cannot be expressed that way and needs per-layer `update_definition()` calls
    after creation. This is what tells us which case we are in.
    """
    detail: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        flc = FeatureLayerCollection.fromitem(item)
    except Exception as exc:
        return {"error": str(exc)}, [f"Could not read view definition: {exc}"]

    detail["capabilities"] = flc.properties.get("capabilities", "")
    detail["is_view"] = bool(flc.properties.get("isView", False))

    layers = []
    queries, field_sets = set(), set()
    for layer in list(flc.layers) + list(flc.tables):
        lp = layer.properties
        all_fields = lp.get("fields", []) or []
        visible = [f["name"] for f in all_fields if f.get("visible", True)]
        hidden = [f["name"] for f in all_fields if not f.get("visible", True)]
        vdq = lp.get("viewDefinitionQuery", "") or ""
        layers.append(
            {
                "id": lp.get("id"),
                "name": lp.get("name"),
                "view_definition_query": vdq,
                "visible_field_count": len(visible),
                "hidden_fields": hidden,
                "capabilities": lp.get("capabilities", ""),
            }
        )
        queries.add(vdq)
        field_sets.add(tuple(sorted(hidden)))

    detail["layers"] = layers
    detail["uniform_query"] = len(queries) <= 1
    detail["uniform_field_visibility"] = len(field_sets) <= 1

    if not detail["uniform_query"]:
        warnings.append(
            "Layers use different definition queries -- create_view()'s service-level "
            "`query` cannot express this; per-layer update_definition() is required."
        )
    if not detail["uniform_field_visibility"]:
        warnings.append(
            "Layers hide different fields -- create_view()'s `visible_fields` cannot "
            "express this; per-layer update_definition() is required."
        )
    return detail, warnings


def describe_experience(item: Item) -> tuple[dict[str, Any], list[str]]:
    """Read an Experience Builder app's datasources via the purpose-built class."""
    detail: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        from arcgis.apps.expbuilder import WebExperience

        exp = WebExperience(item)
        sources = exp.datasources
        detail["datasources"] = sources if isinstance(sources, (dict, list)) else str(sources)
        detail["datasource_count"] = len(sources) if hasattr(sources, "__len__") else None
    except Exception as exc:
        warnings.append(
            f"Could not read datasources via WebExperience ({exc}). Falling back to a "
            f"raw JSON scan for this app; verify its data sources by hand after cloning."
        )
    # datasources reads the draft; a remapped experience must be republished before
    # end users see the corrected sources.
    warnings.append("ExB reads the draft config -- republish after remap and verify both states.")
    return detail, warnings


# ---------------------------------------------------------------- orchestration


def _common_title_prefix(titles: list[str]) -> str:
    """Longest shared prefix across template titles.

    Templates are typically named uniformly ("VSCLR Template - Design View",
    "VSCLR Template - QC View"). Stripping the shared stem turns each title into a
    pattern -- "{base} - Design View" -- which is what the manifest needs. A
    suggestion for a human to check, not a guarantee.
    """
    if len(titles) < 2:
        return ""
    prefix = titles[0]
    for t in titles[1:]:
        while prefix and not t.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


# Ordered longest-first: the shared stem greedily absorbs whatever separator
# follows it, and the pattern has to put that separator back.
_TITLE_SEPARATORS = (" - ", " \u2013 ", " \u2014 ", " | ", ": ", " ")


def _title_pattern(title: str, prefix: str) -> str:
    """Turn one template title into a manifest title pattern.

    The shared stem detected across templates ends up including the separator --
    "VSCLR Template - " rather than "VSCLR Template" -- so stripping it naively
    yields "{base}Design View". Reattaching the separator produces the intended
    "{base} - Design View".
    """
    remainder = title[len(prefix):] if prefix and title.startswith(prefix) else title
    if not remainder:
        return "{base}"
    for sep in _TITLE_SEPARATORS:
        if prefix.endswith(sep):
            return f"{{base}}{sep}{remainder}"
    return f"{{base}}{remainder}"


def _service_name_pattern(title: str, prefix: str) -> str:
    from agol_provision.naming import sanitize_service_name

    remainder = title[len(prefix):] if prefix and title.startswith(prefix) else title
    try:
        suffix = sanitize_service_name(remainder)
    except Exception:
        suffix = ""
    return f"{{base_sn}}_{suffix}" if suffix else "{base_sn}"


def inspect_all(gis: GIS, items: list[Item]) -> list[TemplateItem]:
    """Classify, key, and inspect every template item, then link the graph."""
    known_ids = {i.itemid for i in items}
    taken: set[str] = set()
    results: list[TemplateItem] = []

    # Masters and views first so their keys read naturally in the manifest.
    ordered = sorted(items, key=lambda i: {"master": 0, "view": 1, "map": 2}.get(classify(i), 3))

    for item in ordered:
        role = classify(item)
        key = suggest_key(item, taken)
        taken.add(key)

        detail: dict[str, Any] = {}
        warnings: list[str] = []
        if role == "master":
            detail, warnings = describe_master(item)
        elif role == "view":
            detail, warnings = describe_view(item)
        elif item.type == "Web Experience":
            detail, warnings = describe_experience(item)

        if role == "unknown":
            warnings.append(
                f"Type {item.type!r} has no handler. It will be skipped unless added "
                f"to the manifest by hand."
            )

        results.append(
            TemplateItem(
                item=item,
                role=role,
                key=key,
                depends_on=find_dependencies(item, known_ids),
                detail=detail,
                warnings=warnings,
            )
        )
    return results


def build_manifest_dict(gis: GIS, inspected: list[TemplateItem], name: str) -> dict[str, Any]:
    """Assemble a manifest skeleton with real ids and the derived dependency graph."""
    by_id = {t.item_id: t for t in inspected}
    prefix = _common_title_prefix([t.title for t in inspected])

    masters = [t for t in inspected if t.role == "master"]
    if not masters:
        raise ValueError(
            "No master feature service found among the templates. Expected exactly one "
            "Feature Service without the 'View Service' type keyword."
        )
    if len(masters) > 1:
        raise ValueError(
            "Found more than one candidate master feature service: "
            + ", ".join(f"{m.title} ({m.item_id})" for m in masters)
            + ". Narrow the discovery scope, or build the manifest by hand."
        )
    master = masters[0]

    def refs(t: TemplateItem) -> list[str]:
        return [by_id[d].key for d in t.depends_on if d in by_id]

    manifest: dict[str, Any] = {
        "name": name,
        "version": 1,
        "source_org": gis.url,
        "master": {
            "key": "master",
            "template_item_id": master.item_id,
            "title": "{base}",
            "service_name": "{base_sn}",
        },
        "views": [],
        "groups": [],
        "maps": [],
        "apps": [],
    }

    for t in inspected:
        if t.role == "view":
            manifest["views"].append(
                {
                    "key": t.key,
                    "template_item_id": t.item_id,
                    "title": _title_pattern(t.title, prefix),
                    "service_name": _service_name_pattern(t.title, prefix),
                }
            )
        elif t.role in ("map", "app"):
            manifest["maps" if t.role == "map" else "apps"].append(
                {
                    "key": t.key,
                    "template_item_id": t.item_id,
                    "item_type": t.item.type,
                    "title": _title_pattern(t.title, prefix),
                    "consumes": refs(t),
                    # Sharing cannot be derived: the template's own sharing reflects
                    # template groups, not the per-project groups this tool creates.
                    "share_to": [],
                }
            )
    return manifest


def write_snapshots(inspected: list[TemplateItem], out_dir: Path) -> int:
    """Dump each template's item JSON. Committed to git, these give real diffs.

    This is the concrete replacement for keeping copies in SharePoint: version
    history that can be diffed, on the only representation that matters.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for t in inspected:
        payload = {
            "item_id": t.item_id,
            "title": t.title,
            "type": t.item.type,
            "type_keywords": list(t.item.typeKeywords or []),
            "role": t.role,
            "snippet": t.item.snippet,
            "description": t.item.description,
            "tags": list(t.item.tags or []),
            "data": _safe_json(t.item),
            "detail": t.detail,
        }
        (out_dir / f"{t.key}.json").write_text(json.dumps(payload, indent=2, default=str))
        written += 1
    return written


def _safe_json(item: Item) -> Any:
    try:
        data = item.get_data()
    except Exception as exc:
        return {"_unreadable": str(exc)}
    if isinstance(data, bytes):
        return {"_binary_bytes": len(data)}
    return data


def write_report(gis: GIS, inspected: list[TemplateItem], path: Path) -> None:
    """Human-readable findings, for review before committing to the build."""
    by_id = {t.item_id: t for t in inspected}
    lines: list[str] = [
        "# Template Discovery Report",
        "",
        f"Org: {gis.url}",
        f"Items inspected: {len(inspected)}",
        "",
        "This is generated. Read it before building; it records what the templates",
        "actually contain, which is what the provisioning code has to reproduce.",
        "",
        "## Inventory",
        "",
        "| Key | Title | Type | Role |",
        "| --- | --- | --- | --- |",
    ]
    for t in inspected:
        lines.append(f"| `{t.key}` | {t.title} | {t.item.type} | {t.role} |")

    lines += ["", "## Dependency graph", ""]
    graphed = [t for t in inspected if t.depends_on]
    if not graphed:
        lines += [
            "No inter-item references found. That is suspicious for a template set with",
            "maps and apps -- verify the account can read each item's data JSON.",
            "",
        ]
    for t in graphed:
        names = ", ".join(f"`{by_id[d].key}`" for d in t.depends_on if d in by_id)
        lines.append(f"- `{t.key}` -> {names}")

    orphans = [t for t in inspected if t.role in ("map", "app") and not t.depends_on]
    if orphans:
        lines += [
            "",
            "### Items with no detected dependencies",
            "",
            "These reference no other template item. Either they genuinely stand alone,",
            "or their data JSON could not be read -- worth confirming by hand.",
            "",
        ]
        lines += [f"- `{t.key}` ({t.item.type})" for t in orphans]

    master = next((t for t in inspected if t.role == "master"), None)
    if master:
        d = master.detail
        lines += [
            "",
            "## Master schema",
            "",
            f"- Layers: {d.get('layer_count')}, tables: {d.get('table_count')}",
            f"- Relationships: {len(d.get('relationships', []))}",
            f"- Fields with domains: {len(d.get('fields_with_domains', []))}",
            f"- Subtypes: {d.get('subtype_count')}",
            f"- Attachments enabled: {d.get('has_attachments')}",
            f"- Editor tracking: {d.get('editor_tracking')}",
            f"- Layers reporting attribute rules: {d.get('attribute_rule_layers')}",
            "",
            "Everything above must survive the copy in Phase 0b.",
        ]
        if d.get("relationships"):
            lines += ["", "| Layer | Relationship | Cardinality | Role |", "| --- | --- | --- | --- |"]
            for r in d["relationships"]:
                lines.append(
                    f"| {r['layer']} | {r['name']} | {r['cardinality']} | {r['role']} |"
                )

    views = [t for t in inspected if t.role == "view"]
    if views:
        lines += [
            "",
            "## Views",
            "",
            "`uniform` means every layer in the view shares one definition query and one",
            "hidden-field set. Where it is false, `create_view()`'s service-level",
            "arguments cannot express the configuration and per-layer",
            "`update_definition()` calls are required after creation.",
            "",
            "| Key | Capabilities | Layers | Uniform query | Uniform fields |",
            "| --- | --- | --- | --- | --- |",
        ]
        for t in views:
            d = t.detail
            lines.append(
                f"| `{t.key}` | {d.get('capabilities', '?')} | {len(d.get('layers', []))} "
                f"| {d.get('uniform_query')} | {d.get('uniform_field_visibility')} |"
            )
        for t in views:
            layers = t.detail.get("layers", [])
            if not any(lyr.get("view_definition_query") or lyr.get("hidden_fields") for lyr in layers):
                continue
            lines += ["", f"### `{t.key}`", ""]
            for lyr in layers:
                q = lyr.get("view_definition_query") or "(none)"
                hidden = lyr.get("hidden_fields") or []
                lines.append(f"- **{lyr.get('name')}** (id {lyr.get('id')})")
                lines.append(f"  - query: `{q}`")
                lines.append(f"  - hidden fields: {len(hidden)}")

    flagged = [t for t in inspected if t.warnings]
    lines += ["", "## Warnings", ""]
    if not flagged:
        lines.append("None.")
    for t in flagged:
        lines.append(f"- **`{t.key}`**")
        lines += [f"  - {w}" for w in t.warnings]

    lines += [
        "",
        "## Next steps",
        "",
        "1. Review the generated manifest -- shorten the suggested keys, check the title",
        "   and service-name patterns, and fill in `share_to` for each map and app.",
        "2. Commit the snapshots. They are the version history for these templates.",
        "3. Run the Phase 0b spike to prove the master schema copies faithfully.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))

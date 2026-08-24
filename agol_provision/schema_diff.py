"""Structural comparison of two hosted feature services.

Used by the Phase 0b spike to answer the one question the whole tool rests on:
does copying the master template produce a schema-identical empty service? A copy
that quietly drops relationship classes would not fail loudly -- it would produce
projects that look right until someone tries to use a related table.

Fingerprints are keyed by layer and field *name* rather than id. Ids usually
survive a copy, but names are what the maps, views, and apps actually reference,
so a name change is the breaking change worth catching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Severity = Literal["critical", "warning", "info"]

# A dropped relationship or field silently breaks downstream items; a changed
# service-level capability string is usually intentional and set per view anyway.
_SEVERITY = {
    "layer": "critical",
    "table": "critical",
    "field": "critical",
    "relationship": "critical",
    "geometry": "critical",
    "field_type": "critical",
    "domain": "warning",
    "subtype": "warning",
    "attachments": "warning",
    "index": "warning",
    "editor_tracking": "warning",
    "capabilities": "info",
}


@dataclass(frozen=True)
class Difference:
    kind: str
    where: str
    detail: str

    @property
    def severity(self) -> Severity:
        return _SEVERITY.get(self.kind, "warning")  # type: ignore[return-value]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.kind} @ {self.where}: {self.detail}"


def fingerprint_layer(props: Any) -> dict[str, Any]:
    """Reduce one layer or table definition to the parts that must survive a copy."""
    get = props.get if hasattr(props, "get") else lambda k, d=None: getattr(props, k, d)

    fields = {}
    for f in get("fields", []) or []:
        domain = f.get("domain") or None
        fields[f.get("name")] = {
            "type": f.get("type"),
            "nullable": f.get("nullable"),
            "domain": (domain or {}).get("name") if domain else None,
            "domain_type": (domain or {}).get("type") if domain else None,
        }

    relationships = sorted(
        (
            {
                "name": r.get("name"),
                "cardinality": r.get("cardinality"),
                "role": r.get("role"),
                "key_field": r.get("keyField"),
            }
            for r in (get("relationships", []) or [])
        ),
        key=lambda r: (r["name"] or "", r["role"] or ""),
    )

    return {
        "geometry_type": get("geometryType", None),
        "fields": fields,
        "relationships": relationships,
        "has_attachments": bool(get("hasAttachments", False)),
        "subtype_count": len(get("subtypes", []) or []),
        "index_names": sorted(i.get("name", "") for i in (get("indexes", []) or [])),
    }


def fingerprint_service(flc: Any) -> dict[str, Any]:
    """Reduce a FeatureLayerCollection to a comparable structure."""
    props = flc.properties
    pget = props.get if hasattr(props, "get") else lambda k, d=None: getattr(props, k, d)

    def named(collection: Any) -> dict[str, Any]:
        out = {}
        for lyr in collection or []:
            lp = lyr.properties if hasattr(lyr, "properties") else lyr
            lget = lp.get if hasattr(lp, "get") else lambda k, d=None: getattr(lp, k, d)
            out[lget("name", f"unnamed_{lget('id')}")] = fingerprint_layer(lp)
        return out

    editor = pget("editorTrackingInfo", {}) or {}
    ed_get = editor.get if hasattr(editor, "get") else lambda k, d=None: getattr(editor, k, d)

    return {
        "layers": named(getattr(flc, "layers", [])),
        "tables": named(getattr(flc, "tables", [])),
        "capabilities": pget("capabilities", ""),
        "editor_tracking": bool(ed_get("enableEditorTracking", False)),
    }


def diff_fingerprints(source: dict[str, Any], copy: dict[str, Any]) -> list[Difference]:
    """Everything present in ``source`` but missing or altered in ``copy``."""
    diffs: list[Difference] = []

    for section, kind in (("layers", "layer"), ("tables", "table")):
        src, cpy = source.get(section, {}), copy.get(section, {})

        for name in sorted(set(src) - set(cpy)):
            diffs.append(Difference(kind, name, "present in template, missing from copy"))
        for name in sorted(set(cpy) - set(src)):
            diffs.append(Difference(kind, name, "present in copy, absent from template"))

        for name in sorted(set(src) & set(cpy)):
            diffs.extend(_diff_layer(name, src[name], cpy[name]))

    if source.get("editor_tracking") != copy.get("editor_tracking"):
        diffs.append(
            Difference(
                "editor_tracking",
                "service",
                f"template={source.get('editor_tracking')}, copy={copy.get('editor_tracking')}",
            )
        )
    if source.get("capabilities") != copy.get("capabilities"):
        diffs.append(
            Difference(
                "capabilities",
                "service",
                f"template={source.get('capabilities')!r}, copy={copy.get('capabilities')!r}",
            )
        )
    return diffs


def _diff_layer(name: str, src: dict[str, Any], cpy: dict[str, Any]) -> list[Difference]:
    diffs: list[Difference] = []

    if src.get("geometry_type") != cpy.get("geometry_type"):
        diffs.append(
            Difference("geometry", name,
                       f"{src.get('geometry_type')} -> {cpy.get('geometry_type')}")
        )

    s_fields, c_fields = src.get("fields", {}), cpy.get("fields", {})
    for f in sorted(set(s_fields) - set(c_fields)):
        diffs.append(Difference("field", f"{name}.{f}", "missing from copy"))
    for f in sorted(set(s_fields) & set(c_fields)):
        s, c = s_fields[f], c_fields[f]
        if s["type"] != c["type"]:
            diffs.append(
                Difference("field_type", f"{name}.{f}", f"{s['type']} -> {c['type']}")
            )
        if s["domain"] != c["domain"]:
            diffs.append(
                Difference("domain", f"{name}.{f}",
                           f"domain {s['domain']!r} -> {c['domain']!r}")
            )

    s_rels = {(r["name"], r["role"]): r for r in src.get("relationships", [])}
    c_rels = {(r["name"], r["role"]): r for r in cpy.get("relationships", [])}
    for key in sorted(set(s_rels) - set(c_rels)):
        diffs.append(
            Difference("relationship", name,
                       f"relationship {key[0]!r} (role {key[1]}) missing from copy")
        )
    for key in sorted(set(s_rels) & set(c_rels)):
        if s_rels[key]["cardinality"] != c_rels[key]["cardinality"]:
            diffs.append(
                Difference("relationship", name,
                           f"{key[0]!r} cardinality "
                           f"{s_rels[key]['cardinality']} -> {c_rels[key]['cardinality']}")
            )

    if src.get("has_attachments") != cpy.get("has_attachments"):
        diffs.append(
            Difference("attachments", name,
                       f"{src.get('has_attachments')} -> {cpy.get('has_attachments')}")
        )
    if src.get("subtype_count") != cpy.get("subtype_count"):
        diffs.append(
            Difference("subtype", name,
                       f"{src.get('subtype_count')} -> {cpy.get('subtype_count')} subtypes")
        )
    missing_idx = set(src.get("index_names", [])) - set(cpy.get("index_names", []))
    if missing_idx:
        diffs.append(
            Difference("index", name, f"missing indexes: {', '.join(sorted(missing_idx))}")
        )
    return diffs


def summarize(diffs: list[Difference]) -> str:
    """Verdict line for the spike report."""
    if not diffs:
        return "IDENTICAL - the copy reproduces the template schema exactly."
    critical = [d for d in diffs if d.severity == "critical"]
    warning = [d for d in diffs if d.severity == "warning"]
    if critical:
        return (
            f"NOT USABLE - {len(critical)} critical difference(s). "
            f"A schema copy loses structure the project depends on; use the "
            f"publish-from-FGDB fallback instead."
        )
    return (
        f"USABLE WITH FIXUPS - no critical differences, but {len(warning)} setting(s) "
        f"must be reapplied after copying."
    )

"""Find out which layer AGOL refuses when the copy's layer post fails.

The copy posts every layer definition in one `addToDefinition` call, and when
AGOL rejects it the message names a .NET type (`Invalid definition for
List[LayerCoreInfo]`), not a layer or a property. The first template this
happened on had 19 layers and a table, any of which could have been the one.
It was the two with relationships -- found by exactly this probe -- and the
copy now strips those before posting. The probe stays for the next thing.

The probe gets AGOL to say which. It posts each definition alone, in template
order, so each answer is about one layer. A rejected layer is retried after the
rest are in, because a relationship pointing at a table that is not yet in the
service is refused for ordering rather than for its definition. Anything still
refused is posted again with one suspect group removed at a time -- the first
version AGOL accepts names the property. Every post is atomic on AGOL's side, so
a refused attempt leaves nothing behind and no layer is ever removed.

The payload is built the way `master.layer_definitions` builds it, so the probe
tests what the copy actually sent.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from agol_provision.master import LAYER_STRIPS

Adder = Callable[[dict[str, Any]], Any]

# Tried one at a time on a layer AGOL keeps refusing, most likely first. Each
# entry is (label, top-level keys to drop, per-field keys to drop). Anything the
# copy already strips is a no-op here and skipped.
SUSPECTS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("relationships", ("relationships",), ()),
    ("subtypes", ("subtypes", "subtypeField", "defaultSubtypeCode"), ()),
    ("templates and types", ("templates", "types"), ()),
    ("drawingInfo", ("drawingInfo",), ()),
    ("field domains", (), ("domain",)),
    ("field defaults", (), ("defaultValue",)),
    ("editFieldsInfo", ("editFieldsInfo",), ()),
)


@dataclass
class ProbeResult:
    name: str
    kind: str  # "layer" or "table"
    accepted: bool
    error: str | None = None
    retried: bool = False
    culprit: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def layer_payload(layer: Any) -> dict[str, Any]:
    """The definition the copy posts for this layer: a deep copy, so the
    stripping never touches the template."""
    payload = copy.deepcopy(dict(layer.manager.properties))
    for key in LAYER_STRIPS:
        payload.pop(key, None)
    return payload


def _strip(payload: dict[str, Any], keys: tuple[str, ...], field_keys: tuple[str, ...]) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    for key in keys:
        out.pop(key, None)
    if field_keys:
        for fld in out.get("fields", []) or []:
            for key in field_keys:
                fld.pop(key, None)
    return out


def _attempt(add: Adder, kind: str, payload: dict[str, Any]) -> str | None:
    """Post one definition. Returns AGOL's message on refusal, None on success."""
    try:
        res = add({f"{kind}s": [payload]})
    except Exception as exc:
        return str(exc)
    if isinstance(res, dict) and res.get("success") is False:
        return str(res.get("error", res))
    return None


def probe(layers: list[Any], tables: list[Any], add: Adder) -> list[ProbeResult]:
    """Post each definition alone through `add` and report what AGOL said."""
    results: list[ProbeResult] = []
    for kind, items in (("layer", layers), ("table", tables)):
        for lyr in items:
            payload = layer_payload(lyr)
            name = str(payload.get("name", payload.get("id", "?")))
            error = _attempt(add, kind, payload)
            results.append(ProbeResult(name, kind, error is None, error, payload=payload))

    # Second pass: what was refused for ordering is accepted now that its
    # relationship targets are in.
    for r in results:
        if r.accepted:
            continue
        r.retried = True
        error = _attempt(add, r.kind, r.payload)
        if error is None:
            r.accepted, r.error = True, None

    # Anything still refused: drop one suspect at a time until AGOL accepts it.
    for r in results:
        if r.accepted:
            continue
        for label, keys, field_keys in SUSPECTS:
            stripped = _strip(r.payload, keys, field_keys)
            if stripped == r.payload:
                continue  # the layer has none of this; nothing to learn
            if _attempt(add, r.kind, stripped) is None:
                r.culprit = label
                break
    return results

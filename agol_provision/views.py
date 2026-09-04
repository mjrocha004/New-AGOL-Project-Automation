"""Stage 2: hosted feature layer views, created natively from the new master.

This is the stage that carries the time saving. Creating the four groups by hand
takes a minute; creating the seven views by hand takes a day.

**Views are created, never cloned.** Cloning a hosted feature layer view within a
single organization is unreliable -- it can produce an empty service, or silently
re-point at the source. `create_view()` is deterministic, so the template view's
real configuration is read live at provision time and replayed against the new
master. Reading it live rather than storing it in the manifest means editing a
template view in AGOL propagates to the next project with no manifest edit.

**`create_view()`'s own `query` argument is not used, and must not be.** It
applies the query to `flc.layers[0]` alone -- and rewrites that one layer's field
visibility as a side effect -- so on a view spanning eighteen layers the other
seventeen would be created unfiltered. These views are shared to subcontractors,
so an unfiltered layer leaks data rather than merely showing the wrong rows.
Every definition query is applied per layer instead, for every view, whether or
not the template's queries are uniform.

No `visible_fields` handling exists. Field visibility is uniform across all seven
template views and none hides a field, so the path is not built on spec. If a
template starts hiding fields, discovery reports `Uniform fields: False` and this
module gets revisited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# `create_view()` returns before ArcGIS Online has finished adding the layers: it
# calls `add_to_definition(..., future=True)` and discards the Future. The
# schedule below tops out around 170s, which is generous next to the largest view
# (18 layers). Fixed rather than randomised, so a run is reproducible.
LAYER_WAIT_BACKOFF: tuple[float, ...] = (0.0, 2.0, 3.0, 5.0, 8.0) + (10.0,) * 15

APPLIED = "applied"
MISSING = "missing"
FAILED = "failed"


class ViewError(RuntimeError):
    """The view cannot be built from this master."""


@dataclass(frozen=True)
class LayerView:
    """One layer of a template view, and the filter it carries."""

    layer_id: int
    name: str
    query: str  # "" when the layer is unfiltered


@dataclass(frozen=True)
class QueryOutcome:
    layer: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ViewPlan:
    """Everything about a template view that has to be replayed."""

    capabilities: str
    layers: list[LayerView]
    tables: list[LayerView]

    @property
    def uniform_query(self) -> bool:
        """Whether every layer filters the same way.

        Reported for the run log only. Queries are applied per layer either way,
        because `create_view(query=...)` reaches just the first layer.
        """
        return len({lv.query for lv in [*self.layers, *self.tables]}) <= 1

    @property
    def filtered(self) -> list[LayerView]:
        return [lv for lv in [*self.layers, *self.tables] if lv.query]


def read_template_view(template_view: Any) -> ViewPlan:
    """Read a template view's capabilities, layer subset, and per-layer queries.

    Takes a FeatureLayerCollection -- capabilities live on the service, not on
    the item.
    """
    return ViewPlan(
        capabilities=str(template_view.properties.get("capabilities", "") or ""),
        layers=[_layer_view(lyr) for lyr in template_view.layers],
        tables=[_layer_view(tbl) for tbl in template_view.tables],
    )


def _layer_view(layer: Any) -> LayerView:
    props = layer.properties
    return LayerView(
        layer_id=props.get("id"),
        name=str(props.get("name", "")),
        query=str(props.get("viewDefinitionQuery", "") or ""),
    )


def _names_of(service: Any) -> set[str]:
    """Layer and table names a service currently reports. Tolerates duplicates."""
    return {
        str(layer.properties.get("name", ""))
        for layer in [*service.layers, *service.tables]
    }


def wait_for_layers(
    fetch: Any,
    expected_names: set[str],
    *,
    sleep: Any,
    backoff: tuple[float, ...] = LAYER_WAIT_BACKOFF,
) -> Any:
    """Block until a newly created view really exposes every layer it should.

    `create_view()` adds a view's layers with an asynchronous server job and
    throws away the Future that tracks it, so the item it returns can describe a
    service with no layers at all. Reading it immediately -- as the first live run
    did -- finds nothing and reports every layer missing, moments before they all
    appear.

    ``fetch`` must build a **new** service object each call. A retained
    `FeatureLayerCollection` cannot show the change: its `.layers` is a plain
    attribute assigned once during construction, not a property.

    A fetch that raises is treated as "not ready yet" rather than fatal, because a
    half-built service can answer with an error instead of an empty list.
    """
    seen: set[str] = set()
    for delay in backoff:
        if delay:
            sleep(delay)
        try:
            service = fetch()
            seen = _names_of(service)
        except Exception:
            continue
        if expected_names <= seen:
            return service

    waited = sum(backoff)
    missing = sorted(expected_names - seen)
    raise ViewError(
        f"Waited {waited:.0f}s and the new view still does not report "
        f"{', '.join(repr(n) for n in missing)}. It reports: "
        f"{', '.join(sorted(seen)) or '(nothing)'}. AGOL adds a view's layers "
        f"asynchronously, so this is either an unusually slow job or a failed one "
        f"-- the item exists and is recorded, so `--destroy` will clean it up."
    )


def _by_name(service: Any) -> dict[str, Any]:
    """Layers and tables of a service, keyed by name.

    Name is the correspondence between a template view and the new master, not
    layer id. `copy_feature_layer_collection()` renumbers the copy's layers, so a
    template view's layer id names a *different* layer on the new master -- id 11
    was `Redline_Details` on the template and `Pole_Design` on the copy. Names
    survive the copy; stage 1 checks that on every layer before touching indexes.
    """
    found: dict[str, Any] = {}
    for layer in [*service.layers, *service.tables]:
        name = str(layer.properties.get("name", ""))
        if name in found:
            raise ViewError(
                f"Layer name {name!r} appears twice in the service. Names are how a "
                f"view's layers are matched to the master's, so this is ambiguous. "
                f"Rename one of them before provisioning."
            )
        found[name] = layer
    return found


def source_layers(new_master: Any, plan: ViewPlan) -> tuple[list[Any], list[Any]]:
    """The new master's layer objects this view is built from, in the view's order.

    `create_view()` takes layer *objects* and reads each one's id off its manager,
    so the ids the new views carry come from the new master and stay
    self-consistent. What has to be resolved here is *which* of the master's
    layers each of the template view's layers means -- and that is by name.
    """
    available = _by_name(new_master)

    def resolve(wanted: list[LayerView]) -> list[Any]:
        found = []
        for lv in wanted:
            layer = available.get(lv.name)
            if layer is None:
                raise ViewError(
                    f"The new master has no layer named {lv.name!r}, which this view "
                    f"exposes. Either the master is incomplete, or the template view "
                    f"renames that layer -- views can. The master has: "
                    f"{', '.join(sorted(available))}."
                )
            found.append(layer)
        return found

    return resolve(plan.layers), resolve(plan.tables)


def apply_definition_queries(new_view: Any, plan: ViewPlan) -> list[QueryOutcome]:
    """Set every filtered layer's `viewDefinitionQuery` on the newly created view.

    Done for every view rather than only the ones whose queries differ per layer,
    because the service-level shortcut reaches only the first layer. A layer the
    template leaves unfiltered is left alone. Matched by name, for the same reason
    `source_layers` is: the new view's layer ids come from the new master's, which
    are not the template's.
    """
    available = _by_name(new_view)
    outcomes: list[QueryOutcome] = []

    for lv in plan.filtered:
        layer = available.get(lv.name)
        if layer is None:
            outcomes.append(QueryOutcome(lv.name, MISSING,
                                         "no layer of that name in the new view"))
            continue
        try:
            layer.manager.update_definition({"viewDefinitionQuery": lv.query})
        except Exception as exc:
            outcomes.append(QueryOutcome(lv.name, FAILED, " ".join(str(exc).split())))
        else:
            outcomes.append(QueryOutcome(lv.name, APPLIED))

    return outcomes


def service_of(item: Any) -> Any:
    """The FeatureLayerCollection behind a feature service item.

    Service capabilities and `create_view()` live on the collection rather than
    on the item. This is the module's only arcgis dependency, isolated so the
    rest stays testable against fakes.
    """
    from arcgis.features import FeatureLayerCollection

    return FeatureLayerCollection.fromitem(item)


# ---------------------------------------------------------------- the sync path

# `create_view()` posts the view's layers with `add_to_definition(..., future=True)`
# and throws the Future away, so a job that fails server-side is invisible from
# the client: it hands back an item, and the service stays empty forever. That is
# not hypothetical -- one 18-layer view came back empty and was still empty hours
# later, with no error anywhere.
#
# These two functions rebuild the same definition and post it synchronously, which
# is what `copy_feature_layer_collection()` does for the master and why stage 1
# has never had this class of failure. Either the layers arrive, or AGOL says why.


def source_service_name(master_service: Any) -> str:
    """The master's service name, taken from its own URL.

    Split on "/" rather than with `os.path`, which is what arcgis uses -- the URL
    is not a filesystem path and this code runs on Windows.
    """
    parts = [p for p in str(master_service.url).split("/") if p]
    if len(parts) < 2:
        raise ViewError(f"Cannot read a service name from {master_service.url!r}.")
    return parts[-2]


def build_view_definition(master_service: Any, plan: ViewPlan) -> dict:
    """The `addToDefinition` payload for a view over `plan`'s layers.

    Each entry points at the layer id **on the new master**, not the template's --
    the copy renumbers, so the template's ids name different layers.

    `popupInfo` is omitted rather than sent as null, which is what `create_view()`
    does. A null there is one more thing AGOL can object to, and nothing in this
    project sets popups.
    """
    service_name = source_service_name(master_service)
    available = _by_name(master_service)

    def entry(lv: LayerView, is_table: bool) -> dict:
        layer = available[lv.name]
        layer_id = layer.properties.get("id")
        definition = {
            "id": layer_id,
            "name": lv.name,
            "adminLayerInfo": {
                "viewLayerDefinition": {
                    "sourceServiceName": service_name,
                    "sourceLayerId": layer_id,
                    "sourceLayerFields": "*",
                },
            },
        }
        if is_table:
            definition["type"] = "Table"
        return definition

    return {
        "layers": [entry(lv, False) for lv in plan.layers],
        "tables": [entry(lv, True) for lv in plan.tables],
    }


def add_view_layers(new_view: Any, definition: dict) -> dict:
    """Post a view's layer definition synchronously, so a failure is visible."""
    try:
        result = new_view.manager.add_to_definition(definition, future=False)
    except Exception as exc:
        raise ViewError(
            f"Adding the view's layers failed: {' '.join(str(exc).split())}"
        ) from exc

    if isinstance(result, dict) and result.get("success") is False:
        raise ViewError(
            f"Adding the view's layers was rejected: "
            f"{result.get('error', result)}"
        )
    return result if isinstance(result, dict) else {"success": True}

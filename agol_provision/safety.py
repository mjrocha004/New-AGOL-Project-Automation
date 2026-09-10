"""Guards on the one destructive operation this tool performs.

`spike-master` creates a temporary feature service and deletes it again. That is
the only delete in the codebase. The code path makes it structurally impossible to
delete anything else -- the deleted item is whatever the copy call just returned --
but "structurally impossible if you read the code" is not the same as checked.

These functions check it. If anything about the item is not what the spike
created, the delete is refused and the item is left in place: an orphaned test
service is a nuisance, deleting the wrong thing is not.
"""

from __future__ import annotations

from typing import Any

# Every service the spike creates carries this prefix, so an item lacking it was
# not created by this run and must not be deleted by it.
SPIKE_PREFIX = "ZZZ_SPIKE_TEST_"


def spike_service_name(master_id: str) -> str:
    """Deterministic name for the spike's temporary service.

    Deliberately stable across runs: AGOL reserves a hosted service name
    permanently, even after the service is deleted, so a fresh name each run would
    burn a new one every time.
    """
    return f"{SPIKE_PREFIX}{master_id[:8]}"


def refuse_delete_reason(
    candidate: Any,
    *,
    template_id: str,
    expected_name: str,
) -> str | None:
    """Return why ``candidate`` must NOT be deleted, or None if it is safe.

    Checked rather than assumed, because the cost of being wrong is asymmetric:
    a leftover test service is cleaned up in ten seconds, a deleted master
    feature service is not.
    """
    item_id = getattr(candidate, "itemid", None)
    if not item_id:
        return "it has no item id"

    if item_id == template_id:
        return "it is the template itself"

    if not expected_name.startswith(SPIKE_PREFIX):
        return f"the expected name {expected_name!r} is not a spike name"

    haystack = " ".join(
        str(getattr(candidate, attr, "") or "")
        for attr in ("title", "name", "url")
    )
    if expected_name not in haystack:
        return (
            f"its name does not contain {expected_name!r} -- this is not the "
            f"service the spike created"
        )

    return None


def _is_spike_title(title: str, expected_name: str) -> bool:
    """The spike's exact name, or the `_N` copy_feature_layer_collection() appends
    when that name is already taken."""
    if title == expected_name:
        return True
    rest = title[len(expected_name):] if title.startswith(expected_name) else ""
    return rest.startswith("_") and rest[1:].isdigit()


def find_abandoned_spike(gis: Any, expected_name: str, *, since_ms: int) -> Any | None:
    """Recover the service the copy created and then lost.

    `copy_feature_layer_collection()` creates the empty service first and posts
    the layer definitions second; when AGOL refuses the definitions, the method
    raises and the item it created goes with the exception. This finds it again
    so the spike's guarded delete can run, and is narrower than the guard: the
    spike's own name (or the library's `_N` suffix), owned by this account, and
    created after `since_ms` -- a leftover from an earlier run is not ours to
    touch, whatever it is called. The newest match is the one this run made.
    """
    if not expected_name.startswith(SPIKE_PREFIX):
        return None
    me = gis.users.me.username
    query = f'title:{expected_name} AND owner:{me} AND type:"Feature Service"'
    candidates = []
    for item in gis.content.search(query, max_items=50):
        if getattr(item, "owner", None) != me:
            continue
        if getattr(item, "type", None) != "Feature Service":
            continue
        if not _is_spike_title(str(getattr(item, "title", "") or ""), expected_name):
            continue
        created = getattr(item, "created", None) or 0
        if created < since_ms:
            continue
        candidates.append((created, item))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]

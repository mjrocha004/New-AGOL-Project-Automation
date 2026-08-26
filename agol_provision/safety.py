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

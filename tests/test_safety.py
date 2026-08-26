"""The spike performs the only delete in this codebase. These are the guards.

The costs are asymmetric: a leftover test service is a ten-second cleanup, a
wrongly deleted master feature service is not. So the guard defaults to refusing.
"""

import pytest

from agol_provision.safety import SPIKE_PREFIX, refuse_delete_reason, spike_service_name

MASTER = "abc123def456abc123def456abc123de"


class FakeItem:
    def __init__(self, itemid, title="", name="", url=""):
        self.itemid, self.title, self.name, self.url = itemid, title, name, url


@pytest.fixture
def spike_name():
    return spike_service_name(MASTER)


class TestSpikeName:
    def test_carries_the_prefix(self, spike_name):
        assert spike_name.startswith(SPIKE_PREFIX)

    def test_is_stable_across_runs(self):
        """AGOL reserves a service name permanently, even after deletion. A fresh
        name per run would burn a new one every time."""
        assert spike_service_name(MASTER) == spike_service_name(MASTER)

    def test_differs_per_master(self):
        assert spike_service_name(MASTER) != spike_service_name("f" * 32)


class TestDeleteIsAllowed:
    def test_the_service_the_spike_created(self, spike_name):
        item = FakeItem("newitem", title=spike_name, name=spike_name)
        assert refuse_delete_reason(item, template_id=MASTER, expected_name=spike_name) is None

    def test_name_matching_on_url_alone_is_enough(self, spike_name):
        item = FakeItem("newitem", url=f"https://x/rest/services/{spike_name}/FeatureServer")
        assert refuse_delete_reason(item, template_id=MASTER, expected_name=spike_name) is None


class TestDeleteIsRefused:
    def test_never_the_template_itself(self, spike_name):
        """The case that matters. Even if it somehow carried the spike name."""
        item = FakeItem(MASTER, title=spike_name, name=spike_name)
        reason = refuse_delete_reason(item, template_id=MASTER, expected_name=spike_name)
        assert reason == "it is the template itself"

    def test_never_an_unrelated_existing_service(self, spike_name):
        item = FakeItem("someid", title="CompanyA Moline", name="CompanyA_Moline")
        reason = refuse_delete_reason(item, template_id=MASTER, expected_name=spike_name)
        assert reason and "not the service the spike created" in reason

    def test_never_an_item_without_an_id(self, spike_name):
        assert refuse_delete_reason(
            FakeItem("", title=spike_name), template_id=MASTER, expected_name=spike_name
        )

    def test_never_when_the_expected_name_is_not_a_spike_name(self):
        """Guards against a caller passing a real service name by mistake."""
        item = FakeItem("newitem", title="CompanyA_Moline", name="CompanyA_Moline")
        reason = refuse_delete_reason(
            item, template_id=MASTER, expected_name="CompanyA_Moline"
        )
        assert reason and "not a spike name" in reason

    def test_a_similar_but_different_spike_name_is_refused(self, spike_name):
        """Two masters produce two spike names; one run must not delete the other."""
        other = spike_service_name("f" * 32)
        item = FakeItem("newitem", title=other, name=other)
        assert refuse_delete_reason(item, template_id=MASTER, expected_name=spike_name)


class TestOnlyOneDeleteExists:
    def test_codebase_contains_exactly_one_item_delete(self):
        """A second delete path would bypass these guards entirely."""
        from pathlib import Path

        pkg = Path(__file__).resolve().parent.parent / "agol_provision"
        hits = []
        for f in pkg.rglob("*.py"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                stripped = line.strip()
                if ".delete()" in stripped and not stripped.startswith("#"):
                    hits.append(f"{f.name}:{n}")
        assert len(hits) == 1, f"expected exactly one .delete() call, found: {hits}"
        assert hits[0].startswith("cli.py:"), f"delete moved out of cli.py: {hits[0]}"

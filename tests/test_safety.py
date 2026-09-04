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


class TestEveryDeleteIsGuarded:
    """Deletes may live only where something checks what is being deleted.

    `spike_master` deletes the one service it just created, checked by
    `refuse_delete_reason`. `_destroy` deletes only item ids recorded in run
    state, so it can never touch anything this tool merely found. A delete
    anywhere else would bypass both guards, so adding one has to be a deliberate
    edit to this list rather than something that slips in.
    """

    GUARDED = {
        "spike_master": "refuse_delete_reason checks the item is the spike's own copy",
        "_destroy": "walks run state, so only recorded item ids are reachable",
    }

    def _delete_sites(self):
        import ast
        from pathlib import Path

        pkg = Path(__file__).resolve().parent.parent / "agol_provision"
        sites = {}
        for f in sorted(pkg.rglob("*.py")):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "delete"
                        and not call.args
                    ):
                        sites.setdefault(node.name, []).append(f"{f.name}:{call.lineno}")
        return sites

    def test_deletes_appear_only_in_the_guarded_functions(self):
        sites = self._delete_sites()
        assert set(sites) == set(self.GUARDED), (
            f"delete() found in {sorted(sites)}; only {sorted(self.GUARDED)} may "
            f"delete. A new delete path needs its own guard and an entry here."
        )

    def test_the_guarded_functions_are_where_they_are_expected(self):
        sites = self._delete_sites()
        assert all(
            where.startswith("cli.py:") for wheres in sites.values() for where in wheres
        ), sites


class TestFailingActuallyExits:
    """A failed run must end, not hang.

    `create_view()` starts a ThreadPoolExecutor whose worker polls an AGOL job in
    a `while True` loop with no timeout, and never keeps the Future. Those threads
    are non-daemon, so the interpreter joins them on the way out. After stage 2
    gave up on a stalled view, the process sat there for an hour and a half with
    nothing left to do.
    """

    def test_fail_releases_the_threads_the_interpreter_would_join(self):
        import concurrent.futures.thread as cf_thread
        import pytest

        from agol_provision.cli import _fail

        class Poller:  # _threads_queues is a WeakKeyDictionary
            pass

        marker = Poller()
        cf_thread._threads_queues[marker] = "a poller that never returns"
        try:
            with pytest.raises(SystemExit):
                _fail("stopping")
            assert marker not in cf_thread._threads_queues
        finally:
            cf_thread._threads_queues.pop(marker, None)

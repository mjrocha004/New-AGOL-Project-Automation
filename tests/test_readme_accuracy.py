"""Keep the README honest about the CLI.

Docs drift silently -- a renamed flag leaves instructions that look right and
fail on the machine of whoever follows them. These assertions are cheap and catch
it at commit time.
"""

import re
from pathlib import Path

import click
import pytest

from agol_provision.cli import main

README = Path(__file__).resolve().parent.parent / "README.md"
INVOCATION = re.compile(r"python -m agol_provision\.cli (\S+)((?: [^\n`]*)?)")


@pytest.fixture(scope="module")
def invocations():
    return INVOCATION.findall(README.read_text())


def test_readme_exists_and_is_substantial():
    assert README.exists() and len(README.read_text()) > 2000


def test_every_documented_command_exists(invocations):
    unknown = {name for name, _ in invocations} - set(main.commands)
    assert not unknown, f"README documents commands that do not exist: {sorted(unknown)}"


def test_every_documented_flag_exists(invocations):
    problems = []
    for name, rest in invocations:
        cmd = main.commands.get(name)
        if cmd is None:
            continue
        valid = {o for p in cmd.params if isinstance(p, click.Option) for o in p.opts}
        for flag in re.findall(r"(--[a-z][a-z-]*)", rest):
            if flag not in valid:
                problems.append(f"{name} {flag}")
    assert not problems, f"README uses flags that do not exist: {sorted(set(problems))}"


def test_every_command_appears_somewhere(invocations):
    """A command nobody can find is a command nobody uses."""
    missing = set(main.commands) - {name for name, _ in invocations}
    assert not missing, f"commands never shown in the README: {sorted(missing)}"


def test_stated_test_count_matches_reality():
    """The README claims a test count; drift there erodes trust in the rest."""
    claimed = re.search(r"(\d+) tests pass", README.read_text())
    assert claimed, "README no longer states a test count"

    root = README.parent / "tests"
    collected = sum(
        len(re.findall(r"^\s*def test_", f.read_text(), re.M))
        for f in root.glob("test_*.py")
    )
    # Parametrized cases expand beyond the def count, so the claim should be at
    # least the number of test functions -- never fewer.
    assert int(claimed.group(1)) >= collected, (
        f"README claims {claimed.group(1)} tests but there are at least "
        f"{collected} test functions"
    )


def test_save_ids_format_is_described_accurately():
    """This description went stale once already when the format gained a role tag."""
    import tempfile

    from agol_provision.discovery import write_id_file

    class FakeItem:
        itemid, title, type, typeKeywords = "a" * 32, "T", "Feature Service", []

    path = Path(tempfile.mkdtemp()) / "ids.txt"
    write_id_file([FakeItem()], path)
    body = path.read_text()

    assert "# [master " in body, "role tag missing from the written file"
    readme = README.read_text()
    assert "[role" in readme, "README no longer describes the role tag it writes"

"""Guard against a click option that is declared but never used.

A no-op flag is worse than a missing one: it is accepted without complaint, so
the user believes it took effect. This caught `discover --save-ids` silently
doing nothing.
"""

import inspect

import pytest

from agol_provision.cli import main


def _commands():
    return sorted(main.commands.items())


@pytest.mark.parametrize("name,cmd", _commands(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_option_is_used_in_its_command_body(name, cmd):
    body = inspect.getsource(cmd.callback)
    # Strip the decorators, which mention every parameter by definition.
    body = body[body.index("def "):]

    unused = [
        p.name for p in cmd.params
        if p.name not in ("help",) and body.count(p.name) < 2
    ]
    assert not unused, (
        f"{name} declares {unused} but never uses them in its body -- "
        f"the flag would be accepted and silently ignored"
    )


@pytest.mark.parametrize("name,cmd", _commands(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_option_has_help_text(name, cmd):
    import click

    missing = [
        p.name for p in cmd.params
        if isinstance(p, click.Option) and not p.help and p.name != "help"
    ]
    assert not missing, f"{name} options lack help text: {missing}"

"""Connecting to ArcGIS Online.

The default is ``GIS("home")``, which borrows whichever portal ArcGIS Pro is
signed in to. Nothing is typed, prompted for, or stored, and it works with
SAML/SSO accounts that cannot accept a password from a script. Pro does not need
to be running -- the sign-in token persists -- but this reads the token through
``arcpy``, so commands must run in ArcGIS Pro's Python environment.

Outside a hosted ArcGIS Notebook the ArcGIS API rewrites ``home`` to ``pro``, so
the two are interchangeable on a desktop. Inside a notebook, ``home`` uses the
notebook's identity instead.

Any other ``--profile`` value is treated as a stored ArcGIS profile: username and
org URL in ``~/.arcgisprofile``, password in the OS keyring. That is the fallback
for a machine without ArcGIS Pro, or an unattended run.
"""

from __future__ import annotations

from arcgis.gis import GIS

# `remap_data()` -- which rewires item ids inside cloned maps, dashboards, and
# experiences -- was added in 2.4.0. Below that, provisioning cannot wire up the
# apps. Checked at runtime rather than assumed, because a machine using ArcGIS
# Pro's bundled Python gets whichever version Pro shipped.
MIN_ARCGIS_VERSION = (2, 4, 0)

# Provisioning creates content, groups, and views, and shares items to groups.
# Checking up front turns a confusing mid-run 403 into a clear message.
REQUIRED_PRIVILEGES = {
    "portal:user:createItem": "create items",
    "portal:user:createGroup": "create groups",
    "portal:publisher:publishFeatures": "publish hosted feature layers",
    "portal:user:shareToGroup": "share items to groups",
}


class AuthError(RuntimeError):
    """Cannot connect, or the account lacks a privilege provisioning needs."""


# Passed positionally to GIS() rather than as `profile=`. Outside a hosted
# notebook the ArcGIS API rewrites "home" to "pro", so both mean the same thing
# on a desktop.
PRO_PROFILES = frozenset({"pro", "home"})


def uses_arcgis_pro(profile: str) -> bool:
    """Whether this profile borrows ArcGIS Pro's signed-in connection."""
    return profile.strip().lower() in PRO_PROFILES


def connect(profile: str) -> GIS:
    """Open a GIS connection from a stored profile, or from ArcGIS Pro."""
    if uses_arcgis_pro(profile):
        return _connect_via_pro(profile.strip().lower())

    try:
        gis = GIS(profile=profile)
    except Exception as exc:
        raise AuthError(
            f"Could not connect using profile {profile!r}: {exc}\n"
            f"Create it with: agol-provision setup-profile --name {profile} "
            f"--org https://yourorg.maps.arcgis.com --username YOURUSER\n"
            f"Or drop --profile entirely to use the ArcGIS Pro connection."
        ) from exc

    if gis.users.me is None:
        raise AuthError(
            f"Profile {profile!r} connected anonymously. Provisioning needs an "
            f"authenticated account."
        )
    return gis


def check_privileges(gis: GIS, *, strict: bool = True) -> list[str]:
    """Return the human-readable names of any missing privileges.

    Raises when ``strict`` and anything is missing, so a run fails before creating
    a partial project rather than at the first stage that needs the privilege.
    """
    held = set(getattr(gis.users.me, "privileges", []) or [])
    missing = [label for priv, label in REQUIRED_PRIVILEGES.items() if priv not in held]

    if missing and strict:
        raise AuthError(
            f"{gis.users.me.username} cannot: {', '.join(missing)}.\n"
            f"Provisioning would fail partway through and leave a partial project. "
            f"Ask an org administrator to grant these, or use a service account."
        )
    return missing


def describe(gis: GIS) -> str:
    """One-line connection summary for command output."""
    me = gis.users.me
    return f"{me.username} ({me.role}) @ {gis.url}"


def parse_version(raw: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple.

    Stops at the first non-numeric component so pre-release and build suffixes
    ("2.4.0rc1", "2.4.0+local") compare as their release version rather than
    failing outright.
    """
    parts: list[int] = []
    for chunk in str(raw).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def arcgis_version() -> tuple[int, ...]:
    """Installed ArcGIS API version as a comparable tuple."""
    import arcgis

    return parse_version(arcgis.__version__)


def check_arcgis_version(*, strict: bool = True) -> str | None:
    """Return a problem description if the installed API is too old.

    Raises when ``strict``. This fails early because the symptom otherwise appears
    at stage 4 of a live run, as an AttributeError on an item object.
    """
    import arcgis

    installed = arcgis_version()
    if installed >= MIN_ARCGIS_VERSION:
        return None

    want = ".".join(str(n) for n in MIN_ARCGIS_VERSION)
    problem = (
        f"arcgis {arcgis.__version__} is installed, but {want} or newer is required. "
        f"remap_data() was added in {want}; without it, cloned maps and apps cannot be "
        f"repointed at the new project's layers."
    )
    if strict:
        raise AuthError(problem)
    return problem


def _connect_via_pro(mode: str) -> GIS:
    """Borrow ArcGIS Pro's active portal connection.

    Requires ``arcpy``, because the ArcGIS API reads the token through
    ``arcpy.GetSigninToken()``. That means running inside ArcGIS Pro's own Python
    environment -- a standalone virtual environment will not have it, and the
    underlying error ("the arcpy library could not be found") does not explain
    why.
    """
    try:
        return GIS(mode)
    except ImportError as exc:
        raise AuthError(
            f"--profile {mode} needs ArcGIS Pro's Python environment, and arcpy is not "
            f"available here ({exc}).\n\n"
            f"Either run this from a clone of Pro's conda environment, or use a stored "
            f"profile instead:\n"
            f"  agol-provision setup-profile --name vsclr --org https://yourorg.maps.arcgis.com --username YOURUSER"
        ) from exc
    except Exception as exc:
        raise AuthError(
            f"Could not borrow the ArcGIS Pro connection ({exc}).\n"
            f"Open ArcGIS Pro and confirm it is signed in to the right organization, "
            f"then try again."
        ) from exc


def arcpy_available() -> bool:
    """Whether arcpy can be imported, i.e. whether --profile pro can work."""
    try:
        import arcpy  # noqa: F401

        return True
    except Exception:
        return False

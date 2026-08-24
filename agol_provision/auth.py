"""Connecting to ArcGIS Online.

Credentials never live in this repo. The ArcGIS API's *profile* mechanism stores
the username and org URL in ``~/.arcgisprofile`` and the password in the operating
system keyring, so a checked-out copy of this repo is inert without a local
profile. Create one once:

    agol-provision setup-profile --name vsclr --org https://yourorg.maps.arcgis.com

Thereafter every command takes ``--profile vsclr``.
"""

from __future__ import annotations

from arcgis.gis import GIS

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


def connect(profile: str) -> GIS:
    """Open a GIS connection from a stored profile."""
    try:
        gis = GIS(profile=profile)
    except Exception as exc:
        raise AuthError(
            f"Could not connect using profile {profile!r}: {exc}\n"
            f"Create it with: agol-provision setup-profile --name {profile} "
            f"--org https://yourorg.maps.arcgis.com"
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

"""Derivation of AGOL item titles and hosted service names from project inputs.

Two distinct namespaces are in play and they have different rules:

*Item titles* are display strings. They allow spaces and punctuation, and they are
not required to be unique.

*Service names* form part of the REST URL of a hosted feature service. They must be
alphanumeric-or-underscore, must begin with a letter, and are unique across the
entire organization **permanently** -- a name is not released when the service is
deleted. Getting one wrong halfway through a provisioning run leaves debris that
has to be cleaned up by hand, which is why `preflight` checks every derived name
before anything is created.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# AGOL rejects service names that do not start with a letter. There is also an
# upper length bound; the documented limit has moved between releases, so rather
# than assert a specific number we warn well below any value it has ever taken.
SERVICE_NAME_SOFT_MAX = 100

_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_]+")
_MULTI_UNDERSCORE = re.compile(r"_{2,}")
# Apostrophes sit inside words rather than between them, so they are removed
# outright: "O'Fallon" -> "OFallon", not "O_Fallon". Straight and curly both.
_INTRA_WORD_PUNCT = re.compile(r"['\u2018\u2019]")


class NamingError(ValueError):
    """A project input cannot be turned into a valid AGOL service name."""


def sanitize_service_name(raw: str) -> str:
    """Convert an arbitrary string into a legal AGOL hosted-service name.

    Accents are folded to ASCII, runs of illegal characters collapse to a single
    underscore, and leading/trailing underscores are stripped.

        >>> sanitize_service_name("Smith & Sons")
        'Smith_Sons'
        >>> sanitize_service_name("Zurich")
        'Zurich'

    Raises NamingError when the result would not start with a letter, rather than
    silently mangling the name into something the team would not recognize.
    """
    if not raw or not raw.strip():
        raise NamingError("Cannot derive a service name from an empty string.")

    # "Zürich" -> "Zurich"; drops combining marks rather than dropping the letter.
    folded = unicodedata.normalize("NFKD", raw)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")

    deapostrophized = _INTRA_WORD_PUNCT.sub("", ascii_only)
    cleaned = _INVALID_CHARS.sub("_", deapostrophized)
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("_")

    if not cleaned:
        raise NamingError(
            f"{raw!r} contains no characters usable in a service name. "
            f"Set `service_name_override` in the project config."
        )
    if not cleaned[0].isalpha():
        raise NamingError(
            f"{raw!r} produces service name {cleaned!r}, which does not start with a "
            f"letter. AGOL rejects these. Set `service_name_override` in the project "
            f"config (e.g. 'ThreeM_Moline' for '3M Moline')."
        )
    return cleaned


@dataclass(frozen=True)
class NameContext:
    """Substitution context for the title and service-name patterns in a manifest.

    The manifest owns the actual strings (`"{base} - Design View"`); this class owns
    making them correct. That split means renaming a view is a config edit.
    """

    company: str
    location: str
    service_name_override: str | None = None

    @property
    def base_title(self) -> str:
        """Display title stem, e.g. ``"CompanyA Moline"``."""
        return f"{self.company.strip()} {self.location.strip()}"

    @property
    def base_service_name(self) -> str:
        """Service-name stem, e.g. ``"CompanyA_Moline"``."""
        if self.service_name_override:
            # Still validated -- an override is an escape hatch, not a bypass.
            return sanitize_service_name(self.service_name_override)
        return sanitize_service_name(self.base_title)

    def render_title(self, pattern: str) -> str:
        """Fill a title pattern, e.g. ``"{base} - Design View"``."""
        return pattern.format(
            base=self.base_title, company=self.company.strip(), location=self.location.strip()
        )

    def render_service_name(self, pattern: str) -> str:
        """Fill and validate a service-name pattern, e.g. ``"{base_sn}_Design"``."""
        rendered = pattern.format(
            base_sn=self.base_service_name,
            base=self.base_service_name,  # tolerate {base} in a service pattern
        )
        name = sanitize_service_name(rendered)
        if len(name) > SERVICE_NAME_SOFT_MAX:
            raise NamingError(
                f"Service name {name!r} is {len(name)} characters, over the "
                f"{SERVICE_NAME_SOFT_MAX}-character safety limit. Shorten the company or "
                f"location, or set `service_name_override`."
            )
        return name

    def slug(self) -> str:
        """Filesystem-safe project key, e.g. ``"companya-moline"``.

        Used for state and config filenames, never sent to AGOL. Derived from
        `base_service_name` rather than the title so that an override rescues
        this too -- a company like "3M" cannot start a service name, and fixing
        every service name while still failing on the state filename would make
        the override useless in exactly the case it exists for.
        """
        return self.base_service_name.lower().replace("_", "-")

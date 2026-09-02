"""Naming rules are load-bearing: a bad service name fails a run partway through
and leaves debris that must be hand-cleaned. These run without network access.
"""

import pytest

from agol_provision.naming import (
    NameContext,
    NamingError,
    sanitize_service_name,
)


class TestSanitizeServiceName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("CompanyA Moline", "CompanyA_Moline"),
            ("Smith & Sons", "Smith_Sons"),
            ("O'Fallon", "OFallon"),
            ("Saint-Louis", "Saint_Louis"),
            ("Company  A", "Company_A"),          # collapse repeated separators
            ("  Padded  ", "Padded"),
            ("Already_Valid_123", "Already_Valid_123"),
            ("Zürich", "Zurich"),           # combining diaeresis folded away
            ("Niño Project", "Nino_Project"),  # precomposed n-tilde -> n, tilde dropped
            ("Coeur d'Alene", "Coeur_dAlene"),  # apostrophe drops, space separates
        ],
    )
    def test_produces_legal_names(self, raw, expected):
        assert sanitize_service_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "!!!", "___"])
    def test_rejects_unusable_input(self, raw):
        with pytest.raises(NamingError):
            sanitize_service_name(raw)

    def test_rejects_leading_digit_with_actionable_message(self):
        # AGOL requires a leading letter. Silently mangling "3M" would produce a
        # name nobody recognizes, so this fails loudly and names the escape hatch.
        with pytest.raises(NamingError, match="service_name_override"):
            sanitize_service_name("3M Moline")

    def test_result_is_always_url_safe(self):
        for raw in ["A B", "x/y\\z", "a.b.c", "tab\there"]:
            assert sanitize_service_name(raw).replace("_", "").isalnum()


class TestNameContext:
    @pytest.fixture
    def ctx(self):
        return NameContext(company="CompanyA", location="Moline")

    def test_base_title_is_display_form(self, ctx):
        assert ctx.base_title == "CompanyA Moline"

    def test_base_service_name_is_url_form(self, ctx):
        assert ctx.base_service_name == "CompanyA_Moline"

    def test_renders_view_title(self, ctx):
        assert ctx.render_title("{base} - Design View") == "CompanyA Moline - Design View"

    def test_renders_view_service_name(self, ctx):
        assert ctx.render_service_name("{base_sn}_Design") == "CompanyA_Moline_Design"

    def test_title_and_service_name_diverge(self, ctx):
        """The whole point of the module: these are different namespaces."""
        title = ctx.render_title("{base} - Contractor / CX Redline View")
        service = ctx.render_service_name("{base_sn}_Redline")
        assert " " in title and "/" in title
        assert " " not in service and "/" not in service

    def test_override_is_still_validated(self):
        ctx = NameContext(company="3M", location="Moline", service_name_override="3M_Moline")
        with pytest.raises(NamingError):
            _ = ctx.base_service_name

    def test_override_accepts_a_legal_replacement(self):
        ctx = NameContext(company="3M", location="Moline", service_name_override="ThreeM_Moline")
        assert ctx.base_service_name == "ThreeM_Moline"
        assert ctx.base_title == "3M Moline"  # display name is untouched

    def test_rejects_overlong_service_name(self):
        ctx = NameContext(company="A" * 60, location="B" * 60)
        with pytest.raises(NamingError, match="safety limit"):
            ctx.render_service_name("{base_sn}_Design")

    def test_slug_is_filesystem_safe(self, ctx):
        assert ctx.slug() == "companya-moline"

    def test_slug_honours_the_override(self):
        """Otherwise the escape hatch cannot rescue the case it exists for.

        A company like "3M" cannot start a service name, and the error tells the
        user to set an override. If the slug still derived from the raw title,
        the override would fix every service name and the run would still fail
        on the state filename.
        """
        ctx = NameContext(company="3M", location="Moline", service_name_override="ThreeM_Moline")
        assert ctx.slug() == "threem-moline"

    def test_strips_incidental_whitespace(self):
        ctx = NameContext(company="  CompanyA  ", location="  Moline  ")
        assert ctx.base_title == "CompanyA Moline"
        assert ctx.base_service_name == "CompanyA_Moline"

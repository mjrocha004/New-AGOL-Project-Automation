"""Version gating exists because a machine using ArcGIS Pro's bundled Python gets
whichever `arcgis` version Pro shipped, which may predate remap_data().
"""

import pytest

from agol_provision.auth import MIN_ARCGIS_VERSION, arcgis_version, check_arcgis_version, parse_version


class TestParseVersion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2.4.3", (2, 4, 3)),
            ("2.4", (2, 4)),
            ("2.4.0", (2, 4, 0)),
            ("10.2.1", (10, 2, 1)),
        ],
    )
    def test_parses_release_versions(self, raw, expected):
        assert parse_version(raw) == expected

    @pytest.mark.parametrize("raw", ["2.4.0rc1", "2.4.0+local", "2.4.0.dev3"])
    def test_prerelease_compares_as_its_release(self, raw):
        """A release candidate should not read as older than the floor it meets."""
        assert parse_version(raw)[:2] == (2, 4)

    def test_unparseable_sorts_below_everything(self):
        assert parse_version("unknown") == (0,)

    def test_ordering_is_numeric_not_lexicographic(self):
        """String comparison would place "2.10" before "2.4"."""
        assert parse_version("2.10.0") > parse_version("2.4.0")


class TestVersionGate:
    def test_floor_is_the_version_that_added_remap_data(self):
        assert MIN_ARCGIS_VERSION == (2, 4, 0)

    def test_installed_version_meets_the_floor(self):
        """Guards the lockfile: a resolution below 2.4 would break app rewiring."""
        assert arcgis_version() >= MIN_ARCGIS_VERSION

    def test_check_passes_on_this_install(self):
        assert check_arcgis_version(strict=False) is None


class TestProConnectionMode:
    """`--profile pro` borrows ArcGIS Pro's signed-in connection, which removes both
    the credential-storage and the SSO problem -- at the cost of needing arcpy.
    """

    @pytest.mark.parametrize("value", ["pro", "home", "PRO", "Home", " pro "])
    def test_recognizes_the_pro_connection_modes(self, value):
        from agol_provision.auth import uses_arcgis_pro

        assert uses_arcgis_pro(value)

    @pytest.mark.parametrize("value", ["vsclr", "prod", "homeoffice", ""])
    def test_ordinary_profile_names_are_not_pro_mode(self, value):
        from agol_provision.auth import uses_arcgis_pro

        assert not uses_arcgis_pro(value)

    def test_home_is_treated_as_pro(self):
        """Outside a hosted notebook the ArcGIS API rewrites "home" to "pro", so
        both must route down the same path."""
        from agol_provision.auth import PRO_PROFILES

        assert PRO_PROFILES == frozenset({"pro", "home"})

    def test_missing_arcpy_explains_the_cause_not_just_the_symptom(self):
        """The underlying ImportError says arcpy is missing but not why that
        matters or what to do instead."""
        from agol_provision.auth import AuthError, arcpy_available, connect

        if arcpy_available():
            pytest.skip("arcpy is installed; the failure path cannot be exercised")

        with pytest.raises(AuthError) as exc:
            connect("pro")
        message = str(exc.value)
        assert "ArcGIS Pro's Python environment" in message
        assert "setup-profile" in message  # names the alternative

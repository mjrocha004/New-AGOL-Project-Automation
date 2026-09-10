"""How the spike diagnoses a copy AGOL refuses.

The first master with relationships was refused with a 400 naming a .NET type
and no layer. The diagnosis posts each layer alone, names the layer and the
property, and writes both to the spike report -- on the empty service the
layers were meant for, which the spike already holds and deletes afterwards.
"""


class TestDiagnoseFailedCopy:
    def _template(self):
        class Mgr:
            def __init__(self, props):
                self.properties = props

        class Lyr:
            def __init__(self, props):
                self.manager = Mgr(props)

        flc = type("FLC", (), {})()
        flc.properties = {"name": "Kinetic_Master_FS"}
        flc.layers = [Lyr({"id": 0, "name": "Poles", "fields": []}),
                      Lyr({"id": 1, "name": "Spans", "fields": [], "subtypes": [{"code": 1}]})]
        flc.tables = [Lyr({"id": 2, "name": "Notes", "fields": []})]
        return flc

    def test_names_the_layer_and_property_and_writes_the_report(self, tmp_path):
        from agol_provision.cli import _diagnose_failed_copy

        posted = []

        def add(json_dict):
            posted.append(json_dict)
            for entry in json_dict.get("layers", []) + json_dict.get("tables", []):
                if "subtypes" in entry:
                    raise Exception("Invalid definition for LayerCoreInfo")
            return {"success": True}

        report = tmp_path / "spike.md"
        _diagnose_failed_copy(
            object(), self._template(), Exception("boom"), add=add, report=report,
        )

        text = report.read_text()
        assert "NOT USABLE" in text
        assert "**Spans** (layer): rejected" in text
        assert "**subtypes** were removed" in text
        assert "- Poles (layer): accepted" in text
        assert "- Notes (table): accepted" in text
        # The rejected definition is in the report, so the property can be read.
        assert '"name": "Spans"' in text

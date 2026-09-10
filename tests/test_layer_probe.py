"""When AGOL rejects a whole-service copy, the error names a .NET type, not a
layer. The probe adds each layer alone to find which one is refused, then strips
the usual suspects from that layer to name the property.
"""

import copy

from agol_provision.layer_probe import layer_payload, probe


class FakeLayer:
    def __init__(self, props):
        self.manager = type("Mgr", (), {"properties": props})()


class FakeTarget:
    """Records every add_to_definition call; `reject` decides which fail.

    `reject(payload, present_ids)` returns an error message or None. The set of
    accepted layer ids is tracked so a rule can depend on what is already in the
    service, the way a relationship check does.
    """

    def __init__(self, reject=lambda payload, present: None):
        self.calls = []
        self.present = set()
        self._reject = reject

    def add_to_definition(self, json_dict):
        self.calls.append(copy.deepcopy(json_dict))
        entries = json_dict.get("layers", []) + json_dict.get("tables", [])
        for entry in entries:
            msg = self._reject(entry, self.present)
            if msg:
                raise Exception(msg)
        for entry in entries:
            self.present.add(entry["id"])
        return {"success": True}


def _layer(layer_id, name, **extra):
    return FakeLayer({"id": layer_id, "name": name, "type": "Feature Layer",
                      "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}],
                      **extra})


class TestPayload:
    def test_strips_what_the_copy_strips(self):
        """The copy drops `indexes`, `adminLayerInfo` and `relationships` before
        posting, so the probe must post the same thing or it tests a different
        payload from the one that failed."""
        lyr = _layer(0, "Poles", indexes=[{"name": "i"}], adminLayerInfo={"x": 1},
                     relationships=[{"relatedTableId": 20}])
        payload = layer_payload(lyr)
        assert "indexes" not in payload
        assert "adminLayerInfo" not in payload
        assert "relationships" not in payload
        assert payload["name"] == "Poles"

    def test_does_not_mutate_the_source(self):
        lyr = _layer(0, "Poles", indexes=[{"name": "i"}])
        layer_payload(lyr)
        assert "indexes" in lyr.manager.properties


class TestProbe:
    def test_reports_every_layer_and_table_accepted(self):
        target = FakeTarget()
        results = probe([_layer(0, "Poles"), _layer(1, "Spans")], [_layer(2, "Notes")],
                        target.add_to_definition)
        assert [(r.name, r.kind, r.accepted) for r in results] == [
            ("Poles", "layer", True), ("Spans", "layer", True), ("Notes", "table", True),
        ]
        assert results[2].payload["name"] == "Notes"
        assert all(r.error is None and r.culprit is None for r in results)

    def test_names_the_rejected_layer_with_agols_message(self):
        def reject(payload, present):
            return "Invalid definition" if payload["name"] == "Spans" else None

        results = probe([_layer(0, "Poles"), _layer(1, "Spans")], [], FakeTarget(reject).add_to_definition)
        by_name = {r.name: r for r in results}
        assert by_name["Poles"].accepted
        assert not by_name["Spans"].accepted
        assert by_name["Spans"].error == "Invalid definition"

    def test_a_forward_reference_is_retried_once_its_target_is_in(self):
        """A definition that refers to a layer not yet added may be refused for
        that reason alone. Retrying after everything else is in separates
        'ordering' from 'this definition is bad'."""
        def reject(payload, present):
            needs = payload.get("dependsOnLayer")
            if needs is not None and needs not in present:
                return f"layer {needs} missing"
            return None

        poles = _layer(0, "Poles", dependsOnLayer=5)
        notes = _layer(5, "Notes")
        results = probe([poles], [notes], FakeTarget(reject).add_to_definition)
        assert all(r.accepted for r in results)
        assert results[0].retried

    def test_strips_suspects_one_at_a_time_to_name_the_culprit(self):
        def reject(payload, present):
            return "Invalid definition" if "subtypes" in payload else None

        bad = _layer(0, "Poles", subtypes=[{"code": 1}], subtypeField="TYPE", relationships=[])
        results = probe([bad], [], FakeTarget(reject).add_to_definition)
        assert not results[0].accepted
        assert results[0].culprit == "subtypes"
        assert results[0].error == "Invalid definition"

    def test_field_level_suspects_are_stripped_inside_each_field(self):
        def reject(payload, present):
            if any("domain" in f for f in payload["fields"]):
                return "bad domain"
            return None

        lyr = FakeLayer({"id": 0, "name": "Poles", "type": "Feature Layer",
                         "fields": [{"name": "A", "type": "esriFieldTypeString",
                                     "domain": {"type": "codedValue"}}]})
        results = probe([lyr], [], FakeTarget(reject).add_to_definition)
        assert results[0].culprit == "field domains"
        # The layer's own definition is untouched by the stripping.
        assert "domain" in lyr.manager.properties["fields"][0]

    def test_culprit_is_none_when_no_suspect_explains_it(self):
        results = probe([_layer(0, "Poles")], [],
                        FakeTarget(lambda p, s: "always").add_to_definition)
        assert not results[0].accepted
        assert results[0].culprit is None
        assert results[0].payload["name"] == "Poles"

    def test_a_non_raising_failure_response_counts_as_rejected(self):
        """The library raises on an `error` key, but a `success: false` body
        comes back as a plain dict."""
        results = probe([_layer(0, "Poles")], [],
                        lambda d: {"success": False, "error": {"message": "nope"}})
        assert not results[0].accepted
        assert "nope" in results[0].error

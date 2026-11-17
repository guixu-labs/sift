"""Tests for JSON embedded into inline JavaScript in report pages."""

import json
import unittest
from html.parser import HTMLParser

from sift.reporting.embedded_data import dumps_for_script


class _ScriptBodies(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bodies = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            self._current = ""
            self.bodies.append(self._current)

    def handle_data(self, data):
        if self._current is not None:
            self._current += data
            self.bodies[-1] = self._current

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self._current = None


class _ReportData:
    @staticmethod
    def model_dump(mode="json"):
        return {"payload": "</ScRiPt><script>synthetic marker</script>"}


class TestScriptJson(unittest.TestCase):
    def test_script_terminators_and_angle_brackets_round_trip(self):
        value = {
            "attack": "</ScRiPt><script>synthetic marker</script>",
            "control": '<placeholder>synthetic-sensitive-control</placeholder> & "quotes"\u2028\u2029',
        }

        encoded = dumps_for_script(value, indent=2)

        self.assertNotIn("<", encoded)
        self.assertNotIn("\u2028", encoded)
        self.assertNotIn("\u2029", encoded)
        self.assertEqual(json.loads(encoded), value)

    def test_plain_text_keeps_json_representation(self):
        value = {"output": "ordinary result"}
        self.assertEqual(dumps_for_script(value), json.dumps(value, ensure_ascii=False))


def test_model_report_generators_keep_untrusted_text_inside_data_script():
    from sift.reporting.audit_viewer import AuditReportGenerator
    from sift.reporting.coverage_viewer import CoverageReportGenerator
    from sift.reporting.critique_viewer import CritiqueReportGenerator
    from sift.reporting.trigger_viewer import TriggerReportGenerator

    payload = "</ScRiPt><script>synthetic marker</script>"
    for generator in (
        AuditReportGenerator,
        TriggerReportGenerator,
        CoverageReportGenerator,
        CritiqueReportGenerator,
    ):
        html = generator.generate_html(_ReportData())
        assert payload not in html
        parser = _ScriptBodies()
        parser.feed(html)
        data_scripts = [body for body in parser.bodies if "EMBEDDED_DATA = " in body]
        assert len(data_scripts) == 1
        assert not any(body.strip() == "synthetic marker" for body in parser.bodies)
        prefix = "EMBEDDED_DATA = "
        start = data_scripts[0].index(prefix) + len(prefix)
        data, _end = json.JSONDecoder().raw_decode(data_scripts[0], start)
        assert data["payload"] == payload


def test_main_report_generator_keeps_untrusted_output_inside_data_script(tmp_path):
    from sift.reporting.viewer import ReportGenerator

    workspace = tmp_path / "iteration-1"
    outputs = workspace / "with_skill" / "eval-1" / "run-1" / "outputs"
    outputs.mkdir(parents=True)
    attack = "</ScRiPt><script>globalThis.__SIFT_REPORT_XSS__=1</script>"
    control = 'Authorization: Token SYNTHETIC_NOT_SECRET <placeholder> & "quoted"'
    (outputs / "attack.txt").write_text(attack, encoding="utf-8")
    (outputs / "control.txt").write_text(control, encoding="utf-8")

    html = ReportGenerator().generate_html(workspace, {"metadata": {"skill_name": "fixture"}})
    parser = _ScriptBodies()
    parser.feed(html)

    marker_script = "globalThis.__SIFT_REPORT_XSS__=1"
    assert not any(script.strip() == marker_script for script in parser.bodies)
    script = next(script for script in parser.bodies if "const EMBEDDED_DATA = " in script)
    prefix = "const EMBEDDED_DATA = "
    embedded, _end = json.JSONDecoder().raw_decode(script, script.index(prefix) + len(prefix))
    files = {item["name"]: item["content"] for item in embedded["runs"][0]["outputs"]}
    assert files == {"attack.txt": attack, "control.txt": control}


if __name__ == "__main__":
    unittest.main()

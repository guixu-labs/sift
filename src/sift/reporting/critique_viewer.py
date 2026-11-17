"""critique HTML 报告生成器(抄 coverage_viewer 模式)。"""

from pathlib import Path

from sift.models.critique_report import ContentQualityReport
from sift.reporting.embedded_data import dumps_for_script

_TEMPLATE = Path(__file__).parent / "templates" / "critique-report.html"


class CritiqueReportGenerator:
    @staticmethod
    def generate_html(report: ContentQualityReport) -> str:
        tpl = _TEMPLATE.read_text(encoding="utf-8")
        payload = dumps_for_script(report.model_dump(mode="json"))
        return tpl.replace("/*__EMBEDDED_DATA__*/", f"var EMBEDDED_DATA = {payload};")

    def write_html(self, report: ContentQualityReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.generate_html(report), encoding="utf-8")

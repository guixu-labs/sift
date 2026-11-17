"""trigger HTML 报告生成器。

读 templates/trigger-report.html,把 TriggerReport.model_dump() 作为 EMBEDDED_DATA
注入,返回自包含 HTML。trigger 命令写完 JSON 后用它写 trigger-report.html(与 audit
同款 always-write)。
"""

from __future__ import annotations

from pathlib import Path

from sift.models.trigger_report import TriggerReport
from sift.reporting.embedded_data import dumps_for_script

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_PLACEHOLDER = "/*__EMBEDDED_DATA__*/"


class TriggerReportGenerator:
    """生成 trigger HTML 报告。数据 = TriggerReport.model_dump()。"""

    @staticmethod
    def generate_html(report: TriggerReport) -> str:
        template = (_TEMPLATE_DIR / "trigger-report.html").read_text(encoding="utf-8")
        data = report.model_dump(mode="json")
        payload = dumps_for_script(data, indent=2)
        return template.replace(_PLACEHOLDER, f"const EMBEDDED_DATA = {payload};")

    def write_html(self, report: TriggerReport, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.generate_html(report), encoding="utf-8")
        return path

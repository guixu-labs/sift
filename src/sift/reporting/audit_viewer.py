"""audit HTML 报告生成器。

读 templates/audit-report.html,把 AuditReport.model_dump() 作为 EMBEDDED_DATA 注入,
返回自包含 HTML。audit 命令写完 JSON 后用它写 audit-report.html。
"""

from __future__ import annotations

from pathlib import Path

from sift.models.audit_models import AuditReport
from sift.reporting.embedded_data import dumps_for_script

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_PLACEHOLDER = "/*__EMBEDDED_DATA__*/"


class AuditReportGenerator:
    """生成 audit HTML 报告。数据 = AuditReport.model_dump(),无需 find_runs 递归。"""

    @staticmethod
    def generate_html(report: AuditReport) -> str:
        template = (_TEMPLATE_DIR / "audit-report.html").read_text(encoding="utf-8")
        data = report.model_dump(mode="json")
        payload = dumps_for_script(data, indent=2)
        return template.replace(_PLACEHOLDER, f"const EMBEDDED_DATA = {payload};")

    def write_html(self, report: AuditReport, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.generate_html(report), encoding="utf-8")
        return path

"""audit 子包:transcript 对账(transcript_path -> tool_trace/text/digest 重建 + checker)。

当前包含 rebuild(薄封装 build_digest,供 auditor 复用)和 provenance(确定性文件归因分析)。
"""

from sift.audit.provenance import analyze_provenance

__all__ = ["analyze_provenance"]

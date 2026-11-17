# Historical skill-creator derivation is recorded in packages/sift/NOTICE.md.
# Run discovery and preview encoding were subsequently reimplemented for Sift.
# Historical skill-creator derivation is recorded in packages/sift/NOTICE.md.
# Run discovery and preview encoding were subsequently reimplemented for Sift.
"""Report generator — produces self-contained HTML from evaluation data."""

from __future__ import annotations

import base64
import copy
import json
import logging
import mimetypes
import os
import signal
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from sift.executables import resolve_executable
from sift.execution.error_triage import classify_error
from sift.reporting.embedded_data import dumps_for_script
from sift.text_output import print_text

METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}

# Preview kinds are an output schema contract with viewer.html. Unknown suffixes
# remain downloadable binary attachments, including PDF files.
_PREVIEW_SUFFIXES = {
    "text": frozenset(
        ".c .cpp .css .csv .go .h .hpp .html .java .js .json .jsx .md .py "
        ".r .rb .rs .sh .sql .toml .ts .tsx .txt .xml .yaml .yml".split()
    ),
    "image": frozenset(".gif .jpeg .jpg .png .svg .webp".split()),
    "xlsx": frozenset({".xlsx"}),
}

_MIME_BY_SUFFIX = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".svg": "image/svg+xml",
}

_IGNORED_RUN_FOLDERS = frozenset({"inputs", "skill", "previous", "__pycache__", "node_modules"})


def get_mime_type(path: Path) -> str:
    """Resolve explicit preview formats before the platform MIME registry."""
    override = _MIME_BY_SUFFIX.get(path.suffix.lower())
    if override is not None:
        return override
    guessed, _encoding = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def find_runs(workspace: Path) -> list[dict]:
    """Collect terminal run directories, then retain final retries and sort them."""
    records = []
    for directory in _run_directories(workspace):
        record = _build_run(workspace, directory)
        if record:
            records.append(record)
    return sorted(_dedup_retries(records), key=_report_run_order)


def _report_run_order(record: dict) -> tuple:
    return record.get("eval_id", float("inf")), record["id"]


def _dedup_retries(runs: list[dict]) -> list[dict]:
    """同 ``(config, eval_id, run_number)`` 只保留**最终 retry**。

    runner 在 UNEVAL/失败时自动重试,产出 ``run-N`` + ``run-N-retry-K``。被 retry
    取代的旧尝试若当独立 run 显示,会让用户看到已被覆盖的失败结果(如全 UNEVAL 的
    首次尝试),误读成 skill 表现差。与 benchmark/comparison 的 ``index_runs`` 同口径:
    同一逻辑 run 只取最高 retry = 最终尝试(canonical)。
    """
    from sift.execution.run_paths import retry_count

    best: dict[tuple, tuple[int, dict]] = {}  # key -> (retry_count, run)
    for r in runs:
        key = (r.get("config"), r.get("eval_id"), r.get("run_number"))
        rc = retry_count(r.get("id", "").rsplit("/", 1)[-1])
        prev = best.get(key)
        if not (prev is None or rc > prev[0]):
            continue
        best[key] = (rc, r)
    return [v[1] for v in best.values()]


def _run_directories(workspace: Path):
    """Depth-first walk with a stack; an outputs folder terminates that branch.

    Reverse insertion preserves the old alphabetical visitation order, including
    which equal-numbered retry wins. Archived and hidden children are excluded.
    """
    pending = [workspace]
    while pending:
        directory = pending.pop()
        if not directory.is_dir():
            continue
        if directory.joinpath("outputs").is_dir():
            yield directory
            continue
        children = sorted(directory.iterdir(), reverse=True)
        pending.extend(child for child in children if _visit_run_child(child))


def _visit_run_child(path: Path) -> bool:
    return not path.name.startswith(".") and path.name not in _IGNORED_RUN_FOLDERS


def _build_run(root: Path, run_dir: Path) -> dict | None:
    outputs_dir = run_dir / "outputs"
    if not outputs_dir.is_dir():
        return None

    eval_id, config, run_number = _run_location(run_dir, root)
    eval_id, prompt = _run_prompt(root, eval_id, run_dir)
    record = {
        "id": str(run_dir.relative_to(root)),
        "eval_id": eval_id,
        "config": config,
        "run_number": run_number,
        "prompt": prompt,
        "outputs": _embed_run_outputs(outputs_dir),
    }
    for artifact in ("grading", "compliance", "calibration"):
        record[artifact] = _read_optional_run_json(run_dir / f"{artifact}.json")
    record["previous_output"] = _previous_run_output(root, run_dir)

    error = (record.get("grading") or {}).get("error", "") or ""
    triage = classify_error(error) if error else None
    record.update(
        error_root_cause=triage.root_cause if triage else None,
        error_suggestion=triage.suggestion if triage else None,
    )
    return record


def _component_number(component: str) -> int:
    try:
        return int(component.split("-")[1])
    except (ValueError, IndexError):
        logging.getLogger(__name__).debug(
            "Optional operation failed; continuing with fallback", exc_info=True
        )
        return 0


def _run_location(run_dir: Path, root: Path) -> tuple[int, str, int]:
    """Read config and numeric identifiers from either supported run layout."""
    parts = run_dir.relative_to(root).parts
    config = "unknown"
    eval_id = 0
    run_number = 0

    if len(parts) >= 3:
        config = parts[0]
        eval_id = _component_number(parts[1])
        run_number = _component_number(parts[-1])
    elif len(parts) >= 2:
        config = parts[0]
        eval_id = _component_number(parts[0])
        run_number = _component_number(parts[-1])
    elif len(parts) >= 1:
        run_number = _component_number(parts[-1])
    return eval_id, config, run_number


def _run_prompt(root: Path, eval_id: int, run_dir: Path) -> tuple[int, str]:
    """Prefer evaluation metadata, falling back to the individual run prompt."""
    sources = ((root / "eval_metadata.json", True), (run_dir / "run_debug.json", False))
    prompt = ""
    for source, supplies_eval_id in sources:
        if prompt:
            break
        metadata = _read_optional_run_json(source)
        if metadata is None:
            continue
        try:
            prompt = metadata.get("prompt", "")
            if supplies_eval_id:
                eval_id = metadata.get("eval_id", eval_id)
        except Exception:
            logging.getLogger(__name__).debug(
                "Optional operation failed; continuing with fallback", exc_info=True
            )
    return eval_id, prompt


def _embed_run_outputs(outputs_dir: Path) -> list[dict]:
    """Apply the report's visibility policy before embedding sorted artifacts."""
    visible = filter(_is_report_artifact, sorted(outputs_dir.rglob("*")))
    return [_embed_file(artifact) for artifact in visible]


def _is_report_artifact(path: Path) -> bool:
    if not path.is_file():
        return False
    return not (path.name.startswith(".") or path.name in METADATA_FILES)


def _previous_run_output(root: Path, run_dir: Path) -> dict | None:
    """Find the first output from the previous matching run."""
    previous_output = None
    prev_dir = root / "previous" / str(run_dir.relative_to(root))
    if prev_dir.is_dir() and (prev_dir / "outputs").is_dir():
        prev_outputs = sorted((prev_dir / "outputs").iterdir())
        if prev_outputs:
            previous_output = _embed_file(prev_outputs[0])
    return previous_output


def _embed_file(path: Path) -> dict:
    """Try the preferred preview, then a binary download if preview reading fails."""
    preferred = _preview_kind(path.suffix.lower())
    attempts = (preferred, "binary") if preferred != "binary" else ("binary",)
    mime = get_mime_type(path)
    for kind in attempts:
        try:
            return _read_preview(path, kind, mime)
        except Exception:
            if kind != "binary":
                logging.getLogger(__name__).debug(
                    "Optional operation failed; continuing with fallback", exc_info=True
                )
    return {"name": path.name, "type": "error", "content": "Could not read file"}


def _preview_kind(suffix: str) -> str:
    for kind, extensions in _PREVIEW_SUFFIXES.items():
        if suffix in extensions:
            return kind
    return "binary"


def _read_preview(path: Path, kind: str, mime: str) -> dict:
    preview = {"name": path.name, "type": kind}
    if kind == "text":
        preview["content"] = path.read_text(encoding="utf-8", errors="replace")
    else:
        encoded = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        field = "data_b64" if kind == "xlsx" else "data_uri"
        preview[field] = encoded if kind == "xlsx" else f"data:{mime};base64,{encoded}"
    return preview


def _load_vet_report(workspace: Path) -> dict | None:
    path = workspace / "vet-report.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug(
                "Optional operation failed; continuing with fallback", exc_info=True
            )
    return None


def _load_gate_verdict(root: Path) -> dict | None:
    # gate.json 落在 run 树根(= output_dir,gate 默认写 benchmark_path.parent),
    # report workspace 即该根(iteration-1 死层删除后无父子之分)。
    path = root / "gate.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug(
                "Optional operation failed; continuing with fallback", exc_info=True
            )
    return None


def load_previous_iteration(workspace: Path) -> dict | None:
    for name in ("previous", "previous_iteration"):
        prev_dir = workspace / name
        if not (prev_dir.is_dir()):
            continue
        return {"runs": find_runs(prev_dir)}
    return None


def _aggregate_compliance(runs: list[dict]) -> list[dict]:
    """把 per-run compliance.json 按 side(config)聚合成整体遵循性。

    遵循性出题依据是 SKILL(非 eval),故 N 个 transcript 各审一次 = 一次性喂给 audit 多会话审计;
    per-slot findings 合并再 aggregate_by_instruction,与一次喂 N transcript **结果等价**(聚合可交换)。
    复用 audit.aggregate_by_instruction。全空 digest 的 side(L1 理解题 cc 纯推理无工具)贡献 0 findings → 不产空聚合。
    """
    from collections import defaultdict

    from sift.audit.aggregate import aggregate_by_instruction, aggregate_coverage
    from sift.audit.noise import annotate_aggregate_noise
    from sift.models import AuditFinding, AuditVerdict

    by_side: dict[str, list[dict]] = defaultdict(list)  # side -> findings(dict)
    side_runs: dict[str, int] = defaultdict(int)  # side -> 贡献了非空 findings 的 run 数
    for r in runs:
        comp = r.get("compliance")
        if not comp:
            continue
        side = r.get("config") or "?"
        findings = comp.get("findings") or []
        by_side[side].extend(findings)
        if not (findings):
            continue
        side_runs[side] += 1

    out: list[dict] = []
    for side, findings in by_side.items():
        if not findings:
            continue  # 该 side 全空 digest → 跳过(不产空聚合)
        # 用 validated model 计数:旧 compliance.json 的 verdict "UNCLEAR" 经 AuditFinding
        # legacy validator 折成 UNRESOLVED,与新数据统一(summary 与 by_instruction 同源)。
        validated = [AuditFinding.model_validate(f) for f in findings]
        ai = aggregate_by_instruction(validated)
        # 聚合级噪声判别(每 side 各跑一次:fail_count>0 的行标 noise_label / noise_reasons)。
        # 在 model_dump 前做,使 by_instruction 字段携带标注——与 audit() 产出的 report 对齐。
        ai = annotate_aggregate_noise(ai)
        out.append(
            {
                "side": side,
                "summary": {
                    "total": len(validated),
                    "pass": sum(1 for f in validated if f.verdict == AuditVerdict.PASS),
                    "fail": sum(1 for f in validated if f.verdict == AuditVerdict.FAIL),
                    "na": sum(1 for f in validated if f.verdict == AuditVerdict.NA),
                    "unresolved": sum(1 for f in validated if f.verdict == AuditVerdict.UNRESOLVED),
                    "indeterminate": sum(
                        1 for f in validated if f.verdict == AuditVerdict.INDETERMINATE
                    ),
                },
                "by_instruction": [a.model_dump(mode="json") for a in ai],
                # 声明覆盖视图(同一 findings 池的姊妹聚合):这批考卷实测触达了声明的多大
                # 比例(covered=任一 run PASS/FAIL;uncovered=全 N/A——所有考卷都没触发该场景;
                # derived 反推指令不计)。旧 compliance.json findings 无 section → 分组落
                # "(未分节)",聚合照常可算。
                "coverage": aggregate_coverage(validated).model_dump(mode="json"),
                "runs_contributed": side_runs[side],
            }
        )
    return out


def _load_previous_benchmark(workspace: Path) -> dict | None:
    """读 workspace/previous/benchmark.json(run 端 fresh 重跑时 MOVE 进去的旧聚合)。
    与 load_previous_iteration(读 run 树,供文件级面板)互补:这里读的是聚合数字,供「vs 上次」纵向 delta。
    """
    path = workspace / "previous" / "benchmark.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _pct(rate) -> int | None:
    """率(0-1 fraction)→ 百分比整数;None 透传(指标缺失)。"""
    return None if rate is None else round(rate * 100)


def _pp(cur_rate, prev_rate) -> int | None:
    """两率之差 → 百分点(cur - prev)*100 四舍五入;任一缺失 → None。"""
    if cur_rate is None or prev_rate is None:
        return None
    return round((cur_rate - prev_rate) * 100)


def _compute_previous_delta(current: dict | None, previous: dict | None) -> dict | None:
    """current vs previous benchmark 的聚合纵向 delta(Task 3「vs 上次」卡的数据)。

    预算成百分比/百分点(模板直接插,免 JS 算术)。双率(客观 programmatic / 主观 llm,
    与 dashboard Δ 卡同源)+ 盲评胜率 + 样本数(不同样本数时模板显式标出,提醒可比性)。
    None / 缺字段 → 该指标省略(不报假 0)。无 run_summary 可比 → 整体返 None(模板不显卡)。
    """
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return None
    cur_summary = current.get("run_summary") or {}
    prev_summary = previous.get("run_summary") or {}
    if not cur_summary or not prev_summary:
        return None

    def _rates(summary, side):
        s = summary.get(side) or {}
        prog = s.get("pass_rate_programmatic") or {}
        llm = s.get("pass_rate_llm") or {}
        return prog.get("mean"), llm.get("mean")

    sides = []
    for side in [k for k in cur_summary if k != "delta"]:
        cur_prog, cur_llm = _rates(cur_summary, side)
        prev_prog, prev_llm = _rates(prev_summary, side) if side in prev_summary else (None, None)
        sides.append(
            {
                "label": side,
                "cur_prog": _pct(cur_prog),
                "prev_prog": _pct(prev_prog),
                "d_prog": _pp(cur_prog, prev_prog),
                "cur_llm": _pct(cur_llm),
                "prev_llm": _pct(prev_llm),
                "d_llm": _pp(cur_llm, prev_llm),
            }
        )

    cur_wr = (current.get("comparison_summary") or {}).get("win_rate")
    prev_wr = (previous.get("comparison_summary") or {}).get("win_rate")

    return {
        "sides": sides,
        "cur_win": _pct(cur_wr),
        "prev_win": _pct(prev_wr),
        "d_win": _pp(cur_wr, prev_wr),
        "cur_runs": (current.get("metadata") or {}).get("runs_per_configuration"),
        "prev_runs": (previous.get("metadata") or {}).get("runs_per_configuration"),
    }


def _normalize_benchmark_expectations(bench: dict | None) -> dict | None:
    """viewer 读的是已存的 benchmark.json:comparator 历史版本(或当次 LLM)声明的
    expectation_results 计数可能与它逐条 details 不一致(一次性产 JSON,数不对自己的列表),
    原样透传会让顶部 passed/total 与下方逐条 ✓/✗ 对不上。生成 report 前按 details 重算,
    与 comparator._recompute_expectation_counts 同一规则,覆盖 benchmark + previous 的所有
    comparisons,使显示永远自洽。返回 deepcopy,不动调用方的 dict。
    """
    if not bench or not isinstance(bench, dict):
        return bench
    bench = copy.deepcopy(bench)
    for comp in bench.get("comparisons") or []:
        er = comp.get("expectation_results") if isinstance(comp, dict) else None
        if not isinstance(er, dict):
            continue
        for v in er.values():
            if not isinstance(v, dict):
                continue
            details = v.get("details") or []
            if not details:
                continue
            passed = sum(1 for d in details if isinstance(d, dict) and d.get("passed"))
            total = len(details)
            v["passed"] = passed
            v["total"] = total
            v["pass_rate"] = passed / total if total else 0.0
    return bench


class ReportGenerator:
    def __init__(self):
        self.template_dir = Path(__file__).parent / "templates"

    def generate_html(
        self,
        workspace: Path,
        benchmark: dict | None = None,
        previous: dict | None = None,
        scorecard: dict | None = None,
    ) -> str:
        template_path = self.template_dir / "viewer.html"
        if not template_path.exists():
            return "<html><body><h1>Error: viewer.html not found</h1></body></html>"

        template = template_path.read_text(encoding="utf-8")

        runs = find_runs(workspace)
        skill_name = workspace.name

        if benchmark and isinstance(benchmark, dict):
            meta = benchmark.get("metadata", {})
            if meta.get("skill_name"):
                skill_name = meta["skill_name"]

        # viewer 原样嵌入 benchmark/previous 的 comparisons.expectation_results;早期数据里
        # LLM 声明的 passed/total 与逐条 details 不一致,这里按 details 重算(与 comparator 同规则)。
        benchmark = _normalize_benchmark_expectations(benchmark)

        if previous is None:
            previous = load_previous_iteration(workspace)
        previous = _normalize_benchmark_expectations(previous)

        prev_outputs: dict = {}
        if previous:
            for prev_run in previous.get("runs", []):
                rid = prev_run.get("id", "")
                po = prev_run.get("outputs")
                if not (po):
                    continue
                prev_outputs[rid] = po

        data = {
            "skill_name": skill_name,
            "runs": runs,
            "compliance_aggregate": _aggregate_compliance(runs),
            "previous": previous,
            "previous_outputs": prev_outputs,
            "previous_delta": _compute_previous_delta(
                benchmark, _load_previous_benchmark(workspace)
            ),
            "benchmark": benchmark,
            "scorecard": scorecard,
            "vet_report": _load_vet_report(workspace),
            "gate": _load_gate_verdict(workspace),
        }

        json_data = dumps_for_script(data)
        html = template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {json_data};")

        return html

    def write_static(
        self,
        workspace: Path,
        output_path: Path,
        benchmark: dict | None = None,
        previous: dict | None = None,
        scorecard: dict | None = None,
    ) -> None:
        html = self.generate_html(workspace, benchmark, previous=previous, scorecard=scorecard)
        output_path.write_text(html, encoding="utf-8")

    def serve(
        self,
        workspace: Path,
        port: int = 8080,
        benchmark: dict | None = None,
        open_browser: bool = False,
    ) -> None:
        gen = self

        class Handler(BaseHTTPRequestHandler):
            def handle_get(self):
                _write_http_response(self, 200, gen.generate_html(workspace, benchmark))

            def handle_post(self):
                _write_http_response(self, 404)

            # BaseHTTPRequestHandler dispatches through these protocol names.
            do_GET = handle_get
            do_POST = handle_post

            def log_message(self, message_format, *args):
                pass

        _kill_port(port)
        server = HTTPServer(("", port), Handler)
        url = f"http://localhost:{port}"
        print_text(f"Serving report at {url}")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()


def _write_http_response(
    handler: BaseHTTPRequestHandler, status: int, html: str | None = None
) -> None:
    handler.send_response(status)
    headers = {} if html is None else {"Content-Type": "text/html; charset=utf-8"}
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.end_headers()
    if html is not None:
        handler.wfile.write(html.encode("utf-8"))


def _kill_port(port: int) -> None:
    if sys.platform == "win32":
        return
    try:
        for pid in _listening_processes(port):
            _terminate_port_process(pid)
    except Exception:
        logging.getLogger(__name__).debug(
            "Optional operation failed; continuing with fallback", exc_info=True
        )


def _listening_processes(port: int) -> list[str]:
    command = [resolve_executable("lsof"), "-ti", f":{port}"]
    completed = subprocess.run(command, capture_output=True, text=True)
    listing = completed.stdout.strip()
    return listing.split("\n") if listing else []


def _terminate_port_process(pid):
    try:
        process_id = int(pid)
    except ValueError:
        logging.getLogger(__name__).debug(
            "Optional operation failed; continuing with fallback", exc_info=True
        )
        return
    try:
        os.kill(process_id, signal.SIGTERM)
    except ProcessLookupError:
        logging.getLogger(__name__).debug(
            "Optional operation failed; continuing with fallback", exc_info=True
        )


def _read_optional_run_json(path: Path):
    """Read an optional run artifact; unreadable artifacts remain absent."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).debug(
            "Could not read optional run artifact %s", path, exc_info=True
        )
        return None

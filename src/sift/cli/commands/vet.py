from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.table import Table

from sift.cli.main import app, console
from sift.parser import resolve_skill_dir


def _is_git_url(value: str) -> bool:
    """Return whether the input uses a Git URL form supported by vet_url()."""
    # A one-letter prefix followed by a colon is a Windows drive, not an SCP host.
    if re.match(r"^[A-Za-z]:", value):
        return False
    if urlsplit(value).scheme in {"file", "git", "http", "https", "ssh"}:
        return True
    return (
        re.fullmatch(
            r"(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/:\s]+):(?!//)[^?#\s]+(?:[?#].*)?",
            value,
        )
        is not None
    )


def _render_security_report(report, output_console: Console) -> None:
    table = Table(title=f"Security Scan: {report.skill_name}")
    table.add_column("Scanner", style="bold")
    table.add_column("Status")
    table.add_column("Detail")
    for s in report.scanners:
        if s.status == "PASS":
            status = "[green]PASS[/green]"
        elif s.status == "FAIL":
            status = "[red]FAIL[/red]"
        elif s.status == "WARN":
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[dim]SKIP[/dim]"
        table.add_row(s.name, status, s.message)
    output_console.print(table)

    if report.findings:
        output_console.print("\n[bold]Findings:[/bold]")
        for f in report.findings:
            color = "red" if f.severity in ("CRITICAL", "HIGH") else "yellow"
            loc = (
                f" ({f.file_path}" + (f":{f.line}" if f.line else "") + ")"
                if f.file_path
                else ""
            )
            output_console.print(
                f"  [{color}]{f.severity}[/{color}] [{f.scanner}] {f.description}{loc}"
            )

    if report.verdict == "BLOCKED":
        output_console.print(
            f"\n[red bold]VERDICT: BLOCKED[/red bold] — {report.summary}"
        )
    elif report.verdict == "REVIEW_NEEDED":
        output_console.print(
            f"\n[yellow bold]VERDICT: REVIEW NEEDED[/yellow bold] — {report.summary}"
        )
    else:
        output_console.print(
            f"\n[green bold]VERDICT: SAFE[/green bold] — {report.summary}"
        )


@app.command(rich_help_panel="静态体检(不跑 cc)")
def vet(
    skill_path: str = typer.Argument(..., help="Path to skill directory or Git URL"),
    output_format: str = typer.Option(
        "table", "--format", help="Output format: table or json"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Output file path"
    ),
    skip_external: bool = typer.Option(
        False, "--skip-external", help="Skip external scanners (aguara, skill-scanner)"
    ),
):
    """4 扫描器安全审查(skill)。"""
    is_git_url = _is_git_url(skill_path)
    if not is_git_url:
        local_path = Path(skill_path)
        if not local_path.exists():
            console.print(f"[red]Error:[/red] Path '{skill_path}' does not exist")
            raise typer.Exit(code=2)
        skill_path = resolve_skill_dir(local_path)
    from sift.security.vetter import SecurityVetter

    try:
        vetter = SecurityVetter(skip_external=skip_external)
        report = vetter.vet_url(skill_path) if is_git_url else vetter.vet(skill_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if output_format == "json":
        data = report.model_dump(mode="json")
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        _render_security_report(report, console)

    # 落盘:仅当显式 -o(默认不写文件,避免污染 skill 目录)
    if output and output_format != "json":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"Written to {output}")

    if report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
        raise typer.Exit(code=1)

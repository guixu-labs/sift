"""vet CLI:落盘行为(无 -o 不写文件,有 -o 才写)。

防回归:旧版 vet 无条件写 `<skill>/vet-report.json` 污染被测 skill 目录;
现改为默认只终端输出,显式 -o 才落盘(对齐 run/audit)。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sift.executables import resolve_executable
from sift.cli.main import app
from sift.cli.commands.vet import _is_git_url

runner = CliRunner()


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:org/repo.git",
        "alice@git.example.com:org/repo.git",
        "github.com:org/repo.git",
        "github.com:/org/repo.git",
    ],
)
def test_vet_recognizes_scp_git_urls(url: str):
    assert _is_git_url(url)


@pytest.mark.parametrize("path", [r"C:\skills\foo", r"D:relative-skill"])
def test_vet_does_not_treat_windows_drive_paths_as_git_urls(path: str):
    assert not _is_git_url(path)


def _skill(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Creates and validates data scripts for users.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return sp


def test_vet_no_output_does_not_pollute_skill_dir(tmp_path: Path):
    """无 -o:只终端输出,不往 skill 目录写 vet-report.json。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["vet", str(sp), "--skip-external"])
    assert r.exit_code == 0, r.output  # 干净 skill → SAFE
    assert not (sp / "vet-report.json").exists(), (
        "默认不应往 skill 目录写 vet-report.json"
    )


def test_vet_with_output_writes_there(tmp_path: Path):
    """有 -o:写到指定文件,且不污染 skill 目录。"""
    sp = _skill(tmp_path)
    out = tmp_path / "v.json"
    r = runner.invoke(app, ["vet", str(sp), "--skip-external", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert not (sp / "vet-report.json").exists(), "-o 时也不该往 skill 目录写"


def test_vet_git_url_clones_and_reports_repository_identity(tmp_path: Path):
    """A Git URL should use the remote scan path and keep a stable source identity."""
    repo = tmp_path / "remote-skill"
    repo.mkdir()
    (repo / "SKILL.md").write_text(
        "---\nname: remote-skill\ndescription: Creates and validates data scripts for users.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    git = resolve_executable("git")
    subprocess.run([git, "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(
        [git, "-C", str(repo), "config", "user.name", "Sift Test"], check=True
    )
    subprocess.run(
        [git, "-C", str(repo), "config", "user.email", "sift-test@example.invalid"],
        check=True,
    )
    subprocess.run([git, "-C", str(repo), "add", "SKILL.md"], check=True)
    subprocess.run(
        [git, "-C", str(repo), "commit", "-m", "Add test skill"],
        check=True,
        capture_output=True,
    )

    url = repo.as_uri()
    output = tmp_path / "report.json"
    result = runner.invoke(
        app,
        ["vet", url, "--skip-external", "--format", "json", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["skill_name"] == "remote-skill"
    assert report["skill_path"] == url
    assert report["verdict"] == "SAFE"
    assert [scanner["status"] for scanner in report["scanners"][:2]] == ["SKIP", "SKIP"]

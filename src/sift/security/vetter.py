"""Multi-scanner security vetting engine for Skill directories.

Integrates with external scanners (aguara, skill-scanner) via subprocess
and implements native Python scanners (secrets-scan, structure-check) for
zero-dependency baseline coverage.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from sift.executables import resolve_executable
from sift.models import (
    ScannerResult,
    ScannerStatus,
    SecurityFinding,
    SecurityReport,
    SecuritySeverity,
    SecurityVerdict,
)

_SECRET_PATTERN = re.compile(
    r"(api_key|apikey|secret|token|password|credential)\s*[:=]\s*"
    r"(?:(?P<quote>['\"])(?P<quoted>[A-Za-z0-9_\-]{16,})(?P=quote)|"
    r"(?P<bare>[A-Za-z0-9_\-]{16,}))(?![A-Za-z0-9_\-])",
    re.IGNORECASE,
)
_PLACEHOLDER_PATTERN = re.compile(
    r"(?:^|[._-])"
    r"(?:your|replace|example|sample|dummy|fake|placeholder|changeme|change_me|todo|insert|enter)"
    r"(?:$|[._-])",
    re.IGNORECASE,
)
_BASE64_PATTERN = re.compile(r"['\"][A-Za-z0-9+/]{40,}={0,2}['\"]")


def _scan_credential_lines(skill_path: Path) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for rel, text in _scan_text_files(
        skill_path,
        exclude_suffixes=(
            ".pyc",
            ".pyo",
            ".so",
            ".dylib",
            ".png",
            ".jpg",
            ".gif",
            ".pdf",
        ),
    ):
        for i, line in enumerate(text.splitlines(), 1):
            match = _SECRET_PATTERN.search(line)
            if not match:
                continue
            bare_value = match.group("bare")
            if bare_value and _PLACEHOLDER_PATTERN.search(bare_value):
                continue
            severity = SecuritySeverity.MEDIUM if bare_value else SecuritySeverity.HIGH
            description = (
                "Unquoted credential-like value detected (review manually)"
                if bare_value
                else "Hardcoded credentials detected"
            )
            findings.append(
                SecurityFinding(
                    scanner="secrets-scan",
                    severity=severity,
                    description=description,
                    file_path=rel,
                    line=i,
                )
            )
            break
    return findings


class SecurityVetter:
    def __init__(self, skip_external: bool = False):
        self._skip_external = skip_external

    @staticmethod
    def _scan_secrets(skill_path: Path) -> ScannerResult:
        findings = _scan_credential_lines(skill_path)
        for rel, text in _scan_text_files(
            skill_path, include_suffixes=(".sh", ".py", ".js", ".ts")
        ):
            has_base64 = any(_BASE64_PATTERN.search(line) for line in text.splitlines())
            if not (has_base64 and not any(f.file_path == rel for f in findings)):
                continue
            findings.append(
                SecurityFinding(
                    scanner="secrets-scan",
                    severity=SecuritySeverity.MEDIUM,
                    description="Base64 encoded string found (review manually)",
                    file_path=rel,
                )
            )

        has_high = any(
            f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)
            for f in findings
        )
        has_medium = any(f.severity == SecuritySeverity.MEDIUM for f in findings)

        if has_high:
            status = ScannerStatus.FAIL
            msg = "Hardcoded credentials detected"
        elif has_medium:
            status = ScannerStatus.WARN
            msg = "Potential secrets require manual review"
        else:
            status = ScannerStatus.PASS
            msg = "No hardcoded secrets found"

        return ScannerResult(
            name="secrets-scan", status=status, findings=findings, message=msg
        )

    @staticmethod
    def _scan_structure(skill_path: Path) -> ScannerResult:
        findings, issues = _check_skill_metadata(skill_path)

        dangerous_pattern = re.compile(
            r"(rm\s+-rf\s|curl.*\|\s*(ba)?sh|wget.*\|\s*(ba)?sh|\beval\s|\bexec\s+\S)",
            re.IGNORECASE,
        )
        for rel, text in _scan_text_files(
            skill_path, include_suffixes=(".sh", ".py", ".js", ".ts")
        ):
            for i, line in enumerate(text.splitlines(), 1):
                if not (dangerous_pattern.search(line)):
                    continue
                findings.append(
                    SecurityFinding(
                        scanner="structure-check",
                        severity=SecuritySeverity.HIGH,
                        description="Dangerous shell command detected",
                        file_path=rel,
                        line=i,
                    )
                )
                issues += 1
                break

        if issues > 0:
            status = ScannerStatus.FAIL
            msg = f"{issues} structure issue(s)"
        elif any(f.severity == SecuritySeverity.MEDIUM for f in findings):
            status = ScannerStatus.WARN
            msg = "Minor structure warnings"
        else:
            status = ScannerStatus.PASS
            msg = "Skill structure valid"

        return ScannerResult(
            name="structure-check", status=status, findings=findings, message=msg
        )

    def vet(self, skill_path: Path) -> SecurityReport:
        skill_path = skill_path.resolve()
        skill_name = skill_path.name

        scanners: list[ScannerResult] = []

        scanners.append(self._scan_aguara(skill_path))
        scanners.append(self._scan_skill_scanner(skill_path))
        scanners.append(self._scan_secrets(skill_path))
        scanners.append(self._scan_structure(skill_path))

        all_findings: list[SecurityFinding] = []
        for s in scanners:
            all_findings.extend(s.findings)

        high_count = sum(
            1
            for f in all_findings
            if f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)
        )
        medium_count = sum(
            1 for f in all_findings if f.severity == SecuritySeverity.MEDIUM
        )

        if high_count > 0:
            verdict = SecurityVerdict.BLOCKED
            summary = f"{high_count} HIGH/CRITICAL, {medium_count} MEDIUM"
        elif medium_count > 0:
            verdict = SecurityVerdict.REVIEW_NEEDED
            summary = f"{medium_count} MEDIUM severity findings"
        else:
            verdict = SecurityVerdict.SAFE
            summary = "All scanners passed"

        return SecurityReport(
            skill_name=skill_name,
            skill_path=str(skill_path),
            verdict=verdict,
            scanners=scanners,
            findings=all_findings,
            summary=summary,
        )

    def vet_url(self, url: str) -> SecurityReport:
        tmpdir = tempfile.mkdtemp(prefix="sift-vet-")
        try:
            scp_match = re.fullmatch(
                r"(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/:\s]+):(?!//)(?P<path>[^?#\s]+)(?:[?#].*)?",
                url,
            )
            if scp_match:
                user = scp_match.group("user")
                host = scp_match.group("host")
                repo_path = scp_match.group("path")
                safe_url = f"ssh://{user + '@' if user else ''}{host}/{repo_path.lstrip('/')}"
            else:
                parts = urlsplit(url)
                safe_url = parts._replace(
                    netloc=parts.netloc.rsplit("@", 1)[-1], query="", fragment=""
                ).geturl()
                repo_path = parts.path
            skill_name = repo_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            try:
                subprocess.run(
                    [resolve_executable("git"), "clone", "--depth", "1", url, tmpdir],
                    capture_output=True,
                    timeout=60,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"git clone failed for {safe_url} (exit code {exc.returncode})"
                ) from None
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"git clone timed out for {safe_url}") from None
            except OSError:
                raise RuntimeError(
                    f"git clone could not start for {safe_url}"
                ) from None
            report = self.vet(Path(tmpdir))
            report.skill_name = skill_name or Path(tmpdir).name
            report.skill_path = safe_url
            return report
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _scan_aguara(self, skill_path: Path) -> ScannerResult:
        if self._skip_external:
            return ScannerResult(
                name="aguara",
                status=ScannerStatus.SKIP,
                message="External scanners disabled",
            )

        aguara_bin = shutil.which("aguara")
        if not aguara_bin:
            go_bin = os.path.expanduser("~/go/bin/aguara")
            if os.path.isfile(go_bin):
                aguara_bin = go_bin
        if not aguara_bin:
            return ScannerResult(
                name="aguara",
                status=ScannerStatus.SKIP,
                message="aguara not installed (https://github.com/garagon/aguara)",
            )

        try:
            proc = subprocess.run(
                [aguara_bin, "scan", str(skill_path), "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            result = proc.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ScannerResult(
                name="aguara",
                status=ScannerStatus.SKIP,
                message="aguara execution failed",
            )

        if not result:
            return ScannerResult(
                name="aguara", status=ScannerStatus.PASS, message="No issues found"
            )

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return ScannerResult(
                name="aguara", status=ScannerStatus.PASS, message="No structured output"
            )

        return _aguara_report(data)

    def _scan_skill_scanner(self, skill_path: Path) -> ScannerResult:
        if self._skip_external:
            return ScannerResult(
                name="skill-scanner",
                status=ScannerStatus.SKIP,
                message="External scanners disabled",
            )

        scanner_bin = shutil.which("skill-scanner")
        if not scanner_bin:
            return ScannerResult(
                name="skill-scanner",
                status=ScannerStatus.SKIP,
                message="skill-scanner not installed (https://pypi.org/project/cisco-ai-skill-scanner/)",
            )

        try:
            proc = subprocess.run(
                [scanner_bin, "scan", str(skill_path), "--format", "json"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            result = proc.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ScannerResult(
                name="skill-scanner",
                status=ScannerStatus.SKIP,
                message="skill-scanner execution failed",
            )

        if not result:
            return ScannerResult(
                name="skill-scanner",
                status=ScannerStatus.PASS,
                message="No issues found",
            )

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return ScannerResult(
                name="skill-scanner",
                status=ScannerStatus.PASS,
                message="No structured output",
            )

        return _scanner_security_result(data)


def _scanner_security_result(data):
    severity_str = data.get("severity", "none").lower()
    severity_map = {
        "critical": SecuritySeverity.CRITICAL,
        "high": SecuritySeverity.HIGH,
        "medium": SecuritySeverity.MEDIUM,
    }
    severity = severity_map.get(severity_str, SecuritySeverity.LOW)

    findings: list[SecurityFinding] = []
    if severity in (
        SecuritySeverity.CRITICAL,
        SecuritySeverity.HIGH,
        SecuritySeverity.MEDIUM,
    ):
        findings.append(
            SecurityFinding(
                scanner="skill-scanner",
                severity=severity,
                description=data.get("description", "Vulnerability detected"),
            )
        )

    if severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH):
        status = ScannerStatus.FAIL
    elif severity == SecuritySeverity.MEDIUM:
        status = ScannerStatus.WARN
    else:
        status = ScannerStatus.PASS

    msg = data.get("description", "No critical vulnerabilities found")
    return ScannerResult(
        name="skill-scanner", status=status, findings=findings, message=msg
    )


def _scan_text_files(skill_path: Path, include_suffixes=None, exclude_suffixes=()):
    """Yield readable scan inputs with the requested suffix policy and relative names."""
    visited_dirs: set[str] = set()
    skill_root = os.path.normcase(os.path.realpath(skill_path))

    def is_within_skill(path: str) -> bool:
        resolved = os.path.normcase(os.path.realpath(path))
        try:
            return os.path.commonpath((skill_root, resolved)) == skill_root
        except ValueError:
            return False

    for root, dirs, files in os.walk(skill_path, followlinks=True):
        # Directory symlinks are part of the skill's reachable content. Follow them,
        # but visit each resolved directory once so aliases and cycles cannot recurse
        # indefinitely or duplicate findings.
        resolved_root = os.path.normcase(os.path.realpath(root))
        if not is_within_skill(root):
            dirs.clear()
            continue
        if resolved_root in visited_dirs:
            dirs.clear()
            continue
        visited_dirs.add(resolved_root)

        # Do not follow directory symlinks that escape the skill root.
        dirs[:] = [dirname for dirname in dirs if is_within_skill(os.path.join(root, dirname))]

        for fname in files:
            path = Path(root) / fname
            if not is_within_skill(path):
                continue
            if include_suffixes is not None and path.suffix not in include_suffixes:
                continue
            if path.suffix in exclude_suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                logging.getLogger(__name__).debug(
                    "Could not read scanner input %s", path, exc_info=True
                )
                continue
            yield str(path.relative_to(skill_path)), text


def _aguara_report(data: dict) -> ScannerResult:
    """Map structured Aguara findings and severities into a scanner report."""
    findings: list[SecurityFinding] = []
    for f in data.get("findings", []):
        sev = f.get("severity", 0)
        if sev >= 4:
            severity = SecuritySeverity.HIGH
        elif sev >= 3:
            severity = SecuritySeverity.MEDIUM
        else:
            severity = SecuritySeverity.LOW
        findings.append(
            SecurityFinding(
                scanner="aguara",
                severity=severity,
                description=f.get("description", ""),
                file_path=f.get("file_path", ""),
                line=f.get("line"),
            )
        )

    high = [
        f
        for f in findings
        if f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)
    ]
    medium = [f for f in findings if f.severity == SecuritySeverity.MEDIUM]

    if high:
        status = ScannerStatus.FAIL
        msg = f"{len(high)} HIGH severity findings"
    elif medium:
        status = ScannerStatus.WARN
        msg = f"{len(medium)} MEDIUM severity findings"
    else:
        status = ScannerStatus.PASS
        msg = "No prompt injection patterns found"

    return ScannerResult(name="aguara", status=status, findings=findings, message=msg)


def _check_skill_metadata(skill_path: Path) -> tuple[list[SecurityFinding], int]:
    """检查 SKILL.md 及其 frontmatter，区分缺失和格式警告。"""
    findings: list[SecurityFinding] = []
    issues = 0

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        findings.append(
            SecurityFinding(
                scanner="structure-check",
                severity=SecuritySeverity.HIGH,
                description="Missing SKILL.md",
            )
        )
        issues += 1
    else:
        content = ""
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            logging.getLogger(__name__).debug(
                "Optional operation failed; continuing with fallback", exc_info=True
            )

        if content and not content.lstrip().startswith("---"):
            findings.append(
                SecurityFinding(
                    scanner="structure-check",
                    severity=SecuritySeverity.MEDIUM,
                    description="SKILL.md missing YAML frontmatter",
                )
            )

    return findings, issues

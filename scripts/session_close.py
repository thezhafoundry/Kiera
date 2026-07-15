#!/usr/bin/env python3
"""Run safe, read-only end-of-session second-brain checks."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ISSUE_STATUSES = {"open", "resolved"}
SECRET_RE = re.compile(r"rnd_[A-Za-z0-9_-]{20,}|Bearer\s+rnd_[A-Za-z0-9_-]+")
STALE_RE = re.compile(
    r"There is no playout queue anymore|manual UI.*agentGender toggle|"
    r"currently `True` in the deployed worker|No redeploy needed|no redeploy needed"
)


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)


def wiki_lint(root: Path) -> list[str]:
    wiki = root / "wiki"
    pages = sorted((wiki / "pages").rglob("*.md"))
    slugs: dict[str, list[Path]] = {}
    errors: list[str] = []
    for page in pages:
        slugs.setdefault(page.stem, []).append(page)
        text = page.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not match:
            errors.append(f"missing frontmatter: {page}")
            continue
        fields = dict(re.findall(r"^([A-Za-z_]+):[ \t]*(.*)$", match.group(1), re.M))
        if fields.get("type", "").strip() == "issue" and fields.get("status", "").strip() not in ISSUE_STATUSES:
            errors.append(f"invalid issue status: {page}")
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", fields.get("updated", "").strip()):
            errors.append(f"invalid updated date: {page}")
    for slug, matches in slugs.items():
        if len(matches) > 1:
            errors.append(f"duplicate wiki slug: {slug}")

    all_docs = [wiki / "WIKI.md", wiki / "index.md", wiki / "log.md", *pages]
    referenced: set[str] = set()
    for page in all_docs:
        text = page.read_text(encoding="utf-8")
        for slug in re.findall(r"\[\[([^]|#]+)", text):
            referenced.add(slug)
            if slug not in slugs:
                errors.append(f"broken wikilink in {page}: {slug}")
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", text):
            target = target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (page.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"broken markdown link in {page}: {target}")
            try:
                relative = resolved.relative_to((wiki / "pages").resolve())
            except ValueError:
                continue
            if relative.suffix == ".md":
                referenced.add(relative.stem)
    for page in pages:
        if page.stem not in referenced:
            errors.append(f"orphan wiki page: {page}")
    return errors


def scan_repo(root: Path, pattern: re.Pattern[str]) -> list[str]:
    matches: list[str] = []
    ignored = {".git", "RVC", "graphify-out", ".venv", "__pycache__"}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        # The checker source necessarily contains the patterns it scans for.
        if path == root / "scripts" / "session_close.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(text):
            matches.append(str(path.relative_to(root)))
    return sorted(matches)


def build_report(root: Path, write_report: bool) -> tuple[int, str]:
    status = run(root, "git", "status", "--short")
    diff_check = run(root, "git", "diff", "--check")
    recent = run(root, "git", "log", "-5", "--oneline")
    wiki_errors = wiki_lint(root)
    secrets = scan_repo(root, SECRET_RE)
    stale_claims = scan_repo(root, STALE_RE)
    changed = run(root, "git", "diff", "--name-only").stdout.splitlines()
    failures = []
    if diff_check.returncode != 0:
        failures.append("git diff --check")
    if wiki_errors:
        failures.append("wiki lint")
    if secrets:
        failures.append("secret-pattern scan")
    if stale_claims:
        failures.append("stale-claim scan")

    changed_lines = [f"- `{item}`" for item in changed] or ["- None"]
    wiki_error_lines = [f"- {error}" for error in wiki_errors]
    secret_lines = [f"- Credential match: `{item}`" for item in secrets]
    stale_lines = [f"- Stale claim: `{item}`" for item in stale_claims]

    lines = [
        f"# Keira session-close report — {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        f"Result: {'BLOCKED — ' + ', '.join(failures) if failures else 'CHECKS PASSED'}",
        "",
        "## Changed tracked files",
        *changed_lines,
        "",
        "## Git status",
        "```text",
        status.stdout.rstrip() or "clean",
        "```",
        "",
        "## Wiki lint",
        f"- Pages checked: {len(list((root / 'wiki/pages').rglob('*.md')))}",
        f"- Errors: {len(wiki_errors)}",
        *wiki_error_lines,
        "",
        "## Security and stale-claim scans",
        f"- Credential-pattern matches: {len(secrets)}",
        *secret_lines,
        f"- Stale-claim matches: {len(stale_claims)}",
        *stale_lines,
        "",
        "## Recent commits",
        "```text",
        recent.stdout.rstrip() or "No commits found",
        "```",
        "",
        "## Manual handoff",
        "- Reconcile durable decisions in `.agents/decisions/log.md`.",
        "- Update `.agents/projects/active-backlog.md` for new or closed work.",
        "- Record live Render/Modal/Twilio verification separately from checkout evidence.",
        "- Review the diff before staging or committing.",
    ]
    report = "\n".join(lines) + "\n"
    if write_report:
        reports_dir = root / ".agents" / "session-reports"
        try:
            reports_dir.mkdir(parents=True, exist_ok=True)
            filename = datetime.now().strftime("%Y-%m-%d-%H%M%S.md")
            report_path = reports_dir / filename
            report_path.write_text(report, encoding="utf-8")
            print(f"Wrote {report_path}")
        except OSError as exc:
            # Keep the checker useful in read-only checkouts or restricted CI
            # sandboxes: print the report and explain why persistence was skipped.
            print(f"Could not write session report: {exc}", file=sys.stderr)
    return (1 if failures else 0), report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    exit_code, report = build_report(root, args.write_report)
    print(report, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

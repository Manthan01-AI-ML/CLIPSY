#!/usr/bin/env python3
"""
secrets_audit.py — One-time audit script for ClipWise codebase.

Run from repo root:
    python secrets_audit.py

Checks:
  1. Hardcoded API keys / passwords / DB credentials in source files
  2. Raw SQL strings (parameterised queries vs string concat)
  3. Logger calls that might leak sensitive data
  4. .env files that should be in .gitignore
  5. Existing .env files committed to git

Exit code 0 = clean, 1 = issues found.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# Patterns that indicate a hardcoded secret in source code.
# Each pattern is (regex, severity, description).
SECRET_PATTERNS = [
    # OpenAI-style API key
    (r"sk-[A-Za-z0-9_\-]{20,}", "CRITICAL", "OpenAI/Anthropic API key"),
    # Anthropic
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "CRITICAL", "Anthropic API key"),
    # AWS
    (r"AKIA[0-9A-Z]{16}", "CRITICAL", "AWS access key"),
    (r"aws_secret_access_key\s*=\s*['\"][A-Za-z0-9/+=]{40}['\"]", "CRITICAL", "AWS secret"),
    # Google API key
    (r"AIza[0-9A-Za-z\-_]{35}", "CRITICAL", "Google API key"),
    # Stripe
    (r"sk_(live|test)_[0-9a-zA-Z]{24,}", "CRITICAL", "Stripe API key"),
    # GitHub token
    (r"gh[pousr]_[A-Za-z0-9_]{36,}", "CRITICAL", "GitHub token"),
    # JWT in source (almost always a leak)
    (r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}", "HIGH", "JWT token"),
    # Hardcoded password assignment
    (r"password\s*=\s*['\"][^'\"]{4,}['\"]", "HIGH", "Hardcoded password"),
    (r"passwd\s*=\s*['\"][^'\"]{4,}['\"]", "HIGH", "Hardcoded password"),
    # DB connection strings with embedded creds
    (r"(postgres|mysql|mongodb)://[^:\s]+:[^@\s]+@", "HIGH", "DB URL with embedded password"),
    # Generic "secret" assignment
    (r"(?:secret|api_key|apikey)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]", "MEDIUM", "Possible API key"),
]

# Files to exclude from scanning (won't contain real secrets, would produce noise)
EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".pytest_cache", ".mypy_cache", "storage", "uploads",
}
EXCLUDE_FILES_REGEX = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|mp4|mov|webm|mkv|wav|mp3|ico|woff2?|ttf|eot|otf|"
    r"min\.js|min\.css|map|lock|sqlite|db|pdf|zip|tar|gz)$",
    re.I,
)
# Skip this audit script itself (would self-flag)
EXCLUDE_FILES = {"secrets_audit.py"}

# False-positive tolerances — strings that LOOK like secrets but are clearly examples.
PLACEHOLDER_TOKENS = {
    "your-api-key", "your_api_key", "YOUR_API_KEY", "test_api_key", "fake_key",
    "REDACTED", "[REDACTED]", "<your-key-here>", "xxxxxxxxx",
    "example-key", "abcdef123456", "supersecret", "changeme", "password123",
    "yourpassword", "PLACEHOLDER",
}


def is_placeholder(matched: str) -> bool:
    """Best-effort filter for example/placeholder values."""
    low = matched.lower()
    return any(ph.lower() in low for ph in PLACEHOLDER_TOKENS)


def scan_secrets(root: Path) -> list[tuple[Path, int, str, str, str]]:
    """Return list of (file, line_no, severity, description, snippet)."""
    findings = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(p in path.parts for p in EXCLUDE_DIRS):
            continue
        if path.name in EXCLUDE_FILES:
            continue
        if EXCLUDE_FILES_REGEX.search(path.name):
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue

        for line_no, line in enumerate(content.splitlines(), 1):
            for pattern, severity, desc in SECRET_PATTERNS:
                m = re.search(pattern, line)
                if m and not is_placeholder(m.group()):
                    findings.append((path, line_no, severity, desc, line.strip()[:120]))
    return findings


# ---------------------------------------------------------------------------
# Raw SQL audit — find string concat into queries
# ---------------------------------------------------------------------------
SQL_PATTERNS = [
    # f-string interpolation into SELECT/INSERT/UPDATE/DELETE
    (r'f["\'](?:SELECT|INSERT|UPDATE|DELETE)\b[^"\']*\{[^}]+\}', "f-string SQL — possible injection"),
    # % string formatting with SQL keywords
    (r'["\'](?:SELECT|INSERT|UPDATE|DELETE)\b[^"\']*["\']\s*%\s*', "SQL via %% formatting — possible injection"),
    # .format() on SQL strings
    (r'["\'](?:SELECT|INSERT|UPDATE|DELETE)\b[^"\']*["\']\.format\(', "SQL via .format() — possible injection"),
    # String concatenation +
    (r'["\'](?:SELECT|INSERT|UPDATE|DELETE)\b[^"\']*["\'] *\+ *\w', "SQL via string concat — possible injection"),
]


def scan_sql(root: Path) -> list[tuple[Path, int, str, str]]:
    findings = []
    for path in root.rglob("*.py"):
        if any(p in path.parts for p in EXCLUDE_DIRS):
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(content.splitlines(), 1):
            for pattern, desc in SQL_PATTERNS:
                if re.search(pattern, line, re.I):
                    findings.append((path, line_no, desc, line.strip()[:120]))
    return findings


# ---------------------------------------------------------------------------
# Logger audit — find logger calls that include risky variables
# ---------------------------------------------------------------------------
RISKY_LOG_PATTERNS = [
    (r'log(?:ger)?\.(?:info|warning|error|debug|critical)\([^)]*\b(password|passwd|pwd|secret|token|api_key|jwt)\b', "Logger with sensitive var name"),
    (r'print\([^)]*\b(password|passwd|pwd|secret|token|api_key|jwt)\b', "print() with sensitive var name"),
]


def scan_logs(root: Path) -> list[tuple[Path, int, str, str]]:
    findings = []
    for path in root.rglob("*.py"):
        if any(p in path.parts for p in EXCLUDE_DIRS):
            continue
        try:
            content = path.read_text(errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(content.splitlines(), 1):
            for pattern, desc in RISKY_LOG_PATTERNS:
                if re.search(pattern, line, re.I):
                    findings.append((path, line_no, desc, line.strip()[:120]))
    return findings


# ---------------------------------------------------------------------------
# Git tracking audit — make sure .env isn't committed
# ---------------------------------------------------------------------------
def check_env_in_git(root: Path) -> list[str]:
    issues = []
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        issues.append(".gitignore missing — should exist with .env in it")
    else:
        gi = gitignore.read_text()
        if ".env" not in gi:
            issues.append(".env is NOT in .gitignore — add it immediately")

    # Check if .env is currently tracked by git
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=root,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            issues.append(
                ".env is currently TRACKED in git. Remove it: "
                "`git rm --cached .env && git commit -m 'remove tracked .env'` "
                "AND rotate every secret in it (treat them as compromised)."
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Not a git repo or git unavailable — skip silently
        pass
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    root = Path.cwd()
    print(f"Scanning: {root}\n")

    issues = 0

    print("=" * 70)
    print("1. HARDCODED SECRETS")
    print("=" * 70)
    secrets = scan_secrets(root)
    if not secrets:
        print("  ✓ No hardcoded secrets found")
    else:
        for path, lineno, sev, desc, snippet in secrets:
            rel = path.relative_to(root)
            print(f"  [{sev:8s}] {rel}:{lineno}  {desc}")
            print(f"             > {snippet}")
            issues += 1

    print()
    print("=" * 70)
    print("2. RAW SQL (potential injection)")
    print("=" * 70)
    sql = scan_sql(root)
    if not sql:
        print("  ✓ No raw SQL string concatenation found "
              "(SQLAlchemy ORM is the safe path)")
    else:
        for path, lineno, desc, snippet in sql:
            rel = path.relative_to(root)
            print(f"  [HIGH    ] {rel}:{lineno}  {desc}")
            print(f"             > {snippet}")
            issues += 1

    print()
    print("=" * 70)
    print("3. RISKY LOGGER CALLS")
    print("=" * 70)
    logs = scan_logs(root)
    if not logs:
        print("  ✓ No logger calls referencing sensitive variable names")
    else:
        print("  Note: these are PATTERN matches. Review each — many are "
              "false positives (e.g. logging that an API key was 'set').")
        print("  The redaction filter we added will sanitise output anyway.")
        for path, lineno, desc, snippet in logs:
            rel = path.relative_to(root)
            print(f"  [REVIEW  ] {rel}:{lineno}  {desc}")
            print(f"             > {snippet}")

    print()
    print("=" * 70)
    print("4. .env file tracking")
    print("=" * 70)
    env_issues = check_env_in_git(root)
    if not env_issues:
        print("  ✓ .env handling looks correct")
    else:
        for issue in env_issues:
            print(f"  [HIGH    ] {issue}")
            issues += 1

    print()
    print("=" * 70)
    print(f"SUMMARY: {issues} critical/high issues found")
    print("=" * 70)
    sys.exit(0 if issues == 0 else 1)


if __name__ == "__main__":
    main()
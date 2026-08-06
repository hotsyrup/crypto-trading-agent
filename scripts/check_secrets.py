from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".pytest_cache", ".ruff_cache", "__pycache__", "data"}
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    "Telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "EVM private key assignment": re.compile(
        r"(?im)^\s*(?:private[_-]?key|wallet[_-]?key)\s*[:=]\s*[\"']?0x[0-9a-f]{64}"
    ),
    "seed phrase assignment": re.compile(r"(?i)\b(?:seed phrase|mnemonic)\s*[:=]\s*\w+"),
}


def main() -> int:
    findings: list[str] = []
    checked = 0
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            findings.append(f"symlink is not allowed: {relative}")
            continue
        if not path.is_file():
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            findings.append(f"credential-bearing environment file: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        checked += 1
        content = path.read_text(encoding="utf-8")
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append(f"possible {label}: {relative}")

    if findings:
        print("Secret-pattern check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"Secret-pattern check passed ({checked} files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

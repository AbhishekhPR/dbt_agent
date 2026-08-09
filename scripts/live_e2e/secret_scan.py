"""Secret scan over the evidence bundle.

Fails closed: an unrecognised match is a failure, not a warning. The bundle is
meant to be shareable, so anything that looks like a credential must be gone
before it is considered complete.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Each pattern names the thing it is looking for, so a hit is actionable.
PATTERNS = [
    ("relium service token", re.compile(r"rlm_[A-Za-z0-9]{6,}\.[A-Za-z0-9_\-]{8,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github app private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack webhook", re.compile(r"https://hooks\.slack\.com/services/\S+")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("dsn with password", re.compile(r"postgres(?:ql)?://[^\s\"']*:[^\s\"'@/]+@")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key body", re.compile(r"MII[A-Za-z0-9+/]{40,}")),
    ("bearer header value", re.compile(r"[Bb]earer\s+[A-Za-z0-9_\-\.]{20,}")),
]

# The recording transports deliberately use unreachable hosts. They are not
# credentials and must not be reported as such.
ALLOWED = (
    "hooks.slack.invalid",
    "recorded-installation-token",
    "postgresql://relium_app:PASSWORD@",
)

# ``.sql`` is here because a database export is the one artifact that carries
# whole rows out of the run. It is the most likely place for a credential to
# escape, so it must be scanned like any other text evidence, not skipped.
TEXT_SUFFIXES = {".json", ".md", ".txt", ".log", ".csv", ".html", ".yml", ".yaml",
                 ".sql"}


def scan(root: Path):
    findings = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append({"file": str(path), "kind": "unreadable",
                             "detail": type(exc).__name__})
            continue
        for name, pattern in PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if any(allowed in snippet for allowed in ALLOWED):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append({
                    "file": str(path.relative_to(root)), "line": line,
                    "kind": name,
                    # Never echo the value: report only its shape.
                    "length": len(snippet),
                    "prefix": snippet[:4] + "…",
                })
    return scanned, findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    scanned, findings = scan(root)
    print(f"scanned {scanned} text file(s) under {root}")
    if findings:
        print(f"FAIL: {len(findings)} potential secret(s):")
        for finding in findings:
            print(f"  {finding['file']}:{finding.get('line', '?')} "
                  f"[{finding['kind']}] {finding.get('prefix')} "
                  f"({finding.get('length')} chars)")
        return 1
    print("PASS: no credentials, tokens, keys or credentialed DSNs found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

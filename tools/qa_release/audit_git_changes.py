"""Inspect the exact staged release changes without logging matched secrets."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    tree = git("write-tree").decode().strip()
    names = [value.decode() for value in git("ls-files", "-z").split(b"\0") if value]
    changed = [value.decode() for value in git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", args.base).split(b"\0") if value]
    private_names = {"settings.ini", "credentials.json", "devices.json", "pairing.json", ".git-credentials",
                     "conversation-drafts.json", "signing.properties", "local.properties"}
    private_parts = {".venv", ".venv-release", ".venv-assets", "node_modules", "artifacts", "agent_workspace", ".gradle"}
    private_suffixes = {".apk", ".jks", ".keystore", ".p12", ".pfx", ".dpapi", ".vault", ".log"}
    path_findings = []
    for name in names:
        path = Path(name)
        if (path.name in private_names or path.suffix.lower() in private_suffixes
                or any(part in private_parts for part in path.parts)
                or path.name.startswith(".env") and path.name != ".env.example"):
            path_findings.append(name)
    patterns = {
        "provider_token": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,255}\b"),
        "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{50,255})\b"),
        "private_key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
        "profile_path": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s\"'<>]{2,})", re.I),
        "url_credentials": re.compile(r"https?://[^\s/:\"'<>]+:[^\s/@\"'<>]{4,}@[^\s\"'<>]+", re.I),
        "literal_credential": re.compile(r'''(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password)\s*[:=]\s*["']([A-Za-z0-9_./+-]{24,})["']'''),
    }
    placeholders = re.compile(r"^(?:Public|Default|USER|YOUR_USER|username|runner|test|example|someone|QA)$", re.I)
    findings, binary_changes, hashes = [], [], {}
    for name in changed:
        value = git("show", ":" + name)
        hashes[name] = hashlib.sha256(value).hexdigest()
        try:
            if b"\0" in value[:8192]:
                raise UnicodeError()
            text = value.decode("utf-8-sig")
        except UnicodeError:
            binary_changes.append(name)
            continue
        for kind, pattern in patterns.items():
            for match in pattern.finditer(text):
                if kind == "profile_path" and placeholders.fullmatch(match.group(1)):
                    continue
                findings.append({"path": name, "kind": kind,
                                 "line": text.count("\n", 0, match.start()) + 1,
                                 "fingerprint": hashlib.sha256(match.group().encode()).hexdigest()[:16]})
    assert tree == git("write-tree").decode().strip(), "Index changed during audit"
    report = {"passed": not path_findings and not findings and not binary_changes,
              "base": args.base, "tree": tree, "tracked_paths_checked": len(names),
              "changed_files_checked": len(changed), "path_findings": path_findings,
              "findings": findings, "binary_changes_require_review": binary_changes, "blob_sha256": hashes,
              "scope": "All index path names plus exact changed UTF-8 blobs. Existing Git history is retained and is outside this change-only audit; binary changes require independent review.",
              "raw_secrets_emitted": False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("passed", "tree", "tracked_paths_checked", "changed_files_checked", "path_findings", "findings", "binary_changes_require_review")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

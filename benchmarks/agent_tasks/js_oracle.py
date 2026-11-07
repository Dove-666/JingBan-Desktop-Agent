"""Independent hidden-case checks for a deliberately narrow JavaScript subset.

Candidate solution.js is data loaded from an owned benchmark workspace. Only
the fixed QuickJS helper executes it, without Python/OS/network JS bindings.
Hidden inputs and Python reference algorithms never live in that workspace.
This measures small JavaScript functions and JSON artifacts, not repository
builds, terminal access, general development ability, or OS confinement.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SDK_PYTHON = ROOT / "agent_backend" / "nanobot" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
CODE_WORKER = ROOT / "agent_backend" / "nanobot" / "code_worker.py"
MAX_SOURCE_BYTES = 65_536
MAX_ARTIFACT_BYTES = 262_144
UNICODE_CASE = "unicode_distinct_window_v1"
INTERVAL_CASE = "grouped_interval_union_v1"
CASES = (UNICODE_CASE, INTERVAL_CASE)


class JsOracleError(ValueError):
    """Only static codes appear in benchmark reports."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_UNICODE_README = """# Unicode window repair

Modify only solution.js. Keep this README unchanged. This is a small JavaScript
function repair task, not a repository build. Do not use modules, Node, shell,
Python, files, network or host callbacks inside the function.

Define a top-level function solve(input). Input is {"text": a Unicode string}.
Return {"length": integer, "start": integer, "text": string} describing the
longest contiguous substring with no repeated Unicode code point. Count and
index Unicode code points, not UTF-16 code units or grapheme clusters. Do not
normalize combining marks or change letter case. For ties choose the earliest
start. Empty input returns {"length":0,"start":0,"text":""}.

Examples: "abcabcbb" -> length 3, start 0, text "abc";
"abba" -> length 2, start 0, text "ab"; "🐳a🐳b" -> length 3, start 1, text "a🐳b".
Repair the actual saved function; hidden inputs are checked independently.
"""

_UNICODE_SEED = """function solve(input) {
  const text = input.text;
  const last = {};
  let left = 0, bestStart = 0, bestLength = 0;
  for (let right = 0; right < text.length; right++) {
    const char = text[right];
    if (last[char] !== undefined) left = last[char] + 1;
    last[char] = right;
    if (right - left + 1 >= bestLength) {
      bestStart = left;
      bestLength = right - left + 1;
    }
  }
  return {length: bestLength, start: bestStart, text: text.slice(bestStart, bestStart + bestLength)};
}
"""

_INTERVAL_README = """# Grouped interval algorithm and artifact

Implement the top-level function solve(input) in solution.js and create
result.json for the exact input in intervals.json. Keep this README and
intervals.json byte-for-byte unchanged. Write only inside this workspace.
This is a small synchronous JavaScript/JSON task, not a build or terminal task.
Functions have no host IO; use the scoped read/write tools to load/save data.

Input: {"records":[{"group":string,"start":integer,"end":integer}, ...]}.
All coordinates and totals fit JavaScript's safe-integer range. Normalize each
record's endpoints using min/max. A record denotes a half-open interval [start,
end); zero-length records count but contribute no covered interval. Group by
the exact string, including names such as "__proto__". Merge overlapping OR
touching intervals. Sort merged intervals by start, then end; sort groups
lexicographically by Unicode code points, not locale or UTF-16 code units.
Preserve groups containing only zero-length records with an empty merged list.

Return exactly {"groups":[{"group":string,"merged":[[start,end],...],
"covered":integer,"source_count":integer},...],"total_covered":integer,
"record_count":integer}. covered is the sum of end-start within a group;
total_covered is the sum of group coverage, and record_count includes all rows.
An empty records array produces groups [], total_covered 0, record_count 0.

Read back solution.js and result.json. Hidden inputs separately test the saved
function; a correct public artifact alone cannot prove the algorithm works.
"""

_INTERVAL_SEED = """function solve(input) {
  // Implement the contract in README.md; this stub does not compute unions.
  return {groups: [], total_covered: 0, record_count: input.records.length};
}
"""

_PUBLIC_INTERVALS = {"records": [
    {"group": "north", "start": 8, "end": 12},
    {"group": "north", "start": 1, "end": 4},
    {"group": "north", "start": 4, "end": 8},
    {"group": "north", "start": 20, "end": 18},
    {"group": "南站", "start": -5, "end": -2},
    {"group": "南站", "start": -3, "end": 2},
    {"group": "南站", "start": 10, "end": 10},
    {"group": "__proto__", "start": 3, "end": 7},
    {"group": "__proto__", "start": 5, "end": 6},
    {"group": "🐳", "start": 0, "end": 0},
]}
_INTERVAL_SOURCE = json.dumps(_PUBLIC_INTERVALS, ensure_ascii=False, indent=2) + "\n"


def catalog_entries():
    """Draft entries only: callers explicitly integrate the new oracle kinds."""
    return deepcopy([
        {"id": "js_unicode_window_repair", "suite": "real_model", "category": "development_js_subset",
         "implementation": "implemented",
         "objective": "Repair an owned JavaScript function's logic and Unicode code-point boundaries, verified with hidden inputs; no general repository/build claim.",
         "prompt": "Read README.md and solution.js. Fix the saved solve(input) function for the complete Unicode window contract. Modify only solution.js, preserve README.md, inspect the saved source, and check representative cases with the available pure-JavaScript calculation tool. No terminal, package or build is requested.",
         "fixtures": [{"path": "README.md", "text": _UNICODE_README}, {"path": "solution.js", "text": _UNICODE_SEED}],
         "oracles": [{"kind": "js_hidden_cases", "path": "solution.js", "case": UNICODE_CASE}]},
        {"id": "js_interval_union_artifact", "suite": "real_model", "category": "development_js_subset",
         "implementation": "implemented",
         "objective": "Implement a small JavaScript interval algorithm and produce its exact public JSON artifact, with separate hidden function checks; no general build claim.",
         "prompt": "Read README.md, intervals.json and solution.js. Implement solve(input) for the complete grouped interval-union contract, and create result.json for the actual intervals.json input. Preserve README.md and intervals.json exactly. Write only solution.js and result.json, use the pure-JavaScript computation tool as needed, and read back both saved outputs. No shell, Node, Python or package installation is requested.",
         "fixtures": [{"path": "README.md", "text": _INTERVAL_README},
                      {"path": "intervals.json", "text": _INTERVAL_SOURCE},
                      {"path": "solution.js", "text": _INTERVAL_SEED}],
         "oracles": [{"kind": "js_hidden_cases", "path": "solution.js", "case": INTERVAL_CASE},
                     {"kind": "js_algorithm_artifact", "path": "result.json", "source_path": "intervals.json", "case": INTERVAL_CASE}]},
    ])


def _task(case):
    if case not in CASES:
        raise JsOracleError("JS_ORACLE_UNKNOWN_CASE")
    return catalog_entries()[CASES.index(case)]


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(workspace, name, maximum):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir() or name not in {"README.md", "solution.js", "intervals.json", "result.json"}:
        raise JsOracleError("JS_ORACLE_INVALID_WORKSPACE")
    target = root / name
    try:
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file() or target.is_symlink():
            raise JsOracleError("JS_ORACLE_WORKSPACE_ESCAPE")
        if resolved.stat().st_size > maximum:
            raise JsOracleError("JS_ORACLE_FILE_LIMIT")
        with resolved.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum or target.resolve(strict=True) != resolved:
            raise JsOracleError("JS_ORACLE_FILE_LIMIT")
        return raw
    except OSError:
        raise JsOracleError("JS_ORACLE_FILE_UNAVAILABLE") from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JsOracleError("JS_ORACLE_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(JsOracleError("JS_ORACLE_INVALID_JSON_NUMBER")))
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, JsOracleError):
            raise
        raise JsOracleError("JS_ORACLE_INVALID_JSON") from None


def _same(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _unicode_reference(value):
    """Exhaustive substring oracle, independent of the seeded sliding window."""
    text = value["text"]
    best_start, best_length = 0, 0
    for start in range(len(text)):
        seen = set()
        for end in range(start, len(text)):
            if text[end] in seen:
                break
            seen.add(text[end])
            if len(seen) > best_length:
                best_start, best_length = start, len(seen)
    return {"length": best_length, "start": best_start, "text": text[best_start:best_start + best_length]}


def _interval_reference(value):
    """Event-sweep oracle; candidate examples use sorted interval accumulation."""
    rows = value["records"]
    grouped = {}
    for row in rows:
        grouped.setdefault(row["group"], []).append(row)
    groups = []
    for group in sorted(grouped):
        events = Counter()
        for row in grouped[group]:
            start, end = sorted((row["start"], row["end"]))
            if start < end:
                events[start] += 1
                events[end] -= 1
        merged, level, previous = [], 0, None
        for point, change in sorted(events.items()):
            if previous is not None and level > 0 and point > previous:
                if merged and merged[-1][1] == previous:
                    merged[-1][1] = point
                else:
                    merged.append([previous, point])
            level += change
            previous = point
        covered = sum(end - start for start, end in merged)
        groups.append({"group": group, "merged": merged, "covered": covered, "source_count": len(grouped[group])})
    return {"groups": groups, "total_covered": sum(row["covered"] for row in groups), "record_count": len(rows)}


def _hidden_cases(case):
    if case == UNICODE_CASE:
        strings = ("", "a", "abba", "abcabcbb", "tmmzuxt", "abcaefbg", "🐳🐳", "🐳a🐳b",
                   "a𝄞b𝄞c", "e\u0301e\u0301", "👩\u200d💻👩\u200d💻", "汉字🐳汉鲸鱼", "\0a\0b",
                   "".join(chr(0x400 + i) for i in range(512)) + "Ж")
        return [{"text": text} for text in strings]
    if case == INTERVAL_CASE:
        return [
            {"records": []},
            {"records": [{"group": "one", "start": 2, "end": 8}]},
            {"records": [{"group": "a", "start": s, "end": e} for s, e in ((8, 10), (1, 3), (3, 8), (2, 7), (20, 18))]},
            {"records": [{"group": "a", "start": s, "end": e} for s, e in ((-5, -2), (-3, 4), (4, 4), (9, 9))]},
            {"records": [{"group": group, "start": s, "end": e} for group, s, e in
                         (("__proto__", 1, 5), ("constructor", 3, 8), ("__proto__", 5, 10), ("toString", 7, 7))]},
            {"records": [{"group": group, "start": 5, "end": 1} for group in ("🐳", "\ue000", "汉", "a", "𝄞")]},
            {"records": [{"group": "zero", "start": value, "end": value} for value in (5, 1, -7)]},
            {"records": [{"group": str(i % 4), "start": (i * 7) % 31 - 12, "end": (i * 13) % 37 - 8} for i in range(40)]},
            deepcopy(_PUBLIC_INTERVALS),
        ]
    raise JsOracleError("JS_ORACLE_UNKNOWN_CASE")


# This wrapper is fixed host code. Candidate source runs as a script in the
# same value-only VM, then the actual saved global solve function is invoked.
# A top-level candidate return is a syntax error and cannot bypass this wrapper.
_CONTROLLED_PROGRAM = """const source = input.source, cases = input.cases;
const evaluate = eval, define = Object.defineProperty;
const solve = evaluate(source + '\\n;typeof solve === "function" ? solve : null;');
if (typeof solve !== 'function')
  throw new Error('Required solve function unavailable');
const outputs = [];
for (let index = 0; index < cases.length; index++) {
  const value = solve(cases[index]);
  define(outputs, '' + index, {value, enumerable:true, configurable:true, writable:true});
}
return outputs;
"""


def _execute(source, cases):
    if not SDK_PYTHON.is_file() or not CODE_WORKER.is_file():
        raise JsOracleError("JS_ORACLE_RUNTIME_UNAVAILABLE")
    raw = json.dumps({"code": _CONTROLLED_PROGRAM, "input": {"source": source, "cases": cases}},
                     ensure_ascii=True, allow_nan=False).encode("ascii")
    environment = {key: value for key, value in os.environ.items() if key.casefold() in {"systemroot", "systemdrive", "windir"}}
    try:
        result = subprocess.run([str(SDK_PYTHON), "-I", "-u", str(CODE_WORKER)], input=raw,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=6, cwd=str(CODE_WORKER.parent),
            env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode != 0 or len(result.stdout) > 1_048_576:
            raise JsOracleError("JS_ORACLE_RUNTIME_FAILED")
        value = _json(result.stdout)
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise JsOracleError("JS_ORACLE_RUNTIME_FAILED")
        if not value["ok"]:
            code = value.get("code")
            allowed = {"CODE_TIME_LIMIT", "CODE_MEMORY_LIMIT", "CODE_OUTPUT_LIMIT", "CODE_EVALUATION_FAILED", "CODE_RUNTIME_MISSING"}
            raise JsOracleError("JS_ORACLE_" + code.removeprefix("CODE_") if isinstance(code, str) and code in allowed else "JS_ORACLE_RUNTIME_FAILED")
        if (set(value) != {"ok", "result", "runtime", "scope"} or value["runtime"] != "quickjs-ng"
                or value["scope"] != "pure_json_computation" or not isinstance(value["result"], list)):
            raise JsOracleError("JS_ORACLE_RUNTIME_FAILED")
        return value["result"]
    except subprocess.TimeoutExpired:
        raise JsOracleError("JS_ORACLE_TIME_LIMIT") from None
    except OSError:
        raise JsOracleError("JS_ORACLE_RUNTIME_UNAVAILABLE") from None


def evaluate_js(workspace, case, *, artifact=False):
    """Run hidden saved-function checks; optionally verify its public artifact.

    Reports contain no candidate source, hidden inputs, actual/expected values,
    stack traces or private paths. All fixture preservation is checked again
    after execution, so concurrent mutation cannot approve a stale solution.
    """
    report = {"passed": False, "scope": "javascript_function_hidden_cases", "issues": [], "case_count": 0}
    try:
        task = _task(case)
        readonly = {fixture["path"]: fixture["text"].encode("utf-8") for fixture in task["fixtures"] if fixture["path"] != "solution.js"}
        for name, expected in readonly.items():
            if _read(workspace, name, MAX_ARTIFACT_BYTES) != expected:
                raise JsOracleError("JS_ORACLE_SOURCE_CHANGED")
        source_raw = _read(workspace, "solution.js", MAX_SOURCE_BYTES)
        try:
            source = source_raw.decode("utf-8")
        except UnicodeError:
            raise JsOracleError("JS_ORACLE_INVALID_SOURCE_ENCODING") from None
        if not source.strip():
            raise JsOracleError("JS_ORACLE_EMPTY_SOURCE")
        cases = _hidden_cases(case)
        expected = [(_unicode_reference(value) if case == UNICODE_CASE else _interval_reference(value)) for value in cases]
        # Each hidden input gets a fresh VM/process. A candidate cannot retain
        # test-order state, mutate later hidden inputs, or change earlier return
        # objects before the host snapshots them. Reject the first bad result.
        for hidden, answer in zip(cases, expected):
            actual = _execute(source, [hidden])
            if not _same(actual, [answer]):
                raise JsOracleError("JS_ORACLE_HIDDEN_CASE_MISMATCH")
        artifact_raw = None
        if artifact:
            if case != INTERVAL_CASE:
                raise JsOracleError("JS_ORACLE_ARTIFACT_UNSUPPORTED")
            public = _json(_read(workspace, "intervals.json", MAX_ARTIFACT_BYTES))
            artifact_raw = _read(workspace, "result.json", MAX_ARTIFACT_BYTES)
            actual_artifact = _json(artifact_raw)
            if not _same(actual_artifact, _interval_reference(public)):
                raise JsOracleError("JS_ORACLE_ARTIFACT_MISMATCH")
            report["scope"] = "javascript_function_hidden_cases_and_public_json_artifact"
        if _read(workspace, "solution.js", MAX_SOURCE_BYTES) != source_raw:
            raise JsOracleError("JS_ORACLE_SOLUTION_CHANGED_DURING_CHECK")
        for name, original in readonly.items():
            if _read(workspace, name, MAX_ARTIFACT_BYTES) != original:
                raise JsOracleError("JS_ORACLE_SOURCE_CHANGED")
        if artifact_raw is not None and _read(workspace, "result.json", MAX_ARTIFACT_BYTES) != artifact_raw:
            raise JsOracleError("JS_ORACLE_ARTIFACT_CHANGED_DURING_CHECK")
        report.update(passed=True, case_count=len(cases), source_sha256=_hash(source_raw))
        if artifact_raw is not None:
            report["artifact_sha256"] = _hash(artifact_raw)
    except JsOracleError as error:
        report["issues"] = [error.code]
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        report["issues"] = ["JS_ORACLE_INVALID_OUTPUT"]
    return report


_UNICODE_REFERENCE_JS = """function solve(input) {
  const points=Array.from(input.text), last=new Map();
  let left=0, start=0, length=0;
  for(let right=0; right<points.length; right++) {
    if(last.has(points[right])) left=Math.max(left,last.get(points[right])+1);
    last.set(points[right],right);
    if(right-left+1>length) {start=left;length=right-left+1;}
  }
  return {length,start,text:points.slice(start,start+length).join('')};
}
"""

_INTERVAL_REFERENCE_JS = """function solve(input) {
  const grouped=new Map();
  for(const row of input.records) {
    if(!grouped.has(row.group)) grouped.set(row.group,[]);
    grouped.get(row.group).push([Math.min(row.start,row.end),Math.max(row.start,row.end)]);
  }
  const compare=(a,b)=>{const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0));
    for(let i=0;i<Math.min(x.length,y.length);i++) if(x[i]!==y[i]) return x[i]-y[i];return x.length-y.length;};
  const groups=[];let total_covered=0;
  for(const group of Array.from(grouped.keys()).sort(compare)) {
    const rows=grouped.get(group), merged=[];
    for(const [start,end] of rows.slice().sort((a,b)=>a[0]-b[0]||a[1]-b[1])) {
      if(start===end) continue;
      const prior=merged[merged.length-1];
      if(prior&&start<=prior[1]) prior[1]=Math.max(prior[1],end);else merged.push([start,end]);
    }
    const covered=merged.reduce((sum,[start,end])=>sum+end-start,0);total_covered+=covered;
    groups.push({group,merged,covered,source_count:rows.length});
  }
  return {groups,total_covered,record_count:input.records.length};
}
"""


def write_reference_outputs(workspace, case):
    """Offline oracle self-tests only; modifies solution/output, never fixtures."""
    _task(case)
    root = Path(workspace).resolve(strict=True)
    # Reuse the read guard before replacing an existing fixture path.
    _read(root, "solution.js", MAX_SOURCE_BYTES)
    source = _UNICODE_REFERENCE_JS if case == UNICODE_CASE else _INTERVAL_REFERENCE_JS
    (root / "solution.js").write_text(source, encoding="utf-8", newline="")
    if case == INTERVAL_CASE:
        target = root / "result.json"
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise JsOracleError("JS_ORACLE_WORKSPACE_ESCAPE")
        public = _json(_read(root, "intervals.json", MAX_ARTIFACT_BYTES))
        target.write_text(json.dumps(_interval_reference(public), ensure_ascii=False), encoding="utf-8", newline="")


def write_public_only_mutant(workspace, case):
    """Offline negative guard: public examples/artifact cannot prove hidden behavior."""
    _task(case)
    root = Path(workspace).resolve(strict=True)
    _read(root, "solution.js", MAX_SOURCE_BYTES)
    if case == UNICODE_CASE:
        public = {text: _unicode_reference({"text": text}) for text in ("", "abcabcbb", "abba", "🐳a🐳b")}
        source = ("function solve(input) { const examples=" + json.dumps(public, ensure_ascii=False)
                  + "; return Object.hasOwn(examples,input.text) ? examples[input.text] : {length:0,start:0,text:''}; }\n")
    else:
        public = _json(_read(root, "intervals.json", MAX_ARTIFACT_BYTES))
        source = "function solve(input) { return " + json.dumps(_interval_reference(public), ensure_ascii=False) + "; }\n"
    (root / "solution.js").write_text(source, encoding="utf-8", newline="")

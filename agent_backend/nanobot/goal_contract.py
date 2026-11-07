"""Host-owned acceptance of the original task against actual observations.

Requirements restrict completion; they grant no file or computer capabilities.
The small direct-file extractor is only an additional deterministic guard, never
a routing classifier. Every goal not fully specified by trusted requirements
still needs independent semantic verification. A plan or finish summary cannot
serve as evidence that a user's goal happened.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from artifact_verifier import ArtifactVerifier
from goal_evidence import canonical_json, digest

_HASH = re.compile(r"[a-f0-9]{64}\Z")
_FORMATS = frozenset({"auto", "text", "json", "csv", "docx", "xlsx", "pptx", "pdf", "zip"})
_VERDICT_KEYS = frozenset({"schema", "goal_id", "evidence_id", "answer_sha256", "status", "evidence_ids"})
_FILE_TOKEN = re.compile(
    r"(?P<quote>[`\"'“‘])(?P<quoted>[^`\"'“”‘’\r\n]{1,300}\.[A-Za-z0-9]{1,12})[`\"'”’]"
    r"|(?P<bare>(?:[A-Za-z]:[\\/]|\.{0,2}[\\/])?[^\s<>`\"'“”‘’,，、。；;!?！？()（）:：]+\.[A-Za-z0-9]{1,12})(?![A-Za-z0-9_]|\.[A-Za-z0-9_])")
_ACTION = re.compile(r"(?i)(?<![A-Za-z])(?:create|write|save|generate|make)(?![A-Za-z])|创建|新建|写入|生成|保存"
    r"|输出(?=\s*(?:(?:一个|一份|一张)\s*)?(?:Word|PDF|Excel|PowerPoint|Markdown|JSON|CSV|文本)\s*(?:文件|文档|表格)(?!名))")
_READ = re.compile(r"(?i)(?<![A-Za-z])(?:read|show|print|display)(?![A-Za-z])|读取|读出|显示|查看")
_NON_ACTION = re.compile(r"(?i)\b(?:do\s+not|don't|never|avoid|without|not\s+to)\b|不要|别|无需|不必|禁止|不能|不需要")
_EXPLANATORY = re.compile(r"(?i)\b(?:explain|describe|translate|quote|example|how\s+(?:to|do|would)|what\s+(?:does|would))\b(?!\.[A-Za-z0-9])|解释|翻译|引用|举例|示例|如何|怎么(?:样)?(?:才能|去)?(?:创建|新建|写入|生成|保存)")
# Bare tokens are only a high-precision auxiliary constraint. An arbitrary
# dotted expression is not necessarily a file: 12.50, v1.2, example.com and
# console.log must remain for the semantic judge unless file intent is explicit.
_BARE_FILE_SUFFIXES = frozenset({"txt", "md", "json", "jsonl", "csv", "tsv", "yaml", "yml", "toml", "ini",
    "xml", "html", "htm", "css", "js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "pyi", "java", "c",
    "cc", "cpp", "h", "hpp", "cs", "rs", "go", "sql", "docx", "xlsx", "pptx", "pdf", "zip"})
_DOMAIN_SUFFIXES = frozenset({"com", "org", "net", "edu", "gov", "mil", "int", "io", "ai", "co", "cn", "uk",
                              "us", "de", "jp", "dev", "app", "xyz", "online", "site", "tech"})
_FILE_WORD_BEFORE = re.compile(r"(?i)(?:\b(?:file|files|filename|document|documents|path)\b(?:\s+(?:named|called|at|as|to|is|a|an|the))*[\s:=]*|(?:文件名?|文档|路径)(?:名为|叫做?|是|为|：|:)?\s*)$")
_FILE_WORD_AFTER = re.compile(r"(?i)^\s*(?:(?:的)?(?:文件|文档)|(?:file|document)\b)")
_DIRECT_FILE_OPERAND = re.compile(r"(?i)^\s*(?:(?:\b(?:a|an|the|new|empty|original|output|file|files|filename|document|documents|report|named|called|as|at|to|into)\b|工作目录(?:里|中|内)?的?|授权(?:工作)?目录(?:里|中|内)?的?|一个|一份|新的?|空的?|原始|名为|叫做?|文件名?|文档|路径|报告|到|为|成|在)[\s:=：-]*)*$")
_FILE_LIST_JOIN = re.compile(r"(?i)^\s*(?:,\s*(?:and\b\s*)?|and\b\s*|，\s*|、\s*|(?:以及|和|及|与)\s*)")
_QUOTED_FILE_CONTENT = re.compile(
    r"(?i)^\s*[,，]?\s*(?:containing\s+exactly|with\s+(?:the\s+)?(?:content|text)(?:\s*(?:must\s+be|is|=|:))?(?:\s+exactly)?"
    r"|(?:content|text)\s*(?:must\s+be|is|=|:)\s*(?:exactly)?|(?:内容|文本)(?:必须)?(?:恰好为|严格为|完全为|为|是|：|:))"
    r"\s*(?:`[^`\r\n]*`|\"[^\"\r\n]*\"|'[^'\r\n]*'|“[^”\r\n]*”|‘[^’\r\n]*’)")
_CHINESE_FILE_BOUNDARY = re.compile(r"(?i)\.(?:" + "|".join(sorted(_BARE_FILE_SUFFIXES, key=lambda value: (-len(value), value)))
    + r")(?P<join>以及|和|及|与)")
_CHINESE_FILE_JOIN = re.compile(r"以及|和|及|与")
_FORMAT_FILE_INTRO = re.compile(r"(?i)\s*(?:(?:一个|一份|一张)\s*)?"
    r"(?:Word|PDF|Excel|PowerPoint|Markdown|JSON|CSV|文本)\s*(?:文件|文档|表格)(?:名为|叫作|叫做)?\s*")
_MEMBER_FILE_INTRO = re.compile(r"(?<=[,，])\s*(?:(?:其中|里面|包内|内部)\s*)?(?:包含|包括|含有|内含)\s*")


def _file_scan_view(clause, visible, verbs):
    """Keep offsets while separating structural Chinese file-list grammar.

    Do not ban 和/与/及 from basenames. A join is structural only after an
    observed known extension and before another actual filename; explicit
    quoted filenames remain single tokens. Format introductions are masked
    only at an operand start, with a complete filename following the noun.
    """
    view = list(clause)
    operand_starts = [verb.end() for verb in verbs]

    def right_file(position):
        while position < len(clause) and clause[position].isspace(): position += 1
        candidate = _FILE_TOKEN.match(clause, position)
        if candidate is None: return False
        path = candidate.group("quoted") or candidate.group("bare")
        return path.rsplit(".", 1)[-1].casefold() in _BARE_FILE_SUFFIXES

    boundaries = [(match.start("join"), match.end("join")) for match in _CHINESE_FILE_BOUNDARY.finditer(visible)]
    # A quoted left operand hides its extension from `visible`, while its
    # closing quote can still be followed by an unspaced Chinese conjunction.
    for candidate in _FILE_TOKEN.finditer(clause):
        if candidate.group("quoted") is None: continue
        if candidate.group("quoted").rsplit(".", 1)[-1].casefold() not in _BARE_FILE_SUFFIXES: continue
        position = candidate.end()
        while position < len(clause) and clause[position].isspace(): position += 1
        join = _CHINESE_FILE_JOIN.match(visible, position)
        if join is not None: boundaries.append((join.start(), join.end()))
    for start, end in boundaries:
        if right_file(end):
            view[start:end] = " " * (end - start)
            operand_starts.append(end)
    for start in operand_starts:
        intro = _FORMAT_FILE_INTRO.match(visible, start)
        if intro is not None and right_file(intro.end()):
            view[intro.start():intro.end()] = " " * (intro.end() - intro.start())
    # Without spaces, descriptive prose itself can be greedily included in
    # a bare filename ("里面包含甲.txt"). Keep its offset in the raw gap so
    # it cannot look like a plain top-level comma-separated operand.
    for intro in _MEMBER_FILE_INTRO.finditer(visible):
        if right_file(intro.end()):
            view[intro.start():intro.end()] = " " * (intro.end() - intro.start())
    return "".join(view)


def _file_list_operand(gap, previous_path):
    connector = _FILE_LIST_JOIN.match(gap)
    if connector and (_DIRECT_FILE_OPERAND.fullmatch(gap[connector.end():])
                      or _FORMAT_FILE_INTRO.fullmatch(gap[connector.end():])):
        return True
    # A text file's closed literal can precede a second top-level operand.
    # For ZIP, "containing" describes members rather than the binary file's
    # text. Never lift those member names into workspace obligations.
    if previous_path.rsplit(".", 1)[-1].casefold() == "zip":
        return False
    content = _QUOTED_FILE_CONTENT.match(gap)
    if content is None:
        return False
    rest = gap[content.end():]
    connector = _FILE_LIST_JOIN.match(rest)
    return bool(connector and (_DIRECT_FILE_OPERAND.fullmatch(rest[connector.end():])
                               or _FORMAT_FILE_INTRO.fullmatch(rest[connector.end():])))


def _file_operand(path, *, quoted, prefix, tail, suffix, listed=False):
    """Do not turn amounts, code expressions or website names into file goals."""
    explicit = bool(_FILE_WORD_BEFORE.search(prefix) or _FILE_WORD_AFTER.search(suffix))
    direct = listed or bool(_DIRECT_FILE_OPERAND.fullmatch(tail))
    if "://" in path or path.startswith("//") or re.match(r"\s*\(", suffix):
        return False
    extension = path.rsplit(".", 1)[-1].casefold()
    if not direct:
        return False
    if path.casefold().startswith("www.") or extension in _DOMAIN_SUFFIXES:
        return explicit
    if explicit:
        return True
    if extension in _BARE_FILE_SUFFIXES:
        return True
    # Numeric filenames are valid when explicitly quoted as the verb operand;
    # bare decimals/version numbers never become hard artifact constraints.
    return bool(quoted and re.fullmatch(r"(?i)v?[0-9]+(?:[._-][0-9]+)+", path))


class GoalContractError(ValueError):
    def __init__(self, code="INVALID_GOAL_CONTRACT"):
        self.code = code
        super().__init__(code)


def _sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise GoalContractError("INVALID_GOAL_NUMBER")
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise GoalContractError("INVALID_GOAL_NUMBER") from None


def _requirement(raw, index):
    allowed = {"id", "kind", "path", "format", "exact_text", "contains_text", "sha256", "json_values", "write_observed"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise GoalContractError()
    item = deepcopy(raw)
    kind = item.get("kind", "artifact")
    if not isinstance(kind, str) or kind not in {"artifact", "read_file", "gui"}:
        raise GoalContractError()
    item["kind"] = kind
    identifier = item.get("id", f"requirement-{index + 1}")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier):
        raise GoalContractError()
    item["id"] = identifier
    if kind == "gui":
        if set(item) - {"kind", "id"}:
            raise GoalContractError()
        return item
    path = item.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or "\0" in path:
        raise GoalContractError()
    item["path"] = path
    item.setdefault("format", "auto")
    if not isinstance(item["format"], str) or item["format"] not in _FORMATS:
        raise GoalContractError()
    if "exact_text" in item and (not isinstance(item["exact_text"], str) or len(item["exact_text"]) > 1048576):
        raise GoalContractError()
    if "contains_text" in item:
        if (not isinstance(item["contains_text"], list) or len(item["contains_text"]) > 32
                or any(not isinstance(text, str) or not text or len(text) > 65536 for text in item["contains_text"])):
            raise GoalContractError()
    if "sha256" in item and (not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"])):
        raise GoalContractError()
    if "write_observed" in item and type(item["write_observed"]) is not bool:
        raise GoalContractError()
    if kind == "read_file" and item.get("write_observed"):
        raise GoalContractError()
    if "json_values" in item:
        values = item["json_values"]
        if not isinstance(values, list) or not 1 <= len(values) <= 64:
            raise GoalContractError()
        for check in values:
            if (not isinstance(check, dict) or set(check) != {"pointer", "number"}
                    or not isinstance(check["pointer"], str) or len(check["pointer"]) > 1024
                    or check["pointer"] and not check["pointer"].startswith("/")
                    or re.search(r"~(?![01])", check["pointer"])):
                raise GoalContractError()
            check["number"] = str(_decimal(check["number"]))
    return item


def _unquoted_prefix(text):
    """Exclude quoted instructions, preserving only verbs outside quotes."""
    return re.sub(r"`[^`]*`|\"[^\"]*\"|'[^']*'|“[^”]*”|‘[^’]*’",
                  lambda match: " " * len(match.group()), text)


def infer_file_requirements(task):
    """Conservative direct imperative guards, never a general intent router.

    Quoted examples, negations, explanations and read requests do not become
    write obligations. Any omitted or ambiguous semantics still go to a judge.
    Paths here are inert strings; WorkspaceFiles enforces existing authority.
    """
    text = re.sub(r"```[\s\S]*?```", " ", task)
    found = []
    for clause in re.split(r"[\n;；。！？!?]", text):
        visible = _unquoted_prefix(clause)
        if _NON_ACTION.search(visible) or _EXPLANATORY.search(visible):
            continue
        writes, reads = list(_ACTION.finditer(visible)), list(_READ.finditer(visible))
        verbs = sorted([*writes, *reads], key=lambda item: item.start())
        scan = _file_scan_view(clause, visible, verbs)
        scan_visible = _unquoted_prefix(scan)
        # Start at the verb boundary as Chinese instructions need not put a
        # space between 创建 and report.txt. Quote masking preserves offsets.
        candidates = {}
        for verb in verbs:
            for match in _FILE_TOKEN.finditer(scan, verb.end()):
                candidates[match.start(), match.end()] = match
        previous_operand = None
        for match in sorted(candidates.values(), key=lambda item: item.start()):
            if any(match.start() <= item.start() < match.end() for item in verbs):
                # An earlier verb's scan can include a following unspaced
                # Chinese command (再写入report.txt). It is not a second path.
                continue
            prefix = visible[:match.start()]
            prior_writes = [verb for verb in writes if verb.end() <= match.start()]
            prior_reads = [verb for verb in reads if verb.end() <= match.start()]
            if not prior_writes and not prior_reads:
                continue
            last_write = prior_writes[-1].end() if prior_writes else -1
            last_read = prior_reads[-1].end() if prior_reads else -1
            kind = "artifact" if last_write > last_read else "read_file"
            verb_end = max(last_write, last_read)
            tail = scan_visible[verb_end:match.start()]
            if len(tail) > 90 or re.search(r"(?i)\b(?:from|using|based\s+on)\b|根据|基于|源文件|输入文件", tail):
                continue
            group = "quoted" if match.group("quoted") is not None else "bare"
            path = clause[match.start(group):match.end(group)].rstrip(",.")
            if match.group("quoted") and (_ACTION.search(path) or _READ.search(path)):
                # A quoted instruction is not a filename obligation. Ambiguous
                # filenames with such words remain for the independent judge.
                continue
            if "://" in path or not path:
                continue
            listed = False
            if previous_operand is not None and previous_operand[1] == verb_end:
                # Require a bare connector from the preceding accepted file,
                # under the same verb. The whole raw gap matters: masking a
                # quoted body or matching an arbitrary trailing "and" would
                # invent workspace outputs from archive members or prose.
                gap = clause[previous_operand[0]:match.start()]
                listed = _file_list_operand(gap, previous_operand[2])
            if not _file_operand(path, quoted=bool(match.group("quoted")), prefix=prefix,
                                 tail=tail, suffix=clause[match.end():], listed=listed):
                continue
            previous_operand = (match.end(), verb_end, path)
            requirement = {"kind": kind, "path": path, "format": "auto"}
            if kind == "artifact" and path.rsplit(".", 1)[-1].casefold() != "zip":
                suffix = clause[match.end():]
                exact = re.match(r"(?i)^\s*[,，]?\s*(?:with\s+)?(?:containing\s+exactly|(?:content|text)\s*(?:must\s+be|is|=|:)\s*exactly|(?:内容|文本)(?:必须)?(?:恰好为|严格为|完全为|为|是|：|:))\s*[`\"'“‘]([^`\"'”’\r\n]*)[`\"'”’]", suffix)
                if exact:
                    requirement["exact_text"] = exact.group(1)
                else:
                    literal = re.match(r"(?i)^\s*[,，]?\s*containing\s+exactly\s+([^\s`\"'“”‘’，。;；.]{1,256})(?=$|[.,，。;；]|\s+(?:and|then)\b)", suffix)
                    if literal:
                        requirement["exact_text"] = literal.group(1)
            existing = next((item for item in found if item["path"] == path), None)
            if existing is not None:
                # A read before a write is often a version check, not a weaker
                # replacement goal. Preserve the final explicit write content;
                # a later read never downgrades an artifact requirement.
                if kind == "artifact":
                    existing.update(requirement)
            else:
                found.append(requirement)
            if len(found) >= 32:
                return found
    return found


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _pointer(value, pointer):
    if not pointer:
        return value
    for part in pointer.split("/")[1:]:
        token = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
            value = value[int(token)]
        else:
            raise ValueError("INVALID_POINTER")
    return value


def _office_content(raw, suffix, files):
    """Read content from the same hashed bytes; never start Office or render.

    The document parser is pure stdlib for OOXML. Text/cell facts support
    content requirements; they make no claim about rendered layout or formula
    recalculation. Explicit limits remain visible to the independent judge.
    """
    from document_tools import DocumentTools
    parser = DocumentTools(files.root, files=files)
    first, metadata = parser._office_text(raw, suffix, "1" if suffix == ".pptx" else "")
    sections = metadata.get("sections", [])
    selected = sections[:32]
    pieces = []
    remaining = 32768
    complete_sections = 0
    if suffix == ".docx":
        pieces = [first[:remaining]]
        truncated = len(first) > remaining
    else:
        truncated = len(sections) > len(selected)
        for index, section in enumerate(selected):
            body = first if index == 0 else parser._office_text(raw, suffix, section)[0]
            if remaining <= 0:
                truncated = True
                break
            pieces.append(body[:remaining])
            complete_sections += len(body) <= remaining
            truncated |= len(body) > remaining
            remaining -= len(body)
    result = {"status": "partial" if truncated else "available",
              "scope": "parsed_office_content_not_visual_layout", "format": suffix.lstrip("."),
              "text": "\n\n".join(pieces), "truncated": bool(truncated),
              "sections": list(selected), "total_sections": len(sections),
              "sections_complete": complete_sections if sections else None,
              "visual_layout_verified": False, "rendering_performed": False}
    if suffix != ".xlsx":
        return result
    # Text extraction alone cannot distinguish numeric literals, text cells
    # and uncalculated formulas. Preserve those actual OOXML cell types.
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relation_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    typed, value_characters, cells_truncated = [], 0, False
    with parser._archive(raw) as archive:
        workbook = parser._xml(archive, "xl/workbook.xml")
        relations = {item.get("Id"): item.get("Target") for item in parser._xml(archive, "xl/_rels/workbook.xml.rels")
                     if item.get("TargetMode") != "External"}
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.text or "" for item in node.findall(f".//{{{namespace}}}t"))
                      for node in parser._xml(archive, "xl/sharedStrings.xml").findall(f"{{{namespace}}}si")]
        for sheet in workbook.findall(f"{{{namespace}}}sheets/{{{namespace}}}sheet"):
            if sheet.get("name") not in selected:
                continue
            source = parser._relationship("xl", relations[sheet.get(f"{{{relation_namespace}}}id")])
            for cell in parser._xml(archive, source).findall(f".//{{{namespace}}}c"):
                if len(typed) >= 512 or value_characters >= 32768:
                    cells_truncated = True
                    break
                cell_type = cell.get("t", "n")
                value = cell.findtext(f"{{{namespace}}}v", "")
                formula = cell.findtext(f"{{{namespace}}}f", None)
                if cell_type == "s":
                    value = shared[int(value)]
                elif cell_type == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(f".//{{{namespace}}}t"))
                kind = ("formula_cached" if formula is not None else
                        "number_literal" if cell_type == "n" else
                        "text" if cell_type in {"s", "inlineStr", "str"} else
                        "boolean" if cell_type == "b" else "error" if cell_type == "e" else "other")
                row = {"sheet": sheet.get("name", ""), "cell": cell.get("r", ""), "kind": kind,
                       "value": value[:4096], "truncated": len(value) > 4096}
                if formula is not None:
                    row["formula"] = formula[:4096]
                    row["truncated"] |= len(formula) > 4096
                typed.append(row)
                value_characters += len(row["value"]) + len(row.get("formula", ""))
    result.update(cells=typed, cells_truncated=cells_truncated or len(sections) > len(selected),
                  formula_recalculation_verified=False)
    return result


class GoalContract:
    """An immutable original goal plus host-specified or inferred constraints."""

    def __init__(self, original_task, *, requirements=(), require_judge=True):
        if (not isinstance(original_task, str) or not original_task.strip()
                or len(original_task) > 131072 or type(require_judge) is not bool
                or not isinstance(requirements, (list, tuple)) or len(requirements) > 64):
            raise GoalContractError()
        self.original_task = original_task
        self.require_judge = require_judge
        items = [_requirement(item, index) for index, item in enumerate(requirements)]
        if len({item["id"] for item in items}) != len(items) or not require_judge and not items:
            # Unknown natural language can never silently become a deterministic
            # success. Only explicit host requirements may opt out of the judge.
            raise GoalContractError()
        self._requirements = tuple(items)
        self.goal_id = digest({"task": original_task, "requirements": items, "require_judge": require_judge})

    @classmethod
    def from_task(cls, original_task, *, requirements=(), require_judge=True):
        if not isinstance(requirements, (list, tuple)):
            raise GoalContractError()
        explicit = list(requirements)
        if not require_judge and not explicit:
            raise GoalContractError()
        inferred = infer_file_requirements(original_task) if isinstance(original_task, str) else []
        paths = {item.get("path") for item in explicit if isinstance(item, dict)}
        # Host constraints take precedence for a given file and are never
        # weakened by a heuristic. The judge still sees the complete task.
        inferred = [item for item in inferred if item["path"] not in paths]
        return cls(original_task, requirements=explicit + inferred, require_judge=require_judge)

    @property
    def requirements(self):
        return deepcopy(list(self._requirements))

    def _file_check(self, requirement, files, verifier, observations):
        check = {"id": requirement["id"], "kind": requirement["kind"],
                 "path": requirement["path"], "passed": False, "issues": []}
        try:
            target, raw, sha = files.read_bytes(requirement["path"])
            path = files.display_path(target)
        except (ValueError, OSError):
            check["issues"] = ["GOAL_FILE_UNAVAILABLE"]
            return check, None
        check.update(path=path, sha256=sha, bytes=len(raw), readback=True)
        proof = {"id": "file-" + digest({"path": path, "sha256": sha})[:32],
                 "source": "host_file_readback", "path": path, "sha256": sha, "bytes": len(raw)}
        try:
            text = raw.decode("utf-8-sig")
            proof.update(text=text[:32768], truncated=len(text) > 32768, characters=len(text))
        except UnicodeError:
            text = None
            proof.update(binary=True, truncated=False)
        if target.suffix.casefold() in {".docx", ".xlsx", ".pptx"}:
            try:
                proof["content_readback"] = _office_content(raw, target.suffix.casefold(), files)
            except (ValueError, OSError, TypeError, KeyError, IndexError, RecursionError):
                proof["content_readback"] = {"status": "unavailable", "code": "GOAL_DOCUMENT_CONTENT_UNAVAILABLE",
                    "scope": "parsed_office_content_not_visual_layout", "visual_layout_verified": False,
                    "rendering_performed": False}
        try:
            report = verifier.verify(path, kind=requirement["format"],
                                     expected_bytes=0 if not raw else None)
            proof["format_check"] = {key: report[key] for key in ("passed", "kind", "checks", "issues", "sha256") if key in report}
            if not report.get("passed"):
                check["issues"].append("GOAL_FILE_FORMAT_MISMATCH")
            elif report.get("sha256") != sha:
                check["issues"].append("GOAL_FILE_CHANGED")
        except (ValueError, OSError, TypeError):
            check["issues"].append("GOAL_FILE_FORMAT_UNVERIFIED")
        if "exact_text" in requirement and text != requirement["exact_text"]:
            check["issues"].append("GOAL_FILE_TEXT_MISMATCH")
        if any(text is None or part not in text for part in requirement.get("contains_text", ())):
            check["issues"].append("GOAL_FILE_CONTENT_MISSING")
        if requirement.get("sha256", sha) != sha:
            check["issues"].append("GOAL_FILE_HASH_MISMATCH")
        if "json_values" in requirement:
            try:
                data = json.loads(text, parse_float=Decimal, parse_int=Decimal,
                                  parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
                                  object_pairs_hook=_strict_object)
                if any(_decimal(_pointer(data, item["pointer"])) != _decimal(item["number"])
                       for item in requirement["json_values"]):
                    raise ValueError("MISMATCH")
            except (ValueError, TypeError, KeyError, IndexError, InvalidOperation, RecursionError):
                check["issues"].append("GOAL_FILE_NUMBER_MISMATCH")
        if requirement.get("write_observed"):
            matching = False
            for record in observations:
                receipt = record.get("file_write", {})
                try:
                    if (record.get("effect") == "file" and isinstance(receipt, dict)
                            and receipt.get("source") == "host_successful_file_write"
                            and receipt.get("sha256") == sha and receipt.get("bytes") == len(raw)
                            and files.resolve(receipt.get("path", "")) == target):
                        matching = True
                except (ValueError, TypeError, OSError):
                    continue
            if not matching:
                check["issues"].append("GOAL_WRITE_NOT_OBSERVED")
        check["passed"] = not check["issues"]
        return check, proof

    def snapshot(self, files, evidence, *, answer="", artifacts=(), gui_goal_verified=False, pending_effects=()):
        """Fresh authorized readback, suitable as the independent judge's input.

        Candidate answer is separate from evidence. Call this again after file,
        answer or tool changes; a receipt for the previous snapshot is stale.
        """
        if not isinstance(answer, str):
            raise GoalContractError("INVALID_GOAL_ANSWER")
        observed = evidence.snapshot() if hasattr(evidence, "snapshot") else deepcopy(evidence)
        if not isinstance(observed, dict) or observed.get("schema") != "aura.goal_evidence.v1":
            raise GoalContractError("INVALID_GOAL_EVIDENCE")
        verifier = ArtifactVerifier(files.root, files=files)
        checks, file_proofs = [], []
        for requirement in self._requirements:
            if requirement["kind"] == "gui":
                checks.append({"id": requirement["id"], "kind": "gui", "passed": bool(gui_goal_verified),
                               "issues": [] if gui_goal_verified else ["GOAL_GUI_UNVERIFIED"]})
            else:
                check, proof = self._file_check(requirement, files, verifier, observed.get("observations", []))
                checks.append(check)
                if proof is not None and not any(item["id"] == proof["id"] for item in file_proofs):
                    file_proofs.append(proof)
        # Artifact reports are host readback only. Their path/hash/format facts
        # can help the judge, but their task_fully_verified flag is not authority.
        reports = [{key: item[key] for key in ("path", "passed", "sha256", "bytes", "kind", "checks", "issues") if key in item}
                   for item in artifacts if isinstance(item, dict)]
        readback_issues = []
        # An unrecognized natural-language task may still have real outputs.
        # Read those actual artifacts too: a write tool's JSON acknowledgement
        # and a format pass do not show their contents to the independent judge.
        for index, report in enumerate(reports):
            path = report.get("path")
            if not isinstance(path, str):
                readback_issues.append("GOAL_ARTIFACT_READBACK_FAILED")
                continue
            proof = next((item for item in file_proofs if item["path"] == path), None)
            if proof is None:
                _, proof = self._file_check({"id": f"artifact-readback-{index}", "kind": "read_file",
                    "path": path, "format": "auto"}, files, verifier, observed.get("observations", []))
                if proof is not None:
                    file_proofs.append(proof)
            if proof is None:
                readback_issues.append("GOAL_ARTIFACT_READBACK_FAILED")
            elif report.get("sha256") and report["sha256"] != proof["sha256"]:
                readback_issues.append("GOAL_ARTIFACT_CHANGED")
        facts = {"observed": observed, "files": file_proofs, "requirement_checks": checks,
                 "artifact_checks": reports, "gui_goal_verified": bool(gui_goal_verified),
                 "pending_effects": list(pending_effects), "readback_issues": sorted(set(readback_issues))}
        if hasattr(evidence, "redact"):
            facts = evidence.redact(facts)
        evidence_id = digest({"goal_id": self.goal_id, "facts": facts})
        return {"schema": "aura.goal_snapshot.v1", "goal_id": self.goal_id,
                "evidence_id": evidence_id, "original_task": self.original_task,
                "requirements": self.requirements, "answer": answer,
                "answer_sha256": _sha_text(answer), "facts": facts,
                "evidence_ids": [item["id"] for item in observed.get("observations", [])] + [item["id"] for item in file_proofs],
                "judge_required": self.require_judge}

    def assess(self, snapshot, independent_verdict=None):
        issues = []
        if not self._valid_snapshot(snapshot):
            return {"passed": False, "issues": ["GOAL_SNAPSHOT_INVALID"], "requirements": [], "judge_status": "invalid"}
        facts = snapshot["facts"]
        checks = facts["requirement_checks"]
        if any(not item.get("passed") for item in checks):
            issues.append("GOAL_REQUIREMENT_FAILED")
        if facts.get("pending_effects"):
            issues.append("GOAL_EFFECT_UNRESOLVED")
        issues.extend(facts.get("readback_issues", []))
        status = "not_required"
        if self.require_judge:
            status, code = self._verdict(snapshot, independent_verdict)
            if code:
                issues.append(code)
        return {"passed": not issues, "issues": issues, "requirements": deepcopy(checks),
                "judge_status": status, "goal_id": self.goal_id,
                "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
                "scope": "Original host task; authorized current file readback, real tool observations and independently bound semantic judgment."}

    def _valid_snapshot(self, snapshot):
        try:
            if (not isinstance(snapshot, dict) or snapshot.get("schema") != "aura.goal_snapshot.v1"
                    or snapshot.get("goal_id") != self.goal_id
                    or snapshot.get("original_task") != self.original_task
                    or snapshot.get("requirements") != self.requirements
                    or snapshot.get("judge_required") is not self.require_judge
                    or not isinstance(snapshot.get("answer"), str)
                    or snapshot.get("answer_sha256") != _sha_text(snapshot["answer"])):
                return False
            facts = snapshot["facts"]
            checks = facts["requirement_checks"]
            if (not isinstance(checks, list) or len(checks) != len(self._requirements)
                    or any(check.get("id") != requirement["id"] or check.get("kind") != requirement["kind"]
                           or type(check.get("passed")) is not bool
                           for check, requirement in zip(checks, self._requirements))):
                return False
            ids = [item["id"] for item in facts["observed"]["observations"]] + [item["id"] for item in facts["files"]]
            return (snapshot["evidence_ids"] == ids and len(set(ids)) == len(ids)
                    and snapshot["evidence_id"] == digest({"goal_id": self.goal_id, "facts": facts}))
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            return False

    def _verdict(self, snapshot, value):
        if value is None:
            return "missing", "GOAL_JUDGE_MISSING"
        if (not isinstance(value, dict) or set(value) != _VERDICT_KEYS
                or value.get("schema") != "aura.goal_verdict.v1"
                or not isinstance(value.get("status"), str)
                or value["status"] not in {"satisfied", "not_satisfied", "unknown", "answer_only"}
                or not isinstance(value.get("evidence_ids"), list)
                or any(not isinstance(item, str) for item in value["evidence_ids"])
                or len(value["evidence_ids"]) > 128
                or len(set(value["evidence_ids"])) != len(value["evidence_ids"])):
            return "invalid", "GOAL_JUDGE_INVALID"
        if any(value.get(key) != snapshot.get(key) for key in ("goal_id", "evidence_id", "answer_sha256")):
            return "stale", "GOAL_JUDGE_STALE"
        known = set(snapshot["evidence_ids"])
        if not set(value["evidence_ids"]) <= known:
            return "invalid", "GOAL_JUDGE_INVALID"
        status = value["status"]
        if status == "answer_only":
            # Read-only questions can be answered without arbitrary tool calls.
            # A required deliverable or an actual effect excludes this shortcut.
            if (any(item["kind"] in {"artifact", "gui"} for item in self._requirements)
                    or snapshot["facts"]["observed"].get("effect_count")
                    or snapshot["facts"].get("pending_effects") or not snapshot["answer"].strip()):
                return "invalid", "GOAL_ANSWER_ONLY_INVALID"
            return status, None
        if status == "satisfied":
            # A claimed action/derived answer needs cited actual observations.
            # No-evidence conversational answers must use answer_only explicitly.
            if not value["evidence_ids"]:
                return "invalid", "GOAL_JUDGE_EVIDENCE_MISSING"
            return status, None
        return status, "GOAL_NOT_SATISFIED" if status == "not_satisfied" else "GOAL_UNVERIFIED"

"""Pure trusted fixture/reference/byte oracles. Never executes candidate code."""
from __future__ import annotations

import csv
from decimal import Decimal,localcontext
import hashlib
import io
import json
import random
import re
import stat
import zipfile

MEMBERS={"solution.py","results.json"}
HASH=re.compile(r"[a-f0-9]{64}\Z")
AMOUNT=re.compile(r"(?:0|[1-9][0-9]{0,29})\.[0-9]{2}\Z")
TOTAL=re.compile(r"-?(?:0|[1-9][0-9]{0,33})\.[0-9]{2}\Z")

SEED_SOURCE='''def group_totals(rows):
    totals = {}
    for row in rows:
        if row["status"] != "pending":
            category = row["category"]
            totals[category] = totals.get(category, 0.0) + float(row["amount"])
    return [{"category": category, "total": f"{total:.2f}"}
            for category, total in totals.items()]
'''

FIXED_SOURCE='''from decimal import Decimal, localcontext


def group_totals(rows):
    with localcontext() as context:
        context.prec = 80
        totals = {}
        for row in rows:
            if row["status"] == "pending":
                continue
            category = row["category"]
            amount = Decimal(row["amount"])
            if row["status"] == "refund":
                amount = -amount
            totals[category] = totals.get(category, Decimal(0)) + amount
        return [{"category": category,
                 "total": format((total if total else Decimal(0)).quantize(Decimal("0.01")), "f")}
                for category, total in sorted(totals.items())]
'''

SPEC='''# Owned offline Python repair fixture
Nonce: {nonce}

Repair the actual solution.py function group_totals(rows). rows is a list of
dictionaries with exactly category, status, amount string fields.

All test amounts are NONNEGATIVE decimal strings with exactly two fractional
digits and at most 30 integer digits. status is paid, refund, or pending.
paid adds amount; refund SUBTRACTS amount; pending is ignored completely and
does not create a category. A negative NET total is allowed. Every category
with any paid/refund row remains present even when its net total is zero.
Return a list of exactly {{"category": str, "total": str}} records, sorted by
the original category string using Python Unicode codepoint order. Do not
normalize, trim, merge, or casefold category strings. Format totals with exactly
two fractional digits and no exponent/grouping/leading plus; zero is "0.00".
Empty rows returns []. Do not mutate rows or the dictionaries it contains.
There are at most 100 rows per case. Use Decimal with sufficient explicit
local context precision (80 is sufficient); Python's default 28 is not.

Use the stdlib only. No network, packages, applications, GUI or external data.
Preserve SPEC.md and input.csv exactly. Read input.csv with csv.DictReader.
Save the repaired solution.py in the actual workspace first. Then use
compute_project_artifact on current SHA-256 read-only snapshots of solution.py,
SPEC.md and input.csv to compile/run the source and publish fixed.zip. The ZIP
must contain ONLY solution.py (exactly the actual saved source bytes) and
results.json (strict JSON output of group_totals on all input.csv records).
Do not publish results.json or temporary build files to the workspace.
After completion the workspace may contain ONLY solution.py, SPEC.md,
input.csv and fixed.zip. Read back the actual outputs before completion.
'''


def sha(raw):return hashlib.sha256(raw).hexdigest()


def unique_members(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("PROJECT_QA_DUPLICATE_JSON")
        result[key]=value
    return result


def strict_json(raw):
    def bad(_):raise ValueError("PROJECT_QA_NONFINITE_JSON")
    return json.loads(raw.decode("utf-8"),object_pairs_hook=unique_members,parse_constant=bad)


def encode(value):return json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")


def reference(rows):
    """Trusted Decimal only. Not shared with or staged beside candidate code."""
    if type(rows) is not list or len(rows)>100:raise ValueError("PROJECT_QA_ROWS_INVALID")
    with localcontext() as context:
        context.prec=80;totals={}
        for row in rows:
            if (type(row) is not dict or set(row)!={"category","status","amount"}
                    or any(type(v) is not str for v in row.values()) or not AMOUNT.fullmatch(row["amount"])
                    or row["status"] not in {"paid","refund","pending"}):raise ValueError("PROJECT_QA_ROWS_INVALID")
            if row["status"]=="pending":continue
            number=Decimal(row["amount"])
            if row["status"]=="refund":number=-number
            totals[row["category"]]=totals.get(row["category"],Decimal(0))+number
        return [{"category":name,"total":format((value if value else Decimal(0)).quantize(Decimal(".01")),"f")}
                for name,value in sorted(totals.items())]


def visible_rows(nonce):
    if type(nonce) is not str or not re.fullmatch("[a-f0-9]{32}",nonce):raise ValueError("PROJECT_QA_NONCE_INVALID")
    whale="鲸鱼🌊_"+nonce[:6]
    return [{"category":name,"status":status,"amount":amount} for name,status,amount in [
        (whale,"paid","0.10"),("A-big","paid","9007199254740993.01"),(whale,"paid","0.20"),
        ("A-big","refund","0.02"),(whale,"refund","0.05"),(whale,"pending","999.99"),
        ("café","paid","3.50"),("café","refund","4.75"),("e\u0301","paid","0.01"),
        ("only-pending","pending","5.00"),("零净额","paid","2.00"),("零净额","refund","2.00"),
        ("三十位精度","paid","999999999999999999999999999999.99"),("三十位精度","refund","0.01")]]


def csv_bytes(rows):
    stream=io.StringIO(newline="");writer=csv.DictWriter(stream,fieldnames=("category","status","amount"),lineterminator="\n")
    writer.writeheader();writer.writerows(rows);return stream.getvalue().encode("utf-8")


def hidden_cases(seed):
    if type(seed) is not int or seed<0:raise ValueError("PROJECT_QA_SEED_INVALID")
    rng=random.Random(seed);tag=f"{rng.getrandbits(64):016x}"
    def row(c,s,a):return {"category":c,"status":s,"amount":a}
    cases=[[],[row("pending_"+tag,"pending","999999999999999999999999999999.99")],
        [row("small_"+tag,"paid","0.10"),row("small_"+tag,"paid","0.20")],
        [row("refund_"+tag,"paid","1.00"),row("refund_"+tag,"refund","9.25")],
        [row("big_"+tag,"paid","999999999999999999999999999999.99"),row("big_"+tag,"refund","0.01")],
        [row("鲸鱼🌊","paid","1.00"),row("é","paid","2.00"),row("e\u0301","refund","0.01"),row("Z","paid","0.00")]]
    names=["类别🐳_"+tag,"Z_"+tag,"a_"+tag,"é_"+tag,"e\u0301_"+tag]
    for _ in range(5):
        batch=[row(rng.choice(names),rng.choice(["paid","refund","pending"]),
            str(rng.randrange(10**26,10**29))+f".{rng.randrange(100):02d}") for _ in range(35)]
        cases.append(batch);permutation=list(batch);rng.shuffle(permutation);cases.append(permutation)
    return cases


def result_shape(value):
    if type(value) is not list or len(value)>100:return False
    names=[]
    for row in value:
        if (type(row) is not dict or set(row)!={"category","total"} or type(row["category"]) is not str
                or type(row["total"]) is not str or not TOTAL.fullmatch(row["total"]) or row["total"]=="-0.00"):return False
        names.append(row["category"])
    return names==sorted(set(names))


def package_oracle(raw,current_source,expected):
    checks={"ZIP_STRICT_TWO_MEMBERS":False,"ZIP_SOURCE_MATCHES_ACTUAL_SAVED_SOURCE":False,"ZIP_RESULTS_STRICT_EXACT":False}
    metadata={}
    try:
        if type(raw) is not bytes or len(raw)>4*1024*1024 or not raw:raise ValueError()
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            entries=package.infolist()
            if len(entries)!=2 or {i.filename for i in entries}!=MEMBERS:raise ValueError()
            if any(i.orig_filename!=i.filename or i.is_dir() or i.flag_bits&1 or i.file_size>262144
                   or stat.S_IFMT(i.external_attr>>16) not in {0,stat.S_IFREG} for i in entries):raise ValueError()
            if sum(i.file_size for i in entries)>524288:raise ValueError()
            source=package.read("solution.py");results=package.read("results.json")
        checks["ZIP_STRICT_TWO_MEMBERS"]=True
        checks["ZIP_SOURCE_MATCHES_ACTUAL_SAVED_SOURCE"]=source==current_source
        value=strict_json(results)
        checks["ZIP_RESULTS_STRICT_EXACT"]=result_shape(value) and value==expected
        metadata={"zip_sha256":sha(raw),"source_sha256":sha(source),"results_sha256":sha(results),"zip_bytes":len(raw)}
    except (ValueError,OSError,UnicodeError,zipfile.BadZipFile,RuntimeError,KeyError,NotImplementedError,RecursionError,OverflowError):pass
    return {"passed":all(checks.values()),"checks":checks,"metadata":metadata}


def publication_witnesses(history,inputs,zip_sha,zip_bytes):
    """Only paired actual tool results, never assistant summaries, are evidence."""
    calls={};found=[]
    for row in history:
        if row.get("role")=="assistant":
            for call in row.get("tool_calls",[]):
                function=call.get("function",{})
                if function.get("name")=="compute_project_artifact":
                    try:arguments=strict_json(function["arguments"].encode("utf-8"))
                    except (ValueError,AttributeError,UnicodeError,KeyError):continue
                    identity=call.get("id")
                    if type(identity) is not str or not identity:continue
                    calls[identity]=arguments if identity not in calls else None
        elif row.get("role")=="tool" and row.get("name")=="compute_project_artifact":
            arguments=calls.get(row.get("tool_call_id"))
            if type(arguments) is not dict:continue
            try:
                rows=arguments["inputs"]
                if type(rows) is not list or len(rows)!=len(inputs):continue
                pairs={item["path"]:item["expected_sha256"] for item in rows if type(item) is dict and set(item)=={"path","expected_sha256"}}
                if len(pairs)!=len(inputs) or pairs!=inputs or arguments["output"]["target"]!="fixed.zip":continue
                value=strict_json(row["content"].encode("utf-8"))
                if (value.get("schema")!="aura.project_artifact.v1" or value.get("written")!="fixed.zip"
                        or value.get("sha256")!=zip_sha or type(value.get("bytes")) is not int or value["bytes"]!=zip_bytes
                        or value.get("task_verified") is not False or not HASH.fullmatch(value.get("snapshot_sha256",""))
                        or value.get("artifact",{}).get("path")!="fixed.zip" or value["artifact"].get("sha256")!=zip_sha
                        or type(value["artifact"].get("bytes")) is not int or value["artifact"]["bytes"]!=zip_bytes):continue
                found.append({"snapshot_sha256":value["snapshot_sha256"],"artifact_sha256":zip_sha,
                    "paired_call_sha256":sha(str(row["tool_call_id"]).encode())})
            except (ValueError,UnicodeError,TypeError,KeyError,AttributeError):continue
    return found


def hidden_oracle(value,cases,source_sha,nonce):
    expected=[reference(rows) for rows in cases]
    shape=type(value) is dict and set(value)=={"nonce","source_sha256","compiled","inputs_unchanged","outputs"}
    outputs=value.get("outputs") if shape else None
    checks={"CURRENT_SOURCE_COMPILED_IN_MXC":shape and value["source_sha256"]==source_sha and value["compiled"] is True,
        "FRESH_HIDDEN_NONCE":shape and value["nonce"]==nonce,
        "HIDDEN_INPUTS_NOT_MUTATED":shape and value["inputs_unchanged"] is True,
        "HIDDEN_EXACT_DECIMAL_UNICODE_FUNCTION_RESULTS":type(outputs) is list and len(outputs)==len(expected)
            and all(result_shape(row) for row in outputs) and outputs==expected}
    return {"passed":all(checks.values()),"checks":checks,"case_count":len(cases),
        "expected_sha256":sha(encode(expected)),"observed_sha256":sha(encode(outputs)) if outputs is not None else None}

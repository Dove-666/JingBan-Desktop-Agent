"""A deliberately permissive judge double for pre-existing SDK guard tests.

These tests exercise deterministic/GUI/checkpoint guards, not model judgement
quality. Keeping the judge channel separate prevents it consuming the planner
script. The dedicated test_goal_contract_worker suite additionally tests wrong,
missing, stale and overoptimistic independent verdicts. No fixture result is a
real model benchmark or evidence of product-level task success.
"""
import json


def judge_message(body):
    from goal_judge import POLICY as outcome_policy
    from goal_revision import POLICY as revision_policy
    messages = body.get("messages", [])
    if (len(messages) == 2 and messages[0].get("role") == "system"
            and messages[0].get("content") == revision_policy):
        snapshot = json.loads(messages[1]["content"])
        if snapshot.get("schema") != "aura.goal_revision_snapshot.v1":
            raise AssertionError("Unexpected independent revision payload")
        # Legacy steering fixtures exercise transport/ACK, not cancellation
        # interpretation. They retain every requirement; dedicated revision
        # SDK fixtures below separately test supersession and invalid patches.
        return {"role": "assistant", "content": json.dumps({"schema": "aura.goal_revision_patch.v1",
            "timeline_id": snapshot["timeline_id"], "revision_id": snapshot["revision_id"],
            "base_requirements_id": snapshot["base_requirements_id"], "status": "resolved",
            "operations": [{"op": "keep", "requirement_id": item["id"]} for item in snapshot["active_requirements"]]})}
    if (len(messages) != 2 or messages[0].get("role") != "system"
            or messages[0].get("content") != outcome_policy):
        return None
    snapshot = json.loads(messages[1]["content"])
    if snapshot.get("schema") != "aura.goal_snapshot.v1":
        raise AssertionError("Unexpected independent judgement payload")
    facts = snapshot["facts"]
    observations = facts["observed"]["observations"]
    rejected = (facts["pending_effects"] or facts.get("readback_issues")
                or any(not row["passed"] for row in facts["requirement_checks"])
                or any(not row["passed"] for row in facts["artifact_checks"]))
    evidence = snapshot["evidence_ids"]
    status = "not_satisfied" if rejected else "satisfied" if evidence else "answer_only"
    return {"role": "assistant", "content": json.dumps({
        "schema": "aura.goal_verdict.v1", "goal_id": snapshot["goal_id"],
        "evidence_id": snapshot["evidence_id"], "answer_sha256": snapshot["answer_sha256"],
        "status": status, "evidence_ids": evidence[:128] if status == "satisfied" else []})}


def send_judge(handler, message):
    payload = json.dumps({"id": "fixture-independent-judge", "object": "chat.completion",
        "created": 1, "model": "offline", "choices": [{"index": 0, "message": message,
                                                       "finish_reason": "stop"}]}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    try:
        handler.wfile.write(payload)
    except ConnectionError:
        pass

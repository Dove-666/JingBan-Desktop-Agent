"""Owned QA scope/oracle negatives plus actual isolated SDK/DOM task fixtures."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import qa_owned_browser as qa
from tools.qa_owned_browser_worker import exact_fixture_policy, BACKEND
if str(BACKEND) not in sys.path: sys.path.insert(0, str(BACKEND))
from browser_network import BrowserNetworkError, PublicBrowserPolicy
from browser_session import BrowserError, BrowserSession, find_browser


class OwnedBrowserPolicyOracleTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_does_not_grant_other_port_path_public_or_production_loopback(self):
        prefix = "/" + "a" * 32 + "/"
        policy = exact_fixture_policy("http://127.0.0.1:12345", prefix)
        self.assertEqual("127.0.0.1", (await policy.validate("http://127.0.0.1:12345" + prefix + "form"))[3])
        for url in ("http://127.0.0.1:12346" + prefix + "form", "http://127.0.0.1:12345/outside",
                    "http://127.0.0.1:12345" + prefix + "../outside", "http://127.0.0.1:12345" + prefix + "%2e%2e/outside",
                    "http://localhost:12345" + prefix + "form", "https://example.com/", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url), self.assertRaises(BrowserNetworkError): await policy.validate(url)
        with self.assertRaises(BrowserNetworkError): await PublicBrowserPolicy().validate("http://127.0.0.1:12345" + prefix + "form")

    async def test_answer_echo_without_actual_source_or_independent_post_never_passes(self):
        fixture = qa.OwnedFixture()
        try:
            answer = json.dumps({**fixture.values, "sources": [fixture.url("research/a"), fixture.url("research/b")]})
            self.assertFalse(qa.oracle(qa.CASES[0], fixture, answer, [])['passed'])
            answer = json.dumps({"receipt": fixture.receipt, "receipt_url": fixture.url("receipt")})
            fake = [{"role": "tool", "name": "browser_observe", "content": json.dumps({
                "url": fixture.url("receipt"), "text": "receipt=" + fixture.receipt})}]
            self.assertFalse(qa.oracle(qa.CASES[1], fixture, answer, fake)["passed"])
        finally: fixture.close()

    async def test_default_offline_does_not_load_configuration_or_spawn_a_browser(self):
        with tempfile.TemporaryDirectory(prefix="owned-browser-offline-") as owned, \
             patch.object(qa, "run_case", side_effect=AssertionError("Offline must not execute")):
            path = Path(owned) / "report.json"
            self.assertEqual(0, qa.main(["--output", str(path)]))
            self.assertTrue(all(row["status"] == "not_run" and not row["real_model_attempted"] for row in json.loads(path.read_text())["cases"]))


@unittest.skipUnless(find_browser() and importlib.util.find_spec("websockets"), "actual anonymous Edge/Chrome and CDP needed")
class OwnedBrowserRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_redirect_to_other_port_or_non_nonce_route_is_never_fetched(self):
        fixture = qa.OwnedFixture()
        try:
            for guarded, route, blocked in ((guarded, route, blocked) for guarded in (False, True)
                    for route, blocked in (("redirect_port", fixture.prefix + "forbidden"), ("redirect_path", "/outside"))):
                browser = BrowserSession()
                browser.policy = exact_fixture_policy(fixture.origin, fixture.prefix)
                browser.proxy.policy = browser.policy
                if guarded:
                    from tools.qa_http_response import relay_final_http_response
                    original_pipe = browser.proxy._pipe
                    async def guarded_pipe(reader, writer, response_state):
                        response_state.final_headers_forwarded = True
                        return await relay_final_http_response(reader, writer, original_pipe)
                    browser.proxy._relay_http_response = guarded_pipe
                try:
                    try: await browser.navigate(fixture.url(route))
                    except (BrowserNetworkError, BrowserError): pass
                    self.assertNotIn(("GET", blocked), fixture.snapshot()["events"])
                    self.assertTrue(any(row.get("reason") == "BROWSER_PUBLIC_TARGET_REQUIRED" for row in browser._blocked))
                finally:
                    process = browser._process
                    profile = Path(browser._profile.name) if browser._profile is not None else None
                    await browser.close()
                    if process is not None: self.assertIsNotNone(process.returncode)
                    if profile is not None: self.assertFalse(profile.exists())
        finally: fixture.close()


@unittest.skipUnless(find_browser() and (BACKEND / ".venv/Scripts/python.exe").is_file(), "actual SDK/browser needed")
class OwnedBrowserSdkTests(unittest.TestCase):
    def test_qa_candidate_preserves_both_positive_owned_tasks(self):
        for case in qa.CASES:
            with self.subTest(case=case):
                row = qa.run_case(case, response_guard=True)
                self.assertEqual("passed", row["status"], row["independent_oracle"])
                self.assertTrue(row["worker_closed"])
                self.assertEqual(0, row["browser_host_audit"]["gateway_unknown_responses"])

    def test_two_source_research_uses_actual_sdk_dom_and_independent_html_oracle(self):
        row = qa.run_case(qa.CASES[0])
        self.assertEqual("passed", row["status"], {key: row[key] for key in ("status", "error_code", "agent_status", "independent_oracle")})
        self.assertEqual(2, len(row["independent_oracle"]["source_documents"]))
        self.assertTrue(row["worker_closed"])
        self.assertTrue(row["source_snapshot"]["unchanged"])
        self.assertFalse(row["real_model_attempted"])

    def test_form_records_exactly_one_real_post_and_fresh_independent_receipt(self):
        row = qa.run_case(qa.CASES[1])
        self.assertEqual("passed", row["status"], {key: row[key] for key in ("status", "error_code", "agent_status", "independent_oracle")})
        self.assertEqual(1, row["independent_oracle"]["post_count"])
        self.assertTrue(row["source_snapshot"]["unchanged"])
        self.assertTrue(row["worker_closed"])

    def test_production_preheader_response_loss_stays_unknown_without_transport_replays(self):
        row = qa.run_case(qa.CASES[1], lost_response=True, repeat_input=False)
        qa.save_report(qa.ROOT / "artifacts/owned-browser-production-preheader-proof.json", row)
        self.assertEqual(1, row["browser_host_audit"]["dom_click_evaluations"])
        self.assertEqual(1, row["browser_host_audit"]["network_post_events"])
        self.assertEqual("needs_user", row["agent_status"])
        # Historical three-request failure is kept in a different immutable
        # report. This asserts only the new plain-HTTP failure boundary.
        self.assertEqual(1, row["independent_oracle"]["post_count"], row["effect_count"])

    def test_started_response_loss_stays_unresolved_and_same_ref_reclick_cannot_repeat_it(self):
        row = qa.run_case(qa.CASES[1], lost_response=True, response_loss="started_headers")
        self.assertEqual("passed", row["status"], {key: row[key] for key in ("status", "error_code", "agent_status", "independent_oracle")})
        self.assertEqual("needs_user", row["agent_status"])
        self.assertEqual(1, row["independent_oracle"]["post_count"])
        self.assertEqual(1, row["browser_host_audit"]["dom_click_evaluations"])
        self.assertEqual("guard", row["suite"])
        self.assertFalse(row["real_model_attempted"])

    def test_application_idempotency_limits_logical_effect_while_transport_repeats_requests(self):
        row = qa.run_case(qa.CASES[1], lost_response=True, idempotent=True)
        qa.save_report(qa.ROOT / "artifacts/owned-browser-idempotent-loss-guard.json", row)
        self.assertEqual("passed", row["status"], {key: row[key] for key in ("status", "error_code", "agent_status", "independent_oracle")})
        self.assertEqual("needs_user", row["agent_status"])
        self.assertGreaterEqual(row["independent_oracle"]["post_count"], 1)
        self.assertEqual(1, row["effect_count"])
        self.assertEqual("guard", row["suite"])
        self.assertFalse(row["real_model_attempted"])

    def test_qa_only_final_header_guard_reduces_transport_replays_but_keeps_goal_unknown(self):
        rows = []
        for phase in ("pre_headers", "partial_headers", "interim_only"):
            row = qa.run_case(qa.CASES[1], lost_response=True, response_loss=phase, response_guard=True)
            rows.append(row)
            self.assertEqual("passed", row["status"], {key: row[key] for key in ("status", "error_code", "agent_status", "independent_oracle")})
            self.assertEqual(1, row["independent_oracle"]["post_count"])
            self.assertEqual("needs_user", row["agent_status"])
            self.assertEqual(1, row["browser_host_audit"]["dom_click_evaluations"])
            self.assertEqual(1, row["browser_host_audit"]["network_post_events"])
            self.assertEqual(1, row["browser_host_audit"]["fetch_post_events"])
            self.assertEqual(1, row["browser_host_audit"]["gateway_unknown_responses"])
        qa.save_report(qa.ROOT / "artifacts/owned-browser-http-candidate-proof.json", {"cases": rows})

    def test_production_final_header_guard_keeps_each_loss_phase_unknown_without_replay(self):
        rows = []
        for phase in ("pre_headers", "partial_headers", "interim_only"):
            row = qa.run_case(qa.CASES[1], lost_response=True, response_loss=phase)
            rows.append(row)
            self.assertFalse(row["qa_only_http_response_guard"])
            self.assertEqual("passed", row["status"], row["independent_oracle"])
            self.assertEqual("needs_user", row["agent_status"])
            self.assertEqual(1, row["effect_count"])
            self.assertEqual(1, row["independent_oracle"]["post_count"])
            for field in ("dom_click_evaluations", "network_post_events", "fetch_post_events"):
                self.assertEqual(1, row["browser_host_audit"][field])
        qa.save_report(qa.ROOT / "artifacts/owned-browser-production-http-proof.json", {"cases": rows})


if __name__ == "__main__": unittest.main()

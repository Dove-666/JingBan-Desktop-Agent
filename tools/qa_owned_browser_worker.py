"""Explicit owned-QA worker adapter; never imported by production workers.

The constructor grant is minted by the QA host, not tool arguments or an env
variable. Only one exact loopback origin and nonce path prefix are reachable.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "agent_backend/nanobot"


def exact_fixture_policy(origin, prefix):
    if str(BACKEND) not in sys.path: sys.path.insert(0, str(BACKEND))
    from browser_network import PublicBrowserPolicy, BrowserNetworkError
    parsed = urlsplit(origin)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or origin != f"http://127.0.0.1:{parsed.port}"
            or not isinstance(prefix, str) or not re.fullmatch(r"/[a-f0-9]{32}/", prefix)):
        raise ValueError("QA_FIXTURE_SCOPE_INVALID")
    port = parsed.port

    class ExactFixturePolicy(PublicBrowserPolicy):
        def target(self, url):
            try:
                value = urlsplit(url)
                allowed = (isinstance(url, str) and len(url) <= 4096
                    and not any(ord(char) < 32 for char in url)
                    and value.scheme == "http" and value.hostname == "127.0.0.1" and value.port == port
                    and value.username is None and value.password is None
                    and value.path.startswith(prefix) and "%" not in value.path
                    and "\\" not in value.path and ".." not in value.path and "//" not in value.path)
            except (ValueError, TypeError, AttributeError): allowed = False
            if not allowed: raise BrowserNetworkError("BROWSER_PUBLIC_TARGET_REQUIRED")
            return value, "127.0.0.1", port, "127.0.0.1"

    return ExactFixturePolicy()


def worker_main(origin, prefix, audit_path, response_guard=False):
    if str(BACKEND) not in sys.path: sys.path.insert(0, str(BACKEND))
    import browser_session
    original = browser_session.BrowserSession
    audit = {"dom_click_evaluations": 0, "network_post_events": 0, "fetch_post_events": 0, "gateway_unknown_responses": 0,
             "browser_cleanup_confirmed": False}
    audit_path = Path(audit_path)
    if not audit_path.is_absolute() or audit_path.is_symlink(): raise ValueError("QA_AUDIT_PATH_INVALID")
    def save_audit(): audit_path.write_text(json.dumps(audit), encoding="utf-8")

    class OwnedQABrowser(original):
        def __init__(self):
            super().__init__()
            self.policy = exact_fixture_policy(origin, prefix)
            self.proxy.policy = self.policy
            if response_guard:
                # This QA origin permits only HTTP; CONNECT is rejected before
                # _pipe. Production's general HTTP/TLS relay is not modified.
                from qa_http_response import relay_final_http_response
                original_pipe = self.proxy._pipe
                async def final_header_pipe(reader, writer, response_state):
                    response_state.final_headers_forwarded = True
                    if not await relay_final_http_response(reader, writer, original_pipe):
                        audit["gateway_unknown_responses"] += 1; save_audit()
                self.proxy._relay_http_response = final_header_pipe

        async def _evaluate(self, tab, mode, args):
            if mode == "act" and args.get("action") == "click":
                audit["dom_click_evaluations"] += 1
                save_audit()
            return await super()._evaluate(tab, mode, args)

        async def _event(self, frame):
            params = frame.get("params", {})
            if params.get("request", {}).get("method") == "POST":
                if frame.get("method") == "Network.requestWillBeSent": audit["network_post_events"] += 1
                if frame.get("method") == "Fetch.requestPaused": audit["fetch_post_events"] += 1
                save_audit()
            return await super()._event(frame)

        async def close(self):
            process, profile = self._process, self._profile
            await super().close()
            audit["browser_cleanup_confirmed"] = bool(self._close_complete and (process is None or process.returncode is not None)
                and (profile is None or not Path(profile.name).exists()))
            save_audit()

    browser_session.BrowserSession = OwnedQABrowser
    import worker
    asyncio.run(worker.main())

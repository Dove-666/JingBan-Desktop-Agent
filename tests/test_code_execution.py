"""Real child-process QuickJS capability tests, not an OS sandbox claim.

Only synthetic values, temporary canary files and a localhost listener are used.
Run with agent_backend/nanobot/.venv/Scripts/python.exe to use the pinned engine.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_backend.nanobot import code_execution
from agent_backend.nanobot.code_execution import CodeExecutionError, available, run_code


@unittest.skipUnless(available(), "Pinned QuickJS lives in the agent SDK environment")
class CodeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def result(self, code, input=None):
        envelope = json.loads(await run_code(code, input))
        self.assertEqual(envelope["scope"], "pure_json_computation")
        self.assertEqual(envelope["runtime"], "quickjs-ng")
        self.assertIs(envelope["ok"], True)
        return envelope["result"]

    async def error(self, code, input=None, expected="CODE_EVALUATION_FAILED"):
        with self.assertRaises(CodeExecutionError) as caught:
            await run_code(code, input)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)

    async def test_independent_child_receives_only_json_and_minimal_environment(self):
        real_spawn, processes, options = asyncio.create_subprocess_exec, [], []
        async def track(*args, **kwargs):
            options.append((args, kwargs))
            child = await real_spawn(*args, **kwargs)
            processes.append(child)
            return child
        with patch.dict(os.environ, {"AURA_PRIVATE_SECRET": "must-not-inherit", "HTTPS_PROXY": "private-proxy"}), \
                patch.object(code_execution.asyncio, "create_subprocess_exec", side_effect=track):
            self.assertEqual(await self.result("return input.rows.map(row => ({n: row.n * 2}));",
                                              {"rows": [{"n": 2}, {"n": 7}]}), [{"n": 4}, {"n": 14}])
        self.assertEqual(len(processes), 1)
        self.assertNotEqual(processes[0].pid, os.getpid())
        self.assertEqual(processes[0].returncode, 0)
        args, kwargs = options[0]
        self.assertIn("-I", args)
        self.assertTrue(all(key.casefold() in {"systemroot", "systemdrive", "windir"} for key in kwargs["env"]))
        self.assertNotIn("AURA_PRIVATE_SECRET", kwargs["env"])
        self.assertNotIn("HTTPS_PROXY", kwargs["env"])

    async def test_utf8_emoji_source_input_names_and_quotes_are_data(self):
        dangerous = '\\"}); globalThis.PWNED = true; // \n</script>\u2028鲸鱼🐳'
        value = {"SOURCE": dangerous, "INPUT": dangerous, "quotes": ["'", '"', "\\", "\n"],
                 "中文": "你好🐳", "nested": {"__proto__": "data", "constructor": "data"}}
        self.assertEqual(await self.result("const 鲸鱼 = input; return {value:鲸鱼, injected:typeof globalThis.PWNED};", value),
                         {"value": value, "injected": "undefined"})
        self.assertEqual(await self.result('return "中文🐳\\n\\\"引号\\\"";'), '中文🐳\n"引号"')
        await self.error('"}); return {injected:true}; //', expected="CODE_EVALUATION_FAILED")

    async def test_constructor_chains_never_acquire_host_capabilities(self):
        names = ["process", "require", "fetch", "std", "os", "Python", "python", "__import__", "load",
                 "read", "write", "print", "console", "WebSocket", "XMLHttpRequest", "Deno", "Bun", "tools"]
        probe = "return Object.fromEntries(input.map(name => [name, globalThis.constructor.constructor('return typeof ' + name)()]));"
        self.assertEqual(await self.result(probe, names), {name: "undefined" for name in names})
        self.assertEqual(await self.result("return [typeof globalThis.constructor.constructor, typeof Function, typeof eval];"),
                         ["function", "function", "function"])
        # Ordinary JavaScript constructors are permitted; they carry no host IO.
        self.assertEqual(await self.result("return globalThis.constructor.constructor('return 6 * 7')();"), 42)

    async def test_file_canary_is_neither_read_nor_written(self):
        with tempfile.TemporaryDirectory(prefix="aura-code-canary-") as directory:
            source = Path(directory, "private-canary.txt")
            target = Path(directory, "should-never-exist.txt")
            secret = "only-python-fixture-knows-this-content"
            source.write_text(secret, encoding="utf-8")
            attempts = ["return require('fs').readFileSync(input.source, 'utf8');",
                        "return require('fs').writeFileSync(input.target, 'changed');",
                        "return std.open(input.source, 'r').readAsString();",
                        "return os.open(input.target, 577);",
                        "return Python.eval('open(' + JSON.stringify(input.source) + ').read()');",
                        "return globalThis.constructor.constructor('return process')().mainModule.require('fs').readFileSync(input.source);",
                        "return import(input.source);"]
            for code in attempts:
                with self.subTest(code=code):
                    with self.assertRaises(CodeExecutionError) as caught:
                        await run_code(code, {"source": str(source), "target": str(target)})
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertNotIn(str(source), str(caught.exception))
                    self.assertEqual(source.read_text(encoding="utf-8"), secret)
                    self.assertFalse(target.exists())

    async def test_network_canary_receives_no_connection(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listener.settimeout(0.1)
            port = listener.getsockname()[1]
            for code in ("return fetch(input.url);", "return new WebSocket(input.url);",
                         "return new XMLHttpRequest();", "return require('net').connect(input.port, '127.0.0.1');",
                         "return import(input.url);"):
                with self.subTest(code=code):
                    await self.error(code, {"url": f"http://127.0.0.1:{port}/canary", "port": port})
            with self.assertRaises(socket.timeout):
                listener.accept()

    async def test_non_json_returns_are_rejected_without_silent_data_loss(self):
        cases = ["return NaN;", "return Infinity;", "return -Infinity;", "return undefined;",
                 "return {x:undefined};", "return [undefined];", "return function(){};", "return {f(){}};",
                 "return Symbol('private');", "return {x:Symbol()};", "return 1n;", "return [1,,3];",
                 "const x={}; x.self=x; return x;", "return new Date();", "return new Uint8Array([1,2]);",
                 "return Object.defineProperty({}, 'hidden', {value:1});", "return '\\ud800';"]
        for code in cases:
            with self.subTest(code=code):
                await self.error(code)
        self.assertEqual(await self.result("return (12345678901234567890n * 2n).toString();"), "24691357802469135780")

    async def test_intrinsic_mutation_cannot_forge_the_returned_json(self):
        code = """JSON.stringify=()=>'{"forged":true}';
          Object.prototype.toJSON=()=>({forged:true});
          Array.prototype.toJSON=()=>['forged'];
          Object.getOwnPropertyDescriptor=()=>({value:'forged'});
          Object.getPrototypeOf=()=>null; Number.isFinite=()=>true;
          Reflect.ownKeys=()=>['forged']; Object.hasOwn=()=>true;
          return {actual: [1, 2, '鲸鱼🐳'], nested: {ok:true}};"""
        self.assertEqual(await self.result(code), {"actual": [1, 2, "鲸鱼🐳"], "nested": {"ok": True}})
        self.assertEqual(await self.result("return typeof Object.prototype.toJSON;"), "undefined")
        self.assertEqual(await self.result("const x=Object.create(null); x.ok=1; return x;"), {"ok": 1})

    async def test_long_unicode_and_concatenated_results_cross_native_rope_boundary(self):
        text = "".join(chr(0x400 + i) for i in range(512))
        self.assertEqual(await self.result("return [{text:input,length:Array.from(input).length}];", text),
                         [{"text": text, "length": 512}])
        self.assertEqual(await self.result("let value='';for(let i=0;i<4096;i++)value+='鲸';return {text:value};"),
                         {"text": "鲸" * 4096})

    async def test_accessors_and_async_values_never_run_as_serialization_callbacks(self):
        for code in ("return {get x(){while(true){}}};",
                     "return {toJSON(){return {forged:1}}};",
                     "return Promise.resolve(1);", "return (async()=>1)();",
                     "Promise.prototype.then=undefined; return Promise.resolve(1);",
                     "Object.prototype.value=1; return {get x(){while(true){}}};"):
            with self.subTest(code=code):
                await self.error(code)

    async def test_cpu_memory_stack_and_output_limits_are_static_failures(self):
        started = time.monotonic()
        await self.error("while (true) {}", expected="CODE_TIME_LIMIT")
        self.assertLess(time.monotonic() - started, 7)
        await self.error("const x=[]; while(true) x.push(new Array(10000).fill('memory'));", expected="CODE_MEMORY_LIMIT")
        await self.error("function recurse(){return recurse()} return recurse();", expected="CODE_MEMORY_LIMIT")
        await self.error("return 'x'.repeat(262145);", expected="CODE_OUTPUT_LIMIT")
        await self.error("return '鲸'.repeat(100000);", expected="CODE_OUTPUT_LIMIT")

    async def test_error_messages_never_echo_source_input_or_thrown_secret(self):
        secret = "private-code-and-input-canary"
        for code in ("throw new Error(input.secret); // " + secret,
                     "return require(input.secret); // " + secret,
                     "return {get bad(){ throw new Error(input.secret) }};"):
            with self.subTest(code=code):
                with self.assertRaises(CodeExecutionError) as caught:
                    await run_code(code, {"secret": secret})
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(code, str(caught.exception))
                self.assertEqual(str(caught.exception), caught.exception.code)

    async def test_cancel_during_spawn_reaps_real_child_even_with_repeated_stop(self):
        real_spawn, children = asyncio.create_subprocess_exec, []
        created, release = asyncio.Event(), asyncio.Event()
        async def slow_spawn(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            created.set()
            await release.wait()
            return child
        with patch.object(code_execution.asyncio, "create_subprocess_exec", side_effect=slow_spawn):
            task = asyncio.create_task(run_code("while(true){}"))
            await asyncio.wait_for(created.wait(), 3)
            started = time.monotonic()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)

    async def test_cancel_during_evaluation_leaves_no_live_helper(self):
        real_spawn, children, created = asyncio.create_subprocess_exec, [], asyncio.Event()
        async def track(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            created.set()
            return child
        with patch.object(code_execution.asyncio, "create_subprocess_exec", side_effect=track):
            task = asyncio.create_task(run_code("while(true){}"))
            await asyncio.wait_for(created.wait(), 3)
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(await self.result("return 42;"), 42)

    async def test_input_and_source_limits_reject_coercions_before_spawning(self):
        for value in (float("nan"), float("inf"), {1: "non-string key"}, (1, 2), {"x": object()},
                      {"x": "\ud800"}, 9007199254740992):
            with self.subTest(value=type(value).__name__):
                await self.error("return input;", value, expected="CODE_INVALID_REQUEST")
        await self.error("x" * 65537, expected="CODE_INVALID_REQUEST")
        await self.error("return input;", "x" * (2 * 1024 * 1024), expected="CODE_INPUT_LIMIT")


if __name__ == "__main__":
    unittest.main()

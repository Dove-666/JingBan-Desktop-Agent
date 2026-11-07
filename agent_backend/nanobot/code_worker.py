"""One isolated QuickJS value computation; no Python/OS/network JS bindings.

This is a language-runtime boundary, not a general OS sandbox. Never add host
callbacks, module loaders, std/os, environment, filesystem or network objects.
Native engine vulnerabilities require ordinary dependency maintenance.
"""
from __future__ import annotations

import json
import sys

MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 262144


def compute(request):
    import quickjs
    if (not isinstance(request, dict) or set(request) != {"code", "input"}
            or not isinstance(request["code"], str) or not 1 <= len(request["code"]) <= 65536):
        return {"ok": False, "code": "CODE_INVALID_REQUEST"}
    context = quickjs.Context()
    context.set_memory_limit(64 * 1024 * 1024)
    context.set_max_stack_size(512 * 1024)
    context.set_time_limit(2)
    # Capture intrinsics before untrusted code runs. A plain JSON.stringify of
    # its result silently drops undefined/functions and converts NaN to null;
    # it also runs model-defined toJSON methods and can itself be overwritten.
    # Encode only explicit data descriptors without executing getters/toJSON.
    # No engine object/proxy/function is ever passed into a Python callback.
    script = """(function(INPUT, SOURCE, LIMIT) {
      const parse = JSON.parse, quote = JSON.stringify, makeFunction = Function;
      const array = Array.isArray, finite = Number.isFinite;
      const prototype = Object.getPrototypeOf, descriptor = Object.getOwnPropertyDescriptor;
      const ownKeys = Reflect.ownKeys, own = Object.hasOwn, objectPrototype = Object.prototype;
      const Failure = Error;
      const input = parse(INPUT);
      const result = makeFunction('input', '"use strict";\\n' + SOURCE)(input);
      let size = 0;
      const ancestors = [];
      function piece(text) {
        size += text.length;
        if (size > LIMIT) throw new Failure('AURA_CODE_OUTPUT_LIMIT');
        return text;
      }
      function encode(value, depth) {
        if (depth > 128) throw new Failure('AURA_CODE_NON_JSON');
        if (value === null) return piece('null');
        if (typeof value === 'boolean') return piece(value ? 'true' : 'false');
        if (typeof value === 'string') return piece(quote(value));
        if (typeof value === 'number' && finite(value)) return piece(quote(value));
        if (typeof value !== 'object') throw new Failure('AURA_CODE_NON_JSON');
        for (let i = 0; i < ancestors.length; i++)
          if (ancestors[i] === value) throw new Failure('AURA_CODE_NON_JSON');
        const isArray = array(value);
        if (!isArray && prototype(value) !== null && prototype(value) !== objectPrototype)
          throw new Failure('AURA_CODE_NON_JSON');
        const keys = ownKeys(value);
        ancestors[ancestors.length] = value;
        let output = piece(isArray ? '[' : '{');
        let count = 0;
        if (isArray) {
          const length = descriptor(value, 'length').value;
          if (keys.length !== length + 1) throw new Failure('AURA_CODE_NON_JSON');
          for (let i = 0; i < length; i++) {
            const item = descriptor(value, '' + i);
            if (!item || !own(item, 'value')) throw new Failure('AURA_CODE_NON_JSON');
            if (i) output += piece(',');
            output += encode(item.value, depth + 1);
          }
        } else {
          for (let i = 0; i < keys.length; i++) {
            const key = keys[i];
            if (typeof key !== 'string') throw new Failure('AURA_CODE_NON_JSON');
            const item = descriptor(value, key);
            if (!item || !item.enumerable || !own(item, 'value')) throw new Failure('AURA_CODE_NON_JSON');
            if (count++) output += piece(',');
            output += piece(quote(key)) + piece(':') + encode(item.value, depth + 1);
          }
        }
        ancestors.length -= 1;
        return output + piece(isArray ? ']' : '}');
      }
      // QuickJS-NG may retain concatenation ropes (tag -6), which the Python
      // binding cannot return directly. Native JSON parsing materializes a
      // flat string; captured intrinsics keep model mutation out of this step.
      return parse(quote(encode(result, 0)));
    })(""" + json.dumps(json.dumps(request["input"], ensure_ascii=True, allow_nan=False)) + "," \
        + json.dumps(request["code"], ensure_ascii=True) + "," + str(MAX_OUTPUT) + ")"
    try:
        result = context.eval(script)
        if not isinstance(result, str) or len(result.encode("utf-8")) > MAX_OUTPUT:
            return {"ok": False, "code": "CODE_OUTPUT_LIMIT"}
        value = json.loads(result)
        # Lone UTF-16 surrogates are legal JS strings but cannot cross the
        # worker's UTF-8 tool protocol. Keep ordinary Unicode/emoji intact.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return {"ok": True, "result": value,
                "runtime": "quickjs-ng", "scope": "pure_json_computation"}
    except quickjs.JSException as exc:
        value = str(exc).casefold()
        code = ("CODE_OUTPUT_LIMIT" if "aura_code_output_limit" in value else
                "CODE_TIME_LIMIT" if "interrupted" in value else
                "CODE_MEMORY_LIMIT" if any(message in value for message in
                    ("out of memory", "stack overflow", "maximum call stack size exceeded")) else
                "CODE_EVALUATION_FAILED")
        return {"ok": False, "code": code}
    except (UnicodeError, TypeError, ValueError, RecursionError):
        return {"ok": False, "code": "CODE_EVALUATION_FAILED"}


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            value = {"ok": False, "code": "CODE_INPUT_LIMIT"}
        else:
            value = compute(json.loads(raw))
    except ImportError:
        value = {"ok": False, "code": "CODE_RUNTIME_MISSING"}
    except Exception:
        value = {"ok": False, "code": "CODE_INVALID_REQUEST"}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

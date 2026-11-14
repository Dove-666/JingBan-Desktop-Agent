import { publishedCuaChannel, installNativeReceipt } from '../compat-channel.mjs';
var __knownSymbol = (name2, symbol) => (symbol = Symbol[name2]) ? symbol : Symbol.for("Symbol." + name2);
var __typeError = (msg) => {
  throw TypeError(msg);
};
var __using = (stack, value, async) => {
  if (value != null) {
    if (typeof value !== "object" && typeof value !== "function") __typeError("Object expected");
    var dispose, inner;
    if (async) dispose = value[__knownSymbol("asyncDispose")];
    if (dispose === void 0) {
      dispose = value[__knownSymbol("dispose")];
      if (async) inner = dispose;
    }
    if (typeof dispose !== "function") __typeError("Object not disposable");
    if (inner) dispose = function() {
      try {
        inner.call(this);
      } catch (e) {
        return Promise.reject(e);
      }
    };
    stack.push([async, dispose, value]);
  } else if (async) {
    stack.push([async]);
  }
  return value;
};
var __callDispose = (stack, error, hasError) => {
  var E = typeof SuppressedError === "function" ? SuppressedError : function(e, s, m, _) {
    return _ = Error(m), _.name = "SuppressedError", _.error = e, _.suppressed = s, _;
  };
  var fail = (e) => error = hasError ? new E(e, error, "An error was suppressed during disposal") : (hasError = true, e);
  var next = (it) => {
    while (it = stack.pop()) {
      try {
        var result = it[1] && it[1].call(it[2]);
        if (it[0]) return Promise.resolve(result).then(next, (e) => (fail(e), next()));
      } catch (e) {
        fail(e);
      }
    }
    if (hasError) throw error;
  };
  return next();
};

// third_party/deepseek_harness/cua-windows/src/index.ts
import { isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Schema from "@deepseek-ai/schemastery";
import { installCua } from "@deepseek-ai/dsh-cua";
import { CuaConfigFields } from "@deepseek-ai/dsh-cua/config";

// third_party/deepseek_harness/cua-windows/src/native-build.ts
import { constants } from "node:fs";
import { access } from "node:fs/promises";
import { join } from "node:path";
import { buildNativeExecutable as compileExecutable, sourceNativePackage } from "@deepseek-ai/dsh-cua/native-build";
function nativeHostArchitecture(platform, architecture) {
  if (platform !== "win32" || architecture !== "arm64" && architecture !== "x64")
    throw new Error("The Windows Computer Use provider requires Windows on x64 or ARM64");
  return architecture;
}
function nativeExecutablePath(packageRoot, architecture) {
  const root = packageRoot.replace(/([\\/])app\.asar(?=[\\/])/u, "$1app.asar.unpacked");
  return join(root, "native", "bin", `win-${architecture}`, "dsh-cua-windows.exe");
}
async function checkedExecutable(executable, signal) {
  await access(executable, constants.X_OK);
  signal.throwIfAborted();
  return executable;
}
async function buildNativeExecutable(spec) {
  const nativeRoot = join(spec.packageRoot, "native");
  const runtime = `win-${spec.architecture}`;
  const directory = join(nativeRoot, "bin", runtime);
  return compileExecutable({
    ...spec,
    lockPath: join(nativeRoot, ".build", "compiler"),
    argv: [
      "dotnet",
      "publish",
      join(nativeRoot, "entry", "CuaWindows.csproj"),
      "--configuration",
      "Release",
      "--runtime",
      runtime,
      "--self-contained",
      "true",
      "--output",
      directory,
      "-p:EnableWindowsTargeting=true"
    ],
    resolveExecutable: () => checkedExecutable(join(directory, "dsh-cua-windows.exe"), spec.signal)
  });
}
async function prepareNativeExecutable(spec) {
  spec.signal.throwIfAborted();
  if (await sourceNativePackage(spec.packageRoot, "packages/experimental/cua-windows")) return buildNativeExecutable(spec);
  return checkedExecutable(nativeExecutablePath(spec.packageRoot, spec.architecture), spec.signal);
}

// third_party/deepseek_harness/cua-windows/src/native-runtime.ts
import { createHash, randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";
import { addAbortListener } from "node:events";
import { realpath } from "node:fs/promises";
import { PassThrough, Writable } from "node:stream";
import assert2 from "node:assert/strict";
import { NativeError as NativeError2 } from "@deepseek-ai/dsh-cua/native-client";
import { z } from "zod";

// third_party/deepseek_harness/cua-windows/src/native-transport.ts
import { Socket } from "node:net";
import { Duplex } from "node:stream";
import assert from "node:assert/strict";
import { NativeError } from "@deepseek-ai/dsh-cua/native-client";
function connectWindowsPipe(subprocess, config, endpoint, runtimeKey, transport) {
  if (transport === "pipe") {
    const stream2 = new Socket();
    const connected = new Promise((resolve2, reject) => {
      stream2.once("connect", resolve2);
      stream2.once("error", reject);
    });
    void connected.catch(() => {
    });
    stream2.connect(endpoint);
    return { stream: stream2, connected, dispose: async () => {
    } };
  }
  const handle = subprocess.spawn({
    argv: [
      config.helperPath,
      "--bridge",
      "--runtime-key",
      runtimeKey,
      "--connect-timeout-ms",
      String(config.connectTimeoutMs),
      "--grace-ms",
      String(config.graceMs)
    ],
    cwd: config.cwd,
    graceMs: config.graceMs,
    stdio: { stdin: "pipe", stdout: "pipe", stderr: { maxBytes: 65536 } }
  });
  assert(handle.stdin && handle.stdout, "The Windows bridge requires standard input and output");
  const stream = Duplex.from({ readable: handle.stdout, writable: handle.stdin });
  stream.once("end", () => {
    stream.end();
  });
  void handle.done.then((outcome) => {
    if (outcome.exitCode !== 0 || outcome.signal !== null)
      stream.destroy(new NativeError("NATIVE_EXITED", `Windows bridge failed: ${handle.collected.stderr?.readFrom(0).text ?? String(outcome.exitCode)}`));
  }, (error) => {
    stream.destroy(error instanceof Error ? error : new Error(String(error)));
  });
  return {
    stream,
    connected: Promise.resolve(),
    async dispose() {
      handle.stdin?.end();
      const timer = setTimeout(() => {
        handle.terminate();
      }, config.graceMs);
      try {
        const outcome = await handle.done;
        if (!await handle.waitForExit()) throw new NativeError("NATIVE_CLEANUP", "Windows bridge process did not exit");
        if (outcome.exitCode !== 0 || outcome.signal !== null)
          throw new NativeError("NATIVE_CLEANUP", `Windows bridge exited unsuccessfully (${String(outcome.exitCode)}, ${String(outcome.signal)})`);
      } finally {
        clearTimeout(timer);
      }
    }
  };
}

// third_party/deepseek_harness/cua-windows/src/native-runtime.ts
var Ready = z.object({
  type: z.literal("ready"),
  protocol: z.literal(1),
  runtimeKey: z.string().min(1),
  instanceId: z.string().min(1)
});
var Started = Ready.extend({ endpoint: z.string().min(1), launcherId: z.string().min(1) });
var closedFrame = Buffer.from('{"type":"closed"}\n');
function failure(error) {
  return error instanceof Error ? error : new Error(String(error));
}
async function interruptible(operation, signal) {
  var _stack = [];
  try {
    const cancelled = Promise.withResolvers();
    const _listener = __using(_stack, addAbortListener(signal, () => {
      cancelled.reject(signal.reason);
    }));
    return await Promise.race([operation, cancelled.promise]);
  } catch (_) {
    var _error = _, _hasError = true;
  } finally {
    __callDispose(_stack, _error, _hasError);
  }
}
function firstFrame(stream, signal) {
  signal.throwIfAborted();
  return new Promise((resolve2, reject) => {
    let bytes = Buffer.alloc(0);
    const clean = () => {
      stream.off("data", data).off("error", rejectFrame).off("end", ended).off("close", ended);
      signal.removeEventListener("abort", aborted);
    };
    const rejectFrame = (error) => {
      clean();
      reject(error);
    };
    const ended = () => {
      rejectFrame(new NativeError2("NATIVE_EOF", "Helper ended before native readiness"));
    };
    const aborted = () => {
      rejectFrame(failure(signal.reason));
    };
    const data = (chunk) => {
      const newline = chunk.indexOf(10);
      const size = newline < 0 ? chunk.length : newline + 1;
      if (bytes.length + size > 8192) {
        rejectFrame(new NativeError2("NATIVE_PROTOCOL", "Native readiness exceeded its byte limit"));
        return;
      }
      bytes = Buffer.concat([bytes, chunk.subarray(0, size)]);
      if (newline < 0) return;
      stream.pause();
      clean();
      try {
        resolve2({ value: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, -1))), rest: chunk.subarray(size) });
      } catch (error) {
        reject(new NativeError2("NATIVE_PROTOCOL", "Invalid native readiness frame", { cause: error }));
      }
    };
    stream.on("data", data).on("error", rejectFrame).on("end", ended).on("close", ended);
    signal.addEventListener("abort", aborted, { once: true });
  });
}
var WindowsHelper = class {
  constructor(owner, config) {
    this.owner = owner;
    this.config = config;
    this.ready = this.readiness.promise;
    void this.ready.catch(() => {
    });
    this.done = this.run();
    void this.done.catch(() => {
    });
  }
  ready;
  done;
  sockets = /* @__PURE__ */ new Set();
  readiness = Promise.withResolvers();
  startup = new AbortController();
  handle;
  phase = "starting";
  stopTimer;
  error;
  get ended() {
    return this.phase === "closing" || this.phase === "closed";
  }
  async stop() {
    if (!this.ended) {
      this.phase = "closing";
      this.startup.abort(new NativeError2("NATIVE_CLOSED", "Computer Use is shutting down"));
      this.handle?.stdin?.end();
      this.stopTimer = setTimeout(() => {
        this.handle?.terminate();
      }, this.config.graceMs);
    }
    await this.done;
  }
  async run() {
    const startupTimer = setTimeout(() => {
      this.startup.abort(new NativeError2("NATIVE_CONNECT_TIMEOUT", "Windows helper did not become ready before connectTimeoutMs; check helper diagnostics and Windows application-control policy"));
    }, this.config.connectTimeoutMs);
    try {
      const helperPath = await realpath(this.config.helperPath);
      const digest = createHash("sha256");
      for await (const chunk of createReadStream(helperPath, { signal: this.startup.signal })) digest.update(chunk);
      const runtimeKey = digest.digest("hex");
      const instanceId = randomUUID().replaceAll("-", "");
      this.startup.signal.throwIfAborted();
      const handle = this.owner.subprocess.spawn({
        argv: [
          helperPath,
          "--serve",
          "--instance-id",
          instanceId,
          "--runtime-key",
          runtimeKey,
          "--connect-timeout-ms",
          String(this.config.connectTimeoutMs),
          "--grace-ms",
          String(this.config.graceMs)
        ],
        cwd: this.config.cwd,
        graceMs: this.config.graceMs,
        stdio: { stdin: "pipe", stdout: "pipe", stderr: { maxBytes: 65536 } }
      });
      this.handle = handle;
      assert2(handle.stdin && handle.stdout, "The helper requested piped control and readiness streams");
      handle.stdout.on("error", (error) => {
        this.error ??= error;
        this.startup.abort(error);
      });
      const exit = handle.done.then((outcome2) => {
        const diagnostic = handle.collected.stderr?.readFrom(0).text.trim() || "no diagnostic output";
        throw new NativeError2(
          "NATIVE_EXITED",
          `Windows helper exited before readiness (${String(outcome2.exitCode)}): ${diagnostic}. Check Windows application-control policy and helper signing.`
        );
      });
      let frame;
      try {
        frame = await Promise.race([firstFrame(handle.stdout, this.startup.signal), exit]);
      } catch (error) {
        if (error instanceof NativeError2 && error.code === "NATIVE_EOF") await interruptible(exit, this.startup.signal);
        throw error;
      }
      const ready = Started.parse(frame.value);
      if (ready.runtimeKey !== runtimeKey || ready.launcherId !== instanceId || frame.rest.length !== 0)
        throw new NativeError2("NATIVE_PROTOCOL", "Windows helper readiness has another identity or unexpected output");
      this.startup.signal.throwIfAborted();
      handle.stdout.on("data", () => {
        this.error ??= new NativeError2("NATIVE_PROTOCOL", "Windows helper wrote unsolicited readiness data");
        void this.stop().catch((error) => {
          this.error ??= failure(error);
        });
      });
      handle.stdout.resume();
      clearTimeout(startupTimer);
      this.phase = "ready";
      this.readiness.resolve(ready);
      const outcome = await handle.done;
      if (!this.ended) this.error ??= new NativeError2(
        "NATIVE_EXITED",
        `Windows helper stopped (${String(outcome.exitCode)}, ${String(outcome.signal)}); select applications again`
      );
    } catch (error) {
      this.error ??= failure(error);
    } finally {
      clearTimeout(startupTimer);
      this.phase = "closing";
      const error = this.error ?? new NativeError2("NATIVE_CLOSED", "Windows helper closed; select applications again");
      for (const socket of this.sockets) socket.destroy(error);
      const handle = this.handle;
      try {
        if (handle) {
          handle.stdin?.end();
          const timer = setTimeout(() => {
            handle.terminate();
          }, this.config.graceMs);
          try {
            await handle.done;
            if (!await handle.waitForExit()) throw new NativeError2("NATIVE_CLEANUP", "Windows helper process range did not exit");
          } finally {
            clearTimeout(timer);
          }
        }
      } finally {
        clearTimeout(this.stopTimer);
        this.phase = "closed";
        this.readiness.reject(error);
      }
    }
  }
};
var WindowsChannel = class {
  stdout = new PassThrough();
  stdin;
  done;
  startup = new AbortController();
  readiness = Promise.withResolvers();
  closed = Promise.withResolvers();
  socket;
  closing = false;
  closeTimer;
  constructor(helper, config, pollIntervalMs, launchWaitMs, owner, transport) {
    this.stdin = new Writable({
      write: (chunk, _encoding, callback) => {
        void this.readiness.promise.then((socket) => {
          if (this.closing) {
            callback(new NativeError2("NATIVE_CLOSED", "Native connection is closing"));
            return;
          }
          socket.write(chunk, callback);
        }, callback);
      },
      final: (callback) => {
        this.close();
        callback();
      },
      destroy: (error, callback) => {
        this.close();
        callback(error);
      }
    });
    for (const stream of [this.stdin, this.stdout]) stream.on("error", () => {
    });
    void this.readiness.promise.catch(() => {
    });
    void this.closed.promise.catch(() => {
    });
    this.done = this.run(helper, config, pollIntervalMs, launchWaitMs, owner, transport);
    void this.done.catch(() => {
    });
  }
  close() {
    if (this.closing) return;
    this.closing = true;
    this.startup.abort(new NativeError2("NATIVE_CLOSED", "Native connection closed before readiness"));
    if (this.socket && !this.socket.destroyed) this.socket.write('{"type":"close"}\n');
  }
  waitForClose() {
    return this.closed.promise;
  }
  async run(helperPromise, config, pollIntervalMs, launchWaitMs, owner, transport) {
    let helper;
    let connection;
    let closed;
    let caught;
    let admitted = false;
    const observation = { acknowledged: false };
    const timer = setTimeout(() => {
      this.startup.abort(new NativeError2("NATIVE_CONNECT_TIMEOUT", "Windows native connection exceeded connectTimeoutMs; check helper diagnostics and Windows application-control policy"));
    }, config.connectTimeoutMs);
    try {
      helper = await interruptible(helperPromise, this.startup.signal);
      const ready = await interruptible(helper.ready, this.startup.signal);
      this.startup.signal.throwIfAborted();
      connection = connectWindowsPipe(owner.subprocess, config, ready.endpoint, ready.runtimeKey, transport);
      const socket = connection.stream;
      this.socket = socket;
      helper.sockets.add(socket);
      let socketError;
      closed = new Promise((resolve2, reject) => {
        socket.on("error", (error) => {
          socketError = error;
        });
        socket.on("close", () => {
          if (socketError) reject(socketError);
          else if (!observation.acknowledged) reject(new NativeError2("NATIVE_EOF", "Windows pipe closed without cleanup acknowledgement"));
          else resolve2();
        });
      });
      void closed.catch(() => {
      });
      const abort = () => {
        socket.destroy(failure(this.startup.signal.reason));
      };
      this.startup.signal.addEventListener("abort", abort, { once: true });
      try {
        const frame = firstFrame(socket, this.startup.signal);
        void frame.catch(() => {
        });
        await connection.connected;
        this.startup.signal.throwIfAborted();
        {
          const {
            maxRequestBytes,
            maxResponseBytes,
            axTimeoutMs,
            textEventIntervalMs,
            screenshotTimeoutMs,
            timeoutMs,
            graceMs,
            maxClickHoldMs
          } = config;
          socket.write(JSON.stringify({
            type: "hello",
            protocol: 1,
            runtimeKey: ready.runtimeKey,
            config: { maxRequestBytes, maxResponseBytes, axTimeoutMs, textEventIntervalMs, screenshotTimeoutMs, timeoutMs, graceMs, pollIntervalMs, launchWaitMs, maxClickHoldMs }
          }) + "\n");
        }
        const response = await frame;
        const accepted = Ready.parse(response.value);
        if (accepted.runtimeKey !== ready.runtimeKey || accepted.instanceId !== ready.instanceId)
          throw new NativeError2("NATIVE_PROTOCOL", "Windows pipe belongs to another helper instance");
        this.startup.signal.throwIfAborted();
        admitted = true;
        this.startup.signal.removeEventListener("abort", abort);
        clearTimeout(timer);
        const close = () => {
          this.closeTimer ??= setTimeout(() => {
            socket.destroy(new NativeError2("NATIVE_CLEANUP", "Windows pipe did not acknowledge cleanup"));
          }, config.graceMs);
        };
        this.startup.signal.addEventListener("abort", close, { once: true });
        let prefix = Buffer.alloc(0);
        const receive = (chunk) => {
          let offset = 0;
          while (offset < chunk.length) {
            const newline = chunk.indexOf(10, offset);
            const end = newline < 0 ? chunk.length : newline + 1;
            const piece = chunk.subarray(offset, end);
            if (prefix !== void 0) prefix = prefix.length + piece.length <= closedFrame.length ? Buffer.concat([prefix, piece]) : void 0;
            if (newline >= 0) {
              if (prefix?.equals(closedFrame)) {
                if (!this.closing) {
                  socket.destroy(new NativeError2("NATIVE_PROTOCOL", "Unexpected Windows cleanup acknowledgement"));
                  return;
                }
                observation.acknowledged = true;
              }
              prefix = Buffer.alloc(0);
            }
            offset = end;
          }
          if (!this.closing) this.stdout.write(chunk);
        };
        socket.on("data", receive);
        if (response.rest.length !== 0) receive(response.rest);
        socket.resume();
        this.readiness.resolve(socket);
        try {
          await closed;
        } finally {
          this.startup.signal.removeEventListener("abort", close);
        }
      } finally {
        this.startup.signal.removeEventListener("abort", abort);
      }
    } catch (error) {
      const cause = failure(error);
      if (!this.closing || admitted) caught = cause;
      this.readiness.reject(cause);
    } finally {
      clearTimeout(timer);
      clearTimeout(this.closeTimer);
      this.closing = true;
      this.socket?.destroy();
      if (closed) {
        try {
          await closed;
        } catch (error) {
          if (admitted) caught ??= failure(error);
        }
      }
      if (this.socket) helper?.sockets.delete(this.socket);
      let cleanupError;
      try {
        await connection?.dispose();
      } catch (error) {
        cleanupError = failure(error);
        caught ??= cleanupError;
      }
      if (admitted && !observation.acknowledged) {
        cleanupError = new NativeError2("NATIVE_CLEANUP", "Windows pipe ended without native cleanup acknowledgement");
      }
      this.stdin.destroy();
      if (caught) this.stdout.destroy(caught);
      else this.stdout.end();
      if (cleanupError) this.closed.reject(cleanupError);
      else this.closed.resolve();
    }
    if (caught) throw caught;
  }
};
var WindowsNativeRuntime = class {
  /**
   * @param owner Managed subprocess provider.
   * @param pollIntervalMs Clipboard and application-readiness polling interval.
   * @param launchWaitMs Maximum observation wait after a Shell launch; expiry is not a launch failure.
   * @param transport Resolved native Windows or WSL connection method.
   */
  constructor(owner, pollIntervalMs, launchWaitMs, transport = "pipe") {
    this.owner = owner;
    this.pollIntervalMs = pollIntervalMs;
    this.launchWaitMs = launchWaitMs;
    this.transport = transport;
  }
  helper;
  disposal;
  channels = /* @__PURE__ */ new Set();
  /**
   * Connect one Agent without transferring process ownership to that Agent.
   * @param config Executable identity and per-connection limits.
   * @returns The byte stream used by the shared native request client.
   */
  connect(config) {
    if (this.disposal) throw new NativeError2("NATIVE_CLOSED", "Windows Computer Use is disposed");
    const acquire = async () => {
      if (this.helper?.ended) await this.helper.done;
      if (this.disposal) throw new NativeError2("NATIVE_CLOSED", "Windows Computer Use is disposed");
      if (!this.helper || this.helper.ended) this.helper = new WindowsHelper(this.owner, config);
      return this.helper;
    };
    const channel = new WindowsChannel(acquire(), config, this.pollIntervalMs, this.launchWaitMs, this.owner, this.transport);
    this.channels.add(channel);
    void channel.done.then(() => this.channels.delete(channel), () => this.channels.delete(channel));
    return channel;
  }
  /** Close Agent pipes and join this Host's connector; other Hosts retain their service leases. */
  dispose() {
    return this.disposal ??= (async () => {
      for (const channel of this.channels) channel.close();
      const channels = await Promise.allSettled([...this.channels].map((channel) => channel.waitForClose()));
      let processError;
      try {
        await this.helper?.stop();
      } catch (error) {
        processError = error;
      }
      const errors = [];
      for (const result of channels) {
        if (result.status === "rejected") errors.push(result.reason);
      }
      if (processError !== void 0) errors.push(processError);
      if (errors.length) throw new AggregateError(errors, "Windows Computer Use cleanup failed");
    })();
  }
};

// third_party/deepseek_harness/cua-windows/src/guidance.ts
import { COMMON_GUIDANCE } from "@deepseek-ai/dsh-cua/guidance";
var PLATFORM_GUIDANCE = "Windows: accessibility tree operations are available only at window level. Simulated mouse and keyboard input are available at window level. Supported modifiers: ctrl, alt, shift, win. control is an alias for ctrl.";
var methodDetails = /* @__PURE__ */ new Map([
  ["cua.getApp", "Windows accepts an exact AUMID returned by listApps or an existing absolute .exe path, including an executable absent from listApps. Display names and PIDs are not alternative selectors. Selection uses the existing application consent flow and starts the application only when it is not running."],
  ["cua.findApps", "Not implemented on Windows. Use listApps to discover applications, or pass a known absolute .exe path to getApp."],
  ["getAccessibilityTree", 'Set the node count or depth for one read. Windows accepts maxElements from 1 to 10000 and maxDepth from 1 to 100. Omitted limits use the provider settings; maxDepth=1 includes only the window root. If the tree is truncated, select a narrower window or increase the relevant limit. includeMenus expands menu contents. Element IDs are UI Automation RuntimeIds formatted as slash-separated integer strings, such as "12/345/6". Pass the complete string unchanged as elementId. Each element lists all its supported patterns, not action names. Disabled is an observed state, not a prohibition on attempting an operation.'],
  ["listActions", 'Actions use case-sensitive Pattern.Action names, such as "Invoke.Invoke", "Toggle.Toggle", and "ExpandCollapse.Expand". Each pattern has a known set of native UI Automation operations. One pattern can expose multiple actions. Use a known operation of an advertised pattern directly; call listActions when you need to confirm the exposed operations. Query methods are not exposed through performAction.'],
  ["invoke", "The provider chooses default action for well-known patterns. A failed operation is not retried through another pattern or simulated input."],
  ["performAction", 'Use the exact Pattern.Action name. Omit argument or pass null for a parameter-free operation. Pass a single parameter as its direct JSON value; pass multiple parameters as an object using their native names. Enum values use their exact names as strings. Examples: await win.performAction(id, "Value.SetValue", "hello"); await win.performAction(id, "Window.SetWindowVisualState", "Normal"); await win.performAction(id, "Transform.Resize", { width: 800, height: 600 }).']
]);
var GUIDANCE = `${PLATFORM_GUIDANCE}

${COMMON_GUIDANCE.replace(/^- ([\w.]+)\(.*$/gm, (entry, method) => {
  const detail = methodDetails.get(method);
  if (!detail) return entry;
  if (method === "getAccessibilityTree") return `${entry.replace(/ — .*$/, "")} \u2014 ${detail}`;
  return `${entry} ${detail}`;
})}`;

// third_party/deepseek_harness/cua-windows/src/windows-host.ts
import { createHash as createHash2 } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { join as join2, win32 } from "node:path";
import { isWsl } from "@deepseek-ai/dsh-native-command";
import { writeFileAtomic } from "@deepseek-ai/dsh-atomic-write";
import { NativeError as NativeError3 } from "@deepseek-ai/dsh-cua/native-client";
import { z as z2 } from "zod";
function windowsHost(facts = {}) {
  if ((facts.platform ?? process.platform) === "win32") return "windows";
  if (isWsl(facts)) return "wsl";
  throw new NativeError3("UNSUPPORTED", "Windows Computer Use requires native Windows or WSL; ordinary Linux is unsupported");
}
async function command(spec, argv) {
  spec.signal.throwIfAborted();
  const signal = AbortSignal.any([spec.signal, AbortSignal.timeout(spec.timeoutMs)]);
  const handle = spec.subprocess.spawn({
    argv,
    cwd: process.cwd(),
    signal,
    graceMs: spec.graceMs,
    stdio: { stdin: "ignore", stdout: { maxBytes: 8192 }, stderr: { maxBytes: 8192 } }
  });
  try {
    const outcome = await handle.done;
    signal.throwIfAborted();
    if (outcome.exitCode !== 0 || outcome.signal !== null)
      throw new NativeError3("WSL_INTEROP", `Windows interoperation failed: ${handle.collected.stderr?.readFrom(0).text ?? outcome.exitCode}`);
    return handle.collected.stdout?.readFrom(0).text.trim() ?? "";
  } finally {
    handle.terminate();
    if (!await handle.waitForExit()) throw new NativeError3("NATIVE_CLEANUP", "Windows preparation process did not exit");
  }
}
async function prepareWsl(spec) {
  const script = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); @{ architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant(); cache = [IO.Path]::Combine([Environment]::GetFolderPath('LocalApplicationData'), 'DeepSeekHarness', 'cua') } | ConvertTo-Json -Compress";
  try {
    const output = await command(spec, [spec.powershellPath, "-NoProfile", "-NonInteractive", "-Command", script]);
    const result = z2.object({ architecture: z2.enum(["x64", "arm64"]), cache: z2.string().refine((value) => win32.isAbsolute(value)) }).strict().parse(JSON.parse(output));
    const cacheDirectory = await command(spec, ["wslpath", "-u", result.cache]);
    if (!cacheDirectory.startsWith("/")) throw new NativeError3("WSL_INTEROP", "wslpath did not return an absolute cache directory");
    return { architecture: result.architecture, cacheDirectory };
  } catch (error) {
    spec.signal.throwIfAborted();
    throw new NativeError3("WSL_INTEROP", "WSL cannot prepare Windows Computer Use; check Windows interoperation and windowsPowerShellPath", { cause: error });
  }
}
async function retainWslHelper(helperPath, cacheDirectory, signal) {
  const bytes = await readFile(helperPath, { signal });
  const digest = createHash2("sha256").update(bytes).digest("hex");
  const directory = join2(cacheDirectory, digest);
  const retained = join2(directory, "dsh-cua-windows.exe");
  await mkdir(directory, { recursive: true });
  let existing;
  try {
    existing = await readFile(retained, { signal });
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  signal.throwIfAborted();
  if (existing && !existing.equals(bytes)) throw new NativeError3("NATIVE_STARTUP", "The cached Windows helper does not match its executable digest");
  if (!existing) await writeFileAtomic(retained, bytes, { mode: 493 });
  signal.throwIfAborted();
  return retained;
}

// third_party/deepseek_harness/cua-windows/src/index.ts
var CAPABILITIES = {
  platform: "windows",
  methods: [
    "getCapabilities",
    "listApps",
    "getApp",
    "activate",
    "show",
    "listWindows",
    "getState",
    "listActions",
    "performAction",
    "invoke",
    "mouseMove",
    "click",
    "setValue",
    "selectText",
    "typeText",
    "paste",
    "pressKey",
    "sendKeyEvent",
    "scroll",
    "drag",
    "screenshot"
  ],
  appLevelAccessibility: false,
  keyboard: ["window"],
  browser: false,
  coordinateSpace: "screenshot-pixels"
};
var name = "cua-windows";
var inject = ["computerUse", "tools", "systemPrompt", "subprocess", "agents"];
var Config = Schema.object({
  ...CuaConfigFields,
  connectTimeoutMs: CuaConfigFields.connectTimeoutMs.default(3e4),
  windowsPowerShellPath: Schema.string().min(1).default("powershell.exe"),
  pollIntervalMs: Schema.number().step(1).min(1).max(2 ** 31 - 1).default(25),
  textEventIntervalMs: CuaConfigFields.textEventIntervalMs.default(0),
  maxRequestBytes: CuaConfigFields.maxRequestBytes.max(2 ** 31 - 1),
  maxResponseBytes: CuaConfigFields.maxResponseBytes.max(2 ** 31 - 1),
  maxClipboardBytes: CuaConfigFields.maxClipboardBytes.max(2 ** 31 - 1)
});
async function apply(ctx, config) {
  const host = windowsHost();
  if (config.helperPath !== void 0 && !isAbsolute(config.helperPath)) throw new Error("Windows CUA helperPath must be absolute");
  const controller = new AbortController();
  const prepare = async () => {
    const wsl = host === "wsl" ? await prepareWsl({
      subprocess: ctx.subprocess,
      signal: controller.signal,
      timeoutMs: config.connectTimeoutMs,
      graceMs: config.graceMs,
      powershellPath: config.windowsPowerShellPath
    }) : void 0;
    let helperPath2 = config.helperPath;
    if (helperPath2 === void 0) {
      const architecture = wsl?.architecture ?? nativeHostArchitecture(process.platform, process.arch);
      helperPath2 = await prepareNativeExecutable({
        packageRoot: resolve(fileURLToPath(new URL("..", import.meta.url))),
        architecture,
        subprocess: ctx.subprocess,
        timeoutMs: config.buildTimeoutMs,
        graceMs: config.graceMs,
        signal: controller.signal
      });
    }
    return wsl ? retainWslHelper(helperPath2, wsl.cacheDirectory, controller.signal) : helperPath2;
  };
  const prepared = prepare();
  ctx.effect(() => async () => {
    controller.abort();
    await prepared.catch(() => {
    });
  }, "cua-windows.native-preparation");
  const helperPath = await prepared;
  controller.signal.throwIfAborted();
  const launchWaitMs = 1e3;
  const runtime = installNativeReceipt(new WindowsNativeRuntime(ctx, config.pollIntervalMs, launchWaitMs, host === "wsl" ? "stdio" : "pipe"));
  ctx.effect(() => () => runtime.dispose(), "cua-windows.native-runtime");
  installCua(ctx, config, {
    helperPath,
    createChannel: (_owner, native) => publishedCuaChannel(runtime.connect(native)),
    guidance: GUIDANCE, getGuidance: () => GUIDANCE,
    capabilities: CAPABILITIES,
    permissionParameters: (_method, permission) => ({ permission })
  });
}
export {
  Config,
  apply,
  inject,
  name
};

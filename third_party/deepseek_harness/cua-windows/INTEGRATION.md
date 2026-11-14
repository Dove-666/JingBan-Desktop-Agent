# DeepSeek Harness Windows computer use

This directory contains the official Windows computer-use provider from
DeepSeek Harness commit `39163dd30f703a79079e2d2446545cff3c453738` (0.2.2-rc.3).
`UPSTREAM.json` records the source hashes and `LICENSE` retains the MIT license.
`package.upstream.json` is the unchanged upstream package manifest.

Runtime TypeScript source is unchanged. The local package manifest
resolves monorepo dependency ranges to published versions and adds a standalone
esbuild command. `tsconfig.build.json` is used only for that standalone bundle.
After bundling, `patch-compiled.mjs` adds the static `guidance` registration
property expected by the npm-published shared CUA package; the newer callback
is retained. It checks the exact registration before changing the generated
JavaScript.

The published shared CUA also uses numeric window tokens, while this Windows
source revision exchanges native handle strings. `compat-channel.mjs` maps each
connection's observed handles to random uint32 tokens and converts them back at
the native boundary. It never coerces a 64-bit handle through a JavaScript
number. Unknown tokens are rejected, and application/snapshot ownership remains
checked by the upstream implementation.

Native teardown writes an authenticated, per-run receipt only after every
channel's actual `closed` acknowledgement and the provider's disposal complete.
The signing token is consumed from a private environment variable and removed
immediately; it is never included in the receipt or passed to shell tools. The
host checks its run/session identity and signature. This is required because
Cordis disposal and SDK shutdown can suppress teardown failures; a process exit
alone does not establish that native input stopped.

The native server also carries a narrow response-admission fix: a client may
receive the response newline before the server's async write has returned. The
upstream implementation rejected the next sequential request as `NATIVE_BUSY`
and closed the pipe. It now distinguishes operation completion from response
flush completion, and awaits the flush before admitting the next operation.
Concurrent desktop operations are still rejected. `patches/` retains the exact
original file, unified patch, and original/patched hashes. The native smoke test
sends 250 sequential requests without delays before testing an owned window.

## Build

Run `npm run build` in this directory after installing development dependencies.
Build the native helper with `./build-native.ps1 -DotnetPath <dotnet.exe>` using
the .NET 10 SDK. The helper is self-contained; users do not need an installed
.NET runtime. Keep the complete `native/bin/win-x64` directory when packaging.
`BUILD.json` records hashes of all generated native files. The native binaries
and intermediate build outputs are excluded from Git.

## Host integration

The host loads `@deepseek-ai/dsh-computer-use` followed by
`@deepseek-ai/dsh-cua-windows`. The desktop adapter supplies an absolute
`helperPath`, so a user task never downloads or builds a helper. Plugin entry
points are absolute file URLs so private SDK profile homes can resolve them. It explicitly
sets `agentPreset: false` to expose the official raw `cua` tool to SDK Agents.
The npm-published 0.2.2-rc.3 shared CUA package predates master's later
`cua_run_code` implementation despite using the same version string. The host
therefore uses the published raw interface and does not claim that the complete
npm dependency graph matches the Windows provider's source revision. The SDK
handles application consent through its user-question channel. Disabling
computer access omits both plugins.

Upstream Windows support includes application/window discovery, application
launch, accessibility inspection/actions, screenshots and keyboard/mouse input.
The bundled upstream README predates the native `launchApp` implementation;
the fixed source revision is the reference for that operation. The helper
requires an unlocked interactive Windows session and Windows Graphics Capture.

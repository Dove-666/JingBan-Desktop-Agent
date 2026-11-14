# Node.js distribution notice

The shipped Windows x64 runtime is **Node.js v24.22.3**, pinned in `runtime.json` with its official archive, executable and license SHA256 values. `tools/packaging/node_runtime.py` downloads that exact archive, verifies both members, and prepares the private `runtime/node` directory. The package builder copies and verifies the same executable, `LICENSE`, and `provenance.json`; it never selects Node from the build machine's PATH.

The complete upstream license and dependency notices from that verified archive are preserved in `LICENSE-v24.22.3.txt`. Source checkout execution prefers the private runtime. Fallback versions must match `^24.20.0 || >=26.7.0`, which excludes Windows versions affected by [nodejs/node#61999](https://github.com/nodejs/node/pull/61999).

`LICENSE-v24.15.0.txt` is retained as a historical notice for earlier builds, not the currently shipped runtime.

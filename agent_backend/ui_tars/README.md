# 鲸伴 UI-TARS bridge

## Goal verification (current)

`finished()` ends the action loop and starts a separate read-only result check.
It is no longer proof of task success. After all inputs settle, a fresh native
screenshot is taken. A separate JSON-only model request receives the exact
original task, the first actual observation, the observation before the last
input (where distinct), and the fresh final observation. It receives no action
model claims or generated completion summary. Only an explicit high-confidence
pass covering every requirement, the target identity and any temporal/order
requirements can produce `status=completed`.

Final frames include `verification={status,method,reason,checked_at}`. Status is
`verified`, `failed` or `unverified`; reasons are fixed codes, never raw model
text or screenshots. A failed check, missing target evidence, malformed JSON,
unavailable model, capture failure or uncertain result produces `needs_user`
and a truthful fixed summary. No input is automatically replayed; in particular,
an uncertain like/send/save action is not clicked a second time. Screenshots
remain in memory and are sent to the same configured model endpoint, not saved.
Visual checking remains model-dependent; it is not a deterministic guarantee
that an external application persisted every off-screen change.

Host-only `options.verifyOnly=true` captures and judges without starting the
action loop. Relative targets such as “第一个视频” or “the previous item” remain
unverified in this mode because it has no pre-action identity/order anchor.
The bridge validates this option as boolean; it is not a model tool argument.
Both action-model and judge requests retain per-request timeouts. The judge has
a 30-second network/body-read deadline, while the whole task remains unlimited
when `timeoutMs=0`. Stop aborts verification as well as action generation.

Only the first parsed prediction is dispatched for each observation. This is
enforced before the pinned SDK's batch dispatch loop: merely skipping extra
native calls would still let a trailing `finished()` set its hardcoded END.

Design references read at pinned source versions (no upstream source copied):

- [Midscene aiAssert](https://github.com/web-infra-dev/midscene/blob/1527e33dac3a289309c3cbc5a1e6745ee05bbf98/packages/core/src/agent/insight.ts),
  MIT: independent assertion query, false/error fails the assertion.
- [Agent S3 worker](https://github.com/simular-ai/Agent-S/blob/0fff0e634869dab72027435b4586e62300185d41/gui_agents/s3/agents/worker.py),
  Apache-2.0: separate reflection model and fresh screenshot assignment.
- Installed UI-TARS SDK 1.2.3 `dist/GUIAgent.js`: the actual observation/model/
  batch action sequence and unconditional END on parsed `finished` were checked.

2026-11-01: `npm run check` and all 66 Node tests passed. Ten added tests cover
missed clicks, independently confirmed success, malformed/unavailable judges,
partial/wrong-target/unknown outcomes, fresh capture failure, batched actions,
verify-only ordinal rejection, Stop during checking and bounded judge requests.
All use synthetic pixels/local HTTP/fake operators, with no user desktop input
or production model requests.

## Windows screenshot recovery (current)

The host-selected `options.pythonExecutable` also runs the fixed, isolated
`windows-screen-capture.py` helper. It captures fresh composed pixels with
Win32 GDI into a newly allocated DIB, selects the foreground window's monitor,
and reports physical monitor coordinates. It reads no titles, clipboard or
task text and writes no image files. It also queries the parent Node process's
DPI awareness and corresponding logical monitor bounds. GDI pixels are
validated at physical size, then normalized into the pinned pointer driver's
logical input domain. A desktop/monitor probe precedes each input action.
The real acceptance environment currently has one 2560 x 1600 monitor;
negative-origin metadata and safe rejection are covered by synthetic tests.
The pinned libnut driver normalizes movement against the primary monitor
without `MOUSEEVENTF_VIRTUALDESK`, so a foreground window on another monitor
is rejected before pixels reach the model. It is never paired with a primary
monitor image or an unsupported pointer offset.

Capture recovery is limited to two read-only attempts. Each prefers GDI and
may fall back to the pinned NutJS capture only when a readable primary monitor
at (0, 0) matches its resulting logical PNG dimensions and DPI density. The
real 150% display reports physical 2560 x 1600 and logical 1707 x 1067; the
fractional X and Y ratios are handled without assuming they are equal.
No action, application launch,
typing, model request, or whole task is automatically replayed. Native handles
are released after every capture; previous images are never substituted.

Active Windows session and input-desktop checks run before and after capture.
Lock/secure desktop and disconnected session failures stop immediately.
Black frames, invalid dimensions and monitor changes cannot reach the model
as usable observations. Fixed errors `DESKTOP_LOCKED`,
`DESKTOP_SESSION_UNAVAILABLE`, `SCREENSHOT_DISPLAY_UNAVAILABLE` and
`SCREENSHOT_BLACK_FRAME` pass through the host with specific static messages.
They contain no native exception text or personal content.

The production archive's 2026-11-01 07:43:04 UTC failed turn records
`gui_screenshot_failed` after 18.016 seconds. It retained no underlying native
exception, so the historical native cause cannot be uniquely recovered.
This change fixes reproduced capture-recovery and availability-check gaps;
it does not attribute that historical failure to an unrecorded cause.

The DPI/input boundary is based on the installed libnut 2.7.1 adapter and its
[Win32 pointer implementation](https://github.com/nut-tree/libnut-core/blob/v2.7.1/src/win32/mouse.c),
which normalizes points against [the primary display dimensions](https://github.com/nut-tree/libnut-core/blob/v2.7.1/src/win32/screen.c).

## Windows text input (current)

The native Agent integration supplies its actual Python interpreter as
`options.pythonExecutable`. This private option is validated as an absolute
Python executable path and is never a model argument. Windows `type` uses the
fixed sibling `windows-text-input.py` helper through stdin JSON and a hidden
child process in the task's process tree. It dispatches UTF-16 Unicode
`SendInput` pairs (including surrogate pairs), uses native Enter for newlines,
checks the foreground window during entry, and rejects partial event counts.
It reads no clipboard and invokes no shell. Cancellation kills the owned
helper and waits for its close; uncertain input is never retried automatically.

This replaces two observed failures in the pinned operator: clipboard reads
could fail, and libnut `typeString` corrupted Chinese/emoji and dropped
newlines. The real owned `QPlainTextEdit` probe passed exact leading/trailing
spaces, Chinese text, emoji and multiline input. A separate real Agent + GUI
model run filled a private fullscreen fixture, saved once, visually confirmed
its result, and passed exact widget/label/file-byte checks in 17.547 seconds.
Evidence lives under
`agent_workspace/qa-gui-agent-f4dae0a2ac5f487ba3518d9a37ac64e3` and
`agent_workspace/qa-gui-agent-759f1e6562b2405fa1f157cbbf5a252a`.
Production settings remained unchanged; arbitrary application task quality
still requires task-specific acceptance. The final Node regression has 39
passing tests, including a fake-helper test proving stdin-only text,
single dispatch and cancellation that waits for child exit.

`options.maxSteps`（或 `max_steps`）默认 `0`，表示不限制任务步数。协议保留整数 `0`，仅在进程内部传给 SDK 的 `maxLoopCount` 转为 `Infinity`；不会用一个很大的整数模拟不限。显式正整数仍可用于兼容和测试。取消、`call_user` 和 `finished` 仍会终止循环；默认不设置整项任务总时限。单次模型请求的网络超时独立保留。

任务上下文跟随模型服务：保留本任务全部图片和动作，适配层在 SDK 调用前恢复完整历史，绕过上游固定保留五张图片的行为。不会静默删图、压缩历史或在超窗后自动重试较小请求；模型服务报告超窗时返回静态 `CONTEXT_LENGTH_EXCEEDED`。请求不设置 `max_tokens` / `max_completion_tokens` 等输出上限，由服务端模型默认控制输出。

宿主可另传 `reference_context` 纯文本，默认空字符串。原始 `task` 保持原文；模型请求将它标为 `USER AUTHORIZED TASK`，将参考资料单独标为不可信的 `HOST REFERENCE CONTEXT`，资料不能扩大、替换或改写授权任务。参考正文不按固定字符数裁剪。本机父子 stdio 接受完整 JSON 行，不再以 128 KiB 作为上下文窗口；JSON、命令、ID 和字段类型校验保留。

观察和动作的 `progress.status` 为 `running` / `completed` / `failed`。`completed` 在实际操作函数返回后发送，表示该次操作完成，不等于整个任务目标已验证；`finished` 仍是模型根据观察报告的任务完成状态。

## 通用视觉模型的像素坐标模式

`config.coordinate_profile`（或 `coordinateProfile`）可选 `native` / `pixel`，默认 `native`。此选项与模型名称无关；`gui_provider` 表示动作格式，不表示模型已经具备专用 UI-TARS 权重。

`native` 保留官方 SDK 的提示词、图片预处理和动作行为，供专用模型使用。`pixel` 仅支持 `ui-tars-1.5` 动作格式，供能够理解截图和输出该动作格式的通用视觉模型使用：图片尺寸与固定 SDK 1.5 解析器的 28 倍数坐标系对齐，每步明确图片的像素宽高，并说明 Windows 热键、桌面快捷方式双击及观察后续画面。热键别名统一，越界坐标不会发送给原生操作器。此适配不代表通用模型具有专用 UI-TARS 的定位能力。

`options.diagnostics: true` 可在现有 `progress` 中输出目标坐标 `point: {x,y}`；默认关闭。诊断仅包含动作类型、步骤和坐标，不包含任务、模型想法、截图、热键内容或输入文本，不写入磁盘。坐标是操作器收到的桌面逻辑坐标。

上述分支使用真实 SDK、localhost 假模型和人工图片验证；这些测试不执行实际桌面输入。

按需启动的 Windows GUI 后台，不替换鲸伴的 Qt 窗口、鲸鱼娘 PNG、眼睛或耳尾效果。普通聊天不需要启动此进程。Node 进程通过 stdin/stdout JSON Lines 通信；不监听网络端口，不自行安装或更新软件，也不保存 API key、截图或任务记录。

## 已安装的依赖

- Node.js >=22；本机验证 Node v24.15.0。
- 官方 `@ui-tars/sdk@1.2.3`、`@ui-tars/operator-nut-js@1.2.3`，锁定在 `package-lock.json`。
- `uuid@14.0.3`：官方 SDK 1.2.3 的构建产物导入 `uuid`，但没有在自身 dependencies 声明；本项目显式补齐，未修改上游文件。
- Windows 本机 native operator 已成功导入。导入检查不截图、不执行鼠标键盘输入。
- GUI 模型尚需宿主提供正确 endpoint、model、key。SDK 已安装不代表模型已配置，也不代表真实 GUI 任务已成功完成。

依赖已在此目录安装。换机器时可手动执行 `npm ci --no-audit --no-fund`，运行时不会执行 npm。

## 启动及检查

宿主使用 argv 数组启动，cwd 为此目录或任意位置均可：

```text
node E:\github\AURA-Desktop-Agent\agent_backend\ui_tars\worker.mjs
```

Node 可由宿主配置路径，或使用 PATH 中 `node` 的绝对解析结果；不要把任务和凭据放入 argv。不要用 `npm start` 启动协议进程，npm 自身横幅会污染 stdout。

```powershell
node agent_backend/ui_tars/probe.mjs
npm --prefix agent_backend/ui_tars run check
npm --prefix agent_backend/ui_tars test
```

`probe.mjs` 只导入现有 SDK 和 native 模块；不会调用截图或输入 API。`worker.mjs` 启动只发 ready，首次 run 才加载 GUI SDK。

## 协议 v1

UTF-8，一行一个 JSON 对象，每行必须以换行结束；最多 128 KiB。stdout 仅协议，不输出第三方调试日志。API key 仅通过 stdin，错误不转发上游 message/stack，progress 不包含截图、输入内容、任务原文或模型推理。

启动输出：

```json
{"protocol":1,"type":"ready","backend":"ui-tars","sdkVersion":"1.2.3","providers":["ui-tars-1.5","ui-tars-1.0","doubao-1.5-15b","doubao-1.5-20b"],"singleMonitor":true,"lazy":true}
```

输入任务示例（凭据只是假值）：

```json
{"type":"run","id":"gui-1","task":"打开记事本","config":{"baseURL":"https://example.invalid/v1/","apiKey":"placeholder","model":"your-gui-model","provider":"ui-tars-1.5"},"options":{"maxSteps":0,"timeoutMs":0}}
```

- `id`：1–128 个英文字母、数字、下划线、点、冒号、连字符。
- `task`：非空，最多 16000 字符。
- `baseURL`：HTTP(S)，不接受 URL 用户名、密码、query 或 fragment。认证放在 apiKey。
- `apiKey`：可为空字符串，适用于无需认证的本地模型服务；不会继承其他服务商的环境变量 key。
- `model`：非空，最多 256 字符。
- `provider`：**动作与坐标解析格式**，不是 API 服务商品牌；见下表。
- `maxSteps`：默认 `0`，不限步数；显式非负安全整数可用于兼容，正整数表示该次测试的步数限制。
- `timeoutMs`：默认 `0`，表示不创建整项任务计时器，不另设 120 秒或 10 分钟兜底。显式有限值（1000～2147483647 毫秒）仅保留用于开发兼容和测试；超出 Node 原生计时器范围会拒绝，避免溢出成极短时限。停止与父进程退出仍会取消任务，单次模型请求仍沿用 SDK 的 30 秒网络超时。
- 同时兼容 `base_url`、`api_key`、`max_steps`、`timeout_ms`，便于 Python 宿主传参。

| provider | SDK uiTarsVersion |
|---|---|
| ui-tars-1.5 | 1.5 |
| ui-tars-1.0 | 1.0 |
| doubao-1.5-15b | doubao-1.5-15B |
| doubao-1.5-20b | doubao-1.5-20B |

所有格式均走 OpenAI 兼容 Chat Completions；任意普通聊天模型不一定能生成正确动作。请勿把 `anthropic` 等聊天服务商字符串直接当作 GUI provider。未验证新的 UI-TARS-2、Seed 或其他模型的动作格式，不自动猜测或套用。

现代 GPT 传输兼容不增加设置项：当 model 最后一段（斜杠后，忽略大小写）匹配 `^(?:o1|o3|o4)(?:-|$)|^gpt-(?:5|6)(?:[.-]|$)` 时，ClientOptions.fetch 在 Chat Completions 请求上将 max_tokens 转为至少 4096 的 max_completion_tokens，设置 reasoning_effort=low，移除 temperature、top_p、seed、stop、frequency_penalty、presence_penalty、thinking。模型名、endpoint、key 和 AbortSignal 保持不变；不自动重试。其他模型，包括 UI-TARS/Doubao，保留 SDK 原请求参数。

## 合成界面格式探测

`synthetic-model-probe.mjs` 是独立探测器，**不导入 native operator、不读取真实屏幕、不执行物理输入**。它用 840×560（28 的倍数）人工界面，让真实 SDK 先解析一次绿色 TARGET 按钮点击，再在 DONE 状态下 finished；判定实际解析点是否落在已知按钮框内。

```text
node E:\github\AURA-Desktop-Agent\agent_backend\ui_tars\synthetic-model-probe.mjs
```

stdin 写入一次 `{"config":{"baseURL":"https://example.invalid/v1/","apiKey":"placeholder","model":"your-model","provider":"ui-tars-1.5"}}` 后关闭 stdin（EOF）。真实模型会接收**合成**图像，最多 2 次请求、总计 65 秒，0 自动重试。stdout 只返回一个 JSON 结果，含 status、code、steps、clicks、parsedTargetHit、finished、parsedPoint；不返回原始 thought、任务内容或凭据。status=passed 仅证明一次合成图的初步格式和定位兼容，不代表完成过真实桌面任务、复杂任务可靠性或达到 GPT 桌面 agent 水平。运行此探测会调用宿主提供的模型；普通启动/probe.mjs 不会。

进度只给阶段、步数和动作名：

```json
{"protocol":1,"type":"progress","id":"gui-1","stage":"observing","step":1,"status":"completed"}
{"protocol":1,"type":"progress","id":"gui-1","stage":"acting","step":1,"action":"click","status":"running"}
{"protocol":1,"type":"progress","id":"gui-1","stage":"acting","step":1,"action":"click","status":"completed"}
```

终态为一个 `final` 或 `error`：

```json
{"protocol":1,"type":"final","id":"gui-1","status":"completed","summary":"已核对当前画面，目标及所需结果均已确认。","steps":4,"verification":{"status":"verified","method":"visual","reason":"GOAL_STATE_CONFIRMED","checked_at":"2026-11-01T00:00:00.000Z"}}
{"protocol":1,"type":"error","id":"gui-1","code":"MODEL_FAILED","message":"The GUI model request failed. Check the endpoint, key, model, and UI-TARS provider format.","steps":1}
```

`final.status` 可为 `completed`、`needs_user`、`cancelled`、`timed_out`。只有 SDK 明确 `finished/end` 才返回 completed；call_user 为 needs_user。SDK completed 是模型报告完成，宿主仍可根据用户任务核验目标软件/文件结果。

错误码包括 INVALID_REQUEST、INVALID_PROVIDER、BUSY、NOT_RUNNING、SDK_UNAVAILABLE、UNSUPPORTED_PLATFORM、STEP_LIMIT、SCREENSHOT_FAILED、MODEL_FAILED、ACTION_FAILED、SDK_FAILED。参数错误不回显用户值；上游异常不会输出模型配置和 key。

取消与探测：

```json
{"type":"abort","id":"gui-1"}
{"type":"ping","id":"probe-1"}
```

同进程始终只允许一个 run。abort 发出 stopping 进度并取消模型请求；任务真正结束前仍保持 BUSY。ping 不加载 native SDK。stdin EOF、SIGINT、SIGTERM 会停止当前任务；EOF 后不接新任务。

## 行为边界

- 当前仅 Windows、单显示器；UI-TARS 官方本机指南仍要求单显示器。
- 取消/超时在模型请求及动作边界生效，已经发出的原生动作不能撤销。宿主应等待终态；必要时仅终止自己启动的进程树，不能把强制终止当作物理动作已撤销。
- 原生 press/release 长按动作不开放；保留原子快捷键、单/双/右键、拖拽、输入、滚动、悬停、等待。未知动作失败，不直接执行任意代码。
- 不自动升级权限、不安装软件、不修改鲸伴设置。实际 GUI 操作需要桌面会话和宿主明确派发任务。
- API 请求由宿主选定的模型 endpoint 接收截图；此桥接没有添加额外云服务或图像 API。

## 验证

2026-11-01：`npm run check` 通过，33 项本地测试通过。其中真实 SDK 已完成 17 轮观察后结束、在第 13 次请求时取消、总超时和用户接管终止。变化的人工帧测试确认每次请求包含最新截图、全部历史图片和按序历史动作；延迟假操作器验证完成进度的真实返回时序。另覆盖模型服务超窗报错、无强制输出上限和完整/增量历史去重。详见 [视觉循环审计](VISUAL_LOOP_AUDIT.md)；本轮验证没有真实桌面截图、物理输入或云 API 请求。

2026-10-31：依赖安装完成，`node probe.mjs` 为 ready；静态检查通过；15 项测试通过，包含真实 SDK、localhost 假模型 HTTP、真实 UI-TARS 动作解析与假 Operator，覆盖完成、取消模型请求、防止取消后执行、最大步数、call_user、任务互斥、JSONL、超大输入、现代 GPT 请求参数、旧模型参数保留、合成图实际点击位置、CLI 输出与凭据不泄漏。

测试全程使用人工构造的像素与假 Operator；**没有采集桌面截图，没有移动物理鼠标，没有执行键盘输入，没有调用真实云模型**。

## 官方来源

- [UI-TARS SDK 文档](https://github.com/bytedance/UI-TARS-desktop/blob/main/docs/sdk.md)
- [GUIAgent 源码](https://github.com/bytedance/UI-TARS-desktop/blob/main/packages/ui-tars/sdk/src/GUIAgent.ts)
- [NutJS operator 源码](https://github.com/bytedance/UI-TARS-desktop/blob/main/packages/ui-tars/operators/nut-js/src/index.ts)
- [本机部署与单显示器限制](https://github.com/bytedance/UI-TARS-desktop/blob/main/docs/quick-start.md)
- SDK 与 operator 均为 Apache-2.0；下游依赖许可证见各安装包。

## Task-scoped recovery and tool composition (2026-11-02)

The host may run multiple GUI subgoals in one task. `GuiBridge.run(parent_task,
action_id=host_id, subtask=bounded_subgoal)` binds the exact parent authorization,
and deduplicates that specific identity. Reusing an identity with different
arguments is rejected; a verified application launch no longer prevents a later
visual or UIA operation. Legacy calls without an action ID keep their one-shot
contract. Action IDs must be minted by the host, never accepted as model authority.

`effect_state` describes input dispatch (`not_dispatched`, `confirmed`, or
`uncertain`); it is separate from independent goal verification. A cleanly
executed action with an unmet goal returns private bounded `feedback.checks` so
the parent planner can inspect and repair. An input exception/cancellation with
unknown delivery blocks further mutating GUI calls. Read-only verification is
still available; `resolve_uncertainty()` is host-only and requires fresh passing
evidence. Earlier action IDs and their cached outcomes never change or replay.

Subgoal verification is labelled `verification_scope=subgoal`; only a final
fresh verification of the whole parent task can prove the parent goal. Explicit
subgoals share their task-owned Node process and bounded screen evidence, which
is cleared when the host closes the task. Screenshots never enter JSONL output.

`await bridge.pause_current(action_id=None)` interrupts only the active child
subgoal so the outer planner can admit a user's steering correction. Explicit
subcalls have distinct wire IDs; a quiet abort arriving after its call settled
cannot cancel a later call or leave a stale error on the shared pipe. A clean
model-wait interruption preserves confirmed dispatch state; uncertain input
delivery still requires fresh observation. Task-owned evidence survives steering
as reference data, while the updated parent authorization replaces instructions.

The visual model working set retains at most eight target/navigation/current
images, bounded recent action text, and explicit omission notices. Its adapter
keeps at most 24 image payloads in memory. A context rejection can retry only a
strictly smaller request; malformed action syntax gets one request-only repair.
Temporary 429/502/503/504 model responses may retry once without repeating input.
Identical actions against unchanged pixels trigger a no-progress handoff, rather
than adding a whole-task step or duration limit. Windows drag at coordinate zero
and horizontal scroll are handled explicitly; unsupported directions fail.

UIA also provides observed window references, verified activation, filtered and
paged snapshots, expand/collapse and directional scroll. It preserves native
identity checks, password filtering and fresh-reference mutation guards. Secure
desktop and unsupported monitor coordinate domains still stop safely; this
release does not claim multi-monitor input support without physical validation.

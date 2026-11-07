# 鲸伴 chat frontend

The desktop chat panel uses the real `@assistant-ui/react` library, its
`ExternalStoreRuntime`, thread primitives and message primitives. The local
light CSS is designed for the existing desktop application. It does not copy the
official demo's stylesheet.

The Python application loads the committed production file at
`desktop_pet/ui/web_chat/index.html`. JavaScript and CSS are bundled into that
file. Runtime rendering is offline; no Node.js service, CDN, remote font, or
frontend API key is required.

## Rebuild

Node.js and npm are only needed when modifying or rebuilding the frontend.

```powershell
cd frontend/chat-ui
npm ci
npm run build
```

The build type-checks TypeScript and replaces `desktop_pet/ui/web_chat/index.html`.
Keep the production file and `THIRD_PARTY_NOTICES.txt` in source control.

`npm run dev` runs Vite for frontend development. The real chat data and actions
require the Qt application and its QWebChannel bridge; opening the development
page alone does not create sample chats or a fake backend.

## Bridge

The page loads `qrc:///qtwebchannel/qwebchannel.js`. The registered `bridge`
provides `getState(callback)`, the `stateChanged` JSON signal, and
`dispatch(action, JSON.stringify(payload), callback)` with a JSON `{ok: boolean}`
result. Python remains responsible for requests, data, dialogs and exports.

State includes messages, title-only conversations, busy/loading/error information,
capability flags, per-panel drafts and `{id, serial}` message-focus requests.
Sending a message clears its draft only after Python accepts it. Draft edits are
sent to the host immediately; drafts survive a frontend reload without being
written to disk. Subsequent state updates do not overwrite local edits.

Provider and user text are rendered as safe Markdown. Raw HTML is skipped,
external images are omitted and links do not navigate the privileged page.
The content security policy and native resource interceptor block remote content.

## UI behavior

- Windows launch entries configure Chromium software rendering before Qt starts
  (`--disable-gpu`). This process-local compatibility path avoids GPU content
  rendering for the 2D chat; it does not change the OS graphics profile, Qt Quick
  backend, application preferences, or another application's environment.
  Set `AURA_WEBENGINE_RENDERER=auto` before launch to retain the original path.
- Web drafts stay in the host draft dictionary while the native editor is hidden.
  The native document is restored on switching to compatibility chat. Identical
  snapshots and unused visual-observer countdown ticks do not repaint chat;
  explicit ready/reload/open acknowledgements still force the required snapshot.
- The ice-white theme adds restrained whale-blue accents and stable glass
  highlights around the composer and conversation menu. Live editors use no
  backdrop filters or SVG maps, avoiding Qt compositing invalidations while
  typing. SVG refraction remains an explicit opt-in for non-editing surfaces.
  Reduced transparency/forced colors use a solid fallback.
- Draft changes keep the message converter and message context stable. A hidden
  measurement textarea handles growth/shrink and width changes without resetting
  the live editor's height on each keystroke.
- The original UI files and production bundle are preserved in
  `artifacts/ui_glass_20261031/baseline`. With the pet exited, run
  `artifacts/ui_glass_20261031/restore_ui.bat` to restore the baseline. The script
  first archives current files; it never resets the repository or user settings.
- The white chat surface keeps history titles, messages and the composer in focus.
  Routine storage/keyboard hints and permanent observation status bars are omitted.
  Each sidebar conversation menu contains find, export, rename, clear and delete.
  Find/export/clear load the selected conversation before acting; failed loads or
  navigation cancel the queued action. The top header has no duplicate tools or
  new-conversation button. New conversation stays in the sidebar; Ctrl+N remains available.
  Model, memory, observation, reading and context-budget controls live in settings.
  Chat errors and active generation remain visible in the chat surface.
- Enter sends, Shift+Enter inserts a line break; IME composition never sends.
- Ctrl+N opens a new conversation; Ctrl+F searches history; Ctrl+Shift+F finds
  messages in the current conversation; Escape dismisses the conversation menu.
- The sidebar becomes a dismissible overlay on narrow windows.
- New replies follow the latest message only when the reader is near the bottom.
- Synced image attachments appear automatically above the user's question as
  inline thumbnails. Clicking the image opens the existing native preview.
  The host validates the current message reference and generates a bounded PNG
  thumbnail in a worker; original files and local paths stay outside the web state.
- The composer plus button opens the native image picker. A conversation-scoped
  preview can be removed before sending; image-only sends use a default question.
  Picking an image does not call the model. Imports use the same bounded inbox
  and history image references as phone images, so they can sync to the phone.
- Screen context is collapsed until requested, and native focus requests expand it.
- Export and destructive confirmations retain the existing native behavior.
- The sidebar settings action opens the chat window's embedded native settings
  page instead of a debug window. API error recovery targets the API category.
  The web view stays mounted while settings are shown, preserving drafts and
  reading position. Credentials never enter the WebChannel snapshot.

Dependency licenses are included with the offline production asset in
`desktop_pet/ui/web_chat/THIRD_PARTY_NOTICES.txt`.
The adapted SVG refraction sources and upstream MIT notices are recorded in
`third_party/liquid_glass/` and included in that generated notice file.

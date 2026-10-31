# AURA chat frontend

The desktop chat panel uses the real `@assistant-ui/react` library, its
`ExternalStoreRuntime`, thread primitives and message primitives. AURA's local
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

- Enter sends, Shift+Enter inserts a line break; IME composition never sends.
- Ctrl+N opens a new conversation; Ctrl+F searches history; Escape dismisses tools.
- The sidebar becomes a dismissible overlay on narrow windows.
- New replies follow the latest message only when the reader is near the bottom.
- Screen context is collapsed until requested, and native focus requests expand it.
- Service settings, long-term memory, budget, export and destructive confirmations
  retain the existing native application behavior.

Dependency licenses are included with the offline production asset in
`desktop_pet/ui/web_chat/THIRD_PARTY_NOTICES.txt`.

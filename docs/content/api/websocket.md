---
title: "WebSocket Chat"
weight: 1
---

# WebSocket Chat Protocol

The primary chat interface uses a WebSocket connection at `/api/chat/ws`.

## Connection

```javascript
const ws = new WebSocket('ws://localhost:8000/api/chat/ws');
```

The WebSocket includes auto-reconnect logic with a 3-second delay.

## Message Format

All messages are JSON objects with a `type` or `action` field.

### Client to Server

#### New Session

```json
{
  "action": "new_session"
}
```

#### Resume Session

```json
{
  "action": "resume_session",
  "session_id": "abc-123"
}
```

Loads the last 20 messages as context.

#### Send Message

```json
{
  "action": "send",
  "text": "Hello, how are you?"
}
```

The model used is the one configured on the server (`COPILOT_MODEL`). There is no per-message model override.

#### Approve Tool (HITL)

Sent in response to a `tool_approval_request` event when the guardrails policy requires human-in-the-loop confirmation:

```json
{
  "action": "approve_tool",
  "call_id": "call-abc-123",
  "response": "yes"
}
```

`response` is `"yes"` or `"y"` to approve, anything else to reject.

### Server to Client

#### Session Created

Sent immediately after a `new_session` action:

```json
{
  "type": "session_created",
  "session_id": "abc-123"
}
```

#### Text Delta

Streamed token-by-token during response generation:

```json
{
  "type": "delta",
  "content": "Here"
}
```

#### Event

System events and status updates, including tool lifecycle and HITL notifications:

```json
{
  "type": "event",
  "event": "tool_start",
  "tool": "web_search",
  "call_id": "call-abc-123",
  "arguments": "{\"query\": \"latest news\"}"
}
```

```json
{
  "type": "event",
  "event": "tool_done",
  "call_id": "call-abc-123",
  "result": "..."
}
```

```json
{
  "type": "event",
  "event": "tool_approval_request",
  "call_id": "call-abc-123",
  "tool": "delete_file",
  "arguments": "{\"path\": \"/tmp/data.csv\"}"
}
```

```json
{
  "type": "event",
  "event": "approval_resolved",
  "call_id": "call-abc-123",
  "approved": true
}
```

#### Sandbox Result

Emitted after a sandboxed code execution completes:

```json
{
  "type": "sandbox_result",
  "stdout": "Hello\n",
  "stderr": "",
  "exit_code": 0
}
```

#### Media

Sent when the agent produces file attachments (images, audio, documents):

```json
{
  "type": "media",
  "files": [
    { "url": "/api/media/output_abc.png", "mime": "image/png", "name": "output_abc.png" }
  ]
}
```

#### Cards

Rich attachment cards produced by the agent (e.g. from Bot Framework adaptive cards):

```json
{
  "type": "cards",
  "cards": [ { "contentType": "application/vnd.microsoft.card.adaptive", "content": {} } ]
}
```

#### Command Response

Response to slash commands:

```json
{
  "type": "message",
  "content": "Available models: ..."
}
```

#### Error

```json
{
  "type": "error",
  "content": "Session not found"
}
```

#### Done

Marks the end of a response:

```json
{
  "type": "done"
}
```

## Slash Commands via WebSocket

Slash commands are detected by the `CommandDispatcher` and handled server-side. Send them as regular messages:

```json
{
  "action": "send",
  "text": "/status"
}
```

## Auxiliary HTTP Endpoints

### Suggestions

```
GET /api/chat/suggestions
```

Returns an array of suggested conversation starters.

### Models

```
GET /api/models
GET /api/chat/models
```

Returns available LLM models and the currently active model.

## Session Lifecycle

1. Client connects to WebSocket
2. Client sends `new_session` or `resume_session`
3. Server responds with `type: session_created`
4. Client sends messages, server streams deltas
5. On disconnect, the session is preserved for resume

# Architecture

```mermaid
sequenceDiagram
  participant U as Browser
  participant W as WebSocket
  participant S as Store
  participant A as Agent runtime
  participant P as Groq/Whisper/Edge
  U->>W: text or whole PCM utterance
  W->>S: create tenant-scoped call
  W->>P: transcribe PCM (audio only)
  W->>A: user turn
  A->>S: tenant-scoped retrieval
  A->>P: generate reply
  A->>S: persist state and event
  W-->>U: reply + actual turn time
  W->>P: synthesize audio
  W-->>U: MP3 audio
  U->>W: next turn / interrupt
  W->>W: cancel prior task
  W-->>U: cancel playback
```

The FastAPI process owns WebSocket sessions. `Engine` handles conservative escalation, tenant-scoped lexical evidence, provider calls, and structured call state. `Store` encapsulates SQLite operations and unique constraints. Each HTTP read/write derives tenant identity from a hashed API key; each WebSocket call is bound to one tenant. Twilio maps the called number to tenant configuration, verifies HMAC signatures, and uses a call ID plus turn number for repeat handling.

No LangGraph was added: the current graph is a small linear state machine with one conditional escalation branch. A framework would add complexity without solving the missing streaming and persistence work. The provider classes are boundaries; only Groq LLM, faster-whisper, Edge TTS, HubSpot, and Google Calendar are implemented, with availability determined by configuration. The old clinic-specific implementation remains archived for audit.

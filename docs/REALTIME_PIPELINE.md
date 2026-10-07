# Voice pipeline and latency

The browser captures signed 16-bit PCM while the button is held. On release, a single WebSocket message carries the entire utterance. The server transcribes with faster-whisper, retrieves tenant text, calls Groq, persists a reply, then synthesizes MP3 with Edge TTS. A new turn cancels the current generation task and instructs the browser to stop playback. A generation counter prevents a stale task from sending audio or committing state after cancellation.

Measured today: `turn_completed.response_ms` covers retrieval plus LLM and state update. `/api/analytics` calculates p50/p95 only from stored samples, returning `null` with zero samples. `tts_completed.duration_ms` is recorded separately. `speech_end_to_STT_ms`, `STT_to_first_LLM_token_ms`, and `LLM_to_TTS_start_ms` are not available because STT/LLM/TTS are not streamed. Browser network and playback time are not included in `end_to_end_response_ms`; this field is currently server turn processing time and should be renamed when full instrumentation is added.

There is no server-side VAD. Push-to-talk release is the endpoint signal. Interruption is explicit by new turn/button, not acoustic echo cancellation. The phone integration uses Twilio Gather speech turns and Twilio Say, so audio cannot be interrupted by local TTS cancellation. These are deployment blockers for a low-latency real-time claim.

# AGENTS.md — Working instructions for AI assistants

This file is a handover note for any LLM-driven assistant (Claude, etc.) that is asked to modify, debug, or extend this service. Read it before touching code or infrastructure.

---

## What this service is

A self-hosted LiveKit voice agent stack deployed on Railway. The voice "brain" (LLM + STT + TTS) runs entirely through **Eden AI v3** — the user has one Eden AI key that fans out to any provider Eden AI proxies (OpenAI, Deepgram, ElevenLabs, etc.) by changing a model string.

End use case: an interactive voice agent that reads a report's findings to a user in a meeting and takes follow-up questions. A separate downstream process (NOT in this repo) consumes the resulting transcript and extracts action items.

---

## Live deployment

Project: **`paperclip`** on Railway.
Project ID: `cc1a9d88-d64b-4599-a642-f9553493e014`
Production environment ID: `a356bdc7-bce4-4951-b317-5a5e0d06b4ef`

| Service | Service ID | Notes |
|---|---|---|
| `redis` | `313d6db4-c93a-4178-b92d-9db9d7f5be79` | Dedicated; do not reuse the other Redis services in this project (they have volumes and belong to other apps) |
| `livekit-server` | `75d670a5-2359-4c9e-9c79-6a99c8f87789` | Public domain + **TCP proxy on app port 7882** (required for WebRTC) |
| `voice-agent` | `2cc775bf-d385-42e5-9c66-2f4e5f73e612` | Background worker, no public domain |
| `web-frontend` | `1a602fb6-de00-4355-910b-c92cadf6e161` | FastAPI server that mints LiveKit tokens |

Public URLs:
- web-frontend: `https://web-frontend-production-509b.up.railway.app`
- livekit-server (signaling): `https://livekit-server-production-d281.up.railway.app`
- livekit-server (TCP/ICE): `yamabiko.proxy.rlwy.net:33866 → :7882`

Source repo (the user's fork): `https://github.com/captain-cron/livekit-voice-agent-edenai`, branch `main`.

---

## Architecture

```
Browser ⇄ web-frontend           (HTTPS, generates LiveKit token)
   │
   └── WSS signaling ──→ livekit-server
   └── TCP ICE media ──→ livekit-server (via Railway TCP proxy on :33866)
                              │
                              ├── Redis (room/participant state)
                              │
                              └── voice-agent (worker, registers itself,
                                  receives audio, runs STT→LLM→TTS through Eden AI)
```

---

## Code layout

```
voice-agent/
  agent.py              # entrypoint + session config. THIS IS WHERE BUSINESS LOGIC GOES.
  edenai_plugin.py      # custom EdenAISTT + EdenAITTS using Eden AI v3
  pyproject.toml
  Dockerfile
livekit-server/
  entrypoint.sh         # reads RAILWAY_TCP_PROXY_* env vars at startup, writes livekit.yaml
web-frontend/
  main.py + app/        # FastAPI + static UI. Page header text is hardcoded ("OpenAI STT → ...") and currently misleading — the actual pipeline is Eden AI. Cosmetic; safe to update.
```

---

## Eden AI v3 integration (critical details)

The Eden AI API was migrated from v2 to v3 in this project. **Do not regress to v2 paths.**

### LLM
- Endpoint: `POST https://api.edenai.run/v3/chat/completions`
- OpenAI-compatible body. Used via `livekit.plugins.openai.LLM(model=..., base_url="https://api.edenai.run/v3", api_key=EDENAI_API_KEY)`.
- Model format: `provider/model`, e.g. `openai/gpt-5.4-mini`. The v2 `/v2/llm/chat/completions` endpoint does NOT know about `gpt-5.4-mini` — it will return 404 with `"Model 'openai/gpt-5.4-mini' in llm/chat does not exist"`. If you see that error, the request is hitting v2.

### STT (two-step async flow)
1. `POST /v3/upload` — multipart with the WAV bytes. Response: `{"file_id": "<uuid>"}`.
2. `POST /v3/universal-ai/async` — JSON body:
   ```json
   {
     "model": "audio/speech_to_text_async/deepgram/nova-3",
     "input": {"file": "<uuid>", "language": "en-US"}
   }
   ```
   Response often contains `output.text` immediately (small clips finish synchronously). If `status` is not `success`, poll `GET /v3/universal-ai/async/{public_id}`.

The `file` field rejects raw URLs to anonymous WAVs with `"Invalid URL"` — always use the upload-first flow.

### TTS (synchronous)
- `POST /v3/universal-ai/` with `{"model": "audio/tts/elevenlabs/eleven_flash_v2_5", "input": {"text": "..."}}`.
- Response: `{"status": "success", "output": {"audio_resource_url": "https://..."}}` (CloudFront-signed mp3 URL).
- The plugin fetches that URL and hands the mp3 bytes to LiveKit's `AudioEmitter` with `mime_type="audio/mp3"`; LiveKit decodes to PCM internally. **Do not** try to push raw PCM unless you also resample.

### Env vars (live values in Railway)
- `EDENAI_API_KEY` — bearer token.
- `EDENAI_MODEL` — LLM, e.g. `openai/gpt-5.4-mini`.
- `EDENAI_STT_PROVIDER` — full model path, passed verbatim as the `model` field.
- `EDENAI_TTS_PROVIDER` — same.

---

## Turn-taking config (current)

In `voice-agent/agent.py` → `_build_session()`:
- `allow_interruptions=False` — agent finishes its sentence before listening; user cannot barge in.
- `min_endpointing_delay=1.2` — STT waits 1.2 s of silence before finalizing.
- 0.5 s `asyncio.sleep()` before the opening greeting, so the user's initial connection audio can't race the first turn.

These are tuned for **structured Q&A** (the meeting/report use case). For free-form chitchat, you would lower `min_endpointing_delay` and set `allow_interruptions=True`.

---

## How to add a new "session type" (e.g. meeting review)

`entrypoint()` already inspects the room name to pick a mode (currently just pipeline vs realtime, both behaving identically). The pattern for adding a real domain-specific flow:

```python
@server.rtc_session()
async def entrypoint(ctx: JobContext):
    room_name = getattr(ctx.room, "name", "")

    if room_name.startswith("meeting-"):
        report_id = parse_report_id(room_name)
        report = await fetch_report(report_id)        # <-- new module: reports.py
        agent = MeetingReviewAgent(report)
        opening = "Greet briefly, then read findings 1..N in order, then ask for questions."
    else:
        agent = VoiceAssistant()
        opening = "Greet the user and offer your assistance."

    session = _build_session(ctx)
    await session.start(agent=agent, room=ctx.room)
    await asyncio.sleep(0.5)
    await session.generate_reply(instructions=opening)
```

`MeetingReviewAgent` is a subclass of `livekit.agents.Agent` whose `__init__` stuffs the report into the `instructions` string. Tools (functions the agent can call mid-turn, e.g. `lookup_quote(finding_number)`) go on that class with `@function_tool`.

The web-frontend would need a corresponding route or query-string handler that creates rooms named `meeting-{report_id}-...`. The frontend currently only emits `pipeline-...` and `realtime-...` prefixes.

---

## Deploying changes (important gotcha)

**Pushing to GitHub does NOT auto-deploy.** `deploymentTriggerCreate` failed during initial setup (Bad Access — the project token can't link a GitHub source). So each Railway service is wired to the repo at creation time but has no webhook for new pushes.

To deploy a change:
1. Commit and push to `main` on the fork.
2. Trigger a deploy explicitly with `serviceInstanceDeployV2`, passing the latest commit SHA:
   ```
   gh api repos/captain-cron/livekit-voice-agent-edenai/branches/main --jq .commit.sha
   ```
   then POST to Railway GraphQL with `commitSha` set to that value.
3. Watch the deploy status (`deployment(id: ...) { status meta }`) and check logs (`deploymentLogs(deploymentId: ...)`).

The Railway CLI's `add` and `link` commands do **not** work with the project token in this environment — use the GraphQL API directly with header `Project-Access-Token: <token>`.

---

## Manual infrastructure steps (cannot be automated)

If `livekit-server` is ever recreated, the TCP proxy must be added by hand:
1. Railway UI → paperclip → livekit-server → Settings → Networking → "Add TCP Proxy"
2. Application port: **`7882`**
3. After it provisions, redeploy `livekit-server` so the entrypoint picks up `RAILWAY_TCP_PROXY_*` env vars.

Symptom of a missing TCP proxy: page loads, "Connected" appears, "Agent is speaking" appears, but **no audio** is ever heard and the browser console logs `"could not establish pc connection"`. Signaling works over the HTTP proxy; media does not.

---

## Common pitfalls to avoid

- **Don't switch the LLM back to `/v2/llm/...`.** It will 404 on current models.
- **Don't try to send a raw WAV URL to `/v3/universal-ai/async` for STT.** It will reject with "Invalid URL" or "Field 'file' must be a valid UUID or URL". Upload first → use the UUID.
- **Don't reuse the existing `Redis-dfrO` or `Redis` services** in the paperclip project. They have volumes and belong to other apps. The lowercase `redis` service is ours.
- **Don't add a volume to any of the four services.** Everything in this stack is stateless or session-only:
  - `redis`: live room coordination, OK to lose on restart.
  - `livekit-server`: config regenerated from env vars on startup.
  - `voice-agent`: VAD model baked into the image at build time.
  - `web-frontend`: pure token mint, no state.
- **Don't regenerate `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` on `livekit-server`** without redeploying `voice-agent` and `web-frontend` — they reference those vars via `${{livekit-server.LIVEKIT_API_KEY}}`.
- **Don't enable streaming on Eden AI STT.** Eden AI v3 STT is launch+poll only. The plugin already handles this. Pipeline latency is ~1–2 s per turn end-to-end, which is the floor with this provider.
- **Don't try to use the `/realtime` route as speech-to-speech.** Eden AI has no realtime API; the route currently runs the same pipeline as `/pipeline`. Treat them as one mode.

---

## Quick reference: testing the agent

1. Open `https://web-frontend-production-509b.up.railway.app/pipeline`
2. Click Connect
3. Wait ~0.5 s for the greeting to start
4. Speak, wait ~1.2 s of silence, the agent replies
5. Errors / activity show up in `voice-agent` Railway logs

Things to grep for in voice-agent logs when something breaks:
- `Starting pipeline session for room` — entrypoint fired
- `failed to generate LLM completion` — LLM call failed (full error in the traceback body)
- `failed to recognize speech` — STT failed (often Eden AI upload or poll error)
- `closing agent session due to participant disconnect` — user hung up (or VAD timed out)

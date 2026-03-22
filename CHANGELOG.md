# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] — 2026-03-22

### Added
- **WebGL Aura Visualizer** — Replaced custom Canvas 2D aura with the official LiveKit agents-ui WebGL shader-based visualizer (turbulence, bloom, color-shifting effects)
- **Dynamic mode switching** — Users switch between Pipeline and Realtime modes via the web UI; the agent detects the mode from the room-name prefix (`pipeline-*` / `realtime-*`) with no restart needed
- **Cyan-themed UI** — Connect button, mode nav, and status indicators restyle to match the aura's `#00e5ff` cyan palette with translucent glow effects
- **Railway deploy skill** — `.claude/skills/railway-deploy/` with full deployment lifecycle automation and a verification script

### Changed
- **Responsive layout overhaul** — Header, aura, transcript console, and controls now share vertical space properly; aura shrinks when transcript appears instead of overflowing
- **StatusBadge** — Changed from pill-button style to a minimal dot + text indicator so it doesn't compete visually with the mode nav tabs
- **TranscriptConsole** — Tighter margins and reduced max-height on mobile for better fit
- **ConnectButton** — Full-width on mobile (capped at 280px), glassy border style instead of solid fill

### Removed
- **`AGENT_MODE` env var** — No longer needed; mode is fully driven by room-name prefix from the web frontend. Removed from `agent.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, and `README.md`
- **Custom Canvas 2D rendering** — Replaced by the official LiveKit WebGL shader component

### Fixed
- **esbuild postfix `++` on cast** — Fixed `(value as number)++` syntax in vendor `react-shader-toy.tsx` that esbuild rejected

---

## [1.0.0] — 2026-03-19

### Added
- Initial release: LiveKit server, Python voice agent (pipeline + realtime), Redis, and web frontend
- Railway one-click deploy template with TCP proxy for WebRTC ICE
- React SPA with audio visualization, transcript console, and connection management
- Pipeline mode: OpenAI Whisper STT -> GPT-4o-mini -> TTS-1
- Realtime mode: OpenAI Realtime API (speech-to-speech)
- Debug monitor overlay for dev builds showing mic/agent RMS levels
- Mobile-responsive design with safe-area support

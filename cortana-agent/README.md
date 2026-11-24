# cortana-agent

Cortana: the family's voice assistant on the Pi Zero satellite in the downstairs hall. It is
Vector's brain (`../vector-agent`) without the robot: the same LangGraph **Functional API**
ReAct loop, Claude (Haiku 5.5, effort low), skills, memory and safety guard. It runs as a
container on the Pi 5, next to Vector.

```
Pi Zero (192.168.1.96)                                  Pi 5: cortana-agent
  mic -> openWakeWord "okay_nabu"                         satellite.py: Wyoming client
      -> wyoming-satellite :10700  <==== Wyoming TCP ====>  VAD (webrtcvad) -> end of speech
  BT speaker <- aplay (bluealsa)                          voice.py: whisper.cpp :10330 (vector-whisper)
                                                          agent.py: ReAct loop (Claude)
                                                          voice.py: Piper :10200, sentence by sentence
```

The satellite is a Wyoming *server*: cortana-agent connects to it and runs the voice
pipeline, which Home Assistant's Wyoming integration used to do. Only one server can hold
the satellite, so HA's "Cortana Downstairs" Wyoming entry must be deleted.

* `app/satellite.py`: the conversation with wyoming-satellite (run-satellite, run-pipeline,
  audio, transcript, played, pings, reconnects). After a reply that ends with a question, it
  sends `detection`, and the patched satellite (`deploy/pizero/`) listens again without the
  wake word (`FOLLOW_UP_MODE`).
* `app/voice.py`: end-of-speech detection, Whisper and Piper clients. Her voice is `PIPER_VOICE` (now `en_US-hfc_female-medium`; Piper downloads it on first use) and `VOICE_FX=shimmer` adds a light digital chorus + echo (the `Shimmer` class; empty = plain).
* `app/conversation.py`: one turn. Text streams from the agent and each finished sentence is
  spoken (after the speech guard) while the rest is still being written.
* The brain is shared with Vector: `../agent-core` (ReAct loop, LLM setup, skills,
  conversation folding, notes, belief store, A2A). `app/llm.py`, `app/skills.py`,
  `app/activity.py`, `app/mcp_tools.py`, `app/beliefs.py` and `app/sleepbatch.py` are one-line
  links to it; `app/agent.py` passes her tools, prompt and guard to the shared loop.
* Memory: what she jots down while talking is in `/data/memories.json` (today's notes). With
  `BELIEFS_URL` and `BELIEFS_TOKEN` set, her nightly run (`NIGHTLY_AT`) files the notes as
  beliefs in her own tenant of the belief store on the NUC, and she gets the tools `recall`
  and `belief_history`. She cannot see who speaks, so a fact is filed under a person only
  when the speaker said who they are.
* A2A: with `A2A_TOKEN` set she has a door for other agents on `A2A_PORT` (8096). With
  `PEER_NAME`, `PEER_A2A_URL` and `PEER_A2A_TOKEN` set she can ask Vector (`ask_vector`); the
  tool is not offered on a turn that itself came over A2A.
* Nightly run: file notes as beliefs, fold the day's conversation, prune checkpoints.
  `POST /nightly` on the text door runs it once now.
* `app/reminders.py`: timers and reminders, stored in `/data/reminders.json`. When one goes
  off, the satellite's timer chime rings and she says it.
* `app/music.py`: the NAS music library (`music` skill). MPD on the Zero plays the files
  as they are on the Bluetooth speaker; we queue over its protocol (:6600, `MPD_HOST`) with
  Vector's matching (song / artist / album / shuffle). The speaker takes one stream at a
  time: the satellite's `cortana-play` pauses MPD for chimes and replies, and `Player`
  resumes it once she's quiet (`satellite.idle()`); a pause nobody talked through (the
  queue ended, someone pressed pause) is left alone.
* `app/memory.py`: facts behind a `MemoryStore` seam. `MEMORY_BACKEND=json` today;
  `kumiho` is a stub for shared memory with Vector later. Every fact records `by`.
* `app/server.py`: the process. `GET /health`, `GET /status`, `POST /chat {"text","speak"}`
  and `POST /announce {"text"}`, on 127.0.0.1:8092 only.

Tools: web search (Tavily MCP), Home Assistant (`home` skill: calendar, shopping list,
lights, announcements), music from the NAS (`music` skill), memory, timers and reminders, learned skills, `recent_activity` and
`search_logs`.

## Deploy

`deploy/pi/deploy.sh` syncs this folder to `~/wire-pod/cortana-agent` and builds it on the
Pi. Secrets are in `~/wire-pod/cortana.env` (see `deploy/pi/cortana.env.example`) and state
is in `~/wire-pod/cortana-data`. Zero side: `deploy/pizero/setup.sh [SPEAKER_MAC]`, then
`deploy/pizero/music.sh` for MPD.

```sh
curl -s localhost:8092/chat -d '{"text":"set a two minute tea timer"}'   # on the Pi
docker exec cortana-agent python -m pytest -q tests
```

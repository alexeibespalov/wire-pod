# vector-agent

Vector's conversational brain: a LangGraph **Functional API** ReAct agent that sits behind
wire-pod's "Custom" knowledge provider (OpenAI-compatible `POST /v1/chat/completions`, SSE).

```
wire-pod (wake word -> VOSK STT -> intent match) --unmatched--> vector-agent :8091
  @entrypoint(checkpointer=AsyncSqliteSaver)   # one thread per conversation (10 min idle = new)
    loop: @task call_model -> @task call_tool x N -> ... -> final spoken text
  <-- streams text + {{playAnimationWI||...}} / {{newVoiceRequest||now}} cues back to wire-pod
```

* `app/prompt.py`: persona + rules (rebuilt per turn with time and memories)
* `app/tools.py`: robot tools via wire-pod's local `/api-sdk/*` (speed/time-capped motion,
  built-in behaviours via `cloud_intent`, eye colour, battery) + memory tools
* `app/agent.py`: the ReAct loop
* `app/server.py`: OpenAI-compatible SSE front door; ignores wire-pod's system prompt/history
* `app/navmap.py` / `app/explore.py`: map understanding. Summarises the robot's nav map (listen-only SDK feed via `faces.register_subscriber`) and reads exploration photo descriptions from `AGENT_DATA_DIR/explore/` (read-only); tools `where_am_i`, `exploration_findings`, `show_map` + the `map` skill
* Model: any OpenAI-compatible endpoint (`UPSTREAM_BASE`/`UPSTREAM_KEY`/`FORCE_MODEL`);
  `LLM_EXTRA_BODY` defaults to `{"enable_thinking": false}` for Qwen.

Deploy with `deploy/pi/deploy.sh` (builds the image on the Pi).

## Exploration photos and the sleep routine

* `app/explore_capture.py`: listen-only hook (via `faces.register_subscriber`) on the SDK
  connection. While he explores on his own (off the charger, moving, not calm/held) it takes a
  camera photo (from wire-pod's `/cam-stream`, no behaviour control) on a rising edge of the
  proximity sensor (`found_object`, < `EXPLORE_PROX_MM`, signal quality gate) or when he sees
  his charger / cube / a custom object (`robot_observed_object`). Faces never trigger.
  Debounced (per-trigger/object cooldowns, min interval, hourly cap) and capped on disk.
* `app/sleep_routine.py`: job framework for chores while he sleeps (`@job(name, order,
  budget_s, has_work)`; cancelled the moment he wakes; resumable). Asleep = WireOS calm power
  mode (requested by the Sleeping behaviour) for `SLEEP_CALM_SECONDS` + no conversation for
  `SLEEP_QUIET_MINUTES`; fallback = on charger, idle, night hours. Jobs never take control,
  animate or play audio. First job: caption/objects/setting for each new photo via `VISION_MODEL`.
* Storage contract under `$AGENT_DATA_DIR/explore` (`/data/explore`):
  `photos/<YYYYmmdd-HHMMSS>-<trigger>-<id8>.jpg`; `captures.jsonl` (append-only, written by the
  capture module): `{"id","ts","trigger":"prox"|"object","object":str|null,"prox_mm":int|null,
  "pose":{"x_mm","y_mm","angle_deg","origin_id"},"file"}`; `analyses.jsonl` (append-only, written
  by the sleep job): `{"id","analyzed_at","model","caption","objects":[],"setting"}`.
  Poses are comparable only within one `origin_id` (resets on reboot/pickup). Old photos are
  deleted past `EXPLORE_MAX_PHOTOS`, but their `captures.jsonl` lines stay, so check the file exists.
* Tools: `recent_exploration_finds`, `set_exploration_capture`, `sleep_routine_status`,
  `run_sleep_routine_now`; skill `exploration`. Config vars: see `deploy/pi/.env.example`.

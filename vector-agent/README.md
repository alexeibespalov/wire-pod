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
* Model: any OpenAI-compatible endpoint (`UPSTREAM_BASE`/`UPSTREAM_KEY`/`FORCE_MODEL`);
  `LLM_EXTRA_BODY` defaults to `{"enable_thinking": false}` for Qwen.

Deploy with `deploy/pi/deploy.sh` (builds the image on the Pi).

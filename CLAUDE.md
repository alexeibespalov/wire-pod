# wire-pod: the home of Vector and Cortana

This repository holds all the code for the two assistants in the house and is where they are
released from. It is one of three working folders on this laptop, each with its own job. Knowing
which folder owns what keeps a change from being made in the wrong place.

## Who owns what

| Working folder | Owns | Releases to |
|---|---|---|
| `~/Development/vector` (this repo is `wire-pod/` inside it) | All Vector and Cortana code and their deployment files | The Raspberry Pi 5 (both agents) and the Pi Zero (Cortana's satellite) |
| `~/Development/nucagentcontrol` | Managing the NUC, plus the belief store code: `beliefs-api/` and `belief-management/` | The NUC |
| `~/Development/rpiagentcontrol` | Routine management of the Pi devices only: updates, logs, restarts, Home Assistant upkeep | Nothing. It holds no agent code and deploys nothing |

## What is in here

- `agent-core/`: the brain both assistants share (ReAct loop, LLM setup, skills, notes, belief store
  client, reconcile job, A2A). Read `agent-core/README.md` first.
- `vector-agent/`: Vector's persona, tools, safety guard, robot modules and sleep jobs.
- `cortana-agent/`: Cortana's persona, tools, voice pipeline, timers and music.
- `deploy/pi/`: what runs on the Pi 5. `deploy.sh` syncs the three source folders and rebuilds both
  containers there.
- `deploy/pizero/`: Cortana's satellite on the Pi Zero. `setup.sh` applies it.
- `chipper/` and the rest: the wire-pod server itself (a fork of `kercre123/wire-pod`).

## How a change reaches a device

Edit here, run the tests, commit, then deploy with the script. The copies on the devices are outputs:
`deploy.sh` mirrors the agent folders with `--delete`, so anything edited by hand on the Pi is
overwritten by the next deploy and never reaches this repository. For the same reason, fix a problem
found on a device here and redeploy, even when a one-line edit over SSH would be quicker.

```sh
cd agent-core && python -m pytest tests -q
cd vector-agent && python -m pytest tests -q
cd cortana-agent && python -m pytest tests -q
./deploy/pi/deploy.sh --dry-run     # what would change on the Pi 5
./deploy/pi/deploy.sh               # sync, rebuild, restart
```

A deploy restarts both assistants for a few seconds, so say so before running one.

Secrets and per-device settings are not in this repository. They live on the Pi in `.env` (Vector's
brain profile), `vector.common.env` (Vector's settings that must survive a profile switch) and
`cortana.env`. The examples in `deploy/pi/` list every setting. Changing those files is part of a
release from here, not routine device management.

## When the work belongs to another folder

- **The belief store** (the API on the NUC, the belief library, tenants and tokens) is owned by
  `nucagentcontrol`. The client lives here in `agent-core/beliefs.py` and `reconcile.py`. A change to
  the API needs both sides, so describe what the other side must change and ask John to do that part in
  a session started from `~/Development/nucagentcontrol`.
- **The Pi devices themselves** (OS updates, Home Assistant, Docker upkeep, Bluetooth, the network) are
  managed from `~/Development/rpiagentcontrol`. If a change here needs something from the device, such
  as a package or a Home Assistant setting, say what is needed and hand it over the same way.

Handing over means stopping at the boundary, not working around it: tell John which folder the next
step belongs to and what to ask for there.

## Committing and pushing

Commit work here so the devices never run code that exists only in a working tree. This is a public
fork (`alexeibespalov/wire-pod`), so ask before pushing, and keep tokens, family details and anything
copied from the agents' data out of commits.

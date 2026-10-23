# One User, One Agent

A per-viewer lifecycle agent for the OTT dialect platform. For every app open, the viewer's agent:

1. assembles what is known about the viewer, with the source, time and freshness of each fact, and resolves conflicts explicitly;
2. commits to one measurable goal, or records why it can't;
3. decides what to change (a home page now, perhaps one conditional message later) or decides to do nothing;
4. publishes within the latency and call budgets, and falls back when a dependency fails;
5. checks every action independently before publishing it;
6. judges each goal from attributable signals and writes the lesson back to the viewer's state;
7. measures impact against the randomized holdout.

Backend: Python 3.11+, FastAPI, SQLite. The default model provider is a deterministic mock, so no API key is needed.

## Setup

```bash
cd submission
python -m pip install -r requirements.txt      # fastapi, uvicorn, pytest, httpx
```

The data pack is expected next to this folder at `../one_user_one_agent_pack`. Otherwise pass `--pack PATH` or set `OUA_PACK`.

## Run with Docker (easiest)

Needs Docker Desktop. From the `submission/` folder:

```bash
docker compose build                     # build the image once
docker compose up api                    # API on http://localhost:8000/docs  (Ctrl+C to stop)
docker compose run --rm replay           # full replay -> simulate -> judge -> reports into ./sample_run
docker compose run --rm tests            # test suite
```

The data pack is not copied into the image. It is mounted read-only from `../one_user_one_agent_pack`. If yours lives elsewhere, set `OUA_PACK_DIR` (for example `OUA_PACK_DIR=/path/to/pack docker compose run --rm replay`; in PowerShell, `$env:OUA_PACK_DIR="C:\path\to\pack"` first). The API keeps its state in the `api_state` Docker volume between restarts.

Without Compose:

```bash
docker build -t oua-agent .
docker run --rm -p 8000:8000 -v "$(pwd)/../one_user_one_agent_pack:/data/pack:ro" oua-agent
docker run --rm -v "$(pwd)/../one_user_one_agent_pack:/data/pack:ro" -v "$(pwd)/sample_run:/app/sample_run" oua-agent all --out /app/sample_run
```

## One command: replay, simulate, judge, report

```bash
cd src
python -m oua all --out ../sample_run
```

This replays `replay/triggers.jsonl` together with the live and control events (about 15 s). It then runs the pack's outcome simulator on the published actions, judges every goal and writes the impact and final reports. On a different replay day or seed:

```bash
python -m oua all --out ../run2 --replay-dir /path/to/other/replay --seed their-seed
```

Individual steps: `python -m oua replay|simulate|judge --out ../sample_run`.

Inspect one viewer: what the agent knew, decided and observed up to a moment:

```bash
python -m oua explain --out ../sample_run --viewer U-04945 --at 2026-10-01T12:00:00Z
```

## Dashboard (see it working in the browser)

```bash
cd src
python -m oua serve --reset              # then open http://127.0.0.1:8000/
```

On the dashboard you can:

- **Load demo data**: loads 60 real app opens from the replay day plus 13 scenarios, each showing one behaviour (duplicate, offline-buffered open, three dialects, holdout, purchase right after a decision, false model claim, model outage). It also sends the follow-up messages that are due.
- **Browse decisions**: filter by Published, No action, Holdout or Blocked. Click one to see why it was made: goal and success signal, a confidence bar with its reason, dialect conflicts and how they were resolved, the independent checks passed and failed, model use, the published page with real title names, the plan, and every fact with its source and age.
- **Send an app open** for any viewer, dialect or campaign link, optionally buffered offline, and see the decision immediately.
- **Look up a viewer**: active goal, plan, goal history, lessons and a decision timeline.

Every decision has its own link (`http://127.0.0.1:8000/#D-00064`).

## API

```bash
cd src
python -m oua serve                      # http://127.0.0.1:8000/docs
```

| Endpoint | Purpose |
|---|---|
| `POST /v1/triggers` | app_open / login_success → decision (idempotent on `event_id`) |
| `POST /v1/events` | other live events → memory update, plan revision |
| `POST /v1/control` | `title_withdrawn`, `goal_rule_corrected`, `dependency_unavailable`, `dependency_degraded` |
| `POST /v1/plan-steps/run?now=` | run due conditional nudges |
| `GET /v1/viewers/{id}` | durable state: active goal, plan, history, lessons |
| `GET /v1/viewers/{id}/explain?at=` | what the agent knew / decided / observed up to `at` |
| `GET /v1/decisions/{id}` | one decision record |

## Tests

```bash
cd submission
python -m pytest -q          # 23 tests, ~60 s (includes a full replay with invariant checks)
```

They cover:

- **Late and duplicate triggers:** duplicates, offline-buffered events, wrong phone clocks, and a login plus app open in the same session.
- **Conflicting sources:** three different dialects, an unsupported dialect, duplicate profile rows, and an opt-out event overriding the profile.
- **Tool failures:** model timeout, malformed output, provider outage, and the data store down with and without cached facts.
- **Corrected goal rule:** the G03 correction re-points only open G03 goals and withdraws a success that no longer qualifies.
- **Safety:** holdout, injected catalogue text, consent, quiet hours, the frequency cap and copy rules; a purchase right after a decision; a withdrawn title.
- **End to end:** a full replay checked against invariants for every published action.

## Real model (optional)

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...            # or: ant auth login
python -m oua all --provider anthropic --out ../run_live
```

The model is used only to review a goal choice and to draft push copy. It receives structured facts (no catalogue descriptions, search queries or deeplinks). Its output only adjusts confidence or is checked before use. `model_faults.jsonl` is reproduced only in mock mode.

**Privacy:** the pack is treated as personal data. Mock mode sends nothing anywhere. Use the real provider only under an API agreement that does not retain or train on submitted content.

## Repository map

```
src/oua/
  facts.py      step 1: facts with provenance, precedence and conflict resolution
  goals.py      goal catalogue: eligibility from evidence, versioned success rules, measurability
  planner.py    step 2: goal ranking (model may review, never decide)
  content.py    step 3: goal-aligned home pages and message titles, valid content only
  verifier.py   step 5: independent checks (holdout, dialect, availability, consent, quiet hours, cap, copy, templates,
                duplicates, injected text, eligibility)
  agent.py      one agent per viewer: triggers, live events, plan steps, control events
  llm.py        provider abstraction: mock (default, reproduces model_faults) and Anthropic
  runtime.py    per-decision clock, call budgets, dependency fault windows, data-store gateway
  events.py     event normalisation (dedupe, true time from sent-occurred), history store
  store.py      durable state in SQLite: viewer docs, ledger, decisions, actions, goals, audit
  replay.py     replay harness with simulated workers and latency report
  judge.py      step 6: verdicts + attribution, lessons fed back to viewer state
  impact.py     step 7: ITT vs holdout, misleading comparisons, pilot re-analysis
  api.py, cli.py
tests/
sample_run/     decisions.jsonl, actions.jsonl, outcomes.jsonl, viewer_state/, latency_report.json,
                impact_report.md, final_report.md, audit.jsonl, sim/ (simulator output)
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) and [AI_COLLABORATION.md](AI_COLLABORATION.md).

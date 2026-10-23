# Architecture

```
 trigger / live event / control event / plan-step timer
        │
        ▼
 AgentSystem (router, per-viewer lock) ──► ViewerAgent(viewer)  ◄── the only writer of that viewer's state
        │                                     │
        │   1 understand  facts.build_facts  ◄── StoreGateway (user data store: budget, timeout, fault windows)
        │   2 goal        planner.choose_goal ── ModelGateway.goal_review (optional, confidence only)
        │   3 decide      content.build_home_page / pick_message_title
        │   5 verify      Verifier (independent: re-derives from pack + facts + action log)
        │   4 act         publish within budget, or fall back to a smaller action / no action
        │   6 close loop  live events → provisional success, plan revision; judge.py after the horizon
        ▼
 StateStore (SQLite): viewer_state · trigger_ledger · decisions · actions · goals · audit
```

## Ownership

- Every viewer has exactly one `ViewerAgent`. It is created per call from that viewer's durable state document, under a per-viewer lock held by `AgentSystem`. Triggers, live events, plan steps and control-event fan-out for one viewer all pass through that lock.
- The state document holds a single `active_goal` and a single `plan`. No other component writes goals or plans. Control events (a rule correction, a title withdrawal) are fanned out by the system to the *owning* agents, which apply them.
- State writes use an optimistic version check (`save_viewer(expected_version)`), so a second writer fails loudly instead of overwriting.
- Holdout viewers have an agent too. It assembles facts and commits a **shadow goal** under the same policy, but never acts. This is what makes goal success comparable across arms.

## Planning

- **Goal choice** (`planner.py`) is deterministic: a lifecycle-stage priority list over the goals the viewer is provably eligible for (`goals.py`).
  - G10 is never chosen: its success signal is null.
  - G11 is never chosen: `tv_app_open` does not exist in the stream.
  - G05 ranks high only with purchase intent (a paywall view in the last 14 days). Otherwise its base rate is about 0.4% and it comes last.
- **Revision on change, not on a schedule.** The goal is kept while it is open, in its window and still fits the viewer. It is revised when:
  - a live event meets the success signal (provisional success, the plan closes);
  - a lifecycle event removes the goal's premise (for example, a purchase while on G08 or G09);
  - the deadline passes;
  - a rule correction reopens a provisional success.
- **Plan** = a home page now, plus at most one *conditional* nudge for goals where a message plausibly helps (G01, G03, G07, G12, G04).
  - The nudge is scheduled for 19:00 IST, or 12:30 IST the next day if 19:00 is under 2 hours away. It is never planned past the goal deadline.
  - At send time the agent re-reads history. It sends only if the success signal is still absent and every check passes again.
  - The nudge is cancelled on success, opt-out, a goal change or a data-store fault.

## Source handling (precedence and freshness per fact)

| Fact | Rule |
|---|---|
| Event time | `received_at` is trusted. Buffering = `sent_at - occurred_at`, measured on one phone clock, so it is immune to a wrong clock. Event age = buffering. Triggers older than 600 s are history, not a live visit. |
| Duplicates | `event_id` is unique; re-deliveries are dropped (the trigger ledger records the delivery count). |
| Dialect for the home page | Only the trigger's app setting, because the app requests the page for its current setting. Profile and campaign-link dialects are recorded as conflicts. An unsupported value (`None`, `NA`, `d7`) means no page. |
| Dialect for messages | Latest valid app setting seen in events, then the profile. |
| Subscription status | A lifecycle event received after the profile row wins. Otherwise the profile is used. For duplicate rows, "ever paid" is monotonic: a newer `never` after an older paid row is a bad merge. |
| Consent | Profile opt-in AND no later `notification_opt_out`. No profile row means no consent. Disagreeing duplicate rows mean no consent. |
| "Never did X" | Provable only if signup falls inside the 30-day history window. Otherwise it becomes an *assumption* with reduced certainty (G02, G12). |

Every fact carries `source`, `observed_at`, `age_s`, `status` (ok / conflict_resolved / assumed / unknown), `conflicts`, `resolution` and `refs` (inspectable ids such as `event:<uuid>` or `profile:U-…@updated_at`). Decisions copy the key facts into `evidence_freshness`.

## Memory

SQLite (`store.py`), one file per run:

- `viewer_state`: per-viewer document with the active goal, the plan, decision and action ids, the messages sent (for the frequency cap), observed live events, goal history, lessons, sessions already acted on, and the last facts (a cache for data-store outages).
- `decisions`, `actions`, `goals`, `audit`: append-only records. Goals are indexed by `(goal_id, status)`, so a rule correction touches only open goals of that goal (*no reprocessing of every viewer*).
- `trigger_ledger`: idempotency on `event_id`, with an atomic insert-or-count.
- After judging, each goal's verdict is written to its row and a lesson goes into the viewer's `learned` list. The planner reads `learned` and does not re-pick a goal that failed for that viewer in the last 7 days. A push `token_invalid` disables push for that viewer.

## Verification (independent of the decision step)

`Verifier` re-derives everything from the pack, the viewer's facts and the agent's own action log. It never reads the planner's reasons or any model output.

- **Home page:** holdout/arm, page dialect equal to the app setting, every title in that dialect, available at publish time and not withdrawn, no quarantined (injected-text) titles, unique `content_slug`, row size 3–10, at most 6 rows, no identical page already live, and goal eligibility re-checked.
- **Message:** arm, consent (profile plus opt-out events), quiet hours at `send_at`, at most one message per IST day across channels (platform deliveries plus our own), title dialect and availability, and a working push token.
  - Push copy: length, forbidden phrases, prices only from `pricing.json`, no offers, must name the promoted title, deeplink format.
  - WhatsApp: approved template, exact variable set, values from the catalogue or pricing. `wa_new_episode_v2` is refused because we can't prove an episode is new.
- **Model claims:** a model that asserts eligibility the rules do not support is recorded as `model_claim: rejected` and has no effect on the decision.
- **Injected text:** catalogue descriptions are scanned at load (T0002 and T0522 are flagged). Flagged titles are excluded from all output and logged as a security event. No free text from the data ever reaches a model.

## Latency

- `published_at = deliver_at + queue wait + processing`. Processing is measured wall time plus the simulated latency of every dependency call. Timeouts on the interactive path: model 2.5 s, data store 2 s.
- A home page only helps if it is live before the viewer reaches home, 3–40 s after opening the app. The soft target is 3 s. In the sample run, all 910 pages met it: p50 44 ms, p95 802 ms, max 2.0 s. No decision was over 60 s.
- Budgets: at most 8 model calls and 20 tool calls per decision, enforced by `DecisionClock`. In practice a decision uses at most 1 model call and 2 tool calls. The model is only called for agent-arm viewers with more than one eligible goal or an assumption-based eligibility.
- Load shedding: if a trigger waited in the queue for more than 5 s, the optional model review is skipped.
- Stress replay (each 15:00–15:10 trigger copied 3x, plus 1,000 app opens in one minute, run on only 2 simulated workers): p95 580 ms and a maximum queue wait of 759 ms in the burst minute, with all 1,444 pages inside 3 s.
- With a real model at 1–3 s per call, capacity per worker drops to about 20–60 decisions a minute. The design therefore keeps the model off the critical path. The home page needs no model call, and the review can be shed.

## Failure recovery

| Failure | Behaviour |
|---|---|
| Model timeout / 503 / malformed / schema-invalid | Ignored. The deterministic decision stands, confidence -0.05, and the failure is recorded in `model[]`. |
| Data store slow or timing out | 2 s timeout. Fall back to this agent's cached facts (an existing goal continues) or to trigger-plus-snapshot facts. **A new goal is never committed on partial facts.** The fallback action is a generic in-dialect page, and no messages are sent. |
| Store unavailable at nudge time | The nudge is cancelled ("not sending blind"). |
| Title withdrawn mid-day | Live pages containing the title are found through the action log. Their owning agents republish without it, and the old action is marked superseded. Nudges re-check availability at send time. |
| Goal rule corrected | A new rule version is created. Only open or provisionally-achieved goals of that goal_id are re-pointed. A provisional success that no longer qualifies is reopened. |
| Duplicate or late trigger | Ledger dedupe; same session already acted on means no action; buffered trigger means no action and no goal. |
| Crash mid-decision | Each decision commits its records in one SQLite transaction, so it is either fully recorded or not at all. |

## Trust boundaries

| Input | Treated as |
|---|---|
| `received_at`, our own action log, `pricing.json`, `goals.json`, `interventions.json` | Trusted |
| Phone clocks, the profile snapshot (a day stale, duplicated), campaign links | Evidence with known failure modes; precedence rules above |
| Catalogue titles and descriptions, search queries, deeplinks | Content, never instructions. Scanned, quarantined if instruction-like, never sent to a model |
| Model output | Advice only. Never evidence, never approval. Checked against schema and rules |
| Holdout assignment | Absolute. No action of any kind |

## Measurement

`judge.py` gives two separate answers per goal:

- **rule_met**: did the success signal, under the current rule version, occur in the window?
- **verdict**: was it *attributable*? An exposure (personalised `home_page_shown` of our page, or a delivery or click of our message) must precede the signal. A purchase within 10 minutes of the decision is `in_flight`. Signals without attribution are `inconclusive`.

`impact.py` compares arms by intention to treat. The pre-declared primary metric is first-goal success, with goals committed by the same policy in both arms. Secondary metrics are goal-agnostic: play within 3 days, completion within 3 days, return on days 2–7, purchase within 7 days. It also shows *why* "served vs everyone else" and "served vs unserved" mislead, and re-analyses the pilot: its +22.4 pp headline becomes -1.3 pp under ITT.

## Scaling to 10M viewers and 40 dialects

- Partition by viewer id: a per-viewer actor (or a keyed stream partition) replaces the in-process lock. State goes into a key-value store such as DynamoDB or Bigtable, with the same document plus version check. Decisions, actions and the audit log go to an append-only log (Kafka / object storage), and goals to an indexed table.
- Precompute per-dialect candidate pools and popularity, refreshed on catalogue change. A decision then needs only the viewer's state plus one history read.
- Dialects are data, not code: the validity set comes from the catalogue. A dialect with too few titles simply produces fewer rows (d3 has 16 titles today), never a substitute dialect.
- Use the model asynchronously (copy drafting, periodic goal-policy review) and keep the 3 s home-page path deterministic.
- Rule corrections and title withdrawals use secondary indexes (open goals by goal_id, live pages by title_id), so the work is proportional to the affected viewers.

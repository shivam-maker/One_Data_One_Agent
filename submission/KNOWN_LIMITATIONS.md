# Known limitations

What I left incomplete on purpose, and what I know is weak.

## Decision quality

- **The goal priority is hand-set, not learned.** The priority list encodes lifecycle judgment. The impact report shows per-goal ITT lifts, but they don't yet feed back into goal ranking at the policy level. Per-viewer lessons only block re-picking a goal that just failed. Observed lift is weak for G07 (resume series: 2.9% agent vs 3.8% holdout), so a policy-level learner should demote it or change its lever.
- **G07 is under-specified.** I read "started a series" as "finished at least one episode of it and played it in the last 30 days". A single abandoned sample of episode 1 counts as a rejection, not an unfinished series.
- **G06 "recently lapsed"** is defined as within 30 days (`Settings.recent_lapse_days`). Both choices are documented in decisions as assumptions.
- **G04 is almost never eligible.** Only 4 profiles say `trial_active` and the history has no trial purchases. The `wa_trial_ending_v1` path is therefore implemented in the verifier but not used by the planner.
- **Home-page content ranking is simple:** popularity in the dialect, the viewer's top genre, recency and premium flag. There is no learned recommender, by design: the brief scores decisions, not ranking accuracy.
- **Nudges** go to five goals with one fixed send-slot rule. There is no per-viewer send-time optimisation.

## Real-time and infrastructure

- **The user data store is in memory**, loaded from `events.jsonl` at start-up. It has no persistence or concurrency control of its own. Durable *agent* state is in SQLite.
- **The idempotency ledger** is claimed in its own statement before the decision transaction. A crash between the two would leave a claimed trigger without a decision, and a retry would be treated as a duplicate. In production the claim belongs in the same transaction, or needs a "claimed but unfinished" timeout.
- **Concurrency in replay is simulated:** a worker pool on a simulated clock. The API runs decisions in threads with per-viewer locks over one SQLite connection behind a store lock. That is fine for a demo, not for production throughput.
- **Plan-step timers live in memory** (`AgentSystem.timers`). They are not reloaded if the API process restarts. In the replay, nudges due after the replay day ends are evaluated at the end of the replay with the information available then, and published with their future `send_at`.
- **The latency of mock dependencies is simulated** (deterministic hash). Latency numbers show the design's behaviour, not a real model's.

## Measurement

- **One replay day and one simulator seed.** Per-goal confidence intervals are wide. The primary metric (first-goal success, +1.1 pp) is not significant. Two goal-agnostic secondary metrics are (play within 3 days +5.4 pp, completion within 3 days +5.1 pp). I report this as is, and do not swap the primary metric after seeing results.
- **Primary-metric conditioning.** The primary metric covers viewers who got a goal. Goal commitment depends only on trigger freshness, which is fixed before the agent acts, but the arms differ by chance in how many viewers had only buffered triggers (106 vs 139). The goal-agnostic secondary metrics over all triggered viewers are the cleaner ITT check.
- **Attribution rules are heuristics:** exposure before signal, and a 10-minute in-flight window for purchases. They decide per-viewer verdicts and lessons only. The causal claim comes solely from the ITT arm comparison.
- **No variance reduction** (CUPED on pre-period activity) and no multiple-testing correction across the per-goal table.

## Model

- **The real provider is a thin optional path:** structured output, 2.5 s timeout, no retries. It was not exercised in the sample run, which is mock mode. `model_faults.jsonl` is reproduced only by the mock.
- **Three of the five scripted model faults didn't fire in the replay.** Two were offline-buffered triggers and one was a holdout viewer, so the agent correctly never called the model for them. Each fault type is covered by a unit test instead.
- **The injection scanner is a pattern list.** It catches the two planted descriptions, but a determined attacker could phrase around it. The real protection is structural: data text is never sent to a model or used as an instruction, and only catalogue fields are copied into messages.

## Not done

- No UI. Auditability is through `decisions.jsonl`, `viewer_state/`, `audit.jsonl`, `python -m oua explain` and `GET /v1/viewers/{id}/explain`.
- No authentication on the API.
- No in-app messages: the channel exists in the policy, but the planner does not use it, to keep "at most one nudge" simple.

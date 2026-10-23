# Final report: replay of 2026-10-01

## Headline

- 2679 trigger decisions for 1746 viewers; 90 duplicate deliveries ignored idempotently (no second decision).
- Decision status: {'PUBLISHED': 910, 'HOLDOUT_NO_ACTION': 1325, 'NO_ACTION': 444}
- Published actions: {'home_page': 917, 'push': 269, 'whatsapp': 16}
- Latency: p50 42 ms, p95 801 ms, max 2001 ms; 0 decisions over 60 s, 0 over 5 min.
- Policy problems reported by the simulator: **0** (counts: {'actions_total': 1202, 'home_page_accepted': 917, 'home_page_published_but_not_shown': 14, 'home_page_shown_personalised': 832, 'organic_purchases_scripted': 36, 'push_clicked': 5, 'push_delivered': 235, 'push_requested': 269, 'push_undeliverable_token': 34, 'whatsapp_delivered': 16, 'whatsapp_requested': 16})
- Primary impact (ITT, first-goal success, agent vs holdout): +1.11 pp, 95% CI [-2.4, 4.62], p=0.536. See impact_report.md.
- Earlier pilot, correct ITT: -1.30 pp (CI [-3.76, 1.16]), not the +22.4 pp in its report.

## Why the agent did nothing (agent arm)

- stale/buffered trigger: 191
- page already live (idempotent): 160
- unsupported app dialect: 93
- Holdout viewers (no action by design): 1325 decisions

## Goals committed (new)

- agent G01: 58
- agent G02: 100
- agent G03: 1
- agent G04: 1
- agent G05: 122
- agent G06: 16
- agent G07: 313
- agent G08: 71
- agent G09: 19
- agent G12: 46
- holdout G01: 50
- holdout G02: 101
- holdout G04: 1
- holdout G05: 101
- holdout G06: 14
- holdout G07: 319
- holdout G08: 78
- holdout G09: 12
- holdout G12: 59
- Never chosen: G10 (no success signal defined), G11 (`tv_app_open` is not in the event stream).

## Surprise events: what was noticed and who was affected

- **Offline-buffered app opens**: 424 triggers older than 600s (some days old). They updated memory but committed no goal and published nothing. Age uses `sent_at - occurred_at`, which is immune to a wrong phone clock.
- **Purchase right after a decision**: 8 plans revised on a live purchase; pending nudges cancelled; such purchases are judged 'in_flight' (not credited) when they land within 10 minutes of the decision.
- **Three dialects (app vs profile vs campaign link)**: 141 decisions; the app setting was used, the others are listed in `dialect.conflicts`. Triggers with an unsupported app dialect (None, 'NA', 'd7') got no page, because the app would never show a page in a different dialect.
- **title_withdrawn** at 2026-10-01T07:00:00.000Z: 7 live page(s) contained T0001; each owning agent republished or withdrew its page. Pending nudges re-check availability when they run. affected viewers: 7
- **goal_rule_corrected** at 2026-10-01T08:30:00.000Z: G03 success rule -> G03-r2; re-pointed 0 open goal(s) only (out of 582 goal instances) affected viewers: 0; 1 G03 goal(s) opened later used the corrected rule from the start. Re-pointing open goals is covered by tests/test_agent.py::test_goal_rule_correction_touches_only_open_goals_of_that_goal
- **dependency_unavailable** at 2026-10-01T10:35:00.000Z: model_provider unavailable until 2026-10-01T10:42:00.000Z; decisions in the window use fallbacks (see decisions with degraded=true) decisions in the window: 21, published: 10, model failures handled: 6, on cached/partial facts: 0, max latency 178 ms
- **dependency_degraded** at 2026-10-01T13:10:00.000Z: user_data_store degraded until 2026-10-01T13:17:00.000Z; decisions in the window use fallbacks (see decisions with degraded=true) decisions in the window: 16, published: 4, model failures handled: 0, on cached/partial facts: 14, max latency 2001 ms
- **Unsupported model claims**: rejected in ['D-00885']; the decision used deterministic eligibility. Fault triggers that arrived buffered offline or for holdout viewers never reached the model.
- **Model failures (timeout / malformed / 503)**: 7: Counter({'unavailable': 6, 'malformed output': 1}). All fell back to the deterministic path.
- **Data store degraded**: 14 decisions used cached facts or trigger-only facts; none committed a new goal on partial facts.
- **Catalogue text with instructions**: ['T0002', 'T0522'] quarantined; no page or message contains them and catalogue text never reaches a model.
- **Evening peak / busiest minute**: 79 triggers in 2026-10-01T04:45, p95 latency 57 ms; evening peak p95 770 ms.

## Files

- decisions.jsonl: every decision (including no-action and holdout decisions) with evidence, freshness, conflicts, checks, confidence and timing
- actions.jsonl: what was published (input to the simulator)
- outcomes.jsonl: goal verdicts and attribution
- viewer_state/: one durable state document per viewer (goals, plan, history, lessons)
- latency_report.json, impact_report.md, impact.json, audit.jsonl, control_log.json, sim/

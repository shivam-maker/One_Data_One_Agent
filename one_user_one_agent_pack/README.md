# One User, One Agent: data pack

Synthetic data for the take-home assignment "One User, One Agent". Every user, event and title is generated; no real
person or title appears. The data is deliberately imperfect in the ways real product data is imperfect. Finding and
handling those imperfections is part of the assignment.

Only this pack is valid evidence about users and titles.

## Contents

| Path | What it is |
|---|---|
| `data/events.jsonl` | 30 days of app events (2026-09-01 to 2026-09-30 IST) for 5000 users: 142,906 lines |
| `data/profiles_snapshot.csv` | Daily warehouse export taken 2026-09-30 05:30 IST: 4,888 rows |
| `data/catalogue.csv` | 2,014 title records across six dialects (`d1`-`d6`) |
| `data/episodes.csv` | Episodes for every title: 26,986 rows |
| `config/goals.json` | The 12 candidate goals, with eligibility and success conditions |
| `config/interventions.json` | What the agent can change (home page, push, WhatsApp, in-app message) and the rules |
| `config/pricing.json` | The only prices that exist |
| `config/messaging_policy.md` | Consent, quiet hours, frequency, honesty and holdout rules |
| `pilot/pilot_assignment.csv` | Randomized arms of an earlier pilot (days 1-14) and whether each user was served |
| `pilot/pilot_outcomes.csv` | Each pilot user's goal and whether it succeeded |
| `pilot/pilot_summary_report.md` | The internal summary report written after the pilot |
| `replay/triggers.jsonl` | The replay day (2026-10-01 IST): 2,769 app-open and login triggers, in delivery order |
| `replay/live_events.jsonl` | Other events that arrive during the replay day, in delivery order |
| `replay/control_events.jsonl` | Operational events during the replay day (corrections, outages, catalogue changes) |
| `replay/model_faults.jsonl` | Faults your mock model provider must reproduce at the given triggers |
| `replay/arms.csv` | Randomized arm of every user for the replay: `agent` or `holdout` |
| `simulator/simulate_outcomes.py` | Plays the users and the app after your agent acts (standard-library Python) |

## Conventions

- Timestamps are ISO-8601 UTC (`...Z`). IST is UTC+05:30. All users are in India.
- `received_at` is set by our servers and is trustworthy. `occurred_at` and `sent_at` come from the phone's clock,
  which can be wrong. Some phones buffer events offline and send them much later.
- `app_dialect` and `app_language` on every event are the app's current settings (the "app settings feed").
- An event can be delivered more than once with the same `event_id`.
- `content_slug` identifies the content; a title can have more than one `title_id` (language variants).

## data/events.jsonl

One JSON object per line:

```
{"event_id": "uuid", "user_id": "U-00042", "event": "app_open",
 "occurred_at": "...Z", "sent_at": "...Z", "received_at": "...Z", "session_id": "uuid",
 "app_dialect": "d2", "app_language": "hin", "platform": "android", "app_version": "6.1.4",
 "properties": {}}
```

| event | properties |
|---|---|
| `signup_completed` | `method` |
| `login_success` | none |
| `app_open` | `deeplink` (optional): `ott://c/<campaign>/<dialect>/title/<title_id>` |
| `app_dialect_changed` | `from`, `to` |
| `home_viewed` | `layout`: `default` or `personalised` |
| `title_viewed` | `title_id` |
| `search` | `query` |
| `play_started` | `title_id`, `episode_id` |
| `play_progress` | `title_id`, `episode_id`, `percent` (25, 50, 75) |
| `episode_completed` | `title_id`, `episode_id` (90% or more watched) |
| `paywall_viewed` | `title_id` (optional) |
| `purchase_completed` | `plan_id`, `price_inr`, `is_trial` |
| `subscription_renewed` | `plan_id`, `price_inr` |
| `subscription_lapsed` | none |
| `message_delivered`, `message_clicked` | `channel`, `campaign_id`, `title_id` |
| `notification_opt_out` | `channel` |

## data/profiles_snapshot.csv

`user_id, signup_at, subscription_status (never | trial_active | active | lapsed), plan_started_at, plan_ends_at,
preferred_dialect, preferred_language, notifications_opt_in, whatsapp_opt_in, platform, updated_at, snapshot_at`

## data/catalogue.csv and data/episodes.csv

`title_id, content_slug, title, dialect, language, content_type (film | show | micro_drama), genres (| separated),
release_date, available_from, available_until (empty = no end), is_premium, description`

`episode_id, content_slug, season, episode, minutes`

## The replay day

`replay/triggers.jsonl` has the same schema as `events.jsonl` plus `deliver_at`, the moment the trigger reaches your
agent. Feed triggers to your agent in `deliver_at` order, together with `live_events.jsonl` and
`control_events.jsonl`. You may run faster than real time, but measure your own processing time per decision and
record each action's `published_at` as `deliver_at + your measured processing time`.

`control_events.jsonl` lines have `at` and `type` (`title_withdrawn`, `goal_rule_corrected`,
`dependency_unavailable`, `dependency_degraded`). Your harness should make the named dependency fail or slow down
for the stated window.

`model_faults.jsonl`: when your agent handles the named trigger, your mock or replay model provider must return the
given `model_output` (or time out, for `fault: timeout`) instead of a normal answer.

## Actions (input to the simulator)

Write every action your agent publishes to a JSONL file, one object per line:

```
{"action_id": "A-0001", "decision_id": "D-0001", "user_id": "U-00042", "trigger_event_id": "uuid",
 "type": "home_page", "published_at": "...Z", "dialect": "d2", "hero_title_id": "T0042",
 "rows": [{"row_type": "popular_in_dialect", "title_ids": ["T0101", "T0102", "T0103"]}]}

{"action_id": "A-0002", "decision_id": "D-0001", "user_id": "U-00042", "trigger_event_id": "uuid",
 "type": "push", "published_at": "...Z", "send_at": "...Z", "title_id": "T0101",
 "text": {"title": "...", "body": "..."}, "deeplink": "ott://title/T0101"}

{"action_id": "A-0003", ..., "type": "whatsapp", "send_at": "...Z", "template_id": "wa_recommend_title_v1",
 "variables": {"title_name": "...", "dialect_name": "..."}, "title_id": "T0101"}
```

`type` is one of `home_page`, `push`, `whatsapp`, `in_app_message`. Doing nothing is not an action; record it in your
decision output.

## Running the simulator

```
python simulator/simulate_outcomes.py --pack . --actions sample_run/actions.jsonl --out sample_run/sim
```

It writes `outcome_events.jsonl` (what the triggered users do in the 14 days after the replay day, in the
`events.jsonl` schema, plus `home_page_shown` and `message_failed` events) and `simulator_report.json`. Use these
events to judge your goals and to measure impact against the holdout. Our evaluators run the same simulator with a
different seed and a second, unseen replay day.

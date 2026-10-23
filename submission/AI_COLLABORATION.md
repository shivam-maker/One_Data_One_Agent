# AI collaboration log

> **Candidate: review and extend this file before you submit.** It records what happened in the Claude Code session
> that produced the first version of this repository. Add your own review notes, the decisions you overruled, and
> anything you change afterwards. You are responsible for every line you submit.

## Tools

- **Claude Code (Claude Opus 5.5)** in VS Code. It read the brief and the data pack, profiled the data, wrote the code, tests and documents, and ran the replay, the simulator and the test suite.
- The data pack's own simulator (`simulator/simulate_outcomes.py`) was used as the outcome oracle. It was read in full before the agent was designed, so the design follows what the platform actually accepts (row rules, dialect matching, page TTL, consent and quiet hours).

## How the output was checked

1. **Data profiling before design.** Scripts counted the traps in the pack before any agent code was written:
   - 2,176 duplicate event ids and 90 duplicate triggers;
   - 453 triggers older than 10 minutes, and phone clocks wrong by up to 9 days;
   - 30 viewers with duplicate profile rows (for example, `lapsed` vs `never`) and 103 triggered viewers with no profile;
   - app dialects `None`, `NA` and `d7` on 185 triggers;
   - injected instructions in the descriptions of T0002 and T0522;
   - G10 (null signal) and G11 (`tv_app_open` absent);
   - the pilot's served-vs-others comparison.
2. **Every run was inspected, not just executed.** After each replay, the goal mix, the no-action reasons, the fault-trigger decisions, the simulator's policy-problem report and the latency report were reviewed.
3. **Tests:** 23 automated tests, including a full replay with invariants asserted over every published action:
   - holdout viewers never get an action;
   - every page title is in the page dialect and available;
   - rows have 3 or more titles, with unique slugs;
   - no messages in quiet hours or above the daily cap;
   - one decision per trigger, and every no-action has a reason.
4. **The simulator reports 0 policy problems** for the sample run (no consent, quiet-hour, cap, holdout or template violations).

## Course corrections made during the session (found by checking output)

| What the first version did | How it was found | Change |
|---|---|---|
| G07 (resume series) took most goals, because anyone who sampled one episode weeks ago counted as "unfinished" | Goal distribution of the first replay | "Started" now means at least one finished episode plus a play in the last 30 days |
| A second trigger whose identical page was already live came out as `BLOCKED_BY_CHECKS` | Reviewing the one blocked decision | Treated as an idempotent `NO_ACTION` with the reason |
| Only one of the three "unsupported claim" faults was exercised | Inspecting decisions on fault triggers | Confirmed the other two were buffered triggers and the third was a holdout viewer (correctly no model call); added unit tests for every fault type |
| The goal assignment rate differed by arm (86.4% vs 83.0%) | Impact report sanity check | Traced to viewers whose only triggers were buffered (106 vs 139), a pre-treatment property; explained in the report instead of hidden |
| The purchase test passed without reaching the in-flight path (the test viewer's goal wasn't G05) | Checking which branch ran | The test now selects a G05 viewer and asserts the `in_flight` verdict |
| SQLite calls from API threads could interleave inside another thread's transaction | Code review of the API path | Every store method now runs under a re-entrant store lock |

## Suggestions deliberately not taken

- **Letting the model choose the goal or the titles.** That would make eligibility depend on generated text (the brief's "will score poorly" list) and make goal assignment differ between arms. The model only reviews (confidence only) and drafts copy that is verified.
- **Using the profile's `preferred_dialect` when the app reports an unsupported dialect.** The simulator, and the brief's dialect rule, show the app only renders a page for its own setting, so a substitute-dialect page would never be seen. The agent does nothing instead.
- **Reporting "served vs not served" uplift.** It looks impressive (+20 to +37 pp in this run) and is selection. Only ITT is reported as impact.
- **Swapping the primary metric to the significant secondary one after seeing results.** The primary metric stays as declared.

## Candidate notes

_(add yours here)_

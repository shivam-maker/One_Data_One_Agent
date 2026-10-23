# Impact report

Population: every viewer with at least one trigger on the replay day, analysed by **randomized arm** (intention to treat): 863 agent-arm viewers, 883 holdout viewers. Holdout viewers received no action; the agent still committed a *shadow goal* for them with the same deterministic policy, so goal success can be compared like for like.

## Primary metric (declared before looking at results)

Share of viewers whose first committed goal met its success rule inside its window, agent vs holdout:

- 14.3% (107/746) vs 13.2% (97/733): **+1.1 pp**, 95% CI [-2.4, +4.6], p=0.536
- Goal assignment rate: agent 86.4%, holdout 83.0%. Goals never depend on the arm; the gap comes from viewers whose only triggers were buffered offline (agent 105, holdout 137), a property of the trigger stream fixed before the agent acts. No goal is committed for them in either arm.

Per goal (first goal per viewer, rule met):

| Goal | Agent | Holdout | Diff (pp) | 95% CI | p |
|---|---|---|---|---|---|
| G01 | 32.8% (19/58) | 28.0% (14/50) | +4.8 | [-12.6, +22.1] | 0.5924 |
| G02 | 16.0% (16/100) | 10.9% (11/101) | +5.1 | [-4.3, +14.5] | 0.2882 |
| G03 | 0.0% (0/1) | 0.0% (0/0) | +0.0 | [+nan, +nan] | None |
| G04 | 0.0% (0/1) | 0.0% (0/1) | +0.0 | [+0.0, +0.0] | None |
| G05 | 4.9% (6/122) | 4.0% (4/101) | +1.0 | [-4.5, +6.4] | 0.7309 |
| G06 | 6.2% (1/16) | 7.1% (1/14) | -0.9 | [-18.9, +17.1] | 0.9221 |
| G07 | 2.9% (9/313) | 3.8% (12/319) | -0.9 | [-3.7, +1.9] | 0.5342 |
| G08 | 33.8% (24/71) | 28.6% (22/77) | +5.2 | [-9.7, +20.2] | 0.4921 |
| G09 | 38.9% (7/18) | 36.4% (4/11) | +2.5 | [-33.7, +38.8] | 0.8918 |
| G12 | 54.4% (25/46) | 49.1% (29/59) | +5.2 | [-14.0, +24.4] | 0.5972 |

## Secondary metrics (all triggered viewers, goal-agnostic)

| Metric | Result |
|---|---|
| play_3d | 57.1% (493/863) vs 51.8% (457/883): **+5.4 pp**, 95% CI [+0.7, +10.0], p=0.0243 |
| complete_3d | 34.4% (297/863) vs 29.3% (259/883): **+5.1 pp**, 95% CI [+0.7, +9.4], p=0.0226 |
| return_day2_7 | 80.0% (690/863) vs 78.8% (696/883): **+1.1 pp**, 95% CI [-2.7, +4.9], p=0.559 |
| purchase_7d | 2.8% (24/863) vs 3.6% (32/883): **-0.8 pp**, 95% CI [-2.5, +0.8], p=0.3175 |

## Randomization check (pre-period)

- agent: {'viewers': 863, 'pre_period_plays_per_viewer': 4.53, 'status_mix': {'never': 383, 'active': 323, 'lapsed': 102, 'no_profile': 54, 'trial_active': 1}}
- holdout: {'viewers': 883, 'pre_period_plays_per_viewer': 4.65, 'status_mix': {'never': 380, 'lapsed': 113, 'active': 340, 'no_profile': 49, 'trial_active': 1}}

## Comparisons we do not trust, and why

1. **Served vs everyone else** (the pilot report's method), play within 3 days: 67.5% (421/624) vs 47.1% (529/1122): **+20.3 pp**, 95% CI [+15.6, +25.0], p=0.0. 'Served' here means the viewer actually saw a personalised page, which requires them to have opened the app in the page's dialect while it was live. Those viewers are already the most active, so this compares engaged viewers with everyone else and mixes the agent's effect with selection.
2. **Served vs unserved inside the agent arm**: 67.5% (421/624) vs 30.1% (72/239): **+37.3 pp**, 95% CI [+30.5, +44.2], p=0.0. This is the same selection problem: who gets served depends on behaviour after randomization.
3. **Agent-arm success rate alone** (verdict 'succeeded'): it has no counterfactual. Many viewers would reach the goal anyway, as the holdout shows.

Only the arm-level comparison is causal: the arms were randomized before the replay, every viewer is counted in the arm they were assigned to, and the holdout received nothing.

## Per-viewer attribution (agent arm)

- Verdicts: {'failed': 640, 'succeeded': 94, 'inconclusive': 13}
- Attribution of rule-met goals: {'exposed_before_signal:home_page_shown': 89, 'exposed_before_signal:message_clicked': 1, 'in_flight': 5, 'no_prior_exposure': 8, 'exposed_before_signal:message_delivered': 4}
- 'in_flight': a purchase within 10 minutes of the decision (the purchase flow was already open). 'no_prior_exposure': the signal happened before the viewer saw anything from the agent. Neither is credited, but both still count in the ITT comparison above, because ITT does not need attribution.

## Re-analysis of the earlier pilot

- Report headline (served vs all others): 40.1% (87/217) vs 17.7% (649/3674): **+22.4 pp**, 95% CI [+15.8, +29.1], p=0.0
- **Correct ITT (treatment arm vs holdout arm): 18.2% (349/1912) vs 19.6% (387/1979): **-1.3 pp**, 95% CI [-3.8, +1.2], p=0.2998**
- Unserved treatment viewers vs holdout: 15.5% (262/1695) vs 19.6% (387/1979): **-4.1 pp**, 95% CI [-6.5, -1.6], p=0.0012

The pilot's 2.3x uplift is a selection effect. 'Served' viewers were the ones who came back and saw a page; they were compared with a group that includes inactive viewers and the whole holdout. Compared by arm, the pilot shows no positive effect. The unserved treatment viewers doing *worse* than the holdout is the signature of selection: the served group took the engaged viewers out of the treatment arm.

## Caveats

- Outcomes come from the supplied simulator with one seed. Evaluators use another seed; the ITT design is unchanged, but the numbers will move.
- One replay day gives a few hundred viewers per goal, so per-goal intervals are wide.
- Goals were committed only on live (fresh) triggers. Viewers whose only triggers were buffered offline have no goal in either arm.

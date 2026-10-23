#!/usr/bin/env python3
"""Outcome simulator for the "One User, One Agent" replay (Python 3.9+, standard library only).

It plays the role of the users and the app after the replay day: given the actions your agent published, it
generates what each triggered user does over the next 14 days (app opens, home pages shown, plays, completions,
purchases, message delivery, clicks and opt-outs).

    python simulator/simulate_outcomes.py --pack . --actions sample_run/actions.jsonl --out sample_run/sim

Writes <out>/outcome_events.jsonl (same schema as data/events.jsonl) and <out>/simulator_report.json.
The same pack, the same actions and the same --seed always give the same output.
"""
import argparse
import csv
import hashlib
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
REPLAY_START = datetime(2026, 10, 1, tzinfo=IST)
HORIZON_DAYS = 14
PAGE_TTL = timedelta(minutes=120)
MIN_ROW = 3
MAX_ROW = 10
MAX_ROWS = 6


def h01(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None


def iso(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def ist_day(dt):
    return dt.astimezone(IST).date()


def quiet_hours(dt):
    h = dt.astimezone(IST).hour
    return h >= 22 or h < 8


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_pack(pack):
    cat = {}
    with open(os.path.join(pack, 'data/catalogue.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            cat[r['title_id']] = {
                'slug': r['content_slug'], 'dialect': r['dialect'], 'type': r['content_type'],
                'genres': r['genres'].split('|'), 'premium': r['is_premium'] == 'true',
                'from': ts(r['available_from']), 'until': ts(r['available_until']) if r['available_until'] else None}
    episodes = defaultdict(list)
    with open(os.path.join(pack, 'data/episodes.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            episodes[r['content_slug']].append((int(r['season']), int(r['episode']), r['episode_id']))
    for v in episodes.values():
        v.sort()
    profiles = {}
    with open(os.path.join(pack, 'data/profiles_snapshot.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            old = profiles.get(r['user_id'])
            if old is None or r['updated_at'] > old['updated_at']:
                profiles[r['user_id']] = r
    arms = {}
    with open(os.path.join(pack, 'replay/arms.csv'), encoding='utf-8') as f:
        for r in csv.DictReader(f):
            arms[r['user_id']] = r['arm']
    with open(os.path.join(pack, 'config/interventions.json'), encoding='utf-8') as f:
        interventions = json.load(f)
    return cat, episodes, profiles, arms, interventions


def available(t, at):
    return t is not None and t['from'] <= at and (t['until'] is None or at < t['until'])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pack', default='.', help='root of the data pack')
    ap.add_argument('--actions', required=True, help='JSONL of published actions (see README, "Actions")')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--seed', default='oua-practice', help='simulation seed (evaluators use their own)')
    a = ap.parse_args()
    pack = a.pack
    cat, episodes, profiles, arms, interventions = load_pack(pack)
    approved_templates = {t['template_id']: t for t in interventions['whatsapp']['approved_templates']}

    # Triggers and scripted events of the replay day.
    triggers = defaultdict(dict)
    for e in read_jsonl(os.path.join(pack, 'replay/triggers.jsonl')):
        triggers[e['user_id']][e['event_id']] = e
    live = defaultdict(list)
    for e in read_jsonl(os.path.join(pack, 'replay/live_events.jsonl')):
        live[e['user_id']].append(e)
    withdrawn = {}
    for c in read_jsonl(os.path.join(pack, 'replay/control_events.jsonl')):
        if c['type'] == 'title_withdrawn':
            withdrawn[c['title_id']] = ts(c['at'])
    for tid, at in withdrawn.items():
        if tid in cat and (cat[tid]['until'] is None or at < cat[tid]['until']):
            cat[tid]['until'] = at

    # Observable history of the triggered users only.
    users = set(triggers)
    hist = {u: {'days': set(), 'sessions': set(), 'play_sessions': set(), 'plays': 0, 'completes': 0,
                'genres': defaultdict(int), 'progress': {}, 'opt_out': set(), 'paid': False, 'first': None}
            for u in users}
    for e in read_jsonl(os.path.join(pack, 'data/events.jsonl')):
        u = e['user_id']
        if u not in hist:
            continue
        h = hist[u]
        r = ts(e['received_at'])
        h['first'] = r if h['first'] is None or r < h['first'] else h['first']
        ev = e['event']
        if ev == 'app_open':
            h['days'].add(ist_day(r))
            h['sessions'].add(e['session_id'])
        elif ev == 'play_started':
            h['plays'] += 1
            h['play_sessions'].add(e['session_id'])
            t = cat.get(e['properties'].get('title_id'))
            if t:
                for g in t['genres']:
                    h['genres'][g] += 1
        elif ev == 'episode_completed':
            h['completes'] += 1
            t = cat.get(e['properties'].get('title_id'))
            if t:
                h['progress'][t['slug']] = e['properties'].get('episode_id')
        elif ev == 'notification_opt_out':
            h['opt_out'].add(e['properties'].get('channel'))
        elif ev in ('purchase_completed', 'subscription_renewed'):
            h['paid'] = True
        elif ev == 'subscription_lapsed':
            h['paid'] = False

    # Actions.
    actions = defaultdict(list)
    report = defaultdict(int)
    problems = []
    for act in read_jsonl(a.actions):
        u = act.get('user_id')
        report['actions_total'] += 1
        if u not in users:
            report['rejected_unknown_or_untriggered_user'] += 1
            continue
        if arms.get(u) == 'holdout':
            report['holdout_actions_ignored'] += 1
            problems.append({'action_id': act.get('action_id'), 'user_id': u, 'problem': 'action_on_holdout_user'})
            continue
        actions[u].append(act)

    out_events = []
    rng_seed = a.seed

    def emit(u, event, when, session, props=None, dialect=None):
        out_events.append({'event_id': '%s-%06d' % (rng_seed[:3], len(out_events)), 'user_id': u, 'event': event,
                           'occurred_at': iso(when), 'sent_at': iso(when), 'received_at': iso(when),
                           'session_id': session, 'app_dialect': dialect, 'app_language': None, 'platform': None,
                           'app_version': None, 'properties': props or {}})

    def validate_page(act, u):
        """Returns (valid_rows, hero_ok, reasons). Mirrors what the backend accepts."""
        reasons = []
        at = ts(act.get('published_at'))
        d = act.get('dialect')
        rows_ok = []
        seen = set()
        for row in (act.get('rows') or [])[:MAX_ROWS]:
            keep = []
            for tid in (row.get('title_ids') or [])[:MAX_ROW]:
                t = cat.get(tid)
                if not t or t['dialect'] != d or not available(t, at) or t['slug'] in seen:
                    continue
                seen.add(t['slug'])
                keep.append(tid)
            if len(keep) >= MIN_ROW:
                rows_ok.append({'row_type': row.get('row_type'), 'title_ids': keep})
            else:
                reasons.append('row_dropped_below_minimum')
        if len(act.get('rows') or []) > MAX_ROWS:
            reasons.append('rows_over_maximum_ignored')
        hero = cat.get(act.get('hero_title_id')) if act.get('hero_title_id') else None
        hero_ok = bool(hero and hero['dialect'] == d and available(hero, at))
        if act.get('hero_title_id') and not hero_ok:
            reasons.append('hero_rejected')
        return rows_ok, hero_ok, reasons

    for u in sorted(users):
        rng = random.Random('%s:%s' % (rng_seed, u))
        h = hist[u]
        prof = profiles.get(u) or {}
        span = max(1, (REPLAY_START - (h['first'] or REPLAY_START - timedelta(days=1))).days)
        open_rate = min(0.9, max(0.04, len(h['days']) / min(30, span)))
        sess = max(1, len(h['sessions']))
        play_rate = min(0.8, max(0.15, len(h['play_sessions']) / sess)) if h['sessions'] else 0.3
        complete_rate = min(0.8, max(0.2, h['completes'] / h['plays'])) if h['plays'] else 0.35
        status = prof.get('subscription_status') or 'unknown'
        paid = h['paid'] or status == 'active'
        top_genre = max(h['genres'], key=h['genres'].get) if h['genres'] else None
        push_ok = prof.get('notifications_opt_in') == 'true' and 'push' not in h['opt_out']
        wa_ok = prof.get('whatsapp_opt_in') == 'true' and 'whatsapp' not in h['opt_out']
        token_ok = h01('token', u) < 0.87
        base_buy = 0.0 if paid else {'never': 0.004, 'trial_active': 0.03, 'lapsed': 0.006}.get(status, 0.006)
        organic_paid_at = min([ts(e['received_at']) for e in live[u] if e['event'] == 'purchase_completed'] or [None],
                              key=lambda x: x or datetime.max.replace(tzinfo=UTC))

        for e in live[u]:  # scripted organic events happen regardless of the agent
            out_events.append(dict(e))
        if organic_paid_at:
            paid = True

        # Pages: only accepted (validated) pages are stored by the backend.
        pages = []
        for act in sorted((x for x in actions[u] if x.get('type') == 'home_page'), key=lambda x: x.get('published_at') or ''):
            rows_ok, hero_ok, reasons = validate_page(act, u)
            for r_ in reasons:
                report['home_page_' + r_] += 1
            if not rows_ok and not hero_ok:
                report['home_page_rejected_empty'] += 1
                continue
            report['home_page_accepted'] += 1
            pages.append((ts(act['published_at']), act.get('dialect'), rows_ok, hero_ok, act))

        def page_effect(at, dialect):
            live_pages = [p for p in pages if p[0] <= at < p[0] + PAGE_TTL]
            if not live_pages:
                return None, 1.0, False
            p = live_pages[-1]
            if p[1] != dialect:  # the app only requests (and shows) the page for its own dialect
                return p, 1.0, False
            mult = 1.0 + 0.10 * min(5, len(p[2])) + (0.05 if p[3] else 0.0)
            ids = [t for r_ in p[2] for t in r_['title_ids']]
            if top_genre and any(top_genre in cat[t]['genres'] for t in ids):
                mult += 0.08
            for r_ in p[2]:
                if r_.get('row_type') == 'next_episode':
                    for t in r_['title_ids']:
                        if cat[t]['slug'] in h['progress']:
                            mult += 0.15
                            break
            return p, min(mult, 1.8), any(cat[t]['premium'] for t in ids)

        carry = {'exposed': False, 'played_after_exposure': False}

        def session(at, dialect, sid, from_message=None):
            nonlocal paid
            srng = random.Random('%s:%s:%s' % (rng_seed, u, sid))
            u_home, u_play, u_complete, u_buy = srng.random(), srng.random(), srng.random(), srng.random()
            d_home, d_play, d_done, d_buy = srng.randint(3, 40), srng.randint(30, 300), srng.randint(3, 40), srng.randint(2, 20)
            pick = srng.random()
            emit(u, 'app_open', at, sid, {}, dialect)
            mult, premium, personalised = 1.0, False, False
            if u_home < 0.85 or from_message:
                hv = at + timedelta(seconds=d_home)
                p, mult, premium = page_effect(hv, dialect)
                personalised = p is not None and p[1] == dialect
                emit(u, 'home_page_shown', hv, sid, {'personalised': personalised,
                                                       'page_action_id': p[4].get('action_id') if personalised else None},
                     dialect)
                if personalised:
                    report['home_page_shown_personalised'] += 1
                    carry['exposed'] = True
                elif p is not None:
                    report['home_page_published_but_not_shown'] += 1
            pr = min(0.95, play_rate * mult * (1.25 if from_message else 1.0))
            if u_play < pr:
                pool = [tid for tid, t in cat.items() if t['dialect'] == dialect and available(t, at)]
                if from_message and from_message in cat:
                    pool = [from_message]
                if pool:
                    tid = pool[int(pick * min(40, len(pool)))]
                    eps = episodes.get(cat[tid]['slug']) or [(1, 1, cat[tid]['slug'] + '-s1e1')]
                    ep = eps[0][2]
                    t1 = at + timedelta(seconds=d_play)
                    emit(u, 'play_started', t1, sid, {'title_id': tid, 'episode_id': ep}, dialect)
                    if personalised:
                        carry['played_after_exposure'] = True
                    if u_complete < complete_rate:
                        emit(u, 'episode_completed', t1 + timedelta(minutes=d_done), sid,
                             {'title_id': tid, 'episode_id': ep}, dialect)
            buy = base_buy * (1.5 if premium else 1.0) * (3.0 if from_message and cat.get(from_message, {}).get('premium') else 1.0)
            if not paid and u_buy < buy:
                paid = True
                emit(u, 'purchase_completed', at + timedelta(minutes=d_buy), sid,
                     {'plan_id': 'P_MONTH', 'price_inr': 149, 'is_trial': False}, dialect)

        # Replay day: live sessions are the fresh triggers (dedupe by event_id; offline-buffered history is not a visit).
        seen_sessions = set()
        dialect_now = None
        for e in sorted(triggers[u].values(), key=lambda x: x['received_at']):
            r = ts(e['received_at'])
            true_t = r - (ts(e['sent_at']) - ts(e['occurred_at']))
            dialect_now = e.get('app_dialect') or dialect_now
            if (r - true_t).total_seconds() > 600 or e['session_id'] in seen_sessions:
                continue
            seen_sessions.add(e['session_id'])
            session(r, e.get('app_dialect'), e['session_id'])

        # Messages.
        sent_per_day = defaultdict(int)
        for act in sorted((x for x in actions[u] if x.get('type') in ('push', 'whatsapp', 'in_app_message')),
                          key=lambda x: x.get('send_at') or x.get('published_at') or ''):
            ch = act['type']
            at = ts(act.get('send_at') or act.get('published_at'))
            tid = act.get('title_id')
            report[ch + '_requested'] += 1
            if ch == 'in_app_message':
                report['in_app_message_queued'] += 1
                continue
            if ch == 'push' and not push_ok or ch == 'whatsapp' and not wa_ok:
                report[ch + '_blocked_no_consent'] += 1
                problems.append({'action_id': act.get('action_id'), 'user_id': u, 'problem': ch + '_without_consent'})
                emit(u, 'message_failed', at, None, {'channel': ch, 'reason': 'no_consent', 'action_id': act.get('action_id')})
                continue
            if ch == 'whatsapp' and act.get('template_id') not in approved_templates:
                report['whatsapp_rejected_unapproved_template'] += 1
                emit(u, 'message_failed', at, None, {'channel': ch, 'reason': 'template_not_approved', 'action_id': act.get('action_id')})
                continue
            if not token_ok and ch == 'push':
                report['push_undeliverable_token'] += 1
                emit(u, 'message_failed', at, None, {'channel': ch, 'reason': 'token_invalid', 'action_id': act.get('action_id')})
                continue
            mrng = random.Random('%s:%s:msg:%s' % (rng_seed, u, act.get('action_id')))
            day = ist_day(at)
            sent_per_day[day] += 1
            emit(u, 'message_delivered', at + timedelta(seconds=mrng.randint(2, 30)), None,
                 {'channel': ch, 'action_id': act.get('action_id'), 'title_id': tid})
            report[ch + '_delivered'] += 1
            night, over_cap = quiet_hours(at), sent_per_day[day] > 1
            if night:
                problems.append({'action_id': act.get('action_id'), 'user_id': u, 'problem': ch + '_in_quiet_hours'})
            if over_cap:
                problems.append({'action_id': act.get('action_id'), 'user_id': u, 'problem': 'frequency_cap_exceeded'})
            if (night and mrng.random() < 0.08) or (over_cap and mrng.random() < 0.10):
                emit(u, 'notification_opt_out', at + timedelta(minutes=mrng.randint(1, 30)), None, {'channel': ch})
                report[ch + '_caused_opt_out'] += 1
                continue
            t = cat.get(tid)
            right = t is not None and t['dialect'] == (dialect_now or prof.get('preferred_dialect')) and available(t, at)
            p_click = (0.012 if ch == 'push' else 0.03) * (1.0 if right else 0.2) * (0.3 if night else 1.0)
            if mrng.random() < p_click:
                c = at + timedelta(minutes=mrng.randint(1, 90))
                emit(u, 'message_clicked', c, None, {'channel': ch, 'action_id': act.get('action_id'), 'title_id': tid})
                report[ch + '_clicked'] += 1
                session(c + timedelta(seconds=5), dialect_now, 'msg-%s' % act.get('action_id'), from_message=tid if right else None)

        # The next 14 days.
        for d in range(1, HORIZON_DAYS + 1):
            drng = random.Random('%s:%s:day%d' % (rng_seed, u, d))
            boost = 1.0
            if d <= 3 and carry['played_after_exposure']:
                boost = 1.15
            elif d <= 3 and carry['exposed']:
                boost = 1.08
            u_open, two = drng.random(), drng.random() < 0.35
            if u_open < min(0.95, open_rate * boost):
                for k in range(1 + two):
                    hour = drng.choice([9, 12, 13, 18, 19, 20, 21, 21, 22])
                    at = (REPLAY_START + timedelta(days=d, hours=hour, minutes=drng.randint(0, 59))).astimezone(UTC)
                    session(at, dialect_now, 's%s-%d-%d' % (u, d, k))
        if organic_paid_at:
            report['organic_purchases_scripted'] += 1

    out_events.sort(key=lambda e: e['received_at'])
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, 'outcome_events.jsonl'), 'w', encoding='utf-8') as f:
        for e in out_events:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')
    rep = {'seed': rng_seed, 'triggered_users': len(users), 'events_written': len(out_events),
           'counts': dict(sorted(report.items())), 'policy_problems': problems[:500],
           'policy_problem_count': len(problems)}
    with open(os.path.join(a.out, 'simulator_report.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, indent=2)
    print('wrote %d events for %d users -> %s' % (len(out_events), len(users), a.out), file=sys.stderr)


if __name__ == '__main__':
    main()

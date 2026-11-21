#!/usr/bin/env python3
"""Conservative historical calibration. Preview by default; output contains counts only.

The deployed schema uses room_stats.current_user_count and gift_messages.total_value
for viewer counts and diamond values. Snapshots are associated by room and time.
"""
import argparse
from collections import defaultdict
from datetime import timedelta
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from sqlalchemy import func, update
from models.database import ChatMessage, GiftMessage, LiveSession, RoomStats, UserContribution, get_china_now
from services.data_service import DataService


def repair_data(service, apply=False):
    """Plan changes in memory, and commit a single transaction only with apply=True."""
    now = get_china_now().replace(tzinfo=None)
    cutoff = now - timedelta(days=config.DATA_RETENTION_DAYS or 90)
    counts = dict(peaks=0, orphan_chats=0, orphan_gifts=0, sessions=0, contributions=0)
    db = service.get_session()
    try:
        sessions = db.query(LiveSession).order_by(LiveSession.start_time.desc(), LiveSession.id.desc()).all()
        changes = []
        by_room = defaultdict(list)
        by_id = {}
        for live in sessions:
            by_room[live.live_id].append(live)
            by_id[live.id] = live
        # Do not decrease peaks; snapshots contain no session id in this schema.
        for live in sessions:
            finish = (live.end_time or now) + timedelta(seconds=5)
            peak = db.query(func.max(RoomStats.current_user_count)).filter(
                RoomStats.live_id == live.live_id,
                RoomStats.stats_at >= live.start_time, RoomStats.stats_at <= finish).scalar()
            if peak is not None and peak > (live.peak_viewer_count or 0):
                counts['peaks'] += 1
                changes.append(('peak', live.id, int(peak)))

        extra_income, extra_gifts, extra_chats = defaultdict(float), defaultdict(int), defaultdict(int)
        assigned_values = defaultdict(float)
        affected = set()
        for model, key in [(ChatMessage, 'orphan_chats'), (GiftMessage, 'orphan_gifts')]:
            orphans = db.query(model).filter(model.live_session_id.is_(None)).yield_per(1000)
            for msg in orphans:
                matched = next((live for live in by_room.get(msg.live_id, [])
                    if live.start_time <= msg.created_at <= (live.end_time or now) + timedelta(seconds=5)), None)
                if matched is None:
                    continue
                counts[key] += 1
                affected.add(matched.id)
                if model is GiftMessage:
                    extra_income[matched.id] += float(msg.total_value or 0)
                    extra_gifts[matched.id] += int(msg.gift_count or 0)
                    assigned_values[(msg.live_id, msg.user_id)] += float(msg.total_value or 0)
                else:
                    extra_chats[matched.id] += 1
                changes.append(('orphan', model, msg.id, matched.id))

        # autoflush=False lets both preview and apply calculate the same plan.
        for sid in affected:
            live = by_id[sid]
            # Live handlers persist detail before incrementing aggregates. Their
            # intermediate state must never be recounted by historical repair.
            if live.status != 'ended' or live.start_time < cutoff:
                continue
            income, gifts = db.query(func.coalesce(func.sum(GiftMessage.total_value), 0),
                func.coalesce(func.sum(GiftMessage.gift_count), 0)).filter(GiftMessage.live_session_id == sid).one()
            chats = db.query(func.count(ChatMessage.id)).filter(ChatMessage.live_session_id == sid).scalar() or 0
            totals = (float(income) + extra_income[sid], int(gifts) + extra_gifts[sid], int(chats) + extra_chats[sid])
            if totals != (live.total_income, live.total_gift_count, live.total_chat_count):
                counts['sessions'] += 1
                changes.append(('session', sid, totals, (live.total_income, live.total_gift_count, live.total_chat_count), live.updated_at))

        # Only complete, recent room histories may be reduced, and only by a tiny
        # accounting discrepancy. Archived aggregates are never rewritten downward.
        eligible = [rid for rid, rows in by_room.items() if rows and all(
            live.status == 'ended' and live.start_time >= cutoff for live in rows)]
        if eligible:
            values = {(row.live_id, row.user_id): float(row.income or 0) for row in db.query(
                GiftMessage.live_id, GiftMessage.user_id, func.sum(GiftMessage.total_value).label('income')
            ).join(LiveSession, GiftMessage.live_session_id == LiveSession.id).filter(
                GiftMessage.live_id.in_(eligible), LiveSession.live_id == GiftMessage.live_id,
                GiftMessage.total_value > 0).group_by(GiftMessage.live_id, GiftMessage.user_id).all()}
            for key, amount in assigned_values.items():
                if key[0] in eligible:
                    values[key] = values.get(key, 0) + amount
            for row in db.query(UserContribution).filter(UserContribution.live_id.in_(eligible)).all():
                actual = values.get((row.live_id, row.user_id))
                old = float(row.total_score or 0)
                if actual is None or not math.isfinite(actual) or not math.isfinite(old):
                    continue
                difference = abs(actual - old)
                # More than one diamond is not a rounding discrepancy; leave it
                # for investigation instead of making a destructive correction.
                if 1e-8 < difference <= 1.0:
                    counts['contributions'] += 1
                    changes.append(('contribution', row.id, actual, old, row.updated_at))
        if apply:
            failed_sessions = set()
            counts = dict.fromkeys(counts, 0)
            maximum = func.max if service.engine.dialect.name == 'sqlite' else func.greatest
            for change in changes:
                kind = change[0]
                if kind == 'peak':
                    _, sid, peak = change
                    statement = update(LiveSession).where(LiveSession.id == sid,
                        func.coalesce(LiveSession.peak_viewer_count, 0) < peak).values(
                        peak_viewer_count=maximum(func.coalesce(LiveSession.peak_viewer_count, 0), peak),
                        updated_at=LiveSession.updated_at)
                    key = 'peaks'
                elif kind == 'orphan':
                    _, model, msg_id, sid = change
                    statement = update(model).where(model.id == msg_id, model.live_session_id.is_(None)).values(live_session_id=sid)
                    key = 'orphan_chats' if model is ChatMessage else 'orphan_gifts'
                elif kind == 'session':
                    _, sid, totals, previous, updated_at = change
                    if sid in failed_sessions:
                        continue
                    statement = update(LiveSession).where(LiveSession.id == sid,
                        LiveSession.total_income == previous[0], LiveSession.total_gift_count == previous[1],
                        LiveSession.total_chat_count == previous[2], LiveSession.updated_at == updated_at).values(
                        total_income=totals[0], total_gift_count=totals[1], total_chat_count=totals[2],
                        updated_at=LiveSession.updated_at)
                    key = 'sessions'
                else:
                    _, uid, actual, previous, updated_at = change
                    statement = update(UserContribution).where(UserContribution.id == uid,
                        UserContribution.total_score == previous, UserContribution.updated_at == updated_at).values(
                        total_score=actual, updated_at=UserContribution.updated_at)
                    key = 'contributions'
                changed = db.execute(statement.execution_options(synchronize_session=False)).rowcount
                counts[key] += changed
                if kind == 'orphan' and not changed:
                    failed_sessions.add(sid)
            db.commit()
        else:
            db.rollback()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Historical calibration (counts only; preview by default)')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true')
    modes.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    service = None
    try:
        service = DataService()
        service.engine.echo = False
        # DataService errors elsewhere may carry identifiers; this path never
        # prints rows, SQL, exception messages, names, or connection URLs.
        counts = repair_data(service, apply=args.apply)
        print('mode=' + ('apply' if args.apply else 'dry-run'))
        for key, count in counts.items():
            print(f'{key}={count}')
        return 0
    except Exception:
        print('Historical calibration failed', file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close_session()


if __name__ == '__main__':
    raise SystemExit(main())

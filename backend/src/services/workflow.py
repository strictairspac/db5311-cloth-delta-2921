import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import audit, cases, decisions, evidence, runs
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = _queue(conn, case_id, actor)
        result = found(conn, runs, run_id)
    _wake()
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return {'run_id': run.id, 'token': token, 'payload': found(conn, cases, run.case_id)['payload'],
                'case_id': run.case_id, 'decisions': decisions_for_run(conn, run.case_id)}


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)


def conflicts(conn, evidence_id):
    row = found(conn, evidence, evidence_id)
    conflict = (row['report'] or {}).get('conflict')
    if not conflict:
        raise HTTPException(409, '该证据没有待裁决的版本冲突')
    return row, conflict


def decision_history(conn, case_id):
    statement = select(decisions).where(decisions.c.case_id == case_id).order_by(decisions.c.line)
    return [dict(row) for row in conn.execute(statement).mappings()]


def decisions_for_run(conn, case_id):
    """Ordered choices handed to solve(); one entry per decided receipt line."""
    rows = conn.execute(select(decisions).where(decisions.c.case_id == case_id)
                        .order_by(decisions.c.line)).mappings()
    return {row['line']: dict(row) for row in rows}


def decide(evidence_id, choice, actor):
    """Record one reviewer choice and queue a single recomputation.

    The unique (case_id, line) key in PostgreSQL is the arbiter: two reviewers
    choosing differently at the same time both pass the read check, then one
    INSERT wins and the other is reported as already decided. Retrying the same
    choice returns the same decision and never enqueues a second run.
    """
    try:
        choice = domain.choice_of(choice)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        evidence_row, conflict = conflicts(conn, evidence_id)
        line = conflict['流水行']
        case_id = found(conn, runs, evidence_row['run_id'])['case_id']
        label = domain.CHOICE_LABELS[choice]
        stored = conflict['readings'][domain.CHOICE_LABELS['stored']]
        incoming = conflict['readings'][domain.CHOICE_LABELS['incoming']]
        digest = conflict['digests'][label]

        existing = conn.execute(select(decisions).where(
            decisions.c.case_id == case_id, decisions.c.line == line)).mappings().first()
        if existing is not None:
            if existing['choice'] == choice:
                return _decision_result(conn, dict(existing), replay=True)
            raise HTTPException(409, f"第 {line} 行已由 {existing['actor']} 裁决为"
                                     f"{domain.CHOICE_LABELS[existing['choice']]}")

        payload = found(conn, cases, case_id)['payload']
        if line > len(payload['revisions']) or domain.reading(payload['revisions'][line - 1]) != incoming:
            raise HTTPException(409, '档案内容已变化，请按当前冲突证据重新裁决')
        try:
            pending = domain.next_conflict(payload, decisions_for_run(conn, case_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if pending is None or pending['line'] != line or pending['stored'] != stored:
            raise HTTPException(409, '该冲突不是当前待裁决的一处，请刷新后按最新冲突证据操作')
        try:
            with conn.begin_nested():
                inserted = conn.execute(decisions.insert().values(
                    case_id=case_id, evidence_id=evidence_id, line=line, batch_id=conflict['批次'],
                    revision=conflict['修订号'], choice=choice, stored_reading=stored,
                    incoming_reading=incoming, chosen_digest=digest, actor=actor,
                    created_at=utc_time())).inserted_primary_key[0]
        except IntegrityError:
            stored_row = conn.execute(select(decisions).where(
                decisions.c.case_id == case_id, decisions.c.line == line)).mappings().first()
            if stored_row is not None and stored_row['choice'] == choice:
                return _decision_result(conn, dict(stored_row), replay=True)
            winner = stored_row['actor'] if stored_row is not None else '另一名复核员'
            raise HTTPException(409, f'第 {line} 行已由 {winner} 裁决，请以先落库的决定为准') from None
        log(conn, actor, 'decide', f'{case_id}:{line}')
        run_id = _queue(conn, case_id, actor)
        conn.execute(decisions.update().where(decisions.c.id == inserted).values(run_id=run_id))
        result = _decision_result(conn, found(conn, decisions, inserted), replay=False)
    _wake()
    return result


def _queue(conn, case_id, actor):
    """Insert exactly one durable recompute run; callers own the transaction."""
    run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                          owner=actor, created_at=utc_time())).inserted_primary_key[0]
    log(conn, actor, 'enqueue', run_id)
    return run_id


def _decision_result(conn, row, replay):
    result = {'decision': row, 'replay': replay, 'run_id': row.get('run_id')}
    if row.get('run_id'):
        result['run'] = found(conn, runs, row['run_id'])
    return result


def _wake():
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass

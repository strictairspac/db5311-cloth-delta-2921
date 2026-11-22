from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
# One reviewer decision per conflicting receipt line. The unique key is the
# database-level guard that makes concurrent, different choices first-write-wins.
decisions = Table('conflict_decisions', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False),
    Column('line', Integer, nullable=False),
    Column('batch_id', String(64), nullable=False),
    Column('revision', Integer, nullable=False),
    Column('choice', String(20), nullable=False),
    Column('stored_reading', JSON, nullable=False),
    Column('incoming_reading', JSON, nullable=False),
    Column('chosen_digest', String(64), nullable=False),
    Column('actor', String(40), nullable=False),
    Column('run_id', ForeignKey('business_runs.id')),
    Column('created_at', String(32), nullable=False),
    UniqueConstraint('case_id', 'line', name='uq_conflict_decision_case_line'))

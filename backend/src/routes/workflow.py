from fastapi import APIRouter, Depends
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, decisions, evidence, runs
from src.schemas.workflow import CaseForm, DecisionForm, LoginForm, ReviewForm
from src.services import domain, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/evidence/{identity}/conflict')
def evidence_conflict(identity: int, _=Depends(current_user)):
    with engine.connect() as conn:
        row, conflict = workflow.conflicts(conn, identity)
    return {'evidence_id': row['id'], 'run_id': row['run_id'], 'conflict': conflict}


@router.post('/evidence/{identity}/decisions', status_code=201)
def decide_conflict(identity: int, form: DecisionForm, actor=Depends(reviewer)):
    return workflow.decide(identity, form.choice, actor)


@router.get('/decisions')
def list_decisions(_=Depends(current_user)):
    return workflow.recent(decisions)


@router.get('/evidence/{identity}/decisions')
def evidence_decisions(identity: int, _=Depends(current_user)):
    with engine.connect() as conn:
        row = workflow.found(conn, evidence, identity)
        case_id = workflow.found(conn, runs, row['run_id'])['case_id']
        return workflow.decision_history(conn, case_id)


@router.get('/cases/{case_id}/decisions')
def case_decisions(case_id: int, _=Depends(current_user)):
    with engine.connect() as conn:
        workflow.found(conn, cases, case_id)
        return workflow.decision_history(conn, case_id)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)

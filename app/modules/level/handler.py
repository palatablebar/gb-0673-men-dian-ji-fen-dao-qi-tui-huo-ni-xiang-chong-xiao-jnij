from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store
from app.errors import BusinessError

router = APIRouter()

@router.get('/level')
def page(request: Request):
    return render(request, 'level')

@router.post('/api/level')
def calculate(payload: dict = Body(default={})):
    from app.main import DB_PATH
    return store.save(DB_PATH, evaluate(payload, until=8))

@router.post('/api/level/publish')
def publish(request: Request, payload: dict = Body(default={})):
    from app.main import DB_PATH
    run_id = payload.get('run_id')
    if not run_id:
        raise BusinessError('BAD_RUN_ID', '发布等级须指定 run_id')
    state = store.get(DB_PATH, run_id)
    if 'level' not in state:
        state = evaluate({'dataset': state['source']}, until=8,
                         snapshot=state.get('input_snapshot'))
    return store.publish_tiers(DB_PATH, state)

@router.get('/api/level/history')
def history(member: str | None = None):
    from app.main import DB_PATH
    return store.tier_history(DB_PATH, member)

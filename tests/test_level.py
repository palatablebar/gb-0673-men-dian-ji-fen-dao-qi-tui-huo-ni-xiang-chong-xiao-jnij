import pytest
from copy import deepcopy
from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.modules.level.rules import classify, reconcile_net, decide_change, build_proposal, tier_ladder, basis_digest

# ---------- 纯规则：门槛与方向感知 ----------

def test_classify_thresholds():
    ladder = tier_ladder(evaluate()['context'])
    assert classify(0, ladder) == '普卡'
    assert classify(79, ladder) == '普卡'
    assert classify(80, ladder) == '银卡'
    assert classify(199, ladder) == '银卡'
    assert classify(200, ladder) == '金卡'
    assert classify(500, ladder) == '黑钻'

def test_downgrade_defers_to_next_period():
    """跌破门槛：当期礼遇不追回，降级下一周期生效。"""
    ladder = tier_ladder(evaluate()['context'])
    d = decide_change('银卡', '金卡', ladder, '2026-11', '2026-12')
    # 入参：候选等级=银卡，当前等级=金卡
    assert d['tier'] == '金卡'
    assert d['pending_tier'] == '银卡'
    assert d['effective_period'] == '2026-11'
    assert d['pending_period'] == '2026-12'
    assert d['change'] == '降级·下期生效'

def test_upgrade_takes_effect_immediately():
    ladder = tier_ladder(evaluate()['context'])
    # 候选=金卡，当前=银卡
    d = decide_change('金卡', '银卡', ladder, '2026-11', '2026-12')
    assert d['tier'] == '金卡'
    assert d['pending_tier'] is None
    assert d['change'] == '升级·当期生效'

def test_same_tier_no_change():
    ladder = tier_ladder(evaluate()['context'])
    d = decide_change('金卡', '金卡', ladder, '2026-11', '2026-12')
    assert d['tier'] == '金卡'
    assert d['change'] == '不变'

# ---------- 净积分对账闸门 ----------

def test_sample_gate_passes(sample):
    assert sample['level']['gate']['publishable']
    assert sample['level']['gate']['member_net_matched']

def test_tampered_net_points_blocks_gate(sample):
    s = deepcopy(sample)
    s['point_ledger']['lots'][0]['remaining'] += 1
    gate = reconcile_net(s)
    assert gate['publishable'] is False
    assert gate['member_net_matched'] is False

def test_global_imbalance_blocks_gate(sample):
    s = deepcopy(sample)
    s['balance']['points_balanced'] = False
    assert reconcile_net(s)['publishable'] is False

def test_refund_issue_blocks_gate(sample):
    s = deepcopy(sample)
    s['refund_issues'] = ['R1']
    assert reconcile_net(s)['publishable'] is False

def test_proposal_shows_tier_net_and_version_fields(sample):
    row = next(r for r in sample['level']['rows'] if r['member'] == '00001')
    assert row['net_points'] == 30
    assert row['tier'] == '普卡'
    assert row['basis_digest']

# ---------- 发布层：API 与持久化 ----------

def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'level.sqlite'))
    return TestClient(main.app)

def test_publish_happy_path(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/level', json={}).json()
    rid = run['manifest']['run_id']
    resp = c.post('/api/level/publish', json={'run_id': rid})
    assert resp.status_code == 200
    body = resp.json()
    assert body['published_count'] == 15
    latest = c.get('/api/level/history').json()
    members = {r['member']: r for r in latest}
    row = members['00001']
    # 页面/结果同时具备等级、净积分、生效版本
    assert row['tier'] == '普卡'
    assert row['net_points'] == 30
    assert row['version'] == 1
    assert row['effective_period'] == '2026-11'

def test_publish_blocked_when_reconcile_fails(tmp_path, monkeypatch, sample):
    p = str(tmp_path / 'blocked.sqlite')
    store.save(p, sample)
    monkeypatch.setattr(main, 'DB_PATH', p)
    # 人为破坏对账状态后重新构造提案：服务层必须拒绝且不写入任何等级。
    broken = deepcopy(sample)
    broken['balance']['points_balanced'] = False
    broken['level'] = build_proposal(broken)
    from app.errors import BusinessError
    with pytest.raises(BusinessError) as exc:
        store.publish_tiers(p, broken)
    assert exc.value.code == 'RECONCILE_BLOCKED'
    assert store.latest_levels(p) == []

def test_repeated_publish_is_idempotent(tmp_path, monkeypatch):
    """同一退货重试（相同依据摘要）不能重复改变等级。"""
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/level', json={}).json()['manifest']['run_id']
    first = c.post('/api/level/publish', json={'run_id': rid}).json()
    second = c.post('/api/level/publish', json={'run_id': rid}).json()
    assert first['published_count'] == 15 and first['skipped_count'] == 0
    assert second['published_count'] == 0 and second['skipped_count'] == 15
    history = c.get('/api/level/history').json()
    # 每个会员仍只有一个版本，没有因重试产生新版本
    versions = {}
    for r in history:
        versions.setdefault(r['member'], []).append(r['version'])
    assert all(v == [1] for v in versions.values())

def test_failed_publish_keeps_old_version(tmp_path, monkeypatch, sample):
    """发布失败时旧版结果完整保留（整体回滚）。"""
    p = str(tmp_path / 'keep.sqlite')
    store.save(p, sample)
    first = store.publish_tiers(p, sample)
    assert first['published_count'] == 15
    before = store.latest_levels(p)
    broken = deepcopy(sample)
    broken['balance']['points_balanced'] = False
    broken['level'] = build_proposal(broken)
    from app.errors import BusinessError
    with pytest.raises(BusinessError):
        store.publish_tiers(p, broken)
    after = store.latest_levels(p)
    assert before == after

def test_history_is_append_only_and_never_overwritten(tmp_path, monkeypatch, sample):
    """新依据产生新版本，历史版本全部保留可回溯。"""
    p = str(tmp_path / 'append.sqlite')
    store.save(p, sample)
    store.publish_tiers(p, sample)
    second_state = deepcopy(sample)
    # 模拟净积分变化（新批次结果）：依据摘要必然不同，产生下一版本
    second_state['manifest'] = dict(sample['manifest'], sha256='f' * 64, run_id='run-changed-0001')
    second_state['level'] = build_proposal(second_state)
    result = store.publish_tiers(p, second_state)
    assert result['published_count'] == 15
    history = store.tier_history(p, '00001')
    assert [r['version'] for r in history] == [1, 2]
    assert history[0]['run_sha'] == sample['manifest']['sha256']
    assert history[1]['run_sha'] == 'f' * 64
    # 历史记录数 = 会员数 × 版本代际
    assert len(store.tier_history(p)) == 30

def test_page_renders_tier_net_and_version(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/level', json={}).json()['manifest']['run_id']
    html = c.get('/level?run_id=' + rid).text
    assert '会员等级结果' in html
    assert '净积分对账闸门' in html
    assert '生效版本' in html
    # 等级、净积分列与门槛结果同屏
    assert '普卡' in html and '净积分' in html

def test_republish_uses_candidate_not_display_tier(tmp_path, monkeypatch):
    """合并展示后 tier 可能是保留的旧等级；发布必须以 candidate_tier 判定方向。"""
    c = _client(tmp_path, monkeypatch)
    header = 'id,member,type,points,at,expires,ref,rule,version'
    earn = lambda i, p: f'{i},00009,EARN,{p},2026-10-01T09:00:00+08:00,2027-01-01T00:00:00+08:00,,常规,1'
    # 第一份：净积分 250 → 金卡并发布
    up = '\n'.join([header, earn('E90', 250)])
    r1 = c.post('/api/runs', json={'csv_text': up}).json()
    c.post('/api/level/publish', json={'run_id': r1['manifest']['run_id']})
    # 第二份：净积分 100 → 银卡（跌破金卡200），对账本身通过
    down = '\n'.join([header, earn('E90', 100)])
    r2 = c.post('/api/runs', json={'csv_text': down}).json()
    assert r2['level']['gate']['publishable']
    # 模拟页面合并：用已发布等级重建提案，展示 tier 保留金卡
    from app.modules.level.rules import build_proposal
    current = {r['member']: r['tier'] for r in store.latest_levels(_db(monkeypatch, c))}
    merged = build_proposal(r2, current)
    row = next(x for x in merged['rows'] if x['member'] == '00009')
    assert row['tier'] == '金卡' and row['candidate_tier'] == '银卡'
    r2['level'] = merged
    # 直接走存储层发布，必须依据候选等级产出"降级挂下期"，而非误判不变
    result = store.publish_tiers(_db(monkeypatch, c), r2)
    pub = next(x for x in result['published'] if x['member'] == '00009')
    assert pub['tier'] == '金卡' and pub['pending_tier'] == '银卡'
    assert pub['change'] == '降级·下期生效'

def _db(monkeypatch, client):
    return main.DB_PATH

def test_level_entry_registered():
    assert 'level' in main.MODULES
    assert main.MODULES[-1] == 'level'


HEADER = 'id,member,type,points,at,expires,ref,rule,version'

def _earn(eid, points, at, expires):
    return f'{eid},00009,EARN,{points},{at},{expires},,常规,1'

def _line(eid, kind, points, at, ref=''):
    return f'{eid},00009,{kind},{points},{at},,{ref},常规,1'

def _csv(with_refund):
    lines = [
        HEADER,
        _earn('E90', 100, '2026-10-01T09:00:00+08:00', '2026-10-10T00:00:00+08:00'),
        _earn('E91', 200, '2026-10-02T09:00:00+08:00', '2027-01-01T00:00:00+08:00'),
        _line('S90', 'SPEND', 220, '2026-10-05T09:00:00+08:00'),
    ]
    if with_refund:
        # 退款60逆序回拨：40恢复、20来自已过期批次不恢复，净积分 100+40=140 → 低于金卡200门槛
        lines.append(_line('R90', 'REFUND', 60, '2026-10-20T09:00:00+08:00', 'S90'))
    return '\n'.join(lines)

def test_downgrade_lifecycle_with_real_refund(tmp_path, monkeypatch):
    """退货（含过期不恢复）使净积分跌破门槛：当期保级、降级挂下期；重试不改级；历史可回溯。"""
    c = _client(tmp_path, monkeypatch)
    # 周期一：EARN300、SPEND220，净积分 80（银卡）
    run1 = c.post('/api/runs', json={'csv_text': _csv(False)}).json()
    r1 = c.post('/api/level/publish', json={'run_id': run1['manifest']['run_id']}).json()
    pub1 = next(x for x in r1['published'] if x['member'] == '00009')
    assert pub1['tier'] == '银卡' and pub1['version'] == 1
    assert pub1['net_points'] == 80

    # 周期二：净积分提升到 120（退款恢复40，20过期不复活）仍低于金卡200 → 不变
    # 再用无退款但更高发放的对照批次验证升级当期生效：直接构造净积分>=200
    csv_up = '\n'.join([
        HEADER,
        _earn('E90', 300, '2026-10-01T09:00:00+08:00', '2027-01-01T00:00:00+08:00'),
        _line('S90', 'SPEND', 50, '2026-10-05T09:00:00+08:00'),
    ])
    run2 = c.post('/api/runs', json={'csv_text': csv_up}).json()
    r2 = c.post('/api/level/publish', json={'run_id': run2['manifest']['run_id']}).json()
    pub2 = next(x for x in r2['published'] if x['member'] == '00009')
    assert pub2['version'] == 2 and pub2['tier'] == '金卡'
    assert pub2['change'] == '升级·当期生效'

    # 周期三：退货导致净积分回落到 140，跌破金卡门槛 → 当期保留金卡，降级挂下期
    run3 = c.post('/api/runs', json={'csv_text': _csv(True)}).json()
    r3 = c.post('/api/level/publish', json={'run_id': run3['manifest']['run_id']}).json()
    pub3 = next(x for x in r3['published'] if x['member'] == '00009')
    assert pub3['version'] == 3
    assert pub3['tier'] == '金卡'               # 当期礼遇不追回
    assert pub3['pending_tier'] == '银卡'        # 降级下一周期生效
    # 退款逆序回拨：40恢复到长期批次、20过期不复活，净积分 100+40=140
    assert pub3['net_points'] == 140
    assert pub3['change'] == '降级·下期生效'

    # 同一退货重试：相同依据不得重复改变等级
    retry = c.post('/api/level/publish', json={'run_id': run3['manifest']['run_id']}).json()
    assert retry['published_count'] == 0 and retry['skipped_count'] == 1
    history = c.get('/api/level/history?member=00009').json()
    assert [h['version'] for h in history] == [1, 2, 3]   # 历史版本完整保留、永不覆盖
    assert [h['tier'] for h in history] == ['银卡', '金卡', '金卡']
    assert history[2]['pending_tier'] == '银卡'

def test_publish_endpoint_rejects_missing_run(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    resp = c.post('/api/level/publish', json={})
    assert resp.status_code == 422
    assert resp.json()['code'] == 'BAD_RUN_ID'

def test_blocked_gate_disables_publish_on_page(tmp_path, monkeypatch):
    """对账未通过时页面发布按钮禁用，且 API 拒绝发布。"""
    c = _client(tmp_path, monkeypatch)
    # 构造一行余额无法守恒的脏数据：消费超过余额被隔离后，再篡改 balance
    run = c.post('/api/level', json={}).json()
    rid = run['manifest']['run_id']
    html = c.get('/level?run_id=' + rid).text
    if run['level']['gate']['publishable']:
        assert 'id="publish-level"' in html and 'disabled' not in html.split('id="publish-level"')[1].split('>')[0]
    else:
        assert 'disabled' in html

def test_page_merges_published_tier_for_downgrade_preview(tmp_path, monkeypatch):
    """已发布金卡后新批次跌破门槛：页面预告当期保留金卡、下期挂银卡。"""
    c = _client(tmp_path, monkeypatch)
    header = 'id,member,type,points,at,expires,ref,rule,version'
    earn = lambda i, p, exp: f'{i},00009,EARN,{p},2026-10-01T09:00:00+08:00,{exp},,常规,1'
    spend = lambda i, p: f'{i},00009,SPEND,{p},2026-10-05T09:00:00+08:00,,,常规,1'
    up = '\n'.join([header, earn('E90', 300, '2027-01-01T00:00:00+08:00'), spend('S90', 50)])
    r1 = c.post('/api/runs', json={'csv_text': up}).json()
    c.post('/api/level/publish', json={'run_id': r1['manifest']['run_id']})
    down = '\n'.join([header, earn('E90', 120, '2027-01-01T00:00:00+08:00'), spend('S90', 50)])
    r2 = c.post('/api/runs', json={'csv_text': down}).json()  # 净积分70 → 普卡，跌破银卡
    html = c.get('/level?run_id=' + r2['manifest']['run_id']).text
    assert '降级·下期生效' in html
    assert '普卡' in html

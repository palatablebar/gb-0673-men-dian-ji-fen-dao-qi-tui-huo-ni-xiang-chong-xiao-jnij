import hashlib
from app.config import RULE_VERSION
from app.errors import BusinessError

DEFAULT_TIER = '普卡'

def tier_ladder(context):
    tiers = context.get('tiers')
    if not isinstance(tiers, list) or not tiers:
        raise BusinessError('BAD_TIER_CONFIG', '等级门槛配置缺失')
    ladder = []
    for item in tiers:
        if not isinstance(item, dict) or 'tier' not in item or 'min_points' not in item:
            raise BusinessError('BAD_TIER_CONFIG', '等级门槛须含 tier 与 min_points')
        points = item['min_points']
        if not isinstance(points, int) or points < 0:
            raise BusinessError('BAD_TIER_CONFIG', '等级门槛须为非负整数')
        ladder.append((item['tier'], points))
    ladder.sort(key=lambda x: -x[1])
    names = [name for name, _ in ladder]
    if len(set(names)) != len(names) or ladder[-1][1] != 0:
        raise BusinessError('BAD_TIER_CONFIG', '等级名称重复或缺少零门槛兜底等级')
    return ladder

def rank_of(tier, ladder):
    """等级序号：ladder 按门槛降序，索引越小等级越高；未知等级按最低兜底。"""
    names = [name for name, _ in ladder]
    return names.index(tier) if tier in names else len(names) - 1

def classify(net_points, ladder):
    for name, minimum in ladder:
        if net_points >= minimum:
            return name
    return ladder[-1][0]

def net_by_member(state):
    balances = {}
    for lot in state['point_ledger']['lots']:
        balances[lot['member']] = balances.get(lot['member'], 0) + lot['remaining']
    return dict(sorted(balances.items()))
def reconcile_net(state):
    """净积分对账：批次余额、归档余额、总账守恒三处必须对齐，否则等级不得发布。"""
    ledger_map = net_by_member(state)
    archive_map = {row['member']: row['remaining'] for row in state['archive']['balances']}
    member_matched = ledger_map == archive_map
    total_matched = sum(ledger_map.values()) == state['balance']['remaining']
    global_gate = (state['balance']['row_balanced']
                   and state['balance']['points_balanced']
                   and not state['refund_issues'])
    return {
        'member_net_matched': member_matched,
        'net_total_matched': total_matched,
        'global_gate_passed': global_gate,
        'publishable': bool(member_matched and total_matched and global_gate),
    }

def basis_digest(run_sha, member, net_points, tier):
    raw = '|'.join([run_sha, member, str(net_points), tier, RULE_VERSION])
    return hashlib.sha256(raw.encode()).hexdigest()

def decide_change(candidate, current_tier, ladder, current_period, next_period):
    """方向感知：升级当期立即生效；降级保留下一期生效，当期已确认礼遇不追回。"""
    candidate_rank = rank_of(candidate, ladder)
    current_rank = rank_of(current_tier, ladder)
    if candidate_rank > current_rank:
        return {'tier': current_tier, 'candidate_tier': candidate,
                'pending_tier': candidate, 'change': '降级·下期生效',
                'effective_period': current_period, 'pending_period': next_period}
    if candidate_rank < current_rank:
        return {'tier': candidate, 'candidate_tier': candidate,
                'pending_tier': None, 'change': '升级·当期生效',
                'effective_period': current_period, 'pending_period': None}
    return {'tier': candidate, 'candidate_tier': candidate,
            'pending_tier': None, 'change': '不变',
            'effective_period': current_period, 'pending_period': None}

def build_proposal(state, current_levels=None):
    ladder = tier_ladder(state['context'])
    gate = reconcile_net(state)
    run_sha = state['manifest']['sha256']
    current_levels = current_levels or {}
    period, next_period = (state['context'].get('level_period', ''),
                           state['context'].get('next_period', ''))
    rows = []
    for member, net_points in net_by_member(state).items():
        candidate = classify(net_points, ladder)
        current_tier = current_levels.get(member, DEFAULT_TIER)
        decision = decide_change(candidate, current_tier, ladder, period, next_period)
        rows.append({'member': member, 'net_points': net_points,
                     'tier': decision['tier'],
                     'candidate_tier': candidate,
                     'pending_tier': decision['pending_tier'],
                     'change': decision['change'] if current_levels else
                               ('待发布' if gate['publishable'] else '对账未通过·禁止发布'),
                     'basis_digest': basis_digest(run_sha, member, net_points, candidate)})
    return {'ladder': ladder, 'gate': gate, 'rows': rows,
            'period': period, 'next_period': next_period}

def apply(state):
    proposal = build_proposal(state)
    state['level'] = proposal
    state['views']['level'] = {
        'title': '会员等级发布',
        'rows': proposal['rows'],
        'metrics': {
            '会员数': len(proposal['rows']),
            '净积分合计': sum(r['net_points'] for r in proposal['rows']),
            '对账闸门': '通过' if proposal['gate']['publishable'] else '未通过',
        },
    }

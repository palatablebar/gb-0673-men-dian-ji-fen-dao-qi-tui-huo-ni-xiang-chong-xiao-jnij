import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS tier_versions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        member TEXT NOT NULL,
        version INTEGER NOT NULL,
        tier TEXT NOT NULL,
        pending_tier TEXT,
        net_points INTEGER NOT NULL,
        change TEXT NOT NULL,
        effective_period TEXT NOT NULL,
        pending_period TEXT,
        run_id TEXT NOT NULL,
        run_sha TEXT NOT NULL,
        basis_digest TEXT NOT NULL UNIQUE,
        published_at TEXT NOT NULL,
        UNIQUE(member, version)
    )""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _latest_rows(con):
    return con.execute("""SELECT t.* FROM tier_versions t
        JOIN (SELECT member, MAX(seq) AS max_seq FROM tier_versions GROUP BY member) m
        ON t.member=m.member AND t.seq=m.max_seq""").fetchall()

def latest_levels(path):
    with connect(path) as con:
        return [dict(r) for r in _latest_rows(con)]

def tier_history(path, member=None):
    with connect(path) as con:
        if member is None:
            rows=con.execute('SELECT * FROM tier_versions ORDER BY member, seq').fetchall()
        else:
            rows=con.execute('SELECT * FROM tier_versions WHERE member=? ORDER BY seq',(member,)).fetchall()
        if member and not rows:
            raise BusinessError('NOT_FOUND','该会员暂无等级历史',404)
        return [dict(r) for r in rows]

def publish_tiers(path, state):
    """发布等级：对账闸门通过才允许写入；仅追加，不覆盖；同依据摘要幂等。

    任何失败整体回滚，已发布的旧版本原样保留。
    """
    from datetime import datetime, timezone
    from app.modules.level.rules import decide_change, DEFAULT_TIER
    proposal = state.get('level')
    if proposal is None:
        raise BusinessError('NO_LEVEL_PROPOSAL','等级尚未重算')
    gate = proposal['gate']
    if not gate['publishable']:
        raise BusinessError('RECONCILE_BLOCKED','净积分对账未通过，等级禁止发布', 409)
    ladder = proposal['ladder']
    period, next_period = proposal['period'], proposal['next_period']
    run_id, run_sha = state['manifest']['run_id'], state['manifest']['sha256']
    now = datetime.now(timezone.utc).isoformat()
    published, skipped = [], []
    with connect(path) as con:
        current = {r['member']: dict(r) for r in _latest_rows(con)}
        for row in proposal['rows']:
            member = row['member']
            dup = con.execute('SELECT 1 FROM tier_versions WHERE basis_digest=?',(row['basis_digest'],)).fetchone()
            if dup:
                # 同一退货重试产生相同依据：不重复改变等级，直接回显已发布版本。
                old = con.execute('SELECT * FROM tier_versions WHERE basis_digest=?',(row['basis_digest'],)).fetchone()
                skipped.append(dict(old))
                continue
            old_row = current.get(member)
            current_tier = old_row['tier'] if old_row else DEFAULT_TIER
            # 以候选等级参与方向判定；row['tier'] 可能是页面合并后的当期保留等级。
            candidate = row.get('candidate_tier', row['tier'])
            decision = decide_change(candidate, current_tier, ladder, period, next_period)
            next_version = (old_row['version'] + 1) if old_row else 1
            record = {'member': member, 'version': next_version,
                      'tier': decision['tier'], 'pending_tier': decision['pending_tier'],
                      'net_points': row['net_points'], 'change': decision['change'],
                      'effective_period': decision['effective_period'],
                      'pending_period': decision['pending_period'],
                      'run_id': run_id, 'run_sha': run_sha,
                      'basis_digest': row['basis_digest'], 'published_at': now}
            try:
                con.execute("""INSERT INTO tier_versions
                    (member,version,tier,pending_tier,net_points,change,effective_period,pending_period,
                     run_id,run_sha,basis_digest,published_at)
                    VALUES (:member,:version,:tier,:pending_tier,:net_points,:change,:effective_period,
                            :pending_period,:run_id,:run_sha,:basis_digest,:published_at)""", record)
            except sqlite3.IntegrityError:
                # 并发发布撞版本号或依据摘要：整批回滚，旧版结果保留。
                raise BusinessError('LEVEL_PUBLISH_CONFLICT','等级发布冲突，旧版结果已保留，请刷新后重试', 409)
            published.append(record)
    return {'published': published, 'skipped': skipped,
            'published_count': len(published), 'skipped_count': len(skipped)}

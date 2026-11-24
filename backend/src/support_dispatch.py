from fastapi import HTTPException

from .support_directory import list_desks, lock_counter_rows
from .support_notices import current_edition
from .support_schemas import Category, Desk

# Accessibility cases may only go to desks that can arrange step-free assistance.
ROUTES = {
    Category.STEP_FREE: (Desk.ACCESS, Desk.STATION),
    Category.WAYFINDING: (Desk.STATION, Desk.ACCESS),
    Category.MISSED_CONNECTION: (Desk.DISPATCH, Desk.STATION),
}


def lock_counters(db, plan_id):
    # 容量决策按柜台串行：共用柜台的任一线路派单前都先锁住同一批柜台。
    counter_ids = [row['counter_id'] for row in db.execute(
        'SELECT counter_id FROM support_counter_member WHERE plan_id=%s', (plan_id,)).fetchall()]
    lock_counter_rows(db, counter_ids)


def destination(db, plan_id, category, excluding_case=None):
    desks = {row['desk']: row for row in list_desks(db, plan_id, excluding_case)}
    for index, desk in enumerate(ROUTES[category]):
        entry = desks.get(desk)
        if entry and entry['available'] and entry['active_count'] < entry['capacity']:
            return desk, 'primary' if index == 0 else 'fallback'
    return None, 'waiting'


def counter_for_desk(db, plan_id, desk):
    row = db.execute('SELECT counter_id FROM support_counter_member WHERE plan_id=%s AND desk=%s',
                     (plan_id, desk)).fetchone()
    return row['counter_id'] if row else None


def promote_waiting(db, counter_id, actor):
    # 柜台腾出名额时，按登记先后补派该柜台能接的待派单；每次调用前调用方已持有柜台锁。
    counter = db.execute('SELECT * FROM support_counter WHERE id=%s FOR UPDATE', (counter_id,)).fetchone()
    if not counter or not counter['available']:
        return
    desk = counter['desk']
    categories = [category for category, route in ROUTES.items() if desk in route]
    while True:
        load = db.execute('''SELECT count(*)::int AS n FROM support_case c
                             JOIN support_counter_member m ON m.plan_id = c.plan_id AND m.desk = c.desk
                             WHERE m.counter_id = %s AND c.resolved_at IS NULL''',
                          (counter_id,)).fetchone()['n']
        if load >= counter['capacity']:
            return
        case = db.execute('''SELECT c.* FROM support_case c
                             JOIN support_counter_member m ON m.plan_id = c.plan_id AND m.counter_id = %s
                             WHERE c.resolved_at IS NULL AND c.desk IS NULL AND c.category = ANY(%s)
                             ORDER BY c.opened_at, c.id
                             LIMIT 1 FOR UPDATE OF c SKIP LOCKED''',
                          (counter_id, categories)).fetchone()
        if not case:
            return
        reason = 'primary' if ROUTES[case['category']][0] == desk else 'fallback'
        changed = db.execute('''UPDATE support_case SET desk=%s, route_reason=%s
                                WHERE plan_id=%s AND id=%s AND desk IS NULL RETURNING *''',
                             (desk, reason, case['plan_id'], case['id'])).fetchone()
        if not changed:
            return
        log_event(db, changed, 'redispatched', actor)


def board_cases(db, plan_id):
    cases = db.execute('''SELECT * FROM support_case WHERE plan_id=%s
                         ORDER BY (resolved_at IS NOT NULL),
                                  (desk IS NOT NULL), opened_at, id''', (plan_id,)).fetchall()
    positions = waiting_positions(db, plan_id, cases)
    for case in cases:
        case['queue_position'] = positions.get(case['id'])
    return cases


def waiting_positions(db, plan_id, cases):
    # 待派单在共享队列中的顺位：排在前面的、且能由同一批柜台接待的同组待派单数量 + 1。
    waiting = [case for case in cases if not case['desk'] and not case['resolved_at']]
    if not waiting:
        return {}
    counters = {row['desk']: row for row in list_desks(db, plan_id)}
    desk_of = {row['counter_id']: row['desk'] for row in counters.values()}
    queued = db.execute('''SELECT c.plan_id, c.id, c.category, c.opened_at, m.counter_id
                           FROM support_case c
                           JOIN support_counter_member m ON m.plan_id = c.plan_id
                           WHERE c.desk IS NULL AND c.resolved_at IS NULL
                             AND m.counter_id IN (SELECT counter_id FROM support_counter_member
                                                  WHERE plan_id = %s)''', (plan_id,)).fetchall()
    positions = {}
    for case in waiting:
        eligible = {counters[desk]['counter_id'] for desk in ROUTES[case['category']] if desk in counters}
        ahead = {(row['plan_id'], row['id']) for row in queued
                 if row['counter_id'] in eligible and desk_of[row['counter_id']] in ROUTES[row['category']]
                 and (row['opened_at'], row['id']) < (case['opened_at'], case['id'])}
        positions[case['id']] = len(ahead) + 1
    return positions


def timeline(db, plan_id, case_id):
    get_case(db, plan_id, case_id)
    return db.execute('''SELECT * FROM support_case_event WHERE plan_id=%s AND case_id=%s ORDER BY id''',
                      (plan_id, case_id)).fetchall()


def get_case(db, plan_id, case_id):
    case = db.execute('SELECT * FROM support_case WHERE plan_id=%s AND id=%s', (plan_id, case_id)).fetchone()
    if not case:
        raise HTTPException(404, '找不到该线路的乘客协助单')
    return case


def require_open(case):
    if case['resolved_at']:
        raise HTTPException(409, '已办结的协助单已锁定')


def log_event(db, case, action, actor):
    db.execute('''INSERT INTO support_case_event (plan_id,case_id,action,desk,route_reason,actor)
                  VALUES (%s,%s,%s,%s,%s,%s)''',
               (case['plan_id'], case['id'], action, case['desk'], case['route_reason'], actor))
    return case


def open_case(db, plan_id, data, actor):
    edition = current_edition(db, plan_id)
    if not edition:
        raise HTTPException(409, '请先发布现场指引')
    if data.notice_edition != edition:
        raise HTTPException(409, '现场指引已更新，请重新打开工作台后登记')
    lock_counters(db, plan_id)
    desk, reason = destination(db, plan_id, data.category)
    case = db.execute('''INSERT INTO support_case (plan_id,notice_edition,category,landmark,description,
                          desk,route_reason,opened_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                      (plan_id, edition, data.category, data.landmark, data.description, desk, reason, actor)).fetchone()
    return log_event(db, case, 'opened', actor)


def redispatch_case(db, plan_id, case_id, actor):
    case = get_case(db, plan_id, case_id)
    require_open(case)
    lock_counters(db, plan_id)
    desk, reason = destination(db, plan_id, case['category'], excluding_case=case_id)
    if desk == case['desk']:
        return case
    old_counter = counter_for_desk(db, plan_id, case['desk']) if case['desk'] else None
    changed = db.execute('''UPDATE support_case SET desk=%s,route_reason=%s WHERE plan_id=%s AND id=%s RETURNING *''',
                         (desk, reason, plan_id, case_id)).fetchone()
    log_event(db, changed, 'redispatched', actor)
    if old_counter:
        promote_waiting(db, old_counter, actor)
    return changed


def resolve_case(db, plan_id, case_id, note, actor):
    case = get_case(db, plan_id, case_id)
    require_open(case)
    if not case['desk']:
        raise HTTPException(409, '待派单须先分派到服务点')
    lock_counters(db, plan_id)
    changed = db.execute('''UPDATE support_case SET resolution=%s,resolved_by=%s,resolved_at=now()
                            WHERE plan_id=%s AND id=%s RETURNING *''', (note, actor, plan_id, case_id)).fetchone()
    log_event(db, changed, 'resolved', actor)
    # 办结腾出的名额立即补派给等待最久且该柜台能接的单；重试办结会在上面被 409 挡住，不会重复补派。
    promote_waiting(db, counter_for_desk(db, plan_id, case['desk']), actor)
    return changed

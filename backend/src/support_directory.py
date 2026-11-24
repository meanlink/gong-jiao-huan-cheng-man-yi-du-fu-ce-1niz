from fastapi import HTTPException

from .db import record
from .plans import local_today
from .support_schemas import Desk


def launch_window(db, plan_id):
    handover = db.execute('SELECT effective_on, released_at FROM route_handover WHERE plan_id=%s',
                          (plan_id,)).fetchone()
    if not handover or not handover['released_at']:
        return {'status': 'awaiting_handover', 'effective_on': None}
    day = handover['effective_on']
    return {'status': 'scheduled' if day > local_today() else 'live', 'effective_on': day}


def require_ready(db, plan_id, *, live=False):
    window = launch_window(db, plan_id)
    if window['status'] == 'awaiting_handover':
        raise HTTPException(409, '请先发布采用路线的交接单')
    if live and window['status'] != 'live':
        raise HTTPException(409, '尚未到交接单的计划启用日')


def list_desks(db, plan_id, excluding_case=None):
    # 在办单数按柜台统计：共用同一柜台的所有线路的在办单都计入同一个负荷。
    return db.execute('''SELECT c.id AS counter_id, c.desk, c.location, c.capacity, c.available,
                                c.updated_by, c.updated_at, count(sc.id)::int AS active_count
                         FROM support_counter_member mine
                         JOIN support_counter c ON c.id = mine.counter_id
                         LEFT JOIN support_counter_member peers ON peers.counter_id = c.id
                         LEFT JOIN support_case sc ON sc.plan_id = peers.plan_id AND sc.desk = c.desk
                              AND sc.resolved_at IS NULL AND sc.id IS DISTINCT FROM %s
                         WHERE mine.plan_id = %s
                         GROUP BY c.id, c.desk, c.location, c.capacity, c.available, c.updated_by, c.updated_at
                         ORDER BY c.desk''', (excluding_case, plan_id)).fetchall()


def counter_members(db, plan_id):
    return db.execute('''SELECT m.counter_id, m.plan_id, p.code, p.hub
                         FROM support_counter_member m JOIN route_plan p ON p.id = m.plan_id
                         WHERE m.counter_id IN (SELECT counter_id FROM support_counter_member
                                                WHERE plan_id = %s)
                         ORDER BY m.counter_id, p.id''', (plan_id,)).fetchall()


def shareable_plans(db):
    return db.execute('''SELECT p.id, p.code, p.hub FROM route_plan p
                         JOIN route_handover h ON h.plan_id = p.id
                         WHERE h.released_at IS NOT NULL ORDER BY p.id''').fetchall()


def save_desk(db, plan_id, desk: Desk, data, actor):
    counter = db.execute('''SELECT c.* FROM support_counter c
                            JOIN support_counter_member m ON m.counter_id = c.id
                            WHERE m.plan_id = %s AND c.desk = %s''', (plan_id, desk)).fetchone()
    if counter:
        row = db.execute('''UPDATE support_counter SET location=%s, capacity=%s, available=%s,
                            updated_by=%s, updated_at=now() WHERE id=%s RETURNING *''',
                         (data.location, data.capacity, data.available, actor, counter['id'])).fetchone()
    else:
        row = db.execute('''INSERT INTO support_counter (desk,location,capacity,available,updated_by)
                            VALUES (%s,%s,%s,%s,%s) RETURNING *''',
                         (desk, data.location, data.capacity, data.available, actor)).fetchone()
        db.execute('INSERT INTO support_counter_member (plan_id,desk,counter_id) VALUES (%s,%s,%s)',
                   (plan_id, desk, row['id']))
    record(db, plan_id, actor, 'support.desk_saved', desk=desk, counter_id=row['id'], **data.model_dump())
    return row


def lock_plans(db, plan_ids):
    # 固定按 id 升序加锁，与派单路径「先线路、后柜台」的顺序一致，避免死锁。
    for plan_id in sorted(plan_ids):
        db.execute('SELECT id FROM route_plan WHERE id=%s FOR UPDATE', (plan_id,))


def lock_counter_rows(db, counter_ids):
    for counter_id in sorted(counter_ids):
        db.execute('SELECT id FROM support_counter WHERE id=%s FOR UPDATE', (counter_id,))


def share_counter(db, plan_id, desk: Desk, plan_ids, actor):
    # 共用关系的调整全局串行，再通过线路行锁与派单路径互斥。
    db.execute('SELECT pg_advisory_xact_lock(1051003)')
    wanted = sorted({plan_id, *plan_ids})
    found = db.execute('SELECT id FROM route_plan WHERE id = ANY(%s)', (wanted,)).fetchall()
    if len(found) != len(wanted):
        raise HTTPException(409, '找不到要共用柜台的线路版本')
    released = db.execute('''SELECT plan_id FROM route_handover
                             WHERE plan_id = ANY(%s) AND released_at IS NOT NULL''', (wanted,)).fetchall()
    if len(released) != len(wanted):
        raise HTTPException(409, '只有已发布交接单的线路才能共用柜台')
    counter = db.execute('''SELECT c.* FROM support_counter c
                            JOIN support_counter_member m ON m.counter_id = c.id
                            WHERE m.plan_id = %s AND c.desk = %s''', (plan_id, desk)).fetchone()
    if not counter:
        raise HTTPException(409, '请先保存该服务点的设置，再指定共用线路')
    members = {row['plan_id'] for row in db.execute(
        'SELECT plan_id FROM support_counter_member WHERE counter_id=%s', (counter['id'],)).fetchall()}
    lock_plans(db, set(wanted) | members)
    # 加锁后重读成员与来源柜台，此时共用关系不会被并发修改。
    members = {row['plan_id'] for row in db.execute(
        'SELECT plan_id FROM support_counter_member WHERE counter_id=%s', (counter['id'],)).fetchall()}
    incoming = [pid for pid in wanted if pid not in members]
    outgoing = [pid for pid in members if pid not in wanted]
    sources = {row['plan_id']: row['counter_id'] for row in db.execute(
        'SELECT plan_id, counter_id FROM support_counter_member WHERE plan_id = ANY(%s) AND desk = %s',
        (incoming, desk)).fetchall()} if incoming else {}
    lock_counter_rows(db, {counter['id'], *sources.values()})
    counter = db.execute('SELECT * FROM support_counter WHERE id=%s', (counter['id'],)).fetchone()
    promote = []
    for pid in incoming:
        db.execute('''INSERT INTO support_counter_member (plan_id,desk,counter_id) VALUES (%s,%s,%s)
                      ON CONFLICT (plan_id,desk) DO UPDATE SET counter_id=excluded.counter_id''',
                   (pid, desk, counter['id']))
        source = sources.get(pid)
        if source and source != counter['id']:
            left = db.execute('SELECT count(*) AS n FROM support_counter_member WHERE counter_id=%s',
                              (source,)).fetchone()['n']
            if left:
                promote.append(source)
            else:
                db.execute('DELETE FROM support_counter WHERE id=%s', (source,))
    for pid in outgoing:
        fresh = db.execute('''INSERT INTO support_counter (desk,location,capacity,available,updated_by)
                              VALUES (%s,%s,%s,%s,%s) RETURNING id''',
                           (desk, counter['location'], counter['capacity'], counter['available'],
                            actor)).fetchone()
        db.execute('''INSERT INTO support_counter_member (plan_id,desk,counter_id) VALUES (%s,%s,%s)
                      ON CONFLICT (plan_id,desk) DO UPDATE SET counter_id=excluded.counter_id''',
                   (pid, desk, fresh['id']))
        promote.append(fresh['id'])
    promote.append(counter['id'])
    record(db, plan_id, actor, 'support.counter_shared', desk=desk, counter_id=counter['id'],
           plan_ids=wanted)
    return counter, promote

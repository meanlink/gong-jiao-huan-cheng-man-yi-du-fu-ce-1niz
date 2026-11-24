from fastapi import APIRouter, Depends

from .auth import allowed, identity
from .db import connect
from .plans import get_plan
from .support_directory import (counter_members, launch_window, list_desks, require_ready,
                                save_desk, share_counter, shareable_plans)
from .support_dispatch import (board_cases, open_case, promote_waiting, redispatch_case,
                               resolve_case, timeline)
from .support_notices import list_notices, publish_notice
from .support_schemas import (CounterSharing, Desk, DeskSettings, NoticeText, Resolution,
                              TransferRequest)

router = APIRouter(prefix='/plans/{plan_id}/support', tags=['改线乘客协助'])


@router.get('')
def support_board(plan_id: int, user=Depends(identity)):
    with connect() as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        plan = get_plan(db, plan_id)
        desks = list_desks(db, plan_id)
        members = {}
        for row in counter_members(db, plan_id):
            members.setdefault(row['counter_id'], []).append(
                {'plan_id': row['plan_id'], 'code': row['code'], 'hub': row['hub']})
        for desk in desks:
            desk['members'] = members.get(desk['counter_id'], [])
            desk['shared'] = len(desk['members']) > 1
        return {**launch_window(db, plan_id), 'trial_path': plan['trial_path'],
                'desks': desks, 'notices': list_notices(db, plan_id),
                'cases': board_cases(db, plan_id), 'shareable': shareable_plans(db)}


@router.put('/desks/{desk}')
def set_desk(plan_id: int, desk: Desk, data: DeskSettings, user=Depends(allowed('director'))):
    with connect() as db:
        get_plan(db, plan_id, lock=True)
        require_ready(db, plan_id)
        counter = save_desk(db, plan_id, desk, data, user['sub'])
        # 上限提高或恢复开放后，等待最久的待派单自动补位。
        promote_waiting(db, counter['id'], user['sub'])
        return counter


@router.put('/desks/{desk}/sharing')
def share_desk(plan_id: int, desk: Desk, data: CounterSharing, user=Depends(allowed('director'))):
    with connect() as db:
        # 共用调整在 share_counter 内统一加锁，这里不能再先锁线路，否则会与加锁顺序冲突。
        get_plan(db, plan_id)
        require_ready(db, plan_id)
        counter, promote = share_counter(db, plan_id, desk, data.plan_ids, user['sub'])
        for counter_id in promote:
            promote_waiting(db, counter_id, user['sub'])
        members = [row for row in counter_members(db, plan_id) if row['counter_id'] == counter['id']]
        return {**counter, 'members': members}


@router.post('/notices', status_code=201)
def issue_notice(plan_id: int, data: NoticeText, user=Depends(allowed('director'))):
    with connect() as db:
        get_plan(db, plan_id, lock=True)
        require_ready(db, plan_id)
        return publish_notice(db, plan_id, data, user['sub'])


@router.post('/cases', status_code=201)
def file_case(plan_id: int, data: TransferRequest, user=Depends(allowed('collector'))):
    with connect() as db:
        # All capacity-changing writes take the same plan lock before reading desk load.
        get_plan(db, plan_id, lock=True)
        require_ready(db, plan_id, live=True)
        return open_case(db, plan_id, data, user['sub'])


@router.get('/cases/{case_id}/timeline')
def case_history(plan_id: int, case_id: int, user=Depends(identity)):
    with connect() as db:
        get_plan(db, plan_id)
        return timeline(db, plan_id, case_id)


@router.post('/cases/{case_id}/redispatch')
def recalculate_destination(plan_id: int, case_id: int, user=Depends(allowed('analyst'))):
    with connect() as db:
        get_plan(db, plan_id, lock=True)
        require_ready(db, plan_id, live=True)
        return redispatch_case(db, plan_id, case_id, user['sub'])


@router.post('/cases/{case_id}/resolve')
def complete_case(plan_id: int, case_id: int, data: Resolution, user=Depends(allowed('analyst'))):
    with connect() as db:
        get_plan(db, plan_id, lock=True)
        require_ready(db, plan_id, live=True)
        return resolve_case(db, plan_id, case_id, data.note, user['sub'])

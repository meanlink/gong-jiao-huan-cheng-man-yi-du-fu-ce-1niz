from datetime import date

from psycopg.types.json import Jsonb

from src.analysis import compare, eligible_samples, snapshot_hash
from src.auth import passenger_key
from src.db import record
from src.support_directory import save_desk, share_counter
from src.support_dispatch import open_case, promote_waiting
from src.support_notices import publish_notice
from src.support_schemas import Desk, DeskSettings, NoticeText, TransferRequest

COLLECTOR = 'collector@chengxi.local'
ANALYST = 'analyst@chengxi.local'
DIRECTOR = 'director@chengxi.local'


def seed_support(db, scores):
    first = seed_line(db, scores, code='NM-26A', feeder='K18', ref_prefix='NM',
                      baseline_path='南广场 2 号站台 → 地铁 A 口',
                      trial_path='南门 5 号港湾 → 南厅电梯 → 地铁 B 口')
    if first:
        setup_desks(db, first, [(Desk.ACCESS, '南厅电梯口蓝色协助牌', 1),
                                (Desk.STATION, '南门站务台（B 口）', 2),
                                (Desk.DISPATCH, '5 号港湾调度亭', 2)])
        notice = publish_notice(db, first,
                                NoticeText(previous_edition=0, meeting_point='南门 5 号港湾蓝色换乘牌',
                                           directions='下车后沿蓝色地贴至南厅，使用电梯到地铁 B 口。需要协助时在电梯口等候站务员。'),
                                DIRECTOR)
        for category, landmark, description in [
            ('step_free', '南厅电梯口', '乘客需要无台阶通行协助。'),
            ('step_free', '5 号港湾蓝色牌', '乘客推婴儿车，需要协助抵达电梯。'),
            ('missed_connection', '5 号港湾', '乘客需核对下一班 M2 的换乘时间。'),
        ]:
            open_case(db, first, TransferRequest(category=category, landmark=landmark,
                                                 description=description,
                                                 notice_edition=notice['edition']), COLLECTOR)
    second = seed_line(db, scores, code='NM-26B', feeder='K5', ref_prefix='NB',
                       baseline_path='南广场 1 号站台 → 地铁 A 口',
                       trial_path='南门 3 号港湾 → 南厅电梯 → 地铁 B 口')
    if second:
        setup_desks(db, second, [(Desk.ACCESS, '北厅无障碍服务角', 1),
                                 (Desk.STATION, '南门站务台（B 口）', 2),
                                 (Desk.DISPATCH, '3 号港湾调度亭', 2)])
        notice = publish_notice(db, second,
                                NoticeText(previous_edition=0, meeting_point='南门 3 号港湾绿色换乘牌',
                                           directions='下车后沿绿色地贴至南厅，使用电梯到地铁 B 口。需要协助时前往南门站务台。'),
                                DIRECTOR)
        anchor = first or plan_id_by_code(db, 'NM-26A')
        if anchor:
            # 两条线路共用南门站务台，按同一上限接单。
            _, promote = share_counter(db, anchor, Desk.STATION, [anchor, second], DIRECTOR)
            for counter_id in promote:
                promote_waiting(db, counter_id, DIRECTOR)
        open_case(db, second, TransferRequest(category='wayfinding', landmark='南门站务台',
                                              description='乘客询问 K5 下车后换乘 M2 的站台位置。',
                                              notice_edition=notice['edition']), COLLECTOR)


def seed_line(db, scores, *, code, feeder, ref_prefix, baseline_path, trial_path):
    plan = db.execute('''INSERT INTO route_plan (code,hub,feeder,connection,baseline_path,trial_path,
                        baseline_from,baseline_to,trial_from,trial_to,stage)
                        VALUES (%s,'南门公交枢纽',%s,'M2',%s,%s,'2026-10-12','2026-10-16',
                            '2026-10-19','2026-10-23','decided') ON CONFLICT DO NOTHING RETURNING id''',
                      (code, feeder, baseline_path, trial_path)).fetchone()
    if not plan:
        return None
    add_handover(db, plan['id'], scores, ref_prefix)
    return plan['id']


def plan_id_by_code(db, code):
    row = db.execute('SELECT id FROM route_plan WHERE code=%s', (code,)).fetchone()
    return row['id'] if row else None


def setup_desks(db, plan_id, desks):
    for desk, location, capacity in desks:
        save_desk(db, plan_id, desk, DeskSettings(location=location, capacity=capacity, available=True),
                  DIRECTOR)


def add_handover(db, plan_id, scores, ref_prefix):
    days = {'baseline': date(2026, 10, 14), 'trial': date(2026, 10, 21)}
    with db.cursor() as cur:
        cur.executemany('''INSERT INTO survey_sample (sample_ref,plan_id,passenger_hash,cohort,trip_day,
                           time_band,rating,disposition,collected_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'eligible',%s)''',
                        [(ref, plan_id, passenger_key(ref), cohort, days[cohort], band, rating, COLLECTOR)
                         for (band, cohort), ratings in scores.items()
                         for index, rating in enumerate(ratings, 1)
                         for ref in [f'{ref_prefix}-{band}-{cohort}-{index:02}']])
    samples = eligible_samples(db, plan_id)
    p, effect, bands, adopt = compare(samples)
    assert adopt
    db.execute('''INSERT INTO retest_conclusion (plan_id,status,snapshot_hash,calculated_at,combined_p,
                  stratified_effect,band_results,adopt_allowed,decision,decision_note,signed_by,signed_at)
                  VALUES (%s,'signed',%s,now(),%s,%s,%s,%s,'adopt',%s,%s,now())''',
               (plan_id, snapshot_hash(samples), p, effect, Jsonb(bands), adopt, '按南厅电梯路径启用改线。', DIRECTOR))
    db.execute('''INSERT INTO route_handover (plan_id,effective_on,created_by,released_by,released_at,release_note)
                  VALUES (%s,'2026-10-26',%s,%s,now(),%s)''',
               (plan_id, DIRECTOR, DIRECTOR, '三项凭证齐备，按南厅电梯路径启用。'))
    with db.cursor() as cur:
        cur.executemany('''INSERT INTO handover_task (plan_id,kind,evidence,completed_by,completed_at)
                           VALUES (%s,%s,%s,%s,now())''', [
            (plan_id, 'stop_signage', '南门5号港湾站牌照片.jpg', COLLECTOR),
            (plan_id, 'accessibility_notice', '南厅电梯指引验收单.pdf', COLLECTOR),
            (plan_id, 'dispatch_briefing', 'K18-M2班前交接表.csv', ANALYST),
        ])
    record(db, plan_id, DIRECTOR, 'support.demo_handover_seeded')

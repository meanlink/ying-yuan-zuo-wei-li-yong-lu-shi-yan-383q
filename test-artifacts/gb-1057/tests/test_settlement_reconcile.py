"""试行场次结算：三方对账、回执幂等、版本化补正与并发复核保护（gb-1057）。

链路：排片 → 实验单元 → 观测 → Bootstrap → 审批 → 试行工单 → 排片确认 → 结算/补正/复核。
数据库使用 SQLite 文件库（部分唯一索引 sqlite_where 与 PostgreSQL 语义一致），
通过 RATE_LIMIT 放宽限流以便一次跑完整链路。
"""
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'backend'))

os.environ['DATABASE_URL'] = f'sqlite+pysqlite:////tmp/gb1057_settlement_{os.getpid()}.db'
os.environ['SEED_DEMO'] = 'false'
os.environ['JWT_SECRET'] = 'gb1057-test-secret'
os.environ['RATE_LIMIT'] = '100000'

from fastapi.testclient import TestClient  # noqa: E402

from src.database import Base, SessionLocal, engine  # noqa: E402
from src.main import app  # noqa: E402
from src.models import AuditLog, User  # noqa: E402
from src.operations_models import SchedulingPilot  # noqa: E402
from src.security import hash_password  # noqa: E402
from src.settlement.migration import run_migrations  # noqa: E402

USERS = {
    'planner': 'planner@cinema.local',
    'analyst': 'analyst@cinema.local',
    'revenue': 'revenue@cinema.local',
    'boxoffice': 'boxoffice@cinema.local',
    'admin': 'admin@cinema.local',
}
PASSWORD = 'cinema123'
TREATMENT_SOLD = 12   # 实验单元观测售出
TREATMENT_TOTAL = 15  # 实验单元观测座位
ACTUAL_SEATS = 18     # 排片回执实际投放


def login(client, role):
    response = client.post('/api/auth/login', json={'email': USERS[role], 'password': PASSWORD})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


@pytest.fixture(scope='module')
def env():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        run_migrations(conn)  # 非 PostgreSQL 必须是空操作
    db = SessionLocal()
    try:
        for role, email in USERS.items():
            db.add(User(email=email, name=role, role=role, password_hash=hash_password(PASSWORD)))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        tokens = {role: login(client, role) for role in USERS}
        suffix = uuid.uuid4().hex[:8]
        plan = client.post('/api/plans', headers=tokens['planner'], json={
            'natural_key': f'GB1057-{suffix}', 'film_title': '潮汐边缘', 'version_name': '结算联调',
            'show_date': '2026-11-21', 'show_time': '19:30', 'hall_name': 'IMAX-1',
            'seat_capacity': 120, 'base_price': 58})
        assert plan.status_code == 200, plan.text
        plan_id = plan.json()['id']
        units = {}
        for group, zone in (('control', '前排'), ('treatment', '中排')):
            response = client.post(f'/api/plans/{plan_id}/units', headers=tokens['planner'], json={
                'session_code': 'IMAX-1930', 'hall_name': 'IMAX-1', 'seat_zone': zone,
                'group_name': group, 'seat_count': 30})
            assert response.status_code == 200, response.text
            units[group] = response.json()['id']
        assert client.post(f'/api/plans/{plan_id}/start', headers=tokens['planner']).status_code == 200
        counter = 0

        def observe(unit_id, sold, count):
            nonlocal counter
            for _ in range(count):
                counter += 1
                response = client.post(f'/api/plans/{plan_id}/observations', headers=tokens['boxoffice'], json={
                    'unit_id': unit_id, 'ticket_code': f'T-{suffix}-{counter:03d}',
                    'seat_label': f'X-{counter:03d}', 'sold': sold, 'price_paid': 58 if sold else 0})
                assert response.status_code == 200, response.text

        observe(units['treatment'], True, TREATMENT_SOLD)
        observe(units['treatment'], False, TREATMENT_TOTAL - TREATMENT_SOLD)
        observe(units['control'], True, 10)
        observe(units['control'], False, 5)
        analysis = client.post(f'/api/plans/{plan_id}/analysis', headers=tokens['analyst'])
        assert analysis.status_code == 200, analysis.text
        assert analysis.json()['analysis_status'] == 'ready'
        review_id = analysis.json()['id']
        approved = client.post(f'/api/plans/{plan_id}/reviews/{review_id}/approve',
                               headers=tokens['revenue'], json={'notes': '分层结果确认'})
        assert approved.status_code == 200, approved.text
        yield SimpleNamespace(client=client, tokens=tokens, suffix=suffix, plan_id=plan_id,
                              units=units, review_id=review_id, slot=0)
    Base.metadata.drop_all(engine)


def make_pilot(env, actual_seats=ACTUAL_SEATS, backdate=True):
    """走完整工单流：收益派发 → 策划确认回执 → 必要时把场次挪到昨天使其可结算。"""
    env.slot += 1
    response = env.client.post(f"/api/plans/{env.plan_id}/scheduling-pilots", headers=env.tokens['revenue'], json={
        'review_id': env.review_id, 'unit_id': env.units['treatment'],
        'show_date': (date.today() + timedelta(days=5)).isoformat(), 'show_time': f'20:{env.slot:02d}',
        'pilot_seats': 20, 'objective': '周末放量试验复盘'})
    assert response.status_code == 201, response.text
    pilot_id = response.json()['id']
    confirmed = env.client.post(f"/api/plans/{env.plan_id}/scheduling-pilots/{pilot_id}/confirm",
                                headers=env.tokens['planner'],
                                json={'booking_ref': f'BOOK-{env.suffix}-{env.slot}', 'actual_seats': actual_seats,
                                      'result_note': '排片系统已确认'})
    assert confirmed.status_code == 200, confirmed.text
    if backdate:
        db = SessionLocal()
        try:
            row = db.get(SchedulingPilot, pilot_id)
            row.show_date = date.today() - timedelta(days=1)
            db.commit()
        finally:
            db.close()
    return pilot_id


def payload(reference, sold, **overrides):
    data = {'pos_reference': reference, 'tickets_sold': sold, 'gross_tickets_cents': 6960,
            'ticket_commission_cents': 696, 'concession_margin_cents': 150,
            'incremental_cost_cents': 200, 'subsidy_cents': 0, 'notes': '晚场结算口径说明'}
    data.update(overrides)
    return data


def submit(env, pilot_id, body, role='boxoffice'):
    return env.client.post(f"/api/plans/{env.plan_id}/pilot-settlements/{pilot_id}",
                           headers=env.tokens[role], json=body)


def correct(env, pilot_id, body, role='boxoffice'):
    return env.client.post(f"/api/plans/{env.plan_id}/pilot-settlements/{pilot_id}/corrections",
                           headers=env.tokens[role], json=body)


def review(env, pilot_id, body, role='revenue'):
    return env.client.post(f"/api/plans/{env.plan_id}/pilot-settlements/{pilot_id}/review",
                           headers=env.tokens[role], json=body)


def board_item(env, pilot_id):
    response = env.client.get(f"/api/plans/{env.plan_id}/pilot-settlements", headers=env.tokens['analyst'])
    assert response.status_code == 200, response.text
    return next(item for item in response.json()['items'] if item['pilot_id'] == pilot_id)


def audit_rows(entity_id):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter_by(entity_type='pilot_settlement', entity_id=str(entity_id)).all()
    finally:
        db.close()


def test_aligned_submit_and_board_alignment(env):
    pilot_id = make_pilot(env)
    created = submit(env, pilot_id, payload('POS-A-1', TREATMENT_SOLD))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body['state'] == 'submitted' and body['version'] == 1 and body['is_current']
    assert body['diff_sources'] == []
    assert body['utilization'] == pytest.approx(TREATMENT_SOLD / ACTUAL_SEATS)

    item = board_item(env, pilot_id)
    assert item['observed'] == {'observed': TREATMENT_TOTAL, 'sold': TREATMENT_SOLD}
    assert item['seats'] == ACTUAL_SEATS and item['alignment'] == []
    assert item['can_submit'] is False and item['can_correct'] is True
    assert [row['version'] for row in item['history']] == [1]
    # 已登记场次再次登记被拦截，引导走补正
    assert submit(env, pilot_id, payload('POS-A-2', TREATMENT_SOLD)).status_code == 409


def test_same_receipt_retry_returns_original(env):
    pilot_id = make_pilot(env)
    body = payload('POS-B-1', 10)
    first = submit(env, pilot_id, body)
    assert first.status_code == 201
    retry = submit(env, pilot_id, body)
    assert retry.status_code == 200, retry.text
    assert retry.json()['id'] == first.json()['id']
    assert len(board_item(env, pilot_id)['history']) == 1
    assert len(audit_rows(first.json()['id'])) == 1  # 重试不重复入账也不重复审计
    conflict = submit(env, pilot_id, payload('POS-B-1', 11))  # 同凭证不同内容
    assert conflict.status_code == 409


def test_receipt_cannot_move_to_other_pilot(env):
    first_pilot = make_pilot(env)
    second_pilot = make_pilot(env)
    assert submit(env, first_pilot, payload('POS-C-1', 10)).status_code == 201
    assert submit(env, second_pilot, payload('POS-C-1', 10)).status_code == 409


def test_oversold_against_schedule_is_held(env):
    pilot_id = make_pilot(env)
    created = submit(env, pilot_id, payload('POS-D-1', ACTUAL_SEATS + 1, gross_tickets_cents=8000))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body['state'] == 'held'
    assert set(body['diff_sources']) == {'schedule', 'observation'}
    item = board_item(env, pilot_id)
    assert set(item['alignment']) == {'schedule', 'observation'}
    held = review(env, pilot_id, {'decision': 'approved', 'note': '尝试复核暂缓单',
                                  'expected_settlement_id': body['id']})
    assert held.status_code == 409
    assert '暂缓' in held.json()['detail'] or '对齐' in held.json()['detail']


def test_oversold_against_observation_only_is_held(env):
    pilot_id = make_pilot(env)
    created = submit(env, pilot_id, payload('POS-E-1', TREATMENT_SOLD + 1))  # 13 ≤ 实投 18，但 > 观测售出 12
    assert created.status_code == 201
    assert created.json()['state'] == 'held'
    assert created.json()['diff_sources'] == ['observation']


def test_review_approve_and_version_guard(env):
    pilot_id = make_pilot(env)
    created = submit(env, pilot_id, payload('POS-F-1', 10)).json()
    wrong = review(env, pilot_id, {'decision': 'approved', 'note': '版本号不对',
                                   'expected_settlement_id': created['id'] + 99})
    assert wrong.status_code == 409
    done = review(env, pilot_id, {'decision': 'approved', 'note': '收益确认无误',
                                  'expected_settlement_id': created['id']})
    assert done.status_code == 200, done.text
    assert done.json()['state'] == 'approved' and done.json()['reviewed_by']
    again = review(env, pilot_id, {'decision': 'rejected', 'note': '重复裁定该单',
                                   'expected_settlement_id': created['id']})
    assert again.status_code == 409


def test_submitter_cannot_review_own(env):
    pilot_id = make_pilot(env)
    created = submit(env, pilot_id, payload('POS-G-1', 10), role='admin').json()
    own = review(env, pilot_id, {'decision': 'approved', 'note': '自登自审拦截',
                                 'expected_settlement_id': created['id']}, role='admin')
    assert own.status_code == 409


def test_correction_switches_utilization_in_one_shot(env):
    pilot_id = make_pilot(env)
    v1 = submit(env, pilot_id, payload('POS-H-1', 10)).json()
    assert v1['utilization'] == pytest.approx(10 / ACTUAL_SEATS)
    corrected = correct(env, pilot_id, payload('POS-H-2', TREATMENT_SOLD, expected_settlement_id=v1['id']))
    assert corrected.status_code == 201, corrected.text
    v2 = corrected.json()
    assert v2['version'] == 2 and v2['is_current'] and v2['state'] == 'submitted'
    item = board_item(env, pilot_id)
    assert item['settlement']['id'] == v2['id']
    assert item['settlement']['utilization'] == pytest.approx(TREATMENT_SOLD / ACTUAL_SEATS)
    assert [row['version'] for row in item['history']] == [2, 1]
    old = item['history'][1]
    assert old['state'] == 'superseded' and not old['is_current'] and old['superseded_by'] == v2['id']


def test_failed_correction_keeps_old_version(env):
    pilot_id = make_pilot(env)
    v1 = submit(env, pilot_id, payload('POS-I-1', 10)).json()
    bad = correct(env, pilot_id, payload('POS-I-2', 10, ticket_commission_cents=99999,
                                         expected_settlement_id=v1['id']))
    assert bad.status_code == 422  # 分账超过实收，口径非法
    misaligned_retry = correct(env, pilot_id, payload('POS-I-3', TREATMENT_SOLD + 1,
                                                      expected_settlement_id=v1['id']))
    assert misaligned_retry.status_code == 422  # 补正后仍与实验单元观测不对齐
    item = board_item(env, pilot_id)
    assert item['settlement']['id'] == v1['id'] and item['settlement']['state'] == 'submitted'
    assert item['settlement']['tickets_sold'] == 10 and len(item['history']) == 1


def test_correction_of_approved_marks_recheck(env):
    pilot_id = make_pilot(env)
    v1 = submit(env, pilot_id, payload('POS-J-1', 10)).json()
    assert review(env, pilot_id, {'decision': 'approved', 'note': '收益确认通过',
                                  'expected_settlement_id': v1['id']}).status_code == 200
    corrected = correct(env, pilot_id, payload('POS-J-2', TREATMENT_SOLD, expected_settlement_id=v1['id']))
    assert corrected.status_code == 201
    item = board_item(env, pilot_id)
    old = next(row for row in item['history'] if row['version'] == 1)
    assert old['state'] == 'recheck'  # 已确认旧结果保留并标为待重核
    assert old['review_note'] == '收益确认通过'
    assert item['settlement']['version'] == 2 and item['settlement']['state'] == 'submitted'


def test_concurrent_correction_and_review_never_clobber(env):
    pilot_id = make_pilot(env)
    v1 = submit(env, pilot_id, payload('POS-K-1', 10)).json()
    v2 = correct(env, pilot_id, payload('POS-K-2', TREATMENT_SOLD,
                                        expected_settlement_id=v1['id'])).json()
    stale_correct = correct(env, pilot_id, payload('POS-K-3', TREATMENT_SOLD,
                                                   expected_settlement_id=v1['id']))
    assert stale_correct.status_code == 409  # 基于旧版本的补正被拒绝
    stale_review = review(env, pilot_id, {'decision': 'approved', 'note': '基于旧版的复核',
                                          'expected_settlement_id': v1['id']})
    assert stale_review.status_code == 409  # 基于旧版本的复核被拒绝
    fresh_review = review(env, pilot_id, {'decision': 'approved', 'note': '基于当前版的复核',
                                          'expected_settlement_id': v2['id']})
    assert fresh_review.status_code == 200
    item = board_item(env, pilot_id)
    assert item['settlement']['id'] == v2['id'] and item['settlement']['state'] == 'approved'
    assert len(item['history']) == 2


def test_correction_retry_is_idempotent(env):
    pilot_id = make_pilot(env)
    v1 = submit(env, pilot_id, payload('POS-L-1', 10)).json()
    body = payload('POS-L-2', TREATMENT_SOLD, expected_settlement_id=v1['id'])
    first = correct(env, pilot_id, body)
    assert first.status_code == 201
    retry = correct(env, pilot_id, body)
    assert retry.status_code == 200 and retry.json()['id'] == first.json()['id']
    assert len(board_item(env, pilot_id)['history']) == 2


def test_held_settlement_resolved_by_correction(env):
    pilot_id = make_pilot(env)
    held = submit(env, pilot_id, payload('POS-M-1', TREATMENT_SOLD + 3))  # 售出超过实验单元观测
    assert held.json()['state'] == 'held'
    corrected = correct(env, pilot_id, payload('POS-M-2', TREATMENT_SOLD,
                                               expected_settlement_id=held.json()['id']))
    assert corrected.status_code == 201 and corrected.json()['state'] == 'submitted'
    item = board_item(env, pilot_id)
    assert item['alignment'] == []
    assert item['settlement']['utilization'] == pytest.approx(TREATMENT_SOLD / ACTUAL_SEATS)
    old = next(row for row in item['history'] if row['version'] == 1)
    assert old['state'] == 'superseded' and old['diff_sources'] == ['observation']
    done = review(env, pilot_id, {'decision': 'approved', 'note': '补正后复核通过',
                                  'expected_settlement_id': corrected.json()['id']})
    assert done.status_code == 200


def test_settlement_before_show_start_rejected(env):
    pilot_id = make_pilot(env, backdate=False)
    assert submit(env, pilot_id, payload('POS-N-1', 10)).status_code == 409


def test_invalid_economics_rejected(env):
    pilot_id = make_pilot(env)
    bad = submit(env, pilot_id, payload('POS-O-1', 10, ticket_commission_cents=99999))
    assert bad.status_code == 422
    item = board_item(env, pilot_id)
    assert item['settlement'] is None and item['can_submit'] is True

"""LOCAL 訂閱需進入真正 authority FIFO，不能把 adapter ACK 當成投遞。"""
from contextlib import closing

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _Collector, _KanbanNotification
from gateway.session_authority import initialize_session_authority
from gateway.session_contract import Principal
from gateway.session_local import create_local_session
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_local_resume_compression_child import _runner


async def setup_local(tmp_path, monkeypatch):
    from gateway import run
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    from gateway.run import GatewayRunner
    from types import MethodType
    runner = _runner(tmp_path)
    for name in ('_kanban_sub_op', '_kanban_advance', '_kanban_rewind', '_kanban_unsub'):
        setattr(runner, name, MethodType(getattr(GatewayRunner, name), runner))
    runner._authorization_adapter = lambda platform, profile: runner.adapters.get(platform)
    runner._owns_kanban_dispatcher_lock = lambda: True
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='test')
    runner.session_authority = authority
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    actor = Principal('owner', str(tmp_path), frozenset({'session:create', 'session:read'}), 'socket')
    ref = create_local_session(authority, actor, dict(request_id='origin', source='gui', cwd=str(tmp_path),
                                                     model='fixture', toolsets=[]))
    return runner, authority, ref


def subscription_task(ref):
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title='fixture task', assignee='worker')
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id,
                           notifier_profile='default', chat_type='dm', delivery_mode='notify')
        return tid


async def tick(runner, tid):
    collector = _Collector(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    with closing(kbc.connect()) as conn:
        sub = kbn.list_notify_subs(conn, tid)[0]
        delivery = collector._claim_for_sub(conn, 'default', sub)
    if delivery:
        await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts={}).deliver()
    return delivery


@pytest.mark.asyncio
async def test_local_passive_ack_is_not_delivery(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='fixture completed')
    await tick(runner, tid)
    rows = list_session_admissions(authority.db, session_id=ref.session_id)
    assert len(rows) == 1, 'LOCAL send() ACK consumed the event without durable admission'
    assert tid in rows[0]['payload']['text']
    assert rows[0]['status'] == 'queued'


@pytest.mark.asyncio
async def test_start_and_explicit_progress_have_distinct_durable_versions(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        # 模擬前一輪留下的舊摘要，與新 claim 同時存在。
        conn.execute('UPDATE tasks SET result=? WHERE id=?', ('OBSOLETE_FIXTURE_RESULT', tid))
        conn.commit()
        kb.recompute_ready(conn)
        kb.claim_task(conn, tid)
    await tick(runner, tid)
    first = list_session_admissions(authority.db, session_id=ref.session_id)
    assert len(first) == 1
    assert 'OBSOLETE_FIXTURE_RESULT' not in first[0]['payload']['text']
    with closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, 'worker', 'progress: fixture halfway')
    await tick(runner, tid)
    rows = list_session_admissions(authority.db, session_id=ref.session_id)
    assert len(rows) == 1
    assert 'fixture halfway' in rows[-1]['payload']['text']
    with closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, 'worker', 'progress:  fixture   halfway')
    await tick(runner, tid)
    assert len(list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)) == 2


@pytest.mark.asyncio
async def test_busy_queue_refreshes_and_coalesces_the_same_card(tmp_path, monkeypatch):
    from gateway.session_ingress import execute_admission
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason='old fixture question', kind='needs_input')
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id, delivery_mode='wake')
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, 'owner', 'needs_input: 請 Sho 選擇新方案')
    await tick(runner, tid)
    rows = list_session_admissions(authority.db, session_id=ref.session_id)
    assert len(rows) == 1, 'superseded queued card notifications must not accumulate'
    with closing(kbc.connect()) as conn:
        kb.unblock_task(conn, tid)
        kb.complete_task(conn, tid, summary='new fixture result')
    received = []

    async def handle(event):
        received.append(event.text)
    runner._handle_message = handle
    await execute_admission(authority, ref, rows[0])
    assert len(received) == 1
    assert 'new fixture result' in received[0] and 'old fixture question' not in received[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('comment,phase', [(None, 'needs_input'), ('progress: 工程師正在處理', 'progress')])
async def test_only_a_real_input_request_is_presented_as_a_question(tmp_path, monkeypatch, comment, phase):
    import json
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason='fixture decision', kind='needs_input')
        if comment:
            kb.add_comment(conn, tid, 'worker', comment)
    await tick(runner, tid)
    row = list_session_admissions(authority.db, session_id=ref.session_id)[0]
    state = json.loads(row['payload']['text'].splitlines()[0].removeprefix('[kanban] '))
    assert state['phase'] == phase


@pytest.mark.asyncio
async def test_lost_cursor_ack_and_offline_owner_keep_one_admission(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='durable fixture')
        original = kbn.list_notify_subs(conn, tid)[0]['last_event_id']
    authority.retiring = True
    await tick(runner, tid)
    assert list_session_admissions(authority.db, session_id=ref.session_id) == []
    authority.retiring = False
    real = runner._kanban_advance

    def fail_cursor(*args):
        raise RuntimeStoreError('storage_unavailable')
    runner._kanban_advance = fail_cursor
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid)[0]['last_event_id'] == original
    runner._kanban_advance = real
    await tick(runner, tid)
    assert len(list_session_admissions(authority.db, session_id=ref.session_id)) == 1
    assert await tick(runner, tid) is None


@pytest.mark.asyncio
async def test_unknown_origin_keeps_cursor_and_does_not_use_worker_session(tmp_path, monkeypatch):
    from types import SimpleNamespace
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(SimpleNamespace(session_id='unknown-origin'))
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='do not guess')
        original = kbn.list_notify_subs(conn, tid)[0]['last_event_id']
    await tick(runner, tid)
    assert list_session_admissions(authority.db, session_id=ref.session_id) == []
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid)[0]['last_event_id'] == original


@pytest.mark.asyncio
async def test_unsubscribe_queued_notice_skips_model(tmp_path, monkeypatch):
    from gateway.session_ingress import execute_admission
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='retired fixture')
    await tick(runner, tid)
    row = list_session_admissions(authority.db, session_id=ref.session_id)[0]
    with closing(kbc.connect()) as conn:
        kbn.remove_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id)
    async def unexpected(event):
        pytest.fail('unsubscribed event reached model')
    runner._handle_message = unexpected
    assert await execute_admission(authority, ref, row) == ''


@pytest.mark.asyncio
async def test_notify_only_is_a_durable_note_without_inference(tmp_path, monkeypatch):
    from gateway.session_ingress import execute_admission
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='passive fixture result')
    await tick(runner, tid)
    row = list_session_admissions(authority.db, session_id=ref.session_id)[0]
    async def unexpected(event):
        pytest.fail('notify-only subscription started an agent turn')
    runner._handle_message = unexpected
    assert await execute_admission(authority, ref, row) == ''
    messages = authority.db.get_messages_as_conversation(ref.session_id)
    assert any('passive fixture result' in str(m.get('content')) for m in messages)


@pytest.mark.asyncio
async def test_archive_releases_local_subscription(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='finished fixture')
        kb.archive_task(conn, tid)
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid) == []
    assert list_session_admissions(authority.db, session_id=ref.session_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['crashed', 'timed_out', 'gave_up'])
async def test_failure_notice_preserves_current_error_without_worker_routing(tmp_path, monkeypatch, kind):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        # Dispatcher 事件的隔離 fixture；不啟動真正業務 worker。
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, {'error': 'CURRENT_FIXTURE_FAILURE'})
    await tick(runner, tid)
    row = list_session_admissions(authority.db, session_id=ref.session_id)[0]
    assert kind in row['payload']['text'] and 'CURRENT_FIXTURE_FAILURE' in row['payload']['text']


@pytest.mark.asyncio
async def test_bookkeeping_comment_cannot_resurrect_an_old_input_request(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason='old request', kind='needs_input')
        kb.add_comment(conn, tid, 'owner', 'needs_input: OBSOLETE_QUESTION')
        kb.unblock_task(conn, tid)
        kb.block_task(conn, tid, reason='CURRENT_ENGINEERING_BLOCKER', kind='capability')
        kb.add_comment(conn, tid, 'auto-decomposer', 'bookkeeping only')
    await tick(runner, tid)
    text = list_session_admissions(authority.db, session_id=ref.session_id)[0]['payload']['text']
    assert 'CURRENT_ENGINEERING_BLOCKER' in text and 'OBSOLETE_QUESTION' not in text

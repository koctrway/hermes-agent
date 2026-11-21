"""封存清理只接受 completed，不把失敗收據當成已交付。"""
from contextlib import closing
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_kanban_local_lifecycle import setup_local, subscription_task, tick


@pytest.mark.asyncio
async def test_failed_archived_notice_retains_subscription_and_receipt(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id, delivery_mode='wake')
        kb.complete_task(conn, tid, summary='final fixture')
        kb.archive_task(conn, tid)
        cursor = kbn.list_notify_subs(conn, tid)[0]['last_event_id']
    await tick(runner, tid)

    async def failed(event):
        raise OSError('fixture execution failed')
    runner._handle_message = failed
    await authority._drain(ref)
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid)[0]['last_event_id'] == cursor
    rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
    assert len(rows) == 1 and rows[0]['outcome'] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['notify', 'wake', 'notify+wake'])
async def test_archive_preserves_triage_and_does_not_resurrect_ignored_input(tmp_path, monkeypatch, mode):
    from gateway.kanban_local_delivery import snapshot
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    reference = [str(tmp_path / 'board.db'), tid, ref.session_id, 'default']
    with closing(kbc.connect()) as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id, delivery_mode=mode)
        for i in range(kb.BLOCK_RECURRENCE_LIMIT):
            kb.block_task(conn, tid, reason='CURRENT_ENGINEERING_BLOCKER', kind='capability')
            if i < kb.BLOCK_RECURRENCE_LIMIT - 1:
                kb.unblock_task(conn, tid)
                kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == 'triage'
        kb.add_comment(conn, tid, 'owner', 'needs_input: IGNORED_DURING_TRIAGE')
    before = snapshot(reference)

    async def handle(event):
        assert 'IGNORED_DURING_TRIAGE' not in event.text
        return 'fixture response'
    runner._handle_message = handle
    await tick(runner, tid)
    await authority._drain(ref)
    with closing(kbc.connect()) as conn:
        kb.archive_task(conn, tid)
    after = snapshot(reference)
    assert (json.loads(after[0])['phase'], after[2]) == (json.loads(before[0])['phase'], before[2])
    assert 'IGNORED_DURING_TRIAGE' not in after[0]
    await tick(runner, tid)
    assert len(list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['notify', 'wake', 'notify+wake'])
async def test_archive_keeps_latest_comment_version_instead_of_old_completed_receipt(tmp_path, monkeypatch, mode):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id, delivery_mode=mode)
        kb.block_task(conn, tid, reason='OLD_DETAIL_A', kind='needs_input')
    received = []

    async def handle(event):
        received.append(event.text)
        return 'fixture response'
    runner._handle_message = handle
    await tick(runner, tid)
    await authority._drain(ref)
    with closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, 'owner', 'needs_input: NEW_DETAIL_B')
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        kb.archive_task(conn, tid)
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid), 'old completed receipt revoked the latest queued version'
    await authority._drain(ref)
    text = str(authority.db.get_messages_as_conversation(ref.session_id)) if mode == 'notify' else str(received)
    assert 'NEW_DETAIL_B' in text
    await tick(runner, tid)
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['notify', 'wake', 'notify+wake'])
async def test_archive_without_business_notice_stays_silent(tmp_path, monkeypatch, mode):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id, delivery_mode=mode)
        kb.archive_task(conn, tid)
    await tick(runner, tid)
    assert list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False) == []
    with closing(kbc.connect()) as conn:
        assert kbn.list_notify_subs(conn, tid) == []

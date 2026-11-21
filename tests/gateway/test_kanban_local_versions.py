"""獨立審查第一輪發現的漏報反例。"""
from contextlib import closing
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_state_runtime import list_session_admissions, claim_session_input, settle_session_input
from gateway.session_ingress import execute_admission
from tests.gateway.test_kanban_local_lifecycle import setup_local, subscription_task, tick


@pytest.mark.asyncio
async def test_ordinary_comment_keeps_important_progress(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.recompute_ready(conn)
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, 'worker', 'progress: IMPORTANT_MILESTONE')
        kb.add_comment(conn, tid, 'owner', 'acknowledged')
    await tick(runner, tid)
    assert 'IMPORTANT_MILESTONE' in list_session_admissions(authority.db, session_id=ref.session_id)[0]['payload']['text']


@pytest.mark.asyncio
async def test_metadata_edit_keeps_completed_summary(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='FINAL_RESULT')
        assert kb.edit_task(conn, tid, title='updated metadata')
    await tick(runner, tid)
    row = list_session_admissions(authority.db, session_id=ref.session_id)[0]
    state = json.loads(row['payload']['text'].splitlines()[0].removeprefix('[kanban] '))
    assert state['phase'] == 'completed' and state['detail'] == 'FINAL_RESULT'


@pytest.mark.asyncio
async def test_question_a_b_a_is_a_new_occurrence_not_a_cancelled_retry(tmp_path, monkeypatch):
    runner, authority, ref = await setup_local(tmp_path, monkeypatch)
    tid = subscription_task(ref)
    with closing(kbc.connect()) as conn:
        kb.block_task(conn, tid, reason='needs a choice', kind='needs_input')
        kb.add_comment(conn, tid, 'owner', 'needs_input: question A')
    await tick(runner, tid)
    first = list_session_admissions(authority.db, session_id=ref.session_id)[0]
    with closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, 'owner', 'needs_input: question B')
    await tick(runner, tid)
    second = claim_session_input(authority.db, epoch=authority.epoch, session_id=ref.session_id)
    await execute_admission(authority, ref, second)
    settle_session_input(authority.db, epoch=authority.epoch, admission_id=second['admission_id'],
                         generation=second['generation'], outcome='completed')
    with closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, 'owner', 'needs_input: question A')
    await tick(runner, tid)
    pending = list_session_admissions(authority.db, session_id=ref.session_id)
    assert len(pending) == 1 and pending[0]['admission_id'] != first['admission_id']

"""同一 gateway 的兩個 profile 及三張卡，維持各自原來源。"""
from contextlib import closing

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner
from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session_authorities import owner_scope
from gateway.session_contract import Principal
from gateway.session_local import create_local_session
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_kanban_local_lifecycle import tick
from tests.gateway.test_secondary_local_pick_multiplex import _reserve_homes


@pytest.mark.asyncio
async def test_local_notices_cannot_cross_profiles_or_guess_origins(tmp_path, monkeypatch):
    from gateway import run
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {'platform_toolsets': {'cli': []}})
    root, homes = _reserve_homes(tmp_path, monkeypatch)
    monkeypatch.setenv('HERMES_KANBAN_DB', str(root / 'board.db'))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    config = GatewayConfig()
    config.multiplex_profiles = True
    config._runtime_profile_homes = tuple(homes)
    runner = GatewayRunner(config)
    process_ownership.reserve([home for _, home in homes])
    try:
        await initialize_gateway_runtime(runner)
        pairs = []
        for name, home in homes:
            authority = runner.session_authorities.for_home(home)
            monkeypatch.setattr(authority, '_schedule', lambda ref: None)
            with owner_scope(authority):
                actor = Principal('owner', str(home), frozenset({'session:create', 'session:read'}), 'socket')
                ref = create_local_session(authority, actor, dict(request_id='origin', source='gui',
                                           cwd=str(home), model='fixture', toolsets=[]))
            with closing(kbc.connect()) as conn:
                tid = kb.create_task(conn, title=name + '-only', assignee='fixture')
                kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=ref.session_id,
                                   notifier_profile=name, delivery_mode='wake')
                kb.complete_task(conn, tid, summary=name + '-result')
            pairs.append((authority, ref, tid))
        for authority, ref, tid in pairs:
            await tick(runner, tid)
            rows = list_session_admissions(authority.db, session_id=ref.session_id)
            assert len(rows) == 1 and tid in rows[0]['payload']['text']
        primary, ref, _tid = pairs[0]
        with closing(kbc.connect()) as conn:
            forged = kb.create_task(conn, title='unknown source profile', assignee='fixture')
            kbn.add_notify_sub(conn, task_id=forged, platform='local', chat_id=ref.session_id,
                               notifier_profile='beta', delivery_mode='wake')
            kb.complete_task(conn, forged, summary='must stay pending')
            cursor = kbn.list_notify_subs(conn, forged)[0]['last_event_id']
        await tick(runner, forged)
        with closing(kbc.connect()) as conn:
            assert kbn.list_notify_subs(conn, forged)[0]['last_event_id'] == cursor
        assert len(list_session_admissions(primary.db, session_id=ref.session_id)) == 1
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        for _, home in homes:
            process_ownership.release(home)

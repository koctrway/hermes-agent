"""以本版普通 daemon 驗證 LOCAL 看板通知，模型僅為 loopback 邊界。"""
import asyncio
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

from local_recovery_probe import Model, child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until


def probe(base):
    root = Path(__file__).resolve().parents[3]
    home, user = base / 'state', base / 'user'
    home.mkdir(); user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    cfg = {'gateway': {'multiplex_profiles': False}, 'kanban': {'dispatch_in_gateway': False},
           'model': {'provider': 'custom', 'default': 'fixture', 'base_url': url},
           'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}
    (home / 'config.yaml').write_text(json.dumps(cfg))
    board = home / 'fixture-board.db'
    os.environ.update(HERMES_HOME=str(home), HERMES_KANBAN_DB=str(board))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               HERMES_KANBAN_DB=str(board), OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
    kb.init_db()
    shared = {}

    def rows():
        with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM session_admissions ORDER BY seq')]

    def queued_notice():
        return [r for r in rows() if r['request_id'].startswith('kanban-local:') and r['status'] == 'queued']

    async def wait(predicate, what):
        await async_wait_until(predicate, timeout=45, what=what)

    async def busy(desc):
        async with websocket(home, desc) as ws:
            reply = await rpc(ws, 'session.create', request_id='origin', source='gui', cwd=str(home), toolsets=[])
            assert 'result' in reply, reply
            sid = shared['sid'] = reply['result']['session_id']
            with closing(kbc.connect()) as conn:
                tid = shared['tid'] = kb.create_task(conn, title='isolated lifecycle', assignee='fixture')
                kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=sid, notifier_profile='default', delivery_mode='notify+wake')
            assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='held', text='BLOCK_STARTED')
            assert await asyncio.to_thread(peer.blocked.wait, 25)
            assert 'result' in await rpc(ws, 'prompt.submit', session_id=sid, input_id='human', text='HUMAN_FIFO_FOLLOWER')
            with closing(kbc.connect()) as conn:
                kb.block_task(conn, tid, reason='obsolete fixture question', kind='needs_input')
            await wait(queued_notice, 'notice durable behind busy human turn')
            assert len(queued_notice()) == 1
            with closing(kbc.connect()) as conn:
                kb.add_comment(conn, tid, 'fixture', 'needs_input: CURRENT_FIXTURE_QUESTION')
            await wait(lambda: any('CURRENT_FIXTURE_QUESTION' in r['payload_json'] for r in queued_notice()),
                       'latest demand replaces queued notice')
            assert len(queued_notice()) == 1
            peer.release.set()
            await wait(lambda: all(r['status'] == 'terminal' for r in rows()), 'FIFO settled')
            history = await rpc(ws, 'session.resume', session_id=sid)
            assert 'result' in history and 'CURRENT_FIXTURE_QUESTION' in json.dumps(history), history
            inputs = [next((m.get('content', '') for m in reversed(r['messages']) if m['role'] == 'user'), '')
                      for r in peer.requests if r.get('messages')]
            assert inputs[0] == 'BLOCK_STARTED' and inputs[1] == 'HUMAN_FIFO_FOLLOWER', inputs
            assert len(inputs) == 3 and 'CURRENT_FIXTURE_QUESTION' in inputs[2], inputs
            assert 'obsolete fixture question' not in inputs[2]

    async def recovered(desc):
        await wait(lambda: any('OFFLINE_REVIEW_FIXTURE' in r['payload_json'] and r['status'] == 'terminal'
                              for r in rows()), 'offline event delivered after restart without viewer')
        async with websocket(home, desc) as ws:
            resumed = await rpc(ws, 'session.resume', session_id=shared['sid'])
            assert 'result' in resumed, resumed
            history = await rpc(ws, 'session.resume', session_id=shared['sid'])
            assert 'OFFLINE_REVIEW_FIXTURE' in json.dumps(history), history
        assert all(r['status'] == 'terminal' for r in rows()), rows()
        assert len([r for r in rows() if 'OFFLINE_REVIEW_FIXTURE' in r['payload_json']]) == 1
        inferences = len(peer.requests)
        with closing(kbc.connect()) as conn:
            passive = kb.create_task(conn, title='passive fixture', assignee='fixture')
            kbn.add_notify_sub(conn, task_id=passive, platform='local', chat_id=shared['sid'],
                               notifier_profile='default', delivery_mode='notify')
            kb.complete_task(conn, passive, summary='PASSIVE_NOTE_FIXTURE')
        def note_exists():
            with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as conn:
                return conn.execute("SELECT 1 FROM messages WHERE display_kind='internal_notification' "
                                    "AND content LIKE '%PASSIVE_NOTE_FIXTURE%'").fetchone() is not None
        await wait(note_exists, 'passive notification persisted without inference')
        await wait(lambda: all(r['status'] == 'terminal' for r in rows()), 'passive admission settled')
        assert len(peer.requests) == inferences
        async with websocket(home, desc) as ws:
            history = await rpc(ws, 'session.resume', session_id=shared['sid'])
            assert 'PASSIVE_NOTE_FIXTURE' in json.dumps(history), history

    try:
        with daemon(root, home, env, barrier=False) as (first, desc):
            first_pid, epoch = first.pid, desc['authority_epoch']
            asyncio.run(busy(desc))
        # Gateway 已停；真正 board 事件持久存在，沒有另開 writer 或補寫 transcript。
        with closing(kbc.connect()) as conn:
            kb.unblock_task(conn, shared['tid'])
            kb.claim_task(conn, shared['tid'])
            kb.request_review(conn, shared['tid'], summary='OFFLINE_REVIEW_FIXTURE')
        with daemon(root, home, env, barrier=False) as (second, desc):
            assert second.pid != first_pid and desc['authority_epoch'] > epoch
            asyncio.run(recovered(desc))
        receipt = {'ordinary_daemons': 2, 'first_pid': first_pid, 'second_pid': second.pid,
                   'first_epoch': epoch, 'second_epoch': desc['authority_epoch'],
                   'fifo_human_before_notice': True, 'queued_demand_coalesced': True,
                   'offline_replayed': True, 'passive_note_without_inference': True, 'admissions': len(rows())}
        (base / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
    except BaseException:
        for name in ('first.log', 'restart.log'):
            path = home / name
            if path.exists():
                print(path.read_text()[-18000:], file=sys.stderr)
        raise
    finally:
        peer.release.set()
        peer.shutdown(); peer.server_close()


if __name__ == '__main__':
    probe(Path(sys.argv[1]))

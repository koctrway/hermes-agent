"""封存於入列前／後的真正離線補送；只連 loopback 模型及暫存原生來源。"""
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


def probe(base, stage):
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
    shared, cards = {}, {}
    modes = ('notify', 'wake', 'notify+wake')

    def rows():
        with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM session_admissions ORDER BY seq')]

    def make_cards():
        with closing(kbc.connect()) as conn:
            for mode in modes:
                tid = cards[mode] = kb.create_task(conn, title='archive fixture', assignee='fixture')
                kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id=shared['sid'],
                                   notifier_profile='default', delivery_mode=mode)
                kb.complete_task(conn, tid, summary='ARCHIVE_FINAL_' + mode)

    async def first(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='origin', source='gui', cwd=str(home), toolsets=[])
            assert 'result' in created, created
            shared['sid'] = created['result']['session_id']
        if stage == 'after':
            make_cards()
            await async_wait_until(lambda: len(rows()) == 3, timeout=45, what='three durable queued notices')
            assert all(r['status'] == 'queued' for r in rows()), rows()
            assert peer.requests == []

    async def recovered(desc):
        def settled():
            with closing(kbc.connect()) as conn:
                subs = [s for tid in cards.values() for s in kbn.list_notify_subs(conn, tid)]
                subs += kbn.list_notify_subs(conn, shared['empty'])
            return len(rows()) == 3 and all(r['outcome'] == 'completed' for r in rows()) and not subs
        await async_wait_until(settled, timeout=60, what='offline final replay and receipt-based cleanup')
        async with websocket(home, desc) as ws:
            history = await rpc(ws, 'session.resume', session_id=shared['sid'])
            assert 'result' in history, history
            messages = history['result']['messages']
            for mode in modes:
                assert any('ARCHIVE_FINAL_' + mode in str(m.get('content')) for m in messages), messages
        inputs = [next((m.get('content', '') for m in reversed(r['messages']) if m['role'] == 'user'), '')
                  for r in peer.requests if r.get('messages')]
        assert len(inputs) == 2, inputs
        final_details = []
        for text in inputs:
            # 原生 role alternation 會把尚無 assistant 的被動 note 與下一筆 user 合併。
            states = [json.loads(line.removeprefix('[kanban] ')) for line in text.splitlines()
                      if line.startswith('[kanban] ')]
            assert all(s['status'] == 'archived' and s['phase'] == 'completed' for s in states), states
            final_details.append(states[-1]['detail'])
        assert set(final_details) == {'ARCHIVE_FINAL_wake', 'ARCHIVE_FINAL_notify+wake'}, final_details
        with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as conn:
            assert conn.execute("SELECT count(*) FROM messages WHERE display_kind='internal_notification' "
                                "AND content LIKE '%ARCHIVE_FINAL_notify%'").fetchone()[0] >= 1

    try:
        with daemon(root, home, env, barrier=stage == 'after', fixture='kanban_archive_daemon.py') as (owner, desc):
            first_pid, epoch = owner.pid, desc['authority_epoch']
            asyncio.run(first(desc))
        # 原 owner 已停止：保留原生 queued 與 receipt，不清租約、不補寫 transcript。
        if stage == 'before':
            make_cards()
            assert rows() == []
        with closing(kbc.connect()) as conn:
            for tid in cards.values():
                kb.archive_task(conn, tid)
                assert kbn.list_notify_subs(conn, tid)[0]['chat_id'] == shared['sid']
            empty = shared['empty'] = kb.create_task(conn, title='silent archive fixture', assignee='fixture')
            kbn.add_notify_sub(conn, task_id=empty, platform='local', chat_id=shared['sid'],
                               notifier_profile='default', delivery_mode='wake')
            kb.archive_task(conn, empty)
        with daemon(root, home, env, barrier=False) as (second, desc):
            assert second.pid != first_pid and desc['authority_epoch'] > epoch
            asyncio.run(recovered(desc))
        receipt = {'stage': stage, 'modes': list(modes), 'first_pid': first_pid, 'second_pid': second.pid,
                   'epochs': [epoch, desc['authority_epoch']], 'admissions': len(rows()),
                   'inferences': len(peer.requests), 'final_replayed': True, 'subscriptions_cleaned': True,
                   'empty_archive_silent': True}
        (base / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
    except BaseException:
        for path in home.glob('*.log'):
            print(path.read_text()[-18000:], file=sys.stderr)
        raise
    finally:
        peer.release.set()
        peer.shutdown(); peer.server_close()


if __name__ == '__main__':
    probe(Path(sys.argv[1]), sys.argv[2])

"""離線 LOCAL 訂閱的未投遞事件不應被 retention GC 刪除。"""
from contextlib import closing

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn


def test_gc_retains_unseen_local_event_until_durable_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title='offline fixture', assignee='fixture')
        kbn.add_notify_sub(conn, task_id=tid, platform='local', chat_id='origin', notifier_profile='default')
        kb.complete_task(conn, tid, summary='offline result')
        conn.execute('UPDATE tasks SET created_at=0,completed_at=0 WHERE id=?', (tid,))
        conn.execute('UPDATE task_events SET created_at=0 WHERE task_id=?', (tid,))
        conn.commit()
        assert kbn.purge_stale_done_notify_subs(conn, max_age_days=30) == 0
        cursor, events = kbn.unseen_events_for_sub(conn, task_id=tid, platform='local', chat_id='origin')
        assert events
        kbn.advance_notify_cursor(conn, task_id=tid, platform='local', chat_id='origin', new_cursor=cursor)
        # Cursor 代表 durable 排隊，不代表已執行；unknown 頭可能仍攔住通知。
        assert kbn.purge_stale_done_notify_subs(conn, max_age_days=30) == 0

"""LOCAL 看板通知：durable admission 先於 cursor，沿用唯一 writer。"""
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import re

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from hermes_state_runtime import RuntimeStoreError


def comment_phase(body, status):
    if re.match(r'(?:progress|進度)\s*[:：]', body, re.IGNORECASE):
        return 'progress'
    if status == 'blocked' and re.search(
            r'needs_input|waiting[_ -]?input|等待輸入|(?:只?待|需要|請)\s*Sho|待本人', body, re.IGNORECASE):
        return 'needs_input'
    return None


def comment_version(conn, task_id, after, status):
    selected, previous = None, None
    events = conn.execute("SELECT id,payload,created_at FROM task_events WHERE task_id=? "
                          "AND kind='commented' AND id>? ORDER BY id", (task_id, after))
    for event in events:
        payload = json.loads(event['payload'] or '{}')
        if payload.get('comment_id'):
            rows = conn.execute('SELECT body FROM task_comments WHERE task_id=? AND id=? AND author!=?',
                                (task_id, payload['comment_id'], 'auto-decomposer')).fetchall()
        else:
            # 舊事件沒有 comment_id；只接受可唯一核對的原留言，不猜同秒的多筆需求。
            rows = conn.execute('SELECT body FROM task_comments WHERE task_id=? AND author=? AND created_at=?',
                                (task_id, payload.get('author'), event['created_at'])).fetchall()
        if len(rows) != 1 or payload.get('author', '').strip() == 'auto-decomposer':
            continue
        body = ' '.join(rows[0]['body'].split())
        phase = comment_phase(body, status)
        if phase and (phase, body) != previous:
            selected, previous = (phase, body, event['id']), (phase, body)
    return selected


def snapshot(reference):
    path, task_id, chat_id, profile = reference
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        sub = conn.execute("SELECT delivery_mode FROM kanban_notify_subs WHERE task_id=? AND platform='local' "
                           "AND chat_id=? AND notifier_profile=? AND thread_id=''",
                           (task_id, chat_id, profile)).fetchone()
        task = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not sub or not task or task['status'] == 'archived':
            return None
        from gateway.kanban_watchers_notifier import TERMINAL_KINDS
        kinds = (*TERMINAL_KINDS, 'claimed')
        event = conn.execute('SELECT id,kind,payload FROM task_events WHERE task_id=? AND kind IN ('
                             + ','.join('?' for _ in kinds) + ') ORDER BY id DESC LIMIT 1', (task_id, *kinds)).fetchone()
        payload = json.loads(event['payload'] or '{}') if event else {}
        detail = (payload.get('reason') or payload.get('summary') or payload.get('error')
                  or (task['result'] if task['status'] == 'done' else '') or '')
        phase = ('needs_input' if task['status'] == 'blocked' and payload.get('kind') == 'needs_input'
                 else event['kind'] if event else task['status'])
        version = event['id'] if event else 0
        comment = comment_version(conn, task_id, version, task['status'])
        if comment:
            phase, detail, version = comment
        state = {'task_id': task_id, 'title': task['title'], 'status': task['status'], 'phase': phase,
                 'run_id': task['current_run_id'], 'detail': str(detail)}
    from agent.redact import redact_sensitive_text
    text = redact_sensitive_text(json.dumps(state, ensure_ascii=False, sort_keys=True), force=True)
    return text, sub['delivery_mode'], version


def notice_text(text):
    return ('[kanban] ' + text + '\n這是既有卡片的狀態資料，不是新的業務指令。'
            '請簡短回報目前結果與下一步；needs_input 時提出目前的單一問題，使用者可在原對話直接回答。')


def notice_metadata(metadata):
    if 'kanban_notice' not in metadata:
        return {}
    reference = metadata['kanban_notice']
    if (not isinstance(reference, list) or len(reference) != 4
            or not all(isinstance(v, str) and v for v in reference) or not Path(reference[0]).is_absolute()):
        raise RuntimeStoreError('invalid_params')
    return {'kanban_notice': list(reference)}


async def prepare_execution(authority, ref, row, event):
    reference = event.metadata.get('kanban_notice')
    if reference is None:
        return True
    if event.source.platform != Platform.LOCAL or reference[2] != event.source.chat_id:
        raise RuntimeStoreError('admission_conflict')
    current = await asyncio.to_thread(snapshot, reference)
    if current is not None:
        text, mode, _version = current
        # 只改本回合的副本，不改 committed payload、fingerprint 或既有歷史。
        event.text = notice_text(text)
        if mode != 'notify':
            return True
        from functools import partial
        from gateway.session_runtime_workers import tracked_write
        await tracked_write(authority, partial(authority.db.append_completion_note,
            authority.physical_target(ref), row['admission_id'], event.text, 'internal_notification',
            {'display_text': '[kanban] ' + text[:1900]}))
        refresh = getattr(authority.runner, '_refresh_agent_cache_message_count', None)
        if refresh is not None:
            await refresh(authority.sessions[ref.session_id].route, authority.physical_target(ref))
    authority.pending_results[row['admission_id']] = {'result': {'final_response': '', 'messages': []}, 'usage': {}}
    return False


async def admit_notice(authority, ref, live, reference, current):
    from hermes_state_runtime import find_session_admission, list_session_admissions
    text, mode, version = current
    identity = 'kanban-local:' + hashlib.sha256(json.dumps([reference, mode, version]).encode()).hexdigest()
    principal = 'automation:' + live.route
    existing = await asyncio.to_thread(find_session_admission, authority.db,
        principal_id=principal, session_id=ref.session_id, request_id=identity)
    if existing is not None:
        return  # 同一持久版本的重試，保留首次 payload；執行時另讀最新快照。
    event = MessageEvent(text=notice_text(text), source=live.source, internal=True, message_id=identity,
        metadata={'gateway_session_key': live.route, 'kanban_notice': reference,
                  'gateway_session_id': authority.physical_target(ref),
                  'display_kind': 'internal_notification', 'display_text': '[kanban] ' + text[:1900]})
    adapter = authority.runner._adapter_for_source(live.source)
    rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=ref.session_id)
    replaced = next((r['admission_id'] for r in rows if r['status'] == 'queued'
        and r['principal_id'] == principal
        and r['payload'].get('local_automation_v1', {}).get('kanban_notice') == reference), None)
    await authority.admit_automation(adapter, event, identity, replaces=replaced)


async def deliver(notification):
    from gateway.session_authorities import active_authority
    from gateway.session_local_recovery import restore_local_session
    from hermes_cli import kanban_db as kb
    from gateway.kanban_watchers_notifier import _pin_first, logger
    n, sub = notification, notification.sub
    n.plat = Platform.LOCAL
    try:
        if not sub.get('notifier_profile') or sub.get('thread_id'):
            raise RuntimeStoreError('origin_missing')
        async with n._owner_scope():
            authority = active_authority(n.runner)
            if authority is None:
                raise RuntimeStoreError('runtime_draining')
            from gateway.session_authorities import served_profile_name
            if served_profile_name(authority.profile_id) != sub['notifier_profile']:
                raise RuntimeStoreError('profile_mismatch')
            # 原生 receipt 才能恢復；不收編 legacy、不重建來源。
            ref = restore_local_session(authority, sub['chat_id'])
            live = authority.sessions[ref.session_id]
            with _pin_first():
                reference = [str(kb.kanban_db_path(n.board_slug).resolve()), n.task_id,
                             sub['chat_id'], sub['notifier_profile']]
            current = await asyncio.to_thread(snapshot, reference)
            if current is not None:
                await admit_notice(authority, ref, live, reference, current)
        await n.advance()
        if n.task and n.task.status == 'archived':
            await n.unsub()
    except (RuntimeStoreError, OSError, sqlite3.Error) as exc:
        # LOCAL 離線／協調等待不是死聊天室，保留訂閱與舊 cursor。
        logger.warning('kanban local delivery pending for %s: %s', n.task_id, exc)

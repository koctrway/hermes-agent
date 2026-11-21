"""隔離測試停點：三筆看板 admission commit 後、任何執行開始前暫停 owner。"""
import os
import runpy
import signal

from gateway.session_authority import SessionAuthority
from hermes_state_runtime import list_session_admissions

schedule = SessionAuthority._schedule


def committed_barrier(self, ref):
    pending = list_session_admissions(self.db, session_id=ref.session_id)
    if pending and all('kanban_notice' in r['payload'].get('local_automation_v1', {}) for r in pending):
        if len(pending) == 3:
            os.kill(os.getpid(), signal.SIGSTOP)
        return
    return schedule(self, ref)


SessionAuthority._schedule = committed_barrier
runpy.run_module('gateway.run', run_name='__main__')

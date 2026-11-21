"""隔離真實 daemon／WebSocket／FIFO／loopback 模型；不連使用者對話。"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_local_kanban_busy_and_offline_recovery(tmp_path):
    from tests.gateway.fixtures.local_recovery_probe import child_env
    root = Path(__file__).resolve().parents[2]
    env = child_env()
    env['PYTHONPATH'] = str(root)
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/kanban_local_peer.py'), str(tmp_path)],
                            cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    if target := os.environ.get('HERMES_E2E_ARTIFACTS'):
        import shutil
        out = Path(target)
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_path / 'receipt.json', out / 'kanban-local-receipt.json')
    print(result.stdout)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stage', ['before', 'after'])
def test_archived_final_replays_offline_before_and_after_admission(tmp_path, stage):
    from tests.gateway.fixtures.local_recovery_probe import child_env
    root = Path(__file__).resolve().parents[2]
    env = child_env()
    env['PYTHONPATH'] = str(root)
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/kanban_archive_peer.py'),
                             str(tmp_path), stage], cwd=root, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    if target := os.environ.get('HERMES_E2E_ARTIFACTS'):
        import shutil
        out = Path(target)
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_path / 'receipt.json', out / ('kanban-archive-' + stage + '-receipt.json'))
    print(result.stdout)

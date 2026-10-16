"""Fire a switch in the background for the rollback contention injection."""
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / ''))
target = sys.argv[1]
log = REPO / 'logs' / 'runtime' / ('fa_rb_%s.log' % target)
with open(log, 'w', encoding='utf-8', errors='replace') as f:
    p = subprocess.Popen(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                          '-ExecutionPolicy', 'Bypass', '-File',
                          str(REPO / 'runtime' / 'windows' / 'Switch-Girlfriend-Avatar.ps1'),
                          '-Target', target], stdout=f, stderr=subprocess.STDOUT)
print(json.dumps({'pid': p.pid, 'log': str(log)}) if False else 'launched pid=%d log=%s' % (p.pid, log))
time.sleep(1)

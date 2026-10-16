import psutil, datetime, os, sys
os.chdir(str(__import__('pathlib').Path(__file__).resolve().parents[2] / ''))

def ts(t):
    return datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')

print('== long-lived python/pwsh processes (candidates) ==')
for p in psutil.process_iter(['pid', 'name', 'create_time', 'cmdline']):
    try:
        n = (p.info['name'] or '').lower()
        if n.startswith('python') or n.startswith('pwsh'):
            cl = ' '.join(p.info['cmdline'] or [])[:140]
            low = cl.lower()
            if any(k in low for k in ('ui', 'uvicorn', 'fastapi', 'app:', '7860', '8765', 's2s', 'avatar', 'voice')):
                print("pid=%d start=%s %s" % (p.info['pid'], ts(p.info['create_time']), cl))
    except Exception:
        pass

print()
print('== fix files mtime ==')
for f in ('runtime/ui/app/avatar_admin.py', 'runtime/ui/app/pid_liveness.py',
          'runtime/ui/app/voice_models_admin.py'):
    print(f, ts(os.path.getmtime(f)))

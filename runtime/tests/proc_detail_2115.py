import psutil, datetime

def ts(t):
    return datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')

for pid in (38116, 43444, 46536, 27108, 47264):
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            print('pid=%d start=%s' % (pid, ts(p.create_time())))
            print('  exe=%s' % p.exe())
            print('  cmd=%s' % ' '.join(p.cmdline()))
            print('  cwd=%s' % p.cwd())
    except psutil.NoSuchProcess:
        print('pid=%d DEAD' % pid)
    except Exception as e:
        print('pid=%d ERR %s' % (pid, e))

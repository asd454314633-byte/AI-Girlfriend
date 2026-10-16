import psutil, datetime
os_ns = __import__('os')

def ts(t):
    return datetime.datetime.fromtimestamp(t).strftime('%H:%M:%S')

print('== all python/pwsh processes ==')
for p in psutil.process_iter(['pid', 'name', 'create_time']):
    try:
        n = (p.info['name'] or '').lower()
        if n.startswith(('python', 'pwsh')):
            print('pid=%d start=%s name=%s' % (p.info['pid'], ts(p.info['create_time']), p.info['name']))
    except Exception:
        pass

print()
print('== listeners on stack ports ==')
for c in psutil.net_connections(kind='inet'):
    if c.laddr and c.laddr.port in (7860, 8765, 8090, 50000, 18765, 8010) and c.status == psutil.CONN_LISTEN:
        try:
            p = psutil.Process(c.pid)
            print('port %d pid %d %s start=%s' % (c.laddr.port, c.pid, p.name(), ts(p.create_time())))
        except Exception as e:
            print('port %d pid %d ERR %s' % (c.laddr.port, c.pid, e))

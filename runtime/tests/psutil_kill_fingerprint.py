import os, sys, time, psutil, multiprocessing

# Sacrificial child: sleeps, prints nothing. Parent kills it with psutil and
# reads the exit code back through a handle taken while it was alive.
def child():
    time.sleep(60)

if __name__ == '__main__':
    ctx = multiprocessing.get_context('spawn')
    p = ctx.Process(target=child)
    p.start()
    time.sleep(1.5)
    q = psutil.Process(p.pid)
    h = q._handle if hasattr(q, '_handle') else None
    q.kill()
    p.join(10)
    print('psutil kill -> child exitcode=%r (0x%X)' % (p.exitcode, p.exitcode & 0xFFFFFFFF if isinstance(p.exitcode, int) else 0))
    import ctypes
    print('psutil version', psutil.__version__)

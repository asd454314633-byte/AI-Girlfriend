"""Minimal CDP driver for the final-acceptance browser (port 9223)."""
import asyncio
import json
import urllib.request

CDP_HTTP = 'http://127.0.0.1:9223'
PAGE_URL_HINT = '127.0.0.1:7860'


def list_targets():
    with urllib.request.urlopen(CDP_HTTP + '/json', timeout=5) as r:
        return json.load(r)


class Page:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending = {}
        self._events = []

    @classmethod
    async def attach(cls, url_hint=PAGE_URL_HINT, create_if_missing=True):
        import websockets
        connect = getattr(websockets, 'connect', None)
        if connect is None:
            from websockets.asyncio.client import connect  # noqa
            connect = websockets.asyncio.client.connect
        targets = list_targets()
        page = next((t for t in targets if t.get('type') == 'page' and url_hint in t.get('url', '')), None)
        if page is None:
            if not create_if_missing:
                raise RuntimeError('page target not found: ' + url_hint)
            # Chrome 111+ requires PUT for /json/new; older builds only GET.
            new_url = CDP_HTTP + '/json/new?' + 'http://' + url_hint + '/'
            page = None
            for method in ('PUT', 'GET'):
                try:
                    req = urllib.request.Request(new_url, method=method)
                    with urllib.request.urlopen(req, timeout=10) as r:
                        page = json.load(r)
                    break
                except urllib.error.HTTPError as e:
                    if e.code != 405:
                        raise
            if page is None:
                raise RuntimeError('cannot create CDP tab (PUT and GET both rejected)')
        ws = await connect(page['webSocketDebuggerUrl'], max_size=50 * 1024 * 1024)
        p = cls(ws)
        await p._send('Runtime.enable')
        await p._send('Page.enable')
        return p

    async def _send(self, method, params=None, session_wait=True):
        self._id += 1
        mid = self._id
        msg = {'id': mid, 'method': method}
        if params:
            msg['params'] = params
        await self.ws.send(json.dumps(msg))
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if data.get('id') == mid:
                if 'error' in data:
                    raise RuntimeError('%s: %s' % (method, data['error']))
                return data.get('result', {})
            # ignore events (or store)
            self._events.append(data)
            if len(self._events) > 200:
                del self._events[:100]

    async def eval_js(self, expr, await_promise=True):
        res = await self._send('Runtime.evaluate', {
            'expression': expr, 'awaitPromise': await_promise,
            'returnByValue': True, 'userGesture': True, 'timeout': 30000,
        })
        r = res.get('result', {})
        if r.get('subtype') == 'error' or res.get('exceptionDetails'):
            raise RuntimeError('eval error: %s' % json.dumps(res.get('exceptionDetails') or r)[:400])
        return r.get('value')

    async def navigate(self, url):
        await self._send('Page.navigate', {'url': url})

    async def close(self):
        await self.ws.close()


def run(coro):
    return asyncio.run(coro)

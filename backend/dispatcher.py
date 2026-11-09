"""Single durable wake reservation; never hold database locks during HTTP."""
import json
import threading
import http.client
import socket
import time

DISPATCH_URL = 'http://n8n.automation.svc.cluster.local/webhook/spi-incident-triage-dispatch'

class Dispatcher:
    def __init__(self, store, token, enabled=False, post=None, busy=None):
        self.store=store
        self.token=token
        self.enabled=enabled
        self.post=post or self._post
        self.busy=busy or (lambda:False)
        self.wake=threading.Event()
        self.stopped=threading.Event()
        self.thread=None

    def _post(self):
        # Fixed internal target, no redirects or proxy credential forwarding.
        deadline=time.monotonic()+10
        connection=http.client.HTTPConnection('n8n.automation.svc.cluster.local',80,timeout=10)
        timer=None
        try:
            connection.connect()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Dispatch deadline')
            sock=connection.sock
            def expire():
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            # Socket inactivity timeout alone does not stop trickling headers.
            timer=threading.Timer(remaining,expire)
            timer.daemon=True
            timer.start()
            connection.request('POST','/webhook/spi-incident-triage-dispatch',body=b'{}',
                headers={'X-Worker-Token':self.token,'Content-Type':'application/json'})
            response=connection.getresponse()
            if time.monotonic()>=deadline:raise TimeoutError('Dispatch deadline')
            if not 200<=response.status<300:raise OSError('Dispatch rejected')
        finally:
            if timer:timer.cancel()
            connection.close()

    def signal(self):
        self.wake.set()

    def tick(self):
        if not self.enabled or self.busy():return None
        reservation, deadline=self.store.reserve_dispatch()
        if reservation:
            try:
                self.post()
                event='dispatch_sent'
            except Exception:
                # A failed response does not prove that n8n did not start.
                event='dispatch_unconfirmed'
            print(json.dumps({'component':'spi-triage','event':event}),flush=True)
            # Claim can acknowledge while POST is in flight. Never recreate it.
            return 0
        return deadline

    def _run(self):
        import time
        while not self.stopped.is_set():
            self.wake.clear()
            try:deadline=self.tick()
            except Exception:
                print(json.dumps({'component':'spi-triage','event':'dispatch_internal_error'}),flush=True)
                deadline=time.time()+30
            timeout=None if deadline is None else max(0.05,deadline-time.time())
            self.wake.wait(timeout)

    def start(self):
        if self.enabled and self.thread is None:
            self.thread=threading.Thread(target=self._run, name='spi-dispatch', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopped.set();self.signal()
        if self.thread:self.thread.join(timeout=12)

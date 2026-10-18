"""Meter every SDK HTTP attempt, including login, lookups and token refresh.

The pinned SDK is synchronous; waiting here occurs in its worker thread. No
SQLite lock is held while sleeping or making a request. Server feedback always
extends, and never shortens, the shared schedule.
"""
from __future__ import annotations

import re
import time
import threading
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

CURRENT_ACTION=ContextVar('ctn_current_action',default=None)

def full_retry_after(header):
    """Pinned SDK truncates at 120s; preserve the server's complete delay."""
    try:
        return max(0.,float(header))
    except (TypeError,ValueError):
        try: return max(0.,parsedate_to_datetime(header).timestamp()-time.time())
        except (TypeError,ValueError,OverflowError): return 10.


def route_bucket(method, path):
    path=urlsplit(path).path
    if path in ('/auth/login','/auth/register'):
        return path,5/60
    if method=='GET':
        if path=='/posts':
            return 'feed',10/60
        if re.fullmatch(r'/tags/\d+/posts',path):
            return 'tag_posts',10/60
        return 'read:'+re.sub(r'/\d+(?=/|$)','/:id',path),300/60
    if method=='POST' and path=='/posts':
        return 'publish',3/60
    if path.endswith('/report'):
        return ('account_report' if path.startswith('/accounts/') else 'post_report'),3/60
    if path.endswith(('/like','/follow','/view')) or (method=='DELETE' and re.fullmatch(r'/posts/\d+',path)):
        return method+':'+path.rsplit('/',1)[-1] if not path.rsplit('/',1)[-1].isdigit() else 'delete_post',30/60
    return method+':'+re.sub(r'/\d+(?=/|$)','/:id',path),1.


class HttpPacer:
    def __init__(self, store, rate, budget, *, clock=time.time, sleep=time.sleep):
        self.store,self.rate,self.budget=store,rate,budget
        self.clock,self.sleep=clock,sleep
        self._closed=threading.Event()

    def close(self):
        self._closed.set()

    def _wait(self, delay):
        if self.sleep is time.sleep:
            if self._closed.wait(delay):
                from .base import PlatformError
                raise PlatformError('client shutting down',retryable=False,status=409)
        else:
            self.sleep(delay)

    def attach(self, obj):
        from capturethenarrative import _http
        _http._retry_after=full_retry_after
        if getattr(obj,'_ctn_http_metered',False):
            return obj
        obj._ctn_http_metered=True
        original=obj._request

        def request(method,path,body=None):
            kind='read' if method=='GET' else 'write'
            bucket,rate=route_bucket(method,path)
            cap=int(self.budget.total_requests*self.budget.read_share)
            if kind=='write':
                cap=self.budget.total_requests-cap
            buckets=[('route:'+bucket,rate),('team:'+kind,
                self.rate.read_rps if kind=='read' else self.rate.write_rps)]
            account=getattr(obj,'username',None) or getattr(obj,'account_id',None)
            if account:
                buckets.append(('account:'+str(account)+':'+kind,
                    self.rate.per_account_read_rps if kind=='read' else self.rate.per_account_write_rps))
            try:
                reservation,delay=self.store.reserve_http_request(buckets,kind=kind,cap=cap,now=self.clock())
            except RuntimeError as exc:
                # Nothing was sent: report a definite local failure, never an uncertain send.
                from .base import PlatformError
                raise PlatformError('local '+str(exc),retryable=False,status=409) from None
            try:
                if delay>0: self._wait(delay)
                while True:
                    blocked=max(self.store.request_blocked_until(k) for k,_ in buckets)-self.clock()
                    if blocked<=0: break
                    self._wait(self.store.reschedule_http_request(reservation,buckets,now=self.clock()))
                ident=CURRENT_ACTION.get()
                if ident and method!='GET' and not path.startswith('/auth/'):
                    ready,reason=self.store.guard_wire_action(ident,now=self.clock())
                    if not ready:
                        from .base import PlatformError
                        raise PlatformError(reason,retryable=False,status=409)
                while True:
                    ident=CURRENT_ACTION.get()
                    if ident and method!='GET' and not path.startswith('/auth/'):
                        ready,reason=self.store.guard_wire_action(ident,now=self.clock())
                        if not ready:
                            from .base import PlatformError
                            raise PlatformError(reason,retryable=False,status=409)
                    if method!='GET' and self._closed.is_set():
                        from .base import PlatformError
                        raise PlatformError('client shutting down',retryable=False,status=409)
                    action=ident if ident and method!='GET' and not path.startswith('/auth/') else None
                    sent,wait=self.store.try_mark_http_sent(reservation,cap=cap,now=self.clock(),action=action)
                    if sent: break
                    if isinstance(wait,str):
                        from .base import PlatformError
                        raise PlatformError(wait,retryable=False,status=409)
                    if wait is None:
                        from .base import PlatformError
                        raise PlatformError('local '+kind+' request budget exhausted before send',retryable=False,status=409)
                    self._wait(wait)
            except BaseException:
                self.store.cancel_http_reservation(reservation)
                raise
            try:
                return original(method,path,body=body)
            except Exception as exc:
                retry=getattr(exc,'retry_after',None)
                if retry is not None:
                    self.store.block_request_bucket('route:'+bucket,self.clock()+float(retry))
                raise
            finally:
                limit=getattr(obj,'rate_limit',None)
                if limit is not None and limit.remaining==0 and limit.limit>0:
                    self.store.block_request_bucket('route:'+bucket,self.clock()+limit.reset/limit.limit)

        obj._request=request
        factory=obj._account
        def account(session):
            return self.attach(factory(session))
        obj._account=account
        return obj

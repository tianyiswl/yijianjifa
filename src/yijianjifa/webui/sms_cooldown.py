"""Per-account SMS spacing, retained across tasks and client restarts.

Only a hashed local account identifier and a deadline are stored, never codes.
Reserve before clicking: a timeout cannot prove the platform did not send SMS.
"""
import hashlib
import json
import math
import time
from sqlalchemy import text


class SMSCooldownActive(ValueError):
    def __init__(self,remaining):
        super().__init__(f'验证码获取需间隔60秒，请等待{remaining}秒后再次获取。')


class SMSCooldown:
    def __init__(self,engine,*,clock=None):
        self.engine=engine;self.clock=clock or time.time

    @staticmethod
    def _key(account):
        return 'publish.sms.cooldown:'+hashlib.sha256(('douyin:'+account).encode()).hexdigest()

    def remaining(self,account):
        with self.engine.connect() as c:
            raw=c.execute(text('SELECT value_json FROM settings WHERE key=:key'),{'key':self._key(account)}).scalar()
        return max(0,math.ceil(float(json.loads(raw))-self.clock())) if raw else 0

    def _record(self,account,seconds,*,reserve):
        with self.engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                key=self._key(account);now=self.clock()
                raw=c.execute(text('SELECT value_json FROM settings WHERE key=:key'),{'key':key}).scalar()
                previous=float(json.loads(raw)) if raw else 0
                if reserve and previous>now:raise SMSCooldownActive(math.ceil(previous-now))
                deadline=max(previous,now+seconds)
                c.execute(text('INSERT INTO settings(key,value_json,updated_at) VALUES(:key,:value,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP'),{'key':key,'value':json.dumps(deadline)})
                c.commit()
            except BaseException:c.rollback();raise

    def reserve(self,account):self._record(account,60,reserve=True)

    def observe(self,account,seconds):self._record(account,max(0,seconds),reserve=False)

    def wait(self,account,*,cancelled,pause,on_wait=lambda _:None):
        while not cancelled():
            remaining=self.remaining(account)
            if not remaining:return True
            on_wait(remaining)
            pause(min(1,remaining))
        return False

"""Durable jobs for the local single worker; never run an email send as a retryable job."""
import json
from .db import now


class JobQueue:
    def __init__(self,db):self.db=db

    def enqueue(self,kind,key,payload):
        with self.db.transaction():
            self.db.execute('INSERT INTO jobs(kind,idempotency_key,payload,created_at) VALUES(?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING',
                (kind,key,json.dumps(payload,ensure_ascii=False),now()))
            return self.db.one('SELECT * FROM jobs WHERE idempotency_key=?',(key,))

    def run(self,jid,handler):
        with self.db.transaction():
            row=self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))
            if not row:raise LookupError('작업을 찾을 수 없습니다.')
            if row['status']!='PENDING':return row
            self.db.execute("UPDATE jobs SET status='RUNNING',attempts=attempts+1,started_at=? WHERE id=?",(now(),jid))
        try:
            result=handler(json.loads(row['payload']))
            with self.db.transaction():self.db.execute("UPDATE jobs SET status='COMPLETED',result=?,finished_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),now(),jid))
        except Exception:
            with self.db.transaction():self.db.execute("UPDATE jobs SET status='FAILED',error='처리 실패. 입력과 연결 상태를 확인하세요.',finished_at=? WHERE id=?",(now(),jid))
            raise
        return self.db.one('SELECT * FROM jobs WHERE id=?',(jid,))

"""Hosted-demo seed: sheet exports -> compact gzip/base64 env value -> campaigns and leads on cold start.

Serverless storage resets, so the demo reloads these rows whenever the DB is empty. The value lives in an
encrypted env var, never in git. Build it with:
  python -m ax.seed OUT.txt --pipeline 04.csv --history 09.csv --tax TAX_EXPORT.csv --partner PARTNER_EXPORT.csv
"""
import base64
import csv
import gzip
import io
import json
import re
from urllib.parse import urlparse
from .sheet_import import import_snapshot,read_csv,PIPELINE_COLUMNS,HISTORY_COLUMNS

TAX_KEEP=('전문분야','개인화근거','맞춤협업가설','발송준비상태','출처등급','근거URL','제목','진행 상태')


def rows(text):
    return [r for r in csv.DictReader(io.StringIO(text.lstrip('﻿'))) if any((v or '').strip() for v in r.values())]


def tax_campaign(text):
    pipeline=[]
    for r in rows(text):
        if not (r.get('번호') or '').strip() or not (r.get('대상') or '').strip():continue
        row={'리드ID':'TAX-'+r['번호'].strip(),'받는 사람':r['대상'].strip(),'회사/브랜드':(r.get('회사') or '').strip(),
             '이메일':(r.get('이메일') or '').strip(),'업종':(r.get('전문분야') or '').strip()}
        row.update({k:(r.get(k) or '').strip() for k in TAX_KEEP if (r.get(k) or '').strip()})
        pipeline.append(row)
    return {'campaign':'세무사 70명 아웃바운드 (2026-10-30)','product':'세무 고객관리 AI 협업',
            'value_prop':'세무사 고객 응대·관리 업무를 AI로 줄이는 공동 프로그램','pipeline':pipeline}


def partner_campaign(text):
    latest={}
    for r in rows(text):
        name=(r.get('이름') or '').strip();phone=re.sub(r'\D','',r.get('연락처') or '')
        # Landing-form smoke tests are not leads.
        if not name or name.startswith('[테스트]') or '점검' in name or phone=='01000000000':continue
        latest[name]=r
    pipeline=[]
    for name,r in latest.items():
        link=(r.get('채널·포트폴리오 링크') or '').replace(' ','').strip()
        submitted=re.sub(r'\D','',r.get('접수 시각') or '')
        pipeline.append({'리드ID':'PTN-'+submitted,'받는 사람':name,'회사/브랜드':urlparse(link).netloc or '개인 지원자','이메일':'',
            '업종':(r.get('전문 업종') or '').strip(),'실무 경력':(r.get('실무 경력') or '').strip(),'채널':link,
            '한 줄 소개':' '.join((r.get('한 줄 소개') or '').split()),'접수 시각':(r.get('접수 시각') or '').strip()})
    return {'campaign':'파트너 지원 인바운드 (랜딩 신청)','product':'모범AI 파트너 프로그램',
            'value_prop':'전문가의 AI 수익화·팀 세팅·브랜딩 지원','pipeline':pipeline}


def encode(campaigns):
    return base64.b64encode(gzip.compress(json.dumps(campaigns,ensure_ascii=False).encode(),mtime=0)).decode()


def load(wf,new_campaign,value):
    for c in json.loads(gzip.decompress(base64.b64decode(value))):
        cid=new_campaign(c['campaign'],c['product'],c['value_prop'])
        import_snapshot(wf,{'pipeline':c['pipeline'],'history':c.get('history',[])},cid)


def sheet_campaign(pipeline,history=''):
    return {'campaign':'통합 시트 9명 (V2 발송 완료)','product':'모범AI 커머스 AX','value_prop':'판매 데이터 기반 AI 업무 자동화',
            'pipeline':read_csv(pipeline,PIPELINE_COLUMNS),'history':read_csv(history,HISTORY_COLUMNS) if history else []}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('out');parser.add_argument('--pipeline');parser.add_argument('--history')
    parser.add_argument('--tax');parser.add_argument('--partner')
    args=parser.parse_args()
    read=lambda p:open(p,encoding='utf-8-sig').read() if p else ''
    campaigns=([sheet_campaign(read(args.pipeline),read(args.history))] if args.pipeline else [])+\
        ([tax_campaign(read(args.tax))] if args.tax else [])+([partner_campaign(read(args.partner))] if args.partner else [])
    value=encode(campaigns)
    open(args.out,'w',encoding='ascii').write(value)
    print({c['campaign']:len(c['pipeline']) for c in campaigns},'bytes',len(value))

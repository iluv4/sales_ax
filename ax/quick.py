"""Quick outreach: web search for people -> saved list -> brochure-aware personalized email -> Gmail draft.

One screen (/quick), one AI key. Rows and the brochure live in the same sealed store as the Google token
(local files, or Vercel Blob when hosted) because the hosted SQLite file resets on every cold start.
Nothing is ever sent: the last step only creates a Gmail draft the operator reviews and schedules in Gmail.
"""
import base64
import csv
import io
import json
import os
import secrets
from datetime import datetime,timezone
from fastapi import HTTPException,UploadFile,File
from fastapi.responses import Response
from pydantic import BaseModel,Field
from pypdf import PdfReader
from .gmail import mime_message,GmailDraftClient

ROWS='quick-rows.json'
BROCHURE='quick-brochure.json'
CANDIDATES={'type':'object','additionalProperties':False,'required':['candidates'],'properties':{'candidates':{'type':'array','items':{
    'type':'object','additionalProperties':False,
    'required':['name','instagram','followers','email','website','category','evidence','source_url'],
    'properties':{'name':{'type':'string'},'instagram':{'type':'string'},'followers':{'type':['integer','null']},
        'email':{'type':'string'},'website':{'type':'string'},'category':{'type':'string'},
        'evidence':{'type':'string'},'source_url':{'type':'string'}}}}}}
EMAIL={'type':'object','additionalProperties':False,'required':['subject','body'],
       'properties':{'subject':{'type':'string'},'body':{'type':'string'}}}
OFFER=('모범AI(크레토비)는 각 업계 전문가와 함께 대표님 대상 AI 실무 강의를 만듭니다. 1달 4회·회당 4시간 과정이며, '
       '강의 기획·상세페이지·광고·판매를 저희가 맡고 강사님이 부담하는 비용은 없습니다. 지금 모집 중인 분야: '
       '인플루언서 마케팅, 유통, 해외구매대행, 메타 광고, 세무, 보험, 학원.')


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')


class OpenAIWriter:
    """Responses API with optional web search; output constrained to a JSON schema."""
    def __init__(self):
        from openai import OpenAI
        self.client=OpenAI();self.model=os.getenv('SALES_AX_OPENAI_MODEL','gpt-5')
    def __call__(self,prompt,schema,name,web=False):
        response=self.client.responses.create(model=self.model,input=prompt,reasoning={'effort':'low'},
            tools=[{'type':'web_search'}] if web else [],
            text={'format':{'type':'json_schema','name':name,'schema':schema,'strict':True}})
        return json.loads(response.output_text)


class Search(BaseModel):
    query:str=Field(min_length=2,max_length=300)
    count:int=Field(default=8,ge=1,le=15)
    max_followers:int|None=Field(default=10000,ge=0)


class Edit(BaseModel):
    email:str|None=None
    subject:str|None=None
    body:str|None=None


def register(app,google,gmail_factory,writer_factory=None):
    store=google.store
    # app.state.quick_writer lets tests swap in a fake model without touching the network.
    writer_factory=writer_factory or (lambda:getattr(app.state,'quick_writer',None) or (OpenAIWriter() if os.getenv('OPENAI_API_KEY') else None))

    def load(name,default):
        sealed=store.read(name)
        return google.unseal(sealed) if sealed else default
    def rows():return load(ROWS,[])
    def save(items):store.write(ROWS,google.seal(items))
    def row(items,rid):
        found=next((r for r in items if r['id']==rid),None)
        if not found:raise LookupError('리드를 찾을 수 없습니다.')
        return found
    def writer():
        w=writer_factory()
        if not w:raise HTTPException(503,'AI 키(OPENAI_API_KEY)가 설정되지 않았습니다.')
        return w
    def ai(call,*args,**kwargs):
        try:return call(*args,**kwargs)
        except HTTPException:raise
        except Exception as exc:raise HTTPException(502,'AI 호출 실패: '+type(exc).__name__+' '+str(exc)[:200])

    @app.get('/api/quick/state')
    def state():
        brochure=load(BROCHURE,None)
        return {'rows':rows(),'brochure':brochure and {'filename':brochure['filename'],'chars':len(brochure['text'])},
                'ai_configured':bool(writer_factory()),'google_connected':google.has_token(),'send_enabled':False}

    @app.post('/api/quick/search')
    def search(data:Search):
        limit='' if data.max_followers is None else f' 팔로워가 {data.max_followers:,}명 이하인 계정만 포함하세요.'
        prompt=(f'다음 조건에 맞는 한국의 실제 인물(전문가·인플루언서·사업가)을 웹에서 찾아 {data.count}명까지 알려주세요: "{data.query}".{limit}\n'
                '각 사람마다 인스타그램 아이디(@ 없이), 대략의 팔로워 수, 공개된 연락 이메일, 웹사이트, 분야, '
                '연락할 근거(최근 활동·강점 한두 문장), 근거를 확인한 출처 URL을 적으세요. '
                '이메일은 프로필·웹사이트·공식 문서에 실제로 공개된 것만 적고, 찾지 못하면 빈 문자열로 두세요. 추측해서 만들지 마세요.')
        found=ai(writer(),prompt,CANDIDATES,'candidates',web=True)['candidates']
        items=rows();seen={(r['instagram'] or '').lower() for r in items}|{(r['email'] or '').lower() for r in items}
        added=[]
        for c in found:
            handle=c['instagram'].strip().lstrip('@');email=c['email'].strip()
            if not c['name'].strip() or (handle and handle.lower() in seen) or (email and email.lower() in seen):continue
            if data.max_followers is not None and c['followers'] and c['followers']>data.max_followers:continue
            item={'id':secrets.token_hex(4),'name':c['name'].strip(),'instagram':handle,'followers':c['followers'],'email':email,
                  'website':c['website'].strip(),'category':c['category'].strip(),'evidence':c['evidence'].strip(),
                  'source_url':c['source_url'].strip(),'query':data.query,'status':'발굴','subject':'','body':'',
                  'gmail_draft_id':'','created_at':now(),'updated_at':now()}
            items.append(item);added.append(item);seen|={handle.lower(),email.lower()}-{''}
        save(items)
        return {'added':added,'skipped':len(found)-len(added),'total':len(items)}

    @app.post('/api/quick/brochure')
    async def brochure(file:UploadFile=File(...)):
        content=await file.read(10*1024*1024+1)
        if len(content)>10*1024*1024:raise ValueError('소개서 PDF는 10MB 이하여야 합니다.')
        if not content.startswith(b'%PDF'):raise ValueError('PDF 파일만 올릴 수 있습니다.')
        try:text='\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(content)).pages)
        except Exception:raise ValueError('PDF를 읽을 수 없습니다.')
        filename=os.path.basename(file.filename or 'brochure.pdf')
        store.write(BROCHURE,google.seal({'filename':filename,'text':text[:20000],'pdf':base64.b64encode(content).decode()}))
        return {'filename':filename,'chars':len(text)}

    @app.patch('/api/quick/rows/{rid}')
    def edit(rid:str,data:Edit):
        items=rows();item=row(items,rid)
        for key,value in data.model_dump(exclude_none=True).items():item[key]=value.strip() if key!='body' else value
        item['updated_at']=now();save(items);return item

    @app.delete('/api/quick/rows/{rid}')
    def remove(rid:str):
        items=rows();row(items,rid);save([r for r in items if r['id']!=rid]);return {'deleted':rid}

    @app.post('/api/quick/rows/{rid}/draft')
    def draft(rid:str):
        items=rows();item=row(items,rid);brochure=load(BROCHURE,None)
        sender=os.getenv('SALES_AX_SENDER_NAME','모범AI 파트너십 담당 한현민')
        prompt=('아래 사람에게 보낼 한국어 첫 연락 이메일을 써주세요. 강사 섭외 제안입니다.\n'
                f'[받는 사람] {json.dumps({k:item[k] for k in ("name","instagram","followers","category","evidence","website","source_url")},ensure_ascii=False)}\n'
                f'[우리 제안] {OFFER}\n'
                f'[소개서 내용]\n{(brochure or {}).get("text","(소개서 없음)")[:6000]}\n'
                f'[작성 규칙] 첫 문장은 "{item["name"]}님 안녕하세요"로 시작하고, 둘째 문장에서 근거에 있는 구체적인 활동을 언급하며 왜 연락했는지 밝히세요. '
                '소개서의 핵심 1~2가지만 짧게 전하고, 15분 통화를 제안하며 끝내세요. 본문은 600자 안팎, 과장·허위 없이, '
                f'서명은 "{sender} 드림". 소개서 PDF가 첨부된다고 한 줄 언급하세요. 제목은 30자 안팎.')
        result=ai(writer(),prompt,EMAIL,'email')
        item.update(subject=result['subject'].strip(),body=result['body'].strip(),status='메일 작성됨',updated_at=now())
        save(items);return item

    @app.post('/api/quick/rows/{rid}/gmail')
    def gmail(rid:str):
        items=rows();item=row(items,rid)
        if not item['email']:raise ValueError('이메일 주소가 없습니다. 표에서 이메일을 먼저 입력하세요.')
        if not item['subject'] or not item['body']:raise ValueError('메일을 먼저 작성하세요.')
        try:client=gmail_factory();account=client.account()
        except Exception:raise HTTPException(503,'Gmail 연결이 필요합니다. 설정 화면에서 회사 Google 계정을 연결하세요.')
        brochure=load(BROCHURE,None)
        pdf={'filename':brochure['filename'],'content':base64.b64decode(brochure['pdf'])} if brochure else None
        made=client.upsert(item['gmail_draft_id'] or None,mime_message(account,item['email'],item['subject'],item['body'],pdf))
        item.update(gmail_draft_id=made['id'],status='Gmail 초안',updated_at=now());save(items)
        return item|{'gmail_url':'https://mail.google.com/mail/u/?authuser='+account+'#drafts','sent':False}

    @app.get('/api/quick/rows.csv')
    def export():
        out=io.StringIO();fields=['status','name','instagram','followers','email','website','category','evidence','source_url','subject','body','query','created_at']
        writer_=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');writer_.writeheader();writer_.writerows(rows())
        return Response('﻿'+out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="quick-leads.csv"'})

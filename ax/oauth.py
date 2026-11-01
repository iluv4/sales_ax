"""Single-operator loopback OAuth with PKCE and encrypted local token storage."""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow

SCOPES=['https://www.googleapis.com/auth/gmail.compose','https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/calendar.readonly','https://www.googleapis.com/auth/spreadsheets.readonly']
REDIRECT='http://127.0.0.1:8765/api/integrations/google/callback'


class GoogleConnection:
    def __init__(self,directory,flow_factory=None,account_reader=None):
        self.directory=Path(directory)
        self.pending={};self.lock=threading.RLock()
        self.flow_factory=flow_factory or Flow.from_client_config
        self.account_reader=account_reader

    @property
    def client_path(self):return self.directory/'oauth-client.json'
    @property
    def token_path(self):return self.directory/'google-token.enc'

    def key(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/'local-token.key'
        if not path.exists():
            with path.open('xb') as f:f.write(Fernet.generate_key())
            try:os.chmod(path,0o600)
            except OSError:pass
        return path.read_bytes()

    def configure(self,payload):
        config=json.loads(payload)
        if not isinstance(config,dict):raise ValueError('OAuth 클라이언트 JSON이 필요합니다.')
        section=config.get('web') or config.get('installed')
        if not section or not section.get('client_id') or not section.get('client_secret'):
            raise ValueError('Google Cloud에서 내려받은 OAuth 클라이언트 JSON이 필요합니다.')
        # Pin endpoints: an uploaded JSON cannot redirect token/secret exchange to another service.
        if section.get('auth_uri')!='https://accounts.google.com/o/oauth2/auth' or section.get('token_uri')!='https://oauth2.googleapis.com/token':
            raise ValueError('Google 공식 OAuth 엔드포인트만 허용합니다.')
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.client_path
        path.write_bytes(Fernet(self.key()).encrypt(json.dumps(config).encode()))
        try:os.chmod(path,0o600)
        except OSError:pass
        return {'configured':True,'redirect_uri':REDIRECT,'secrets_returned':False}

    def begin(self):
        if not self.client_path.exists():raise ValueError('OAuth 클라이언트 JSON을 먼저 등록하세요.')
        config=json.loads(Fernet(self.key()).decrypt(self.client_path.read_bytes()))
        flow=self.flow_factory(config,scopes=SCOPES,autogenerate_code_verifier=True)
        flow.redirect_uri=REDIRECT
        url,state=flow.authorization_url(access_type='offline',prompt='consent',include_granted_scopes='true',login_hint='marketing@creatobi.com')
        browser_secret=secrets.token_urlsafe(32)
        with self.lock:
            self.pending={k:v for k,v in self.pending.items() if v['expires']>time.monotonic()}
            self.pending[state]={'flow':flow,'browser_hash':hashlib.sha256(browser_secret.encode()).hexdigest(),'expires':time.monotonic()+600}
        return url,browser_secret

    def finish(self,state,code,browser_secret):
        with self.lock:item=self.pending.pop(state,None)
        if not item or item['expires']<time.monotonic() or not browser_secret or not secrets.compare_digest(item['browser_hash'],hashlib.sha256(browser_secret.encode()).hexdigest()):
            raise ValueError('OAuth 요청이 만료되었거나 브라우저 확인에 실패했습니다. 다시 연결하세요.')
        flow=item['flow'];flow.fetch_token(code=code)
        creds=flow.credentials
        if self.account_reader:account=self.account_reader(creds)
        else:
            from googleapiclient.discovery import build
            account=build('gmail','v1',credentials=creds,cache_discovery=False).users().getProfile(userId='me').execute()['emailAddress']
        expected=os.getenv('SALES_AX_GOOGLE_ACCOUNT','marketing@creatobi.com')
        if account.lower()!=expected.lower():raise ValueError('회사 계정 '+expected+'으로 연결해야 합니다. 토큰을 저장하지 않았습니다.')
        envelope={'credentials':json.loads(creds.to_json()),'account':account.lower(),'connected_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
        self.token_path.write_bytes(Fernet(self.key()).encrypt(json.dumps(envelope).encode()))
        try:os.chmod(self.token_path,0o600)
        except OSError:pass
        return account

    def credentials(self):
        if not self.token_path.exists():raise ValueError('Google 계정을 먼저 연결하세요.')
        envelope=json.loads(Fernet(self.key()).decrypt(self.token_path.read_bytes()))
        creds=Credentials.from_authorized_user_info(envelope['credentials'])
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            envelope['credentials']=json.loads(creds.to_json())
            self.token_path.write_bytes(Fernet(self.key()).encrypt(json.dumps(envelope).encode()))
        if not creds.valid:raise ValueError('Google 연결이 만료되었습니다. 다시 연결하세요.')
        return creds

    def status(self):
        account=None;connected_at=None
        if self.token_path.exists():
            try:
                env=json.loads(Fernet(self.key()).decrypt(self.token_path.read_bytes()))
                account=env['account'];connected_at=env['connected_at']
            except Exception:pass
        return {'client_configured':self.client_path.exists(),'token_stored':bool(account),'account':account,
                'connected_at':connected_at,'live_verified':False,'scopes':SCOPES,'redirect_uri':REDIRECT,
                'state':'MANUAL' if account else 'BLOCKED','send_enabled':False}

"""Single-operator Google OAuth with PKCE and encrypted token storage (local files, or Vercel Blob when hosted)."""
import hashlib
import json
import os
import secrets
import time
from datetime import datetime,timezone
from pathlib import Path
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow

SCOPES=['https://www.googleapis.com/auth/gmail.compose','https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/calendar.readonly','https://www.googleapis.com/auth/spreadsheets.readonly']
LOCAL_REDIRECT='http://127.0.0.1:8765/api/integrations/google/callback'
PENDING_SECONDS=600


def redirect_uri():
    host=os.getenv('SALES_AX_PUBLIC_HOST','').strip().lower()
    return 'https://'+host+'/api/integrations/google/callback' if host else LOCAL_REDIRECT


class FileStore:
    def __init__(self,directory):self.directory=Path(directory)
    def path(self,name):return self.directory/name
    def read(self,name):
        p=self.path(name)
        return p.read_bytes() if p.exists() else None
    def write(self,name,data):
        self.directory.mkdir(parents=True,exist_ok=True)
        p=self.path(name);p.write_bytes(data)
        try:os.chmod(p,0o600)
        except OSError:pass
    def delete(self,name):self.path(name).unlink(missing_ok=True)


class BlobStore:
    """Private Vercel Blob store: serverless instances share nothing on disk. Contents are already Fernet-encrypted."""
    prefix='sales-ax/google/'
    def __init__(self):
        from vercel.blob import sync as blob
        self.blob=blob
    def read(self,name):
        try:return self.blob.get_blob(self.prefix+name,access='private').body
        except self.blob.BlobNotFoundError:return None
    def write(self,name,data):
        self.blob.put_blob(self.prefix+name,bytes(data),access='private',allow_overwrite=True,content_type='application/octet-stream',cache_control_max_age=60)
    def delete(self,name):self.blob.delete_blob(self.prefix+name)


def hosted_store_ready():
    return bool(os.getenv('BLOB_READ_WRITE_TOKEN') and os.getenv('SALES_AX_TOKEN_KEY'))


class GoogleConnection:
    def __init__(self,directory,flow_factory=None,account_reader=None,store=None):
        self.directory=Path(directory)
        self.store=store or FileStore(directory)
        self.flow_factory=flow_factory or Flow.from_client_config
        self.account_reader=account_reader

    @property
    def client_path(self):return self.directory/'oauth-client.json'
    @property
    def token_path(self):return self.directory/'google-token.enc'

    def key(self):
        # Hosted: the key lives in an encrypted env var, never next to the ciphertext it protects.
        if os.getenv('SALES_AX_TOKEN_KEY'):return os.getenv('SALES_AX_TOKEN_KEY').encode()
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/'local-token.key'
        if not path.exists():
            with path.open('xb') as f:f.write(Fernet.generate_key())
            try:os.chmod(path,0o600)
            except OSError:pass
        return path.read_bytes()

    def seal(self,value):return Fernet(self.key()).encrypt(json.dumps(value).encode())
    def unseal(self,data):return json.loads(Fernet(self.key()).decrypt(data))

    def configure(self,payload):
        config=json.loads(payload)
        if not isinstance(config,dict):raise ValueError('OAuth 클라이언트 JSON이 필요합니다.')
        section=config.get('web') or config.get('installed')
        if not section or not section.get('client_id') or not section.get('client_secret'):
            raise ValueError('Google Cloud에서 내려받은 OAuth 클라이언트 JSON이 필요합니다.')
        # Pin endpoints: an uploaded JSON cannot redirect token/secret exchange to another service.
        if section.get('auth_uri')!='https://accounts.google.com/o/oauth2/auth' or section.get('token_uri')!='https://oauth2.googleapis.com/token':
            raise ValueError('Google 공식 OAuth 엔드포인트만 허용합니다.')
        self.store.write('oauth-client.json',self.seal(config))
        return {'configured':True,'redirect_uri':redirect_uri(),'secrets_returned':False}

    def begin(self):
        sealed=self.store.read('oauth-client.json')
        if not sealed:raise ValueError('OAuth 클라이언트 JSON을 먼저 등록하세요.')
        flow=self.flow_factory(self.unseal(sealed),scopes=SCOPES,autogenerate_code_verifier=True)
        flow.redirect_uri=redirect_uri()
        url,state=flow.authorization_url(access_type='offline',prompt='consent',include_granted_scopes='true',login_hint='marketing@creatobi.com')
        browser_secret=secrets.token_urlsafe(32)
        # Persisted, not in memory: the callback may land on a different serverless instance.
        self.store.write('pending-'+hashlib.sha256(state.encode()).hexdigest(),self.seal({'verifier':getattr(flow,'code_verifier',None),
            'browser_hash':hashlib.sha256(browser_secret.encode()).hexdigest(),'expires':time.time()+PENDING_SECONDS}))
        return url,browser_secret

    def finish(self,state,code,browser_secret):
        name='pending-'+hashlib.sha256((state or '').encode()).hexdigest()
        sealed=self.store.read(name)
        if sealed:self.store.delete(name)
        item=self.unseal(sealed) if sealed else None
        if not item or item['expires']<time.time() or not browser_secret or not secrets.compare_digest(item['browser_hash'],hashlib.sha256(browser_secret.encode()).hexdigest()):
            raise ValueError('OAuth 요청이 만료되었거나 브라우저 확인에 실패했습니다. 다시 연결하세요.')
        flow=self.flow_factory(self.unseal(self.store.read('oauth-client.json')),scopes=SCOPES,code_verifier=item['verifier'])
        flow.redirect_uri=redirect_uri()
        flow.fetch_token(code=code)
        creds=flow.credentials
        if self.account_reader:account=self.account_reader(creds)
        else:
            from googleapiclient.discovery import build
            account=build('gmail','v1',credentials=creds,cache_discovery=False).users().getProfile(userId='me').execute()['emailAddress']
        expected=os.getenv('SALES_AX_GOOGLE_ACCOUNT','marketing@creatobi.com')
        if account.lower()!=expected.lower():raise ValueError('회사 계정 '+expected+'으로 연결해야 합니다. 토큰을 저장하지 않았습니다.')
        self.store.write('google-token.enc',self.seal({'credentials':json.loads(creds.to_json()),'account':account.lower(),
            'connected_at':datetime.now(timezone.utc).isoformat()}))
        return account

    def has_token(self):return self.store.read('google-token.enc') is not None

    def credentials(self):
        sealed=self.store.read('google-token.enc')
        if not sealed:raise ValueError('Google 계정을 먼저 연결하세요.')
        envelope=self.unseal(sealed)
        creds=Credentials.from_authorized_user_info(envelope['credentials'])
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            envelope['credentials']=json.loads(creds.to_json())
            self.store.write('google-token.enc',self.seal(envelope))
        if not creds.valid:raise ValueError('Google 연결이 만료되었습니다. 다시 연결하세요.')
        return creds

    def status(self):
        account=None;connected_at=None
        try:
            sealed=self.store.read('google-token.enc')
            if sealed:
                env=self.unseal(sealed)
                account=env['account'];connected_at=env['connected_at']
        except Exception:pass
        return {'client_configured':self.store.read('oauth-client.json') is not None,'token_stored':bool(account),'account':account,
                'connected_at':connected_at,'live_verified':False,'scopes':SCOPES,'redirect_uri':redirect_uri(),
                'state':'MANUAL' if account else 'BLOCKED','send_enabled':False}

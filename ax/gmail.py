"""Gmail drafts only. This adapter deliberately has no send method."""
import base64
import hashlib
import os
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from email.utils import getaddresses


class GmailError(ValueError):
    pass


def decode_raw(raw):
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))


def mime_message(sender, recipient, subject, body, pdf=None, in_reply_to='', references=''):
    for value in (sender, recipient, subject, in_reply_to, references):
        if '\n' in value or '\r' in value:
            raise GmailError('메일 헤더에 줄바꿈을 넣을 수 없습니다.')
    if len(getaddresses([recipient])) != 1 or getaddresses([recipient])[0][1] != recipient:
        raise GmailError('수신자는 정확히 한 명이어야 합니다.')
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, recipient, subject
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to
    msg.set_content(body)
    if pdf:
        msg.add_attachment(pdf['content'], maintype='application', subtype='pdf', filename=pdf['filename'])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def verify_raw(raw, sender, recipient, subject, body, pdf=None):
    msg = decode_raw(raw)
    if msg.get_all('Cc') or msg.get_all('Bcc'):
        raise GmailError('CC/BCC가 발견되었습니다.')
    if len(msg.get_all('To', [])) != 1 or getaddresses(msg.get_all('To', [])) != [('', recipient)]:
        raise GmailError('Gmail 수신자 불일치')
    if getaddresses(msg.get_all('From', [])) != [('', sender)] or str(msg['Subject']) != subject:
        raise GmailError('Gmail 발신자 또는 제목 불일치')
    text = msg.get_body(preferencelist=('plain',)).get_content()
    if text.replace('\r\n', '\n').rstrip('\n') != body.replace('\r\n', '\n').rstrip('\n'):
        raise GmailError('Gmail 본문 불일치')
    attachments = list(msg.iter_attachments())
    if len(attachments) != (1 if pdf else 0):
        raise GmailError('Gmail 첨부 개수 불일치')
    if pdf and (attachments[0].get_filename() != pdf['filename'] or
                hashlib.sha256(attachments[0].get_payload(decode=True)).hexdigest() != pdf['sha256']):
        raise GmailError('Gmail PDF 파일명 또는 내용 불일치')


class GmailDraftClient:
    def __init__(self, service):
        self.service = service

    @classmethod
    def configured(cls):
        # Credentials are obtained from ADC or a user-controlled authorized-user JSON outside the repo.
        import google.auth
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        path = os.getenv('GMAIL_AUTHORIZED_USER_FILE')
        scopes = ['https://www.googleapis.com/auth/gmail.compose', 'https://www.googleapis.com/auth/gmail.readonly']
        creds = Credentials.from_authorized_user_file(path, scopes=scopes) if path else google.auth.default(scopes=scopes)[0]
        return cls(build('gmail', 'v1', credentials=creds, cache_discovery=False))

    def account(self):
        return self.service.users().getProfile(userId='me').execute()['emailAddress'].lower()

    def upsert(self, draft_id, raw, thread_id=''):
        message = {'raw': raw}
        if thread_id:
            message['threadId'] = thread_id
        resource = self.service.users().drafts()
        if draft_id:
            return resource.update(userId='me', id=draft_id, body={'message': message}).execute()
        return resource.create(userId='me', body={'message': message}).execute()

    def read(self, draft_id):
        return self.service.users().drafts().get(userId='me', id=draft_id, format='raw').execute()['message']['raw']

    def thread_messages(self, thread_id):
        return self.service.users().threads().get(userId='me', id=thread_id, format='full').execute().get('messages', [])

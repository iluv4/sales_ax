"""Explicit boundaries around pinned upstream logic; never expose legacy SMTP to web."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor' / 'sales' / 'src'))
sys.path.insert(0, str(ROOT / 'vendor' / 'saas'))
from sales_agent.config import Config, ICPConfig, CompanyConfig
from sales_agent.store import Store
from sales_agent.tools import build_tools
from sales_agent.prompts import build_system_prompt
from app.config import Settings
from app.service import SalesService


def campaign_config(campaign):
    icp = json.loads(campaign['icp'])
    return Config(icp=ICPConfig(**icp), company=CompanyConfig(
        name='모범AI', product=campaign['product'], value_proposition=campaign['value_prop']))


def qualification(score, fit, rationale, stage='DISCOVERED'):
    # Preserve the exact existing fit semantics, including weak fits not advancing.
    store = Store()
    try:
        lid = store.add_lead('canonical-adapter')
        mapping = {'DISCOVERED': 'new', 'RESEARCH_REQUIRED': 'researching', 'VERIFIED': 'researching'}
        store.update_lead(lid, stage=mapping.get(stage, 'contacted'))
        tools = {t.name: t for t in build_tools(store)}
        result = json.loads(tools['qualify_lead'].call(dict(lead_id=lid, score=score, fit=fit, rationale=rationale)))
        return {'score': result['score'], 'fit': result['fit'],
                'stage': {'qualified': 'QUALIFIED', 'disqualified': 'DISQUALIFIED'}.get(result['stage'], stage)}
    finally:
        store.close()


def compose_body(campaign, body):
    settings = Settings(sender_name='모범AI', sender_company='크레토비', sender_email=campaign['sender'])
    return SalesService.compose_body(SimpleNamespace(s=settings), body)


def schedule_next_touch(db, lead, step):
    # Reuse upstream cadence through a narrow storage bridge, without instantiating a mailer.
    class Bridge:
        s = Settings(followup_intervals=(3, 4), call_after_days=2)

        def today(self):
            return datetime.now(ZoneInfo('Asia/Seoul')).date()

        def cancel_tasks(self, lid, kinds):
            for kind in kinds:
                db.execute("UPDATE tasks SET status='canceled' WHERE lead_id=? AND kind=? AND status='open'", (lid, kind))

        def add_task(self, lid, kind, title, due, step=0):
            db.execute('INSERT INTO tasks(lead_id,kind,title,due_date,step) VALUES(?,?,?,?,?) '
                       'ON CONFLICT(lead_id,kind,step) DO NOTHING', (lid, kind, title, due.isoformat(), step))
    SalesService._schedule_next_touch(Bridge(), {'id': lead['id'], 'name': lead['name']}, step)


def research_prompt(campaign):
    return build_system_prompt(campaign_config(campaign), True)

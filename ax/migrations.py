"""Additive, repeatable SQLite migrations; source databases remain untouched."""
def migrate(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,value_proposition TEXT NOT NULL,proof_points TEXT NOT NULL DEFAULT '[]',price_policy TEXT NOT NULL DEFAULT 'UNCONFIRMED',template_id INTEGER);
    CREATE TABLE IF NOT EXISTS icp_profiles(id INTEGER PRIMARY KEY,name TEXT NOT NULL,config TEXT NOT NULL,scoring_rules TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS document_links(id INTEGER PRIMARY KEY,proposal_id INTEGER NOT NULL REFERENCES proposals(id),lead_id INTEGER NOT NULL REFERENCES leads(id),provider TEXT NOT NULL,url TEXT NOT NULL,external_doc_id TEXT,external_link_id TEXT,verification_state TEXT NOT NULL DEFAULT 'unknown',token_hash TEXT UNIQUE,revoked INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,UNIQUE(provider,url));
    CREATE TABLE IF NOT EXISTS document_events(id INTEGER PRIMARY KEY,link_id INTEGER NOT NULL REFERENCES document_links(id),provider TEXT NOT NULL,event_type TEXT NOT NULL,page_number INTEGER,dwell_ms INTEGER,occurred_at TEXT NOT NULL,source TEXT NOT NULL,external_event_id TEXT,is_mock INTEGER NOT NULL DEFAULT 0,observation_source TEXT,session_id TEXT,UNIQUE(provider,external_event_id));
    CREATE TABLE IF NOT EXISTS source_records(id INTEGER PRIMARY KEY,provider TEXT NOT NULL,external_id TEXT NOT NULL,lead_id INTEGER REFERENCES leads(id),payload TEXT NOT NULL,imported_at TEXT NOT NULL,UNIQUE(provider,external_id));
    CREATE TABLE IF NOT EXISTS integration_health(provider TEXT PRIMARY KEY,state TEXT NOT NULL,detail TEXT NOT NULL,checked_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS send_attempts(id INTEGER PRIMARY KEY,draft_id INTEGER NOT NULL REFERENCES drafts(id),version INTEGER NOT NULL,status TEXT NOT NULL,gmail_id TEXT,thread_id TEXT,at TEXT NOT NULL,UNIQUE(draft_id,version));
    ''')
    additions={
        'campaigns':{'offer_id':'INTEGER REFERENCES offers(id)','icp_id':'INTEGER REFERENCES icp_profiles(id)','owner_id':"TEXT NOT NULL DEFAULT 'local-operator'",'status':"TEXT NOT NULL DEFAULT 'READY_FOR_DISCOVERY'"},
        'organizations':{'industry':"TEXT NOT NULL DEFAULT ''"},
        'messages':{'source':"TEXT NOT NULL DEFAULT 'gmail_manual'",'is_mock':'INTEGER NOT NULL DEFAULT 0','classification_confidence':'REAL','classified_by':'TEXT'},
        'proposals':{'template_id':'INTEGER REFERENCES templates(id)','version':'INTEGER NOT NULL DEFAULT 1','pptx_content':'BLOB','qa_status':"TEXT NOT NULL DEFAULT 'MANUAL_PDF'",'source_url':'TEXT'},
        'leads':{'next_action_at':'TEXT','assigned_to':"TEXT NOT NULL DEFAULT 'local-operator'"}
    }
    for table,columns in additions.items():
        existing={r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        for name,type_ in columns.items():
            if name not in existing: conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {type_}')
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES(1)')
    conn.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES(2)')
    conn.commit()

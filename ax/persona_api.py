from fastapi import FastAPI
from pydantic import BaseModel,Field
from .persona import PersonaService,PersonaCard


def register(app:FastAPI,wf):
    service=PersonaService(wf)
    @app.get('/api/personas')
    def sessions():return wf.db.rows('SELECT id,title,version,approved_version,campaign_id FROM persona_sessions ORDER BY id DESC')

    @app.post('/api/personas')
    def create():return service.create()

    @app.get('/api/personas/{sid}')
    def read(sid:int):return service.session(sid)

    class Message(BaseModel):
        text:str=Field(min_length=2,max_length=12000)
        request_id:str=Field(min_length=8,max_length=100)

    @app.post('/api/personas/{sid}/messages')
    def message(sid:int,data:Message):return service.message(sid,data.text,data.request_id)

    @app.put('/api/personas/{sid}/card')
    def edit(sid:int,data:PersonaCard):return service.save_card(sid,data,'MANUAL_EDIT')

    class Approve(BaseModel):
        version:int
        confirmed:bool

    @app.post('/api/personas/{sid}/approve')
    def approve(sid:int,data:Approve):return service.approve(sid,data.version,data.confirmed)

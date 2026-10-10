# -*- coding: utf-8 -*-
"""
IDM Backend — serviço único (FastAPI) que roda o motor da fila:
  - POLL agendado (a cada POLL_MINUTES, padrão 15) → run_motor → Firestore
  - WEBHOOK do Clinic Expert (lead/agendamento novo) → run_motor → Firestore (tempo real)
  - /health p/ monitoramento

Projetado para o IDM crescer: novos jobs entram como novas rotas/agendamentos aqui.
Segurança: o webhook exige o header X-IDM-Secret == WEBHOOK_SECRET.
"""
import os, hmac, hashlib, threading
from fastapi import FastAPI, Request, Header, HTTPException
from apscheduler.schedulers.background import BackgroundScheduler

from motor_fila import run_motor
from firebase_writer import push_fila

POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "15"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

app = FastAPI(title="IDM Backend", version="1.0")
_lock = threading.Lock()

def _rodar(motivo: str):
    # trava p/ não rodar duas vezes em paralelo (webhook + poll ao mesmo tempo)
    if not _lock.acquire(blocking=False):
        print(f"[run] ({motivo}) já há uma execução em curso — ignorado")
        return {"status": "busy"}
    try:
        res = run_motor()
        ok = push_fila(res)
        print(f"[run] ({motivo}) {res['total_leads_pipeline']} leads | resumo {res['resumo']} | firestore={ok}")
        return {"status": "ok", "total": res["total_leads_pipeline"], "resumo": res["resumo"], "firestore": ok}
    finally:
        _lock.release()

@app.on_event("startup")
def _startup():
    sched = BackgroundScheduler(timezone="America/Sao_Paulo")
    sched.add_job(lambda: _rodar("poll"), "interval", minutes=POLL_MINUTES,
                  next_run_time=None)  # 1ª execução no 1º intervalo; use /run para rodar já
    sched.start()
    print(f"[startup] poll a cada {POLL_MINUTES} min ligado")

@app.get("/health")
def health():
    return {"status": "up", "poll_minutes": POLL_MINUTES}

@app.post("/run")
def run_now(x_idm_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    return _rodar("manual")

@app.post("/webhook/clinic-expert")
async def webhook(request: Request, x_idm_secret: str = Header(default="")):
    # validação simples por segredo compartilhado (o CE deve enviar esse header,
    # ou troque por validação de assinatura HMAC do corpo quando o CE suportar)
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await request.json()
    evento = (body.get("event") or body.get("type") or "").lower()
    # só reagimos ao que muda a fila; o resto devolve 200 e ignora
    if any(k in evento for k in ("patient", "booking", "opportunity", "lead", "crm")):
        res = _rodar(f"webhook:{evento}")
        return {"ok": True, "acionado": True, "detalhe": res}
    return {"ok": True, "acionado": False, "evento": evento}

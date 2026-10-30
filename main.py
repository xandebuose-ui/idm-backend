# -*- coding: utf-8 -*-
"""
IDM Backend — serviço único (FastAPI):
  - POLL agendado (a cada POLL_MINUTES, padrão 15) → run_motor → cache + Firestore (se houver)
  - WEBHOOK do Clinic Expert → roda na hora (tempo real)
  - /painel  → página web (HTTP Basic Auth) com a FILA DE PRIORIDADE ao vivo
  - /fila.json → mesma fila em JSON (Basic Auth)
  - /health  → status público
Segurança: /painel e /fila.json exigem usuário+senha (PANEL_USER / PANEL_PASS).
Dado de paciente só aparece atrás dessa autenticação.
"""
import os, hmac, threading, secrets, html, datetime as dt
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from apscheduler.schedulers.background import BackgroundScheduler

from motor_fila import run_motor, FECHADO
from firebase_writer import push_fila
from diag import router as diag_router

POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "15"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PANEL_USER = os.environ.get("PANEL_USER", "")
PANEL_PASS = os.environ.get("PANEL_PASS", "")
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "")  # link secreto (1 toque, celular/desktop)

app = FastAPI(title="IDM Backend", version="1.1")
app.include_router(diag_router)
security = HTTPBasic()
_lock = threading.Lock()
_last = {"result": None, "quando": None}

def _rodar(motivo: str):
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        res = run_motor()
        _last["result"] = res
        _last["quando"] = dt.datetime.now().isoformat(timespec="seconds")
        ok = push_fila(res)  # grava no Firestore se as credenciais existirem; senão ignora
        print(f"[run] ({motivo}) {res['total_leads_pipeline']} leads | {res['resumo']} | firestore={ok}")
        return {"status": "ok", "total": res["total_leads_pipeline"], "resumo": res["resumo"]}
    finally:
        _lock.release()

def _vigia_cracha():
    try:
        import firebase_writer as _fw
        _fw.atualizar_alerta_cracha()
    except Exception as e:
        print("[cracha] vigia erro:", e)

@app.on_event("startup")
def _carregar_termometro_do_firestore():
    try:
        import firebase_writer as _fw
        db = _fw._get_db()
        if not db:
            return
        d = db.collection(os.environ.get("TERM_COLL", "escuta_termometro")).document("atual").get()
        if not d.exists:
            return
        res = d.to_dict() or {}
        if _TERM.get("result"):
            return  # ja classificou algo neste processo; nao sobrescreve
        _TERM["result"] = res
        _TERM["quando"] = res.get("gerado_em")
        n = 0
        # NAO semeia _TERM_CACHE: as chaves antigas (lt='') nao casavam com o universo novo
        # e entupiam o orcamento de classificacao (pendente travava). O cache reconstroi ao vivo.
        print("[termometro] resultado carregado do firestore (cache NAO semeado de proposito)", flush=True)
    except Exception as e:
        print("[termometro] load firestore erro:", e, flush=True)


def _carregar_universo_firestore():
    try:
        import firebase_writer as _fw
        db = _fw._get_db()
        if not db:
            return
        d = db.collection(os.environ.get("ESCUTA_ORFAOS_COLL", "escuta_orfaos")).document("universo").get()
        if not d.exists:
            return
        data = d.to_dict() or {}
        u = data.get("universo") or []
        if not u:
            return
        cur = dict(_last_cruz.get("result") or {})
        if not cur.get("universo"):
            cur["universo"] = u
            cur.setdefault("origem", data.get("origem") or {})
            _last_cruz["result"] = cur
            _last_cruz["quando"] = _last_cruz.get("quando") or data.get("gerado_em")
            print("[cruzamento] universo carregado do firestore: %d" % len(u), flush=True)
    except Exception as e:
        print("[cruzamento] load universo erro:", e, flush=True)


def _startup():
    sched = BackgroundScheduler(timezone="America/Sao_Paulo")
    sched.add_job(lambda: _rodar("poll"), "interval", minutes=POLL_MINUTES)
    sched.add_job(_vigia_cracha, "interval", minutes=30)
    sched.add_job(lambda: _rodar_cruzamento("poll"), "interval", minutes=5)
    sched.add_job(lambda: _rodar_termometro("poll"), "interval", minutes=5)
    try:
        _vigia_cracha()
    except Exception:
        pass
    sched.start()
    try:
        threading.Thread(target=_rodar_cruzamento, args=("startup",), daemon=True).start()
    except Exception:
        pass
    try:
        threading.Timer(5, _carregar_termometro_do_firestore).start()
        threading.Timer(8, _carregar_universo_firestore).start()
        threading.Timer(25, lambda: _rodar_termometro("startup")).start()
    except Exception:
        pass
    print(f"[startup] poll {POLL_MINUTES}min; vigia cracha 30min; cruzamento 15min")

def _auth(cred: HTTPBasicCredentials = Depends(security)):
    ok_u = PANEL_USER and hmac.compare_digest(cred.username, PANEL_USER)
    ok_p = PANEL_PASS and hmac.compare_digest(cred.password, PANEL_PASS)
    if not (ok_u and ok_p):
        raise HTTPException(status_code=401, detail="unauthorized",
                            headers={"WWW-Authenticate": "Basic"})
    return True

def _get_result():
    if _last["result"] is None:
        _rodar("primeira-carga")
    return _last["result"], _last["quando"]

@app.get("/health")
def health():
    return {"status": "up", "poll_minutes": POLL_MINUTES,
            "tem_fila": _last["result"] is not None, "ultima_rodada": _last["quando"]}

@app.post("/run")
def run_now(x_idm_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    return _rodar("manual")

@app.post("/webhook/clinic-expert")
async def webhook(request: Request, x_idm_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    body = await request.json()
    evento = (body.get("event") or body.get("type") or "").lower()
    if any(k in evento for k in ("patient", "booking", "opportunity", "lead", "crm", "message", "chat", "conversation", "ticket")):
        threading.Thread(target=_rodar_cruzamento, args=("webhook",), daemon=True).start()
        return {"ok": True, "acionado": True, "detalhe": _rodar(f"webhook:{evento}")}
    return {"ok": True, "acionado": False, "evento": evento}


_WH_LAST = {}  # ultimo webhook CRM recebido (memoria) - debug

@app.post("/webhook/ce-crm")
async def webhook_ce_crm(request: Request):
    # Evento de CRM do Clinic Expert (crm_opportunity.created).
    # O payload NAO traz telefone (so uuid da oportunidade + paciente{uuid,name} + etapa/status);
    # confirmado em 29/10/2026 com lead real. Por isso NAO extraimos telefone aqui: a acao correta
    # e disparar um REFRESH do cruzamento (debounce 60s), que re-busca a API interna ja com o lead
    # novo + telefone e re-classifica. Nunca destrutivo; nao exige segredo; nao altera a logica
    # travada (apenas chama a mesma pipeline _rodar_cruzamento que o poll ja usa).
    import json as _j, time as _t
    try:
        body = await request.json()
    except Exception:
        body = {}
    evt = (body.get("type") or body.get("event") or "").lower()
    res = body.get("resource") or body.get("data") or body
    if not isinstance(res, dict):
        res = {}
    pz = res.get("pipeline")
    pipe = (pz.get("name") if isinstance(pz, dict) else pz) or res.get("pipeline_name") or ""
    sz = res.get("stage")
    stage = (sz.get("name") if isinstance(sz, dict) else sz) or ""
    status = (sz.get("status") if isinstance(sz, dict) else None) or res.get("status") or ""
    try:
        print("[wh-crm] evt=%s pipe=%s stage=%s status=%s" % (evt, pipe, stage, status), flush=True)
    except Exception:
        pass
    global _WH_LAST
    _WH_LAST = {"recebido_em": dt.datetime.utcnow().isoformat() + "Z", "evt": evt,
                "pipe": pipe, "stage": stage, "status": status, "payload": body}
    acionou = False
    if evt.startswith("crm_opportunity") or evt.startswith("sale."):
        now = _t.time()
        if now - _LAST_TICK.get("wh", 0.0) > 60:
            _LAST_TICK["wh"] = now
            threading.Thread(target=_rodar_cruzamento, args=("webhook-crm",), daemon=True).start()
            acionou = True
    return {"ok": True, "evt": evt, "pipe": pipe, "stage": stage, "acionou_refresh": acionou}

# ---------------- TimTim (webhook da conta do Instituto) ----------------
# Receptor do webhook oficial do TimTim (lead.create / lead.update / lead_source.update /
# message.create). Fase 1 (29/10/2026): so GRAVA o payload bruto no Firestore, colecao
# escuta_tintim, 1 doc por evento — nenhum processamento ainda (o coletor unico le daqui).
# Auth: o TimTim nao assina requisicoes -> segredo vai na URL (?k=TINTIM_WEBHOOK_KEY).
# Sempre responde 200 quando o segredo bate, mesmo se o Firestore falhar (o TimTim desativa
# o webhook sozinho em erro HTTP); a falha fica em _TT_STATS para o /webhook/tintim/status.
TINTIM_WEBHOOK_KEY = os.environ.get("TINTIM_WEBHOOK_KEY", "")
_TT_STATS = {"recebidos": 0, "gravados": 0, "erro_grava": 0, "nao_autorizado": 0,
             "ultimo_em": None, "ultimo_evt": None, "ultimo_erro": None, "por_evento": {}}

def _tt_doc_id(evt, body):
    import hashlib as _h
    ph = str(body.get("phone") or (body.get("lead") or {}).get("phone") or "")
    mid = str(body.get("message_id") or "")
    base = "%s|%s|%s|%s" % (evt, ph, mid, body.get("updated_isoformat") or body.get("updated") or "")
    return dt.datetime.utcnow().strftime("%Y%m%d%H%M%S") + "_" + _h.sha1(base.encode()).hexdigest()[:10]

@app.post("/webhook/tintim")
async def webhook_tintim(request: Request, k: str = ""):
    if not TINTIM_WEBHOOK_KEY or not hmac.compare_digest(k or "", TINTIM_WEBHOOK_KEY):
        _TT_STATS["nao_autorizado"] += 1
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {"_raw": (await request.body())[:4000].decode("utf-8", "ignore")}
    if not isinstance(body, dict):
        body = {"_raw": body}
    evt = str(body.get("event_type") or "").lower() or "desconhecido"
    _TT_STATS["recebidos"] += 1
    _TT_STATS["ultimo_em"] = dt.datetime.utcnow().isoformat() + "Z"
    _TT_STATS["ultimo_evt"] = evt
    _TT_STATS["por_evento"][evt] = _TT_STATS["por_evento"].get(evt, 0) + 1
    try:
        db = _fw._get_db()
        if db is None:
            raise RuntimeError("firestore indisponivel")
        coll = os.environ.get("ESCUTA_TINTIM_COLL", "escuta_tintim")
        doc = {"evento": evt, "recebido_em": _TT_STATS["ultimo_em"],
               "phone": body.get("phone") or (body.get("lead") or {}).get("phone"),
               "payload": body}
        db.collection(coll).document(_tt_doc_id(evt, body)).set(doc)
        _TT_STATS["gravados"] += 1
    except Exception as e:
        _TT_STATS["erro_grava"] += 1
        _TT_STATS["ultimo_erro"] = str(e)[:200]
        print("[tintim] erro ao gravar:", e, flush=True)
    try:
        print("[tintim] evt=%s phone=%s" % (evt, (body.get("phone") or "")[-4:]), flush=True)
    except Exception:
        pass
    return {"ok": True, "evento": evt}

# ---------------- Meta Ads (token do usuario do sistema, so leitura) ----------------
import meta_ads as _meta

@app.get("/meta_status")
def meta_status():
    return _meta.status()

import tintim_api as _tt

@app.get("/tintim_status")
def tintim_status():
    return _tt.status()

@app.get("/webhook/tintim/status")
def webhook_tintim_status():
    return {"configurado": bool(TINTIM_WEBHOOK_KEY), **_TT_STATS}

@app.get("/webhook/ce-crm/last/{token}")
def webhook_ce_crm_last(token: str):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    return _WH_LAST or {"vazio": True}


@app.get("/fila.json")
def fila_json(_: bool = Depends(_auth)):
    res, quando = _get_result()
    return JSONResponse({"ultima_rodada": quando, **(res or {})})

@app.get("/painel", response_class=HTMLResponse)
def painel(_: bool = Depends(_auth)):
    res, quando = _get_result()
    return HTMLResponse(_build_html(res, quando))

@app.get("/p/{token}", response_class=HTMLResponse)
def painel_link(token: str):
    # Link secreto (capability URL): 1 toque no celular, sem caixa de senha.
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    res, quando = _get_result()
    return HTMLResponse(_build_html(res, quando))

@app.get("/p/{token}/fila.json")
def fila_link(token: str):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    res, quando = _get_result()
    return JSONResponse({"ultima_rodada": quando, **(res or {})})

# -------------------------- render do painel --------------------------
def _acao(flags):
    for x in flags:
        if x.startswith("ERRO"): return "Corrigir cadastro + qualificar"
        if "compareceu/faltou" in x: return "Reagendar — faltou e sem novo horário"
        if "sem avaliação agendada" in x: return "Puxar avaliação (lead parado)"
        if x.startswith("RESGATE"): return "Reengajar (esfriou)"
        if x.startswith("OK"): return "Confirmar presença"
    return "—"

def _build_html(res, quando):
    if not res:
        return "<h1>Sem dados ainda. Recarregue em alguns segundos.</h1>"
    r = res.get("resumo", {})
    fila = res.get("fila", [])
    from collections import Counter
    etapas = Counter(L["etapa"] for L in fila)
    acion = [L for L in fila if L["stage"] not in FECHADO
             and any(("AGIR" in x or "ERRO" in x or "RESGATE" in x) for x in L["flags"])][:30]
    def esc(s): return html.escape(str(s if s is not None else ""))
    rows = ""
    for i, L in enumerate(acion, 1):
        cls = "hot" if L["etapa"] in ("Qualificação", "Agendamento") else "warm"
        err = ' <span class="tag err">cadastro</span>' if any("ERRO" in x for x in L["flags"]) else ""
        tel = esc(L.get("telefone") or "")
        rows += (f"<tr><td class=rk>{i}</td>"
                 f"<td><b>{esc(L.get('nome'))}</b>{err}<div class=tel>{tel}</div></td>"
                 f"<td><span class='pill {cls}'>{esc(L['etapa'])}</span></td>"
                 f"<td class=mut>{esc(L.get('vendedor') or '—')}</td>"
                 f"<td>{esc(_acao(L['flags']))}</td></tr>")
    ordem = ["Entrada","Qualificação","Agendamento","No-Show","Reagendamento","Orçamento",
             "Recuperação N1","Recuperação N2","Recuperação N3","Ganho - Novo Cliente","Desqualificado","Perdido"]
    mx = max(etapas.values()) if etapas else 1
    bars = ""
    for e in ordem:
        n = etapas.get(e, 0)
        if not n: continue
        w = max(int(n/mx*100), 3)
        col = "#b23a2e" if e in ("Recuperação N3","Perdido","Desqualificado") else ("#1e7d4f" if e=="Ganho - Novo Cliente" else "#b7892f")
        bars += (f"<div class=brow><div class=blab>{esc(e)}</div>"
                 f"<div class=btrack><div class=bfill style='width:{w}%;background:{col}'></div></div>"
                 f"<div class=bval>{n}</div></div>")
    return f"""<!doctype html><html lang=pt-BR><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta http-equiv=refresh content="300">
<title>IDM · Fila de Prioridade</title>
<style>
:root{{--ink:#1c1c20;--mut:#6b6b73;--line:#e6e2d9;--gold:#b7892f;--bg:#faf8f4;--hot:#c23b2a;--hotb:#fbe6e2;--warm:#b07d14;--warmb:#fbf1d8;--red:#b23a2e;--redb:#f8e7e4}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"Helvetica Neue",Arial,sans-serif;color:var(--ink);background:#fff;padding:16px;max-width:1100px;margin:0 auto}}
.head{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid var(--gold);padding-bottom:8px;margin-bottom:12px;flex-wrap:wrap;gap:6px}}
.name{{font-family:Georgia,serif;font-size:22px}}.sub{{font-size:11px;color:var(--gold);letter-spacing:2px;text-transform:uppercase}}
.meta{{font-size:11px;color:var(--mut);text-align:right}}
.kpis{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
.kpi{{flex:1;min-width:120px;background:var(--bg);border:1px solid var(--line);border-top:3px solid var(--gold);border-radius:5px;padding:8px 12px}}
.kpi.alert{{border-top-color:var(--red);background:var(--redb)}}
.kpi .v{{font-size:22px;font-weight:700}}.kpi.alert .v{{color:var(--red)}}.kpi .l{{font-size:10px;color:var(--mut);text-transform:uppercase}}
h2{{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--gold);margin:16px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:10px;text-transform:uppercase;color:var(--mut);border-bottom:1.5px solid var(--ink);padding:6px}}
td{{padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:middle;font-size:13px}}
tr:nth-child(even) td{{background:#fcfbf8}}
.rk{{font-family:Georgia,serif;font-weight:700;color:var(--gold);width:26px;text-align:center}}
.tel{{color:var(--mut);font-size:11px}}.mut{{color:var(--mut)}}
.pill{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;white-space:nowrap}}
.hot{{background:var(--hotb);color:var(--hot)}}.warm{{background:var(--warmb);color:var(--warm)}}
.tag.err{{font-size:9px;background:var(--redb);color:var(--red);padding:1px 5px;border-radius:10px;font-weight:700}}
.brow{{display:flex;align-items:center;gap:8px;margin:3px 0}}.blab{{width:140px;font-size:12px;text-align:right}}
.btrack{{flex:1;background:#eee;border-radius:3px;height:12px}}.bfill{{height:12px;border-radius:3px}}.bval{{width:28px;font-weight:700;font-size:12px}}
.foot{{margin-top:16px;padding-top:8px;border-top:1px solid var(--line);font-size:10px;color:var(--mut)}}
</style></head><body>
<div class=head>
  <div><div class=name>Instituto Dayane Mafra</div><div class=sub>Fila de Prioridade Comercial · ao vivo</div></div>
  <div class=meta>Atualizado: {esc((quando or '')[:16].replace('T',' '))}<br>recarrega sozinho a cada ~{POLL_MINUTES} min</div>
</div>
<div class=kpis>
  <div class=kpi><div class=v>{res.get('total_leads_pipeline',0)}</div><div class=l>Leads no pipeline</div></div>
  <div class='kpi alert'><div class=v>{r.get('ativos_sem_agenda',0)}</div><div class=l>Ativos sem avaliação</div></div>
  <div class='kpi alert'><div class=v>{r.get('erros_cadastro',0)}</div><div class=l>Erros de cadastro</div></div>
  <div class=kpi><div class=v>{r.get('em_resgate',0)}</div><div class=l>Em resgate</div></div>
</div>
<h2>Agir agora — ordenado por prioridade</h2>
<table><thead><tr><th>#</th><th>Lead</th><th>Etapa</th><th>Vendedor</th><th>O que fazer</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Saúde do funil — leads por etapa</h2>{bars}
<div class=foot>Fonte: API oficial do Clinic Expert (CRM · Agenda · Vendas) · só leitura. Origem real (pago×orgânico) e tempo exato sem resposta vêm da conversa (camada à parte).</div>
</body></html>"""




# ---- crachá (token do CliniChat): recebe do navegador e guarda no Firestore ----
import firebase_writer as _fw
_INGEST_SECRET = os.environ.get("INGEST_SECRET", "") or WEBHOOK_SECRET

@app.post("/ingest_token")
async def ingest_token(request: Request, x_idm_secret: str = Header(default="")):
    if not _INGEST_SECRET or not hmac.compare_digest(x_idm_secret, _INGEST_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = (body.get("token") or "").strip()
    if not token.startswith("eyJ") or len(token) < 100:
        raise HTTPException(status_code=400, detail="token invalido")
    ok = _fw.save_token(token, origem=body.get("origem"))
    return {"ok": ok, **_fw.token_info()}

@app.get("/token_status")
def token_status(_: bool = Depends(_auth)):
    return _fw.token_info()


@app.get("/cracha_status")
def cracha_status_publico():
    return _fw.token_info()


@app.post("/ingest_app_jwt")
async def ingest_app_jwt(request: Request):
    # Recebe o app_jwt (Sanctum) da sessao logada do CE e SO guarda se ele realmente funciona
    # na API interna de CRM (auto-validacao: nao precisa de segredo, so um token valido passa).
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = (body.get("token") or "").strip()
    if len(token) < 20:
        raise HTTPException(status_code=400, detail="token invalido")
    import urllib.request as _u, urllib.error as _ue
    req = _u.Request("https://api.clinicaexperts.com.br/api/crm/opportunity?page=1&per_page=1",
                     headers={"Authorization": "Bearer " + token,
                              "User-Agent": "Mozilla/5.0 Chrome/129.0", "Accept": "application/json"})
    try:
        with _u.urlopen(req, timeout=20) as r:
            ok_ce = (r.status == 200)
    except _ue.HTTPError as e:
        return {"ok": False, "motivo": "token nao validou no CE", "status": e.code}
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:100]}
    if not ok_ce:
        return {"ok": False, "motivo": "token nao validou no CE"}
    saved = _fw.save_app_jwt(token, origem=body.get("origem"))
    return {"ok": bool(saved), "validado_no_ce": True}


@app.get("/gate")
def gate_check():
    import motor_cruzamento as _o
    try:
        _of, _cf, _on, nopps, diag = _o._indice_ce()
        g = _o.verificar_gate()
        return {"gate": g, "diag": diag, "tem_app_jwt": _o._app_jwt() is not None}
    except Exception as e:
        return {"erro": str(e)[:200]}


# ---- CORS p/ receber o crachá direto da página do CE ----
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("ALLOW_ORIGINS","https://app.clinicaexperts.com.br,https://instituto-mafra-sistema.netlify.app").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/diag_clinichat")
def diag_clinichat():
    import urllib.request, urllib.error, json as _json
    tok = _fw.get_token()
    if not tok:
        return {"ok": False, "motivo": "sem cracha"}
    url = ("https://clinichat.clinicaexperts.com.br/contact-channel"
           "?from=0&to=1&orderBy=lastMessageTime%3ADESC&filters=%7B%22status%22%3A%22inbox%22%7D")
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tok,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status = r.status
            data = _json.loads(r.read().decode("utf-8", "ignore") or "{}")
        total = None
        if isinstance(data, dict):
            pg = data.get("pagination") or {}
            total = pg.get("total")
        return {"ok": True, "status": status, "inbox_total": total}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "motivo": "http"}
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120]}


# ---- Bloco B: motor de cruzamento (conversa = verdade; so-leitura) ----
import motor_cruzamento as _cruz
_last_cruz = {"result": None, "quando": None}
_cruz_lock = threading.Lock()

def _sp_now():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _rodar_cruzamento(motivo="poll"):
    if motivo == "poll":
        n = _sp_now()
        mins = n.hour * 60 + n.minute
        if mins < 510 or mins > 1200:
            return {"ok": True, "skip": "fora do horario comercial"}
    if not _cruz_lock.acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        res = _cruz.rodar_cruzamento()
        _last_cruz["result"] = res
        _last_cruz["quando"] = res.get("gerado_em")
        return res
    except Exception as e:
        import traceback as _tb
        _last_cruz["erro"] = repr(e)[:300]; _last_cruz["erro_em"] = _sp_now().isoformat(timespec="seconds")
        print("[cruzamento] erro:", e); print(_tb.format_exc(), flush=True)
        return {"ok": False, "erro": str(e)[:160]}
    finally:
        _cruz_lock.release()

@app.post("/run_orfaos")
def rodar_cruzamento_now(x_idm_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    threading.Thread(target=_rodar_cruzamento, args=("manual",), daemon=True).start()
    return {"ok": True, "started": True}

@app.get("/orfaos.json")
def cruzamento_json():
    return {"ok": True, "quando": _last_cruz["quando"], "result": _last_cruz["result"], "erro": _last_cruz.get("erro"), "progresso": _cruz.PROG}


# ---- Jarbas: analise por lead (sob demanda; modelo via JARBAS_MODEL) ----
import jarbas as _jarbas

def _jarbas_auth(authorization: str, x_idm_secret: str) -> bool:
    # admin/teste: header secreto == WEBHOOK_SECRET
    if WEBHOOK_SECRET and x_idm_secret and hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        return True
    # navegador: Firebase ID token (Bearer) de usuario autenticado do projeto
    if authorization.startswith("Bearer "):
        tok = authorization[7:].strip()
        try:
            from firebase_admin import auth as _fbauth
            _fw._get_db()  # garante o app firebase inicializado
            _fbauth.verify_id_token(tok)
            return True
        except Exception:
            return False
    return False

@app.get("/jarbas/analise")
def jarbas_analise(conv_id: str, nome: str = "", temperatura: str = "", origem: str = "",
                   authorization: str = Header(default=""), x_idm_secret: str = Header(default="")):
    if not _jarbas_auth(authorization, x_idm_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    return _jarbas.analisar(conv_id, {"nome": nome, "temperatura": temperatura, "origem": origem})


@app.get("/jarbas/teste")
def jarbas_teste(x_idm_secret: str = Header(default="")):
    # Smoke-test protegido: pega a 1a conversa real do inbox e roda o Jarbas.
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    convs = _cruz._lista("inbox", cap=80)
    tentativas = 0
    for c in convs:
        cid = c.get("conversationId")
        if not cid:
            continue
        tentativas += 1
        if tentativas > 15:
            break
        res = _jarbas.analisar(cid, {"nome": (c.get("contact") or {}).get("name") or "", "temperatura": "", "origem": ""})
        if res.get("ok"):
            return {"usou_conv": cid, "tentativas": tentativas, "analise": res}
    return {"ok": False, "motivo": "nenhuma conversa analisavel no inbox", "tentativas": tentativas}


# ---- Termometro: Haiku classifica os leads do inbox em quente/morno/frio ----
_TERM = {"result": None, "quando": None}
_term_lock = threading.Lock()
_TERM_CACHE = {}
_TEMP_OVERRIDE = {}  # conv_id -> temperatura da leitura profunda (drawer); vence o batch
_TEMP_MODEL = os.environ.get("JARBAS_TEMP_MODEL", "claude-haiku-5-5")

def _fonte_de_leads():
    # Abstrai a FONTE de leads. Hoje: inbox do CliniChat (WhatsApp unico) + gate de identidade.
    # Futuro (numero dedicado so de leads): TERM_FONTE=dedicado -> inbox ja 100% lead, sem gate.
    # Trocar e 1 env var; o modulo do gate fica removivel sem tocar termometro/painel/Jarbas.
    modo = os.environ.get("TERM_FONTE", "inbox_ce")
    convs = _cruz._lista("inbox", cap=int(os.environ.get("TERM_CAP", "80")))
    aplicar_gate = (modo != "dedicado")
    return convs, aplicar_gate

def _rodar_termometro(motivo="poll"):
    import time as _t
    if motivo == "poll":
        n = _sp_now(); mins = n.hour * 60 + n.minute
        if mins < 510 or mins > 1200:
            return {"ok": True, "skip": "fora do horario comercial"}
    if not _term_lock.acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        print("[termometro] iniciando passada (%s)" % motivo, flush=True)
        universo = ((_last_cruz.get("result") or {}).get("universo")) or []
        if not universo:
            print("[termometro] universo do cruzamento ainda nao pronto; pulando", flush=True)
            return {"ok": True, "skip": "universo nao pronto"}
        orc = int(os.environ.get("TERM_BATCH", "20"))
        baldes = {"quente": [], "morno": [], "frio": [], "cross_sell": [],
                  "operacional": [], "revisar": [], "pendente": []}
        feitas = 0
        tratados = {}
        try:
            import firebase_writer as _fw2
            _dbt = _fw2._get_db()
            if _dbt:
                for _d in _dbt.collection("escuta_tratados").stream():
                    tratados[_d.id] = _d.to_dict()
        except Exception as _e:
            print("[termometro] tratados erro:", _e, flush=True)
        alertas = []
        for L in universo:
            cid = L.get("conv")
            if not cid:
                continue
            ident = L.get("identidade") or "novo"
            sit = L.get("situacao") or "sem_opp"
            if ident != "cliente" and sit == "perdido":
                continue  # Perdido/Desqualificado -> Reativacao (depois)
            vazamento = bool(ident != "cliente" and sit == "sem_opp")
            nome = L.get("nome"); fone_raw = L.get("telefone"); lt = L.get("lt") or ""
            chave = (lt, ident)
            cache = _TERM_CACHE.get(cid)
            if cache and cache[0] == chave:
                cls = cache[1]
            elif feitas < orc:
                cls = _jarbas.classifica_lead(cid, _TEMP_MODEL, ident)
                feitas += 1; _t.sleep(0.4)
                if cls.get("temperatura") in ("quente", "morno", "frio", "cross_sell", "operacional"):
                    _TERM_CACHE[cid] = (chave, cls)
            elif cache:
                cls = cache[1]
            else:
                cls = {"temperatura": "pendente", "motivo": "aguardando classificacao"}
            t = cls.get("temperatura")
            if t not in baldes or t == "pendente":
                t = "revisar" if t != "pendente" else "pendente"
            _ov = _TEMP_OVERRIDE.get(cid)
            if _ov in baldes and _ov not in ("revisar", "pendente"):
                t = _ov  # leitura profunda do drawer vence o batch
            tr = tratados.get(str(cid))
            alerta = ""
            if tr:
                lt_m = tr.get("lt_marcado") or ""
                if lt and lt_m and lt > lt_m:
                    continue
                if (_t.time() - float(tr.get("ts") or 0)) / 60.0 < 20:
                    continue
                alerta = "marcado sem acao"
                alertas.append({"conv": cid, "nome": nome, "marcado_em": tr.get("marcado_em")})
            baldes[t].append({"conv": cid, "nome": nome, "telefone": fone_raw, "lt": lt,
                              "identidade": ident, "motivo": cls.get("motivo", ""),
                              "alerta": alerta, "vazamento": vazamento})
        import datetime as _dtm
        _hoje = _sp_now().date().isoformat()
        _sem = (_sp_now().date() - _dtm.timedelta(days=7)).isoformat()
        _eh = _es = 0
        for L in universo:
            dcri = (L.get("criado") or "")[:10]
            if dcri:
                if dcri == _hoje:
                    _eh += 1
                if dcri >= _sem:
                    _es += 1
        try:
            _origem = ((_last_cruz.get("result") or {}).get("origem")) or {}
        except Exception:
            _origem = {}
        entrada = {"hoje": _eh, "semana": _es, "origem": _origem}
        contagem = {k: len(v) for k, v in baldes.items()}
        funil = contagem["quente"] + contagem["morno"] + contagem["frio"] + contagem["cross_sell"]
        res = {"gerado_em": _sp_now().isoformat(timespec="seconds"), "total": len(universo),
               "classificadas_no_ciclo": feitas, "em_cache": len(_TERM_CACHE),
               "fonte": "cruzamento", "gate": True,
               "funil": funil, "contagem": contagem, "entrada": entrada,
               "vazamento_total": sum(1 for _b in ("quente", "morno", "frio", "cross_sell")
                                      for _l in baldes[_b] if _l.get("vazamento")),
               "alertas": alertas, **baldes}
        _TERM["result"] = res; _TERM["quando"] = res["gerado_em"]
        try:
            import firebase_writer as _fw
            db = _fw._get_db()
            if db:
                db.collection(os.environ.get("TERM_COLL", "escuta_termometro")).document("atual").set(res)
        except Exception as e:
            print("[termometro] firestore erro:", e, flush=True)
        print("[termometro] %s funil=%d ciclo=%d cache=%d univ=%d" % (contagem, funil, feitas, len(_TERM_CACHE), len(universo)), flush=True)
        return res
    except Exception as e:
        print("[termometro] erro:", repr(e), flush=True); return {"ok": False, "erro": str(e)[:140]}
    finally:
        _term_lock.release()

_LAST_TICK = {"term": 0.0, "cruz": 0.0}

def _maybe_tick():
    # Ciclo dirigido por trafego (o painel chama a cada ~90s): nao depende do scheduler.
    import time as _tt, threading as _th
    now = _tt.time()
    try:
        if now - _LAST_TICK["term"] > 55:
            _LAST_TICK["term"] = now
            if (_last_cruz.get("result") or {}).get("universo"):
                _th.Thread(target=lambda: _rodar_termometro("tick"), daemon=True).start()
        cruz_vazio = not ((_last_cruz.get("result") or {}).get("universo"))
        if (now - _LAST_TICK["cruz"] > 1800) or (cruz_vazio and now - _LAST_TICK["cruz"] > 90):
            _LAST_TICK["cruz"] = now
            _th.Thread(target=lambda: _rodar_cruzamento("tick"), daemon=True).start()
    except Exception as _e:
        print("[tick] erro:", _e, flush=True)


@app.get("/termometro.json")
def termometro_publico():
    _maybe_tick()
    # publico: so CONTAGEM (sem PII). Lista completa so pelo token.
    r = _TERM["result"] or {}
    return {"ok": True, "quando": _TERM["quando"], "contagem": r.get("contagem"),
            "total": r.get("total"), "novas": r.get("novas_classificacoes")}

@app.get("/p/{token}/termometro.json")
def termometro_link(token: str):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    _maybe_tick()
    return {"ok": True, "quando": _TERM["quando"], "result": _TERM["result"]}


# ======================= PAINEL DE LEADS (termometro) =======================
@app.get("/p/{token}/analise")
def painel_analise(token: str, conv_id: str, nome: str = "", temperatura: str = "",
                   identidade: str = "", telefone: str = ""):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    ce = {}
    try:
        ce = _cruz.contexto_ce(telefone, nome) if telefone else {}
    except Exception as e:
        ce = {"erro": str(e)[:80]}
    r = _jarbas.analisar(conv_id, {"nome": nome, "temperatura": temperatura,
                                   "origem": identidade, "identidade": identidade, "ce": ce})
    if isinstance(r, dict):
        r["ce"] = ce
        _tp = r.get("temperatura")
        if r.get("ok") and _tp in ("quente", "morno", "frio", "cross_sell", "operacional"):
            _TEMP_OVERRIDE[conv_id] = _tp  # leitura profunda vence; reordena no proximo ciclo
    return r

@app.get("/p/{token}/leads", response_class=HTMLResponse)
def painel_leads(token: str):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    import os as _os
    _p = _os.path.join(_os.path.dirname(__file__), "painel_leads.html")
    with open(_p, encoding="utf-8") as _f:
        return HTMLResponse(_f.read())


@app.post("/p/{token}/tratar")
def painel_tratar(token: str, conv_id: str, lt: str = "", desfazer: bool = False):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    import time as _t2, firebase_writer as _fw3
    db = _fw3._get_db()
    if not db:
        return {"ok": False, "motivo": "sem db"}
    doc = db.collection("escuta_tratados").document(str(conv_id))
    if desfazer:
        doc.delete()
        return {"ok": True, "desfeito": True}
    doc.set({"conv": str(conv_id), "marcado_em": _sp_now().isoformat(timespec="seconds"),
             "ts": int(_t2.time()), "lt_marcado": lt})
    return {"ok": True}


_DBG_CRUZ = {}

@app.get("/debug_orfaos")
def _debug_cruzamento():
    import threading as _th, traceback as _tb
    def _run():
        try:
            r = _cruz.rodar_cruzamento()
            _DBG_CRUZ.clear()
            _DBG_CRUZ["ok"] = True
            _DBG_CRUZ["universo"] = len((r or {}).get("universo") or [])
            _DBG_CRUZ["buckets"] = (r or {}).get("buckets")
            _last_cruz["result"] = r
            _last_cruz["quando"] = (r or {}).get("gerado_em")
        except Exception as e:
            _DBG_CRUZ.clear()
            _DBG_CRUZ["ok"] = False
            _DBG_CRUZ["erro"] = repr(e)[:200]
            _DBG_CRUZ["tb"] = _tb.format_exc()[-1800:]
    _DBG_CRUZ.clear(); _DBG_CRUZ["ok"] = None
    _th.Thread(target=_run, daemon=True).start()
    return {"started": True}

@app.get("/debug_orfaos_res")
def _debug_cruzamento_res():
    return {"dbg": _DBG_CRUZ, "prog": _cruz.PROG}


# ======================= COLETOR UNICO (passo 2 · SPEC-Coletor-Unico 29/10) =======================
# ADITIVO: le 1x e grava o bruto (conversas/crm/vendas/meta) no Firestore + GET /saude.
# Nao altera termometro/Jarbas/painel (seguem FONTE=clinichat ate o passo 4).
import coletor as _coletor
app.include_router(_coletor.router)
_coletor.registrar_extra("tintim_webhook", lambda: {"configurado": bool(TINTIM_WEBHOOK_KEY), **_TT_STATS})
import enriquecedor as _enriq
import diario as _diario
_coletor.registrar_extra("ficha_viva", lambda: dict(_enriq.ST))

def _admin_ok(x_idm_secret: str, authorization: str) -> bool:
    # admin = segredo do Render OU login do app com master=true (Firebase ID token)
    if WEBHOOK_SECRET and x_idm_secret and hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        return True
    if authorization.startswith("Bearer "):
        try:
            _uid, _p = _usuario_leads(authorization)
            return _p.get("master") is True
        except HTTPException:
            return False
    return False

@app.post("/coletor/enriquecer")
def rota_enriquecer(modo: str = "poll", x_idm_secret: str = Header(default=""), authorization: str = Header(default="")):
    if not _admin_ok(x_idm_secret, authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    threading.Thread(target=_enriq.rodar, args=(modo, modo != "completo", True), daemon=True).start()
    return {"ok": True, "iniciado": modo}

@app.post("/diario/gerar")
def rota_diario_gerar(d: str = "", x_idm_secret: str = Header(default=""), authorization: str = Header(default="")):
    if not _admin_ok(x_idm_secret, authorization):
        raise HTTPException(status_code=401, detail="unauthorized")
    return _diario.gerar(d or None)

@app.get("/api/diario")
def api_diario(d: str = "", authorization: str = Header(default="")):
    # leitura do diario pelo app (gestao/master)
    _uid, _p = _usuario_leads(authorization)
    if not (_p.get("master") is True or _p.get("role") == "gestao"):
        raise HTTPException(status_code=403, detail="so gestao")
    return _diario.ler(d or None) or {"vazio": True}

@app.get("/p/{token}/diario", response_class=HTMLResponse)
def pagina_diario(token: str, d: str = ""):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    r = _diario.ler(d or None)
    if not r:
        return HTMLResponse("<p style='font-family:sans-serif;padding:20px'>Diario ainda nao gerado para esse dia.</p>")
    import html as _h
    t = _h.escape(r.get("texto") or "")
    return HTMLResponse("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        "<title>IDM Diario</title><body style='font-family:-apple-system,Segoe UI,sans-serif;background:#f4f6f4;"
                        "padding:16px;max-width:640px;margin:auto'><pre style='white-space:pre-wrap;background:#fff;border-radius:12px;"
                        "padding:16px;font-size:15px;line-height:1.5;box-shadow:0 1px 3px rgba(0,0,0,.08)'>" + t + "</pre>"
                        "<p style='color:#667;font-size:12px'>Previa - nada foi enviado por WhatsApp. Gerado " +
                        _h.escape(str(r.get("gerado_em"))) + "</p></body>")

@app.on_event("startup")
def _coletor_start():
    try:
        _coletor.iniciar()
    except Exception as e:
        print("[coletor] falha ao iniciar:", e, flush=True)
    # passo 3 (30/10): ficha viva + Diario em previa, no MESMO scheduler do coletor (caminho B)
    try:
        _enriq.iniciar(_coletor._SCHED)
        _diario.iniciar(_coletor._SCHED)
    except Exception as e:
        print("[passo3] falha ao iniciar:", e, flush=True)


# ======================= PAINEL DE LEADS DENTRO DO IDM (login do app, sem link secreto) =======================
# 30/10/2026. ADITIVO. O painel continua sendo o MESMO arquivo (painel_leads.html); em /app/leads ele roda
# dentro de um iframe do app IDM (Netlify) e recebe o Firebase ID token do app por postMessage.
# As rotas /api/leads/* conferem o token + a funcao do usuario (users/{uid}) e reusam as mesmas funcoes
# do painel por token (/p/{token}/...), entao o comportamento e identico.
import json
IDM_ORIGIN = os.environ.get("IDM_APP_ORIGIN", "https://instituto-mafra-sistema.netlify.app")
_LEADS_ROLES = [r.strip() for r in os.environ.get("LEADS_ROLES", "gestao,comercial,social_seller,avaliadora").split(",") if r.strip()]
_USR_CACHE = {}

def _usuario_leads(authorization: str):
    """Valida o Firebase ID token e a funcao do usuario. Retorna (uid, perfil) ou levanta 401/403."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="login necessario")
    tok = authorization[7:].strip()
    try:
        from firebase_admin import auth as _fbauth
        db = _fw._get_db()  # garante o app firebase inicializado
        dec = _fbauth.verify_id_token(tok)
    except Exception:
        raise HTTPException(status_code=401, detail="login invalido ou expirado")
    uid = dec.get("uid")
    import time as _tu
    c = _USR_CACHE.get(uid)
    if c and _tu.time() - c[0] < 300:
        perfil = c[1]
    else:
        perfil = {}
        try:
            d = db.collection("users").document(uid).get() if db else None
            perfil = (d.to_dict() or {}) if (d is not None and d.exists) else {}
        except Exception:
            perfil = {}
        _USR_CACHE[uid] = (_tu.time(), perfil)
    ok = perfil.get("master") is True or perfil.get("role") in _LEADS_ROLES or perfil.get("acessoComercial") is True
    if not ok:
        raise HTTPException(status_code=403, detail="seu usuario nao tem acesso ao painel de leads")
    return uid, perfil

@app.get("/app/leads", response_class=HTMLResponse)
def app_leads():
    # Casca do painel para o iframe do IDM. Sem dado nenhum: os dados so vem pelas /api/leads/* com login.
    import os as _os
    _p = _os.path.join(_os.path.dirname(__file__), "painel_leads.html")
    with open(_p, encoding="utf-8") as _f:
        html_ = _f.read()
    inj = '<script>window.IDM_APP=1;window.IDM_ORIGIN=%s;</script>' % json.dumps(IDM_ORIGIN)
    html_ = html_.replace("<head>", "<head>" + inj, 1) if "<head>" in html_ else inj + html_
    return HTMLResponse(html_, headers={"Content-Security-Policy": "frame-ancestors " + IDM_ORIGIN,
                                        "Cache-Control": "no-store"})

@app.get("/api/leads/termometro.json")
def api_leads_termometro(authorization: str = Header(default="")):
    _usuario_leads(authorization)
    _maybe_tick()
    return {"ok": True, "quando": _TERM["quando"], "result": _TERM["result"]}

@app.get("/api/leads/analise")
def api_leads_analise(conv_id: str, nome: str = "", temperatura: str = "", identidade: str = "",
                      telefone: str = "", authorization: str = Header(default="")):
    _usuario_leads(authorization)
    return painel_analise(PANEL_TOKEN, conv_id, nome, temperatura, identidade, telefone)

@app.post("/api/leads/tratar")
def api_leads_tratar(conv_id: str, lt: str = "", desfazer: bool = False, authorization: str = Header(default="")):
    uid, perfil = _usuario_leads(authorization)
    r = painel_tratar(PANEL_TOKEN, conv_id, lt, desfazer)
    try:  # registra QUEM marcou (auditoria da conduta)
        db = _fw._get_db()
        if db and not desfazer:
            db.collection("escuta_tratados").document(str(conv_id)).set(
                {"por_uid": uid, "por_nome": perfil.get("name")}, merge=True)
    except Exception:
        pass
    return r


# ======================= FICHAS (validacao das categorias - rascunho) =======================
# 30/10: pagina so-leitura para o Xandao validar o rascunho das categorias com casos reais.
@app.get("/p/{token}/fichas", response_class=HTMLResponse)
def pagina_fichas(token: str, f: str = "", limite: int = 80):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    import html as _h
    db = _fw._get_db()
    if not db:
        return HTMLResponse("sem banco")
    rows = [d.to_dict() or {} for d in db.collection("escuta_leads").stream()]
    rows = [r for r in rows if r.get("ia") and r.get("identidade") != "sem_telefone"]
    if f:
        fl = f.lower()
        rows = [r for r in rows if fl in json.dumps(r.get("ia"), ensure_ascii=False).lower() or fl in (r.get("origem") or "").lower()
                or fl in (r.get("identidade") or "")]
    rows.sort(key=lambda r: r.get("ultima_msg_em") or "", reverse=True)
    from collections import Counter as _C
    cs, cc, ci, cn = _C(), _C(), _C(), _C()
    for r in rows:
        ia = r["ia"]
        cs.update(ia.get("servicos") or []); cc.update(ia.get("cirurgias_origem") or [])
        ci.update([ia.get("intencao")]); cn.update([ia.get("abordagem_nota")])
    def top(c): return " · ".join("%s <b>%d</b>" % (_h.escape(str(k)), v) for k, v in c.most_common(12))
    e = lambda x: _h.escape(str(x if x is not None else ""))
    trs = []
    for r in rows[:max(1, min(limite, 500))]:
        ia = r["ia"]
        mom = "; ".join("%s: “%s”" % (m.get("tipo"), (m.get("trecho") or "")[:60]) for m in (ia.get("momentos") or []))
        trs.append("<tr><td><b>%s</b><br><small>%s · %s</small></td><td>%s</td><td>%s</td><td>%s</td><td>%s<br><small>%s</small></td>"
                   "<td><small>%s</small></td><td><small>%s</small></td><td><small>%s</small></td></tr>" % (
                       e(r.get("nome") or r.get("fone")), e(r.get("identidade")), e(r.get("origem")),
                       e(", ".join(ia.get("servicos") or [])), e(", ".join(ia.get("cirurgias_origem") or [])), e(ia.get("intencao")),
                       e(ia.get("abordagem_nota")), e(", ".join(x for x in (ia.get("abordagem_falhas") or []) if x != "nenhuma")),
                       e(mom), e(ia.get("resumo")), e(", ".join(r.get("furos") or []))))
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>IDM Fichas (rascunho)</title>"
            "<style>body{font-family:-apple-system,Segoe UI,sans-serif;margin:0;padding:14px;background:#f4f6f4;color:#17201c}"
            "h1{font-size:20px;margin:0 0 4px}.k{background:#fff;border-radius:10px;padding:10px 12px;margin:8px 0;font-size:13px;line-height:1.6}"
            "table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}td,th{border-bottom:1px solid #e3e7e3;padding:7px;vertical-align:top;text-align:left}"
            "th{background:#eef2ef;position:sticky;top:0}.wrap{overflow-x:auto}form{margin:8px 0}input{padding:7px;border:1px solid #ccd;border-radius:8px;width:240px}</style>"
            "<h1>Fichas — categorias em RASCUNHO (%d com análise)</h1>"
            "<div style='color:#5e6b64;font-size:12px'>Só leitura. Para validar a lista e a régua. Fechou/não fechou nunca vem da IA.</div>"
            "<form><input name=f value='%s' placeholder='filtrar: abdominoplastia, laser, com_falhas, Tráfego...'> <button>filtrar</button></form>"
            "<div class=k><b>Serviços:</b> %s<br><b>Cirurgias de origem:</b> %s<br><b>Intenção:</b> %s<br><b>Abordagem:</b> %s</div>"
            "<div class=wrap><table><tr><th>Pessoa</th><th>Serviço</th><th>Cirurgia</th><th>Intenção</th><th>Abordagem</th><th>Momentos-chave</th><th>Resumo</th><th>Furos (regra)</th></tr>%s</table></div>"
            ) % (len(rows), e(f), top(cs), top(cc), top(ci), top(cn), "".join(trs))
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})

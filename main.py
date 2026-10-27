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
def _startup():
    sched = BackgroundScheduler(timezone="America/Sao_Paulo")
    sched.add_job(lambda: _rodar("poll"), "interval", minutes=POLL_MINUTES)
    sched.add_job(_vigia_cracha, "interval", minutes=30)
    sched.add_job(lambda: _rodar_orfaos("poll"), "interval", minutes=5)
    sched.add_job(lambda: _rodar_termometro("poll"), "interval", minutes=5)
    try:
        _vigia_cracha()
    except Exception:
        pass
    sched.start()
    try:
        threading.Thread(target=_rodar_orfaos, args=("startup",), daemon=True).start()
    except Exception:
        pass
    try:
        threading.Timer(25, lambda: _rodar_termometro("startup")).start()
        threading.Timer(95, _auditar_quentes).start()
    except Exception:
        pass
    print(f"[startup] poll {POLL_MINUTES}min; vigia cracha 30min; orfaos 15min")

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
        threading.Thread(target=_rodar_orfaos, args=("webhook",), daemon=True).start()
        return {"ok": True, "acionado": True, "detalhe": _rodar(f"webhook:{evento}")}
    return {"ok": True, "acionado": False, "evento": evento}

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


# ---- Bloco B: motor de orfaos (conversa = verdade; so-leitura) ----
import motor_orfaos as _orf
_last_orf = {"result": None, "quando": None}
_orf_lock = threading.Lock()

def _sp_now():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _rodar_orfaos(motivo="poll"):
    if motivo == "poll":
        n = _sp_now()
        mins = n.hour * 60 + n.minute
        if mins < 510 or mins > 1200:
            return {"ok": True, "skip": "fora do horario comercial"}
    if not _orf_lock.acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        res = _orf.run_orfaos()
        _last_orf["result"] = res
        _last_orf["quando"] = res.get("gerado_em")
        return res
    except Exception as e:
        print("[orfaos] erro:", e)
        return {"ok": False, "erro": str(e)[:160]}
    finally:
        _orf_lock.release()

@app.post("/run_orfaos")
def run_orfaos_now(x_idm_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_idm_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")
    threading.Thread(target=_rodar_orfaos, args=("manual",), daemon=True).start()
    return {"ok": True, "started": True}

@app.get("/orfaos.json")
def orfaos_json():
    return {"ok": True, "quando": _last_orf["quando"], "result": _last_orf["result"]}


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
    convs = _orf._lista("inbox", cap=80)
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
_TEMP_MODEL = os.environ.get("JARBAS_TEMP_MODEL", "claude-haiku-5-5")

def _rodar_termometro(motivo="poll"):
    import time as _t
    if motivo == "poll":
        n = _sp_now(); mins = n.hour * 60 + n.minute
        if mins < 510 or mins > 1200:
            return {"ok": True, "skip": "fora do horario comercial"}
    if not _term_lock.acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        print("[termometro] iniciando passada (%s)" % motivo)
        convs = _orf._lista("inbox", cap=80)
        orc = int(os.environ.get("TERM_BATCH", "12"))
        baldes = {"quente": [], "morno": [], "frio": [], "pendente": []}
        feitas = 0
        for c in convs:
            cid = c.get("conversationId")
            if not cid:
                continue
            ct = c.get("contact") or {}
            lt = c.get("lastMessageTime") or (c.get("conversation") or {}).get("lastActivityTime") or ""
            cache = _TERM_CACHE.get(cid)
            if cache and (not lt or cache[0] == lt):
                cls = cache[1]
            elif feitas < orc:
                cls = _jarbas.classifica_temp(cid, _TEMP_MODEL)
                _TERM_CACHE[cid] = (lt, cls); feitas += 1; _t.sleep(0.4)
            elif cache:
                cls = cache[1]
            else:
                cls = {"temperatura": "pendente", "motivo": "aguardando classificacao"}
            t = cls.get("temperatura")
            if t not in baldes:
                t = "frio"
            baldes[t].append({"conv": cid, "nome": ct.get("name"),
                              "telefone": ct.get("phone") or c.get("identifier"),
                              "motivo": cls.get("motivo", "")})
        res = {"gerado_em": _sp_now().isoformat(timespec="seconds"), "total": len(convs),
               "classificadas_no_ciclo": feitas, "em_cache": len(_TERM_CACHE),
               "contagem": {k: len(v) for k, v in baldes.items()}, **baldes}
        _TERM["result"] = res; _TERM["quando"] = res["gerado_em"]
        try:
            import firebase_writer as _fw
            db = _fw._get_db()
            if db:
                db.collection(os.environ.get("TERM_COLL", "escuta_termometro")).document("atual").set(res)
        except Exception as e:
            print("[termometro] firestore erro:", e)
        print("[termometro] %s | ciclo=%d cache=%d" % (res["contagem"], feitas, len(_TERM_CACHE)))
        return res
    except Exception as e:
        print("[termometro] erro:", repr(e)); return {"ok": False, "erro": str(e)[:140]}
    finally:
        _term_lock.release()

def _auditar_quentes():
    """One-shot: loga nos logs do Render a conversa real dos quentes, pra calibracao. Sem PII em URL."""
    import time as _t
    for _ in range(40):
        if _TERM.get("result"): break
        _t.sleep(3)
    res = _TERM.get("result") or {}
    quentes = (res.get("quente") or [])[:4]
    print("[AUDITA] ===== %d quentes para auditoria de calibracao =====" % len(quentes), flush=True)
    for q in quentes:
        cid = q.get("conv")
        try:
            linhas, _ld = _jarbas._transcript(cid, max_msgs=25)
        except Exception as e:
            print("[AUDITA] conv %s ERRO transcript: %r" % (cid, e), flush=True); continue
        print("[AUDITA] ---- conv=%s | Haiku=QUENTE | motivo: %s" % (cid, (q.get("motivo") or "")[:160]), flush=True)
        if not linhas:
            print("[AUDITA]   (sem conversa legivel)", flush=True); continue
        for ln in linhas[-14:]:
            print("[AUDITA]   " + str(ln)[:240], flush=True)
    print("[AUDITA] ===== fim =====", flush=True)

@app.get("/termometro.json")
def termometro_publico():
    # publico: so CONTAGEM (sem PII). Lista completa so pelo token.
    r = _TERM["result"] or {}
    return {"ok": True, "quando": _TERM["quando"], "contagem": r.get("contagem"),
            "total": r.get("total"), "novas": r.get("novas_classificacoes")}

@app.get("/p/{token}/termometro.json")
def termometro_link(token: str):
    if not PANEL_TOKEN or not hmac.compare_digest(token, PANEL_TOKEN):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "quando": _TERM["quando"], "result": _TERM["result"]}

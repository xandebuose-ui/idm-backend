# -*- coding: utf-8 -*-
import os, json, urllib.request, urllib.error, ssl
from fastapi import APIRouter
from fastapi.responses import JSONResponse
router = APIRouter()
_CTX = ssl.create_default_context()
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"
def _req(method, url, headers=None, data=None, timeout=20):
    h = dict(headers or {})
    h.setdefault("User-Agent", _UA)
    h.setdefault("Accept", "application/json, text/plain, */*")
    h.setdefault("Accept-Language", "pt-BR,pt;q=0.9,en;q=0.8")
    try:
        req = urllib.request.Request(url, method=method, headers=h)
        if data is not None:
            req.data = json.dumps(data).encode(); req.add_header("Content-Type","application/json")
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.status, r.read(30000).decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        try: return e.code, e.read(30000).decode("utf-8","replace")
        except Exception: return e.code, ""
    except Exception as e:
        return "ERR", (type(e).__name__+": "+str(e))[:140]
def _keys(o, n=25):
    try:
        if isinstance(o, dict): return list(o.keys())[:n]
    except Exception: pass
    return None
def _sniff(bd):
    low=(bd or "").lower()
    for m in ("error code: 1010","just a moment","attention required","cloudflare","access denied","<!doctype html"):
        if m in low: return m
    return "(ok/sem bloqueio)"
@router.get("/diag")
def diag():
    out={"nota":"diag v3 (User-Agent de navegador)"}
    key=os.environ.get("CE_API_KEY")
    if key:
        st,bd=_req("GET","https://api.clinicaexperts.com.br/api/v1/patients?page=1",headers={"Authorization":"Bearer "+key})
        e={"status":st,"sniff":_sniff(bd)}
        if st==200:
            try: e["total"]=(json.loads(bd).get("meta") or {}).get("of")
            except Exception: pass
        else: e["msg"]=(bd or "")[:160]
        out["api_oficial_com_chave"]=e
    email=os.environ.get("CE_EMAIL"); senha=os.environ.get("CE_SENHA")
    if not email or not senha:
        out["login"]={"erro":"sem CE_EMAIL/CE_SENHA"}; return JSONResponse(out)
    tentativas=[]; tok=None
    for body in ({"email":email,"password":senha},{"login":email,"password":senha},{"username":email,"password":senha}):
        st,bd=_req("POST","https://api.clinicaexperts.com.br/api/login",data=body)
        try:
            j=json.loads(bd); msg=str(j.get("message") or j.get("error") or "")[:120]
        except Exception:
            j={}; msg=(bd or "")[:120]
        tentativas.append({"fmt":list(body.keys()),"status":st,"msg":msg,"sniff":_sniff(bd)})
        if st==200:
            data=j.get("data") if isinstance(j.get("data"),dict) else {}
            t=j.get("token") or j.get("access_token") or j.get("jwt") or data.get("token") or data.get("access_token")
            if t: tok=t; out["login_resp_keys"]=_keys(j); out["login_data_keys"]=_keys(data); break
    out["login_tentativas"]=tentativas; out["login_ok"]=bool(tok)
    if tok:
        st,bd=_req("GET","https://clinichat.clinicaexperts.com.br/contact-channel?from=0&to=1&orderBy=lastMessageTime%3ADESC&filters=%7B%7D",headers={"Authorization":"Bearer "+tok})
        e={"status":st,"sniff":_sniff(bd)}
        if st==200:
            try: e["total"]=(json.loads(bd).get("pagination") or {}).get("total")
            except Exception: pass
        else: e["msg"]=(bd or "")[:160]
        out["clinichat_com_app_jwt"]=e
        probes={}
        for name,url in [("api_me","https://api.clinicaexperts.com.br/api/me"),("clinichat_token","https://api.clinicaexperts.com.br/api/clinichat/token"),("communication_token","https://api.clinicaexperts.com.br/api/communication/token"),("contact_channel_api","https://api.clinicaexperts.com.br/api/contact-channel?from=0&to=1")]:
            st,bd=_req("GET",url,headers={"Authorization":"Bearer "+tok})
            ee={"status":st}
            if st==200:
                try: ee["keys"]=_keys(json.loads(bd))
                except Exception: pass
            probes[name]=ee
        out["probe_token_clinichat"]=probes
    return JSONResponse(out)

@router.get("/diag_browser")
def diag_browser():
    """Teste de viabilidade: sobe Chromium headless no Render e tenta carregar o app do CE.
    So reporta booleans/contagens. NAO envia senha ainda (so detecta desafio Cloudflare)."""
    import subprocess, sys
    out = {"nota": "teste Playwright/Cloudflare — so booleans/contagens"}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return JSONResponse({"erro_import": str(e)[:160]})
    try:
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           capture_output=True, text=True, timeout=300)
        out["chromium_install_rc"] = r.returncode
        if r.returncode != 0:
            out["chromium_install_err"] = (r.stderr or "")[-200:]
    except Exception as e:
        out["chromium_install_err"] = str(e)[:160]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"])
            ctx = browser.new_context(user_agent=_UA, locale="pt-BR",
                viewport={"width": 1280, "height": 800})
            pg = ctx.new_page()
            pg.goto("https://app.clinicaexperts.com.br/", wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(3500)
            out["url"] = (pg.url or "")[:120]
            out["title"] = (pg.title() or "")[:80]
            html = (pg.content() or "").lower()
            out["cf_challenge"] = any(m in html for m in
                ["just a moment", "checking your browser", "cf-challenge",
                 "attention required", "cf-browser-verification", "error code: 1010"])
            out["inputs_email_ou_texto"] = pg.locator("input[type=email], input[name='email'], input[type=text]").count()
            out["inputs_senha"] = pg.locator("input[type=password]").count()
            out["botoes"] = pg.locator("button, input[type=submit]").count()
            browser.close()
    except Exception as e:
        out["erro_run"] = str(e)[:200]
    return JSONResponse(out)

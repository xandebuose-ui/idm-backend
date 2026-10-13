# -*- coding: utf-8 -*-
import os, json, urllib.request, urllib.error, ssl
from fastapi import APIRouter
from fastapi.responses import JSONResponse
router = APIRouter()
_CTX = ssl.create_default_context()
def _req(method, url, headers=None, data=None, timeout=20):
    try:
        req = urllib.request.Request(url, method=method, headers=dict(headers or {}))
        if data is not None:
            req.data = json.dumps(data).encode()
            req.add_header("Content-Type","application/json"); req.add_header("Accept","application/json")
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
    for m in ("just a moment","attention required","cloudflare","access denied","forbidden","<!doctype html"):
        if m in low: return m
    return "(sem bloqueio html)"
@router.get("/diag")
def diag():
    out={"nota":"diag v2 — status/contagem/mensagens; sem tokens/PII"}
    reach={}
    for name,url in [("clinichat_root","https://clinichat.clinicaexperts.com.br/"),
                     ("api_v1_sem_chave","https://api.clinicaexperts.com.br/api/v1/patients?page=1"),
                     ("firestore","https://firestore.googleapis.com/v1/projects/instituto-mafra/databases/(default)/documents")]:
        st,bd=_req("GET",url); reach[name]={"status":st,"sniff":_sniff(bd),"len":len(bd or "")}
    out["alcancabilidade"]=reach
    key=os.environ.get("CE_API_KEY")
    if key:
        st,bd=_req("GET","https://api.clinicaexperts.com.br/api/v1/patients?page=1",headers={"Authorization":"Bearer "+key})
        e={"status":st}
        if st==200:
            try: e["total"]=(json.loads(bd).get("meta") or {}).get("of")
            except Exception: pass
        else: e["msg"]=(bd or "")[:160]; e["sniff"]=_sniff(bd)
        out["api_oficial_com_chave"]=e
    else:
        out["api_oficial_com_chave"]={"erro":"sem CE_API_KEY"}
    email=os.environ.get("CE_EMAIL"); senha=os.environ.get("CE_SENHA")
    if not email or not senha:
        out["login"]={"erro":"sem CE_EMAIL/CE_SENHA"}; return JSONResponse(out)
    out["login_email_dominio"]=("@"+email.split("@")[-1]) if "@" in email else "(sem @)"
    tentativas=[]; tok=None
    for body in ({"email":email,"password":senha},{"login":email,"password":senha},{"username":email,"password":senha},{"email":email,"senha":senha}):
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
        e={"status":st}
        if st==200:
            try: e["total"]=(json.loads(bd).get("pagination") or {}).get("total")
            except Exception: pass
        else: e["msg"]=(bd or "")[:160]
        out["clinichat_com_app_jwt"]=e
        probes={}
        for name,url in [("api_me","https://api.clinicaexperts.com.br/api/me"),("api_user","https://api.clinicaexperts.com.br/api/user"),("clinichat_token","https://api.clinicaexperts.com.br/api/clinichat/token"),("communication_token","https://api.clinicaexperts.com.br/api/communication/token"),("contact_channel_api","https://api.clinicaexperts.com.br/api/contact-channel?from=0&to=1")]:
            st,bd=_req("GET",url,headers={"Authorization":"Bearer "+tok})
            ee={"status":st}
            if st==200:
                try: ee["keys"]=_keys(json.loads(bd))
                except Exception: pass
            probes[name]=ee
        out["probe_token_clinichat"]=probes
    return JSONResponse(out)

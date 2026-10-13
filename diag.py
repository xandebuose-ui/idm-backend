# -*- coding: utf-8 -*-
"""
diag.py — rota de DIAGNOSTICO temporaria do IDM Backend.
Do ambiente do Render, descobre (1) alcance de clinichat/firestore e (2) o caminho
do token do clinichat. So retorna status/contagem/NOMES de campo. NUNCA tokens,
senhas, nomes de paciente ou conteudo de conversa. Rota publica de proposito
(nada sensivel). Remover quando o caminho estiver cravado.
"""
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
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.status, r.read(30000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(30000).decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return "ERR", (type(e).__name__ + ": " + str(e))[:140]

def _keys(obj, limit=25):
    try:
        if isinstance(obj, dict):
            return list(obj.keys())[:limit]
    except Exception:
        pass
    return None

def _login():
    email = os.environ.get("CE_EMAIL"); senha = os.environ.get("CE_SENHA")
    if not email or not senha:
        return None, {"ok": False, "motivo": "sem CE_EMAIL/CE_SENHA no env"}
    API = "https://api.clinicaexperts.com.br/api/login"
    for body in ({"email": email, "password": senha},
                 {"login": email, "password": senha},
                 {"username": email, "password": senha},
                 {"email": email, "senha": senha}):
        st, bd = _req("POST", API, data=body)
        if st == 200:
            try: j = json.loads(bd)
            except Exception: j = {}
            data = j.get("data") if isinstance(j.get("data"), dict) else {}
            tok = (j.get("token") or j.get("access_token") or j.get("jwt")
                   or data.get("token") or data.get("access_token") or data.get("jwt"))
            info = {"ok": bool(tok), "formato_login": list(body.keys()),
                    "resp_keys": _keys(j), "data_keys": _keys(data)}
            if tok:
                return tok, info
            return None, info
    return None, {"ok": False, "motivo": "login nao respondeu 200 em nenhum formato"}

@router.get("/diag")
def diag():
    out = {"nota": "diagnostico temporario — so status/contagem/nomes de campo"}
    reach = {}
    for name, url in [
        ("clinichat_root", "https://clinichat.clinicaexperts.com.br/"),
        ("clinichat_contact", "https://clinichat.clinicaexperts.com.br/contact-channel?from=0&to=1"),
        ("firestore", "https://firestore.googleapis.com/v1/projects/instituto-mafra/databases/(default)/documents"),
        ("api_v1", "https://api.clinicaexperts.com.br/api/v1/patients"),
    ]:
        st, _ = _req("GET", url)
        reach[name] = st
    out["alcancabilidade"] = reach
    tok, loginfo = _login()
    out["login"] = loginfo
    if tok:
        H = {"Authorization": "Bearer " + tok}
        st, bd = _req("GET",
            "https://clinichat.clinicaexperts.com.br/contact-channel?from=0&to=1&orderBy=lastMessageTime%3ADESC&filters=%7B%7D",
            headers=H)
        entry = {"status": st}
        if st == 200:
            try: entry["total"] = (json.loads(bd).get("pagination") or {}).get("total")
            except Exception: pass
        out["clinichat_com_app_jwt"] = entry
        probes = {}
        for name, url in [
            ("api_me", "https://api.clinicaexperts.com.br/api/me"),
            ("api_user", "https://api.clinicaexperts.com.br/api/user"),
            ("clinichat_token", "https://api.clinicaexperts.com.br/api/clinichat/token"),
            ("communication_token", "https://api.clinicaexperts.com.br/api/communication/token"),
            ("chat_token", "https://api.clinicaexperts.com.br/api/chat/token"),
            ("contact_channel_api", "https://api.clinicaexperts.com.br/api/contact-channel?from=0&to=1"),
        ]:
            st, bd = _req("GET", url, headers=H)
            e = {"status": st}
            if st == 200:
                try: e["keys"] = _keys(json.loads(bd))
                except Exception: pass
            probes[name] = e
        out["probe_token_clinichat"] = probes
    return JSONResponse(out)

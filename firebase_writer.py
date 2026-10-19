# -*- coding: utf-8 -*-
"""
Grava a fila no Firestore do projeto 'instituto-mafra'.
UM único documento snapshot: escuta_fila/atual = {gerado_em, total, resumo, fila:[...]}.
→ 1 escrita por rodada (não 137), barato, sem docs órfãos, e o app lê 1 doc ao vivo.
Acesso de leitura é controlado pelas Regras do Firestore (ver deploy/firestore-rules-escuta.txt).
Usa firebase-admin com conta de serviço (GOOGLE_APPLICATION_CREDENTIALS).
Se as credenciais não estiverem presentes, NÃO quebra o motor — só loga e segue.
"""
import os

_COLL = os.environ.get("ESCUTA_COLLECTION", "escuta_fila")
_DOC = os.environ.get("ESCUTA_DOC", "atual")
_db = None

def _get_db():
    global _db
    if _db is not None:
        return _db
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path or not os.path.isfile(cred_path):
        print("[firebase] sem conta de serviço — pulando gravação (defina GOOGLE_APPLICATION_CREDENTIALS)")
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(cred_path),
                                          {"projectId": os.environ.get("FIREBASE_PROJECT_ID", "instituto-mafra")})
        _db = firestore.client()
        return _db
    except Exception as e:
        print("[firebase] falha ao iniciar:", e)
        return None

def push_fila(resultado):
    """Grava 1 doc snapshot 'atual' com a fila inteira. Idempotente (sobrescreve)."""
    db = _get_db()
    if db is None:
        return False
    try:
        db.collection(_COLL).document(_DOC).set({
            "gerado_em": resultado["gerado_em"],
            "total_leads_pipeline": resultado["total_leads_pipeline"],
            "resumo": resultado["resumo"],
            "fila": resultado["fila"],
        })
        print(f"[firebase] fila gravada em '{_COLL}/{_DOC}' ({len(resultado['fila'])} leads)")
        return True
    except Exception as e:
        print("[firebase] erro ao gravar:", e)
        return False




# ---- crachá (token do CliniChat) — monitor de conversa ----
_TOKEN_COLL = os.environ.get("ESCUTA_TOKEN_COLL", "escuta_token")
_TOKEN_DOC = os.environ.get("ESCUTA_TOKEN_DOC", "clinichat")

def _decode_exp(token):
    try:
        import json, base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None

def save_token(token, origem=None):
    db = _get_db()
    if db is None:
        print("[token] sem firestore — nao guardou")
        return False
    try:
        import time
        db.collection(_TOKEN_COLL).document(_TOKEN_DOC).set({
            "token": token,
            "exp": _decode_exp(token),
            "atualizado_em": int(time.time()),
            "origem": (origem or "")[:60],
        })
        print("[token] crachá guardado")
        return True
    except Exception as e:
        print("[token] erro ao guardar:", e)
        return False

def get_token():
    db = _get_db()
    if db is None:
        return None
    try:
        d = db.collection(_TOKEN_COLL).document(_TOKEN_DOC).get()
        if d.exists:
            return d.to_dict().get("token")
    except Exception as e:
        print("[token] erro leitura:", e)
    return None

def token_info():
    import time
    db = _get_db()
    if db is None:
        return {"ok": False, "motivo": "sem firestore"}
    try:
        d = db.collection(_TOKEN_COLL).document(_TOKEN_DOC).get()
        if not d.exists:
            return {"ok": True, "tem_token": False, "motivo": "nunca recebido"}
        v = d.to_dict()
        exp = v.get("exp")
        agora = int(time.time())
        return {
            "ok": True,
            "tem_token": bool(v.get("token")),
            "atualizado_em": v.get("atualizado_em"),
            "exp": exp,
            "expira_em_horas": (round((exp - agora) / 3600, 1) if exp else None),
            "vencido": (exp is not None and exp < agora),
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:100]}

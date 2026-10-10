# -*- coding: utf-8 -*-
"""
Grava a fila no Firestore do projeto 'instituto-mafra', coleção SEPARADA e trancada
'escuta_fila' (dado de paciente — só gestão/master lê, ver deploy/firestore-rules-escuta.txt).
Usa firebase-admin com uma conta de serviço (GOOGLE_APPLICATION_CREDENTIALS).
Se as credenciais não estiverem presentes, NÃO quebra o motor — só loga e segue.
"""
import os

_COLL = os.environ.get("ESCUTA_COLLECTION", "escuta_fila")
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
    """Grava 1 doc de resumo + 1 doc por lead (id = opp_uuid). Idempotente (merge)."""
    db = _get_db()
    if db is None:
        return False
    try:
        col = db.collection(_COLL)
        # doc de resumo (sempre 'atual')
        col.document("_resumo").set({
            "gerado_em": resultado["gerado_em"],
            "total_leads_pipeline": resultado["total_leads_pipeline"],
            "resumo": resultado["resumo"],
        })
        batch = db.batch()
        for L in resultado["fila"]:
            doc_id = L.get("opp_uuid") or (L.get("nome") or "sem-nome")
            batch.set(col.document(doc_id), L)
        batch.commit()
        print(f"[firebase] fila gravada em '{_COLL}' ({len(resultado['fila'])} leads)")
        return True
    except Exception as e:
        print("[firebase] erro ao gravar:", e)
        return False

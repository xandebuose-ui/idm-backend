# -*- coding: utf-8 -*-
"""
IDM - passo 4 (30/10/2026): as frentes leem do BANCO (gravado pelo coletor), nao da CliniChat.
Chaves (env): FONTE_LISTA / FONTE_CONVERSA = firestore (padrao) | clinichat.
Se o banco nao tiver o dado (conversa recem-chegada, indice velho), devolve None -> quem chamou cai na CliniChat.
"""
import os, time, datetime as dt

import firebase_writer as _fw

_MODO = {"t": 0.0, "lista": "clinichat", "conversa": "clinichat"}

def modo(chave):
    """Chave por frente, guardada no banco (escuta_config/fonte). Comeca em 'clinichat'; vira 'firestore' apos a prova."""
    now = time.time()
    if now - _MODO["t"] > 60:
        try:
            d = _fw._get_db().collection("escuta_config").document("fonte").get()
            v = d.to_dict() or {} if d.exists else {}
            _MODO.update({"t": now, "lista": v.get("lista", "clinichat"), "conversa": v.get("conversa", "clinichat")})
        except Exception:
            _MODO["t"] = now
    return os.environ.get("FONTE_" + chave.upper()) or _MODO.get(chave, "clinichat")

def definir_modo(lista=None, conversa=None):
    v = {}
    if lista:
        v["lista"] = lista
    if conversa:
        v["conversa"] = conversa
    _fw._get_db().collection("escuta_config").document("fonte").set(v, merge=True)
    _MODO["t"] = 0.0
    return {"ok": True, **v}

ST = {"indice_lido_em": None, "indice_n": 0, "indice_idade_min": None, "hits": {}, "fallbacks": {}}
_IDX = {"t": 0.0, "itens": None, "por_cid": {}, "gravado_em": None}

def _conta(k, hit=True):
    d = ST["hits"] if hit else ST["fallbacks"]
    d[k] = d.get(k, 0) + 1

def _idade_min(iso):
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - d).total_seconds() / 60.0
    except Exception:
        return None

def indice(ttl=50, max_idade_min=15, forcar=False):
    """Lista de conversas no mesmo formato do contact-channel. None se o indice estiver velho ou ausente."""
    if not forcar and modo("lista") != "firestore":
        return None
    now = time.time()
    if _IDX["itens"] is None or now - _IDX["t"] > ttl:
        try:
            d = _fw._get_db().collection("escuta_indice").document("conversas").get()
            v = d.to_dict() or {} if d.exists else {}
            itens = v.get("itens") or []
            _IDX.update({"t": now, "itens": itens, "gravado_em": v.get("gravado_em"),
                         "por_cid": {str(x.get("conversationId")): x for x in itens}})
        except Exception:
            _IDX.update({"t": now, "itens": None})
    idade = _idade_min(_IDX.get("gravado_em"))
    ST.update({"indice_lido_em": _IDX.get("gravado_em"), "indice_n": len(_IDX.get("itens") or []),
               "indice_idade_min": round(idade, 1) if idade is not None else None})
    if not _IDX.get("itens") or idade is None or idade > max_idade_min:
        _conta("indice", False)
        return None
    _conta("indice", True)
    return list(_IDX["itens"])

def _fone_do_cid(cid):
    import motor_cruzamento as _cruz
    if not _IDX.get("por_cid"):
        indice(forcar=True)
    c = (_IDX.get("por_cid") or {}).get(str(cid))
    if not c:
        return None
    ct = c.get("contact") or {}
    return _cruz._fone(ct.get("phone") or c.get("identifier"))

def msgs(cid, forcar=False):
    """Mensagens gravadas da conversa (ordem cronologica). None se nada gravado."""
    if not forcar and modo("conversa") != "firestore":
        return None
    fk = _fone_do_cid(cid)
    if not fk:
        return None
    try:
        ref = _fw._get_db().collection(os.environ.get("COLETOR_COLL_CONV", "escuta_conversas")).document(fk)
        out = []
        for col in ref.collections():
            if not col.id.startswith("msgs_"):
                continue
            for d in col.stream():
                m = d.to_dict() or {}
                if str(m.get("conv")) == str(cid):
                    out.append(m)
        out = [m for m in out if m.get("data")]
        out.sort(key=lambda m: m["data"])
        return out or None
    except Exception:
        return None

_MARCA = {"audio": "[audio]", "ptt": "[audio]", "voice": "[audio]", "image": "[imagem]", "photo": "[imagem]",
          "video": "[video]", "document": "[arquivo]", "file": "[arquivo]", "sticker": "[figurinha]",
          "location": "[localizacao]", "contact": "[contato]"}

def transcript(cid, max_msgs=40, forcar=False):
    ms = msgs(cid, forcar)
    if not ms:
        _conta("transcript", False)
        return None
    linhas, last = [], ""
    for m in ms[-max_msgs:]:
        if m.get("de") not in ("lead", "equipe"):
            continue
        t = (m.get("texto") or "").strip()
        if not t and (m.get("transcricao") or "").strip():
            t = "[audio transcrito] " + m["transcricao"].strip()
        if not t and m.get("midia"):
            t = _MARCA.get((m.get("tipo") or "").lower(), "[%s]" % (m.get("tipo") or "midia"))
        if not t:
            continue
        linhas.append("%s: %s" % ("Cliente" if m.get("de") == "lead" else "Atendente", t))
        last = m.get("data") or last
    if not linhas:
        _conta("transcript", False)
        return None
    _conta("transcript", True)
    return linhas, last

def ultima(cid, lt, forcar=False):
    """Ultima mensagem (lead/equipe) gravada. None se o banco ainda nao tem a mensagem mais recente (lt)."""
    ms = msgs(cid, forcar)
    if not ms:
        _conta("ultima", False)
        return None
    ms = [m for m in ms if m.get("de") in ("lead", "equipe")]
    if not ms:
        return None
    m = ms[-1]
    lt_m = _idade_min(lt)
    da_m = _idade_min(m.get("data"))
    if lt_m is not None and da_m is not None and da_m - lt_m > 2:      # banco atrasado em relacao a CliniChat
        _conta("ultima", False)
        return None
    _conta("ultima", True)
    return m

def primeira_do_lead(cid):
    ms = msgs(cid)
    if not ms:
        _conta("primeira", False)
        return None
    for m in ms:
        if m.get("de") == "lead" and (m.get("texto") or "").strip():
            _conta("primeira", True)
            return m["texto"].strip()
    return None

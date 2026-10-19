# -*- coding: utf-8 -*-
# IDM - Motor de Orfaos (Bloco B). Conversa (CliniChat) e a fonte de verdade.
# So-leitura. PII (nome/telefone) vai SO pro Firestore trancado, nunca pro repo publico.
import os, re, json, time, datetime as dt
import urllib.request, urllib.error, urllib.parse
import motor_fila as mf
import firebase_writer as fw

CLINI = "https://clinichat.clinicaexperts.com.br"

def _cc(path):
    tok = fw.get_token()
    if not tok:
        return None, "sem_cracha"
    req = urllib.request.Request(CLINI + path, headers={
        "Authorization": "Bearer " + tok,
        "User-Agent": mf.UA,
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8", "ignore")), None
    except urllib.error.HTTPError as e:
        return None, "http_%s" % e.code
    except Exception as e:
        return None, str(e)[:60]

def _fone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) >= 12 and d.startswith("55"):
        d = d[2:]
    if len(d) >= 10:
        d = d[:2] + d[-8:]
    return d

def _lista(status, cap=600):
    out, frm, step = [], 0, 50
    while frm < cap:
        f = urllib.parse.quote(json.dumps({"status": status}))
        data, err = _cc("/contact-channel?from=%d&to=%d&orderBy=lastMessageTime%%3ADESC&filters=%s" % (frm, frm + step, f))
        if err or not data:
            break
        ccs = data.get("contactChannels") or []
        out += ccs
        total = ((data.get("pagination") or {}).get("total")) or 0
        frm += step
        if frm >= total or not ccs:
            break
        time.sleep(0.2)
    return out

_RUIDO = re.compile(r"confirme o seu hor|j. est. reservad|uhuull|para confirmar o agendamento|seu agendamento foi|lembrete de agendamento|avalia..o de atendimento|pesquisa de satisfa", re.I)
_FORN = re.compile(r"represent|fornecedor|parceria|cat.logo|tabela de pre|revenda|distribuidor|tem mais clientes|divulga|antecipa..o de recebi", re.I)

def _primeira_msg(conv_id):
    data, err = _cc("/conversation/messages/%s?from=0&to=25&orderBy=createdAt%%3AASC&filters=%%7B%%7D" % conv_id)
    if err or not data:
        return None
    items = [m for m in (data.get("conversationItems") or []) if m.get("senderType") == "contacts"]
    items.sort(key=lambda m: m.get("date") or "")
    for m in items:
        t = (m.get("content") or m.get("audioTextContent") or "").strip()
        if t:
            return t
    return None

def _indice_ce():
    opp_fones, comprou_fones = set(), set()
    opps = mf.api_all("crm/opportunities?sort_column=created_at&sort_direction=desc")
    for o in opps:
        ph = None
        cust = o.get("customer") or {}
        if isinstance(cust, dict):
            cp = cust.get("phone")
            ph = (cp.get("number") if isinstance(cp, dict) else cp)
        p = o.get("patient") or {}
        if not ph and p.get("uuid"):
            ph, _ = mf._get_patient(p["uuid"])
        f = _fone(ph)
        if not f:
            continue
        opp_fones.add(f)
        pipe = (o.get("pipeline") or {}).get("name") or ""
        stage = (o.get("stage") or {}).get("name") or ""
        if pipe.upper().startswith("CLIENTES") or stage.startswith("Ganho"):
            comprou_fones.add(f)
    return opp_fones, comprou_fones, len(opps)

def run_orfaos(max_msg=80):
    t0 = time.time()
    opp_fones, comprou_fones, n_opps = _indice_ce()
    convs = _lista("inbox") + _lista("waiting")
    seen, fila = set(), []
    for c in convs:
        cid = c.get("conversationId")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        fila.append(c)
    buckets = {"orfao_frio": 0, "orfao_crosssell": 0, "lead_andamento": 0,
               "cliente_operacional": 0, "ruido": 0, "revisar": 0, "sem_telefone": 0}
    cand, itens = [], []
    for c in fila:
        ct = c.get("contact") or {}
        f = _fone(ct.get("phone") or c.get("identifier"))
        if not f:
            buckets["sem_telefone"] += 1
            continue
        if f in opp_fones:
            buckets["cliente_operacional" if f in comprou_fones else "lead_andamento"] += 1
            continue
        cand.append((c, f, f in comprou_fones))
    for c, f, comprou in cand[:max_msg]:
        ct = c.get("contact") or {}
        msg = _primeira_msg(c.get("conversationId")) or ""
        low = msg.lower()
        if not low.strip():
            buckets["revisar"] += 1
            bucket = "revisar"
        elif _RUIDO.search(low):
            buckets["ruido"] += 1
            continue
        elif _FORN.search(low):
            buckets["revisar"] += 1
            bucket = "revisar"
        else:
            bucket = "orfao_crosssell" if comprou else "orfao_frio"
            buckets[bucket] += 1
        itens.append({
            "conversationId": c.get("conversationId"),
            "nome": ct.get("name"),
            "telefone": ct.get("phone") or c.get("identifier"),
            "tag": ", ".join([t.get("name", "") if isinstance(t, dict) else str(t) for t in (ct.get("tags") or [])]) or "-",
            "bucket": bucket,
            "primeira_msg": msg[:280],
            "comprou": comprou,
        })
    resumo = {
        "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "duracao_s": round(time.time() - t0, 1),
        "opps_crm": n_opps,
        "fila_viva": len(fila),
        "candidatos_sem_opp": len(cand),
        "buckets": buckets,
    }
    _grava(resumo, itens)
    return resumo

def _grava(resumo, itens):
    db = fw._get_db()
    if db is None:
        return
    try:
        coll = os.environ.get("ESCUTA_ORFAOS_COLL", "escuta_orfaos")
        payload = dict(resumo)
        payload["itens"] = itens[:200]
        db.collection(coll).document("_snapshot").set(payload)
        print("[orfaos] gravado: %s" % resumo["buckets"])
    except Exception as e:
        print("[orfaos] erro ao gravar:", e)

if __name__ == "__main__":
    print(run_orfaos())

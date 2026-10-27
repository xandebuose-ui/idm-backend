# -*- coding: utf-8 -*-
# IDM - Motor de Orfaos (Bloco B). Conversa (CliniChat) e a fonte de verdade.
# So-leitura. PII (nome/telefone) vai SO pro Firestore trancado, nunca pro repo publico.
import os, re, json, time, unicodedata, datetime as dt
import urllib.request, urllib.error, urllib.parse
import motor_fila as mf
import firebase_writer as fw

CLINI = "https://clinichat.clinicaexperts.com.br"

def _norm_nome(s):
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    out = []
    for c in s:
        if unicodedata.combining(c):
            continue
        out.append(c if (c.isalpha() or c == " ") else " ")
    return " ".join("".join(out).split())

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

_COMPRADORES = set()
try:
    with open("compradores.txt") as _fh:
        _COMPRADORES = set(x.strip() for x in _fh if x.strip())
except Exception:
    pass

def _eh_comprador(fone):
    if not fone or not _COMPRADORES:
        return False
    salt = os.environ.get("FONE_SALT")
    if not salt:
        return False
    import hmac, hashlib
    h = hmac.new(salt.encode(), fone.encode(), hashlib.sha256).hexdigest()
    return h in _COMPRADORES

def _lista(status=None, cap=800):
    filtro = {} if status is None else {"status": status}
    out, frm, step = [], 0, 50
    while frm < cap:
        f = urllib.parse.quote(json.dumps(filtro))
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

_MSG_CACHE = {}

def _primeira_msg(conv_id):
    if conv_id in _MSG_CACHE:
        return _MSG_CACHE[conv_id]
    data, err = _cc("/conversation/messages/%s?from=0&to=25&orderBy=createdAt%%3AASC&filters=%%7B%%7D" % conv_id)
    if err or not data:
        time.sleep(0.3)
        return None
    items = [m for m in (data.get("conversationItems") or []) if m.get("senderType") == "contacts"]
    items.sort(key=lambda m: m.get("date") or "")
    msg = ""
    for m in items:
        t = (m.get("content") or m.get("audioTextContent") or "").strip()
        if t:
            msg = t
            break
    _MSG_CACHE[conv_id] = msg
    time.sleep(0.4)
    return msg

def _classifica_origem(msg, ct):
    t = " " + (msg or "").lower() + " "
    def has(*ks):
        return any(k in t for k in ks)
    if has("vi o anuncio", "vi o an\u00fancio", "vim pelo anuncio", "vim pelo an\u00fancio",
           "do anuncio", "do an\u00fancio", "pelo anuncio", "pelo an\u00fancio",
           "anuncio de pos", "an\u00fancio de p\u00f3s",
           "quais sao os procedimentos de pos", "quais s\u00e3o os procedimentos de p\u00f3s"):
        return "trafego_pago"
    if has("da bio", "pagina bio", "p\u00e1gina bio", "link da bio", "vim da bio", "sua pagina bio", "sua p\u00e1gina bio"):
        return "organico_bio"
    if has("indica", "indicou", "indicaram", "fui indicad", "me indic"):
        return "relacionamento_indicacao"
    if has("pelo site", "no site", "pelo seu site", "do site"):
        return "organico_site"
    if has("no google", "pelo google", "pesquisei no google", "achei no google"):
        return "organico_google"
    if has("instagram", "direct", " insta "):
        return "organico_instagram"
    if ct.get("instagramIgId") or ct.get("instagramUsername"):
        return "organico_instagram"
    return "nao_informada"

_PAT_CACHE = {}

def _patient_fones(uuid):
    if uuid in _PAT_CACHE:
        return _PAT_CACHE[uuid]
    raw = mf.api("patients/" + uuid)
    pd = raw.get("data") or raw
    fones = set()
    if isinstance(pd, dict):
        ph = pd.get("phone")
        if isinstance(ph, dict):
            ph = ph.get("number") or ph.get("phone")
        f = _fone(ph)
        if f:
            fones.add(f)
        for c in (pd.get("contacts") or []):
            v = None
            if isinstance(c, dict):
                v = c.get("phone") or c.get("number") or c.get("value") or c.get("content")
                if isinstance(v, dict):
                    v = v.get("number")
            elif isinstance(c, str):
                v = c
            f = _fone(v)
            if f:
                fones.add(f)
    _PAT_CACHE[uuid] = fones
    time.sleep(0.22)
    return fones

def _fones_do_registro(p):
    fs = set()
    ph = p.get("phone")
    if isinstance(ph, dict):
        ph = ph.get("number") or ph.get("phone")
    f = _fone(ph)
    if f:
        fs.add(f)
    for c in (p.get("contacts") or []):
        v = None
        if isinstance(c, dict):
            v = c.get("phone") or c.get("number") or c.get("value") or c.get("content")
            if isinstance(v, dict):
                v = v.get("number")
        elif isinstance(c, str):
            v = c
        f = _fone(v)
        if f:
            fs.add(f)
    return fs

def _indice_ce():
    pat_map = {}
    com_fone = 0
    pats = mf.api_all("patients?per_page=100", cap_pages=80)
    for p in pats:
        u = p.get("uuid")
        if not u:
            continue
        fs = _fones_do_registro(p)
        pat_map[u] = fs
        if fs:
            com_fone += 1
    opp_fones, comprou_fones, opp_nomes = set(), set(), {}
    opps = mf.api_all("crm/opportunities?sort_column=created_at&sort_direction=desc")
    match, semfone = 0, 0
    for o in opps:
        u = (o.get("patient") or {}).get("uuid")
        fs = pat_map.get(u) or set()
        pipe = (o.get("pipeline") or {}).get("name") or ""
        stage = (o.get("stage") or {}).get("name") or ""
        comprou = pipe.upper().startswith("CLIENTES") or stage.startswith("Ganho")
        if fs:
            match += 1
            opp_fones |= fs
            if comprou:
                comprou_fones |= fs
        else:
            semfone += 1
            nm = _norm_nome((o.get("patient") or {}).get("name"))
            if len(nm) >= 5 and " " in nm:
                opp_nomes[nm] = opp_nomes.get(nm, False) or comprou
    diag = {"n_patients_lote": len(pats), "patients_com_fone": com_fone,
            "opps": len(opps), "opp_com_fone": match, "opp_sem_fone": semfone,
            "opp_nomes_n": len(opp_nomes)}
    return opp_fones, comprou_fones, opp_nomes, len(opps), diag

def run_orfaos(max_msg=130):
    t0 = time.time()
    opp_fones, comprou_fones, opp_nomes, n_opps, idx_diag = _indice_ce()
    desde = os.environ.get("ESCUTA_DESDE") or ("%04d-%02d-01" % (dt.date.today().year, dt.date.today().month))
    convs = _lista(None)
    seen, fila, total = set(), [], 0
    for c in convs:
        cid = c.get("conversationId")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        total += 1
        dcri = (c.get("createdAt") or (c.get("conversation") or {}).get("createdAt") or "")[:10]
        if dcri and dcri >= desde:
            fila.append(c)
    buckets = {"lead_sem_oport_frio": 0, "lead_sem_oport_crosssell": 0, "lead_andamento": 0,
               "cliente_operacional": 0, "ruido": 0, "revisar": 0, "sem_telefone": 0, "pendente_leitura": 0}
    origem_tot = {}
    novas_leituras = 0
    cand, itens = [], []
    for c in fila:
        ct = c.get("contact") or {}
        f = _fone(ct.get("phone") or c.get("identifier"))
        if not f:
            buckets["sem_telefone"] += 1
            continue
        tem_opp = f in opp_fones
        comprou = (f in comprou_fones) or _eh_comprador(f)
        if not tem_opp:
            nm = _norm_nome(ct.get("name"))
            if len(nm) >= 5 and " " in nm and nm in opp_nomes:
                tem_opp = True
                comprou = opp_nomes[nm]
        if tem_opp:
            buckets["cliente_operacional" if comprou else "lead_andamento"] += 1
            continue
        cand.append((c, f, comprou))
    for c, f, comprou in cand:
        cid = c.get("conversationId")
        ct = c.get("contact") or {}
        ja = cid in _MSG_CACHE
        if not ja and novas_leituras >= max_msg:
            buckets["pendente_leitura"] += 1
            continue
        if not ja:
            novas_leituras += 1
        msg = _primeira_msg(cid) or ""
        low = msg.lower()
        if not low.strip():
            buckets["pendente_leitura"] += 1
            continue
        if _RUIDO.search(low):
            buckets["ruido"] += 1
            continue
        if _FORN.search(low):
            buckets["revisar"] += 1
            bucket = "revisar"
        else:
            bucket = "lead_sem_oport_crosssell" if comprou else "lead_sem_oport_frio"
            buckets[bucket] += 1
        origem = _classifica_origem(msg, ct)
        origem_tot[origem] = origem_tot.get(origem, 0) + 1
        itens.append({
            "conversationId": cid,
            "nome": ct.get("name"),
            "telefone": ct.get("phone") or c.get("identifier"),
            "tag": ", ".join([t.get("name", "") if isinstance(t, dict) else str(t) for t in (ct.get("tags") or [])]) or "-",
            "bucket": bucket,
            "origem": origem,
            "primeira_msg": msg[:280],
            "comprou": comprou,
        })
    resumo = {
        "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
        "duracao_s": round(time.time() - t0, 1),
        "opps_crm": n_opps,
        "opp_fones_n": len(opp_fones),
        "comprou_fones_n": len(comprou_fones),
        "desde": desde,
        "fila_total": total,
        "compradores_zandu": len(_COMPRADORES),
        "fila_viva": len(fila),
        "fila_com_fone": len(fila) - buckets["sem_telefone"],
        "match_opp": buckets["cliente_operacional"] + buckets["lead_andamento"],
        "candidatos_sem_opp": len(cand),
        "novas_leituras": novas_leituras,
        "cache_msgs": len(_MSG_CACHE),
        "origem": origem_tot,
        "buckets": buckets,
        "diag_idx": idx_diag,
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

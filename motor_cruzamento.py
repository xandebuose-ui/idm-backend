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

_OPP_POR_FONE = {}
_FONE2UUID = {}     # telefone normalizado -> set de uuids de paciente
_UUID_OPPS = {}     # uuid -> lista de oportunidades
_UUID_COMPROU = set()  # uuids que ja compraram (Ganho/CLIENTES)
_OPP_NOMES = {}     # nome normalizado -> comprou (fallback quando nao achar por telefone)
_COMPROU_FONE = set()  # telefones que ja compraram (Ganho/CLIENTES), via API interna

INTERNAL = "https://api.clinicaexperts.com.br"

def _app_jwt():
    # app_jwt (Sanctum) para a API INTERNA de CRM. Preferencia: env CE_APP_JWT; senao o guardado no Firestore.
    t = os.environ.get("CE_APP_JWT")
    if t:
        return t.strip()
    try:
        return fw.get_app_jwt()
    except Exception:
        return None

def _api_interna_all(path, cap_pages=30):
    tok = _app_jwt()
    if not tok:
        return None, "sem_app_jwt"
    out, pg = [], 1
    sep = "&" if "?" in path else "?"
    while pg <= cap_pages:
        url = INTERNAL + "/" + path.lstrip("/") + "%spage=%d" % (sep, pg)
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + tok, "User-Agent": mf.UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            return (out or None), "http_%s" % e.code
        except Exception as e:
            return (out or None), str(e)[:60]
        data = d.get("data") or []
        out += data
        m = d.get("meta") or {}
        total = m.get("total") or m.get("of") or 0
        if not data or (total and len(out) >= total):
            break
        pg += 1
    return out, None

def _indice_ce():
    # Oportunidades pela API INTERNA do CRM: traz customer.phone de TODO contato (lead incluso).
    # A API oficial so devolve patient{uuid,name} SEM telefone, e lead nao vive em /patients -> por isso falhava.
    _OPP_POR_FONE.clear(); _FONE2UUID.clear(); _UUID_OPPS.clear(); _UUID_COMPROU.clear(); _OPP_NOMES.clear()
    _COMPROU_FONE.clear()
    opps, err = _api_interna_all("api/crm/opportunity?per_page=100")
    opps = opps or []
    match = 0
    for o in opps:
        cust = o.get("customer") or {}
        ph = cust.get("phone")
        if isinstance(ph, dict):
            ph = ph.get("number") or ph.get("phone")
        f = _fone(ph)
        pipe = o.get("pipeline_name") or (o.get("pipeline") or {}).get("name") or ""
        st = o.get("stage")
        stage = (st.get("name") if isinstance(st, dict) else st) or ""
        comprou = pipe.upper().startswith("CLIENTES") or stage.lower().startswith("ganho")
        od = {"amount": o.get("amount"), "stage": stage, "status": o.get("status"), "pipeline": pipe}
        if f:
            _OPP_POR_FONE.setdefault(f, []).append(od)
            if comprou:
                _COMPROU_FONE.add(f)
            match += 1
        else:
            nm = _norm_nome(cust.get("name"))
            if len(nm) >= 5 and " " in nm:
                _OPP_NOMES[nm] = _OPP_NOMES.get(nm, False) or comprou
    # Resiliencia: se a API interna falhou/veio vazia (app_jwt expirou), usa o ULTIMO mapa salvo;
    # se veio cheia, PERSISTE o mapa (sobrevive a restart e a expiracao do token; o webhook o mantem vivo).
    usou_mapa = False
    if not _OPP_POR_FONE:
        try:
            mp = fw.get_crm_map()
            if mp:
                for _f, _lst in (mp.get("fones") or {}).items():
                    _OPP_POR_FONE[_f] = _lst
                for _f in (mp.get("comprou") or []):
                    _COMPROU_FONE.add(_f)
                usou_mapa = True
        except Exception as _em:
            print("[indice] erro ao carregar mapa salvo:", _em)
    else:
        try:
            fw.save_crm_map({"fones": _OPP_POR_FONE, "comprou": list(_COMPROU_FONE)})
        except Exception:
            pass
    diag = {"fonte": ("mapa_salvo" if usou_mapa else "api_interna"), "opps": len(opps), "opp_com_fone": match,
            "fones_distintos": len(_OPP_POR_FONE), "compraram_fones": len(_COMPROU_FONE),
            "opp_nomes_n": len(_OPP_NOMES), "erro_interna": err, "usou_mapa_salvo": usou_mapa}
    return set(_OPP_POR_FONE.keys()), set(_COMPROU_FONE), _OPP_NOMES, len(opps), diag


def _opps_do_fone(fone_raw):
    # telefone -> oportunidades (index da API interna: tem customer.phone de lead e cliente).
    f = _fone(fone_raw)
    return list(_OPP_POR_FONE.get(f, [])), f


_IDX_CACHE = {"t": 0.0, "data": None}

def indice_ce_cached(ttl=1800):
    # Reusa o indice ja construido pelo cruzamento (compartilhado via _IDX_CACHE); so reconstroi se velho.
    now = time.time()
    if _IDX_CACHE["data"] and (now - _IDX_CACHE["t"]) < ttl:
        return _IDX_CACHE["data"]
    opp_fones, comprou_fones, opp_nomes, _n, _d = _indice_ce()
    _IDX_CACHE["data"] = (opp_fones, comprou_fones, opp_nomes)
    _IDX_CACHE["t"] = now
    return _IDX_CACHE["data"]

def classifica_identidade(fone_raw, nome=None, idx=None):
    # Sinal de APOIO: cliente | lead_cadastrado | novo. Cruza por telefone -> oportunidade (API interna).
    f = _fone(fone_raw)
    if not f:
        return "novo"
    comprou = (f in _COMPROU_FONE) or _eh_comprador(f)
    tem_opp = bool(_OPP_POR_FONE.get(f))
    if not tem_opp:
        nm = _norm_nome(nome)
        if len(nm) >= 5 and " " in nm and nm in _OPP_NOMES:
            tem_opp = True
            comprou = comprou or _OPP_NOMES[nm]
    if comprou:
        return "cliente"
    if tem_opp:   # tem oportunidade/cadastro no CE = lead cadastrado
        return "lead_cadastrado"
    return "novo"



def situacao_lead(fone_raw):
    # Olha SO o funil LEADS: 'ativo' = tem opp LEADS viva (nao Perdido/Desqualificado/Ganho);
    # 'perdido' = tem opp mas nenhuma LEADS viva (Perdido/Desqualif. ou so Reativacao/Cliente -> Reativacao);
    # 'sem_opp' = nenhuma oportunidade (furo do comercial).
    opps, _f = _opps_do_fone(fone_raw)
    if not opps:
        return "sem_opp"
    for o in opps:
        pipe = (o.get("pipeline") or "").upper()
        s = (o.get("stage") or "").lower()
        if pipe.startswith("LEADS") and not (s.startswith("perdido") or s.startswith("desqualific") or s.startswith("ganho")):
            return "ativo"
    return "perdido"


def contexto_ce(fone_raw, nome=None):
    _raw, f = _opps_do_fone(fone_raw)
    _seen, _uniq = set(), []
    for _o in _raw:
        _k = (_o.get("amount"), _o.get("stage"), _o.get("status"))
        if _k in _seen:
            continue
        _seen.add(_k); _uniq.append(_o)
    out = {"oportunidades": _uniq[:5], "tags": "", "annotation": ""}
    if f:
        try:
            d = mf.api("patients?phone=" + f)
            arr = (d.get("data") or []) if isinstance(d, dict) else []
            if arr:
                p0 = arr[0]
                tg = p0.get("tags") or []
                out["tags"] = ", ".join((t.get("name", "") if isinstance(t, dict) else str(t)) for t in tg)
                out["annotation"] = (p0.get("annotation") or "")[:240]
        except Exception as e:
            out["erro"] = str(e)[:80]
    return out


PROG = {"etapa": "boot", "em": 0}

def _prog(msg):
    PROG["etapa"] = msg; PROG["em"] = int(time.time())
    print("[cruzamento] " + msg, flush=True)

def verificar_gate():
    # Gate de SAUDE (sem PII): o cruzamento so vale se a API interna respondeu com volume plausivel.
    # Se o app_jwt falhar/expirar, _OPP_POR_FONE vem vazio -> gate falha -> NAO publica (mantem dado antigo).
    fones = len(_OPP_POR_FONE)
    opps_total = sum(len(v) for v in _OPP_POR_FONE.values())
    min_fones = int(os.environ.get("GATE_MIN_FONES", "120"))
    passou = (fones >= min_fones) and (len(_COMPROU_FONE) > 0) and (opps_total >= min_fones)
    return {"passou": passou, "fones_com_opp": fones, "opps_total": opps_total,
            "compraram": len(_COMPROU_FONE), "min_exigido": min_fones}


def rodar_cruzamento(max_msg=130):
    t0 = time.time()
    _prog("START")
    opp_fones, comprou_fones, opp_nomes, n_opps, idx_diag = _indice_ce()
    _prog('indice ok opps=%d' % n_opps)
    _GATE = verificar_gate()
    if not _GATE["passou"]:
        _prog('GATE FALHOU fones=%d min=%d — NAO publica' % (_GATE["fones_com_opp"], _GATE["min_exigido"]))
        return {"gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
                "gate_falhou": True, "gate": _GATE, "diag_idx": idx_diag, "opps_crm": n_opps}
    _IDX_CACHE["data"] = (opp_fones, comprou_fones, opp_nomes); _IDX_CACHE["t"] = time.time()
    desde = os.environ.get("ESCUTA_DESDE") or (dt.date.today() - dt.timedelta(days=int(os.environ.get("ESCUTA_DIAS", "90")))).isoformat()
    convs = _lista(None)
    _prog('lista %d convs' % len(convs))
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
    cand, itens, universo = [], [], []
    _prog('fila=%d match' % len(fila))
    for c in fila:
        ct = c.get("contact") or {}
        f = _fone(ct.get("phone") or c.get("identifier"))
        if not f:
            buckets["sem_telefone"] += 1
            continue
        tem_opp = bool(_OPP_POR_FONE.get(f))
        comprou = (f in _COMPROU_FONE) or _eh_comprador(f)
        if not tem_opp:
            nm = _norm_nome(ct.get("name"))
            if len(nm) >= 5 and " " in nm and nm in _OPP_NOMES:
                tem_opp = True
                comprou = comprou or _OPP_NOMES[nm]
        if tem_opp:
            buckets["cliente_operacional" if comprou else "lead_andamento"] += 1
            universo.append({"conv": c.get("conversationId"), "nome": ct.get("name"),
                             "telefone": ct.get("phone") or c.get("identifier"),
                             "lt": c.get("lastMessageTime") or (c.get("conversation") or {}).get("updatedAt") or c.get("updatedAt") or "",
                             "criado": (c.get("createdAt") or (c.get("conversation") or {}).get("createdAt") or "")[:10],
                             "identidade": "cliente" if comprou else "lead_cadastrado",
                             "situacao": situacao_lead(f)})
            continue
        cand.append((c, f, comprou))
    _prog('match ok cand=%d lendo 1a-msg' % len(cand))
    for c, f, comprou in cand:
        cid = c.get("conversationId")
        ct = c.get("contact") or {}
        if novas_leituras and novas_leituras % 25 == 0:
            _prog('lidas %d 1a-msgs' % novas_leituras)
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
            universo.append({"conv": cid, "nome": ct.get("name"),
                             "telefone": ct.get("phone") or c.get("identifier"),
                             "lt": c.get("lastMessageTime") or (c.get("conversation") or {}).get("updatedAt") or c.get("updatedAt") or "",
                             "criado": (c.get("createdAt") or (c.get("conversation") or {}).get("createdAt") or "")[:10],
                             "identidade": classifica_identidade(f, ct.get("name")),
                             "situacao": "sem_opp"})
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
        "universo": universo,
    }
    _prog('fim universo=%d' % len(universo))
    try:
        _dbu = fw._get_db()
        if _dbu:
            _dbu.collection(os.environ.get("ESCUTA_ORFAOS_COLL", "escuta_orfaos")).document("universo").set(
                {"universo": universo[:600], "origem": origem_tot, "gerado_em": resumo["gerado_em"]})
    except Exception as _eu:
        print("[cruzamento] universo firestore erro:", _eu, flush=True)
    _grava(resumo, itens)
    return resumo

def _grava(resumo, itens):
    db = fw._get_db()
    if db is None:
        return
    try:
        # colecao interna do cruzamento (nome legado "escuta_orfaos" mantido p/ nao migrar dado vivo)
        coll = os.environ.get("ESCUTA_ORFAOS_COLL", "escuta_orfaos")
        payload = dict(resumo)
        payload["itens"] = itens[:200]
        payload.pop("universo", None)
        db.collection(coll).document("_snapshot").set(payload)
        print("[cruzamento] gravado: %s" % resumo["buckets"])
    except Exception as e:
        print("[cruzamento] erro ao gravar:", e)

if __name__ == "__main__":
    print(rodar_cruzamento())

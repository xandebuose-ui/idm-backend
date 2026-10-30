# -*- coding: utf-8 -*-
"""
IDM - COLETOR UNICO (passo 2 da SPEC-Coletor-Unico, aprovado 29/10/2026). ADITIVO.

"um leitor -> um banco -> todas as frentes": le cada fonte 1x e grava o BRUTO no Firestore
ANTES de qualquer analise. Se o modelo falhar depois, o dado ja esta no banco.

  escuta_conversas/{fone}                       ficha da conversa (nome, 1a chegada, ultima msg, de quem, total)
  escuta_conversas/{fone}/msgs_{AAAA-MM}/{id}   cada mensagem (so texto; midia = so o tipo), idempotente por id
  escuta_crm/{uuid}                             oportunidades (API interna do CE) - so as que mudaram
  escuta_vendas/{id}                            orcamentos/vendas (sales-quotes) + itens oficiais
  escuta_meta/{AAAA-MM-DD}                      gasto/contatos por campanha (Graph API), 1 doc por dia
  escuta_coletor/estado                         watermarks + estatisticas (base do /saude)

NADA do que ja roda muda: termometro/Jarbas/painel seguem lendo do CliniChat (FONTE=clinichat).
A troca para FONTE=firestore e o passo 4, depois de provar que o banco bate.
PII (nome/telefone/texto) so vai para colecoes fechadas do Firestore (Admin SDK); nunca para log.
"""
import os, json, time, hashlib, threading, urllib.request, urllib.error, urllib.parse
import datetime as dt

import motor_cruzamento as _cruz
import firebase_writer as _fw

# ----------------------------------------------------------------------------- config
COLL_CONV = os.environ.get("COLETOR_COLL_CONV", "escuta_conversas")
COLL_CRM = os.environ.get("ESCUTA_CRM_COLL", "escuta_crm")          # ja existe (doc 'mapa'); docs novos = uuid
COLL_VENDAS = os.environ.get("COLETOR_COLL_VENDAS", "escuta_vendas")
COLL_META = os.environ.get("COLETOR_COLL_META", "escuta_meta")
COLL_EST = os.environ.get("COLETOR_COLL_EST", "escuta_coletor")
RETENCAO_DIAS = int(os.environ.get("COLETOR_RETENCAO_DIAS", "365"))   # 12 meses (decisao do Xandao)
PAGE_MSGS = 100
CONV_MIN = int(os.environ.get("COLETOR_CONV_MIN", "10"))              # ciclo de conversas
CRM_MIN = int(os.environ.get("COLETOR_CRM_MIN", "30"))                # fallback do CRM (o refresh do cruzamento ja grava)
JANELA_INICIAL_H = int(os.environ.get("COLETOR_JANELA_INICIAL_H", "24"))  # 1a rodada: so as ultimas 24h (resto = backfill manual)
LISTA_CAP = int(os.environ.get("COLETOR_LISTA_CAP", "5000"))
VENDAS_DETALHES_MAX = int(os.environ.get("COLETOR_VENDAS_DETALHES", "40"))
SECRET = os.environ.get("WEBHOOK_SECRET", "")

_STATUS_TELA = {"won": "Ganho", "open": "Aberto", "due": "Vencido", "lost": "Perdido"}

# ----------------------------------------------------------------------------- tempo
def _utcnow():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def _agora_sp():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _iso(d=None):
    d = d or _utcnow()
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)

DATAS_MSG = {"min": None, "max": None, "n": 0}   # intervalo real das datas de mensagem lidas
CAMPOS_CONV = {}
CRIACAO = {"conv_min": None, "conv_max": None, "canal": {}}   # datas de criacao (nao e PII)
LEITURA_TOTAL = {"conversas": 0, "criada_7d_antes_da_1a_msg": 0, "msgs": 0, "total_api_soma": 0}  # detecta corte de historico   # campos de data/hora que o contact-channel traz (formato mascarado)
FORMATOS = {}   # amostra do FORMATO das datas recebidas (digitos mascarados, sem PII) - diagnostico no /saude

def _amostra(campo, v):
    try:
        import re as _re
        k = "%s:%s:%s" % (campo, type(v).__name__, _re.sub(r"[0-9]", "9", str(v))[:40])
        FORMATOS[k] = FORMATOS.get(k, 0) + 1
        if len(FORMATOS) > 40:
            FORMATOS.pop(next(iter(FORMATOS)))
    except Exception:
        pass

def _campos_conv(c):
    try:
        import re as _re
        if len(CAMPOS_CONV) > 60:
            return
        def walk(o, pre, dep):
            for k, v in (o or {}).items():
                kl = k.lower()
                if isinstance(v, dict) and dep < 2:
                    walk(v, pre + k + ".", dep + 1)
                elif any(x in kl for x in ("time", "date", "at")) and not isinstance(v, (dict, list)):
                    CAMPOS_CONV[pre + k] = _re.sub(r"[0-9]", "9", str(v))[:30]
        walk(c, "", 0)
    except Exception:
        pass

def _parse(v):
    """ISO (com Z/offset/ms) ou epoch (s/ms) -> datetime UTC aware. None se nao der."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            x = float(v)
            if x > 1e12:
                x /= 1000.0
            return dt.datetime.fromtimestamp(x, tz=dt.timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        if "." in s:  # normaliza fracao para 6 digitos (py<3.11 so aceita 3 ou 6)
            head, rest = s.split(".", 1)
            frac, tz = "", ""
            for i, ch in enumerate(rest):
                if not ch.isdigit():
                    frac, tz = rest[:i], rest[i:]
                    break
            else:
                frac = rest
            s = head + "." + (frac + "000000")[:6] + tz
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None

# ----------------------------------------------------------------------------- estado / stats
_FONTES = ("conversas", "crm", "vendas", "meta")
ST = {f: {"ultima_tentativa": None, "ultimo_ok": None, "ok": None, "erro": None, "parcial": False,
          "lidos_ciclo": 0, "gravados_ciclo": 0, "gravados_desde_boot": 0, "ciclos": 0, "motivo": None}
      for f in _FONTES}
WM = {}                       # watermarks persistidos (conversas_lt)
BACKFILL = {"rodando": False}
ID_CAMPO = {}                 # qual campo serviu de id da mensagem (prova da idempotencia no 1o ciclo)
PID2FONE = {}                 # patient_id/externalId -> fone normalizado (liga venda ao telefone)
_LOCKS = {k: threading.Lock() for k in ("conversas", "crm", "vendas", "meta", "backfill", "estado")}
_EXTRA = {}                   # provedores extras de saude (ex.: stats do webhook TimTim, vindos do main)
_CRM_IDX = None
_VEND_IDX = None
_carregado = False

def _db():
    return _fw._get_db()

def _ini(f, motivo):
    ST[f]["ultima_tentativa"] = _iso()
    ST[f]["motivo"] = motivo

def _fim(f, ok, lidos=0, gravados=0, erro=None, parcial=False):
    s = ST[f]
    s["ok"] = bool(ok)
    s["erro"] = (str(erro)[:200] if erro else None)
    s["parcial"] = bool(parcial)
    s["lidos_ciclo"] = lidos
    s["gravados_ciclo"] = gravados
    s["gravados_desde_boot"] += gravados
    s["ciclos"] += 1
    if ok:
        s["ultimo_ok"] = _iso()
    _salvar_estado()
    print("[coletor] %s ok=%s lidos=%d gravados=%d%s" % (f, ok, lidos, gravados,
          (" erro=%s" % s["erro"]) if s["erro"] else ""), flush=True)

def _carregar_estado():
    global _carregado
    if _carregado:
        return
    db = _db()
    if not db:
        return
    try:
        d = db.collection(COLL_EST).document("estado").get()
        if d.exists:
            v = d.to_dict() or {}
            WM.update(v.get("wm") or {})
            for f, s in (v.get("stats") or {}).items():
                if f in ST:
                    for k in ("ultimo_ok", "ultima_tentativa"):
                        if s.get(k) and not ST[f].get(k):
                            ST[f][k] = s.get(k)
        _carregado = True
    except Exception as e:
        print("[coletor] estado load erro:", e, flush=True)

def _salvar_estado():
    db = _db()
    if not db:
        return
    with _LOCKS["estado"]:
        try:
            db.collection(COLL_EST).document("estado").set(
                {"wm": WM, "stats": ST, "id_campo": ID_CAMPO, "atualizado_em": _iso()}, merge=True)
        except Exception as e:
            print("[coletor] estado save erro:", e, flush=True)

def _h(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]

class _Batch:
    """Escrita em lote (limite do Firestore = 500 ops por commit)."""
    def __init__(self, db):
        self.db, self.b, self.n, self.total = db, db.batch(), 0, 0
    def set(self, ref, data, merge=False):
        self.b.set(ref, data, merge=merge)
        self.n += 1
        if self.n >= 400:
            self.flush()
    def flush(self):
        if self.n:
            self.b.commit()
            self.total += self.n
            self.b, self.n = self.db.batch(), 0
        return self.total

# ============================================================================= CONVERSAS
def _lt(cc):
    # 29/10: lastMessageTime vem None; conversation.updatedAt muda por evento de ticket (falso "ativo").
    # Fonte real da atividade = data da ULTIMA MENSAGEM.
    cv = cc.get("conversation") or {}
    lm = cv.get("lastMessage") if isinstance(cv.get("lastMessage"), dict) else {}
    return (lm.get("createdAt") or cv.get("lastActivityTime") or cc.get("lastMessageTime")
            or cv.get("updatedAt") or cc.get("updatedAt") or "")

def _criado(cc):
    return cc.get("createdAt") or (cc.get("conversation") or {}).get("createdAt") or ""

def _lista(desde_dt=None, cap=LISTA_CAP):
    """contact-channel (historico inteiro, filters={}) ordenado por lastMessageTime DESC.
    Com desde_dt: so conversas com atividade depois dele; para de paginar quando a pagina
    inteira ja esta abaixo do corte (nao confia na ordem DENTRO da pagina)."""
    out, frm, step = [], 0, 50
    filtro = urllib.parse.quote(json.dumps({}))
    while frm < cap:
        data, err = _cruz._cc("/contact-channel?from=%d&to=%d&orderBy=lastMessageTime%%3ADESC&filters=%s"
                              % (frm, frm + step, filtro))
        if err or not data:
            return out, (err or "sem_dados")
        ccs = data.get("contactChannels") or []
        total = ((data.get("pagination") or {}).get("total")) or 0
        abaixo = 0
        for c in ccs:
            _amostra("lastMessageTime", c.get("lastMessageTime"))
            _campos_conv(c)
            try:
                cr = _iso(_parse(_criado(c))) if _parse(_criado(c)) else None
                if cr:
                    CRIACAO["conv_min"] = min(CRIACAO["conv_min"] or cr, cr)
                    CRIACAO["conv_max"] = max(CRIACAO["conv_max"] or cr, cr)
                ch = (c.get("channel") or {}).get("createdAt") if isinstance(c.get("channel"), dict) else None
                if ch:
                    CRIACAO["canal"][str(ch)[:10]] = CRIACAO["canal"].get(str(ch)[:10], 0) + 1
            except Exception:
                pass
            d = _parse(_lt(c))
            if desde_dt and d is None:
                abaixo += 1          # sem data legivel: nao entra no incremental (fica para backfill)
                continue
            if desde_dt and d <= desde_dt:
                abaixo += 1
                continue
            out.append(c)
        frm += step
        # NAO para pela ordem: lastMessageTime vem None (29/10) e a ordenacao nao e confiavel -> lista tudo
        if not ccs or frm >= total:
            break
        time.sleep(0.2)
    return out, None

def _pagina_msgs(cid, frm, to, ordem):
    data, err = _cruz._cc("/conversation/messages/%s?from=%d&to=%d&orderBy=createdAt%%3A%s&filters=%%7B%%7D"
                          % (cid, frm, to, ordem))
    if err or data is None:
        return None, None, (err or "sem_dados")
    items = data.get("conversationItems") or []
    total = (data.get("pagination") or {}).get("total")
    try:
        total = int(total) if total is not None else None
    except Exception:
        total = None
    return items, total, None

def _msg_id(m):
    for k in ("id", "_id", "messageId", "uuid"):
        v = m.get(k)
        if v not in (None, ""):
            return str(v), k
    base = "%s|%s|%s|%s|%s" % (m.get("date"), m.get("senderType"), m.get("type"),
                                (m.get("content") or "")[:500], (m.get("audioTextContent") or "")[:200])
    return "h_" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:24], "hash"

_MIDIA = ("audio", "ptt", "voice", "image", "photo", "video", "document", "file", "sticker")

def _msg_doc(m, cid, fk):
    mid, campo = _msg_id(m)
    ID_CAMPO[campo] = ID_CAMPO.get(campo, 0) + 1
    d = m.get("date") or m.get("createdAt") or ""
    _amostra("msg_date", d)
    dd = _parse(d)
    if dd:
        x = _iso(dd)
        DATAS_MSG["n"] += 1
        if DATAS_MSG["min"] is None or x < DATAS_MSG["min"]:
            DATAS_MSG["min"] = x
        if DATAS_MSG["max"] is None or x > DATAS_MSG["max"]:
            DATAS_MSG["max"] = x
    tipo = (m.get("type") or "text").lower()
    midia = tipo in _MIDIA or tipo not in ("text", "chat", "")
    txt = (m.get("content") or "")
    if midia and txt.strip().lower().startswith("http"):
        txt = ""  # midia: guarda so o tipo, nunca o arquivo/link
    st = m.get("senderType") or ""
    snd = m.get("sender") if isinstance(m.get("sender"), dict) else {}
    doc = {
        "id": mid, "conv": cid, "fone": fk, "data": _iso(dd) if dd else None, "data_raw": str(d),
        "de": "lead" if st == "contacts" else ("equipe" if st == "users" else (st or "sistema")),
        "sender_type": st, "remetente": snd.get("name"), "remetente_ext": snd.get("externalId"),
        "tipo": tipo, "midia": bool(midia), "texto": txt[:20000],
        "transcricao": (m.get("audioTextContent") or "")[:20000],
        "atividade": m.get("activityType"), "lida_em": m.get("readAt"),
        "gravado_em": _iso(), "expira_em": (dd or _utcnow()) + dt.timedelta(days=RETENCAO_DIAS),
    }
    mes = (dd - dt.timedelta(hours=3)).strftime("%Y-%m") if dd else "sem_data"   # nunca inventa data
    return mid, mes, doc, dd

def _ler_tudo(cid):
    """Le a conversa inteira (ASC, paginado). completo = recebeu >= total que a API declara."""
    itens, frm, total = [], 0, None
    while True:
        items, total_p, err = _pagina_msgs(cid, frm, frm + PAGE_MSGS, "ASC")
        if err:
            return itens, total, err, False
        if total_p is not None:
            total = total_p
        itens += items
        frm += PAGE_MSGS
        if not items or (total is not None and frm >= total) or frm > 30000:
            break
        time.sleep(0.25)
    completo = (total is None and bool(itens)) or (total is not None and len(itens) >= total)
    return itens, total, None, completo

def _ler_novas(cid, base_dt, total_ant):
    """Incremental: paginas DESC ate cair abaixo de base_dt. Auto-checagem contra o total da API:
    se achou menos novas do que a API diz que existem, devolve precisa_tudo=True."""
    novos, frm, total = [], 0, None
    while True:
        items, total_p, err = _pagina_msgs(cid, frm, frm + PAGE_MSGS, "DESC")
        if err:
            return novos, total, err, True
        if total_p is not None:
            total = total_p
        if not items:
            break
        datas = []
        for m in items:
            d = _parse(m.get("date") or m.get("createdAt"))
            datas.append(d)
            if d is None or d >= base_dt:
                novos.append(m)
        frm += PAGE_MSGS
        validas = [d for d in datas if d is not None]
        if (validas and min(validas) < base_dt) or (total is not None and frm >= total) or frm > 30000:
            break
        time.sleep(0.25)
    estritas = sum(1 for m in novos if (_parse(m.get("date") or m.get("createdAt")) or base_dt) > base_dt)
    esperado = (total or 0) - int(total_ant or 0)
    return novos, total, None, (esperado > 0 and estritas < esperado)

def _coletar_conv(db, cc, forcar_tudo=False):
    """Grava 1 conversa. Retorna (msgs_gravadas, erro, pulou)."""
    cid = str(cc.get("conversationId") or "")
    if not cid:
        return 0, None, True
    ct = cc.get("contact") or {}
    fraw = ct.get("phone") or cc.get("identifier") or ""
    fk = _cruz._fone(fraw) or ("sem_fone_" + cid)
    ext = ct.get("externalId")
    if ext and not fk.startswith("sem_fone"):
        PID2FONE[str(ext)] = fk
    ref = db.collection(COLL_CONV).document(fk)
    snap = ref.get()
    cur = (snap.to_dict() or {}) if snap.exists else {}
    est = ((cur.get("convs") or {}).get(cid)) or {}
    lt = _lt(cc)
    lt_dt, lt_est = _parse(lt), _parse(est.get("lt"))
    if not forcar_tudo and est.get("completo") and lt_est and lt_dt and lt_dt <= lt_est:
        return 0, None, True                                     # nada novo nesta conversa

    base_dt = _parse(est.get("ultima_data")) if (est.get("completo") and not forcar_tudo) else None
    if base_dt:
        itens, total, err, precisa_tudo = _ler_novas(cid, base_dt, est.get("total_api"))
        completo = not err
        if precisa_tudo and not err:
            itens, total, err, completo = _ler_tudo(cid)
    else:
        itens, total, err, completo = _ler_tudo(cid)
        try:
            LEITURA_TOTAL["conversas"] += 1
            LEITURA_TOTAL["msgs"] += len(itens)
            LEITURA_TOTAL["total_api_soma"] += int(total or 0)
            ds = [x for x in (_parse(m.get("date") or m.get("createdAt")) for m in itens) if x]
            cri = _parse(_criado(cc))
            if ds and cri and (min(ds) - cri) > dt.timedelta(days=7):
                LEITURA_TOTAL["criada_7d_antes_da_1a_msg"] += 1
        except Exception:
            pass

    # GRAVA o que foi lido (mesmo parcial: tudo que foi lido fica registrado). 'completo' diz a verdade.
    b = _Batch(db)
    ult_dt, ult_de, prim_dt = None, None, None
    for m in itens:
        mid, mes, doc, dd = _msg_doc(m, cid, fk)
        b.set(ref.collection("msgs_" + mes).document(mid), doc)
        if dd is None:
            continue
        if doc["atividade"] in (None, "", "message") or doc["texto"] or doc["midia"]:
            if ult_dt is None or dd > ult_dt:
                ult_dt, ult_de = dd, doc["de"]
        if prim_dt is None or dd < prim_dt:
            prim_dt = dd
    gravadas = b.flush()

    ultima_ant = _parse(est.get("ultima_data"))
    ultima = max([x for x in (ult_dt, ultima_ant) if x is not None], default=None)
    entrada = {"lt": lt, "total_api": total, "completo": bool(completo and not err),
               "ultima_data": _iso(ultima) if ultima else None, "coletado_em": _iso(),
               "criado": _criado(cc)}
    if err:
        entrada["erro"] = str(err)[:80]
    convs = dict(cur.get("convs") or {})
    convs[cid] = {**est, **entrada}
    total_msgs = sum(int((v or {}).get("total_api") or 0) for v in convs.values())
    ficha = {"fone": fk, "fone_raw": fraw, "nome": ct.get("name"), "external_id": ext,
             "tipo_contato": ct.get("type"), "canal": (cc.get("channel") or {}).get("type") if isinstance(cc.get("channel"), dict) else cc.get("channel"),
             "convs": {cid: convs[cid]}, "total_msgs": total_msgs, "atualizado_em": _iso()}
    candidatos = [x for x in (_parse(cur.get("primeira_chegada")), _parse(_criado(cc)), prim_dt) if x is not None]
    if candidatos:
        ficha["primeira_chegada"] = _iso(min(candidatos))
    ult_ficha = _parse(cur.get("ultima_msg_em"))
    if ult_dt and (ult_ficha is None or ult_dt >= ult_ficha):
        ficha["ultima_msg_em"] = _iso(ult_dt)
        ficha["ultima_msg_de"] = ult_de
    ref.set(ficha, merge=True)
    return gravadas, err, False

def ciclo_conversas(motivo="poll"):
    if not _LOCKS["conversas"].acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        _carregar_estado()
        _ini("conversas", motivo)
        db = _db()
        if not db:
            _fim("conversas", False, erro="firestore indisponivel"); return {"ok": False}
        if not _fw.get_token():
            _fim("conversas", False, erro="sem cracha do CliniChat"); return {"ok": False}
        wm = _parse(WM.get("conversas_lt")) or (_utcnow() - dt.timedelta(hours=JANELA_INICIAL_H))
        desde = wm - dt.timedelta(minutes=10)                     # margem; idempotente por id
        ccs, err_lista = _lista(desde)
        gravadas, falhas, lidas, maior = 0, [], 0, wm
        novidades = []
        for cc in ccs:
            try:
                n, err, pulou = _coletar_conv(db, cc)
            except Exception as e:
                n, err, pulou = 0, repr(e)[:80], False
            gravadas += n
            if not pulou:
                lidas += 1
                novidades.append(cc)
            d = _parse(_lt(cc))
            if err:
                falhas.append(d or wm)
            elif d and d > maior:
                maior = d
            time.sleep(0.15)
        # watermark so avanca ate antes da 1a falha (a conversa que falhou volta no proximo ciclo)
        if not err_lista:
            novo = maior
            if falhas:
                novo = min(novo, min(falhas) - dt.timedelta(seconds=1))
            if novo > wm:
                WM["conversas_lt"] = _iso(novo)
        ok = not err_lista and not falhas
        for _h in list(NOVIDADE_HOOKS):      # 30/10 radar: avisa o termometro na hora (lead novo / msg nova)
            try:
                _h(novidades)
            except Exception as _eh:
                print("[coletor] hook novidade erro:", repr(_eh)[:120], flush=True)
        _fim("conversas", ok, lidos=lidas, gravados=gravadas,
             erro=(err_lista or ("%d conversa(s) com erro" % len(falhas) if falhas else None)),
             parcial=bool(falhas))
        return {"ok": ok, "conversas_com_novidade": lidas, "msgs_gravadas": gravadas, "falhas": len(falhas)}
    except Exception as e:
        _fim("conversas", False, erro=repr(e)); return {"ok": False, "erro": repr(e)[:160]}
    finally:
        _LOCKS["conversas"].release()

def _backfill_run(dias):
    BACKFILL.update({"rodando": True, "dias": dias, "inicio": _iso(), "fim": None, "conversas": 0,
                     "feitas": 0, "puladas": 0, "msgs": 0, "erros": 0, "erro_lista": None})
    try:
        db = _db()
        if not db or not _fw.get_token():
            BACKFILL["erro_lista"] = "sem firestore ou sem cracha"; return
        desde = _utcnow() - dt.timedelta(days=dias)
        ccs, err = _lista(desde)
        BACKFILL["conversas"] = len(ccs)
        BACKFILL["erro_lista"] = err
        for cc in ccs:
            try:
                n, e, pulou = _coletar_conv(db, cc)
            except Exception as ex:
                n, e, pulou = 0, repr(ex)[:80], False
            BACKFILL["msgs"] += n
            if e:
                BACKFILL["erros"] += 1
            elif pulou:
                BACKFILL["puladas"] += 1
            else:
                BACKFILL["feitas"] += 1
            time.sleep(0.3)
    finally:
        BACKFILL["rodando"] = False
        BACKFILL["fim"] = _iso()
        _salvar_estado()
        print("[coletor] backfill fim: %s" % {k: v for k, v in BACKFILL.items() if k != "erro_lista"}, flush=True)
        _LOCKS["backfill"].release()

# ============================================================================= CRM
def _seller(o):
    s = o.get("seller") or o.get("responsible") or o.get("user") or o.get("owner")
    if isinstance(s, dict):
        return s.get("name")
    return s if isinstance(s, str) else None

def gravar_crm(opps, motivo="refresh"):
    """Grava as oportunidades que MUDARAM (hash por uuid). Chamado pelo gancho do refresh do cruzamento."""
    global _CRM_IDX
    if not _LOCKS["crm"].acquire(blocking=False):
        return
    try:
        _ini("crm", motivo)
        db = _db()
        if not db:
            _fim("crm", False, erro="firestore indisponivel"); return
        if _CRM_IDX is None:
            d = db.collection(COLL_EST).document("crm_indice").get()
            _CRM_IDX = ((d.to_dict() or {}).get("h") or {}) if d.exists else {}
        b, mudou = _Batch(db), 0
        for o in opps:
            uuid = o.get("uuid") or o.get("id")
            if not uuid:
                continue
            uuid = str(uuid)
            cust = o.get("customer") or {}
            ph = cust.get("phone")
            if isinstance(ph, dict):
                ph = ph.get("number") or ph.get("phone")
            st = o.get("stage")
            pz = o.get("pipeline")
            fk = _cruz._fone(ph) or None
            for k in ("patient_id", "lead_id"):
                if cust.get(k) and fk:
                    PID2FONE[str(cust.get(k))] = fk
            doc = {"uuid": uuid, "titulo": o.get("title") or o.get("name"),
                   "pipeline": o.get("pipeline_name") or (pz.get("name") if isinstance(pz, dict) else pz),
                   "estagio": (st.get("name") if isinstance(st, dict) else st),
                   "status": o.get("status"), "valor": o.get("amount"),
                   "fone": fk, "fone_raw": ph, "nome": cust.get("name"),
                   "patient_id": cust.get("patient_id"), "lead_id": cust.get("lead_id"),
                   "entity_type": cust.get("entity_type"), "ativo": cust.get("active"),
                   "vendedor": _seller(o), "origem": o.get("origin"),
                   "criado_em_ce": o.get("created_at"), "atualizado_em_ce": o.get("updated_at"),
                   "raw": json.dumps(o, ensure_ascii=False, default=str)[:30000]}
            h = _h(doc)
            if _CRM_IDX.get(uuid) == h:
                continue
            doc["gravado_em"] = _iso()
            b.set(db.collection(COLL_CRM).document(uuid), doc)
            _CRM_IDX[uuid] = h
            mudou += 1
        b.flush()
        if mudou:
            db.collection(COLL_EST).document("crm_indice").set({"h": _CRM_IDX, "atualizado_em": _iso()})
        _fim("crm", True, lidos=len(opps), gravados=mudou)
    except Exception as e:
        _fim("crm", False, lidos=len(opps or []), erro=repr(e))
    finally:
        _LOCKS["crm"].release()

_orig_api_interna_all = None

def _instalar_gancho_crm():
    """Grava o CRM no MESMO refresh do cruzamento (zero leitura extra): envolve _cruz._api_interna_all.
    So grava se a listagem veio COMPLETA (sem erro) - gate da camada."""
    global _orig_api_interna_all
    if _orig_api_interna_all is not None:
        return
    _orig_api_interna_all = _cruz._api_interna_all

    def _wrap(path, cap_pages=30):
        out, err = _orig_api_interna_all(path, cap_pages)
        try:
            if str(path).lstrip("/").startswith("api/crm/opportunity"):
                if out and not err:
                    threading.Thread(target=gravar_crm, args=(list(out), "refresh"), daemon=True).start()
                else:
                    ST["crm"]["ultima_tentativa"] = _iso()
                    ST["crm"]["ok"] = False
                    ST["crm"]["erro"] = "leitura incompleta/sem dado: %s" % (err or "vazio")
        except Exception as e:
            print("[coletor] gancho crm erro:", e, flush=True)
        return out, err

    _cruz._api_interna_all = _wrap

def ciclo_crm(motivo="poll"):
    """Fallback: se o cruzamento nao rodou recentemente (hoje ele so gira com painel aberto/webhook),
    le o CRM por conta propria. O gancho grava."""
    ult = _parse(ST["crm"].get("ultimo_ok"))
    if motivo == "poll" and ult and (_utcnow() - ult) < dt.timedelta(minutes=CRM_MIN - 5):
        return {"ok": True, "skip": "refresh recente"}
    out, err = _cruz._api_interna_all("api/crm/opportunity?per_page=100")
    return {"ok": not err, "lidos": len(out or []), "erro": err}

# ============================================================================= VENDAS (sales-quotes)
def _get_interna(path):
    tok = _cruz._app_jwt()
    if not tok:
        return None, "sem_app_jwt"
    req = urllib.request.Request(_cruz.INTERNAL + "/" + path.lstrip("/"), headers={
        "Authorization": "Bearer " + tok, "User-Agent": _cruz.mf.UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8", "ignore")), None
    except urllib.error.HTTPError as e:
        return None, "http_%s" % e.code
    except Exception as e:
        return None, str(e)[:60]

def ciclo_vendas(motivo="poll"):
    global _VEND_IDX
    if not _LOCKS["vendas"].acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        _ini("vendas", motivo)
        db = _db()
        if not db:
            _fim("vendas", False, erro="firestore indisponivel"); return {"ok": False}
        out, err = _orig_or_cur()("api/sales-quotes?per_page=100&sort_column=created_at&sort_direction=desc",
                                  int(os.environ.get("COLETOR_VENDAS_PAGINAS", "20")))
        out = out or []
        if not out:
            _fim("vendas", False, erro=err or "lista vazia"); return {"ok": False, "erro": err}
        if _VEND_IDX is None:
            d = db.collection(COLL_EST).document("vendas_indice").get()
            _VEND_IDX = ((d.to_dict() or {}).get("h") or {}) if d.exists else {}
        b, mudou, detalhes, pend = _Batch(db), 0, 0, 0
        for q in out:
            qid = str(q.get("id") or "")
            if not qid:
                continue
            buyer = q.get("buyer") if isinstance(q.get("buyer"), dict) else {}
            pid = q.get("buyer_person_id")
            stt = q.get("status")
            base = {"id": qid, "descricao": q.get("description"), "data": q.get("quote_date"),
                    "vencimento": q.get("due_date"), "valor_centavos": q.get("final_amount"),
                    "status": stt, "status_tela": _STATUS_TELA.get(stt, stt),
                    "comprador_id": pid, "comprador": buyer.get("name"), "vendedor": _seller(q),
                    "criado_em_ce": q.get("created_at"), "atualizado_em_ce": q.get("updated_at")}
            fk = PID2FONE.get(str(pid)) if pid is not None else None
            chave = dict(base, fone=fk) if fk else base
            h = _h(chave)
            if _VEND_IDX.get(qid) == h:
                continue
            doc = dict(base)
            if fk:
                doc["fone"] = fk                                   # merge: nunca apaga fone ja gravado
            doc["raw"] = json.dumps(q, ensure_ascii=False, default=str)[:20000]
            doc["gravado_em"] = _iso()
            completo = True
            if stt in ("won", "open", "due"):                     # regra de ouro: itens OFICIAIS do orcamento
                if detalhes < VENDAS_DETALHES_MAX:
                    det, e2 = _get_interna("api/sales-quotes/%s" % qid)
                    detalhes += 1
                    time.sleep(0.3)
                    if det and not e2:
                        dd = det.get("data") if isinstance(det.get("data"), dict) else det
                        doc["itens"] = [{"nome": i.get("name"), "qtd": i.get("quantity"),
                                         "valor_centavos": i.get("final_amount")}
                                        for i in (dd.get("items") or []) if isinstance(i, dict)]
                        doc["combos"] = json.dumps(dd.get("combos") or [], ensure_ascii=False, default=str)[:8000]
                        doc["itens_ok"] = True
                    else:
                        completo = False
                        doc["itens_ok"] = False
                else:
                    completo = False                               # fica para o proximo ciclo
                    pend += 1
            b.set(db.collection(COLL_VENDAS).document(qid), doc, merge=True)
            mudou += 1
            if completo:
                _VEND_IDX[qid] = h
        b.flush()
        if mudou:
            db.collection(COLL_EST).document("vendas_indice").set({"h": _VEND_IDX, "atualizado_em": _iso()})
        _fim("vendas", not err, lidos=len(out), gravados=mudou,
             erro=(err or (("%d com itens pendentes" % pend) if pend else None)), parcial=bool(err or pend))
        return {"ok": not err, "lidos": len(out), "gravados": mudou, "itens_pendentes": pend}
    except Exception as e:
        _fim("vendas", False, erro=repr(e)); return {"ok": False, "erro": repr(e)[:160]}
    finally:
        _LOCKS["vendas"].release()

def _orig_or_cur():
    # vendas nao passa pelo gancho do CRM (evita confundir as contagens)
    return _orig_api_interna_all or _cruz._api_interna_all

# ============================================================================= META
def ciclo_meta(motivo="poll", dia=None):
    if not _LOCKS["meta"].acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    try:
        import meta_ads as _meta
        _ini("meta", motivo)
        db = _db()
        if not db:
            _fim("meta", False, erro="firestore indisponivel"); return {"ok": False}
        if not _meta._tok():
            _fim("meta", False, erro="META_TOKEN ausente"); return {"ok": False}
        dia = dia or (_agora_sp().date() - dt.timedelta(days=1)).isoformat()
        rows = _meta.insights_por_campanha(dia, dia)
        doc = {"data": dia, "conta": _meta.ACT, "campanhas": rows,
               "gasto_total": round(sum(r.get("gasto") or 0 for r in rows), 2),
               "conversas_iniciadas_total": sum(r.get("conversas_iniciadas") or 0 for r in rows),
               "novos_contatos_total": sum(r.get("novos_contatos") or 0 for r in rows),
               "impressoes_total": sum(r.get("impressoes") or 0 for r in rows),
               "coletado_em": _iso()}
        db.collection(COLL_META).document(dia).set(doc)
        _fim("meta", True, lidos=len(rows), gravados=1)
        return {"ok": True, "dia": dia, "campanhas": len(rows), "gasto_total": doc["gasto_total"]}
    except urllib.error.HTTPError as e:
        _fim("meta", False, erro="http_%s" % e.code); return {"ok": False, "http": e.code}
    except Exception as e:
        _fim("meta", False, erro=repr(e)); return {"ok": False, "erro": repr(e)[:160]}
    finally:
        _LOCKS["meta"].release()

def _meta_catchup():
    """No boot: se o doc de ontem nao existe, coleta (cobre deploy/restart que perdeu as 06h00)."""
    db = _db()
    if not db:
        return
    ontem = (_agora_sp().date() - dt.timedelta(days=1)).isoformat()
    try:
        if not db.collection(COLL_META).document(ontem).get().exists:
            ciclo_meta("catchup", ontem)
    except Exception as e:
        print("[coletor] meta catchup erro:", e, flush=True)

def _meta_backfill(dias):
    for i in range(dias, 0, -1):
        ciclo_meta("backfill", (_agora_sp().date() - dt.timedelta(days=i)).isoformat())
        time.sleep(1.0)

# ============================================================================= SAUDE
_PROBE = {}

def _probe(nome, fn, ttl=300):
    c = _PROBE.get(nome)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        r = fn()
    except urllib.error.HTTPError as e:
        r = {"ok": False, "http": e.code}
    except Exception as e:
        r = {"ok": False, "erro": str(e)[:120]}
    _PROBE[nome] = (time.time(), r)
    return r

def _p_clinichat():
    _d, err = _cruz._cc("/contact-channel?from=0&to=1&orderBy=lastMessageTime%3ADESC&filters=%7B%7D")
    return {"ok": not err, "erro": err}

def _p_appjwt():
    if not _cruz._app_jwt():
        return {"ok": False, "erro": "sem app_jwt"}
    _d, err = _get_interna("api/crm/opportunity?page=1&per_page=1")
    return {"ok": not err, "erro": err}

def _p_meta():
    import meta_ads as _meta
    if not _meta._tok():
        return {"ok": False, "erro": "META_TOKEN ausente"}
    c = _meta._get(_meta.ACT, {"fields": "name,account_status"})
    return {"ok": True, "account_status": c.get("account_status")}

def _p_tintim():
    import tintim_api as _tt
    if not _tt._tok():
        return {"ok": False, "erro": "TINTIM_TOKEN ausente"}
    _tt.leadstatus()
    return {"ok": True}

_CONT = {"t": 0.0, "v": None}

def _contagens(ttl=180):
    if _CONT["v"] is not None and time.time() - _CONT["t"] < ttl:
        return _CONT["v"]
    db = _db()
    if not db:
        return None
    def cnt(q):
        try:
            r = q.count().get()
            return int(r[0][0].value)
        except Exception as e:
            return "erro: %s" % str(e)[:60]
    out = {"fichas_conversa": cnt(db.collection(COLL_CONV)),
           "escuta_crm": cnt(db.collection(COLL_CRM)),
           "escuta_vendas": cnt(db.collection(COLL_VENDAS)),
           "escuta_meta_dias": cnt(db.collection(COLL_META)),
           "escuta_tintim": cnt(db.collection(os.environ.get("ESCUTA_TINTIM_COLL", "escuta_tintim")))}
    msgs, por_mes = 0, {}
    hoje = _agora_sp().date().replace(day=1)
    for i in range(13):
        y, m = hoje.year, hoje.month - i
        while m <= 0:
            m += 12; y -= 1
        mes = "%04d-%02d" % (y, m)
        n = cnt(db.collection_group("msgs_" + mes))
        if isinstance(n, int) and n:
            por_mes[mes] = n
            msgs += n
    n = cnt(db.collection_group("msgs_sem_data"))
    if isinstance(n, int) and n:
        por_mes["sem_data"] = n
        msgs += n
    out["mensagens"] = msgs
    out["mensagens_por_mes"] = por_mes
    out["obs"] = "escuta_crm inclui o doc legado 'mapa' (+1)"
    _CONT.update({"t": time.time(), "v": out})
    return out

def registrar_extra(nome, fn):
    _EXTRA[nome] = fn

def saude():
    _carregar_estado()
    cr = _fw.token_info()
    fontes = {
        "clinichat_cracha": {"nivel": cr.get("nivel"), "expira_em_horas": cr.get("expira_em_horas"),
                             "probe": _probe("clinichat", _p_clinichat)},
        "ce_app_jwt": {"probe": _probe("appjwt", _p_appjwt)},
        "meta": {"probe": _probe("meta", _p_meta)},
        "tintim_api": {"probe": _probe("tintim", _p_tintim)},
    }
    try:
        import diario as _dd
        fontes["diario_envio"] = dict(_dd.ENVIO)
    except Exception:
        pass
    for nome, fn in _EXTRA.items():
        try:
            fontes[nome] = fn()
        except Exception as e:
            fontes[nome] = {"erro": str(e)[:80]}
    alertas = []
    if cr.get("nivel") in ("vencido", "sem_cracha"):
        alertas.append("Cracha do CliniChat %s - conversas NAO estao sendo lidas" % cr.get("nivel"))
    elif cr.get("nivel") == "vencendo":
        alertas.append("Cracha do CliniChat vence em %sh" % cr.get("expira_em_horas"))
    if not fontes["ce_app_jwt"]["probe"].get("ok"):
        alertas.append("app_jwt do CE recusado (%s) - CRM/vendas param; re-alimentar via /ingest_app_jwt"
                       % (fontes["ce_app_jwt"]["probe"].get("erro") or fontes["ce_app_jwt"]["probe"].get("http")))
    if not fontes["meta"]["probe"].get("ok"):
        alertas.append("META_TOKEN com problema: %s" % (fontes["meta"]["probe"].get("erro") or fontes["meta"]["probe"].get("http")))
    if not fontes["tintim_api"]["probe"].get("ok"):
        alertas.append("TINTIM_TOKEN com problema: %s" % (fontes["tintim_api"]["probe"].get("erro") or fontes["tintim_api"]["probe"].get("http")))
    _de = fontes.get("diario_envio") or {}
    if _de.get("ok") is False and _de.get("erro"):
        alertas.append("Diario NAO chegou no WhatsApp: %s" % _de.get("erro"))
    atraso = {"conversas": CONV_MIN * 3, "crm": CRM_MIN * 3, "vendas": 180, "meta": 60 * 30}
    for f in _FONTES:
        u = _parse(ST[f].get("ultimo_ok"))
        if u is None:
            alertas.append("coletor %s ainda sem leitura OK" % f)
        elif (_utcnow() - u) > dt.timedelta(minutes=atraso[f]):
            alertas.append("coletor %s sem leitura OK ha %d min" % (f, (_utcnow() - u).total_seconds() // 60))
        if ST[f].get("ok") is False and ST[f].get("erro"):
            alertas.append("coletor %s: %s" % (f, ST[f]["erro"]))
    status = "ok" if not alertas else ("falha" if any(("NAO" in a or "recusado" in a) for a in alertas) else "atencao")
    return {"status": status, "gerado_em": _iso(), "alertas": alertas, "fontes": fontes,
            "coletor": ST, "watermark_conversas": WM.get("conversas_lt"), "id_campo_msgs": ID_CAMPO, "formatos_data": FORMATOS, "campos_data_conversa": CAMPOS_CONV, "datas_msgs_lidas": DATAS_MSG, "leitura_total": LEITURA_TOTAL, "criacao": CRIACAO,
            "backfill": BACKFILL, "contagens": _contagens(),
            "agenda": {"conversas_min": CONV_MIN, "crm": "a cada refresh do cruzamento + fallback %dmin" % CRM_MIN,
                       "vendas": "1x/h (min 10)", "meta": "1x/dia 06h00 (dia anterior) + catchup no boot"}}

# ============================================================================= SCHEDULER + ROTAS
_SCHED = None

NOVIDADE_HOOKS = []   # funcoes chamadas com a lista de conversas que tiveram mensagem nova no ciclo

def _conv_tick():
    # 30/10: radar a cada 1 min.
    # 30/10 (Xandao): roda das 06h00 as 23h59; pausa total da 00h00 as 05h59.
    if _agora_sp().hour >= 6:
        ciclo_conversas("poll")

def iniciar():
    """Chamado no startup do main. Scheduler PROPRIO (o do main nao esta registrado no app)."""
    global _SCHED
    if _SCHED is not None or os.environ.get("COLETOR_ON", "1") == "0":
        return
    _instalar_gancho_crm()
    try:
        _carregar_estado()
    except Exception:
        pass
    from apscheduler.schedulers.background import BackgroundScheduler
    s = BackgroundScheduler(timezone="America/Sao_Paulo",
                            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900})
    agora = _agora_sp()
    s.add_job(lambda: ciclo_crm("boot"), "date", run_date=agora + dt.timedelta(seconds=45))
    s.add_job(lambda: ciclo_crm("poll"), "interval", minutes=CRM_MIN)
    s.add_job(_conv_tick, "interval", minutes=1,
              next_run_time=agora + dt.timedelta(seconds=90))
    s.add_job(lambda: ciclo_vendas("boot"), "date", run_date=agora + dt.timedelta(seconds=150))
    s.add_job(lambda: ciclo_vendas("poll"), "cron", minute=10)
    s.add_job(lambda: ciclo_meta("06h00"), "cron", hour=6, minute=0)
    s.add_job(_meta_catchup, "date", run_date=agora + dt.timedelta(seconds=60))
    s.start()
    _SCHED = s
    print("[coletor] iniciado: conversas %dmin, crm refresh+%dmin, vendas 1h, meta 06h00" % (CONV_MIN, CRM_MIN), flush=True)

from fastapi import APIRouter, Header, HTTPException
import hmac as _hmac

router = APIRouter()

def _check(x):
    if not SECRET or not x or not _hmac.compare_digest(x, SECRET):
        raise HTTPException(status_code=401, detail="unauthorized")

@router.get("/saude")
def rota_saude():
    # publico: so estado/contagens, sem PII
    return saude()

@router.post("/coletor/rodar")
def rota_rodar(fonte: str, x_idm_secret: str = Header(default="")):
    _check(x_idm_secret)
    fn = {"conversas": ciclo_conversas, "crm": ciclo_crm, "vendas": ciclo_vendas, "meta": ciclo_meta}.get(fonte)
    if not fn:
        raise HTTPException(status_code=400, detail="fonte: conversas|crm|vendas|meta")
    threading.Thread(target=fn, args=("manual",), daemon=True).start()
    return {"ok": True, "iniciado": fonte}

@router.post("/coletor/backfill")
def rota_backfill(dias: int = 90, fonte: str = "conversas", x_idm_secret: str = Header(default="")):
    _check(x_idm_secret)
    dias = max(1, min(int(dias), 365))
    if fonte == "meta":
        threading.Thread(target=_meta_backfill, args=(min(dias, 90),), daemon=True).start()
        return {"ok": True, "iniciado": "meta", "dias": min(dias, 90)}
    if not _LOCKS["backfill"].acquire(blocking=False):
        return {"ok": False, "motivo": "backfill ja rodando", "progresso": BACKFILL}
    threading.Thread(target=_backfill_run, args=(dias,), daemon=True).start()
    return {"ok": True, "iniciado": "conversas", "dias": dias, "acompanhar": "/saude -> backfill"}

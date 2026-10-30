# -*- coding: utf-8 -*-
"""
IDM - ENRIQUECEDOR / FICHA VIVA (passo 3 da SPEC-Coletor-Unico, 30/10/2026). ADITIVO.

Le SO do banco (escuta_conversas + msgs, escuta_crm, escuta_vendas, escuta_tintim) e grava
escuta_leads/{fone} - a ficha viva de cada pessoa. Nenhuma chamada ao CliniChat/CE.

Duas camadas:
  1) DETERMINISTICA (regra, sem IA): identidade, origem (prova = 1a mensagem do lead; TimTim so da o
     nome da campanha), CRM, orcamentos/vendas, conduta da equipe (1a resposta, perguntou cidade/origem,
     quem esta esperando quem) e furos.
  2) CATEGORIAS POR IA (status "rascunho" ate o Xandao aprovar a lista e a regua): servico, cirurgia de
     origem, intencao, momentos-chave, nota da abordagem e resumo INCREMENTAL (o modelo recebe o resumo
     anterior + so as mensagens novas; reanalisa a cada mensagem nova, inclusive da equipe).
     "Fechou/nao fechou" NUNCA vem da IA: vem do orcamento Ganho / CRM.
"""
import os, re, json, time, threading, datetime as dt

import firebase_writer as _fw
import motor_cruzamento as _cruz

COLL_LEADS = os.environ.get("ENRIQ_COLL", "escuta_leads")
COLL_CONV = os.environ.get("COLETOR_COLL_CONV", "escuta_conversas")
COLL_CRM = os.environ.get("ESCUTA_CRM_COLL", "escuta_crm")
COLL_VENDAS = os.environ.get("COLETOR_COLL_VENDAS", "escuta_vendas")
COLL_TT = os.environ.get("ESCUTA_TINTIM_COLL", "escuta_tintim")
IA_MODEL = os.environ.get("ENRIQ_MODEL", os.environ.get("JARBAS_TEMP_MODEL", "claude-haiku-5-5"))
IA_ON = os.environ.get("ENRIQ_IA", "1") == "1"
IA_LOTE = int(os.environ.get("ENRIQ_IA_LOTE", "60"))      # max analises de IA por rodada
VERSAO_REGUA = "v0-rascunho-30/10"

ST = {"ultima": None, "ok": None, "erro": None, "fichas": 0, "gravadas": 0, "ia_feitas": 0,
      "ia_erros": 0, "ia_pendentes": 0, "rodando": False, "duracao_s": None}
_LOCK = threading.Lock()

# ----------------------------------------------------------------------------- util
def _utc():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def _iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)

def _parse(v):
    if not v:
        return None
    try:
        if hasattr(v, "timestamp") and not isinstance(v, str):
            d = v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        if "." in s:
            head, rest = s.split(".", 1)
            frac, tz = rest, ""
            for i, ch in enumerate(rest):
                if not ch.isdigit():
                    frac, tz = rest[:i], rest[i:]
                    break
            s = head + "." + (frac + "000000")[:6] + tz
        d = dt.datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    except Exception:
        return None

def _sp_date(d):
    return (d - dt.timedelta(hours=3)).date().isoformat() if d else None

_AUTO = re.compile(r"confirme o seu hor|j. est. reservad|uhuull|para confirmar o agendamento|seu agendamento foi|"
                   r"lembrete de agendamento|avalia..o de atendimento|pesquisa de satisfa|mensagem autom|"
                   r"no momento estamos fora|hor.rio de atendimento", re.I)
_CIDADE = re.compile(r"\b(cidade|bairro|regi[aã]o|onde (voc[eê]|vc) mora|de onde (voc[eê]|vc)|qual (sua|a) localiza|"
                     r"voc[eê] (mora|reside)|mora (em|perto|onde))", re.I)
_ORIGEM_Q = re.compile(r"como (nos|n[oó]s) conheceu|onde (nos|n[oó]s) (conheceu|encontrou|achou)|como (ficou sabendo|chegou at)|"
                       r"quem (te |lhe )?indicou|por onde (nos|n[oó]s) conheceu|conheceu o instituto", re.I)
_LABEL_ORIGEM = {"trafego_pago": "Tráfego pago", "organico_bio": "Orgânico · Bio", "organico_instagram": "Orgânico · Direct/Instagram",
                 "organico_site": "Orgânico · Site", "organico_google": "Orgânico · Google",
                 "relacionamento_indicacao": "Indicação", "nao_informada": "Origem não informada"}

# ----------------------------------------------------------------------------- indices (lidos do banco 1x por rodada)
def _indices(db):
    crm, vendas, tt = {}, {}, {}
    for d in db.collection(COLL_CRM).stream():
        if d.id in ("mapa",):
            continue
        o = d.to_dict() or {}
        f = o.get("fone")
        if f:
            has_task = None
            try:
                has_task = (json.loads(o.get("raw") or "{}") or {}).get("has_task")
            except Exception:
                pass
            crm.setdefault(f, []).append({"uuid": o.get("uuid"), "pipeline": o.get("pipeline"), "estagio": o.get("estagio"),
                                          "status": o.get("status"), "valor": o.get("valor"), "vendedor": o.get("vendedor"),
                                          "criado_em_ce": o.get("criado_em_ce"), "atualizado_em_ce": o.get("atualizado_em_ce"),
                                          "has_task": has_task, "patient_id": o.get("patient_id"), "lead_id": o.get("lead_id")})
    for d in db.collection(COLL_VENDAS).stream():
        v = d.to_dict() or {}
        f = v.get("fone")
        if f:
            vendas.setdefault(f, []).append({"id": v.get("id"), "status": v.get("status"), "status_tela": v.get("status_tela"),
                                             "valor_centavos": v.get("valor_centavos"), "data": v.get("data"),
                                             "itens": [i.get("nome") for i in (v.get("itens") or []) if isinstance(i, dict)]})
    for d in db.collection(COLL_TT).stream():
        e = d.to_dict() or {}
        p = e.get("payload") or {}
        lead = p.get("lead") if isinstance(p.get("lead"), dict) else p
        f = _cruz._fone(e.get("phone") or lead.get("phone") or "")
        if not f:
            continue
        ad = lead.get("ad") if isinstance(lead.get("ad"), dict) else (p.get("ad") if isinstance(p.get("ad"), dict) else {})
        cur = tt.setdefault(f, {"campanha": None, "fonte": None, "eventos": 0})
        cur["eventos"] += 1
        if ad.get("campaign_name"):
            cur["campanha"] = ad.get("campaign_name")
        src = lead.get("source") or p.get("source")
        if src:
            cur["fonte"] = src if isinstance(src, str) else (src.get("name") if isinstance(src, dict) else str(src))
    return crm, vendas, tt

def _msgs(db, fk):
    ref = db.collection(COLL_CONV).document(fk)
    out = []
    for sub in ref.collections():
        if not sub.id.startswith("msgs_"):
            continue
        for d in sub.stream():
            m = d.to_dict() or {}
            m["_dt"] = _parse(m.get("data"))
            out.append(m)
    out.sort(key=lambda m: m["_dt"] or _utc())
    return out

# ----------------------------------------------------------------------------- camada deterministica
def _deterministica(fk, ficha, msgs, crm, vendas, tt):
    lead_msgs = [m for m in msgs if m.get("de") == "lead" and (m.get("texto") or m.get("transcricao") or m.get("midia"))]
    eq = [m for m in msgs if m.get("de") == "equipe" and (m.get("texto") or m.get("midia"))]
    eq_humano = [m for m in eq if not _AUTO.search(m.get("texto") or "")]
    primeiro_lead = lead_msgs[0] if lead_msgs else None
    txt1 = ((primeiro_lead or {}).get("texto") or (primeiro_lead or {}).get("transcricao") or "") if primeiro_lead else ""

    # origem: prova = 1a mensagem do lead; TimTim so nomeia a campanha
    cod = _cruz._classifica_origem(txt1, {}) if txt1 else "nao_informada"
    t = tt.get(fk) or {}
    origem = _LABEL_ORIGEM.get(cod, cod)
    if cod == "trafego_pago":
        origem = "Tráfego pago · " + (t.get("campanha") or "campanha não identificada")
    perguntou_origem = any(_ORIGEM_Q.search(m.get("texto") or "") for m in eq_humano)

    # 1a resposta humana depois da 1a mensagem do lead
    t1 = None
    if primeiro_lead and primeiro_lead.get("_dt"):
        for m in eq_humano:
            if m.get("_dt") and m["_dt"] >= primeiro_lead["_dt"]:
                t1 = round((m["_dt"] - primeiro_lead["_dt"]).total_seconds() / 60.0, 1)
                break
    perguntou_cidade = any(_CIDADE.search(m.get("texto") or "") for m in eq_humano)

    # CRM / vendas
    opps = crm.get(fk) or []
    leads_vivas = [o for o in opps if (o.get("pipeline") or "").upper().startswith("LEADS")
                   and not (o.get("estagio") or "").lower().startswith(("perdido", "desqualific", "ganho"))]
    comprou_crm = any((o.get("pipeline") or "").upper().startswith("CLIENTES") or (o.get("estagio") or "").lower().startswith("ganho") for o in opps)
    vs = vendas.get(fk) or []
    ganhos = [v for v in vs if v.get("status") == "won"]
    abertos = [v for v in vs if v.get("status") in ("open", "due")]
    comprou = comprou_crm or bool(ganhos) or _cruz._eh_comprador(fk)
    identidade = "cliente" if comprou else ("lead_cadastrado" if opps else "novo")
    if fk.startswith("sem_fone"):
        identidade = "sem_telefone"
    if any((o.get("estagio") or "").lower().startswith(("perdido", "desqualific")) for o in opps) and not leads_vivas and not comprou:
        situacao_crm = "perdido"
    elif leads_vivas:
        situacao_crm = "em_andamento"
    elif opps:
        situacao_crm = "cliente" if comprou else "outro_funil"
    else:
        situacao_crm = "sem_oportunidade"
    principal = (leads_vivas or opps or [None])[0]

    # quem espera quem
    ult = _parse(ficha.get("ultima_msg_em"))
    de = ficha.get("ultima_msg_de")
    horas = round((_utc() - ult).total_seconds() / 3600.0, 1) if ult else None
    esperando = "equipe" if de == "lead" else ("lead" if de == "equipe" else None)

    furos = []
    if esperando == "equipe" and horas is not None and horas >= 2 and identidade != "sem_telefone":
        furos.append("lead_sem_resposta")
    if identidade in ("novo",) and lead_msgs and not opps:
        furos.append("sem_oportunidade_no_crm")
    if identidade != "cliente" and len(eq_humano) >= 2 and not perguntou_cidade:
        furos.append("cidade_nao_perguntada")
    if identidade != "cliente" and cod == "nao_informada" and len(eq_humano) >= 2 and not perguntou_origem:
        furos.append("origem_nao_perguntada")
    if t1 is not None and t1 > 60:
        furos.append("primeira_resposta_lenta")
    if principal and principal.get("has_task") is False and situacao_crm == "em_andamento":
        furos.append("oportunidade_sem_tarefa")

    return {
        "fone": fk, "nome": ficha.get("nome"), "external_id": ficha.get("external_id"),
        "primeira_chegada": ficha.get("primeira_chegada"), "data_chegada_sp": _sp_date(_parse(ficha.get("primeira_chegada"))),
        "ultima_msg_em": ficha.get("ultima_msg_em"), "ultima_msg_de": de, "horas_desde_ultima": horas, "esperando": esperando,
        "total_msgs": ficha.get("total_msgs"), "msgs_lead": len(lead_msgs), "msgs_equipe": len(eq), "msgs_equipe_humanas": len(eq_humano),
        "identidade": identidade, "situacao_crm": situacao_crm,
        "origem": origem, "origem_codigo": cod, "origem_tintim": t.get("fonte"), "campanha_tintim": t.get("campanha"),
        "primeira_msg_lead": txt1[:500],
        "conduta": {"primeira_resposta_min": t1, "perguntou_cidade": perguntou_cidade, "perguntou_origem": perguntou_origem},
        "crm": {"oportunidades": len(opps), "estagio": (principal or {}).get("estagio"), "pipeline": (principal or {}).get("pipeline"),
                "vendedor": (principal or {}).get("vendedor"), "valor": (principal or {}).get("valor"),
                "uuid": (principal or {}).get("uuid"), "tem_tarefa": (principal or {}).get("has_task"),
                "patient_id": (principal or {}).get("patient_id"), "lead_id": (principal or {}).get("lead_id")},
        "vendas": {"orcamentos": len(vs), "ganhos": len(ganhos), "abertos": len(abertos),
                   "ganho_centavos": sum(int(v.get("valor_centavos") or 0) for v in ganhos),
                   "aberto_centavos": sum(int(v.get("valor_centavos") or 0) for v in abertos),
                   "itens_comprados": sorted({i for v in ganhos for i in (v.get("itens") or []) if i})[:20]},
        "furos": furos,
    }

# ----------------------------------------------------------------------------- camada IA (rascunho)
SERVICOS = ["pos_op_corporal", "pos_op_facial", "pre_operatorio", "drenagem_linfatica", "laser_depilacao", "laser_outros",
            "estetica_corporal", "estetica_facial", "massagem_spa", "mentoria_curso_profissional", "outro", "nenhum_identificado"]
CIRURGIAS = ["abdominoplastia", "lipoaspiracao", "lipo_hd_lipoescultura", "mamoplastia_aumento", "mamoplastia_reducao",
             "mastopexia", "rinoplastia", "blefaroplastia", "lifting_facial", "gluteoplastia", "braquioplastia",
             "cirurgia_combinada_mommy_makeover", "bariatrica_ou_pos_bariatrica", "outra", "nao_mencionada"]
INTENCOES = ["quer_preco", "quer_agendar", "duvida_tratamento", "logistica_de_cliente", "reclamacao_insatisfacao",
             "fornecedor_parceiro_ruido", "interno_ou_engano", "outro"]
MOMENTOS = ["pediu_preco", "objecao_preco", "pediu_agendamento", "agendou_avaliacao", "sumiu_apos_preco",
            "informou_data_cirurgia", "informou_cirurgiao", "reclamacao", "pediu_desconto", "comparou_concorrente"]
FALHAS = ["nao_perguntou_cidade_bairro", "passou_preco_fechado", "nao_conduziu_para_avaliacao", "demorou_a_responder",
          "nao_retomou_lead_parado", "tom_frio_ou_robotico", "nao_perguntou_origem", "informacao_incorreta", "nenhuma"]

_SCHEMA = {"type": "object", "properties": {
    "servicos": {"type": "array", "items": {"type": "string", "enum": SERVICOS}},
    "cirurgias_origem": {"type": "array", "items": {"type": "string", "enum": CIRURGIAS}},
    "intencao": {"type": "string", "enum": INTENCOES},
    "momentos": {"type": "array", "items": {"type": "object", "properties": {
        "tipo": {"type": "string", "enum": MOMENTOS}, "trecho": {"type": "string"}}, "required": ["tipo"]}},
    "abordagem_nota": {"type": "string", "enum": ["correta", "com_falhas", "sem_dados"]},
    "abordagem_falhas": {"type": "array", "items": {"type": "string", "enum": FALHAS}},
    "resumo": {"type": "string"}},
    "required": ["servicos", "cirurgias_origem", "intencao", "momentos", "abordagem_nota", "abordagem_falhas", "resumo"]}

_SYSTEM = ("Voce le conversas de WhatsApp do Instituto Dayane Mafra (Barueri/Alphaville), clinica de PRE e POS-OPERATORIO de "
           "cirurgia plastica e estetica. O Instituto NAO opera: o cirurgiao opera; o Instituto faz o pre/pos (drenagem, etc.). "
           "Por isso separe: 'servicos' = o que a pessoa quer/usa DO INSTITUTO; 'cirurgias_origem' = a cirurgia plastica que ela fez "
           "ou vai fazer (so marque se a conversa disser; senao 'nao_mencionada').\n"
           "REGUA DE ABORDAGEM (rascunho %s) - avalie SO o atendimento humano da equipe (ignore mensagens automaticas):\n"
           "1) perguntar cidade/bairro logo no inicio (2a mensagem); 2) perguntar como conheceu o Instituto (origem);\n"
           "3) preco: passar a MEDIA de valores e dizer que a Avaliacao define o valor final - NUNCA preco fechado;\n"
           "4) proximo passo e sempre a AVALIACAO (presencial ou online); 5) responder rapido e retomar lead parado;\n"
           "6) tom acolhedor, humano, sem parecer robo. Se a equipe quase nao falou, use 'sem_dados'.\n"
           "'momentos': marque os momentos-chave com um trecho CURTO (ate 12 palavras) da conversa. "
           "'resumo': 2 a 3 frases objetivas sobre quem e a pessoa, o que quer e em que pe esta. "
           "Nunca invente: se nao estiver na conversa, nao marque. Voce NAO decide se houve venda.") % VERSAO_REGUA

def _linha(m):
    who = "Lead" if m.get("de") == "lead" else ("Equipe" if m.get("de") == "equipe" else "Sistema")
    t = (m.get("texto") or "").strip()
    if not t and m.get("transcricao"):
        t = "[audio] " + m["transcricao"].strip()
    if not t and m.get("midia"):
        t = "[%s]" % (m.get("tipo") or "midia")
    if not t:
        return None
    if who == "Equipe" and _AUTO.search(t):
        who = "Equipe(automatica)"
    d = m.get("_dt")
    return "%s %s: %s" % ((d - dt.timedelta(hours=3)).strftime("%d/%m %H:%M") if d else "", who, t[:600])

def _ia(fk, msgs, anterior):
    import jarbas as _j
    ate_ant = _parse((anterior or {}).get("ia_ate"))
    novos = [m for m in msgs if m.get("_dt") and (ate_ant is None or m["_dt"] > ate_ant)]
    linhas = [l for l in (_linha(m) for m in (novos if ate_ant else msgs)[-120:]) if l]
    if not linhas:
        return None
    ctx = ""
    if ate_ant and (anterior or {}).get("ia"):
        a = anterior["ia"]
        ctx = ("ANALISE ANTERIOR (atualize com as mensagens novas; mantenha o que continua valendo):\n" +
               json.dumps({k: a.get(k) for k in ("servicos", "cirurgias_origem", "intencao", "momentos",
                                                   "abordagem_nota", "abordagem_falhas", "resumo")}, ensure_ascii=False) + "\n\n")
    user = ctx + ("MENSAGENS NOVAS:\n" if ctx else "CONVERSA:\n") + "\n".join(linhas)[:24000]
    r = _j._struct_call(IA_MODEL, _SYSTEM, user, _SCHEMA, "categorias", 900, timeout=60)
    ia = {k: r.get(k) for k in _SCHEMA["properties"]}
    ia["status"] = "rascunho"
    ia["regua"] = VERSAO_REGUA
    ia["modelo"] = IA_MODEL
    ultima = max((m["_dt"] for m in msgs if m.get("_dt")), default=None)
    return ia, (_iso(ultima) if ultima else None)

# ----------------------------------------------------------------------------- rodada
def rodar(motivo="poll", so_mudadas=True, com_ia=True):
    if not _LOCK.acquire(blocking=False):
        return {"ok": True, "skip": "ja rodando"}
    t0 = time.time()
    ST.update({"rodando": True, "erro": None})
    try:
        db = _fw._get_db()
        if not db:
            ST.update({"ok": False, "erro": "firestore indisponivel"}); return {"ok": False}
        crm, vendas, tt = _indices(db)
        fichas = {d.id: (d.to_dict() or {}) for d in db.collection(COLL_CONV).stream()}
        atuais = {d.id: (d.to_dict() or {}) for d in db.collection(COLL_LEADS).stream()}
        grav, ia_feitas, ia_erros, ia_pend = 0, 0, 0, 0
        for fk, ficha in fichas.items():
            ant = atuais.get(fk) or {}
            mudou_msg = (ant.get("ultima_msg_em") != ficha.get("ultima_msg_em")) or not ant
            precisa_ia = com_ia and IA_ON and ant.get("ia_ate") != ficha.get("ultima_msg_em")
            if so_mudadas and not mudou_msg and not precisa_ia and motivo not in ("completo",):
                continue
            msgs = _msgs(db, fk)
            doc = _deterministica(fk, ficha, msgs, crm, vendas, tt)
            doc["atualizado_em"] = _iso(_utc())
            if precisa_ia and not doc["msgs_lead"]:
                doc["ia_ate"] = ficha.get("ultima_msg_em")          # nada do lead para ler: nao tenta de novo
                doc["ia_dispensada"] = "sem mensagem do lead"
            elif precisa_ia:
                if ia_feitas + ia_erros < IA_LOTE:
                    try:
                        res = _ia(fk, msgs, ant)
                        if res:
                            doc["ia"], doc["ia_ate"] = res[0], ficha.get("ultima_msg_em")
                            doc["ia_em"] = _iso(_utc())
                            ia_feitas += 1
                    except Exception as e:
                        ia_erros += 1
                        doc["ia_erro"] = str(e)[:160]
                    time.sleep(0.3)
                else:
                    ia_pend += 1
            db.collection(COLL_LEADS).document(fk).set(doc, merge=True)
            grav += 1
        ST.update({"ultima": _iso(_utc()), "ok": True, "fichas": len(fichas), "gravadas": grav, "ia_feitas": ia_feitas,
                   "ia_erros": ia_erros, "ia_pendentes": ia_pend, "duracao_s": round(time.time() - t0, 1)})
        print("[enriq] %s fichas=%d gravadas=%d ia=%d ia_erros=%d pend=%d %.0fs" % (motivo, len(fichas), grav, ia_feitas,
              ia_erros, ia_pend, time.time() - t0), flush=True)
        return dict(ST)
    except Exception as e:
        ST.update({"ok": False, "erro": repr(e)[:200]})
        print("[enriq] erro:", repr(e), flush=True)
        return {"ok": False, "erro": repr(e)[:200]}
    finally:
        ST["rodando"] = False
        _LOCK.release()

def iniciar(sched):
    if sched is None or os.environ.get("ENRIQ_ON", "1") == "0":
        return
    agora = dt.datetime.utcnow() - dt.timedelta(hours=3)
    sched.add_job(lambda: rodar("poll"), "interval", minutes=int(os.environ.get("ENRIQ_MIN", "15")),
                  next_run_time=agora + dt.timedelta(seconds=240))
    sched.add_job(lambda: rodar("completo", so_mudadas=False, com_ia=True), "cron", hour=6, minute=25)  # antes do Diario
    print("[enriq] agendado: a cada %s min + completo 06h25" % os.environ.get("ENRIQ_MIN", "15"), flush=True)

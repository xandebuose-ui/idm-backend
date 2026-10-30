# -*- coding: utf-8 -*-
"""
IDM - CADENCIA (30/10/2026). Regra do gestor: nenhuma conversa de lead termina sem TAREFA no CRM (proximo passo + data).

Todo dia 06h35 (depois da ficha viva completa das 06h25, antes do Diario das 06h45):
  1) CONFERE as tarefas de cadencia de ONTEM: o lead agora tem tarefa ABERTA no CRM *com descricao*? -> resolvido.
     Tarefa sem descricao NAO conta (a atendente tem que anotar o que foi feito / o proximo passo).
  2) GERA as de HOJE: uma tarefa no "Minhas Tarefas" do IDM (colecao tasks) para cada lead em andamento no funil LEADS
     sem tarefa aberta no CRM. Dono = responsavel da oportunidade no CRM; se vazio, quem mais falou com o lead.
     Sem dono identificado -> vai para a funcao 'comercial'.
  3) 07h00: manda a lista de cada atendente no WhatsApp dela pelo CliniChat.
Resumo do dia em escuta_cadencia/{AAAA-MM-DD} (o Diario le daqui).
"""
import os, json, unicodedata, datetime as dt
from collections import defaultdict

import firebase_writer as _fw

COLL = "escuta_cadencia"
FONES = {}
for _p in os.environ.get("ATENDENTES_FONES", "isadora:11948631848,julia:11988611885").split(","):
    if ":" in _p:
        k, v = _p.split(":", 1)
        FONES[k.strip().lower()] = v.strip()
ST = {"ultima": None, "ok": None, "erro": None, "geradas": 0, "conferidas": 0, "resolvidas": 0, "envios": {}}

def _sp_now():
    return dt.datetime.utcnow() - dt.timedelta(hours=3)

def _key(nome):
    s = unicodedata.normalize("NFKD", (nome or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.split()[0] if s.split() else ""

def _usuarios(db):
    out = {}
    for d in db.collection("users").stream():
        u = d.to_dict() or {}
        k = _key(u.get("name"))
        if k and u.get("role") in ("comercial", "recepcao", "gestao", "social_seller", "avaliadora"):
            out[k] = {"uid": d.id, "nome": u.get("name"), "role": u.get("role")}
    return out

def _pp(fone):
    import motor_cruzamento as _cruz
    try:
        return _cruz.proximo_passo(fone) or {}
    except Exception:
        return {}

def _dm(iso):
    s = str(iso or "")
    return (s[8:10] + "/" + s[5:7]) if len(s) >= 10 else "?"

def conferir(db, dia):
    """Confere os itens de cadencia de 'dia' contra o CRM/agenda de AGORA (regra 30/10):
    - tipo proximo_passo: resolvido se hoje tem tarefa aberta COM DATA (hoje ou depois) ou agendamento futuro,
      ou se a oportunidade foi fechada (ganho/perdido). Descricao NAO e exigida.
    - tipo atualizar_etapa: resolvido se a etapa do CRM ja esta em Agendamento (ou a opp foi fechada)."""
    ref = db.collection(COLL).document(dia)
    snap = ref.get()
    if not snap.exists:
        return None
    doc = snap.to_dict() or {}
    res = {"total": 0, "resolvidos": 0, "sem_descricao": 0, "por_dono": {}}
    for it in doc.get("itens") or []:
        pp = _pp(it.get("fone"))
        if it.get("tipo") == "atualizar_etapa":
            ok = (not pp.get("tem_opp_viva")) or (pp.get("etapa") or "").lower().startswith("agend")
        else:
            ok = bool(pp.get("ok")) or not pp.get("tem_opp_viva")
        it["resolvido"] = ok
        d = res["por_dono"].setdefault(it.get("dono") or "sem dono", {"total": 0, "resolvidos": 0, "sem_descricao": 0})
        d["total"] += 1; res["total"] += 1
        if ok:
            d["resolvidos"] += 1; res["resolvidos"] += 1
    ref.set({"itens": doc.get("itens") or [], "conferencia": res,
             "conferido_em": _sp_now().isoformat(timespec="seconds")}, merge=True)
    return res

def gerar(db, dia):
    usuarios = _usuarios(db)
    leads = [x.to_dict() or {} for x in db.collection("escuta_leads").stream()]
    itens = []
    for l in leads:
        if l.get("tipo_contato") not in (None, "lead") or l.get("interno"):
            continue
        if l.get("situacao_crm") != "em_andamento":
            continue
        pp = _pp(l.get("fone"))
        if pp.get("ok") and not pp.get("atualizar_etapa"):
            continue                                   # tem proximo passo com data: em dia
        if not pp.get("tem_opp_viva"):
            continue
        if pp.get("ok") and pp.get("atualizar_etapa"):
            tipo = "atualizar_etapa"
        elif pp.get("tipo") == "agendamento_sem_agenda":
            tipo = "lancar_agendamento"
        else:
            tipo = "proximo_passo"
        crm = l.get("crm") or {}
        dono_nome = crm.get("vendedor") or (l.get("conduta") or {}).get("atendente")
        u = usuarios.get(_key(dono_nome)) if dono_nome else None
        h = l.get("horas_desde_ultima")
        itens.append({"fone": l.get("fone"), "nome": l.get("nome") or l.get("fone"), "etapa": pp.get("etapa") or crm.get("estagio"),
                      "dono": (u or {}).get("nome") or None, "uid": (u or {}).get("uid"), "tipo": tipo,
                      "pp_tipo": pp.get("tipo"), "pp_quando": pp.get("quando"),
                      "dias_parado": int((h or 0) // 24), "esperando": l.get("esperando")})
    itens.sort(key=lambda i: (i["tipo"] != "proximo_passo", i.get("esperando") != "equipe", -(i["dias_parado"])))
    b = db.batch(); n = 0
    novos_ids = set()
    for it in itens:
        tid = "cad_%s_%s" % (dia, it["fone"])
        novos_ids.add(tid)
        if it["tipo"] == "lancar_agendamento":
            titulo = "📅 Lançar o agendamento na agenda ou corrigir a etapa: %s" % it["nome"]
            desc = ("A oportunidade está na etapa Agendamento, mas não há horário na agenda do Clinic Expert. "
                    "Se a pessoa agendou, lance o horário na agenda. Se não agendou, volte a etapa e crie a próxima tarefa COM DATA.")
        elif it["tipo"] == "atualizar_etapa":
            titulo = "🔄 Atualizar etapa no CRM: %s" % it["nome"]
            desc = ("Já tem agendamento em %s, mas a oportunidade ainda está em %s. Mude a etapa para Agendamento."
                    % (_dm(it.get("pp_quando")), it.get("etapa") or "?"))
        else:
            titulo = "📌 Definir próximo passo no CRM: %s" % it["nome"]
            if it.get("esperando") == "equipe":
                quem = "o lead está esperando resposta"
            else:
                quem = "última conversa há %d dia(s)" % it["dias_parado"]
            venc = (" A última tarefa venceu em %s." % _dm(it.get("pp_quando"))) if it.get("pp_tipo") == "vencida" else ""
            desc = ("Etapa %s · %s.%s Crie uma TAREFA COM DATA para a próxima ação (ligar, nova abordagem, mandar "
                    "vídeo…) ou faça o agendamento. Se o lead desistiu, mova para Perdido." % (it.get("etapa") or "?", quem, venc))
        t = {"title": titulo, "description": desc, "priority": "alta", "dueTime": "12:00", "date": dia,
             "createdBy": "jarbas-idm", "origem": "cadencia", "fone": it["fone"], "tipo_cadencia": it["tipo"]}
        if it.get("uid"):
            t["targetUserId"] = it["uid"]
        else:
            t["targetRole"] = "comercial"
        b.set(db.collection("tasks").document(tid), t)
        n += 1
        if n % 400 == 0:
            b.commit(); b = db.batch()
    b.commit()
    # tarefas de cadencia de HOJE que nao valem mais pela regra nova: remove (so as que ninguem concluiu)
    try:
        feitas = {c.to_dict().get("taskId") for c in db.collection("task_completions").where("date", "==", dia).stream()}
        for d in db.collection("tasks").where("date", "==", dia).stream():
            if d.id.startswith("cad_%s_" % dia) and d.id not in novos_ids and d.id not in feitas:
                d.reference.delete()
    except Exception as e:
        print("[cadencia] limpeza erro:", repr(e)[:120], flush=True)
    por_dono = defaultdict(int)
    for it in itens:
        por_dono[it.get("dono") or "sem dono"] += 1
    db.collection(COLL).document(dia).set({"dia": dia, "itens": itens, "por_dono": dict(por_dono), "regra": "v2-30/10 proximo passo",
                                           "gerado_em": _sp_now().isoformat(timespec="seconds")})
    return itens

def rodar():
    db = _fw._get_db()
    if not db:
        ST.update({"ok": False, "erro": "sem banco"}); return ST
    hoje = _sp_now().date().isoformat()
    ontem = (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    try:
        conf = conferir(db, ontem)
        itens = gerar(db, hoje)
        ST.update({"ultima": _sp_now().isoformat(timespec="seconds"), "ok": True, "erro": None, "geradas": len(itens),
                   "conferidas": (conf or {}).get("total", 0), "resolvidas": (conf or {}).get("resolvidos", 0)})
        print("[cadencia] %s geradas=%d conferidas=%s" % (hoje, len(itens), conf), flush=True)
    except Exception as e:
        import traceback
        ST.update({"ok": False, "erro": repr(e)[:200]})
        print("[cadencia] erro:", traceback.format_exc(), flush=True)
    return dict(ST)

def texto_atendente(nome, itens, conf_dela=None):
    L = ["*Bom dia, %s! ☀️ Cadência do dia · IDM*" % nome.split()[0], ""]
    if conf_dela and conf_dela.get("total"):
        L.append("Ontem: %d de %d resolvidos." % (conf_dela["resolvidos"], conf_dela["total"]))
        L.append("")
    pp = [i for i in itens if i.get("tipo") not in ("atualizar_etapa", "lancar_agendamento")]
    et = [i for i in itens if i.get("tipo") == "atualizar_etapa"]
    la = [i for i in itens if i.get("tipo") == "lancar_agendamento"]
    if la:
        L.append("📅 *Etapa Agendamento sem horário na agenda* (%d):" % len(la))
        for it in la[:10]:
            L.append("• %s" % it["nome"])
        L.append("→ Se agendou, lance o horário na agenda. Se não, volte a etapa e crie a próxima tarefa com data.")
        L.append("")
    if pp:
        L.append("📌 Leads *sem próximo passo* no CRM (%d):" % len(pp))
        for it in pp[:20]:
            extra = "esperando você" if it.get("esperando") == "equipe" else (
                "tarefa venceu %s" % _dm(it.get("pp_quando")) if it.get("pp_tipo") == "vencida" else "%dd sem conversa" % it["dias_parado"])
            L.append("• %s, %s (%s)" % (it["nome"], it.get("etapa") or "?", extra))
        if len(pp) > 20:
            L.append("… e mais %d no IDM (Minhas Tarefas)." % (len(pp) - 20))
        L.append("→ Para cada um: crie uma *tarefa com data* (ligar, nova abordagem…) ou faça o agendamento. Desistiu? Mova para Perdido.")
        L.append("")
    if et:
        L.append("🔄 *Já agendados* com etapa desatualizada no CRM (%d):" % len(et))
        for it in et[:10]:
            L.append("• %s: agendado %s, etapa %s" % (it["nome"], _dm(it.get("pp_quando")), it.get("etapa") or "?"))
        L.append("→ Mude a etapa para Agendamento.")
        L.append("")
    L.append("_Responda *ok* para manter este canal aberto._")
    return "\n".join(L)

def enviar_atendentes():
    import diario as _d
    db = _fw._get_db()
    hoje = _sp_now().date().isoformat()
    ontem = (_sp_now().date() - dt.timedelta(days=1)).isoformat()
    s = db.collection(COLL).document(hoje).get()
    if not s.exists:
        return {}
    itens = (s.to_dict() or {}).get("itens") or []
    so = db.collection(COLL).document(ontem).get()
    conf = ((so.to_dict() or {}).get("conferencia") or {}).get("por_dono", {}) if so.exists else {}
    por = defaultdict(list)
    for it in itens:
        if it.get("dono"):
            por[it["dono"]].append(it)
    res = {}
    for dono, lst in por.items():
        fone = FONES.get(_key(dono))
        if not fone:
            res[dono] = {"ok": False, "erro": "sem telefone cadastrado"}
            continue
        res[dono] = _d.enviar(texto_atendente(dono, lst, conf.get(dono)), fone)
    ST["envios"] = {k: {"ok": v.get("ok"), "erro": v.get("erro")} for k, v in res.items()}
    db.collection(COLL).document(hoje).set({"envios": ST["envios"]}, merge=True)
    return res

def iniciar(sched):
    if sched is None or os.environ.get("CADENCIA_ON", "1") == "0":
        return
    sched.add_job(rodar, "cron", hour=6, minute=35)
    sched.add_job(enviar_atendentes, "cron", hour=7, minute=0)
    print("[cadencia] agendada 06h35 (gera/confere) + 07h00 (WhatsApp das atendentes)", flush=True)

# -*- coding: utf-8 -*-
"""
Jarbas — analise por lead (sob demanda).
Le a conversa real do CliniChat, aplica o cerebro v1 (metodo do Instituto)
e devolve 3 blocos: diagnostico, conducao, mensagem.
Cache no Firestore por conversa + ultima mensagem (nao reprocessa sem conversa nova).
Modelo via env JARBAS_MODEL (default Sonnet). Chave via ANTHROPIC_API_KEY.
"""
import os, json, time
import motor_orfaos as _orf   # reusa _cc() e CLINI (leitura autenticada do CliniChat)
import firebase_writer as _fw # reusa _get_db() (Firestore)

_MODEL = os.environ.get("JARBAS_MODEL", "claude-sonnet-5-5")
_MAXTOK = int(os.environ.get("JARBAS_MAXTOK", "1200"))
_CEREBRO_PATH = os.environ.get("JARBAS_CEREBRO", "jarbas_cerebro_v1.md")
_CACHE_COLL = os.environ.get("JARBAS_CACHE_COLL", "jarbas_analises")

_cerebro_cache = None
def _cerebro():
    global _cerebro_cache
    if _cerebro_cache is None:
        try:
            with open(_CEREBRO_PATH, encoding="utf-8") as fh:
                _cerebro_cache = fh.read()
        except Exception:
            _cerebro_cache = ""
    return _cerebro_cache

def _transcript(conv_id, max_msgs=40):
    data, err = _orf._cc("/conversation/messages/%s?from=0&to=%d&orderBy=createdAt%%3AASC&filters=%%7B%%7D" % (conv_id, max_msgs))
    if err or not data:
        return [], (err or "sem_dados")
    items = data.get("conversationItems") or []
    items.sort(key=lambda m: m.get("date") or "")
    linhas, last_date = [], ""
    _marca = {"audio": "[audio]", "ptt": "[audio]", "voice": "[audio]",
              "image": "[imagem]", "photo": "[imagem]", "video": "[video]",
              "document": "[arquivo]", "file": "[arquivo]", "sticker": "[figurinha]",
              "location": "[localizacao]", "contact": "[contato]"}
    for m in items:
        content = (m.get("content") or "").strip()
        atc = (m.get("audioTextContent") or "").strip()
        typ = (m.get("type") or "").lower()
        if content:
            t = content
        elif atc:
            t = "[audio transcrito] " + atc
        elif typ in _marca:
            t = _marca[typ]
        elif typ and typ not in ("text", "chat"):
            t = "[%s]" % typ
        else:
            continue
        who = "Cliente" if m.get("senderType") == "contacts" else "Atendente"
        linhas.append("%s: %s" % (who, t))
        last_date = m.get("date") or last_date
    return linhas, last_date

def _cache_get(conv_id):
    db = _fw._get_db()
    if db is None:
        return None
    try:
        d = db.collection(_CACHE_COLL).document(conv_id).get()
        if d.exists:
            return d.to_dict()
    except Exception:
        pass
    return None

def _cache_set(conv_id, payload):
    db = _fw._get_db()
    if db is None:
        return
    try:
        db.collection(_CACHE_COLL).document(conv_id).set(payload)
    except Exception:
        pass

def _chamar_modelo(system, user):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=_MODEL, max_tokens=_MAXTOK,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content)

def _parse_json(txt):
    s = (txt or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def _struct_call(model, system, user, schema, toolname, max_tokens, timeout=45):
    import anthropic
    cli = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=1, timeout=timeout)
    base = dict(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[{"name": toolname, "description": "Registra o resultado no formato exigido. Use SEMPRE esta ferramenta.",
                "input_schema": schema}],
        messages=[{"role": "user", "content": user}],
    )
    try:
        msg = cli.messages.create(tool_choice={"type": "tool", "name": toolname}, **base)
    except Exception as e:
        if "tool_choice" in str(e):
            msg = cli.messages.create(tool_choice={"type": "auto"}, **base)
        else:
            raise
    for b in msg.content:
        if getattr(b, "type", None) == "tool_use" and isinstance(getattr(b, "input", None), dict):
            return dict(b.input)
    txt = "".join(getattr(b, "text", "") for b in msg.content)
    return _parse_json(txt)
def analisar(conv_id, meta=None):
    meta = meta or {}
    linhas, last_date = _transcript(conv_id)
    if not linhas:
        return {"ok": False, "motivo": "sem conversa legivel", "conv_id": conv_id}
    cached = _cache_get(conv_id)
    if cached and cached.get("last_date") == last_date and cached.get("analise"):
        return {"ok": True, "cache": True, "conv_id": conv_id, **cached["analise"]}
    transcript = "\n".join(linhas[-60:])
    ctx = []
    if meta.get("nome"): ctx.append("Nome: " + str(meta["nome"]))
    if meta.get("temperatura"): ctx.append("Temperatura (do termometro): " + str(meta["temperatura"]))
    if meta.get("origem"): ctx.append("Origem: " + str(meta["origem"]))
    if meta.get("identidade"): ctx.append("Identidade (sinal de apoio): " + str(meta["identidade"]))
    _ce = meta.get("ce") or {}
    if _ce.get("oportunidades"):
        _ops = "; ".join(("valor %s | %s | %s" % (o.get("amount"), o.get("pipeline") or "-", o.get("stage") or "-")) for o in _ce["oportunidades"][:4])
        ctx.append("Oportunidades no CRM do CE: " + _ops)
    if _ce.get("tags"): ctx.append("Tags no CE: " + str(_ce["tags"]))
    if _ce.get("annotation"): ctx.append("Anotacao no CE: " + str(_ce["annotation"]))
    system = (_cerebro() +
              "\n\n---\nVoce e o Jarbas. Responda com diagnostico, conducao e mensagem. "
              "'diagnostico' = 1-2 frases. 'conducao' = a jogada concreta agora. "
              "'mensagem' = mensagem pronta pra enviar no WhatsApp, no tom do Instituto.\n"
              "REGRAS DE SAIDA (importante):\n"
              "- NUNCA cite o nome da persona (Mariana/Renata/Patricia) na resposta; use o perfil so "
              "internamente pra calibrar o tom.\n"
              "- O Instituto faz o CUIDADO (pre e pos-operatorio, estetica); NAO realiza a cirurgia. "
              "A cirurgia e feita pelo CIRURGIAO (medico externo). Se o texto disser 'cirurgia com o Dr. X', "
              "entenda que o Dr. X e quem OPERA; o pre/pos-op e com o Instituto. Nao confunda os dois.\n"
              "- Se a pessoa NAO for cliente com pacote (identidade 'novo' ou 'lead no CE') OU tiver oportunidade "
              "EM ABERTO no CRM, aponte no diagnostico como OPORTUNIDADE DE VENDA a atacar (orcamento/oportunidade "
              "aberta sem fechamento). Use os dados do CE acima, nao invente numeros.")
    user = ("Contexto do lead:\n" + ("\n".join(ctx) or "(sem metadados)") +
            "\n\nConversa real (ordem cronologica):\n" + transcript)
    _schema_an = {"type": "object", "properties": {
        "diagnostico": {"type": "string"},
        "conducao": {"type": "string"},
        "mensagem": {"type": "string"}},
        "required": ["diagnostico", "conducao", "mensagem"]}
    try:
        parsed = _struct_call(_MODEL, system, user, _schema_an, "analise", 1400)
    except KeyError:
        return {"ok": False, "motivo": "ANTHROPIC_API_KEY ausente no Render", "conv_id": conv_id}
    except Exception as e:
        return {"ok": False, "motivo": "falha modelo: " + str(e)[:140], "conv_id": conv_id}
    analise = {k: str(parsed.get(k, "")).strip() for k in ("diagnostico", "conducao", "mensagem")}
    _cache_set(conv_id, {"analise": analise, "last_date": last_date,
                         "gerado_em": int(time.time()), "modelo": _MODEL})
    return {"ok": True, "cache": False, "conv_id": conv_id, **analise}


_TEMP_SYS = (
    "Voce e um vendedor experiente do Instituto Mafra (clinica de pos-operatorio e estetica). "
    "Leia a conversa (Cliente = o lead; Atendente = a clinica) e julgue a TEMPERATURA do lead como um vendedor faria, "
    "lendo a intencao real, nao so palavras-chave:\n"
    "- quente: demonstra prontidao (pede preco/valor/agendar/marcar/horario, diz quero/como faco/fechar) OU a ultima "
    "mensagem e do cliente aguardando resposta com interesse real.\n"
    "- morno: tem interesse mas sem urgencia nem data; esfriou ou aguarda sem sinal forte.\n"
    "- frio: sem engajamento real, parado ou sumiu.\n"
    "Responda SOMENTE um JSON: {\"temperatura\":\"quente|morno|frio\",\"motivo\":\"frase curta\"}."
)

def classifica_temp(conv_id, modelo):
    linhas, _ld = _transcript(conv_id, max_msgs=25)
    if not linhas:
        return {"temperatura": "frio", "motivo": "sem conversa legivel"}
    user = "Conversa (Cliente = o lead):\n" + "\n".join(linhas[-40:])
    try:
        import anthropic
        cli = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=1, timeout=30)
        msg = cli.messages.create(model=modelo, max_tokens=400, system=_TEMP_SYS,
                                  messages=[{"role": "user", "content": user}])
        txt = "".join(getattr(b, "text", "") for b in msg.content)
        return _parse_json(txt)
    except Exception as e:
        return {"temperatura": "erro", "motivo": str(e)[:120]}


_TEMP_SYS2 = (
    "Voce e um vendedor experiente do Instituto Mafra (clinica de pos-operatorio e estetica).\n"
    "Vou te dar a CONVERSA (Cliente = a pessoa; Atendente = a clinica) e uma DICA de identidade da pessoa.\n"
    "Julgue, lendo a INTENCAO real (nao palavras soltas), em qual balde a conversa cai:\n"
    "- quente: prospect pronto pra avancar/comprar (pede preco/valor, quer agendar Avaliacao, 'quero', 'como faco', decisao clara).\n"
    "- morno: prospect com interesse mas sem urgencia nem data; 'vou ver', 'depois'.\n"
    "- frio: prospect sem engajamento real, parado ou sumiu.\n"
    "- cross_sell: a pessoa JA E CLIENTE e demonstra querer COMPRAR algo novo que ainda nao tem (servico/procedimento diferente do que ja faz).\n"
    "- operacional: a pessoa JA E CLIENTE tratando de LOGISTICA/administrativo do que ja tem (agendar/confirmar/remarcar sessao de pacote existente, duvida operacional). ISSO NAO E VENDA.\n"
    "Regras de decisao:\n"
    "- A CONVERSA manda sempre. A dica so ajuda.\n"
    "- Dica 'cliente' + conversa so logistica -> operacional.\n"
    "- Dica 'cliente' + quer comprar coisa nova -> cross_sell.\n"
    "- Dica 'novo' ou 'lead' -> quente/morno/frio pela intencao.\n"
    "- Mesmo sem a dica, se a conversa e so logistica de paciente (confirmar horario de sessao) -> operacional; se e intencao de compra -> quente/morno/frio ou cross_sell.\n"
    "Responda SOMENTE um JSON: {\"temperatura\":\"quente|morno|frio|cross_sell|operacional\",\"motivo\":\"frase curta\"}."
)

def classifica_lead(conv_id, modelo, identidade="novo"):
    linhas, _ld = _transcript(conv_id, max_msgs=25)
    if not linhas:
        return {"temperatura": "frio", "motivo": "sem conversa legivel"}
    user = ("Dica de identidade da pessoa: " + str(identidade) +
            "\n\nConversa (Cliente = a pessoa):\n" + "\n".join(linhas[-40:]))
    _schema_cl = {"type": "object", "properties": {
        "temperatura": {"type": "string",
                        "enum": ["quente", "morno", "frio", "cross_sell", "operacional"]},
        "motivo": {"type": "string", "description": "frase curta, ate 14 palavras"}},
        "required": ["temperatura", "motivo"]}
    try:
        return _struct_call(modelo, _TEMP_SYS2, user, _schema_cl, "classificar", 300)
    except Exception as e:
        return {"temperatura": "revisar", "motivo": ("falha: " + str(e)[:80])}

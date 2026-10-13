# -*- coding: utf-8 -*-
"""
IDM Coletor — Fase 1 (go/no-go).
Navegador real headless (Playwright) que: carrega o app do CE, detecta desafio
Cloudflare, faz login com CE_EMAIL/CE_SENHA e tenta alcançar o clinichat
(lê o token da sessão DENTRO do próprio navegador do bot e chama a API interna).
Retorna só booleans/status/contagens — NUNCA token, senha, nome ou conteúdo.
"""
import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="IDM Coletor", version="0.1")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

# JS que roda DENTRO do navegador do bot: acha o JWT do clinichat na memória do
# app (Vue store) e chama a API interna. Devolve só {jwt:bool,status,total}.
_JS_CLINICHAT = r"""
async () => {
  let jwt = null;
  const looksJwt = s => typeof s === 'string' && s.startsWith('eyJ') && s.length > 120;
  // 1) varre a store do Vue (padrao Vue2) por uma chave de token
  try {
    const el = document.querySelector('#app');
    const root = el && (el.__vue__ || (el.__vue_app__ && el.__vue_app__._instance));
    const state = root && root.$store && root.$store.state;
    const seen = new Set();
    const walk = (o, d) => {
      if (!o || d > 7 || seen.has(o) || jwt) return;
      seen.add(o);
      for (const k in o) {
        try {
          const v = o[k];
          if (looksJwt(v) && /jwt|token|auth/i.test(k)) { jwt = v; return; }
          else if (v && typeof v === 'object') walk(v, d + 1);
        } catch (e) {}
        if (jwt) return;
      }
    };
    if (state) walk(state, 0);
  } catch (e) {}
  // 2) fallback: varre localStorage/sessionStorage por qualquer eyJ com aud clinichat
  if (!jwt) {
    try {
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i++) {
          const val = store.getItem(store.key(i)) || '';
          const m = val.match(/eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+/g) || [];
          for (const t of m) {
            try {
              const p = JSON.parse(atob(t.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
              if (p && (p.aud === 'clinichat' || p.clinic_id)) { jwt = t; break; }
            } catch (e) {}
          }
          if (jwt) break;
        }
        if (jwt) break;
      }
    } catch (e) {}
  }
  if (!jwt) return { jwt: false };
  try {
    const r = await fetch('https://clinichat.clinicaexperts.com.br/contact-channel?from=0&to=1&orderBy=lastMessageTime%3ADESC&filters=%7B%7D',
      { headers: { Authorization: 'Bearer ' + jwt } });
    if (!r.ok) return { jwt: true, status: r.status };
    const j = await r.json();
    return { jwt: true, status: 200, total: (j.pagination && j.pagination.total) || null };
  } catch (e) { return { jwt: true, erro_fetch: String(e).slice(0, 100) }; }
}
"""


@app.get("/health")
def health():
    return {"status": "up", "service": "idm-coletor"}


@app.get("/diag_browser")
def diag_browser():
    from playwright.sync_api import sync_playwright
    out = {"etapas": {}}
    email = os.environ.get("CE_EMAIL")
    senha = os.environ.get("CE_SENHA")
    out["tem_credenciais"] = bool(email and senha)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=_UA, locale="pt-BR",
                viewport={"width": 1366, "height": 900})
            pg = ctx.new_page()
            # 1) carregar o app
            try:
                pg.goto("https://app.clinicaexperts.com.br/",
                        wait_until="networkidle", timeout=60000)
            except Exception:
                pg.wait_for_timeout(2000)
            pg.wait_for_timeout(2500)
            html = (pg.content() or "").lower()
            cf = any(m in html for m in ["just a moment", "checking your browser",
                     "cf-challenge", "attention required", "error code: 1010"])
            out["etapas"]["carregou_app"] = {
                "url": (pg.url or "")[:120], "cf_challenge": cf,
                "inputs_email": pg.locator("input[type='email'], input[name='email']").count(),
                "inputs_senha": pg.locator("input[type='password']").count(),
            }
            if cf or not (email and senha):
                browser.close()
                return JSONResponse(out)
            # 2) login
            try:
                pg.locator("input[type='email'], input[name='email']").first.fill(email)
                pg.locator("input[type='password']").first.fill(senha)
                pg.locator("button[type='submit'], button:has-text('Entrar'), button:has-text('Acessar'), button:has-text('Login')").first.click()
                pg.wait_for_timeout(7000)
                url_pos = (pg.url or "")
                out["etapas"]["login"] = {
                    "url": url_pos[:120],
                    "logado": ("/login" not in url_pos.lower()),
                }
                if "/login" in url_pos.lower():
                    browser.close()
                    return JSONResponse(out)
            except Exception as e:
                out["etapas"]["login_err"] = str(e)[:160]
                browser.close()
                return JSONResponse(out)
            # 3) alcançar o clinichat
            try:
                pg.goto("https://app.clinicaexperts.com.br/comunicacao/canais-de-comunicacao",
                        wait_until="networkidle", timeout=60000)
                pg.wait_for_timeout(3500)
                out["etapas"]["clinichat"] = pg.evaluate(_JS_CLINICHAT)
            except Exception as e:
                out["etapas"]["clinichat_err"] = str(e)[:160]
            browser.close()
    except Exception as e:
        out["erro_geral"] = str(e)[:200]
    return JSONResponse(out)

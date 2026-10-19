# -*- coding: utf-8 -*-
"""
IDM Coletor — Fase 1 (go/no-go) v3.
Distingue "fill nao registrou" de "trava anti-robo no envio".
Mudancas v3: digita com TECLADO REAL (type) em vez de fill; detecta captcha/
Turnstile; captura TODAS as requisicoes POST (url->status) p/ ver se o login
dispara; captura um trecho do texto da tela depois do envio.
Retorna so booleans/status/contagens/trechos — NUNCA token, senha, nome ou PII.
"""
import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="IDM Coletor", version="0.3")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

_JS_CLINICHAT = r"""
async () => {
  let jwt = null;
  const looksJwt = s => typeof s === 'string' && s.startsWith('eyJ') && s.length > 120;
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

_SEL_CAPTCHA = ("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile'], "
                ".cf-turnstile, [class*='turnstile'], [class*='captcha'], "
                "iframe[src*='recaptcha'], .g-recaptcha, iframe[src*='hcaptcha']")


@app.get("/health")
def health():
    return {"status": "up", "service": "idm-coletor", "version": "0.3"}


@app.get("/diag_browser")
def diag_browser():
    from playwright.sync_api import sync_playwright
    out = {"etapas": {}}
    email = os.environ.get("CE_EMAIL")
    senha = os.environ.get("CE_SENHA")
    out["tem_credenciais"] = bool(email and senha)
    if senha:
        out["senha_len"] = len(senha)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(user_agent=_UA, locale="pt-BR",
                viewport={"width": 1366, "height": 900})
            pg = ctx.new_page()

            # captura TODAS as respostas de POST (url -> status)
            posts = {}
            def _on_resp(resp):
                try:
                    if resp.request.method == "POST":
                        posts[resp.url[:110]] = resp.status
                except Exception:
                    pass
            pg.on("response", _on_resp)

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
                "captcha_na_tela": pg.locator(_SEL_CAPTCHA).count(),
            }
            if cf or not (email and senha):
                browser.close()
                return JSONResponse(out)

            # 2) login com TECLADO REAL + diagnostico
            try:
                email_in = pg.locator("input[type='email'], input[name='email']").first
                senha_in = pg.locator("input[type='password']").first
                email_in.click()
                email_in.fill("")
                pg.keyboard.type(email, delay=25)
                senha_in.click()
                senha_in.fill("")
                pg.keyboard.type(senha, delay=25)
                pg.wait_for_timeout(300)
                # botao de envio: prioriza type=submit, senao texto
                btn = pg.locator("button[type='submit']")
                if btn.count() == 0:
                    btn = pg.locator("button:has-text('Entrar'), button:has-text('Acessar')")
                n_btn = btn.count()
                clicou = False
                try:
                    if n_btn > 0:
                        btn.first.click(timeout=5000)
                        clicou = True
                    else:
                        senha_in.press("Enter")
                except Exception:
                    try:
                        senha_in.press("Enter")
                    except Exception:
                        pass
                # esperar sair da tela de login
                try:
                    pg.wait_for_url(
                        lambda u: ("signin" not in u.lower() and "login" not in u.lower()),
                        timeout=12000)
                except Exception:
                    pass
                pg.wait_for_timeout(2500)
                url_pos = (pg.url or "")
                inputs_apos = pg.locator("input[type='password']").count()
                captcha_apos = pg.locator(_SEL_CAPTCHA).count()
                erro = None
                for sel in ["[role='alert']", ".v-messages__message", ".invalid-feedback",
                            ".text-danger", ".error", ".el-message", ".Toastify__toast",
                            ".toast", "[class*='error']"]:
                    try:
                        l = pg.locator(sel)
                        if l.count() > 0:
                            t = (l.first.inner_text() or "").strip()
                            if t:
                                erro = t[:160]
                                break
                    except Exception:
                        pass
                try:
                    corpo = (pg.inner_text("body") or "").replace("\n", " ").strip()[:280]
                except Exception:
                    corpo = None
                logado = ("signin" not in url_pos.lower()
                          and "login" not in url_pos.lower()
                          and inputs_apos == 0)
                out["etapas"]["login"] = {
                    "url": url_pos[:120],
                    "logado": logado,
                    "botoes_submit": n_btn,
                    "clicou_botao": clicou,
                    "inputs_senha_apos": inputs_apos,
                    "captcha_apos": captcha_apos,
                    "erro_tela": erro,
                    "posts": dict(list(posts.items())[:14]),
                    "corpo_trecho": corpo,
                }
                if not logado:
                    browser.close()
                    return JSONResponse(out)
            except Exception as e:
                out["etapas"]["login_err"] = str(e)[:160]
                out["etapas"]["posts"] = dict(list(posts.items())[:14])
                browser.close()
                return JSONResponse(out)

            # 3) alcancar o clinichat
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

"""
Serviço de email via Resend — CaseOS.
Templates dark com identidade visual da plataforma: pixel chars, verde ácido #C8FF00.
"""
import asyncio
import httpx
import logging
from app.config import settings

_BASE    = "https://api.resend.com"
_ASSETS  = "https://caseos.voandonaia.com/assets"
_LOGO    = f"{_ASSETS}/logo-caseos-white.png"
_ACID    = "#C8FF00"
_BG      = "#080D18"
_CARD    = "#111827"
_TEXT    = "#E8EDF5"
_MUTED   = "#8B96A8"
_DIM     = "#4A5568"
_BTN_TXT = "#0A0A0B"

log = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }


async def _send(payload: dict, _tentativas: int = 3) -> bool:
    """
    Envia o e-mail via Resend com até `_tentativas` tentativas.
    Retenta apenas em erros transitórios (5xx, timeout de rede).
    Erros de cliente (4xx) não são retentados.
    """
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY não configurado — e-mail não enviado para %s", payload.get("to"))
        return False

    for tentativa in range(1, _tentativas + 1):
        async with httpx.AsyncClient() as c:
            try:
                log.info(
                    "Enviando e-mail via Resend (tentativa %d/%d): from=%s to=%s subject=%s",
                    tentativa, _tentativas,
                    payload.get("from"), payload.get("to"), payload.get("subject"),
                )
                r = await c.post(f"{_BASE}/emails", json=payload, headers=_headers(), timeout=15)

                if r.status_code in (200, 201):
                    log.info("E-mail enviado com sucesso: %s", r.json())
                    return True

                # Erros de cliente (4xx) — não adianta retentar
                if 400 <= r.status_code < 500:
                    log.error("Resend erro de cliente %s: %s", r.status_code, r.text)
                    return False

                # Erros de servidor (5xx) — retenta com backoff
                log.warning("Resend erro de servidor %s (tentativa %d): %s",
                            r.status_code, tentativa, r.text[:200])

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                log.warning("Resend timeout/rede (tentativa %d): %s", tentativa, exc)
            except Exception as exc:
                log.error("Resend exceção inesperada: %s", exc)
                return False   # exceção não-transitória — não retenta

        if tentativa < _tentativas:
            await asyncio.sleep(2 ** (tentativa - 1))   # backoff: 1s, 2s

    log.error("Resend: todas %d tentativas falharam para %s", _tentativas, payload.get("to"))
    return False


def _base(*, char_url: str, headline: str, body_html: str, cta_url: str,
          cta_label: str, extra_html: str = "", notice_html: str = "") -> str:
    """Template base com dark theme, logo, pixel char e botão verde."""
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{_BG};font-family:'Helvetica Neue',Arial,sans-serif;-webkit-font-smoothing:antialiased">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{_BG};padding:32px 16px">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" border="0" style="max-width:580px;width:100%">

  <!-- LOGO -->
  <tr>
    <td style="padding:0 0 28px 4px">
      <img src="{_LOGO}" alt="CaseOS" width="93" height="28"
           style="width:93px;height:28px;display:block;border:0;outline:none">
    </td>
  </tr>

  <!-- CARD PRINCIPAL -->
  <tr>
    <td style="background:{_CARD};border-radius:12px;border:1px solid rgba(200,255,0,0.12);overflow:hidden;mso-border-radius:12px">

      <!-- BARRA VERDE TOPO -->
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="height:3px;background:{_ACID};font-size:0;line-height:0">&nbsp;</td>
      </tr></table>

      <!-- HERO: texto + pixel char -->
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:36px 36px 24px 36px;vertical-align:middle;width:55%">
            <p style="font-family:monospace;font-size:9px;letter-spacing:.22em;text-transform:uppercase;
                      color:{_ACID};margin:0 0 12px 0">CaseOS</p>
            <p style="font-size:22px;font-weight:700;color:{_TEXT};margin:0 0 14px 0;line-height:1.3">
              {headline}
            </p>
            {body_html}
            {notice_html}
          </td>
          <td style="padding:0 24px 0 0;vertical-align:bottom;text-align:right;width:45%">
            <img src="{char_url}" width="170" alt=""
                 style="max-width:170px;display:block;margin-left:auto">
          </td>
        </tr>
      </table>

      {extra_html}

      <!-- BOTÃO CTA -->
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 36px 36px 36px">
            <a href="{cta_url}"
               style="display:block;background:{_ACID};color:{_BTN_TXT};
                      font-family:monospace;font-size:11px;letter-spacing:.2em;
                      text-transform:uppercase;text-decoration:none;text-align:center;
                      padding:17px 24px;border-radius:5px;font-weight:700;
                      mso-padding-alt:17px 24px">
              {cta_label} &rarr;
            </a>
          </td>
        </tr>
      </table>

    </td>
  </tr>

  <!-- DIVISOR -->
  <tr><td style="height:1px;background:rgba(255,255,255,0.05);font-size:0;line-height:0;margin:20px 0">&nbsp;</td></tr>

  <!-- RODAPÉ -->
  <tr>
    <td style="padding:16px 4px 8px 4px;text-align:center">
      <p style="font-size:10px;color:{_DIM};margin:0 0 4px 0;font-family:monospace;letter-spacing:.08em">
        CaseOS · IA para profissionais de saúde
      </p>
      <p style="font-size:10px;color:{_DIM};margin:0">
        <a href="{settings.frontend_url}" style="color:{_DIM};text-decoration:none">
          caseos.voandonaia.com
        </a>
        &nbsp;·&nbsp;
        <a href="mailto:contato@voandonaia.com" style="color:{_DIM};text-decoration:none">
          Voando na IA &middot; Contato
        </a>
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body></html>"""


# ── Verificação de e-mail ────────────────────────────────────────────────────

async def enviar_verificacao_email(destinatario: str, nome: str, token: str) -> bool:
    if not settings.resend_api_key:
        return False

    from app.services.email import _backend_url
    url = f"{_backend_url()}/auth/verify?token={token}"

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-lupa.png",
        headline=f"Confirme seu<br>e-mail, {nome.split()[0]}.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Clique no botão abaixo para ativar sua conta no CaseOS.
            Link válido por <strong style="color:{_TEXT}">24 horas</strong>.
          </p>""",
        cta_url=url,
        cta_label="Verificar e-mail",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Confirme seu e-mail para começar",
        "html": html,
    })


def _backend_url() -> str:
    """Retorna a URL pública do backend. Usa BACKEND_URL (explícita) ou deriva de GOOGLE_REDIRECT_URI."""
    if settings.backend_url and settings.backend_url != "http://localhost:8000":
        return settings.backend_url.rstrip("/")
    # Fallback para dev: extrai de google_redirect_uri
    uri = settings.google_redirect_uri
    return uri.split("/auth/")[0] if "/auth/" in uri else "http://localhost:8000"


# ── Boas-vindas (novo usuário) ───────────────────────────────────────────────

async def enviar_boas_vindas(destinatario: str, nome: str, via_google: bool = False) -> bool:
    if not settings.resend_api_key:
        return False

    primeiro = nome.split()[0]
    char = f"{_ASSETS}/pixel-astronaut.png" if via_google else f"{_ASSETS}/pixel-researcher.png"

    metodo_html = f"""
      <div style="background:#0D1425;border-left:3px solid {_ACID};border-radius:0 4px 4px 0;
                  padding:12px 16px;margin:16px 0">
        <p style="font-size:11px;color:{_MUTED};margin:0;font-family:monospace;letter-spacing:.05em">
          Sua conta usa <strong style="color:{_TEXT}">Login com Google</strong>.
          Não é preciso senha — clique em "Entrar com Google" sempre que acessar.
        </p>
      </div>""" if via_google else ""

    features = f"""
      <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 4px 0">
        <tr>
          <td style="padding:24px 36px 8px 36px">
            <p style="font-family:monospace;font-size:9px;letter-spacing:.15em;
                      text-transform:uppercase;color:{_DIM};margin:0 0 12px 0">
              O QUE VOCÊ PODE FAZER
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 36px 24px 36px">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="33%" style="text-align:center;padding:16px 8px;background:#0D1425;
                    border-radius:6px;border-top:2px solid rgba(200,255,0,0.3)">
                  <p style="font-size:18px;margin:0 0 6px 0">🔬</p>
                  <p style="font-size:11px;color:{_MUTED};margin:0;line-height:1.5">
                    Relatos CARE<br>estruturados
                  </p>
                </td>
                <td width="4%" style="font-size:0">&nbsp;</td>
                <td width="33%" style="text-align:center;padding:16px 8px;background:#0D1425;
                    border-radius:6px;border-top:2px solid rgba(200,255,0,0.3)">
                  <p style="font-size:18px;margin:0 0 6px 0">📋</p>
                  <p style="font-size:11px;color:{_MUTED};margin:0;line-height:1.5">
                    Checklist<br>automático
                  </p>
                </td>
                <td width="4%" style="font-size:0">&nbsp;</td>
                <td width="33%" style="text-align:center;padding:16px 8px;background:#0D1425;
                    border-radius:6px;border-top:2px solid rgba(200,255,0,0.3)">
                  <p style="font-size:18px;margin:0 0 6px 0">📄</p>
                  <p style="font-size:11px;color:{_MUTED};margin:0;line-height:1.5">
                    Export DOCX<br>pronto
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>"""

    html = _base(
        char_url=char,
        headline=f"Bem-vindo ao<br>CaseOS, {primeiro}.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Sua conta está pronta. Plano gratuito inclui
            <strong style="color:{_TEXT}">1 relato por mês</strong>.
          </p>
          {metodo_html}""",
        cta_url=f"{settings.frontend_url}/dashboard.html",
        cta_label="Acessar o CaseOS",
        extra_html=features,
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"Bem-vindo ao CaseOS, {primeiro}! Sua conta está pronta",
        "html": html,
    })


# ── Aviso: login é via Google ────────────────────────────────────────────────

async def enviar_aviso_login_google(destinatario: str, nome: str) -> bool:
    if not settings.resend_api_key:
        return False

    primeiro = nome.split()[0]

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-lupa.png",
        headline=f"Sua conta usa<br>Login com Google.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0 0 14px 0;line-height:1.7">
            Olá, {primeiro}. Recebemos uma solicitação de redefinição de senha,
            mas sua conta no CaseOS foi criada via Google —
            <strong style="color:{_TEXT}">não há senha para redefinir</strong>.
          </p>""",
        cta_url=f"{settings.frontend_url}/login.html",
        cta_label="Entrar com Google",
        notice_html=f"""
          <div style="background:#0D1425;border-left:3px solid {_ACID};border-radius:0 4px 4px 0;
                      padding:12px 16px;margin:16px 0 0 0">
            <p style="font-size:11px;color:{_MUTED};margin:0;font-family:monospace;letter-spacing:.04em">
              Na tela de login, clique em
              <strong style="color:{_TEXT}">"Entrar com Google"</strong>
              para acessar sua conta normalmente.
            </p>
          </div>""",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Sua conta usa Login com Google",
        "html": html,
    })


# ── Redefinição de senha ─────────────────────────────────────────────────────

async def enviar_reset_senha(destinatario: str, nome: str, token: str) -> bool:
    if not settings.resend_api_key:
        return False

    url = f"{settings.frontend_url}/login.html?reset={token}"

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-lupa.png",
        headline="Redefinir sua<br>senha.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Olá, {nome.split()[0]}. Clique no botão abaixo para criar uma nova senha.
            Link válido por <strong style="color:{_TEXT}">1 hora</strong>.
          </p>""",
        cta_url=url,
        cta_label="Criar nova senha",
        notice_html=f"""
          <p style="font-size:11px;color:{_DIM};margin:14px 0 0 0;font-family:monospace">
            Se não foi você, ignore este e-mail. Sua senha não será alterada.
          </p>""",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Redefinição de senha",
        "html": html,
    })


# ── Boas-vindas Pro ──────────────────────────────────────────────────────────

async def enviar_boas_vindas_pro(destinatario: str, nome: str) -> bool:
    if not settings.resend_api_key:
        return False

    primeiro = nome.split()[0]

    extras = f"""
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 36px 24px 36px">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="48%" style="background:#0D1425;border-radius:6px;padding:16px;
                    border-top:2px solid {_ACID};text-align:center">
                  <p style="font-family:monospace;font-size:28px;font-weight:700;
                            color:{_ACID};margin:0">30</p>
                  <p style="font-size:10px;color:{_MUTED};margin:4px 0 0 0;letter-spacing:.08em;
                            text-transform:uppercase">Relatos / mês</p>
                </td>
                <td width="4%">&nbsp;</td>
                <td width="48%" style="background:#0D1425;border-radius:6px;padding:16px;
                    border-top:2px solid {_ACID};text-align:center">
                  <p style="font-family:monospace;font-size:28px;font-weight:700;
                            color:{_ACID};margin:0">∞</p>
                  <p style="font-size:10px;color:{_MUTED};margin:4px 0 0 0;letter-spacing:.08em;
                            text-transform:uppercase">Referências</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>"""

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-glow.png",
        headline=f"Plano Pro ativado,<br>{primeiro}. 🎉",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Você agora tem acesso completo ao CaseOS Pro.
            Gere até <strong style="color:{_TEXT}">30 relatos por mês</strong>
            com todas as funcionalidades desbloqueadas.
          </p>""",
        cta_url=f"{settings.frontend_url}/dashboard.html",
        cta_label="Acessar CaseOS Pro",
        extra_html=extras,
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"[CaseOS] Plano Pro ativado — bem-vindo, {primeiro}!",
        "html": html,
    })


# ── Artigo pronto ────────────────────────────────────────────────────────────

async def enviar_artigo_pronto(
    destinatario: str, nome: str, sessao_id: str,
    care_score: int, total_refs: int, flags: list[str],
) -> bool:
    if not settings.resend_api_key:
        return False

    download_url = f"{settings.frontend_url}/dashboard.html"

    flags_html = ""
    if flags:
        itens = "".join(
            f"<p style='font-size:12px;color:#D97706;margin:4px 0'>"
            f"⚠ {f}</p>" for f in flags
        )
        flags_html = f"""
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:0 36px 16px 36px">
                <div style="background:#1a1400;border:1px solid rgba(217,119,6,0.3);
                            border-radius:6px;padding:14px 16px">
                  <p style="font-size:9px;letter-spacing:.15em;text-transform:uppercase;
                            color:#D97706;margin:0 0 8px 0;font-family:monospace">
                    Pontos para revisão
                  </p>
                  {itens}
                </div>
              </td>
            </tr>
          </table>"""

    score_color = _ACID if care_score >= 10 else ("#D97706" if care_score >= 7 else "#E53E3E")

    extras = f"""
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 36px 20px 36px">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="48%" style="background:#0D1425;border-radius:6px;padding:16px;
                    border-top:2px solid {score_color};text-align:center">
                  <p style="font-family:monospace;font-size:30px;font-weight:700;
                            color:{score_color};margin:0">{care_score}</p>
                  <p style="font-size:10px;color:{_MUTED};margin:4px 0 0 0;letter-spacing:.08em;
                            text-transform:uppercase">CARE Score / 26</p>
                </td>
                <td width="4%">&nbsp;</td>
                <td width="48%" style="background:#0D1425;border-radius:6px;padding:16px;
                    border-top:2px solid {_ACID};text-align:center">
                  <p style="font-family:monospace;font-size:30px;font-weight:700;
                            color:{_ACID};margin:0">{total_refs}</p>
                  <p style="font-size:10px;color:{_MUTED};margin:4px 0 0 0;letter-spacing:.08em;
                            text-transform:uppercase">Referências</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
      {flags_html}"""

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-tablet.png",
        headline="Seu relato está<br>pronto.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Olá, {nome.split()[0]}. O CaseOS concluiu a geração do seu relato de caso.
            Acesse o dashboard para baixar o DOCX.
          </p>""",
        cta_url=download_url,
        cta_label="Baixar relatório",
        extra_html=extras,
        notice_html=f"""
          <p style="font-size:10px;color:{_DIM};margin:14px 0 0 0;font-family:monospace">
            Revise o conteúdo antes de qualquer submissão. Responsabilidade editorial do autor.
          </p>""",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"[CaseOS] Relato pronto — CARE Score {care_score}/26",
        "html": html,
    })


# ── Aviso de pagamento falhou ────────────────────────────────────────────────

async def enviar_aviso_pagamento_falhou(
    destinatario: str, nome: str, proxima_tentativa: str = ""
) -> bool:
    if not settings.resend_api_key:
        return False

    primeiro = nome.split()[0]
    aviso_tentativa = (
        f"<p style='font-size:11px;color:{_DIM};margin:10px 0 0 0;font-family:monospace'>"
        f"Próxima tentativa automática: <strong style='color:{_TEXT}'>{proxima_tentativa}</strong></p>"
        if proxima_tentativa else ""
    )

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-flask.png",
        headline=f"Falha no<br>pagamento, {primeiro}.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0 0 12px 0;line-height:1.7">
            Não conseguimos cobrar sua assinatura CaseOS.
            Atualize seu método de pagamento para manter o acesso ao plano ativo.
          </p>""",
        cta_url=f"{settings.frontend_url}/dashboard.html",
        cta_label="Atualizar pagamento",
        notice_html=f"""
          <div style="background:#1a1000;border-left:3px solid #D97706;border-radius:0 4px 4px 0;
                      padding:12px 16px;margin:16px 0 0 0">
            <p style="font-size:11px;color:#D97706;margin:0;font-family:monospace;letter-spacing:.04em">
              ⚠ Sem atualização, sua assinatura será cancelada e o plano voltará para Gratuito.
            </p>
            {aviso_tentativa}
          </div>""",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Ação necessária: pagamento não aprovado",
        "html": html,
    })


# ── Erro no pipeline ─────────────────────────────────────────────────────────

async def enviar_erro_pipeline(destinatario: str, nome: str, sessao_id: str) -> bool:
    if not settings.resend_api_key:
        return False

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-flask.png",
        headline="Erro na geração<br>do relato.",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Olá, {nome.split()[0]}. Houve uma falha no processamento do seu relato.
            Nossa equipe foi notificada. Você pode tentar novamente — o crédito não foi consumido.
          </p>""",
        cta_url=f"{settings.frontend_url}/dashboard.html",
        cta_label="Tentar novamente",
        notice_html=f"""
          <p style="font-size:10px;color:{_DIM};margin:14px 0 0 0;font-family:monospace">
            Sessão: {sessao_id[:8]}...
            &nbsp;·&nbsp; Se o problema persistir, responda este e-mail.
          </p>""",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Erro na geração — tente novamente",
        "html": html,
    })


# ── Alerta de quota / upsell ─────────────────────────────────────────────────

async def enviar_alerta_quota(
    destinatario: str, nome: str,
    plano_atual: str, artigos_usados: int, limite: int,
    pct: int,
) -> bool:
    if not settings.resend_api_key:
        return False

    primeiro = nome.split()[0]
    proximos = {
        "free":     ("Starter",       "R$ 49/mês",  "6"),
        "starter":  ("Pro",           "R$ 99/mês",  "12"),
        "pro":      ("Institucional", "R$ 349/mês", "ilimitados"),
    }
    prox_nome, prox_preco, prox_qtd = proximos.get(plano_atual, ("Pro", "R$ 99/mês", "12"))

    bar_color = _ACID if pct < 90 else "#E53E3E"
    bar_width = min(pct, 100)

    extras = f"""
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:0 36px 20px 36px">
            <div style="background:#0D1425;border-radius:6px;padding:16px;border:1px solid rgba(200,255,0,0.1)">
              <p style="font-size:11px;font-family:monospace;letter-spacing:.1em;text-transform:uppercase;color:{_DIM};margin:0 0 10px 0">
                USO DO PLANO {plano_atual.upper()}
              </p>
              <div style="background:rgba(255,255,255,0.07);border-radius:4px;height:6px;margin-bottom:8px;overflow:hidden">
                <div style="width:{bar_width}%;height:100%;background:{bar_color};border-radius:4px;font-size:0">&nbsp;</div>
              </div>
              <p style="font-size:13px;color:{_TEXT};margin:0;font-family:monospace">
                <strong style="color:{bar_color}">{artigos_usados}</strong> / {limite} relatos usados ({pct}%)
              </p>
            </div>
            <div style="background:#0A0F1F;border-radius:6px;padding:16px;margin-top:10px;border-top:2px solid {_ACID}">
              <p style="font-size:11px;font-family:monospace;letter-spacing:.1em;text-transform:uppercase;color:{_DIM};margin:0 0 8px 0">
                PRÓXIMO PLANO: {prox_nome.upper()}
              </p>
              <p style="font-size:13px;color:{_TEXT};margin:0">
                {prox_qtd} relatos/mês &nbsp;·&nbsp;
                <strong style="color:{_ACID}">{prox_preco}</strong>
              </p>
            </div>
          </td>
        </tr>
      </table>"""

    html = _base(
        char_url=f"{_ASSETS}/pixel-char-flask.png",
        headline=f"Quase no limite,<br>{primeiro}!",
        body_html=f"""
          <p style="font-size:13px;color:{_MUTED};margin:0;line-height:1.7">
            Você já usou <strong style="color:{_TEXT}">{pct}%</strong> dos seus relatos
            este mês. Faça upgrade para continuar publicando sem interrupções.
          </p>""",
        cta_url=f"{settings.frontend_url}/dashboard.html",
        cta_label=f"Upgrade para {prox_nome}",
        extra_html=extras,
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"[CaseOS] Você usou {pct}% da quota — considere o upgrade",
        "html": html,
    })


# ── E-mail manual (Admin) ─────────────────────────────────────────────────────

async def enviar_email_admin(
    destinatario: str, nome: str,
    assunto: str, mensagem: str,
    cta_url: str = "",
    cta_label: str = "Acessar o CaseOS",
) -> bool:
    if not settings.resend_api_key:
        return False

    # Converte quebras de linha em parágrafos HTML
    paragrafos = "".join(
        f"<p style='font-size:13px;color:{_MUTED};margin:0 0 10px 0;line-height:1.7'>{p.strip()}</p>"
        for p in mensagem.split("\n") if p.strip()
    )

    html = _base(
        char_url=f"{_ASSETS}/pixel-researcher.png",
        headline="Mensagem da<br>equipe CaseOS.",
        body_html=paragrafos,
        cta_url=cta_url or f"{settings.frontend_url}/dashboard.html",
        cta_label=cta_label,
        notice_html=f"""
          <p style="font-size:10px;color:{_DIM};margin:14px 0 0 0;font-family:monospace">
            Esta mensagem foi enviada pela equipe do CaseOS para {nome}.
          </p>""",
    )

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": assunto,
        "html": html,
    })

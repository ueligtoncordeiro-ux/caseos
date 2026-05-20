"""
Serviço de email via Resend.
Dispara notificações transacionais: artigo pronto, verificação, reset de senha, boas-vindas Pro.
"""
import httpx
import logging
from app.config import settings

_BASE = "https://api.resend.com"
log = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }


def _backend_url() -> str:
    """URL base do backend (sem trailing slash)."""
    # Usa GOOGLE_REDIRECT_URI como referência para extrair a base do backend
    uri = settings.google_redirect_uri  # ex: https://caseos-api-production.up.railway.app/auth/google/callback
    return uri.split("/auth/")[0]


async def _send(payload: dict) -> bool:
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY não configurado — e-mail não enviado para %s", payload.get("to"))
        return False
    async with httpx.AsyncClient() as c:
        try:
            log.info("Enviando e-mail via Resend: from=%s to=%s subject=%s",
                     payload.get("from"), payload.get("to"), payload.get("subject"))
            r = await c.post(f"{_BASE}/emails", json=payload, headers=_headers(), timeout=15)
            if r.status_code not in (200, 201):
                log.error("Resend error %s: %s", r.status_code, r.text)
                return False
            log.info("E-mail enviado com sucesso: %s", r.json())
            return True
        except Exception as exc:
            log.error("Resend exception: %s", exc)
            return False


async def enviar_verificacao_email(destinatario: str, nome: str, token: str) -> bool:
    if not settings.resend_api_key:
        return False

    # Link vai DIRETO para o backend — que faz redirect para login.html?msg=verified
    url = f"{_backend_url()}/auth/verify?token={token}"

    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5;margin:0 0 6px">Confirme seu e-mail.</p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 28px">
          Olá, {nome}. Clique abaixo para ativar sua conta no CaseOS.
          Link válido por <strong style="color:#E8EDF5">24 horas</strong>.
        </p>
        <a href="{url}"
           style="display:block;background:#C8FF00;color:#0A0A0B;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;font-weight:700;">
          Verificar e-mail →
        </a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">
          CaseOS · Se não se cadastrou, ignore este e-mail.
        </p>
      </div></body></html>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Confirme seu e-mail",
        "html": html,
    })


async def enviar_reset_senha(destinatario: str, nome: str, token: str) -> bool:
    if not settings.resend_api_key:
        return False

    url = f"{settings.frontend_url}/login.html?reset={token}"

    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5;margin:0 0 6px">Redefinir senha.</p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 28px">
          Olá, {nome}. Link válido por <strong style="color:#E8EDF5">1 hora</strong>.
          Se não solicitou, ignore este e-mail.
        </p>
        <a href="{url}"
           style="display:block;background:#C8FF00;color:#0A0A0B;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;font-weight:700;">
          Redefinir senha →
        </a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">CaseOS</p>
      </div></body></html>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Redefinição de senha",
        "html": html,
    })


async def enviar_boas_vindas(destinatario: str, nome: str, via_google: bool = False) -> bool:
    """E-mail de boas-vindas para novo usuário (plano free)."""
    if not settings.resend_api_key:
        return False

    metodo = "Google" if via_google else "e-mail e senha"
    obs_google = """
        <div style="background:#1a2235;border-left:3px solid #C8FF00;padding:12px 16px;border-radius:0 4px 4px 0;margin-bottom:20px">
          <p style="font-size:12px;color:#8B96A8;margin:0">
            Sua conta foi criada via <strong style="color:#E8EDF5">Login com Google</strong>.
            Não é necessário senha — basta clicar em "Entrar com Google" sempre que acessar.
          </p>
        </div>""" if via_google else ""

    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:22px;color:#C8FF00;margin:0 0 6px">
          Bem-vindo ao CaseOS, {nome.split()[0]}.
        </p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 24px">
          Sua conta foi criada com sucesso via {metodo}.
        </p>
        {obs_google}
        <div style="background:#0D1425;border-radius:6px;padding:16px 20px;margin-bottom:24px">
          <p style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#4A5568;margin:0 0 12px">O que você pode fazer agora</p>
          <p style="font-size:13px;color:#8B96A8;margin:0 0 8px">✦ &nbsp;Gerar relatos de caso clínico estruturados</p>
          <p style="font-size:13px;color:#8B96A8;margin:0 0 8px">✦ &nbsp;Revisão automática com checklist CARE</p>
          <p style="font-size:13px;color:#8B96A8;margin:0">✦ &nbsp;Exportar em DOCX pronto para submissão</p>
        </div>
        <a href="{settings.frontend_url}/dashboard"
           style="display:block;background:#C8FF00;color:#0A0A0B;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;font-weight:700;">
          Acessar o CaseOS →
        </a>
        <p style="font-size:11px;color:#4A5568;text-align:center;margin-top:20px">
          Plano gratuito inclui 1 relato/mês.<br>
          Dúvidas? Responda este e-mail.
        </p>
      </div></body></html>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"Bem-vindo ao CaseOS, {nome.split()[0]}!",
        "html": html,
    })


async def enviar_aviso_login_google(destinatario: str, nome: str) -> bool:
    """Avisa usuário Google-only que tentou 'esqueci senha' que seu login é via Google."""
    if not settings.resend_api_key:
        return False

    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5;margin:0 0 6px">
          Sua conta usa Login com Google.
        </p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 20px">
          Olá, {nome.split()[0]}. Recebemos uma solicitação de redefinição de senha para seu e-mail,
          mas sua conta no CaseOS foi criada via <strong style="color:#E8EDF5">Google</strong> —
          por isso não há senha para redefinir.
        </p>
        <div style="background:#1a2235;border-left:3px solid #C8FF00;padding:14px 16px;border-radius:0 4px 4px 0;margin-bottom:24px">
          <p style="font-size:12px;color:#8B96A8;margin:0">
            Para entrar no CaseOS, clique em <strong style="color:#E8EDF5">"Entrar com Google"</strong>
            na tela de login. Não é necessário senha.
          </p>
        </div>
        <a href="{settings.frontend_url}/login"
           style="display:block;background:#C8FF00;color:#0A0A0B;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;font-weight:700;">
          Ir para o login →
        </a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">
          Se não foi você quem solicitou, ignore este e-mail. CaseOS.
        </p>
      </div></body></html>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Sua conta usa Login com Google",
        "html": html,
    })


async def enviar_boas_vindas_pro(destinatario: str, nome: str) -> bool:
    if not settings.resend_api_key:
        return False

    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(200,255,0,0.2);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:22px;color:#C8FF00;margin:0 0 6px">
          Bem-vindo ao CaseOS Pro.
        </p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 20px">
          Olá, {nome}. Seu plano Pro foi ativado — <strong style="color:#E8EDF5">30 relatos/mês</strong>.
        </p>
        <a href="{settings.frontend_url}/dashboard.html"
           style="display:block;background:#C8FF00;color:#0A0A0B;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;font-weight:700;">
          Acessar plataforma →
        </a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">CaseOS</p>
      </div></body></html>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Plano Pro ativado",
        "html": html,
    })


async def enviar_artigo_pronto(
    destinatario: str,
    nome: str,
    sessao_id: str,
    care_score: int,
    total_refs: int,
    flags: list[str],
) -> bool:
    if not settings.resend_api_key:
        return False

    flags_html = ""
    if flags:
        itens = "".join(f"<li style='margin:4px 0;color:#b45309'>{f}</li>" for f in flags)
        flags_html = f"""
        <div style='background:#fefce8;border:1px solid #fcd34d;border-radius:6px;padding:14px 16px;margin-top:16px'>
          <p style='font-weight:600;color:#92400e;margin:0 0 8px'>⚠ Pontos para revisão humana:</p>
          <ul style='margin:0;padding-left:18px'>{itens}</ul>
        </div>"""

    download_url = f"{settings.frontend_url}/artigo/{sessao_id}/resultado"

    html = f"""<!DOCTYPE html>
    <html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:560px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:22px;color:#E8EDF5;margin:0 0 6px">
          Seu relato está pronto.
        </p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 28px">
          Olá, {nome}. O CaseOS concluiu a geração do seu relato de caso.
        </p>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px">
          <div style="background:#0D1425;border-radius:5px;padding:14px;border-top:2px solid rgba(200,255,0,0.3)">
            <p style="font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#4A5568;margin:0 0 6px">CARE Score</p>
            <p style="font-family:Georgia,serif;font-style:italic;font-size:28px;color:#C8FF00;margin:0">{care_score}</p>
            <p style="font-size:11px;color:#8B96A8;margin:4px 0 0">de 13 itens</p>
          </div>
          <div style="background:#0D1425;border-radius:5px;padding:14px;border-top:2px solid rgba(200,255,0,0.3)">
            <p style="font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#4A5568;margin:0 0 6px">Referências</p>
            <p style="font-family:Georgia,serif;font-style:italic;font-size:28px;color:#C8FF00;margin:0">{total_refs}</p>
            <p style="font-size:11px;color:#8B96A8;margin:4px 0 0">citações</p>
          </div>
        </div>

        {flags_html}

        <a href="{download_url}"
           style="display:block;background:#C8FF00;color:#0A0A0B;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;margin-top:24px;font-weight:700;">
          Download DOCX →
        </a>

        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">
          Revise o conteúdo antes de qualquer submissão.<br>
          CaseOS · Gerado com IA · Responsabilidade editorial do autor.
        </p>
      </div>
    </body>
    </html>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"[CaseOS] Seu relato está pronto — CARE Score {care_score}/13",
        "html": html,
    })


async def enviar_erro_pipeline(destinatario: str, nome: str, sessao_id: str) -> bool:
    if not settings.resend_api_key:
        return False

    html = f"""<!DOCTYPE html>
    <body style="font-family:Arial,sans-serif;background:#080D18;padding:32px">
      <div style="max-width:560px;margin:0 auto;background:#111827;border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5">
          Ocorreu um erro na geração.
        </p>
        <p style="color:#8B96A8;font-size:13px">
          Olá, {nome}. Houve uma falha no processamento do seu relato (sessão {sessao_id}).
          Você pode tentar novamente acessando a plataforma.
        </p>
        <p style="font-size:10px;color:#4A5568;margin-top:20px">CaseOS</p>
      </div>
    </body>"""

    return await _send({
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[CaseOS] Erro na geração — tente novamente",
        "html": html,
    })

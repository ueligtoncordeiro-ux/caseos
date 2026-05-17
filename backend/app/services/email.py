"""
Serviço de email via Resend.
Dispara notificações transacionais: artigo pronto, verificação, reset de senha, boas-vindas Pro.
"""
import httpx
from app.config import settings

_BASE = "https://api.resend.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }


async def enviar_artigo_pronto(
    destinatario: str,
    nome: str,
    sessao_id: str,
    care_score: int,
    total_refs: int,
    flags: list[str],
) -> bool:
    """
    Envia email de entrega ao usuário.
    Retorna True se enviado com sucesso.
    """
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

    html = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:560px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:22px;color:#E8EDF5;margin:0 0 6px">
          Seu artigo está pronto.
        </p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 28px">
          Olá, {nome}. O RCCS concluiu a geração do seu relato de caso.
        </p>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px">
          <div style="background:#0D1425;border-radius:5px;padding:14px;border-top:2px solid rgba(29,233,182,0.3)">
            <p style="font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#4A5568;margin:0 0 6px">CARE Score</p>
            <p style="font-family:Georgia,serif;font-style:italic;font-size:28px;color:#1DE9B6;margin:0">{care_score}</p>
            <p style="font-size:11px;color:#8B96A8;margin:4px 0 0">de 13 itens</p>
          </div>
          <div style="background:#0D1425;border-radius:5px;padding:14px;border-top:2px solid rgba(29,233,182,0.3)">
            <p style="font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#4A5568;margin:0 0 6px">Referências</p>
            <p style="font-family:Georgia,serif;font-style:italic;font-size:28px;color:#1DE9B6;margin:0">{total_refs}</p>
            <p style="font-size:11px;color:#8B96A8;margin:4px 0 0">citações</p>
          </div>
        </div>

        {flags_html}

        <a href="{download_url}"
           style="display:block;background:#1DE9B6;color:#080D18;font-family:monospace;
                  font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;
                  text-align:center;padding:16px;border-radius:4px;margin-top:24px;
                  box-shadow:0 0 24px rgba(29,233,182,.2)">
          Download DOCX
        </a>

        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">
          Revise o conteúdo antes de qualquer submissão.<br>
          RCCS v1.0 · Gerado com IA · Responsabilidade editorial do autor.
        </p>
      </div>
    </body>
    </html>"""

    payload = {
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": f"[RCCS] Seu artigo está pronto — CARE Score {care_score}/13",
        "html": html,
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{_BASE}/emails", json=payload,
                                     headers=_headers(), timeout=15)
            return resp.status_code == 200
        except Exception:
            return False


async def enviar_erro_pipeline(destinatario: str, nome: str, sessao_id: str) -> bool:
    """Notifica o usuário em caso de falha no pipeline."""
    if not settings.resend_api_key:
        return False

    html = f"""
    <body style="font-family:Arial,sans-serif;background:#080D18;padding:32px">
      <div style="max-width:560px;margin:0 auto;background:#111827;border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5">
          Ocorreu um erro na geração.
        </p>
        <p style="color:#8B96A8;font-size:13px">
          Olá, {nome}. Houve uma falha no processamento do seu relato (sessão {sessao_id}).
          Nossa equipe foi notificada. Você pode tentar novamente acessando a plataforma.
        </p>
        <p style="font-size:10px;color:#4A5568;margin-top:20px">RCCS v1.0</p>
      </div>
    </body>"""

    payload = {
        "from": settings.resend_from_email,
        "to": [destinatario],
        "subject": "[RCCS] Erro na geração — tente novamente",
        "html": html,
    }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{_BASE}/emails", json=payload,
                                     headers=_headers(), timeout=15)
            return resp.status_code == 200
        except Exception:
            return False


async def enviar_verificacao_email(destinatario: str, nome: str, token: str) -> bool:
    if not settings.resend_api_key:
        return False
    url = f"{settings.frontend_url}/login.html?verify={token}"
    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5;margin:0 0 6px">Confirme seu e-mail.</p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 28px">Olá, {nome}. Clique abaixo para ativar sua conta. Link válido por <strong style="color:#E8EDF5">24 horas</strong>.</p>
        <a href="{url}" style="display:block;background:#1DE9B6;color:#080D18;font-family:monospace;font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;text-align:center;padding:16px;border-radius:4px;">Verificar e-mail</a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">RCCS v1.0</p>
      </div></body></html>"""
    payload = {"from": settings.resend_from_email, "to": [destinatario],
               "subject": "[RCCS] Confirme seu e-mail", "html": html}
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(f"{_BASE}/emails", json=payload, headers=_headers(), timeout=15)
            return r.status_code == 200
        except Exception:
            return False


async def enviar_reset_senha(destinatario: str, nome: str, token: str) -> bool:
    if not settings.resend_api_key:
        return False
    url = f"{settings.frontend_url}/login.html?reset={token}"
    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(255,255,255,0.07);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:20px;color:#E8EDF5;margin:0 0 6px">Redefinir senha.</p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 28px">Olá, {nome}. Link válido por <strong style="color:#E8EDF5">1 hora</strong>. Se não solicitou, ignore.</p>
        <a href="{url}" style="display:block;background:#1DE9B6;color:#080D18;font-family:monospace;font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;text-align:center;padding:16px;border-radius:4px;">Redefinir senha</a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">RCCS v1.0</p>
      </div></body></html>"""
    payload = {"from": settings.resend_from_email, "to": [destinatario],
               "subject": "[RCCS] Redefinição de senha", "html": html}
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(f"{_BASE}/emails", json=payload, headers=_headers(), timeout=15)
            return r.status_code == 200
        except Exception:
            return False


async def enviar_boas_vindas_pro(destinatario: str, nome: str) -> bool:
    if not settings.resend_api_key:
        return False
    html = f"""<!DOCTYPE html><html lang="pt-BR">
    <body style="font-family:'IBM Plex Sans',Arial,sans-serif;background:#080D18;margin:0;padding:32px">
      <div style="max-width:520px;margin:0 auto;background:#111827;border:1px solid rgba(29,233,182,0.2);border-radius:8px;padding:36px">
        <p style="font-family:Georgia,serif;font-style:italic;font-size:22px;color:#1DE9B6;margin:0 0 6px">Bem-vindo ao RCCS Pro.</p>
        <p style="font-size:13px;color:#8B96A8;margin:0 0 20px">Olá, {nome}. Plano Pro ativado — <strong style="color:#E8EDF5">30 artigos/mês</strong>.</p>
        <a href="{settings.frontend_url}/index.html" style="display:block;background:#1DE9B6;color:#080D18;font-family:monospace;font-size:12px;letter-spacing:.18em;text-transform:uppercase;text-decoration:none;text-align:center;padding:16px;border-radius:4px;">Acessar plataforma</a>
        <p style="font-size:10px;color:#4A5568;text-align:center;margin-top:20px">RCCS v1.0</p>
      </div></body></html>"""
    payload = {"from": settings.resend_from_email, "to": [destinatario],
               "subject": "[RCCS] Plano Pro ativado", "html": html}
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(f"{_BASE}/emails", json=payload, headers=_headers(), timeout=15)
            return r.status_code == 200
        except Exception:
            return False

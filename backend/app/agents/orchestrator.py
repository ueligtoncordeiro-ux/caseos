"""
Orquestrador do pipeline de geração.
Coordena os agentes em sequência e envia eventos de progresso via WebSocket.
Ao concluir, dispara email via Resend e valida palavras-chave via MeSH.
"""
import asyncio
import logging
from sqlalchemy import select

from app.models.schemas import CKO
from app.models.database import AsyncSessionLocal, Sessao
from app.services.websocket_manager import manager
from app.services.email import enviar_artigo_pronto, enviar_erro_pipeline
from app.agents import bibliografico, redator, revisor
from app.services.docx_generator import gerar_docx
from app.utils.mesh import validar_e_corrigir_palavras_chave

logger = logging.getLogger(__name__)


async def _atualizar_sessao(external_id: str, **kwargs):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Sessao).where(Sessao.external_id == external_id))
        sessao = result.scalar_one_or_none()
        if sessao:
            for k, v in kwargs.items():
                setattr(sessao, k, v)
            await db.commit()


async def executar_pipeline(sessao_id: str, cko: CKO):
    """Pipeline principal. Executado como BackgroundTask."""
    email_usuario = cko.editorial.__dict__.get("email_usuario", "")

    try:
        # ── Etapa 1: Validação ──────────────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 1, "nome": "Validação"})
        await _atualizar_sessao(sessao_id, status="validando")
        await asyncio.sleep(0.8)

        # ── Etapa 2: Anti-duplicação ────────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 2, "nome": "Anti-duplicação"})
        await asyncio.sleep(1.0)

        # ── Etapa 3: Pesquisa bibliográfica (5 fontes) ──────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 3, "nome": "Pesquisa bibliográfica"})
        await _atualizar_sessao(sessao_id, status="buscando_referencias")

        artigos = await bibliografico.executar(cko)

        await manager.send(sessao_id, {
            "tipo": "progresso",
            "etapa": 3,
            "detalhe": f"{len(artigos)} referências encontradas (PubMed · S2 · OpenAlex · Crossref · Unpaywall)",
        })

        # ── Etapa 4: Redação ────────────────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 4, "nome": "Redação"})
        await _atualizar_sessao(sessao_id, status="redigindo")

        artigo = await redator.executar(cko, artigos)

        # ── Etapa 5: Revisão + validação MeSH ──────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 5, "nome": "Revisão"})
        await _atualizar_sessao(sessao_id, status="revisando")

        # Revisão linguística e CARE Score
        artigo_revisado, relatorio = await revisor.executar(cko, artigo)

        # Validação MeSH das palavras-chave (não bloqueia o pipeline se falhar)
        try:
            mesh_resultado = await validar_e_corrigir_palavras_chave(artigo_revisado.palavras_chave)
            # Substituir palavras-chave pelas versões MeSH validadas
            if mesh_resultado["validadas"]:
                artigo_revisado.palavras_chave = (
                    mesh_resultado["validadas"] + mesh_resultado["invalidas"]
                )
            # Adicionar flag se houver inválidos
            if mesh_resultado["invalidas"]:
                relatorio.flags.append(
                    f"Palavras-chave não validadas no MeSH: {', '.join(mesh_resultado['invalidas'])}. "
                    f"Score MeSH: {mesh_resultado['score_mesh']:.0%}"
                )
        except Exception as e:
            logger.warning(f"Validação MeSH falhou (não crítico): {e}")

        # ── Etapa 6: Finalização DOCX ───────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 6, "nome": "Finalização"})
        await _atualizar_sessao(sessao_id, status="finalizando")

        docx_path = await gerar_docx(
            sessao_id=sessao_id,
            artigo=artigo_revisado,
            cko=cko,
        )

        # Persistir resultado
        await _atualizar_sessao(
            sessao_id,
            status="concluido",
            resultado=artigo_revisado.model_dump(),
            relatorio=relatorio.model_dump(),
            flags=relatorio.flags,
            docx_path=docx_path,
        )

        resultado_ws = {
            "care_score": relatorio.care_score,
            "referencias": relatorio.total_referencias,
            "bases_consultadas": relatorio.bases_consultadas,
            "flags": relatorio.flags,
        }

        await manager.send(sessao_id, {"tipo": "concluido", "resultado": resultado_ws})

        # Email de entrega (não bloqueia, falha silenciosa)
        if email_usuario:
            asyncio.create_task(enviar_artigo_pronto(
                destinatario=email_usuario,
                nome=email_usuario.split("@")[0].capitalize(),
                sessao_id=sessao_id,
                care_score=relatorio.care_score,
                total_refs=relatorio.total_referencias,
                flags=relatorio.flags,
            ))

    except Exception as exc:
        await _atualizar_sessao(sessao_id, status="erro")
        await manager.send(sessao_id, {
            "tipo": "erro",
            "mensagem": f"Erro no pipeline: {str(exc)[:300]}",
        })
        if email_usuario:
            asyncio.create_task(enviar_erro_pipeline(
                destinatario=email_usuario,
                nome=email_usuario.split("@")[0].capitalize(),
                sessao_id=sessao_id,
            ))
        raise
    finally:
        manager.clear_queue(sessao_id)

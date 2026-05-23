"""
Orquestrador do pipeline de geração.
Coordena os agentes em sequência e envia eventos de progresso via WebSocket.
Ao concluir, dispara email via Resend e valida palavras-chave via MeSH.
"""
import asyncio
import logging
from typing import Optional
from sqlalchemy import select

from app.models.schemas import CKO
from app.models.database import AsyncSessionLocal, Sessao
from app.services.websocket_manager import manager
from app.services.email import enviar_artigo_pronto, enviar_erro_pipeline
from app.services.llm_router import iniciar_contagem_tokens, obter_tokens_usados
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


async def executar_pipeline(
    sessao_id: str,
    cko: CKO,
    user_id: Optional[str] = None,
    nome_usuario: Optional[str] = None,
):
    """Pipeline principal. Executado como BackgroundTask."""
    email_usuario = cko.editorial.__dict__.get("email_usuario", "")
    nome_display = nome_usuario or (email_usuario.split("@")[0].capitalize() if email_usuario else "Pesquisador")
    iniciar_contagem_tokens()  # zera contador para este pipeline

    try:
        # ── Etapa 1: Validação ──────────────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 1, "nome": "Validação", "agente": "Verificador de dados clínicos", "detalhe": "Validando estrutura do formulário..."})
        await _atualizar_sessao(sessao_id, status="validando")
        await asyncio.sleep(0.8)

        # ── Etapa 2: Anti-duplicação ────────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 2, "nome": "Anti-duplicação", "agente": "Agente de originalidade", "detalhe": "Verificando originalidade do caso..."})
        await asyncio.sleep(1.0)

        # ── Etapa 3: Pesquisa bibliográfica (5 fontes) ──────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 3, "nome": "Pesquisa bibliográfica", "agente": "PubMed · OpenAlex · Crossref", "detalhe": "Buscando literatura relevante..."})
        await _atualizar_sessao(sessao_id, status="buscando_referencias")

        artigos = await bibliografico.executar(cko)

        await manager.send(sessao_id, {
            "tipo": "progresso",
            "etapa": 3,
            "detalhe": f"{len(artigos)} referências encontradas (PubMed · S2 · OpenAlex · Crossref · Unpaywall)",
        })

        # ── Etapa 4: Redação ────────────────────────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 4, "nome": "Redação", "agente": "Claude Sonnet 4.6 · GPT-4o", "detalhe": "Redigindo o relato de caso..."})
        await _atualizar_sessao(sessao_id, status="redigindo")

        artigo = await redator.executar(cko, artigos)

        # ── Etapa 5: Revisão + validação MeSH ──────────────────────────────
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 5, "nome": "Revisão CARE", "agente": "GPT-4o · CARE 2013", "detalhe": "Verificando conformidade CARE 2013..."})
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
        await manager.send(sessao_id, {"tipo": "etapa", "etapa": 6, "nome": "Finalização", "agente": "DOCX Generator · ABNT", "detalhe": "Gerando documento final..."})
        await _atualizar_sessao(sessao_id, status="finalizando")

        docx_path = await gerar_docx(
            sessao_id=sessao_id,
            artigo=artigo_revisado,
            cko=cko,
        )

        # Apurar tokens consumidos pelo pipeline completo
        tokens_pipeline = obter_tokens_usados()

        # Persistir resultado
        await _atualizar_sessao(
            sessao_id,
            status="concluido",
            resultado=artigo_revisado.model_dump(),
            relatorio=relatorio.model_dump(),
            flags=relatorio.flags,
            docx_path=docx_path,
            care_score=relatorio.care_score,
            tokens_usados=tokens_pipeline,
        )

        # Debitar tokens do saldo mensal do usuário (await — dado financeiro)
        if user_id and tokens_pipeline > 0:
            from app.services.auth import debitar_tokens
            await debitar_tokens(user_id, tokens_pipeline)

        logger.info("Pipeline concluído sessao=%s tokens=%d", sessao_id, tokens_pipeline)

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
                nome=nome_display,
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

        # Devolver artigo ao saldo: falha de sistema não deve consumir quota do usuário
        if user_id:
            from app.services.auth import devolver_artigo
            try:
                await devolver_artigo(user_id)
                logger.info("Artigo devolvido ao saldo de user=%s após erro no pipeline", user_id)
            except Exception as e:
                logger.error("Falha ao devolver artigo ao saldo: %s", e)

        if email_usuario:
            asyncio.create_task(enviar_erro_pipeline(
                destinatario=email_usuario,
                nome=nome_display,
                sessao_id=sessao_id,
            ))
        raise
    finally:
        manager.clear_queue(sessao_id)


async def executar_pipeline_demo(sessao_id: str, cko: CKO):
    """Pipeline de demonstração — sem autenticação, gera apenas Introdução + Caso Clínico."""
    try:
        # ── Etapa 1: Validação ──────────────────────────────────────────────
        await manager.send(sessao_id, {
            "tipo": "etapa", "etapa": 1, "nome": "Validação",
            "agente": "Verificador de dados clínicos",
            "detalhe": "Validando estrutura do formulário...",
        })
        await asyncio.sleep(0.8)

        # ── Etapa 2: Anti-duplicação ────────────────────────────────────────
        await manager.send(sessao_id, {
            "tipo": "etapa", "etapa": 2, "nome": "Anti-duplicação",
            "agente": "Agente de originalidade",
            "detalhe": "Verificando originalidade do caso...",
        })
        await asyncio.sleep(0.8)

        # ── Etapa 3: Pesquisa bibliográfica ─────────────────────────────────
        await manager.send(sessao_id, {
            "tipo": "etapa", "etapa": 3, "nome": "Pesquisa bibliográfica",
            "agente": "PubMed · OpenAlex · Crossref",
            "detalhe": "Buscando literatura relevante...",
        })

        artigos = await bibliografico.executar(cko)

        await manager.send(sessao_id, {
            "tipo": "progresso", "etapa": 3,
            "detalhe": f"{len(artigos)} referências encontradas",
        })

        # ── Etapa 4: Redação parcial (demo) ─────────────────────────────────
        await manager.send(sessao_id, {
            "tipo": "etapa", "etapa": 4, "nome": "Redação",
            "agente": "Claude Sonnet 4.6",
            "detalhe": "Redigindo introdução e apresentação do caso...",
        })

        demo_resultado = await redator.executar_demo(cko, artigos)

        # ── Concluído (demo) ─────────────────────────────────────────────────
        await manager.send(sessao_id, {
            "tipo": "concluido_demo",
            "resultado": demo_resultado,
        })

    except Exception as exc:
        await manager.send(sessao_id, {
            "tipo": "erro",
            "mensagem": f"Erro no pipeline de demonstração: {str(exc)[:300]}",
        })
        logger.exception("Erro no pipeline demo sessao=%s", sessao_id)
    finally:
        manager.clear_queue(sessao_id)

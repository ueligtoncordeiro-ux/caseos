from io import BytesIO
from pathlib import Path
import re
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import (
    Revisao,
    RevisaoDecisao,
    RevisaoFonte,
    RevisaoFonteChunk,
    Usuario,
    get_db,
)
from app.models.schemas import (
    RevisaoBuscarFontesRequest,
    RevisaoCorpusBuildRequest,
    RevisaoCorpusPerguntaRequest,
    RevisaoCreateRequest,
    RevisaoFonteCreateRequest,
    RevisaoFonteChunkPublico,
    RevisaoFonteDecisaoRequest,
    RevisaoFontePublica,
    RevisaoImportarFontesRequest,
    RevisaoPublica,
    RevisaoRascunhoRequest,
    RevisaoUpdateRequest,
)
from app.services.auth import get_verified_user, check_token_quota, debitar_tokens
from app.services.llm_router import (
    Complexidade,
    chamar,
    iniciar_contagem_tokens,
    mensagem_usuario_erro,
    obter_tokens_usados,
)
from app.services.review_corpus import reconstruir_corpus_fechado
from app.services.scientific_search import buscar_literatura

router = APIRouter(prefix="/revisoes", tags=["revisoes"])

_UPLOAD_EXTENSOES_PERMITIDAS = {".pdf", ".docx", ".txt", ".md"}
_MAX_UPLOAD_BYTES = 12 * 1024 * 1024
_MAX_TEXTO_EXTRAIDO = 120_000
_MAX_ABSTRACT_PREVIEW = 4_000


async def _get_revisao_ou_404(
    revisao_id: str,
    user_id: str,
    db: AsyncSession,
) -> Revisao:
    result = await db.execute(
        select(Revisao).where(
            Revisao.id == revisao_id,
            Revisao.user_id == user_id,
        )
    )
    revisao = result.scalar_one_or_none()
    if not revisao:
        raise HTTPException(status_code=404, detail="Revisão não encontrada.")
    return revisao


async def _get_fonte_ou_404(
    revisao_id: str,
    fonte_id: str,
    user_id: str,
    db: AsyncSession,
) -> RevisaoFonte:
    await _get_revisao_ou_404(revisao_id, user_id, db)
    result = await db.execute(
        select(RevisaoFonte).where(
            RevisaoFonte.id == fonte_id,
            RevisaoFonte.revisao_id == revisao_id,
        )
    )
    fonte = result.scalar_one_or_none()
    if not fonte:
        raise HTTPException(status_code=404, detail="Fonte não encontrada.")
    return fonte


def _fonte_key(artigo: dict) -> str:
    doi = (artigo.get("doi") or "").lower().strip()
    pmid = (artigo.get("pmid") or "").strip()
    titulo = (artigo.get("titulo") or "").lower().strip()
    return doi or (f"pmid:{pmid}" if pmid else titulo)


def _valor_form(valor: Optional[str]) -> Optional[str]:
    valor = (valor or "").strip()
    return valor or None


def _safe_upload_path(user_id: str, revisao_id: str, filename: str) -> Path:
    ext = Path(filename or "").suffix.lower()
    if ext not in _UPLOAD_EXTENSOES_PERMITIDAS:
        permitidas = ", ".join(sorted(_UPLOAD_EXTENSOES_PERMITIDAS))
        raise HTTPException(
            status_code=400,
            detail=f"Formato não suportado. Envie: {permitidas}.",
        )

    base = Path(settings.review_upload_dir).resolve()
    pasta = (base / user_id / revisao_id).resolve()
    destino = (pasta / f"{uuid4().hex}{ext}").resolve()
    try:
        destino.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido.")
    pasta.mkdir(parents=True, exist_ok=True)
    return destino


def _decodificar_texto(conteudo: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return conteudo.decode(encoding)
        except UnicodeDecodeError:
            continue
    return conteudo.decode("utf-8", errors="ignore")


def _extrair_texto_docx(conteudo: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Extração de DOCX indisponível no servidor.",
        )

    documento = Document(BytesIO(conteudo))
    paragrafos = [p.text.strip() for p in documento.paragraphs if p.text.strip()]
    return "\n".join(paragrafos)


def _extrair_texto_pdf(conteudo: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="Extração de PDF indisponível no servidor.",
        )

    try:
        leitor = PdfReader(BytesIO(conteudo))
    except Exception:
        raise HTTPException(status_code=400, detail="Não foi possível ler o PDF enviado.")

    partes: list[str] = []
    for index, pagina in enumerate(leitor.pages):
        if index >= 80:
            break
        texto = pagina.extract_text() or ""
        if texto.strip():
            partes.append(texto.strip())
        if sum(len(parte) for parte in partes) >= _MAX_TEXTO_EXTRAIDO:
            break
    return "\n".join(partes)


def _extrair_texto_upload(filename: str, conteudo: bytes) -> tuple[str, Optional[str]]:
    ext = Path(filename or "").suffix.lower()
    aviso = None
    if ext in {".txt", ".md"}:
        texto = _decodificar_texto(conteudo)
    elif ext == ".docx":
        texto = _extrair_texto_docx(conteudo)
    elif ext == ".pdf":
        texto = _extrair_texto_pdf(conteudo)
    else:
        texto = ""

    texto = " ".join((texto or "").split())
    if len(texto) > _MAX_TEXTO_EXTRAIDO:
        texto = texto[:_MAX_TEXTO_EXTRAIDO].strip()
        aviso = "Texto extraído truncado para manter o corpus responsivo."
    if not texto:
        aviso = "Não foi possível extrair texto automaticamente deste arquivo."
    return texto, aviso


def _fonte_publica(fonte: RevisaoFonte) -> RevisaoFontePublica:
    payload = RevisaoFontePublica.model_validate(fonte)
    metadados = dict(payload.metadados or {})
    metadados.pop("texto_extraido", None)
    payload.metadados = metadados
    return payload


def _termos_busca(texto: str) -> set[str]:
    return {
        termo
        for termo in re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", (texto or "").lower())
        if termo not in {"para", "com", "uma", "que", "por", "dos", "das", "the", "and", "with"}
    }


def _score_chunk(chunk: RevisaoFonteChunk, fonte: RevisaoFonte, termos: set[str]) -> int:
    if not termos:
        return 0
    texto = f"{chunk.texto or ''} {fonte.titulo or ''} {fonte.autores or ''}".lower()
    score = 0
    titulo = (fonte.titulo or "").lower()
    for termo in termos:
        ocorrencias = texto.count(termo)
        if ocorrencias:
            score += ocorrencias
        if termo in titulo:
            score += 3
    if fonte.fonte_base and fonte.fonte_base.lower() == "pubmed":
        score += 1
    return score


async def _selecionar_chunks_corpus(
    revisao_id: str,
    query: str,
    max_chunks: int,
    db: AsyncSession,
) -> list[RevisaoFonteChunk]:
    filtros = [
        RevisaoFonte.revisao_id == revisao_id,
        RevisaoFonte.aprovada_para_escrita.is_(True),
    ]

    async def _carregar() -> list[RevisaoFonteChunk]:
        result = await db.execute(
            select(RevisaoFonteChunk)
            .join(RevisaoFonte, RevisaoFonte.id == RevisaoFonteChunk.fonte_id)
            .where(*filtros)
            .order_by(RevisaoFonteChunk.ordem)
        )
        return result.scalars().all()

    chunks = await _carregar()
    if not chunks:
        await reconstruir_corpus_fechado(revisao_id, db, somente_aprovadas=True)
        chunks = await _carregar()

    if not chunks:
        return []

    fontes_result = await db.execute(
        select(RevisaoFonte).where(
            RevisaoFonte.id.in_({chunk.fonte_id for chunk in chunks})
        )
    )
    fontes = {fonte.id: fonte for fonte in fontes_result.scalars().all()}
    termos = _termos_busca(query)
    def _ranking(chunk: RevisaoFonteChunk) -> tuple[int, int]:
        fonte = fontes.get(chunk.fonte_id)
        if not fonte:
            return (0, -(chunk.ordem or 0))
        return (_score_chunk(chunk, fonte, termos), -(chunk.ordem or 0))

    ranqueados = sorted(chunks, key=_ranking, reverse=True)
    return ranqueados[:max_chunks]


async def _fontes_dos_chunks(
    chunks: list[RevisaoFonteChunk],
    db: AsyncSession,
) -> dict[str, RevisaoFonte]:
    if not chunks:
        return {}
    result = await db.execute(
        select(RevisaoFonte).where(RevisaoFonte.id.in_({chunk.fonte_id for chunk in chunks}))
    )
    return {fonte.id: fonte for fonte in result.scalars().all()}


def _formatar_contexto(
    chunks: list[RevisaoFonteChunk],
    fontes: dict[str, RevisaoFonte],
) -> tuple[str, list[dict]]:
    partes: list[str] = []
    fontes_usadas: list[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        fonte = fontes.get(chunk.fonte_id)
        if not fonte:
            continue
        ref = f"F{idx}"
        metadados = fonte.metadados or {}
        identificadores = []
        if fonte.pmid:
            identificadores.append(f"PMID: {fonte.pmid}")
        if fonte.doi:
            identificadores.append(f"DOI: {fonte.doi}")
        if fonte.fonte_base:
            identificadores.append(f"Base: {fonte.fonte_base}")

        cabecalho = "; ".join(
            parte
            for parte in [
                fonte.titulo,
                fonte.autores,
                fonte.ano,
                fonte.periodico,
                " | ".join(identificadores),
            ]
            if parte
        )
        partes.append(f"[{ref}] {cabecalho}\nTrecho: {chunk.texto.strip()}")
        fontes_usadas.append(
            {
                "ref": ref,
                "fonte_id": fonte.id,
                "chunk_id": chunk.id,
                "ordem": chunk.ordem,
                "titulo": fonte.titulo,
                "autores": fonte.autores,
                "ano": fonte.ano,
                "periodico": fonte.periodico,
                "doi": fonte.doi,
                "pmid": fonte.pmid,
                "url": fonte.url,
                "fonte_base": fonte.fonte_base,
                "origem": fonte.origem,
                "texto_extraido_chars": metadados.get("texto_extraido_chars"),
            }
        )
    return "\n\n".join(partes), fontes_usadas


def _system_corpus_fechado() -> str:
    return (
        "Você é o assistente científico do CaseOS para revisão de literatura. "
        "Responda exclusivamente com base no corpus fechado fornecido pelo usuário. "
        "Não use conhecimento externo, não invente autores, achados, estatísticas, links ou citações. "
        "Quando o corpus não sustentar uma afirmação, diga claramente que as fontes aprovadas não trazem "
        "evidência suficiente. Cite as fontes no texto como [F1], [F2]. Seja direto, técnico e útil."
    )


def _prompt_pergunta(revisao: Revisao, pergunta: str, contexto: str) -> str:
    return f"""
Revisão:
- Tipo: {revisao.tipo}
- Título: {revisao.titulo or "não definido"}
- Tema: {revisao.tema or "não definido"}
- Pergunta: {revisao.pergunta or "não definida"}
- Objetivo: {revisao.objetivo or "não definido"}
- Formato de referências: {revisao.formato_ref}
- Guideline: {revisao.guideline or "não definida"}

Pergunta do usuário:
{pergunta}

Corpus fechado aprovado:
{contexto}

Responda em português, com 1 a 4 parágrafos ou bullets curtos. Sempre cite as fontes usadas.
""".strip()


def _prompt_rascunho(revisao: Revisao, body: RevisaoRascunhoRequest, contexto: str) -> str:
    instrucao = body.instrucao or "Gerar um rascunho científico claro, conciso e editável."
    return f"""
Revisão:
- Tipo: {revisao.tipo}
- Título: {revisao.titulo or "não definido"}
- Tema: {revisao.tema or "não definido"}
- Pergunta: {revisao.pergunta or "não definida"}
- Objetivo: {revisao.objetivo or "não definido"}
- Formato de referências: {revisao.formato_ref}
- Guideline: {revisao.guideline or "não definida"}

Seção solicitada:
{body.secao}

Instrução do usuário:
{instrucao}

Corpus fechado aprovado:
{contexto}

Gere apenas a seção solicitada. Use tom científico, cite no corpo como [F1], [F2] e não crie referências não presentes no corpus.
Se as fontes forem insuficientes, entregue um rascunho parcial e liste objetivamente o que falta.
""".strip()


def _fonte_payload(revisao_id: str, artigo: dict, query: str) -> RevisaoFonte:
    fonte = artigo.get("fonte") or ("PubMed" if artigo.get("pmid") else "")
    return RevisaoFonte(
        revisao_id=revisao_id,
        origem="busca",
        fonte_base=fonte,
        titulo=artigo.get("titulo") or "Sem título",
        autores=artigo.get("autores") or None,
        ano=str(artigo.get("ano") or "") or None,
        periodico=artigo.get("periodico") or None,
        doi=artigo.get("doi") or None,
        pmid=artigo.get("pmid") or None,
        url=artigo.get("url") or artigo.get("url_pubmed") or artigo.get("open_access_url") or None,
        abstract=artigo.get("abstract") or None,
        metadados={
            "query": query,
            "citacoes": artigo.get("citacoes", 0),
            "open_access_url": artigo.get("open_access_url") or "",
            "openalex_id": artigo.get("openalex_id") or "",
            "s2_id": artigo.get("s2_id") or "",
        },
        tags=[fonte.lower().replace(" ", "_")] if fonte else [],
    )


async def _importar_artigos(
    revisao_id: str,
    artigos: list[dict],
    query: str,
    db: AsyncSession,
) -> tuple[list[RevisaoFonte], int]:
    result = await db.execute(
        select(RevisaoFonte).where(RevisaoFonte.revisao_id == revisao_id)
    )
    existentes = {_fonte_key({
        "doi": fonte.doi,
        "pmid": fonte.pmid,
        "titulo": fonte.titulo,
    }) for fonte in result.scalars().all()}

    importadas: list[RevisaoFonte] = []
    duplicadas = 0
    vistas_lote: set[str] = set()
    for artigo in artigos:
        chave = _fonte_key(artigo)
        if not chave or chave in existentes or chave in vistas_lote:
            duplicadas += 1
            continue
        vistas_lote.add(chave)
        fonte = _fonte_payload(revisao_id, artigo, query)
        db.add(fonte)
        importadas.append(fonte)

    if importadas:
        await db.commit()
        for fonte in importadas:
            await db.refresh(fonte)

    return importadas, duplicadas


@router.post("", response_model=RevisaoPublica, status_code=status.HTTP_201_CREATED)
async def criar_revisao(
    body: RevisaoCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    revisao = Revisao(
        user_id=user.id,
        tipo=body.tipo,
        titulo=body.titulo,
        tema=body.tema,
        pergunta=body.pergunta,
        objetivo=body.objetivo,
        formato_ref=body.formato_ref,
        protocolo={},
        criterios={},
        preferencias={},
        checklist={},
    )
    db.add(revisao)
    await db.commit()
    await db.refresh(revisao)
    return revisao


@router.get("")
async def listar_revisoes(
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=10, ge=1, le=50),
    status_filtro: Optional[str] = Query(default=None, alias="status"),
    tipo: Optional[str] = Query(default=None),
):
    filtros = [Revisao.user_id == user.id]
    if status_filtro:
        filtros.append(Revisao.status == status_filtro)
    if tipo:
        filtros.append(Revisao.tipo == tipo)

    total_result = await db.execute(select(func.count()).select_from(Revisao).where(*filtros))
    total = total_result.scalar() or 0

    result = await db.execute(
        select(Revisao)
        .where(*filtros)
        .order_by(desc(Revisao.updated_at), desc(Revisao.created_at))
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )
    revisoes = result.scalars().all()

    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "items": [RevisaoPublica.model_validate(revisao) for revisao in revisoes],
    }


@router.get("/{revisao_id}", response_model=RevisaoPublica)
async def obter_revisao(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    return await _get_revisao_ou_404(revisao_id, user.id, db)


@router.patch("/{revisao_id}", response_model=RevisaoPublica)
async def atualizar_revisao(
    revisao_id: str,
    body: RevisaoUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    revisao = await _get_revisao_ou_404(revisao_id, user.id, db)
    dados = body.model_dump(exclude_unset=True)
    for campo, valor in dados.items():
        setattr(revisao, campo, valor)

    await db.commit()
    await db.refresh(revisao)
    return revisao


@router.delete("/{revisao_id}")
async def deletar_revisao(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    revisao = await _get_revisao_ou_404(revisao_id, user.id, db)
    await db.delete(revisao)
    await db.commit()
    return {"deletado": True, "revisao_id": revisao_id}


@router.post(
    "/{revisao_id}/fontes",
    response_model=RevisaoFontePublica,
    status_code=status.HTTP_201_CREATED,
)
async def adicionar_fonte(
    revisao_id: str,
    body: RevisaoFonteCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    fonte = RevisaoFonte(
        revisao_id=revisao_id,
        origem=body.origem,
        fonte_base=body.fonte_base,
        titulo=body.titulo,
        autores=body.autores,
        ano=body.ano,
        periodico=body.periodico,
        doi=body.doi,
        pmid=body.pmid,
        url=body.url,
        abstract=body.abstract,
        metadados=body.metadados,
        tags=body.tags,
    )
    db.add(fonte)
    await db.commit()
    await db.refresh(fonte)
    return _fonte_publica(fonte)


@router.post(
    "/{revisao_id}/fontes/upload",
    status_code=status.HTTP_201_CREATED,
)
async def enviar_fonte_upload(
    revisao_id: str,
    arquivo: UploadFile = File(...),
    titulo: Optional[str] = Form(default=None),
    autores: Optional[str] = Form(default=None),
    ano: Optional[str] = Form(default=None),
    periodico: Optional[str] = Form(default=None),
    doi: Optional[str] = Form(default=None),
    url: Optional[str] = Form(default=None),
    aprovar_para_escrita: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    filename = arquivo.filename or "fonte"
    destino = _safe_upload_path(user.id, revisao_id, filename)
    conteudo = await arquivo.read()

    if not conteudo:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    if len(conteudo) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Arquivo muito grande. Envie arquivos de até 12 MB.",
        )

    destino.write_bytes(conteudo)
    texto_extraido, aviso = _extrair_texto_upload(filename, conteudo)
    titulo_final = _valor_form(titulo) or Path(filename).stem.replace("_", " ").strip() or "Fonte enviada"
    ext = destino.suffix.lower().lstrip(".")

    metadados = {
        "original_filename": filename,
        "content_type": arquivo.content_type or "",
        "size_bytes": len(conteudo),
        "extension": ext,
        "texto_extraido": texto_extraido,
        "texto_extraido_chars": len(texto_extraido),
    }
    if aviso:
        metadados["extracao_aviso"] = aviso

    fonte = RevisaoFonte(
        revisao_id=revisao_id,
        origem="upload",
        fonte_base="Upload",
        status="importada",
        titulo=titulo_final,
        autores=_valor_form(autores),
        ano=_valor_form(ano),
        periodico=_valor_form(periodico),
        doi=_valor_form(doi),
        pmid=None,
        url=_valor_form(url),
        abstract=texto_extraido[:_MAX_ABSTRACT_PREVIEW] if texto_extraido else None,
        arquivo_path=str(destino),
        metadados=metadados,
        tags=["upload", ext],
        decisao_humana="incluida" if aprovar_para_escrita else None,
        aprovada_para_escrita=aprovar_para_escrita,
    )
    db.add(fonte)
    await db.commit()
    await db.refresh(fonte)

    return {
        "fonte": _fonte_publica(fonte),
        "texto_extraido_chars": len(texto_extraido),
        "extracao_aviso": aviso,
    }


@router.post("/{revisao_id}/buscar-fontes")
async def buscar_fontes_para_revisao(
    revisao_id: str,
    body: RevisaoBuscarFontesRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    try:
        return await buscar_literatura(body.query, max_results=body.max, fontes=body.fontes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{revisao_id}/importar-fontes")
async def importar_fontes_para_revisao(
    revisao_id: str,
    body: RevisaoImportarFontesRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    artigos = body.artigos
    erros: list[str] = []

    if not artigos:
        try:
            resultado = await buscar_literatura(body.query, max_results=body.max, fontes=body.fontes)
            artigos = resultado["artigos"]
            erros = resultado.get("erros", [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    importadas, duplicadas = await _importar_artigos(revisao_id, artigos, body.query, db)
    return {
        "revisao_id": revisao_id,
        "query": body.query,
        "fontes": body.fontes,
        "importadas": len(importadas),
        "duplicadas_ignoradas": duplicadas,
        "erros": erros,
        "items": [_fonte_publica(fonte) for fonte in importadas],
    }


@router.get("/{revisao_id}/fontes")
async def listar_fontes(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=20, ge=1, le=100),
    decisao: Optional[str] = Query(default=None),
    origem: Optional[str] = Query(default=None),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    filtros = [RevisaoFonte.revisao_id == revisao_id]
    if decisao:
        filtros.append(RevisaoFonte.decisao_humana == decisao)
    if origem:
        filtros.append(RevisaoFonte.origem == origem)

    total_result = await db.execute(
        select(func.count()).select_from(RevisaoFonte).where(*filtros)
    )
    total = total_result.scalar() or 0

    result = await db.execute(
        select(RevisaoFonte)
        .where(*filtros)
        .order_by(desc(RevisaoFonte.aprovada_para_escrita), desc(RevisaoFonte.created_at))
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )
    fontes = result.scalars().all()

    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "items": [_fonte_publica(fonte) for fonte in fontes],
    }


@router.post("/{revisao_id}/corpus/reconstruir")
async def reconstruir_corpus(
    revisao_id: str,
    body: RevisaoCorpusBuildRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    resumo = await reconstruir_corpus_fechado(
        revisao_id,
        db,
        somente_aprovadas=body.somente_aprovadas,
    )
    return {"revisao_id": revisao_id, **resumo}


@router.get("/{revisao_id}/corpus")
async def listar_corpus(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
    pagina: int = Query(default=1, ge=1),
    por_pagina: int = Query(default=30, ge=1, le=100),
    fonte_id: Optional[str] = Query(default=None),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)
    filtros = [RevisaoFonte.revisao_id == revisao_id]
    if fonte_id:
        filtros.append(RevisaoFonteChunk.fonte_id == fonte_id)

    total_result = await db.execute(
        select(func.count())
        .select_from(RevisaoFonteChunk)
        .join(RevisaoFonte, RevisaoFonte.id == RevisaoFonteChunk.fonte_id)
        .where(*filtros)
    )
    total = total_result.scalar() or 0

    result = await db.execute(
        select(RevisaoFonteChunk)
        .join(RevisaoFonte, RevisaoFonte.id == RevisaoFonteChunk.fonte_id)
        .where(*filtros)
        .order_by(RevisaoFonte.titulo, RevisaoFonteChunk.ordem)
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    )
    chunks = result.scalars().all()

    return {
        "revisao_id": revisao_id,
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": (total + por_pagina - 1) // por_pagina,
        "items": [RevisaoFonteChunkPublico.model_validate(chunk) for chunk in chunks],
    }


@router.post("/{revisao_id}/corpus/perguntar")
async def perguntar_ao_corpus(
    revisao_id: str,
    body: RevisaoCorpusPerguntaRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(check_token_quota),   # verifica limite de tokens antes da chamada LLM
):
    revisao = await _get_revisao_ou_404(revisao_id, user.id, db)
    chunks = await _selecionar_chunks_corpus(revisao_id, body.pergunta, body.max_chunks, db)
    if not chunks:
        raise HTTPException(
            status_code=400,
            detail=(
                "Corpus fechado vazio. Aprove fontes para escrita ou envie arquivos "
                "antes de perguntar à revisão."
            ),
        )

    fontes = await _fontes_dos_chunks(chunks, db)
    contexto, fontes_usadas = _formatar_contexto(chunks, fontes)
    iniciar_contagem_tokens()
    try:
        resposta = await chamar(
            _system_corpus_fechado(),
            _prompt_pergunta(revisao, body.pergunta, contexto),
            complexidade=Complexidade.MEDIA,
            max_tokens=1400,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=mensagem_usuario_erro(exc))

    tokens_gastos = obter_tokens_usados()
    revisao.tokens_usados = (revisao.tokens_usados or 0) + tokens_gastos
    await db.commit()

    # Debitar do saldo mensal do usuário (não bloqueia — falha silenciosa)
    if tokens_gastos > 0:
        import asyncio as _asyncio
        _asyncio.create_task(debitar_tokens(str(user.id), tokens_gastos))

    return {
        "revisao_id": revisao_id,
        "modo": "corpus_fechado",
        "resposta": resposta,
        "fontes_usadas": fontes_usadas,
        "chunks_usados": len(chunks),
    }


@router.post("/{revisao_id}/rascunho")
async def gerar_rascunho_revisao(
    revisao_id: str,
    body: RevisaoRascunhoRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(check_token_quota),   # verifica limite de tokens antes da chamada LLM
):
    revisao = await _get_revisao_ou_404(revisao_id, user.id, db)
    consulta = " ".join(
        parte
        for parte in [body.secao, body.instrucao, revisao.pergunta, revisao.objetivo, revisao.tema]
        if parte
    )
    chunks = await _selecionar_chunks_corpus(revisao_id, consulta, body.max_chunks, db)
    if not chunks:
        raise HTTPException(
            status_code=400,
            detail=(
                "Corpus fechado vazio. Aprove fontes para escrita ou envie arquivos "
                "antes de gerar rascunhos."
            ),
        )

    fontes = await _fontes_dos_chunks(chunks, db)
    contexto, fontes_usadas = _formatar_contexto(chunks, fontes)
    iniciar_contagem_tokens()
    try:
        rascunho = await chamar(
            _system_corpus_fechado(),
            _prompt_rascunho(revisao, body, contexto),
            complexidade=Complexidade.MEDIA,
            max_tokens=2400,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=mensagem_usuario_erro(exc))

    tokens_gastos = obter_tokens_usados()
    revisao.tokens_usados = (revisao.tokens_usados or 0) + tokens_gastos
    await db.commit()

    # Debitar do saldo mensal do usuário (não bloqueia — falha silenciosa)
    if tokens_gastos > 0:
        import asyncio as _asyncio
        _asyncio.create_task(debitar_tokens(str(user.id), tokens_gastos))

    return {
        "revisao_id": revisao_id,
        "secao": body.secao,
        "modo": "corpus_fechado",
        "rascunho": rascunho,
        "fontes_usadas": fontes_usadas,
        "chunks_usados": len(chunks),
    }


@router.delete("/{revisao_id}/fontes/{fonte_id}")
async def deletar_fonte(
    revisao_id: str,
    fonte_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    fonte = await _get_fonte_ou_404(revisao_id, fonte_id, user.id, db)
    arquivo_path = fonte.arquivo_path
    await db.delete(fonte)
    await db.commit()

    if arquivo_path:
        caminho = Path(arquivo_path)
        try:
            base = Path(settings.review_upload_dir).resolve()
            caminho_resolvido = caminho.resolve()
            caminho_resolvido.relative_to(base)
            if caminho_resolvido.exists():
                caminho_resolvido.unlink()
        except (OSError, ValueError):
            pass

    return {"deletado": True, "fonte_id": fonte_id}


@router.get("/{revisao_id}/corpus/resumo")
async def resumo_corpus(
    revisao_id: str,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    await _get_revisao_ou_404(revisao_id, user.id, db)

    fontes_result = await db.execute(
        select(func.count())
        .select_from(RevisaoFonte)
        .where(RevisaoFonte.revisao_id == revisao_id)
    )
    aprovadas_result = await db.execute(
        select(func.count())
        .select_from(RevisaoFonte)
        .where(
            RevisaoFonte.revisao_id == revisao_id,
            RevisaoFonte.aprovada_para_escrita.is_(True),
        )
    )
    chunks_result = await db.execute(
        select(func.count())
        .select_from(RevisaoFonteChunk)
        .join(RevisaoFonte, RevisaoFonte.id == RevisaoFonteChunk.fonte_id)
        .where(RevisaoFonte.revisao_id == revisao_id)
    )
    chars_result = await db.execute(
        select(func.coalesce(func.sum(func.length(RevisaoFonteChunk.texto)), 0))
        .select_from(RevisaoFonteChunk)
        .join(RevisaoFonte, RevisaoFonte.id == RevisaoFonteChunk.fonte_id)
        .where(RevisaoFonte.revisao_id == revisao_id)
    )

    return {
        "revisao_id": revisao_id,
        "fontes_total": fontes_result.scalar() or 0,
        "fontes_aprovadas": aprovadas_result.scalar() or 0,
        "chunks_total": chunks_result.scalar() or 0,
        "caracteres_indexados": chars_result.scalar() or 0,
        "modo": "corpus_fechado",
    }


@router.patch(
    "/{revisao_id}/fontes/{fonte_id}/decisao",
    response_model=RevisaoFontePublica,
)
async def decidir_fonte(
    revisao_id: str,
    fonte_id: str,
    body: RevisaoFonteDecisaoRequest,
    db: AsyncSession = Depends(get_db),
    user: Usuario = Depends(get_verified_user),
):
    fonte = await _get_fonte_ou_404(revisao_id, fonte_id, user.id, db)
    estava_aprovada = fonte.aprovada_para_escrita
    fonte.decisao_humana = body.decisao
    fonte.motivo_decisao = body.motivo
    fonte.aprovada_para_escrita = body.decisao == "incluida"

    if estava_aprovada and not fonte.aprovada_para_escrita:
        await db.execute(
            delete(RevisaoFonteChunk).where(RevisaoFonteChunk.fonte_id == fonte.id)
        )

    db.add(
        RevisaoDecisao(
            revisao_id=revisao_id,
            fonte_id=fonte.id,
            decisao=body.decisao,
            motivo=body.motivo,
            feita_por="humano",
        )
    )
    await db.commit()
    await db.refresh(fonte)
    return _fonte_publica(fonte)

from pydantic import BaseModel, Field, field_validator, EmailStr, ConfigDict
from typing import Any, Optional
from datetime import datetime


class Identificacao(BaseModel):
    idade: Optional[str] = None
    idade_unidade: str = "anos"
    sexo: Optional[str] = None
    genero: Optional[str] = None
    etnia: Optional[str] = None
    ocupacao: Optional[str] = None
    procedencia: Optional[str] = None
    outras_infos: Optional[str] = None
    decl_responsavel: bool = False


class Historia(BaseModel):
    queixa_principal: str = Field(..., max_length=500)
    duracao_sintomas: Optional[str] = Field(None, max_length=200)
    hda: str = Field(..., max_length=5000)
    historico_previo: Optional[str] = Field(None, max_length=3000)
    historia_familiar: Optional[str] = Field(None, max_length=2000)
    historia_psicossocial: Optional[str] = Field(None, max_length=2000)


class IntervencoesAnteriores(BaseModel):
    tratamentos: Optional[str] = None
    medicamentos: Optional[str] = None
    alergias: Optional[str] = None


class Achados(BaseModel):
    exame_geral: str = Field(..., max_length=3000)
    achados_especificos: str = Field(..., max_length=3000)
    sinais_vitais: Optional[str] = Field(None, max_length=500)


class EventoTimeline(BaseModel):
    tempo: Optional[str] = None
    evento: Optional[str] = None
    responsavel: Optional[str] = None


class Timeline(BaseModel):
    formato: str = "relativo"
    saida: str = "tabela"
    eventos: list[EventoTimeline] = []


class Diagnostico(BaseModel):
    exames_lab: Optional[str] = Field(None, max_length=2000)
    exames_imagem: Optional[str] = Field(None, max_length=2000)
    outros_exames: Optional[str] = Field(None, max_length=2000)
    diagnostico_definitivo: str = Field(..., max_length=500)
    diferenciais: Optional[str] = Field(None, max_length=1000)
    desafios: Optional[str] = Field(None, max_length=1000)
    prognostico: Optional[str] = Field(None, max_length=500)


class Intervencao(BaseModel):
    tipo: str = Field(..., max_length=200)
    descricao: str = Field(..., max_length=3000)
    houve_mudanca: bool = False
    desc_mudanca: Optional[str] = Field(None, max_length=2000)
    just_mudanca: Optional[str] = Field(None, max_length=1000)


class Desfechos(BaseModel):
    desfecho_clinico: str = Field(..., max_length=3000)
    exames_seguimento: Optional[str] = Field(None, max_length=2000)
    adesao: Optional[str] = Field(None, max_length=500)
    tempo_acompanhamento: Optional[str] = Field(None, max_length=200)
    eventos_adversos: Optional[str] = Field(None, max_length=2000)


class Editorial(BaseModel):
    problemas_clinicos: str = Field(..., max_length=2000)
    diferencial_caso: str = Field(..., max_length=2000)
    tipo_desfecho: Optional[str] = Field(None, max_length=200)
    consentimento: bool = False
    area_atuacao: Optional[str] = Field(None, max_length=200)
    especialidade: Optional[str] = Field(None, max_length=200)
    periodico: Optional[str] = Field(None, max_length=300)
    formato_ref: str = Field("vancouver", max_length=50)
    tipo_produto: str = Field("Artigo para Periódico", max_length=100)
    email_usuario: Optional[str] = Field(None, max_length=254)  # RFC 5321 max email length


class ImagemCaso(BaseModel):
    numero_figura: int = Field(..., ge=1, le=20)  # máximo 20 figuras por artigo
    legenda: str = Field(..., max_length=1000)
    titulo_abrev: Optional[str] = Field(None, max_length=200)
    filename: Optional[str] = Field(None, max_length=255)


class Autor(BaseModel):
    nome: str = Field(..., max_length=200)
    instituicao: Optional[str] = Field(None, max_length=500)
    orcid: Optional[str] = Field(None, max_length=50)   # formato 0000-XXXX-XXXX-XXXX
    email: Optional[str] = Field(None, max_length=254)
    correspondente: bool = False


class CKO(BaseModel):
    sessao_id: str
    identificacao: Identificacao
    historia: Historia
    intervencoes_anteriores: IntervencoesAnteriores
    achados: Achados
    timeline: Timeline
    diagnostico: Diagnostico
    intervencao: Intervencao
    desfechos: Desfechos
    perspectiva_paciente: Optional[str] = None
    editorial: Editorial
    imagens: list[ImagemCaso] = Field(default=[], max_length=20)
    autores: list[Autor] = Field(default=[], max_length=20)

    @field_validator("sessao_id")
    @classmethod
    def sessao_nao_vazia(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("sessao_id não pode ser vazio")
        return v.strip()


# ── Responses ──

class Referencia(BaseModel):
    """
    Todos os campos têm default para tolerar referências salvas em formatos parciais
    (ex: {numero, formatada} editadas pela UI) sem causar ValidationError no download.
    extra="ignore" descarta quaisquer chaves extras sem crash.
    """
    model_config = ConfigDict(extra="ignore")

    numero: int = 0
    autores: str = ""
    titulo: str = ""
    periodico: str = ""
    ano: str = ""
    volume: Optional[str] = None
    numero_edicao: Optional[str] = None
    paginas: Optional[str] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    formatada: str = ""


class Resumo(BaseModel):
    """
    Todos os campos são opcionais com default "" para tolerar resumos
    parcialmente editados (regex falhou em alguma seção) sem ValidationError.
    """
    model_config = ConfigDict(extra="ignore")

    introducao: str = ""
    caso: str = ""
    discussao: str = ""
    conclusao: str = ""


class ArtigoGerado(BaseModel):
    """
    Campos com defaults seguros para tolerar resultados em qualquer estado
    (recém gerado, parcialmente editado, importado de versão antiga).
    """
    model_config = ConfigDict(extra="ignore")

    titulo: str = ""
    palavras_chave: list[Any] = []   # Any → normalizado para str no download
    resumo: Resumo = Field(default_factory=Resumo)
    introducao: list[Any] = []       # Any → normalizado para str no download
    caso_clinico: list[Any] = []
    discussao: list[Any] = []
    conclusao: list[Any] = []
    referencias: list[Referencia] = []


class RelatorioGerado(BaseModel):
    care_score: int
    care_itens_atendidos: list[str]
    care_itens_faltantes: list[str]
    flags: list[str]
    bases_consultadas: int
    total_referencias: int
    observacoes: list[str]


class IniciarResponse(BaseModel):
    sessao_id: str
    status: str
    mensagem: str


class StatusResponse(BaseModel):
    sessao_id: str
    status: str
    etapa_atual: Optional[int] = None
    resultado: Optional[dict] = None


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    nome: str
    email: str
    senha: str
    lgpd_aceito: bool
    crm_cro: Optional[str] = None

    @field_validator("senha")
    @classmethod
    def senha_forte(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("A senha deve ter pelo menos 8 caracteres.")
        return v

    @field_validator("lgpd_aceito")
    @classmethod
    def lgpd_obrigatorio(cls, v: bool) -> bool:
        if not v:
            raise ValueError("O aceite dos termos de uso e política de privacidade é obrigatório.")
        return v


class LoginRequest(BaseModel):
    email: str
    senha: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    nova_senha: str

    @field_validator("nova_senha")
    @classmethod
    def senha_forte(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("A senha deve ter pelo menos 8 caracteres.")
        return v


class UpdateProfileRequest(BaseModel):
    nome: Optional[str] = None
    crm_cro: Optional[str] = None


class UsuarioPublico(BaseModel):
    id: str
    email: str
    nome: str
    plano: str
    is_verified: bool
    is_admin: bool = False
    google_picture: Optional[str] = None
    crm_cro: Optional[str] = None
    artigos_mes: int
    tokens_mes: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}

    @property
    def tokens_limite(self) -> int:
        from app.models.database import TOKENS_LIMITE
        return TOKENS_LIMITE.get(self.plano, 50_000)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    usuario: UsuarioPublico


class CheckoutRequest(BaseModel):
    plano: str  # "starter" | "pro" | "institucional"


# ── Review Studio ────────────────────────────────────────────────────────────

TIPOS_REVISAO = {"narrativa", "integrativa", "escopo", "estado_da_arte"}
FORMATOS_REFERENCIA = {"vancouver", "abnt", "apa"}
DECISOES_FONTE = {"incluida", "excluida", "duvida"}


class RevisaoCreateRequest(BaseModel):
    tipo: str = Field("narrativa", max_length=50)
    titulo: Optional[str] = Field(None, max_length=200)
    tema: Optional[str] = Field(None, max_length=2000)
    pergunta: Optional[str] = Field(None, max_length=2000)
    objetivo: Optional[str] = Field(None, max_length=3000)
    formato_ref: str = Field("vancouver", max_length=50)

    @field_validator("tipo")
    @classmethod
    def tipo_valido(cls, v: str) -> str:
        valor = (v or "").strip().lower()
        if valor not in TIPOS_REVISAO:
            raise ValueError("Tipo de revisão inválido.")
        return valor

    @field_validator("formato_ref")
    @classmethod
    def formato_ref_valido(cls, v: str) -> str:
        valor = (v or "").strip().lower()
        if valor not in FORMATOS_REFERENCIA:
            raise ValueError("Formato de referência inválido.")
        return valor


class RevisaoUpdateRequest(BaseModel):
    tipo: Optional[str] = Field(None, max_length=50)
    status: Optional[str] = Field(None, max_length=50)
    titulo: Optional[str] = Field(None, max_length=200)
    tema: Optional[str] = Field(None, max_length=2000)
    pergunta: Optional[str] = Field(None, max_length=2000)
    objetivo: Optional[str] = Field(None, max_length=3000)
    mneumonico: Optional[str] = Field(None, max_length=50)
    guideline: Optional[str] = Field(None, max_length=100)
    formato_ref: Optional[str] = Field(None, max_length=50)
    protocolo: Optional[dict] = None
    criterios: Optional[dict] = None
    preferencias: Optional[dict] = None
    checklist: Optional[dict] = None

    @field_validator("tipo")
    @classmethod
    def tipo_valido(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        valor = v.strip().lower()
        if valor not in TIPOS_REVISAO:
            raise ValueError("Tipo de revisão inválido.")
        return valor

    @field_validator("formato_ref")
    @classmethod
    def formato_ref_valido(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        valor = v.strip().lower()
        if valor not in FORMATOS_REFERENCIA:
            raise ValueError("Formato de referência inválido.")
        return valor


class RevisaoPublica(BaseModel):
    id: str
    tipo: str
    status: str
    titulo: Optional[str] = None
    tema: Optional[str] = None
    pergunta: Optional[str] = None
    objetivo: Optional[str] = None
    mneumonico: Optional[str] = None
    guideline: Optional[str] = None
    formato_ref: str
    protocolo: dict = Field(default_factory=dict)
    criterios: dict = Field(default_factory=dict)
    preferencias: dict = Field(default_factory=dict)
    checklist: dict = Field(default_factory=dict)
    tokens_usados: int = 0
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RevisaoFonteCreateRequest(BaseModel):
    origem: str = Field("busca", max_length=50)
    fonte_base: Optional[str] = Field(None, max_length=80)
    titulo: str = Field(..., min_length=3, max_length=1000)
    autores: Optional[str] = Field(None, max_length=2000)
    ano: Optional[str] = Field(None, max_length=20)
    periodico: Optional[str] = Field(None, max_length=500)
    doi: Optional[str] = Field(None, max_length=200)
    pmid: Optional[str] = Field(None, max_length=80)
    url: Optional[str] = Field(None, max_length=2000)
    abstract: Optional[str] = Field(None, max_length=10000)
    metadados: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class RevisaoFontePublica(BaseModel):
    id: str
    revisao_id: str
    origem: str
    fonte_base: Optional[str] = None
    status: str
    titulo: str
    autores: Optional[str] = None
    ano: Optional[str] = None
    periodico: Optional[str] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    url: Optional[str] = None
    abstract: Optional[str] = None
    arquivo_path: Optional[str] = None
    metadados: dict = Field(default_factory=dict)
    tags: list = Field(default_factory=list)
    relevancia_ia: Optional[int] = None
    decisao_ia: Optional[str] = None
    decisao_humana: Optional[str] = None
    motivo_decisao: Optional[str] = None
    aprovada_para_escrita: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RevisaoFonteDecisaoRequest(BaseModel):
    decisao: str = Field(..., max_length=30)
    motivo: Optional[str] = Field(None, max_length=1000)
    aprovada_para_escrita: bool = False

    @field_validator("decisao")
    @classmethod
    def decisao_valida(cls, v: str) -> str:
        valor = (v or "").strip().lower()
        if valor not in DECISOES_FONTE:
            raise ValueError("Decisão inválida.")
        return valor


class RevisaoBuscarFontesRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=300)
    max: int = Field(12, ge=1, le=30)
    fontes: list[str] = Field(default_factory=lambda: ["pubmed", "openalex", "semantic"])


class RevisaoImportarFontesRequest(RevisaoBuscarFontesRequest):
    artigos: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class RevisaoMatrizItemRequest(BaseModel):
    fonte_id: Optional[str] = None
    autor_ano: Optional[str] = Field(None, max_length=200)
    objetivo: Optional[str] = Field(None, max_length=3000)
    metodo: Optional[str] = Field(None, max_length=3000)
    populacao: Optional[str] = Field(None, max_length=3000)
    principais_achados: Optional[str] = Field(None, max_length=5000)
    limitacoes: Optional[str] = Field(None, max_length=3000)
    contribuicao: Optional[str] = Field(None, max_length=3000)
    secao_sugerida: Optional[str] = Field(None, max_length=100)
    qualidade: Optional[str] = Field(None, max_length=100)
    dados_extraidos: dict = Field(default_factory=dict)


class RevisaoMatrizItemPublico(BaseModel):
    id: str
    revisao_id: str
    fonte_id: Optional[str] = None
    autor_ano: Optional[str] = None
    objetivo: Optional[str] = None
    metodo: Optional[str] = None
    populacao: Optional[str] = None
    principais_achados: Optional[str] = None
    limitacoes: Optional[str] = None
    contribuicao: Optional[str] = None
    secao_sugerida: Optional[str] = None
    qualidade: Optional[str] = None
    dados_extraidos: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

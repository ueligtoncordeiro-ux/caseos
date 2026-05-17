from pydantic import BaseModel, field_validator, EmailStr, field_validator
from typing import Optional
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
    queixa_principal: str
    duracao_sintomas: Optional[str] = None
    hda: str
    historico_previo: Optional[str] = None
    historia_familiar: Optional[str] = None
    historia_psicossocial: Optional[str] = None


class IntervencoesAnteriores(BaseModel):
    tratamentos: Optional[str] = None
    medicamentos: Optional[str] = None
    alergias: Optional[str] = None


class Achados(BaseModel):
    exame_geral: str
    achados_especificos: str
    sinais_vitais: Optional[str] = None


class EventoTimeline(BaseModel):
    tempo: Optional[str] = None
    evento: Optional[str] = None
    responsavel: Optional[str] = None


class Timeline(BaseModel):
    formato: str = "relativo"
    saida: str = "tabela"
    eventos: list[EventoTimeline] = []


class Diagnostico(BaseModel):
    exames_lab: Optional[str] = None
    exames_imagem: Optional[str] = None
    outros_exames: Optional[str] = None
    diagnostico_definitivo: str
    diferenciais: Optional[str] = None
    desafios: Optional[str] = None
    prognostico: Optional[str] = None


class Intervencao(BaseModel):
    tipo: str
    descricao: str
    houve_mudanca: bool = False
    desc_mudanca: Optional[str] = None
    just_mudanca: Optional[str] = None


class Desfechos(BaseModel):
    desfecho_clinico: str
    exames_seguimento: Optional[str] = None
    adesao: Optional[str] = None
    tempo_acompanhamento: Optional[str] = None
    eventos_adversos: Optional[str] = None


class Editorial(BaseModel):
    problemas_clinicos: str
    diferencial_caso: str
    tipo_desfecho: Optional[str] = None
    consentimento: bool = False
    area_atuacao: Optional[str] = None
    especialidade: Optional[str] = None
    periodico: Optional[str] = None
    formato_ref: str = "vancouver"
    tipo_produto: str = "Artigo para Periódico"
    email_usuario: Optional[str] = None


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

    @field_validator("sessao_id")
    @classmethod
    def sessao_nao_vazia(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("sessao_id não pode ser vazio")
        return v.strip()


# ── Responses ──

class Referencia(BaseModel):
    numero: int
    autores: str
    titulo: str
    periodico: str
    ano: str
    volume: Optional[str] = None
    numero_edicao: Optional[str] = None
    paginas: Optional[str] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    formatada: str = ""


class Resumo(BaseModel):
    introducao: str
    caso: str
    discussao: str
    conclusao: str


class ArtigoGerado(BaseModel):
    titulo: str
    palavras_chave: list[str]
    resumo: Resumo
    introducao: list[str]
    caso_clinico: list[str]
    discussao: list[str]
    conclusao: list[str]
    referencias: list[Referencia]


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
    google_picture: Optional[str] = None
    crm_cro: Optional[str] = None
    artigos_mes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    usuario: UsuarioPublico


class CheckoutRequest(BaseModel):
    plano: str  # "pro" | "institucional"

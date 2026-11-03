"""CRUD de notas, agenda e lembretes (REST, com X-Jarvis-Token).

As rotas são `async` de propósito: publicam eventos no barramento, que não é
thread-safe. Rodar numa threadpool quebraria isso. O SQLite é rápido o bastante
para ficar na própria thread do loop.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import tempo
from .seguranca import exigir_token
from .servicos import Servicos

router = APIRouter(dependencies=[Depends(exigir_token)])


def _servicos(request: Request) -> Servicos:
    return request.app.state.servicos


def _sem_nulos(dados: dict) -> dict:
    """No PATCH, um campo enviado como null não pode virar NULL no banco."""
    return {chave: valor for chave, valor in dados.items() if valor is not None}


def _quando_com_fuso(quando: datetime, s: Servicos) -> datetime:
    return tempo.com_fuso(quando, s.cfg.zona)


# --- notas ----------------------------------------------------------------

class NotaEntrada(BaseModel):
    titulo: str = Field(min_length=1, max_length=200)
    texto: str = Field(default="", max_length=20000)


class NotaAtualizacao(BaseModel):
    titulo: Optional[str] = Field(default=None, min_length=1, max_length=200)
    texto: Optional[str] = Field(default=None, max_length=20000)


@router.get("/notes")
async def listar_notas(request: Request):
    return _servicos(request).banco.listar("notas")


@router.post("/notes", status_code=201)
async def criar_nota(corpo: NotaEntrada, request: Request):
    s = _servicos(request)
    agora = tempo.agora_armazenado()
    nota = s.banco.inserir("notas", {
        "titulo": corpo.titulo, "texto": corpo.texto,
        "criada_em": agora, "atualizada_em": agora,
    })
    s.barramento.publicar("nota_criada", nota)
    return nota


@router.get("/notes/{id_}")
async def obter_nota(id_: int, request: Request):
    nota = _servicos(request).banco.obter("notas", id_)
    if nota is None:
        raise HTTPException(status_code=404, detail="nota não encontrada")
    return nota


@router.patch("/notes/{id_}")
async def atualizar_nota(id_: int, corpo: NotaAtualizacao, request: Request):
    s = _servicos(request)
    dados = _sem_nulos(corpo.model_dump(exclude_unset=True))
    dados["atualizada_em"] = tempo.agora_armazenado()
    nota = s.banco.atualizar("notas", id_, dados)
    if nota is None:
        raise HTTPException(status_code=404, detail="nota não encontrada")
    s.barramento.publicar("nota_atualizada", nota)
    return nota


@router.delete("/notes/{id_}", status_code=204)
async def apagar_nota(id_: int, request: Request):
    s = _servicos(request)
    if not s.banco.apagar("notas", id_):
        raise HTTPException(status_code=404, detail="nota não encontrada")
    s.barramento.publicar("nota_removida", {"id": id_})
    return Response(status_code=204)


# --- agenda ---------------------------------------------------------------

class AgendaEntrada(BaseModel):
    titulo: str = Field(min_length=1, max_length=200)
    quando: datetime
    descricao: str = Field(default="", max_length=5000)


class AgendaAtualizacao(BaseModel):
    titulo: Optional[str] = Field(default=None, min_length=1, max_length=200)
    quando: Optional[datetime] = None
    descricao: Optional[str] = Field(default=None, max_length=5000)


@router.get("/agenda")
async def listar_agenda(request: Request):
    s = _servicos(request)
    return [s.saida_agenda(item) for item in s.banco.listar("agenda")]


@router.post("/agenda", status_code=201)
async def criar_compromisso(corpo: AgendaEntrada, request: Request):
    s = _servicos(request)
    agora = tempo.agora_armazenado()
    item = s.banco.inserir("agenda", {
        "titulo": corpo.titulo,
        "descricao": corpo.descricao,
        "quando": tempo.para_armazenamento(_quando_com_fuso(corpo.quando, s)),
        "criada_em": agora,
        "atualizada_em": agora,
    })
    saida = s.saida_agenda(item)
    s.barramento.publicar("agenda_criada", saida)
    return saida


@router.get("/agenda/{id_}")
async def obter_compromisso(id_: int, request: Request):
    s = _servicos(request)
    item = s.banco.obter("agenda", id_)
    if item is None:
        raise HTTPException(status_code=404, detail="compromisso não encontrado")
    return s.saida_agenda(item)


@router.patch("/agenda/{id_}")
async def atualizar_compromisso(id_: int, corpo: AgendaAtualizacao, request: Request):
    s = _servicos(request)
    dados = _sem_nulos(corpo.model_dump(exclude_unset=True))
    if "quando" in dados:
        dados["quando"] = tempo.para_armazenamento(_quando_com_fuso(dados["quando"], s))
    dados["atualizada_em"] = tempo.agora_armazenado()
    item = s.banco.atualizar("agenda", id_, dados)
    if item is None:
        raise HTTPException(status_code=404, detail="compromisso não encontrado")
    saida = s.saida_agenda(item)
    s.barramento.publicar("agenda_atualizada", saida)
    return saida


@router.delete("/agenda/{id_}", status_code=204)
async def apagar_compromisso(id_: int, request: Request):
    s = _servicos(request)
    if not s.banco.apagar("agenda", id_):
        raise HTTPException(status_code=404, detail="compromisso não encontrado")
    s.barramento.publicar("agenda_removida", {"id": id_})
    return Response(status_code=204)


# --- lembretes ------------------------------------------------------------

class LembreteEntrada(BaseModel):
    texto: str = Field(min_length=1, max_length=500)
    quando: datetime


class LembreteAtualizacao(BaseModel):
    texto: Optional[str] = Field(default=None, min_length=1, max_length=500)
    quando: Optional[datetime] = None


def _exigir_futuro(quando: datetime) -> None:
    if quando <= tempo.agora_utc():
        raise HTTPException(status_code=422, detail="quando precisa ser no futuro")


@router.get("/reminders")
async def listar_lembretes(request: Request):
    s = _servicos(request)
    return [s.saida_lembrete(item) for item in s.banco.listar("lembretes")]


@router.post("/reminders", status_code=201)
async def criar_lembrete(corpo: LembreteEntrada, request: Request):
    s = _servicos(request)
    quando = _quando_com_fuso(corpo.quando, s)
    _exigir_futuro(quando)
    agora = tempo.agora_armazenado()
    quando_utc = tempo.para_armazenamento(quando)
    item = s.banco.inserir("lembretes", {
        "texto": corpo.texto, "quando": quando_utc,
        "criado_em": agora, "atualizado_em": agora,
    })
    s.agendador.agendar(item["id"], quando)
    saida = s.saida_lembrete(item)
    s.barramento.publicar("lembrete_criado", saida)
    return saida


@router.get("/reminders/{id_}")
async def obter_lembrete(id_: int, request: Request):
    s = _servicos(request)
    item = s.banco.obter("lembretes", id_)
    if item is None:
        raise HTTPException(status_code=404, detail="lembrete não encontrado")
    return s.saida_lembrete(item)


@router.patch("/reminders/{id_}")
async def atualizar_lembrete(id_: int, corpo: LembreteAtualizacao, request: Request):
    s = _servicos(request)
    if s.banco.obter("lembretes", id_) is None:
        raise HTTPException(status_code=404, detail="lembrete não encontrado")
    dados = _sem_nulos(corpo.model_dump(exclude_unset=True))
    quando = None
    if "quando" in dados:
        quando = _quando_com_fuso(dados["quando"], s)
        _exigir_futuro(quando)
        dados["quando"] = tempo.para_armazenamento(quando)
        # Mudou o horário: volta a ser pendente e entra de novo no agendador.
        dados["disparado_em"] = None
    dados["atualizado_em"] = tempo.agora_armazenado()
    item = s.banco.atualizar("lembretes", id_, dados)
    if quando is not None:
        s.agendador.agendar(id_, quando)
    saida = s.saida_lembrete(item)
    s.barramento.publicar("lembrete_atualizado", saida)
    return saida


@router.delete("/reminders/{id_}", status_code=204)
async def apagar_lembrete(id_: int, request: Request):
    s = _servicos(request)
    if not s.banco.apagar("lembretes", id_):
        raise HTTPException(status_code=404, detail="lembrete não encontrado")
    s.agendador.cancelar(id_)
    s.barramento.publicar("lembrete_removido", {"id": id_})
    return Response(status_code=204)

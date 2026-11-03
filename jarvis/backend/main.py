"""Montagem do app FastAPI.

`criar_app()` é a fábrica: o uvicorn a chama com --factory (ver deploy/README.md).
Importar este módulo não cria nada e não lê o .env, então os testes importam à
vontade.
"""
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import rotas_dados, rotas_sistema, sistema
from .config import Config, carregar_config
from .servicos import Servicos

log = logging.getLogger(__name__)


def criar_app(cfg: Optional[Config] = None, provedor=None) -> FastAPI:
    cfg = cfg if cfg is not None else carregar_config()
    servicos = Servicos(cfg, provedor=provedor)

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI):
        servicos.agendador.iniciar()
        reagendados = servicos.restaurar_lembretes()
        log.info("Jarvis no ar: llm=%s, lembretes reagendados=%d",
                 servicos.provedor.nome, reagendados)
        sistema.cpu_percent()  # primeira leitura é 0.0; esta deixa a próxima válida
        yield
        servicos.agendador.encerrar()

    app = FastAPI(title="Jarvis", version="0.1.0", lifespan=ciclo_de_vida)
    app.state.servicos = servicos

    # Só a Tailscale e localhost. A chave continua obrigatória em cada rota.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=cfg.cors_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(rotas_dados.router)
    app.include_router(rotas_sistema.router)
    app.add_api_websocket_route("/ws/chat", rotas_sistema.ws_chat)

    # Por último: o mount em "/" engole tudo que não casou antes.
    app.mount("/", StaticFiles(directory=cfg.tela_dir, html=True), name="tela")
    return app

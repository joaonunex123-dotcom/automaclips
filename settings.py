"""Configuração centralizada — todo número ajustável do pipeline mora aqui.

Regra do projeto: nenhum valor de comportamento fica cravado no meio do
código. Cada um tem um default aqui e é sobrescrevível por variável de
ambiente, para que ajustar o pipeline em produção não exija editar arquivo.

O .env é carregado ANTES de qualquer leitura abaixo. Sem isso, um override
gravado no .env só valeria se algum outro módulo já tivesse chamado
load_dotenv() antes deste arquivo ser importado — ou seja, dependeria da
ordem de import de quem importa primeiro, não da intenção de quem escreveu
o .env.

Os defaults de score/threshold são PLACEHOLDERS calibrados no escuro: o
número certo depende do tamanho dos canais monitorados. A etapa 7
(analytics/recalibrate) existe justamente para substituí-los por números
observados.
"""
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_BASE_DIR, ".env"))
except ImportError:  # pragma: no cover - ambiente sem python-dotenv
    # Não é fatal: quem exporta as variáveis no shell continua funcionando, e
    # a suíte de testes nunca depende do .env.
    pass


def _int(nome, padrao):
    """Lê inteiro do ambiente. Valor vazio ou inválido cai no default.

    Vazio cai no default de propósito: o .env.example lista as chaves sem
    valor, e uma linha `SCORE_THRESHOLD=` copiada sem preencher não deve
    derrubar o processo — deve simplesmente não sobrescrever nada.
    """
    bruto = (os.getenv(nome) or "").strip()
    if not bruto:
        return padrao
    try:
        return int(bruto)
    except ValueError:
        return padrao


def _float(nome, padrao):
    bruto = (os.getenv(nome) or "").strip()
    if not bruto:
        return padrao
    try:
        return float(bruto)
    except ValueError:
        return padrao


def _bool(nome, padrao):
    bruto = (os.getenv(nome) or "").strip().lower()
    if not bruto:
        return padrao
    return bruto in ("1", "true", "sim", "on", "yes")


def _caminho(nome, padrao):
    bruto = (os.getenv(nome) or "").strip()
    return bruto or padrao


# --- chaves de API ------------------------------------------------------------
#
# Lidas por getenv direto (sem default) porque não existe valor razoável de
# fallback: sem chave, quem precisa dela falha com mensagem clara.
YOUTUBE_API_KEY = (os.getenv("YOUTUBE_API_KEY") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

# --- modo sombra --------------------------------------------------------------
#
# False = gera o clip inteiro e para antes de postar. É o estado padrão até a
# etapa 6: o custo de um clip ruim publicado é permanente (fica no canal, conta
# para o algoritmo), o de um clip ruim renderizado é um arquivo em disco.
AUTO_PUBLISH = _bool("AUTO_PUBLISH", False)

# --- caminhos -----------------------------------------------------------------

DB_PATH = _caminho("CLIPS_DB_PATH", os.path.join(_BASE_DIR, "clips.db"))
SCHEMA_PATH = os.path.join(_BASE_DIR, "db", "schema.sql")
CANAIS_PATH = _caminho("CANAIS_PATH", os.path.join(_BASE_DIR, "sourcing", "canais.json"))

# --- sourcing -----------------------------------------------------------------

# Corte do score, em views ganhas por hora. Vídeo abaixo disto é gravado (com
# status ABAIXO_DO_LIMIAR) mas não entra na fila de processamento — ver
# db/schema.sql para o porquê de gravar em vez de descartar.
SCORE_THRESHOLD = _float("SCORE_THRESHOLD", 500.0)

# Uploads recentes olhados por canal em cada varredura. 50 é o teto de uma
# página da API do YouTube; passar disso custaria uma chamada a mais por canal
# sem ganho — vídeo antigo é filtrado por idade logo em seguida de qualquer
# forma.
MAX_VIDEOS_POR_CANAL = _int("SOURCING_MAX_VIDEOS_POR_CANAL", 20)

# Janela de momento. Depois disto o vídeo já foi distribuído pelo algoritmo da
# plataforma de origem e não tem mais aceleração para capturar.
IDADE_MAXIMA_HORAS = _float("SOURCING_IDADE_MAXIMA_HORAS", 72.0)

# Piso do denominador do score. Sem ele, um vídeo publicado há 3 minutos com 20
# views marca 400 views/h e ganha de um vídeo com 30 mil views em 24 h — o
# score vira medida de ruído de amostragem, não de tração.
IDADE_MINIMA_HORAS = _float("SOURCING_IDADE_MINIMA_HORAS", 1.0)

# Faixa de duração do vídeo-FONTE, em segundos.
#   mínimo  abaixo de 3 min quase sempre é Short/Reel republicado: já é vertical
#           e curto, não há o que recortar.
#   máximo  4 h de transcrição é caro em API e em tempo, e o highlight_detect da
#           etapa 2 manda a transcrição inteira num prompt só.
DURACAO_MINIMA_S = _int("SOURCING_DURACAO_MINIMA_S", 180)
DURACAO_MAXIMA_S = _int("SOURCING_DURACAO_MAXIMA_S", 14400)

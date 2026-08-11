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
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()

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

# --- pipeline: caminhos de mídia ----------------------------------------------
#
# Fora do git (ver .gitignore): são gigabytes, e tudo aqui é reconstruível a
# partir da fila.
DOWNLOADS_DIR = _caminho("CLIPS_DOWNLOADS_DIR", os.path.join(_BASE_DIR, "downloads"))
TRANSCRICOES_DIR = _caminho(
    "CLIPS_TRANSCRICOES_DIR", os.path.join(_BASE_DIR, "downloads", "transcricoes")
)

# --- pipeline: download -------------------------------------------------------

# Seletor de formato do yt-dlp. Teto de 1080p: o clip final é vertical e
# recortado, então resolução acima disso é banda e disco gastos num pixel que
# nunca chega ao vídeo publicado.
YTDLP_FORMATO = _caminho(
    "YTDLP_FORMATO", "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
)

# Áudio extraído para transcrição e análise de energia. 16 kHz mono é o que o
# Whisper consome internamente — entregar já nesse formato evita uma
# reamostragem por execução, e serve igualmente bem ao librosa.
AUDIO_SAMPLE_RATE = _int("CLIPS_AUDIO_SAMPLE_RATE", 16000)

# Vídeos processados por execução do pipeline. Teto existe para uma fila
# grande não virar uma execução de horas sem checkpoint — cada vídeo é
# commitado ao terminar, então a execução seguinte continua de onde parou.
PIPELINE_MAX_VIDEOS = _int("PIPELINE_MAX_VIDEOS", 5)

# --- pipeline: transcrição ----------------------------------------------------
#
# faster-whisper. 'small' equilibra qualidade e tempo em CPU; 'medium'/'large-v3'
# valem a pena com GPU (WHISPER_DEVICE=cuda).
WHISPER_MODELO = _caminho("WHISPER_MODELO", "small")
WHISPER_DEVICE = _caminho("WHISPER_DEVICE", "cpu")
# int8 é o que torna CPU viável; com cuda, use float16.
WHISPER_COMPUTE_TYPE = _caminho("WHISPER_COMPUTE_TYPE", "int8")
# Vazio = detecção automática por áudio. Fixar o idioma quando todos os canais
# monitorados falam a mesma língua economiza a passada de detecção e evita o
# erro clássico de um trecho musical ser detectado como outro idioma.
WHISPER_IDIOMA = _caminho("WHISPER_IDIOMA", "")

# --- pipeline: transcrição pela API da OpenAI ---------------------------------
#
# 'local' (faster-whisper) | 'openai' (Whisper API) | '' (decide sozinho).
#
# O padrão automático usa a API quando há OPENAI_API_KEY no ambiente e cai no
# local quando não há. Sem GPU, transcrever 4 h de áudio em CPU leva horas — a
# API resolve em minutos —, mas ligá-la por padrão numa máquina sem chave
# quebraria o pipeline em vez de degradá-lo.
TRANSCRICAO_BACKEND = _caminho("TRANSCRICAO_BACKEND", "")

# whisper-1 e não os modelos de transcrição mais novos: é o que aceita
# `timestamp_granularities=["word"]`, e sem timestamp por palavra a legenda
# word-by-word da etapa 3 não existe. Trocar por um modelo sem esse suporte
# quebra a etapa 3, não esta.
OPENAI_WHISPER_MODELO = _caminho("OPENAI_WHISPER_MODELO", "whisper-1")

# Teto de upload da API, em MB. O áudio é comprimido para mp3 mono antes de
# subir; o que ainda passar disso é fatiado.
OPENAI_UPLOAD_MAX_MB = _float("OPENAI_UPLOAD_MAX_MB", 24.0)

# Bitrate do mp3 enviado. 32 kbps mono é generoso para fala e derruba o
# tamanho em ~50x contra o wav de trabalho — a API transcreve igual, e o wav
# original continua em disco para o librosa.
OPENAI_MP3_BITRATE = _caminho("OPENAI_MP3_BITRATE", "32k")

# Duração máxima de cada fatia. A 32 kbps, 30 min dão ~7 MB: bem abaixo do
# teto, com folga para áudio que comprime mal.
OPENAI_CHUNK_MAX_S = _float("OPENAI_CHUNK_MAX_S", 1800.0)

# Onde procurar silêncio para cortar a fatia. Cortar no meio de uma palavra
# estraga uma palavra por fronteira; encostar o corte no silêncio mais próximo
# custa uma passada de análise do ffmpeg e evita isso.
OPENAI_CORTE_SILENCIO_DB = _caminho("OPENAI_CORTE_SILENCIO_DB", "-35dB")
OPENAI_CORTE_SILENCIO_MIN_S = _float("OPENAI_CORTE_SILENCIO_MIN_S", 0.35)
# Quanto o corte pode se afastar do alvo para achar silêncio.
OPENAI_CORTE_TOLERANCIA_S = _float("OPENAI_CORTE_TOLERANCIA_S", 120.0)

# --- orçamento da API paga ----------------------------------------------------
#
# Preço publicado do whisper-1, em USD por minuto de áudio. CONFERIR contra a
# tabela vigente da OpenAI antes de confiar no número: é daqui que sai tanto a
# estimativa quanto o corte de orçamento.
WHISPER_API_USD_POR_MINUTO = _float("WHISPER_API_USD_POR_MINUTO", 0.006)

# Teto de gasto acumulado (tabela `custos`). A transcrição que ultrapassaria o
# teto é RECUSADA antes de subir o arquivo — o vídeo fica em 'falha' com o
# motivo, em vez de o saldo acabar no meio da fila e metade dos vídeos voltarem
# com erro de billing. 0 desliga a guarda.
ORCAMENTO_USD = _float("ORCAMENTO_USD", 10.0)

# --- pipeline: highlight_detect (Claude) --------------------------------------

# Ver docs/ e pipeline/highlight_detect.py. Opus 5 tem contexto de 1M, o que
# cobre a transcrição inteira de um vídeo longo num prompt só — que é o que o
# spec pede (nada de janela deslizante perdendo o fio da conversa).
CLAUDE_MODELO = _caminho("CLAUDE_MODELO", "claude-opus-5")

# low | medium | high | xhigh | max. 'high' é o default da API e o piso
# recomendado para trabalho sensível a julgamento — escolher trecho viral é
# exatamente isso. Vale varrer para baixo depois de ter clips medidos.
CLAUDE_EFFORT = _caminho("CLAUDE_EFFORT", "high")

# Teto de saída. A resposta é um JSON de N trechos (pequeno), mas no Opus 5 o
# max_tokens limita raciocínio + texto JUNTOS: apertá-lo trunca a resposta no
# meio do raciocínio e não sobra JSON nenhum.
CLAUDE_MAX_TOKENS = _int("CLAUDE_MAX_TOKENS", 16000)

# Retentativas do SDK para 429/5xx/erro de conexão (o default do SDK é 2).
CLAUDE_MAX_RETRIES = _int("CLAUDE_MAX_RETRIES", 3)

# Fallback server-side: se os classificadores de segurança recusarem o pedido,
# a API refaz a chamada num modelo de fallback dentro da mesma requisição, em
# vez de devolver a recusa. Transcrição de canal aberto é conteúdo que não
# controlamos — a recusa é rara, mas quando acontece derrubaria o vídeo inteiro
# por algo que ninguém escolheu. Desligue com CLAUDE_FALLBACKS=0 se o beta
# causar problema.
CLAUDE_FALLBACKS = _bool("CLAUDE_FALLBACKS", True)

# Quantos trechos pedir por vídeo. Pedir mais do que se pretende usar é
# deliberado: o corte por threshold e a resolução de sobreposição em
# select_clips descartam parte, e é melhor descartar do que ficar sem.
CLIPS_POR_VIDEO = _int("CLIPS_POR_VIDEO", 8)

# Faixa de duração do TRECHO (não do vídeo-fonte), em segundos.
CLIP_DURACAO_MINIMA_S = _float("CLIP_DURACAO_MINIMA_S", 30.0)
CLIP_DURACAO_MAXIMA_S = _float("CLIP_DURACAO_MAXIMA_S", 60.0)

# Corte do score_final (escala 0–10). Trecho abaixo é gravado como descartado.
CLIP_SCORE_THRESHOLD = _float("CLIP_SCORE_THRESHOLD", 6.0)

# --- OpenRouter: modelos das etapas de menor exigência -----------------------
#
# O `highlight_detect` continua no Claude direto (é a decisão que define o
# produto). O que passa por aqui é o trabalho de menor exigência — escrever
# metadado e recalibrar —, onde modelo mais barato resolve.
#
# Os slugs abaixo são os que o João escolheu. NÃO foram verificados contra o
# catálogo do OpenRouter a partir daqui: se um estiver errado, a chamada volta
# 404 com o nome do modelo na mensagem. Por isso são sobrescrevíveis por
# ambiente — corrigir um slug é mexer no .env, não no código.
OPENROUTER_BASE_URL = _caminho("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

MODEL_METADATA = _caminho("MODEL_METADATA", "deepseek/deepseek-v4-flash")
MODEL_RECALIBRATE = _caminho("MODEL_RECALIBRATE", "z-ai/glm-5")

# Usado quando o modelo principal devolve algo que não dá para interpretar.
# Sem saída estruturada garantida (que era o que a API da Anthropic dava), a
# resposta malformada deixa de ser impossível e passa a ser rara — o fallback
# é o que impede que "rara" vire "perdeu o clip".
MODEL_FALLBACK = _caminho("MODEL_FALLBACK", "anthropic/claude-sonnet-4-6")

# Retentativas do SDK para 429/5xx no OpenRouter.
OPENROUTER_MAX_RETRIES = _int("OPENROUTER_MAX_RETRIES", 3)

# Guarda de custo: transcrição acima disto não é enviada ao Claude. Um podcast
# de 4 h dá ~50k tokens e passa folgado; o teto existe para o caso patológico
# (legenda automática duplicada, live de 12 h) não virar uma chamada cara sem
# ninguém decidir isso.
TRANSCRICAO_MAX_TOKENS = _int("TRANSCRICAO_MAX_TOKENS", 200000)

# --- pipeline: energia de áudio (confirmação dos trechos) ---------------------

# Janela do RMS, em segundos. 0,25 s captura a dinâmica de fala e reação sem
# transformar cada sílaba num pico.
ENERGIA_JANELA_S = _float("ENERGIA_JANELA_S", 0.25)

# Um quadro é pico se seu RMS ficar acima deste percentil do vídeo inteiro. É
# relativo de propósito: canal com áudio comprimido e canal com faixa dinâmica
# larga não compartilham um limiar absoluto.
ENERGIA_PERCENTIL = _float("ENERGIA_PERCENTIL", 92.0)

# Distância mínima entre dois picos. Sem isso, uma única gargalhada vira vinte
# picos e a densidade deixa de medir quantos MOMENTOS o trecho tem.
ENERGIA_DISTANCIA_MINIMA_S = _float("ENERGIA_DISTANCIA_MINIMA_S", 1.5)

# Fator aplicado ao score do Claude conforme a densidade de picos no trecho
# (ver pipeline/select_clips.fator_energia). O mínimo é uma PENALIDADE, não um
# veto: trecho sem pico nenhum costuma ser fala parada, mas às vezes é uma
# revelação em voz baixa — perder 30% do score deixa um trecho excelente ainda
# competitivo, um veto o mataria.
FATOR_ENERGIA_MIN = _float("FATOR_ENERGIA_MIN", 0.70)
FATOR_ENERGIA_MAX = _float("FATOR_ENERGIA_MAX", 1.15)

# Densidade (picos por minuto) a partir da qual o fator satura em MAX.
DENSIDADE_PICOS_PLENA = _float("DENSIDADE_PICOS_PLENA", 4.0)

# --- editing (etapa 3) --------------------------------------------------------
#
# Aqui ficam só caminho e volume. TODO parâmetro visual — fonte, cor, posição,
# reframe, zoom, watermark, codec — mora em editing/template_config.json, por
# decisão do projeto: ajustar o visual do clip não pode exigir editar código
# nem mexer no .env.
TEMPLATE_CONFIG_PATH = _caminho(
    "TEMPLATE_CONFIG_PATH", os.path.join(_BASE_DIR, "editing", "template_config.json")
)
RENDER_DIR = _caminho("CLIPS_RENDER_DIR", os.path.join(_BASE_DIR, "render"))

# Clips renderizados por execução. Render é a etapa mais cara em tempo de CPU
# do pipeline inteiro; o teto existe para a execução ter fim previsível.
EDITING_MAX_CLIPS = _int("EDITING_MAX_CLIPS", 10)


def _lista(nome, padrao):
    """Lista separada por vírgula no ambiente. Vazio cai no default."""
    bruto = (os.getenv(nome) or "").strip()
    if not bruto:
        return list(padrao)
    return [item.strip() for item in bruto.split(",") if item.strip()]


# --- publish (etapa 5) --------------------------------------------------------

PLATAFORMA_YOUTUBE = "youtube"
PLATAFORMA_INSTAGRAM = "instagram"

# Para onde publicar. Remover uma daqui é o jeito de rodar só num canal.
PLATAFORMAS = _lista("PLATAFORMAS", [PLATAFORMA_YOUTUBE, PLATAFORMA_INSTAGRAM])

# Publicações por dia, por plataforma.
POSTS_POR_DIA = _int("POSTS_POR_DIA", 3)

# Horários de queda, em HH:MM local. São o FALLBACK: a partir da etapa 7 o
# scheduler prefere os horários que o histórico de engajamento mostrar
# melhores, e só cai aqui enquanto não houver histórico. Estes três são
# palpite de horário de pico de consumo — substitua pelos seus assim que a
# etapa 7 tiver dado.
HORARIOS_PADRAO = _lista("HORARIOS_PADRAO", ["12:00", "18:00", "21:00"])

# Distância mínima entre duas publicações na MESMA plataforma. Dois clips
# seguidos competem entre si pela mesma audiência no feed.
INTERVALO_MINIMO_MIN = _int("INTERVALO_MINIMO_MIN", 120)

# Quantos dias à frente o scheduler pode agendar. Sem teto, uma fila grande
# marcaria posts para daqui a meses — e clip de assunto quente não sobrevive
# a isso.
AGENDAMENTO_MAX_DIAS = _int("AGENDAMENTO_MAX_DIAS", 7)

# --- publish: quota do YouTube ------------------------------------------------

# Números publicados pela API v3: cada upload custa 1600 unidades de um teto
# diário de 10.000. Na prática, seis uploads por dia.
YOUTUBE_QUOTA_DIARIA = _int("YOUTUBE_QUOTA_DIARIA", 10000)
YOUTUBE_CUSTO_UPLOAD = _int("YOUTUBE_CUSTO_UPLOAD", 1600)

# A quota do YouTube zera à meia-noite do PACÍFICO, não do fuso local. Usar a
# data daqui faria o contador virar em outro momento que o teto de verdade —
# em parte do ano, três horas de diferença. Ver publish/quota.py.
QUOTA_FUSO = _caminho("QUOTA_FUSO", "America/Los_Angeles")

# --- publish: YouTube ---------------------------------------------------------
#
# Upload exige OAuth, não a chave de API que o sourcing usa: são credenciais
# diferentes, do mesmo projeto do Google Cloud.
YOUTUBE_CLIENT_SECRETS = _caminho(
    "YOUTUBE_CLIENT_SECRETS", os.path.join(_BASE_DIR, "client_secrets.json")
)
YOUTUBE_OAUTH_TOKEN = _caminho(
    "YOUTUBE_OAUTH_TOKEN", os.path.join(_BASE_DIR, "youtube_token.json")
)
# 22 = People & Blogs. 'private' até você conferir os primeiros clips; troque
# para 'public' quando confiar na fila.
YOUTUBE_CATEGORIA = _caminho("YOUTUBE_CATEGORIA", "22")
YOUTUBE_PRIVACIDADE = _caminho("YOUTUBE_PRIVACIDADE", "private")

# --- publish: Instagram -------------------------------------------------------

INSTAGRAM_USER_ID = _caminho("INSTAGRAM_USER_ID", "")
INSTAGRAM_APP_ID = _caminho("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET = _caminho("INSTAGRAM_APP_SECRET", "")
# Token de longa duração INICIAL. Depois da primeira renovação o valor válido
# passa a viver na tabela `tokens` — um segredo que o programa reescreve não
# cabe num arquivo que o humano edita.
INSTAGRAM_TOKEN_INICIAL = _caminho("INSTAGRAM_TOKEN_INICIAL", "")
INSTAGRAM_API_BASE = _caminho("INSTAGRAM_API_BASE", "https://graph.instagram.com")
# O token dura ~60 dias e pode ser renovado a partir de 24 h de vida. Renovar
# com folga evita que uma semana sem rodar o pipeline deixe o token morrer.
INSTAGRAM_RENOVAR_ANTES_DIAS = _int("INSTAGRAM_RENOVAR_ANTES_DIAS", 10)

# O clip precisa estar acessível por URL pública para o Instagram baixá-lo: a
# API não aceita upload direto de arquivo. Vazio = publicação no Instagram
# fica indisponível (e o modo sombra avisa em vez de falhar no dia D).
CLIPS_BASE_URL = _caminho("CLIPS_BASE_URL", "")

# --- publish: metadata --------------------------------------------------------

# Limites das plataformas, para o texto ser cortado aqui e não recusado lá.
LIMITE_TITULO_YOUTUBE = _int("LIMITE_TITULO_YOUTUBE", 100)
LIMITE_DESCRICAO_YOUTUBE = _int("LIMITE_DESCRICAO_YOUTUBE", 5000)
LIMITE_CAPTION_INSTAGRAM = _int("LIMITE_CAPTION_INSTAGRAM", 2200)
MAX_HASHTAGS = _int("MAX_HASHTAGS", 8)

# Publicações processadas por execução.
PUBLISH_MAX_POR_EXECUCAO = _int("PUBLISH_MAX_POR_EXECUCAO", 10)

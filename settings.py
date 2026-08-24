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


def _lista(nome, padrao):
    """Lista separada por vírgula no ambiente. Vazio cai no default."""
    bruto = (os.getenv(nome) or "").strip()
    if not bruto:
        return list(padrao)
    return [item.strip() for item in bruto.split(",") if item.strip()]


def _mapa_int(nome, padrao=None):
    """'youtube=3,tiktok=2' -> {'youtube': 3, 'tiktok': 2}.

    Serve aos tetos que são número POR PLATAFORMA. A quota do YouTube, o rate
    limit do Instagram e o do TikTok são independentes; um número só obrigaria
    a plataforma mais restrita a ditar o ritmo das outras.

    Item torto é ignorado, um a um, pelo mesmo motivo de `_int`: uma linha
    copiada pela metade do .env.example não deve derrubar o import — o
    resolvedor cai no valor global e o pipeline continua de pé.
    """
    saida = dict(padrao or {})
    for item in _lista(nome, []):
        chave, _, valor = item.partition("=")
        try:
            saida[chave.strip()] = int(valor)
        except ValueError:
            continue
    return saida


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

# --- publish: freios da publicacao real (etapa 6) -----------------------------
#
# A partir daqui o programa produz post PUBLICO e irreversivel. Os tres freios
# abaixo sao independentes de proposito: cada um cobre uma falha que os outros
# nao cobrem.

# 1) Freio de emergencia. A presenca deste arquivo na raiz bloqueia TODA
# publicacao na hora, sem precisar editar .env nem parar processo. Checado
# ANTES de qualquer outra coisa, inclusive antes do AUTO_PUBLISH: emergencia
# nao negocia com configuracao. Nao versionado (ver .gitignore).
ARQUIVO_PARAR_PUBLICACAO = os.path.join(_BASE_DIR, "PARAR_PUBLICACAO")

# 2) Teto absoluto por dia e por plataforma, independente do scheduler. O
# scheduler ja limita pelos horarios, mas ele confia na propria agenda; se um
# bug marcar quinze posts para o mesmo dia, e este numero que impede os quinze
# de sairem. Backstop, nao configuracao de ritmo -- deixe folgado em relacao a
# POSTS_POR_DIA.
MAX_POSTS_DIA_ABSOLUTO = _int("MAX_POSTS_DIA_ABSOLUTO", 6)
# Override por plataforma: 'tiktok=4,instagram=2'. Cada plataforma tem quota e
# rate limit próprios; o global vale para quem não estiver listado.
MAX_POSTS_DIA_ABSOLUTO_PLATAFORMA = _mapa_int("MAX_POSTS_DIA_ABSOLUTO_PLATAFORMA")


def max_posts_dia(plataforma):
    """O teto absoluto DAQUELA plataforma, caindo no global quando não há."""
    return MAX_POSTS_DIA_ABSOLUTO_PLATAFORMA.get(plataforma,
                                                 MAX_POSTS_DIA_ABSOLUTO)


# 3) Periodo de aquecimento: nos primeiros dias de publicacao real, no maximo
# este numero de posts por dia. Publicar no volume cheio antes de ver como os
# primeiros clips performam e apostar a reputacao do canal num template que
# ninguem conferiu. 0 desliga.
AQUECIMENTO_POSTS_DIA = _int("AQUECIMENTO_POSTS_DIA", 1)
# Quantos dias o aquecimento dura, contados do primeiro post publicado.
AQUECIMENTO_DIAS = _int("AQUECIMENTO_DIAS", 3)

# --- orchestrator (etapa 6) ---------------------------------------------------
#
# Intervalos do laco principal, em minutos. O sourcing e o unico que segue o
# ritmo do spec (a cada 6h); os outros rodam mais vezes porque processam fila e
# uma passada vazia custa quase nada.
INTERVALO_SOURCING_MIN = _int("INTERVALO_SOURCING_MIN", 360)
INTERVALO_PIPELINE_MIN = _int("INTERVALO_PIPELINE_MIN", 60)
INTERVALO_EDITING_MIN = _int("INTERVALO_EDITING_MIN", 60)
INTERVALO_PUBLISH_MIN = _int("INTERVALO_PUBLISH_MIN", 15)
# Analytics roda uma vez por dia (etapa 7). Hora local, formato HH:MM.
HORARIO_ANALYTICS = _caminho("HORARIO_ANALYTICS", "05:00")

# --- analytics e recalibracao (etapa 7) ---------------------------------------

# Idade minima de um post para ele entrar na recalibracao. Post de duas horas
# ainda esta na janela de distribuicao inicial da plataforma; comparar um de 2h
# com um de 3 dias mede a idade, nao a qualidade do clip.
ANALYTICS_IDADE_MINIMA_H = _float("ANALYTICS_IDADE_MINIMA_H", 48.0)

# Ate quando remedir um post. Depois disso a curva ja estabilizou e cada
# medicao nova gasta quota para confirmar o que ja se sabe.
ANALYTICS_IDADE_MAXIMA_H = _float("ANALYTICS_IDADE_MAXIMA_H", 720.0)

# Piso do denominador do desempenho (views por hora), mesma logica do score de
# sourcing: sem ele um post de 20 minutos com 50 views marca 150/h e lidera o
# ranking por ruido de amostragem.
ANALYTICS_HORAS_MINIMAS = _float("ANALYTICS_HORAS_MINIMAS", 6.0)

# Retencao media exige a YouTube Analytics API, que e OUTRO escopo de OAuth
# (yt-analytics.readonly) e outra autorizacao. Desligada por padrao: sem ela a
# coluna `retencao` fica NULL e a recalibracao de duracao degrada para
# views/hora por faixa, que e pior mas nao e chute.
ANALYTICS_RETENCAO = _bool("ANALYTICS_RETENCAO", False)

# --- recalibracao: quanto dado e "dado suficiente" ----------------------------
#
# Todos os minimos abaixo existem pelo mesmo motivo: recalibrar com tres clips
# nao aprende nada e ainda estraga o que estava funcionando. Abaixo do minimo,
# cada recalibracao simplesmente NAO acontece e o default do settings continua
# valendo.

# Few-shot: percentil de corte e quantos clips medidos sao necessarios antes de
# alimentar o prompt do highlight_detect.
RECALIBRAR_PERCENTIL_TOPO = _float("RECALIBRAR_PERCENTIL_TOPO", 90.0)
RECALIBRAR_MIN_CLIPS = _int("RECALIBRAR_MIN_CLIPS", 20)
RECALIBRAR_MAX_EXEMPLOS = _int("RECALIBRAR_MAX_EXEMPLOS", 5)

# Desativacao de canal: minimo de clips DAQUELE canal, e o quao abaixo da
# mediana geral ele precisa estar. 0.5 = rende menos da metade do tipico.
RECALIBRAR_MIN_CLIPS_CANAL = _int("RECALIBRAR_MIN_CLIPS_CANAL", 5)
RECALIBRAR_FRACAO_CANAL_RUIM = _float("RECALIBRAR_FRACAO_CANAL_RUIM", 0.5)

# Duracao ideal: largura da faixa em segundos, e minimo de clips por faixa para
# ela ser comparavel.
RECALIBRAR_FAIXA_DURACAO_S = _float("RECALIBRAR_FAIXA_DURACAO_S", 10.0)
RECALIBRAR_MIN_CLIPS_FAIXA = _int("RECALIBRAR_MIN_CLIPS_FAIXA", 5)

# Pesos de horario para o scheduler: minimo de posts naquele horario.
RECALIBRAR_MIN_POSTS_HORARIO = _int("RECALIBRAR_MIN_POSTS_HORARIO", 3)

# Chaves gravadas na tabela `calibracao`.
CALIBRACAO_DURACAO_MIN = "clip_duracao_minima_s"
CALIBRACAO_DURACAO_MAX = "clip_duracao_maxima_s"
CALIBRACAO_LICOES = "licoes_do_historico"

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

# --- transcricao pelas legendas do YouTube ------------------------------------
#
# O backend mais barato dos tres: nao usa modelo, nao gasta API, nao gasta CPU.
# O yt-dlp busca a legenda que o proprio YouTube ja tem, sem chave nenhuma.
#
# Idiomas tentados, na ordem. O yt-dlp aceita curinga ('pt.*' pega pt, pt-BR e
# pt-PT), o que evita perder legenda por causa da variante do codigo.
YOUTUBE_LEGENDAS_IDIOMAS = _lista(
    "YOUTUBE_LEGENDAS_IDIOMAS", ["pt-BR", "pt", "pt.*", "en"]
)

# 'auto' | 'manual', e a escolha NAO e obvia:
#   auto    legenda gerada por reconhecimento de fala. Texto pior (erra nome
#           proprio, nao pontua), mas e a UNICA que traz timestamp por PALAVRA
#           -- sem ele, a legenda word-by-word do template nao existe.
#   manual  legenda enviada pelo canal. Texto muito melhor, timestamp so por
#           frase: o clip sai legendado e sincronizado, sem o destaque andando.
#
# Padrao 'auto' porque o destaque e o que o template promete.
YOUTUBE_LEGENDAS_PREFERIR = _caminho("YOUTUBE_LEGENDAS_PREFERIR", "auto")

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
OPENAI_BASE_URL = _caminho("OPENAI_BASE_URL", "https://api.openai.com/v1")

# 'openrouter' | 'openai'. Quem paga a conta do metadado e da recalibracao.
# Sao compativeis pelo mesmo SDK -- muda a base_url e a chave.
LLM_PROVEDOR = _caminho("LLM_PROVEDOR", "openrouter")

# Quem escolhe o TRECHO: 'anthropic' (Claude direto) ou 'openai'.
#
# O padrao continua anthropic, e nao por inercia: escolher o trecho e a decisao
# que define o produto, e o caminho do Claude usa saida estruturada GARANTIDA,
# cache de prompt e fallback server-side, nenhum dos quais existe do outro
# lado. Trocar e uma decisao de custo consciente, nao de conveniencia -- pelo
# caminho openai a resposta passa a ser JSON PEDIDO, nao garantido, e depende
# do extrator tolerante e do modelo de fallback.
HIGHLIGHT_PROVEDOR = _caminho("HIGHLIGHT_PROVEDOR", "anthropic")

# Modelo usado quando HIGHLIGHT_PROVEDOR nao e anthropic. NAO verificado
# contra o catalogo vigente da OpenAI a partir daqui: se o slug estiver errado
# a chamada volta 404 com o nome dentro, e corrigir e mexer no .env.
MODEL_HIGHLIGHT = _caminho("MODEL_HIGHLIGHT", "gpt-4.1")

# Quantos caracteres de transcricao equivalem a um token, aproximadamente.
# Usado SO no caminho openai, onde nao ha um contador de tokens do provedor a
# mao: a guarda de custo vira estimativa em vez de medida. Conservador de
# proposito -- errar para menos deixaria passar prompt maior que o teto.
CARACTERES_POR_TOKEN = _float("CARACTERES_POR_TOKEN", 3.0)

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


# --- publish (etapa 5) --------------------------------------------------------

PLATAFORMA_YOUTUBE = "youtube"
PLATAFORMA_INSTAGRAM = "instagram"
PLATAFORMA_TIKTOK = "tiktok"

# O TikTok entra por uma chave própria e chega DESLIGADO: publicar nele exige
# um app aprovado na TikTok (ver a seção "publish: TikTok" abaixo), e ligá-lo
# por padrão faria toda instalação existente passar a agendar posts para uma
# plataforma que ainda vai recusá-los.
PUBLICAR_TIKTOK = _bool("PUBLICAR_TIKTOK", False)

_PLATAFORMAS_PADRAO = [PLATAFORMA_YOUTUBE, PLATAFORMA_INSTAGRAM]
if PUBLICAR_TIKTOK:
    _PLATAFORMAS_PADRAO.append(PLATAFORMA_TIKTOK)

# Para onde publicar. Remover uma daqui é o jeito de rodar só num canal —
# e uma lista explícita aqui ganha do PUBLICAR_TIKTOK, que só mexe no padrão.
PLATAFORMAS = _lista("PLATAFORMAS", _PLATAFORMAS_PADRAO)

# Publicações por dia, por plataforma. O número é o RITMO (quantos horários de
# queda o scheduler pode usar num dia), não o freio: o backstop é o
# MAX_POSTS_DIA_ABSOLUTO lá em cima.
POSTS_POR_DIA = _int("POSTS_POR_DIA", 3)
# Override por plataforma: 'tiktok=4,instagram=2'. Existe porque as três
# plataformas não têm o mesmo apetite — o YouTube tem quota dura por dia, o
# TikTok limita publicações por token, e o Instagram, chamadas por hora.
POSTS_POR_DIA_PLATAFORMA = _mapa_int("POSTS_POR_DIA_PLATAFORMA")


def posts_por_dia(plataforma):
    """Quantos posts por dia DAQUELA plataforma, caindo no global."""
    return POSTS_POR_DIA_PLATAFORMA.get(plataforma, POSTS_POR_DIA)

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

# --- publish: TikTok ----------------------------------------------------------
#
# Content Posting API (open.tiktokapis.com/v2). Não é a API por trás do app de
# celular: exige um app registrado em developers.tiktok.com com os escopos
# `video.publish` e `user.info.basic`, e a conta do canal autorizada nele. A
# coleta de métricas da etapa 7 pede um terceiro, `video.list`.
#
# LIMITAÇÃO DA PLATAFORMA, e não defeito daqui: enquanto o app não passar pela
# revisão da TikTok, TODO post sai como SELF_ONLY — visível só para a própria
# conta. Não existe configuração que contorne isso, e a revisão leva dias ou
# semanas. Ver o cabeçalho de publish/tiktok.py.
TIKTOK_CLIENT_KEY = _caminho("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = _caminho("TIKTOK_CLIENT_SECRET", "")

# Access token INICIAL e o refresh que o renova. O access token vale ~24 h —
# um dia, não os ~60 do Instagram —, então renovar é obrigação do programa:
# sem refresh token a fila para sozinha amanhã. Depois da primeira renovação
# os dois valores vigentes passam a viver na tabela `tokens`.
TIKTOK_ACCESS_TOKEN = _caminho("TIKTOK_ACCESS_TOKEN", "")
TIKTOK_REFRESH_TOKEN = _caminho("TIKTOK_REFRESH_TOKEN", "")
TIKTOK_API_BASE = _caminho("TIKTOK_API_BASE", "https://open.tiktokapis.com/v2")
# Renova quando faltar menos que isto para o access token vencer.
TIKTOK_RENOVAR_ANTES_S = _int("TIKTOK_RENOVAR_ANTES_S", 1800)

# Privacidade pedida ao publicar. 'SELF_ONLY' é o padrão por dois motivos: é o
# único valor que um app não revisado aceita, e o primeiro post de verdade
# merece ser conferido antes de ficar público. Troque para
# 'PUBLIC_TO_EVERYONE' quando a revisão sair.
TIKTOK_PRIVACIDADE = _caminho("TIKTOK_PRIVACIDADE", "SELF_ONLY")
# Declaração do humano de que a revisão saiu. Não é detecção — quem detecta é
# o creator_info na hora de publicar; isto serve para o `--verificar` avisar
# antes, em vez de o primeiro post revelar.
TIKTOK_APP_AUDITADO = _bool("TIKTOK_APP_AUDITADO", False)

# 'arquivo' envia os bytes (FILE_UPLOAD); 'url' manda a TikTok baixar de
# CLIPS_BASE_URL (PULL_FROM_URL). O padrão é 'arquivo': ao contrário do
# Instagram, a TikTok aceita upload direto, e o modo url ainda exige provar a
# propriedade do domínio no painel de desenvolvedor.
TIKTOK_MODO_UPLOAD = _caminho("TIKTOK_MODO_UPLOAD", "arquivo")
# Tamanho de cada pedaço do upload, em MB. A API aceita de 5 a 64 MB por
# chunk; um clip de 45 s cabe inteiro num só.
TIKTOK_CHUNK_MB = _int("TIKTOK_CHUNK_MB", 64)

# Limites do vídeo, conferidos AQUI antes de subir dezenas de MB para ouvir
# não. A duração máxima de verdade vem do creator_info da conta (varia por
# conta, e a API recusa o que passar dela); este é o teto de quem ainda não
# perguntou.
TIKTOK_DURACAO_MAXIMA_S = _int("TIKTOK_DURACAO_MAXIMA_S", 600)
TIKTOK_TAMANHO_MAXIMO_MB = _int("TIKTOK_TAMANHO_MAXIMO_MB", 4096)
TIKTOK_FORMATOS = _lista("TIKTOK_FORMATOS", [".mp4", ".webm", ".mov"])

# Interação do post. O que o CRIADOR desligou nas preferências da conta manda
# nestes valores: a API recusa o post que tenta ligar o que ele desligou, e o
# creator_info é quem conta qual é o caso.
TIKTOK_DESABILITAR_COMENTARIO = _bool("TIKTOK_DESABILITAR_COMENTARIO", False)
TIKTOK_DESABILITAR_DUETO = _bool("TIKTOK_DESABILITAR_DUETO", False)
TIKTOK_DESABILITAR_STITCH = _bool("TIKTOK_DESABILITAR_STITCH", False)

# --- publish: metadata --------------------------------------------------------

# Limites das plataformas, para o texto ser cortado aqui e não recusado lá.
LIMITE_TITULO_YOUTUBE = _int("LIMITE_TITULO_YOUTUBE", 100)
LIMITE_DESCRICAO_YOUTUBE = _int("LIMITE_DESCRICAO_YOUTUBE", 5000)
LIMITE_CAPTION_INSTAGRAM = _int("LIMITE_CAPTION_INSTAGRAM", 2200)
# TikTok: o teto da API é o mesmo 2200, mas o que aparece no feed são as duas
# primeiras linhas — o resto fica atrás do "mais". Por isso o CORPO tem alvo
# próprio, bem menor, e as hashtags entram depois dele. Cortar pelo teto da
# API daria uma legenda tecnicamente válida que ninguém lê.
LIMITE_CAPTION_TIKTOK = _int("LIMITE_CAPTION_TIKTOK", 2200)
LIMITE_CORPO_TIKTOK = _int("LIMITE_CORPO_TIKTOK", 150)
MAX_HASHTAGS = _int("MAX_HASHTAGS", 8)

# Publicações processadas por execução.
PUBLISH_MAX_POR_EXECUCAO = _int("PUBLISH_MAX_POR_EXECUCAO", 10)

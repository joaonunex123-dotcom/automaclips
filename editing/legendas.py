"""Gera o arquivo .ass do clip: legenda word-by-word, hook e watermark.

Por que ASS e não drawtext: os três elementos são texto sobre vídeo, e fazê-los
com `drawtext` do ffmpeg exigiria um filtro por palavra (centenas por clip),
mais um caminho de fonte absoluto que muda por sistema operacional. Um único
arquivo de legenda resolve os três de uma vez, com estilo declarado, e o filtro
`ass` do ffmpeg vira uma linha só.

O módulo é **string pura**: entra transcrição e template, sai texto. Nenhum
ffmpeg, nenhuma fonte instalada, nenhum arquivo de vídeo — o que torna a parte
mais detalhista da etapa (o sincronismo palavra a palavra) inteiramente
testável nesta máquina, que não tem ffmpeg.
"""
import logging

from editing.template import cor_ass

log = logging.getLogger(__name__)

# Nomes dos estilos dentro do .ass.
ESTILO_LEGENDA = "Legenda"
ESTILO_HOOK = "Hook"
ESTILO_WATERMARK = "Watermark"


def tempo_ass(segundos):
    """Segundos -> H:MM:SS.CC, o formato de tempo do ASS (centésimos)."""
    segundos = max(0.0, float(segundos))
    centesimos = int(round(segundos * 100))
    horas, resto = divmod(centesimos, 360000)
    minutos, resto = divmod(resto, 6000)
    seg, cent = divmod(resto, 100)
    return f"{horas:d}:{minutos:02d}:{seg:02d}.{cent:02d}"


def escapar(texto):
    """Neutraliza o que o ASS interpretaria como marcação.

    Chaves delimitam override de estilo no ASS: uma chave solta no texto do
    vídeo faria o resto da linha sumir. Não há escape padrão para elas entre os
    renderizadores, então viram parênteses — visualmente próximo e sempre
    seguro. Quebra de linha vira o `\\N` do formato.
    """
    return (
        str(texto)
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r\n", "\\N")
        .replace("\n", "\\N")
        .strip()
    )


def palavras_do_trecho(transcricao, inicio_s, fim_s):
    """Palavras dentro do trecho, com tempo relativo ao INÍCIO DO CLIP.

    A transcrição marca o vídeo-fonte; o .ass marca o clip recortado. Sem a
    subtração aqui, toda legenda apareceria deslocada pelo offset do trecho —
    que é o bug clássico deste tipo de pipeline.

    Segmento sem timestamp de palavra (backend que não devolveu, ou fatia que
    perdeu a granularidade) degrada para uma "palavra" com a frase inteira: a
    legenda continua aparecendo e sincronizada por frase, só perde o destaque
    palavra a palavra. Perder o realce é aceitável; perder a legenda não.
    """
    saida = []
    for segmento in transcricao.get("segmentos", []):
        if segmento.get("fim", 0) <= inicio_s or segmento.get("inicio", 0) >= fim_s:
            continue

        unidades = segmento.get("palavras") or []
        if not unidades:
            texto = (segmento.get("texto") or "").strip()
            if not texto:
                continue
            unidades = [
                {
                    "inicio": segmento.get("inicio", 0.0),
                    "fim": segmento.get("fim", 0.0),
                    "palavra": texto,
                }
            ]

        for unidade in unidades:
            comeco = float(unidade.get("inicio", 0.0))
            termino = float(unidade.get("fim", comeco))
            palavra = (unidade.get("palavra") or "").strip()
            if not palavra or termino <= inicio_s or comeco >= fim_s:
                continue
            saida.append(
                {
                    # Grampeado nas bordas do clip: uma palavra que começa
                    # antes do corte precisa aparecer a partir do segundo zero,
                    # não num tempo negativo (que o ASS ignoraria).
                    "inicio": max(0.0, comeco - inicio_s),
                    "fim": min(fim_s, termino) - inicio_s,
                    "palavra": palavra,
                }
            )

    saida.sort(key=lambda p: p["inicio"])
    return saida


def agrupar(palavras, por_linha):
    """Quebra a lista em linhas de N palavras."""
    por_linha = max(1, int(por_linha))
    return [palavras[i:i + por_linha] for i in range(0, len(palavras), por_linha)]


def _cabecalho(config):
    saida = config["saida"]
    return [
        "[Script Info]",
        "ScriptType: v4.00+",
        # PlayRes tem que casar com a resolução de saída: o ASS posiciona e
        # dimensiona tudo em relação a ela, então um valor diferente encolhe ou
        # estica a legenda inteira sem erro nenhum.
        f"PlayResX: {int(saida['largura'])}",
        f"PlayResY: {int(saida['altura'])}",
        # 0 = quebra automática equilibrada.
        #
        # Já foi 2 (sem quebra nenhuma), com o raciocínio de que
        # palavras_por_linha decide a quebra e o renderizador não deveria
        # opinar. O primeiro render real mostrou o furo: isso valia para a
        # LEGENDA, que tem controle de palavras por linha, e não para o hook,
        # que é frase livre do LLM — "A LIGAÇÃO QUE MUDOU TUDO" a 96px saiu
        # cortada nas duas bordas. A legenda também estoura quando as três
        # palavras são longas.
        #
        # Com 0, palavras_por_linha continua sendo o controle primário (linha
        # que cabe não é tocada) e a quebra só entra como rede de segurança.
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
    ]


def _estilo(nome, secao, largura):
    negrito = -1 if secao.get("negrito") else 0
    margem_lateral = max(10, int(largura * 0.06))
    return (
        f"Style: {nome},{secao['fonte']},{int(secao['tamanho'])},"
        f"{cor_ass(secao['cor'])},{cor_ass(secao['cor'])},"
        f"{cor_ass(secao['contorno_cor'])},&H00000000,"
        f"{negrito},0,0,0,100,100,0,0,"
        f"1,{secao['contorno_espessura']},{secao.get('sombra', 0)},"
        f"{int(secao['alinhamento'])},{margem_lateral},{margem_lateral},"
        f"{int(secao['margem_vertical'])},1"
    )


def _estilos(config):
    largura = int(config["saida"]["largura"])
    linhas = [
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _estilo(ESTILO_LEGENDA, config["legenda"], largura),
        _estilo(ESTILO_HOOK, config["hook"], largura),
        _estilo(ESTILO_WATERMARK, config["watermark"], largura),
        "",
    ]
    return linhas


def _dialogo(inicio, fim, estilo, texto):
    return (
        f"Dialogue: 0,{tempo_ass(inicio)},{tempo_ass(fim)},{estilo},,0,0,0,,{texto}"
    )


def _eventos_legenda(config, palavras):
    """Um evento por palavra: a linha inteira na tela, a atual em destaque."""
    secao = config["legenda"]
    if not secao.get("ativa") or not palavras:
        return []

    normal = cor_ass(secao["cor"])
    destaque = cor_ass(secao["cor_destaque"])
    maiusculas = bool(secao.get("maiusculas"))

    eventos = []
    for linha in agrupar(palavras, secao["palavras_por_linha"]):
        fim_da_linha = max(p["fim"] for p in linha)
        for i, atual in enumerate(linha):
            # O evento se estende até a palavra SEGUINTE, não até o fim da
            # própria palavra. Sem isso, a pausa entre duas palavras apagaria a
            # legenda e ela piscaria a cada respiro do locutor.
            fim = linha[i + 1]["inicio"] if i + 1 < len(linha) else fim_da_linha
            fim = max(fim, atual["fim"])

            partes = []
            for j, palavra in enumerate(linha):
                texto = escapar(palavra["palavra"])
                if maiusculas:
                    texto = texto.upper()
                cor = destaque if j == i else normal
                partes.append(f"{{\\c{cor}}}{texto}")
            eventos.append(
                _dialogo(atual["inicio"], fim, ESTILO_LEGENDA, " ".join(partes))
            )
    return eventos


def _eventos_hook(config, hook_text):
    secao = config["hook"]
    if not secao.get("ativo") or not (hook_text or "").strip():
        return []
    texto = escapar(hook_text)
    if secao.get("maiusculas"):
        texto = texto.upper()
    return [_dialogo(0.0, float(secao["duracao_s"]), ESTILO_HOOK, texto)]


def _eventos_watermark(config, duracao_s):
    secao = config["watermark"]
    if not secao.get("ativo") or not (secao.get("texto") or "").strip():
        return []
    return [
        _dialogo(0.0, float(duracao_s), ESTILO_WATERMARK, escapar(secao["texto"]))
    ]


def gerar_ass(config, palavras, hook_text="", duracao_s=0.0):
    """O .ass completo do clip, como string."""
    linhas = _cabecalho(config) + _estilos(config)
    linhas += [
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text",
    ]
    linhas += _eventos_watermark(config, duracao_s)
    linhas += _eventos_legenda(config, palavras)
    # O hook por último: em empate de tempo, o ASS desenha o evento posterior
    # por cima, e o texto de abertura é o que deve ficar visível no primeiro
    # segundo.
    linhas += _eventos_hook(config, hook_text)
    return "\n".join(linhas) + "\n"


def gerar_para_clip(config, transcricao, inicio_s, fim_s, hook_text=""):
    """Atalho: da transcrição do vídeo-fonte ao .ass do trecho."""
    palavras = palavras_do_trecho(transcricao, inicio_s, fim_s)
    log.debug("%d palavras no trecho %.1f–%.1f.", len(palavras), inicio_s, fim_s)
    return gerar_ass(config, palavras, hook_text, duracao_s=fim_s - inicio_s)

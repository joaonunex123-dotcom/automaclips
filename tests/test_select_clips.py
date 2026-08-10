"""Duração, confirmação por energia, limiar e sobreposição."""
import pytest

import settings
from db import repositorio
from pipeline import select_clips


# --- fator de energia ---------------------------------------------------------

def test_sem_pico_e_penalidade_nao_veto():
    # Trecho sem pico perde nota mas continua na disputa: às vezes a revelação
    # é dita baixinho, e é justamente onde o Claude acerta e o áudio não vê.
    fator = select_clips.fator_energia(0, 45.0, fator_min=0.7, fator_max=1.15)
    assert fator == pytest.approx(0.7)
    assert fator > 0


def test_densidade_plena_satura_no_maximo():
    # 4 picos/min em 60 s = densidade 4 = plena.
    fator = select_clips.fator_energia(
        4, 60.0, fator_min=0.7, fator_max=1.15, densidade_plena=4.0
    )
    assert fator == pytest.approx(1.15)


def test_acima_da_densidade_plena_nao_passa_do_teto():
    fator = select_clips.fator_energia(
        40, 60.0, fator_min=0.7, fator_max=1.15, densidade_plena=4.0
    )
    assert fator == pytest.approx(1.15)


def test_fator_e_monotonico_na_densidade():
    fatores = [
        select_clips.fator_energia(n, 60.0, fator_min=0.7, fator_max=1.15,
                                   densidade_plena=4.0)
        for n in range(5)
    ]
    assert fatores == sorted(fatores)


def test_duracao_zero_nao_divide_por_zero():
    assert select_clips.fator_energia(3, 0.0) == pytest.approx(
        settings.FATOR_ENERGIA_MIN
    )


# --- ajuste de duração --------------------------------------------------------

def test_trecho_na_faixa_passa_intacto():
    assert select_clips.ajustar_duracao(100, 145, 600, minima=30, maxima=60) == (
        100, 145
    )


def test_trecho_longo_e_cortado_no_fim():
    # O gancho está no começo, então o excesso sai do lado certo.
    assert select_clips.ajustar_duracao(100, 200, 600, minima=30, maxima=60) == (
        100, 160
    )


def test_trecho_curto_e_esticado_para_os_dois_lados():
    inicio, fim = select_clips.ajustar_duracao(100, 120, 600, minima=30, maxima=60)
    assert (inicio, fim) == pytest.approx((95.0, 125.0))
    assert fim - inicio == pytest.approx(30.0)


def test_esticar_nao_passa_do_inicio_do_video():
    # Trecho perto do começo: o que não cabe para trás é recuperado para a
    # frente, em vez de sair curto e ser reprovado por segundos que existiam.
    assert select_clips.ajustar_duracao(2, 12, 600, minima=30, maxima=60) == (
        pytest.approx(0.0), pytest.approx(30.0)
    )


def test_esticar_nao_passa_do_fim_do_video():
    inicio, fim = select_clips.ajustar_duracao(580, 595, 600, minima=30, maxima=60)
    assert fim == pytest.approx(600.0)
    assert fim - inicio == pytest.approx(30.0)


def test_video_menor_que_a_duracao_minima_nao_tem_trecho():
    assert select_clips.ajustar_duracao(0, 20, 20, minima=30, maxima=60) is None


def test_fim_e_limitado_pela_duracao_do_video():
    # O Claude pode devolver um fim além do vídeo; cortar aqui evita um ffmpeg
    # pedindo um segundo que não existe na etapa 3.
    inicio, fim = select_clips.ajustar_duracao(500, 900, 560, minima=30, maxima=60)
    assert fim == pytest.approx(560.0)


def test_duracao_do_video_desconhecida_nao_limita():
    # duracao_video=0 significa "não medida", não "vídeo de zero segundo".
    assert select_clips.ajustar_duracao(100, 140, 0, minima=30, maxima=60) == (
        100, 140
    )


def test_intervalo_invertido_e_recusado():
    assert select_clips.ajustar_duracao(200, 100, 600) is None


# --- seleção ------------------------------------------------------------------

def test_energia_reforca_e_enfraquece_a_nota_do_claude(trecho):
    bom = trecho(inicio_s=100, fim_s=160, score_claude=7.0)
    parado = trecho(inicio_s=300, fim_s=360, score_claude=7.0)
    picos = [105.0, 120.0, 135.0, 150.0]      # 4 picos só no primeiro

    avaliados = select_clips.selecionar(
        [bom, parado], picos=picos, duracao_video=600, threshold=0.0
    )
    por_inicio = {a["inicio_s"]: a for a in avaliados}
    assert por_inicio[100]["picos_energia"] == 4
    assert por_inicio[300]["picos_energia"] == 0
    # Mesma nota do Claude, ordem final diferente — que é o ponto da etapa.
    assert por_inicio[100]["score_final"] > por_inicio[300]["score_final"]


def test_energia_sozinha_nao_promove_trecho_ruim(trecho):
    ruim = trecho(inicio_s=100, fim_s=160, score_claude=2.0)
    avaliados = select_clips.selecionar(
        [ruim], picos=[float(t) for t in range(100, 160, 2)],
        duracao_video=600, threshold=6.0,
    )
    # Teto de 1,15 sobre 2,0 não chega perto de 6 — uma vinheta é puro pico.
    assert avaliados[0]["status"] == repositorio.CLIP_DESCARTADO


def test_abaixo_do_limiar_e_gravado_como_descartado(trecho):
    avaliados = select_clips.selecionar(
        [trecho(score_claude=3.0)], picos=[], duracao_video=600, threshold=6.0
    )
    assert len(avaliados) == 1
    assert avaliados[0]["status"] == repositorio.CLIP_DESCARTADO
    assert "score" in avaliados[0]["motivo_descarte"]


def test_sobreposicao_mantem_o_de_maior_score(trecho):
    forte = trecho(inicio_s=100, fim_s=150, score_claude=9.0)
    fraco = trecho(inicio_s=130, fim_s=180, score_claude=7.0)
    avaliados = select_clips.selecionar(
        [fraco, forte], picos=[], duracao_video=600, threshold=0.0
    )
    por_inicio = {a["inicio_s"]: a for a in avaliados}
    assert por_inicio[100]["status"] == repositorio.CLIP_SELECIONADO
    assert por_inicio[130]["status"] == repositorio.CLIP_DESCARTADO
    assert "sobrep" in por_inicio[130]["motivo_descarte"]


def test_trechos_adjacentes_nao_sao_sobreposicao(trecho):
    # Fim de um igual ao início do outro: intervalo semiaberto, sem conflito.
    a = trecho(inicio_s=100, fim_s=150, score_claude=9.0)
    b = trecho(inicio_s=150, fim_s=200, score_claude=8.0)
    avaliados = select_clips.selecionar(
        [a, b], picos=[], duracao_video=600, threshold=0.0
    )
    assert all(i["status"] == repositorio.CLIP_SELECIONADO for i in avaliados)


def test_descartado_por_limiar_nao_bloqueia_sobreposicao(trecho):
    # Um trecho fora do corte não pode "reservar" o minuto e derrubar outro que
    # passou — a sobreposição é resolvida só entre os aprovados.
    fraco = trecho(inicio_s=100, fim_s=150, score_claude=1.0)
    forte = trecho(inicio_s=110, fim_s=160, score_claude=9.0)
    avaliados = select_clips.selecionar(
        [fraco, forte], picos=[], duracao_video=600, threshold=6.0
    )
    por_inicio = {a["inicio_s"]: a for a in avaliados}
    assert por_inicio[110]["status"] == repositorio.CLIP_SELECIONADO


def test_trecho_inajustavel_vira_descartado_e_nao_some(trecho):
    avaliados = select_clips.selecionar(
        [trecho(inicio_s=0, fim_s=10)], picos=[], duracao_video=10, threshold=0.0
    )
    assert len(avaliados) == 1
    assert avaliados[0]["status"] == repositorio.CLIP_DESCARTADO
    assert "duração" in avaliados[0]["motivo_descarte"]


def test_saida_vem_ordenada_pelo_score_final(trecho):
    entrada = [
        trecho(inicio_s=100, fim_s=150, score_claude=5.0),
        trecho(inicio_s=200, fim_s=250, score_claude=9.0),
        trecho(inicio_s=300, fim_s=350, score_claude=7.0),
    ]
    avaliados = select_clips.selecionar(
        entrada, picos=[], duracao_video=600, threshold=0.0
    )
    scores = [a["score_final"] for a in avaliados]
    assert scores == sorted(scores, reverse=True)


def test_nenhum_trecho_e_perdido(trecho):
    entrada = [
        trecho(inicio_s=100, fim_s=150, score_claude=9.0),
        trecho(inicio_s=120, fim_s=170, score_claude=8.0),   # sobrepõe
        trecho(inicio_s=300, fim_s=350, score_claude=1.0),   # abaixo do corte
        trecho(inicio_s=0, fim_s=5),                          # inajustável
    ]
    avaliados = select_clips.selecionar(
        entrada, picos=[], duracao_video=600, threshold=6.0
    )
    assert len(avaliados) == len(entrada)
    assert all(
        a["status"] == repositorio.CLIP_SELECIONADO or a["motivo_descarte"]
        for a in avaliados
    )


def test_lista_vazia():
    assert select_clips.selecionar([], picos=[], duracao_video=600) == []

"""Geração do .ass: sincronismo, destaque palavra a palavra, hook e watermark."""
import pytest

from editing import legendas
from editing.template import cor_ass


def _transcricao(*falas):
    """_transcricao((inicio, fim, [(i, f, palavra), ...]), ...)."""
    return {
        "segmentos": [
            {
                "inicio": i, "fim": f,
                "texto": " ".join(p[2] for p in palavras),
                "palavras": [
                    {"inicio": a, "fim": b, "palavra": w} for a, b, w in palavras
                ],
            }
            for i, f, palavras in falas
        ]
    }


# --- formato de tempo ---------------------------------------------------------

@pytest.mark.parametrize(
    "segundos, esperado",
    [
        (0, "0:00:00.00"),
        (1.5, "0:00:01.50"),
        (61.23, "0:01:01.23"),
        (3661.0, "1:01:01.00"),
        (-5, "0:00:00.00"),      # tempo negativo não existe no ASS
    ],
)
def test_tempo_ass(segundos, esperado):
    assert legendas.tempo_ass(segundos) == esperado


# --- escape -------------------------------------------------------------------

def test_chaves_viram_parenteses():
    # Chave solta no texto do vídeo faria o resto da linha sumir: no ASS elas
    # delimitam override de estilo, e não há escape padrão entre renderizadores.
    assert legendas.escapar("olha {isso} aqui") == "olha (isso) aqui"


def test_quebra_de_linha_vira_marcacao_do_formato():
    assert legendas.escapar("linha um\nlinha dois") == "linha um\\Nlinha dois"


# --- extração de palavras -----------------------------------------------------

def test_tempos_ficam_relativos_ao_inicio_do_clip():
    # A transcrição marca o vídeo-fonte; o .ass marca o clip. Sem a subtração,
    # toda legenda apareceria deslocada pelo offset do trecho.
    transcricao = _transcricao((100.0, 104.0, [(100.0, 101.0, "a"), (101.0, 104.0, "b")]))
    palavras = legendas.palavras_do_trecho(transcricao, 100.0, 130.0)
    assert [p["inicio"] for p in palavras] == pytest.approx([0.0, 1.0])
    assert palavras[1]["fim"] == pytest.approx(4.0)


def test_palavras_fora_do_trecho_ficam_de_fora():
    transcricao = _transcricao(
        (10.0, 12.0, [(10.0, 12.0, "antes")]),
        (100.0, 102.0, [(100.0, 102.0, "dentro")]),
        (500.0, 502.0, [(500.0, 502.0, "depois")]),
    )
    palavras = legendas.palavras_do_trecho(transcricao, 100.0, 130.0)
    assert [p["palavra"] for p in palavras] == ["dentro"]


def test_palavra_atravessando_o_corte_e_grampeada():
    # Palavra que começa antes do corte precisa aparecer a partir do zero: um
    # tempo negativo seria ignorado pelo renderizador e a palavra sumiria.
    transcricao = _transcricao((98.0, 103.0, [(98.0, 103.0, "atravessa")]))
    palavras = legendas.palavras_do_trecho(transcricao, 100.0, 130.0)
    assert palavras[0]["inicio"] == 0.0


def test_segmento_sem_palavras_degrada_para_a_frase():
    # Perder o realce é aceitável; perder a legenda não.
    transcricao = {
        "segmentos": [
            {"inicio": 100.0, "fim": 104.0, "texto": "frase inteira", "palavras": []}
        ]
    }
    palavras = legendas.palavras_do_trecho(transcricao, 100.0, 130.0)
    assert [p["palavra"] for p in palavras] == ["frase inteira"]


def test_saida_vem_ordenada_no_tempo():
    transcricao = _transcricao(
        (110.0, 112.0, [(110.0, 112.0, "segunda")]),
        (100.0, 102.0, [(100.0, 102.0, "primeira")]),
    )
    palavras = legendas.palavras_do_trecho(transcricao, 100.0, 130.0)
    assert [p["palavra"] for p in palavras] == ["primeira", "segunda"]


def test_transcricao_vazia():
    assert legendas.palavras_do_trecho({"segmentos": []}, 0, 30) == []


# --- agrupamento --------------------------------------------------------------

def test_agrupar_em_linhas():
    palavras = [{"palavra": str(i)} for i in range(7)]
    linhas = legendas.agrupar(palavras, 3)
    assert [len(l) for l in linhas] == [3, 3, 1]


def test_agrupar_com_zero_nao_quebra():
    assert len(legendas.agrupar([{"palavra": "a"}], 0)) == 1


# --- geração do arquivo -------------------------------------------------------

@pytest.fixture
def palavras():
    return [
        {"inicio": 0.0, "fim": 0.5, "palavra": "ele"},
        {"inicio": 0.9, "fim": 1.4, "palavra": "nunca"},
        {"inicio": 1.4, "fim": 2.0, "palavra": "contou"},
    ]


def test_playres_acompanha_a_resolucao_de_saida(template, palavras):
    # O ASS posiciona e dimensiona tudo em relação ao PlayRes: um valor
    # diferente encolheria a legenda inteira sem erro nenhum.
    texto = legendas.gerar_ass(template, palavras)
    assert f"PlayResX: {template['saida']['largura']}" in texto
    assert f"PlayResY: {template['saida']['altura']}" in texto


def test_gera_os_tres_estilos(template, palavras):
    texto = legendas.gerar_ass(template, palavras)
    for estilo in (legendas.ESTILO_LEGENDA, legendas.ESTILO_HOOK,
                   legendas.ESTILO_WATERMARK):
        assert f"Style: {estilo}," in texto


def test_um_evento_por_palavra(template, palavras):
    texto = legendas.gerar_ass(template, palavras)
    dialogos = [l for l in texto.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogos) == len(palavras)


def test_a_palavra_atual_sai_em_destaque(template, palavras):
    texto = legendas.gerar_ass(template, palavras)
    dialogos = [l for l in texto.splitlines() if l.startswith("Dialogue:")]
    destaque = cor_ass(template["legenda"]["cor_destaque"])
    normal = cor_ass(template["legenda"]["cor"])

    # No primeiro evento, "ELE" está destacada e as outras não.
    primeiro = dialogos[0]
    assert f"{{\\c{destaque}}}ELE" in primeiro
    assert f"{{\\c{normal}}}NUNCA" in primeiro
    # No segundo, o destaque anda.
    assert f"{{\\c{destaque}}}NUNCA" in dialogos[1]


def test_a_linha_inteira_fica_na_tela(template, palavras):
    # Mostrar só a palavra corrente deixaria a legenda ilegível em tela pequena.
    primeiro = [l for l in legendas.gerar_ass(template, palavras).splitlines()
                if l.startswith("Dialogue:")][0]
    for palavra in ("ELE", "NUNCA", "CONTOU"):
        assert palavra in primeiro


def test_evento_se_estende_ate_a_palavra_seguinte(template, palavras):
    # Há uma pausa entre "ele" (fim 0.5) e "nunca" (início 0.9). Sem esticar,
    # a legenda apagaria e piscaria a cada respiro do locutor.
    primeiro = [l for l in legendas.gerar_ass(template, palavras).splitlines()
                if l.startswith("Dialogue:")][0]
    assert legendas.tempo_ass(0.9) in primeiro


def test_maiusculas_respeitam_o_template(template, palavras):
    config = dict(template)
    config["legenda"] = {**template["legenda"], "maiusculas": False}
    texto = legendas.gerar_ass(config, palavras)
    assert "}ele" in texto and "}ELE" not in texto


def test_legenda_desligada_nao_gera_evento(template, palavras):
    config = dict(template)
    config["legenda"] = {**template["legenda"], "ativa": False}
    texto = legendas.gerar_ass(config, palavras)
    assert "Dialogue:" not in texto


def test_hook_ocupa_o_primeiro_segundo(template, palavras):
    texto = legendas.gerar_ass(template, palavras, hook_text="ele nunca contou isso")
    hook = [l for l in texto.splitlines() if legendas.ESTILO_HOOK in l
            and l.startswith("Dialogue:")]
    assert len(hook) == 1
    assert legendas.tempo_ass(template["hook"]["duracao_s"]) in hook[0]
    assert "ELE NUNCA CONTOU ISSO" in hook[0]


def test_hook_vazio_nao_gera_evento(template, palavras):
    texto = legendas.gerar_ass(template, palavras, hook_text="   ")
    assert legendas.ESTILO_HOOK not in [
        l.split(",")[3] for l in texto.splitlines() if l.startswith("Dialogue:")
    ]


def test_hook_vem_depois_da_legenda_no_arquivo(template, palavras):
    # Em empate de tempo o ASS desenha o evento posterior por cima, e o texto
    # de abertura é o que deve ficar visível no primeiro segundo.
    texto = legendas.gerar_ass(template, palavras, hook_text="abre aqui")
    linhas = texto.splitlines()
    ultimo_legenda = max(i for i, l in enumerate(linhas)
                         if l.startswith("Dialogue:") and legendas.ESTILO_LEGENDA in l)
    indice_hook = next(i for i, l in enumerate(linhas)
                       if l.startswith("Dialogue:") and legendas.ESTILO_HOOK in l)
    assert indice_hook > ultimo_legenda


def test_watermark_desligada_por_padrao(template, palavras):
    texto = legendas.gerar_ass(template, palavras, duracao_s=45.0)
    assert not [l for l in texto.splitlines()
                if l.startswith("Dialogue:") and legendas.ESTILO_WATERMARK in l]


def test_watermark_ligada_dura_o_clip_inteiro(template, palavras):
    config = dict(template)
    config["watermark"] = {**template["watermark"], "ativo": True, "texto": "@canal"}
    texto = legendas.gerar_ass(config, palavras, duracao_s=45.0)
    marca = [l for l in texto.splitlines()
             if l.startswith("Dialogue:") and legendas.ESTILO_WATERMARK in l]
    assert len(marca) == 1
    assert legendas.tempo_ass(45.0) in marca[0]


def test_gerar_para_clip_liga_as_duas_pontas(template):
    transcricao = _transcricao(
        (100.0, 103.0, [(100.0, 101.0, "a"), (101.0, 103.0, "b")])
    )
    texto = legendas.gerar_para_clip(
        template, transcricao, 100.0, 145.0, hook_text="olha isso"
    )
    assert "[Events]" in texto
    assert "OLHA ISSO" in texto
    assert texto.endswith("\n")

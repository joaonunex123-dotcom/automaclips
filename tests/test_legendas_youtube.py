"""Legendas do YouTube como transcrição — sobretudo o formato rolante."""
import pytest

import settings
from pipeline import legendas_youtube as ly

# Formato real das legendas AUTOMÁTICAS: cada bloco repete o texto do anterior
# em linhas próprias e só a última linha traz o conteúdo novo. É o que faz uma
# leitura ingênua triplicar cada palavra.
AUTO = """WEBVTT
Kind: captions
Language: pt-BR

00:00:00.030 --> 00:00:02.669 align:start position:0%

e<00:00:00.719><c> aí</c><00:00:01.199><c> pessoal</c><00:00:01.800><c> bem</c><00:00:02.100><c> vindos</c>

00:00:02.669 --> 00:00:02.679 align:start position:0%
e aí pessoal bem vindos


00:00:02.679 --> 00:00:05.310 align:start position:0%
e aí pessoal bem vindos
a<00:00:03.000><c> mais</c><00:00:03.300><c> um</c><00:00:03.600><c> episódio</c>

00:00:05.310 --> 00:00:05.320 align:start position:0%
a mais um episódio


00:00:05.320 --> 00:00:08.900 align:start position:0%
a mais um episódio
hoje<00:00:06.000><c> eu</c><00:00:06.400><c> vou</c><00:00:07.100><c> contar</c>
"""

# Legenda MANUAL: sem marca de palavra e sem repetição.
MANUAL = """WEBVTT

00:00:00.030 --> 00:00:02.669
E aí, pessoal! Bem-vindos

00:00:02.679 --> 00:00:05.310
a mais um episódio.

00:00:05.320 --> 00:00:08.900
Hoje eu vou contar uma coisa.
"""


# --- tempo --------------------------------------------------------------------

@pytest.mark.parametrize(
    "marca, segundos",
    [("00:00:00.030", 0.03), ("00:01:02.345", 62.345),
     ("01:00:00.000", 3600.0), ("00:00:05,310", 5.31)],
)
def test_converte_a_marca_de_tempo(marca, segundos):
    assert ly._segundos(marca) == pytest.approx(segundos)


# --- legenda automática -------------------------------------------------------

def test_cada_palavra_sai_uma_vez_so():
    # A armadilha do formato rolante: ler o bloco inteiro triplicaria tudo.
    palavras = [p["palavra"] for p in ly.parse_vtt(AUTO)]
    assert palavras == ["e", "aí", "pessoal", "bem", "vindos",
                        "a", "mais", "um", "episódio",
                        "hoje", "eu", "vou", "contar"]


def test_o_texto_repetido_nao_vira_fala_nova():
    # Deduplicar só por tempo não salvaria: o texto repetido recebe o tempo do
    # bloco ATUAL e passaria como se fosse fala nova.
    palavras = [p["palavra"] for p in ly.parse_vtt(AUTO)]
    assert palavras.count("pessoal") == 1
    assert palavras.count("episódio") == 1


def test_cada_palavra_tem_o_proprio_instante():
    # É isto que a legenda word-by-word do template precisa, e é a única razão
    # de preferir a automática à manual.
    por_palavra = {p["palavra"]: p["inicio"] for p in ly.parse_vtt(AUTO)}
    assert por_palavra["e"] == pytest.approx(0.030)
    assert por_palavra["aí"] == pytest.approx(0.719)
    assert por_palavra["vindos"] == pytest.approx(2.100)
    assert por_palavra["mais"] == pytest.approx(3.000)


def test_a_primeira_palavra_do_bloco_nao_se_perde():
    # "a" e "hoje" vêm ANTES da primeira marca do bloco: o tempo delas é o
    # início do bloco. Descartar o trecho sem marca perderia uma palavra por
    # bloco.
    por_palavra = {p["palavra"]: p["inicio"] for p in ly.parse_vtt(AUTO)}
    assert por_palavra["a"] == pytest.approx(2.679)
    assert por_palavra["hoje"] == pytest.approx(5.320)


def test_o_fim_encadeia_na_palavra_seguinte():
    # Sem isso a legenda apagaria entre uma palavra e outra e ficaria piscando.
    palavras = ly.parse_vtt(AUTO)
    for anterior, seguinte in zip(palavras, palavras[1:]):
        assert anterior["fim"] <= seguinte["inicio"]
    assert palavras[-1]["fim"] > palavras[-1]["inicio"]


def test_o_encadeamento_tem_teto_e_o_silencio_sobrevive():
    # Encadear sem teto faria uma pausa de 5s virar uma palavra de 5s — e sem
    # silêncio, montar_segmentos perde a única pista de onde a frase acaba.
    palavras = ly.parse_vtt(AUTO)
    por_palavra = {p["palavra"]: p for p in palavras}
    episodio = por_palavra["episódio"]
    assert episodio["fim"] - episodio["inicio"] <= ly.DURACAO_MAXIMA_PALAVRA_S
    # ...e a pausa até "hoje" continua visível.
    assert por_palavra["hoje"]["inicio"] > episodio["fim"]


def test_tags_de_estilo_somem():
    assert not any("<c" in p["palavra"] for p in ly.parse_vtt(AUTO))


# --- legenda manual -----------------------------------------------------------

def test_manual_vira_frase_por_bloco():
    unidades = ly.parse_vtt(MANUAL)
    assert [u["palavra"] for u in unidades] == [
        "E aí, pessoal! Bem-vindos", "a mais um episódio.",
        "Hoje eu vou contar uma coisa.",
    ]


def test_manual_nao_finge_ter_timestamp_de_palavra():
    # Repetir a frase como se fosse uma palavra faria o destaque piscar o
    # bloco inteiro em vez de andar.
    segmentos = ly.montar_segmentos(ly.parse_vtt(MANUAL))
    assert all(s["palavras"] == [] for s in segmentos)
    assert segmentos[0]["texto"] == "E aí, pessoal! Bem-vindos"


def test_manual_preserva_pontuacao():
    # É a vantagem dela sobre a automática.
    assert "!" in ly.parse_vtt(MANUAL)[0]["palavra"]


# --- segmentos ----------------------------------------------------------------

def test_quebra_na_pausa():
    palavras = ly.parse_vtt(AUTO)
    segmentos = ly.montar_segmentos(palavras, intervalo_frase=0.5,
                                    maximo_palavras=99)
    assert len(segmentos) > 1
    assert all(s["palavras"] for s in segmentos)


def test_teto_de_palavras_evita_segmento_gigante():
    # Fala corrida sem pausa audível produziria um "segmento" de dois minutos,
    # e é o segmento que vira linha no prompt do highlight_detect.
    palavras = [
        {"inicio": i * 0.2, "fim": (i + 1) * 0.2, "palavra": f"p{i}"}
        for i in range(30)
    ]
    segmentos = ly.montar_segmentos(palavras, intervalo_frase=99,
                                    maximo_palavras=10)
    assert [len(s["palavras"]) for s in segmentos] == [10, 10, 10]


def test_segmento_carrega_inicio_e_fim_certos():
    segmento = ly.montar_segmentos(ly.parse_vtt(AUTO))[0]
    assert segmento["inicio"] == pytest.approx(0.030)
    assert segmento["fim"] == pytest.approx(segmento["palavras"][-1]["fim"])


def test_vtt_vazio():
    assert ly.parse_vtt("WEBVTT\n\n") == []
    assert ly.montar_segmentos([]) == []


# --- contrato de saída --------------------------------------------------------

def test_devolve_o_mesmo_contrato_dos_outros_backends(tmp_path):
    caminho = tmp_path / "abc123.pt-BR.vtt"
    caminho.write_text(AUTO, encoding="utf-8")

    resultado = ly.transcrever(caminho_vtt=str(caminho), duracao_s=600.0)

    assert set(resultado) >= {"idioma", "duracao_s", "segmentos"}
    assert resultado["duracao_s"] == 600.0
    assert resultado["segmentos"][0]["palavras"]


def test_idioma_sai_do_nome_do_arquivo(tmp_path):
    caminho = tmp_path / "abc123.pt-BR.vtt"
    caminho.write_text(AUTO, encoding="utf-8")
    assert ly.transcrever(caminho_vtt=str(caminho))["idioma"] == "pt-BR"


def test_legenda_vazia_e_erro(tmp_path):
    caminho = tmp_path / "abc.pt.vtt"
    caminho.write_text("WEBVTT\n\n", encoding="utf-8")
    with pytest.raises(ly.ErroLegendasYoutube, match="vazia"):
        ly.transcrever(caminho_vtt=str(caminho))


def test_sem_video_id_e_erro():
    with pytest.raises(ly.ErroLegendasYoutube, match="video_id"):
        ly.transcrever()


# --- busca via yt-dlp ---------------------------------------------------------

class YdlFalso:
    def __init__(self, opcoes, escrever=None):
        self.opcoes = opcoes
        self._escrever = escrever

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def extract_info(self, url, download=True):
        if self._escrever:
            self._escrever(self.opcoes)
        return {}


def test_pede_a_automatica_por_padrao(tmp_path):
    vistos = {}

    def criar(opcoes):
        vistos.update(opcoes)
        (tmp_path / "abc.pt-BR.vtt").write_text(AUTO, encoding="utf-8")
        return YdlFalso(opcoes)

    caminho, automatica = ly.baixar_vtt("abc", str(tmp_path), criar_ydl=criar)

    assert automatica is True
    assert vistos["writeautomaticsub"] is True
    assert vistos["writesubtitles"] is False
    # Não baixa o vídeo: só a legenda, que são alguns KB.
    assert vistos["skip_download"] is True


def test_preferir_manual_inverte(tmp_path):
    vistos = {}

    def criar(opcoes):
        vistos.update(opcoes)
        (tmp_path / "abc.pt.vtt").write_text(MANUAL, encoding="utf-8")
        return YdlFalso(opcoes)

    _caminho, automatica = ly.baixar_vtt(
        "abc", str(tmp_path), preferir="manual", criar_ydl=criar
    )
    assert automatica is False
    assert vistos["writesubtitles"] is True


def test_cai_no_outro_tipo_quando_o_preferido_nao_existe(tmp_path, caplog):
    # Canal grande costuma ter legenda manual; canal pequeno, só automática.
    tentativas = []

    def criar(opcoes):
        tentativas.append(dict(opcoes))
        if len(tentativas) == 2:      # só a segunda tentativa produz arquivo
            (tmp_path / "abc.pt.vtt").write_text(MANUAL, encoding="utf-8")
        return YdlFalso(opcoes)

    caminho, automatica = ly.baixar_vtt("abc", str(tmp_path), criar_ydl=criar)

    assert len(tentativas) == 2
    assert tentativas[0]["writeautomaticsub"] is True
    assert tentativas[1]["writesubtitles"] is True
    assert automatica is False


def test_video_sem_legenda_nenhuma_diz_o_que_fazer(tmp_path):
    with pytest.raises(ly.ErroLegendasYoutube, match="TRANSCRICAO_BACKEND"):
        ly.baixar_vtt("abc", str(tmp_path),
                      criar_ydl=lambda opcoes: YdlFalso(opcoes))


# --- despachante --------------------------------------------------------------

def test_backend_youtube_e_reconhecido():
    from pipeline import transcribe

    assert transcribe.backend_ativo("youtube") == transcribe.BACKEND_YOUTUBE


def test_youtube_nao_entra_na_escolha_automatica(monkeypatch):
    # Ele depende de o vídeo ter legenda, o que só se descobre tentando. Um
    # padrão que às vezes não existe faria o pipeline falhar em vídeo aleatório
    # sem ninguém ter escolhido isso.
    from pipeline import transcribe

    monkeypatch.setattr(settings, "TRANSCRICAO_BACKEND", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    assert transcribe.backend_ativo() == transcribe.BACKEND_LOCAL

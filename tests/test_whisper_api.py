"""Whisper API: compressão, fatiamento, normalização e custo — sem rede."""
import os

import pytest

import settings
from pipeline import whisper_api


# --- custo --------------------------------------------------------------------

def test_custo_e_proporcional_ao_audio():
    # Calculável ANTES da chamada porque a API cobra por minuto de áudio, não
    # por token — é isso que permite a guarda de orçamento.
    assert whisper_api.estimar_custo(3600, usd_por_minuto=0.006) == pytest.approx(0.36)
    assert whisper_api.estimar_custo(0) == 0.0
    assert whisper_api.estimar_custo(-5) == 0.0


def test_custo_usa_o_preco_de_settings():
    esperado = 60 / 60 * settings.WHISPER_API_USD_POR_MINUTO
    assert whisper_api.estimar_custo(60) == pytest.approx(esperado)


# --- cliente ------------------------------------------------------------------

def test_sem_chave_falha_apontando_a_alternativa():
    with pytest.raises(whisper_api.ErroWhisperAPI, match="TRANSCRICAO_BACKEND=local"):
        whisper_api.construir_cliente(api_key="")


# --- compressão ---------------------------------------------------------------

def test_comprime_para_mp3_mono(tmp_path, executar_ok, ffmpeg_fake):
    wav = tmp_path / "vid1.wav"
    wav.write_text("audio", encoding="utf-8")
    registro = []

    saida = whisper_api.comprimir(
        str(wav), str(tmp_path), bitrate="32k",
        executar=executar_ok(registro), ffmpeg=ffmpeg_fake,
    )
    assert saida.endswith("vid1.mp3")
    cmd = registro[0]["comando"]
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-b:a") + 1] == "32k"


def test_mp3_existente_e_reaproveitado(tmp_path, ffmpeg_fake):
    wav = tmp_path / "vid1.wav"
    wav.write_text("audio", encoding="utf-8")
    (tmp_path / "vid1.mp3").write_text("ja existe", encoding="utf-8")

    def explode(*a, **k):
        raise AssertionError("não deveria recomprimir")

    assert whisper_api.comprimir(
        str(wav), str(tmp_path), executar=explode, ffmpeg=ffmpeg_fake
    ).endswith("vid1.mp3")


# --- silêncios ----------------------------------------------------------------

SAIDA_SILENCEDETECT = """\
[silencedetect @ 0x1] silence_start: 120.5
[silencedetect @ 0x1] silence_end: 121.8 | silence_duration: 1.3
[silencedetect @ 0x1] silence_start: 300.0
[silencedetect @ 0x1] silence_end: 300.9 | silence_duration: 0.9
"""


def test_extrai_os_silencios_do_stderr(tmp_path, ffmpeg_fake):
    audio = tmp_path / "a.mp3"
    audio.write_text("x", encoding="utf-8")

    def executar(comando, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stderr=SAIDA_SILENCEDETECT, stdout="")

    assert whisper_api.detectar_silencios(
        str(audio), executar=executar, ffmpeg=ffmpeg_fake
    ) == [(120.5, 121.8), (300.0, 300.9)]


def test_silencio_sem_fim_e_ignorado(tmp_path, ffmpeg_fake):
    # O ffmpeg pode terminar o arquivo dentro de um silêncio, sem emitir o
    # silence_end. Um par incompleto não é ponto de corte.
    from types import SimpleNamespace

    audio = tmp_path / "a.mp3"
    audio.write_text("x", encoding="utf-8")

    def executar(comando, **kwargs):
        return SimpleNamespace(
            returncode=0, stderr="[silencedetect] silence_start: 500.0", stdout=""
        )

    assert whisper_api.detectar_silencios(
        str(audio), executar=executar, ffmpeg=ffmpeg_fake
    ) == []


def test_falha_do_silencedetect_nao_derruba(tmp_path, ffmpeg_fake):
    # Sem silêncio detectado o corte cai no alvo — degradação aceitável.
    # Falhar aqui derrubaria a transcrição por uma otimização de fronteira.
    from types import SimpleNamespace

    audio = tmp_path / "a.mp3"
    audio.write_text("x", encoding="utf-8")

    def executar(comando, **kwargs):
        return SimpleNamespace(returncode=1, stderr="deu ruim", stdout="")

    assert whisper_api.detectar_silencios(
        str(audio), executar=executar, ffmpeg=ffmpeg_fake
    ) == []


# --- pontos de corte ----------------------------------------------------------

def test_audio_curto_nao_e_fatiado():
    assert whisper_api.pontos_de_corte(600, [], max_chunk_s=1800) == []


def test_corte_encosta_no_silencio_mais_proximo():
    # Cortar no meio de uma palavra estraga uma palavra por fronteira, e as
    # fronteiras caem em posições arbitrárias.
    silencios = [(1750.0, 1752.0), (1900.0, 1901.0)]
    cortes = whisper_api.pontos_de_corte(
        3000, silencios, max_chunk_s=1800, tolerancia_s=120
    )
    assert cortes == [pytest.approx(1751.0)]


def test_sem_silencio_na_tolerancia_corta_no_alvo():
    cortes = whisper_api.pontos_de_corte(
        3000, [(10.0, 12.0)], max_chunk_s=1800, tolerancia_s=60
    )
    assert cortes == [pytest.approx(1800.0)]


def test_varios_cortes_para_audio_longo():
    cortes = whisper_api.pontos_de_corte(6000, [], max_chunk_s=1800)
    assert cortes == pytest.approx([1800.0, 3600.0, 5400.0])
    # Sobra menor que um chunk não vira corte novo.
    assert 6000 - cortes[-1] <= 1800


def test_silencios_ruins_nao_travam_o_laco():
    # Todos antes do primeiro alvo e dentro de uma tolerância enorme: o filtro
    # `> atual` é o que garante progresso e término.
    cortes = whisper_api.pontos_de_corte(
        5000, [(0.1, 0.2), (0.3, 0.4)], max_chunk_s=1800, tolerancia_s=100000
    )
    assert len(cortes) >= 1
    assert cortes == sorted(cortes)


# --- fatiamento ---------------------------------------------------------------

def test_sem_cortes_devolve_o_arquivo_inteiro(tmp_path):
    assert whisper_api.fatiar("/a/b.mp3", [], 600, str(tmp_path)) == [("/a/b.mp3", 0.0)]


def test_fatias_carregam_o_deslocamento(tmp_path, executar_ok, ffmpeg_fake):
    audio = tmp_path / "vid1.mp3"
    audio.write_text("x", encoding="utf-8")
    registro = []

    fatias = whisper_api.fatiar(
        str(audio), [1800.0], 3000.0, str(tmp_path),
        executar=executar_ok(registro), ffmpeg=ffmpeg_fake,
    )
    assert [d for _, d in fatias] == [0.0, 1800.0]

    # -t (duração), não -to: com -ss antes da entrada, -to muda de significado
    # conforme a versão do ffmpeg.
    primeiro = registro[0]["comando"]
    assert primeiro[primeiro.index("-t") + 1] == "1800.000"
    segundo = registro[1]["comando"]
    assert segundo[segundo.index("-ss") + 1] == "1800.000"
    assert segundo[segundo.index("-t") + 1] == "1200.000"


# --- normalização -------------------------------------------------------------

RESPOSTA = {
    "language": "portuguese",
    "duration": 20.0,
    "text": "boa noite pessoal",
    "segments": [
        {"start": 0.0, "end": 3.0, "text": " boa noite "},
        {"start": 3.0, "end": 6.0, "text": " pessoal "},
    ],
    "words": [
        {"word": "boa", "start": 0.0, "end": 1.0},
        {"word": "noite", "start": 1.0, "end": 2.5},
        {"word": "pessoal", "start": 3.2, "end": 5.0},
    ],
}


def test_atribui_as_palavras_de_primeiro_nivel_aos_segmentos():
    # Ao contrário do faster-whisper, a API devolve as palavras numa lista de
    # primeiro nível; a associação por intervalo de tempo é feita aqui.
    segmentos = whisper_api._normalizar(RESPOSTA)
    assert [p["palavra"] for p in segmentos[0]["palavras"]] == ["boa", "noite"]
    assert [p["palavra"] for p in segmentos[1]["palavras"]] == ["pessoal"]
    assert segmentos[0]["texto"] == "boa noite"


def test_deslocamento_da_fatia_e_somado_em_tudo():
    # O bug clássico do fatiamento: a segunda fatia volta com tempos relativos
    # a ela mesma, e sem a soma toda a segunda metade do vídeo fica legendada
    # com o tempo da primeira.
    segmentos = whisper_api._normalizar(RESPOSTA, deslocamento=1800.0)
    assert segmentos[0]["inicio"] == pytest.approx(1800.0)
    assert segmentos[0]["palavras"][0]["inicio"] == pytest.approx(1800.0)
    assert segmentos[1]["fim"] == pytest.approx(1806.0)


def test_resposta_sem_segmentos_usa_o_texto_inteiro():
    segmentos = whisper_api._normalizar(
        {"text": "só o texto", "duration": 12.0, "words": [], "segments": []}
    )
    assert len(segmentos) == 1
    assert segmentos[0]["texto"] == "só o texto"
    assert segmentos[0]["fim"] == pytest.approx(12.0)


def test_resposta_completamente_vazia():
    assert whisper_api._normalizar({"text": "", "segments": [], "words": []}) == []


def test_como_dict_aceita_modelo_pydantic():
    class Fingindo:
        def model_dump(self):
            return {"text": "ok", "segments": [], "words": []}

    assert whisper_api._como_dict(Fingindo())["text"] == "ok"


def test_como_dict_recusa_formato_desconhecido():
    with pytest.raises(whisper_api.ErroWhisperAPI, match="formato inesperado"):
        whisper_api._como_dict(object())


# --- ponta a ponta ------------------------------------------------------------

def test_transcrever_devolve_o_mesmo_contrato_do_backend_local(
    tmp_path, cliente_openai, executar_ok, ffmpeg_fake, monkeypatch
):
    monkeypatch.setattr(whisper_api, "_ffmpeg", lambda c=None: ffmpeg_fake)
    wav = tmp_path / "vid1.wav"
    wav.write_text("audio", encoding="utf-8")
    cliente = cliente_openai(respostas=[RESPOSTA])

    resultado = whisper_api.transcrever(
        str(wav), duracao_s=20.0, cliente=cliente, destino_dir=str(tmp_path),
        executar=executar_ok(),
    )
    assert set(resultado) >= {"idioma", "duracao_s", "segmentos"}
    assert resultado["idioma"] == "portuguese"
    assert resultado["segmentos"][0]["palavras"][0]["palavra"] == "boa"


def test_pede_timestamp_de_palavra_e_de_segmento(
    tmp_path, cliente_openai, executar_ok, ffmpeg_fake, monkeypatch
):
    # ["word"] sozinho faz a API omitir os segmentos; sem timestamp de palavra
    # a legenda word-by-word da etapa 3 não existe.
    monkeypatch.setattr(whisper_api, "_ffmpeg", lambda c=None: ffmpeg_fake)
    wav = tmp_path / "vid1.wav"
    wav.write_text("audio", encoding="utf-8")
    cliente = cliente_openai(respostas=[RESPOSTA])

    whisper_api.transcrever(
        str(wav), duracao_s=20.0, cliente=cliente, destino_dir=str(tmp_path),
        executar=executar_ok(),
    )
    chamada = cliente.chamadas[0]
    assert chamada["timestamp_granularities"] == ["word", "segment"]
    assert chamada["response_format"] == "verbose_json"
    assert chamada["model"] == settings.OPENAI_WHISPER_MODELO


def test_audio_inexistente_falha_antes_de_qualquer_gasto(tmp_path):
    with pytest.raises(whisper_api.ErroWhisperAPI, match="não encontrado"):
        whisper_api.transcrever(str(tmp_path / "nao_existe.wav"))


def test_erro_da_api_vira_erro_do_modulo(
    tmp_path, cliente_openai, executar_ok, ffmpeg_fake, monkeypatch
):
    monkeypatch.setattr(whisper_api, "_ffmpeg", lambda c=None: ffmpeg_fake)
    wav = tmp_path / "vid1.wav"
    wav.write_text("audio", encoding="utf-8")

    with pytest.raises(whisper_api.ErroWhisperAPI, match="saldo"):
        whisper_api.transcrever(
            str(wav), duracao_s=20.0,
            cliente=cliente_openai(erro=RuntimeError("saldo insuficiente")),
            destino_dir=str(tmp_path), executar=executar_ok(),
        )

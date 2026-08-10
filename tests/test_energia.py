"""Envoltória de energia e detecção de picos — numpy puro, sem áudio real."""
import numpy as np
import pytest

from pipeline import energia


# --- rms ----------------------------------------------------------------------

def test_rms_divide_em_quadros_e_centraliza_os_tempos():
    sr = 100
    sinal = np.ones(sr * 2)                      # 2 s de amplitude 1
    valores, tempos = energia.rms(sinal, sr, janela_s=0.5)

    assert len(valores) == 4                     # 2 s / 0,5 s
    assert valores == pytest.approx([1.0] * 4)
    # Centro de cada quadro, não a borda.
    assert tempos == pytest.approx([0.25, 0.75, 1.25, 1.75])


def test_rms_mede_a_energia_e_nao_a_media():
    # Onda simétrica: média zero, RMS 1. Uma média simples daria 0 e a
    # detecção de picos nunca veria nada.
    sr = 100
    sinal = np.tile([1.0, -1.0], sr)      # 200 amostras = 2 s a 100 Hz
    valores, _ = energia.rms(sinal, sr, janela_s=1.0)
    assert valores == pytest.approx([1.0, 1.0])


def test_rms_de_sinal_menor_que_a_janela():
    valores, tempos = energia.rms(np.ones(10), 100, janela_s=1.0)
    assert len(valores) == 0 and len(tempos) == 0


def test_rms_descarta_a_sobra_do_ultimo_quadro():
    valores, _ = energia.rms(np.ones(250), 100, janela_s=1.0)
    assert len(valores) == 2      # 2 quadros inteiros; os 50 restos caem fora


# --- detectar_picos -----------------------------------------------------------

def _curva(indices_altos, total=20, alto=1.0, baixo=0.1, passo=0.25):
    valores = np.full(total, baixo)
    for i, v in indices_altos.items():
        valores[i] = v
    return valores, np.arange(total) * passo


def test_detecta_os_quadros_mais_energeticos():
    valores, tempos = _curva({3: 1.0, 10: 1.0, 17: 1.0})
    picos = energia.detectar_picos(valores, tempos, percentil=90,
                                   distancia_minima_s=1.0)
    assert picos == pytest.approx([0.75, 2.5, 4.25])


def test_distancia_minima_colapsa_a_mesma_reacao():
    # Dois quadros vizinhos acima do limiar: é uma gargalhada, não duas.
    valores, tempos = _curva({3: 1.0, 4: 0.9})
    picos = energia.detectar_picos(valores, tempos, percentil=90,
                                   distancia_minima_s=1.0)
    # Fica o mais energético dos dois.
    assert picos == pytest.approx([0.75])


def test_sem_distancia_minima_os_dois_entram():
    valores, tempos = _curva({3: 1.0, 4: 0.9})
    picos = energia.detectar_picos(valores, tempos, percentil=90,
                                   distancia_minima_s=0.0)
    assert picos == pytest.approx([0.75, 1.0])


def test_sinal_constante_nao_tem_pico():
    # Sem esta guarda, o percentil de um array constante deixa TODO quadro
    # acima do limiar e o vídeo inteiro viraria um pico só.
    valores = np.full(50, 0.3)
    tempos = np.arange(50) * 0.25
    assert energia.detectar_picos(valores, tempos, percentil=90) == []


def test_silencio_nao_tem_pico():
    assert energia.detectar_picos(np.zeros(50), np.arange(50) * 0.25,
                                  percentil=90) == []


def test_curva_vazia():
    assert energia.detectar_picos(np.array([]), np.array([])) == []


def test_picos_saem_ordenados_no_tempo():
    # A seleção é gulosa por energia decrescente; a saída precisa voltar à
    # ordem cronológica para contar_picos_em fazer sentido.
    valores, tempos = _curva({17: 1.0, 3: 0.95, 10: 0.9})
    picos = energia.detectar_picos(valores, tempos, percentil=80,
                                   distancia_minima_s=1.0)
    assert picos == sorted(picos)


# --- contagem por trecho ------------------------------------------------------

def test_contar_picos_em_e_intervalo_semiaberto():
    picos = [10.0, 20.0, 30.0, 40.0]
    assert energia.contar_picos_em(picos, 15.0, 35.0) == 2
    # Início inclusivo, fim exclusivo: um pico na fronteira não conta duas
    # vezes quando dois trechos são adjacentes.
    assert energia.contar_picos_em(picos, 20.0, 30.0) == 1
    assert energia.contar_picos_em(picos, 0.0, 5.0) == 0


# --- integração do módulo -----------------------------------------------------

def test_picos_do_audio_usa_o_carregador_injetado():
    sr = 100
    sinal = np.full(sr * 10, 0.05)
    sinal[sr * 4:sr * 4 + sr // 2] = 1.0     # estouro em t=4 s

    chamadas = []

    def carregar(caminho, sample_rate):
        chamadas.append((caminho, sample_rate))
        return sinal, sr

    picos = energia.picos_do_audio(
        "audio.wav", sample_rate=sr, janela_s=0.25, percentil=95,
        distancia_minima_s=1.0, carregar=carregar,
    )
    assert chamadas == [("audio.wav", sr)]
    assert len(picos) == 1
    assert picos[0] == pytest.approx(4.25, abs=0.5)


def test_carregar_audio_sem_librosa_da_mensagem_util(monkeypatch):
    import builtins

    real = builtins.__import__

    def sem_librosa(nome, *args, **kwargs):
        if nome == "librosa":
            raise ImportError("no module named librosa")
        return real(nome, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", sem_librosa)
    with pytest.raises(energia.ErroEnergia, match="librosa"):
        energia.carregar_audio("audio.wav")

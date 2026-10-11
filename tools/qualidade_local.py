"""Bancada local da validação de qualidade (fases 1 a 4). NÃO publica nada.

    python -m tools.qualidade_local [--saida teste_local/qualidade]
                                    [--fonte caminho.mp4] [--inicio 75 --duracao 30]

Gera, a partir de um trecho local, os vídeos de comparação:

    01_base.mp4                       nada novo
    02_capa_titulo.mp4 + capa_*.png   quadro de título nos primeiros 0,3 s
    03_grafico_desfoque.mp4           modo gráfico, fundo desfocado
    04_grafico_solido.mp4             modo gráfico, fundo sólido
    05_efeitos_visuais.mp4            shutter, flash, glitch, whip pan
    06_efeitos_visuais_sonoros.mp4    tudo ligado (efeitos + SFX + mix)

mais um frame de cada um em frames/, a medição de LUFS e o comparativo.mp4.

Nenhuma chamada paga: o texto do hook é fixo e o trecho é local. A fonte é o
material interno já usado nas bancadas anteriores; como não tem gráfico, o
script insere um gráfico sintético (1920x1080) no meio, para a fase 2.
"""
import argparse
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time

log = logging.getLogger("qualidade_local")

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = "ele nunca contou isso"


# --- fonte sintética ------------------------------------------------------------

def desenhar_grafico(caminho_png, largura=1920, altura=1080):
    """Gráfico de barras com grade, eixos e rótulos (sem ruído de câmera)."""
    import cv2
    import numpy as np

    img = np.full((altura, largura, 3), 252, np.uint8)
    fonte = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, "Alunos aprovados por regiao (%)", (120, 110), fonte, 2.2,
                (30, 30, 30), 5, cv2.LINE_AA)
    x0, y0, x1, y1 = 180, 200, 1780, 940
    for i in range(6):
        y = y1 - (y1 - y0) * i // 5
        cv2.line(img, (x0, y), (x1, y), (205, 205, 205), 2)
        cv2.putText(img, str(i * 20), (70, y + 12), fonte, 1.1, (60, 60, 60), 2,
                    cv2.LINE_AA)
    valores = [62, 95, 48, 81, 70, 36]
    cores = [(200, 120, 40), (60, 160, 230), (80, 180, 90), (60, 60, 200),
             (160, 90, 160), (120, 120, 120)]
    nomes = ["Norte", "Sul", "Leste", "Oeste", "Centro", "Ilhas"]
    largura_barra = 170
    for i, v in enumerate(valores):
        bx = x0 + 60 + i * (largura_barra + 90)
        by = y1 - int((y1 - y0) * v / 100)
        cv2.rectangle(img, (bx, by), (bx + largura_barra, y1), cores[i], -1)
        cv2.putText(img, str(v), (bx + 40, by - 14), fonte, 1.3, (30, 30, 30), 3,
                    cv2.LINE_AA)
        cv2.putText(img, nomes[i], (bx + 10, y1 + 55), fonte, 1.1, (50, 50, 50),
                    2, cv2.LINE_AA)
    cv2.line(img, (x0, y0), (x0, y1), (40, 40, 40), 4)
    cv2.line(img, (x0, y1), (x1, y1), (40, 40, 40), 4)
    cv2.imwrite(caminho_png, img)
    return caminho_png


def gerar_fonte(fonte, saida_mp4, inicio_s, duracao_s, ffmpeg, pasta_tmp):
    """30 s: [0-10) real, [10-20) gráfico 1920x1080, [20-30) real. Áudio contínuo."""
    png = desenhar_grafico(os.path.join(pasta_tmp, "grafico.png"))
    terco = duracao_s / 3.0
    filtro = (
        f"[0:v]fps=30,scale=1920:1080:force_original_aspect_ratio=increase,"
        f"crop=1920:1080,setsar=1,format=yuv420p,split=2[a][b];"
        f"[a]trim=0:{terco:.3f},setpts=PTS-STARTPTS[v0];"
        f"[b]trim={2 * terco:.3f}:{duracao_s:.3f},setpts=PTS-STARTPTS[v2];"
        f"[1:v]fps=30,format=yuv420p,setsar=1,trim=0:{terco:.3f},"
        "setpts=PTS-STARTPTS[v1];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
    )
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{inicio_s}", "-t", f"{duracao_s}", "-i", fonte,
           "-loop", "1", "-t", f"{terco:.3f}", "-i", png,
           "-filter_complex", filtro, "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "aac", "-b:a", "160k", saida_mp4]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])
    return saida_mp4

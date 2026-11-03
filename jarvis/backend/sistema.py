"""Leituras da máquina: temperatura, CPU, RAM e uptime.

Temperatura vem de /sys/class/thermal (valores em milésimos de grau). Se a
pasta não existir (rodando no notebook, por exemplo), devolve None em vez de
quebrar o /status.
"""
import glob
import os
import time

import psutil


def ler_temperatura(diretorio: str):
    """Maior temperatura entre as zonas térmicas, em °C. None se não houver leitura."""
    leituras = []
    for zona in glob.glob(os.path.join(diretorio, "thermal_zone*")):
        try:
            with open(os.path.join(zona, "temp"), encoding="ascii") as arquivo:
                leituras.append(int(arquivo.read().strip()) / 1000.0)
        except (OSError, ValueError):
            continue
    return round(max(leituras), 1) if leituras else None


def cpu_percent() -> float:
    # interval=None mede desde a chamada anterior. O primeiro valor é 0.0,
    # por isso o serviço chama isto uma vez na subida (ver main.py).
    return psutil.cpu_percent(interval=None)


def ram() -> dict:
    memoria = psutil.virtual_memory()
    return {
        "total_mb": round(memoria.total / 1024 / 1024),
        "usada_mb": round(memoria.used / 1024 / 1024),
        "percent": memoria.percent,
    }


def uptime_s() -> int:
    return max(0, int(time.time() - psutil.boot_time()))

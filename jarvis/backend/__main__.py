"""Sobe o serviço: `python -m backend` (dentro de jarvis/)."""
import argparse
import logging
import sys

import uvicorn

from .config import ErroConfiguracao, carregar_config


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Backend do Jarvis")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        carregar_config()  # falha cedo, com mensagem clara, se o .env estiver errado
    except ErroConfiguracao as e:
        print(f"erro de configuração: {e}", file=sys.stderr)
        sys.exit(2)

    uvicorn.run("backend.main:criar_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

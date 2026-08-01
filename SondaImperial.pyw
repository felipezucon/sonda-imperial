"""Sonda Imperial — inicializador da aplicação.

Sobe o servidor local e abre a janela do app. Executar com pythonw (sem console):
    pythonw SondaImperial.pyw
"""

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PORT = 8674
URL = f"http://127.0.0.1:{PORT}"

# Janela dimensionada para caber na área útil do notebook (1280x672 lógicos)
WIN_W, WIN_H = 1240, 645
MIN_W, MIN_H = 780, 520


def _port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _start_server():
    from app.server import run
    threading.Thread(target=run, kwargs={"port": PORT}, daemon=True).start()
    for _ in range(60):
        if _port_open():
            return True
        time.sleep(0.25)
    return False


def main():
    if not _port_open():
        if not _start_server():
            _fatal("O servidor local não iniciou. Verifique se as dependências estão instaladas\n"
                   "(pip install -r requirements.txt) e tente novamente.")
            return

    try:
        import webview
        webview.create_window(
            "Sonda Imperial — Inteligência de Anúncios Meta",
            URL,
            width=WIN_W,
            height=WIN_H,
            min_size=(MIN_W, MIN_H),
            background_color="#0b0d17",
        )
        webview.start()
    except Exception:
        # Sem WebView2 disponível: abre no navegador padrão e mantém o servidor vivo
        import webbrowser
        webbrowser.open(URL)
        while True:
            time.sleep(3600)


def _fatal(msg):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Sonda Imperial", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    main()

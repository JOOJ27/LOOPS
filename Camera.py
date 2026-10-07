import os
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")   # sem logs do OpenCV na tela

import cv2
import curses
import locale
import numpy as np

from Config import(
    CHAR_ASPECT,
    KERNEL
)

locale.setlocale(locale.LC_ALL, '')


def bgr_to_xterm256(img):
    """Converte uma imagem BGR (h, w, 3) em índices da paleta xterm-256 (h, w).

    Cada pixel vira UMA cor da paleta do terminal:
      - cubo 6x6x6 (índices 16..231) para pixels coloridos. Os 6 níveis do
        xterm NÃO são igualmente espaçados: 0, 95, 135, 175, 215, 255;
      - rampa de cinza (232..255, de 8 a 238 em passos de 10) para pixels
        pouco saturados, que no cubo ficariam com só 6 tons de cinza.
    """
    b, g, r = (img[..., i].astype(np.int32) for i in range(3))

    def level(ch):
        # nível mais próximo de {0, 95, 135, 175, 215, 255}
        return (
            (ch >= 48).astype(np.int32) + (ch >= 115) + (ch >= 155)
            + (ch >= 195) + (ch >= 235)
        )

    cube = 16 + 36 * level(r) + 6 * level(g) + level(b)

    lum = (r * 299 + g * 587 + b * 114) // 1000
    gray = 232 + np.clip((lum - 3) // 10, 0, 23)
    gray = np.where(lum < 4, 16, gray)        # preto de verdade
    gray = np.where(lum > 243, 231, gray)     # branco de verdade

    spread = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    return np.where(spread < 14, gray, cube).astype(np.uint8)


class Camera:

    def __init__(self):
        self.TIPO = False
        self.COLOR = True      # também devolve a cor de cada célula
        self.video = cv2.VideoCapture(0)
        self._update_charset()


    def _update_charset(self):
        if self.TIPO:
            chars = ["█", "▓", "▒", "░", "▉", "▊", "▋", "▌", "▍", "▎",
                     "▏", "▐", "▄", "▁", "▂", "▃", "▅", "▆", "▇", " "]
        else:
            chars = "@%#*+=-:. "
        self.ascii_chars = np.array(list(chars))

    def get_frame(self, max_w, max_h):
        """Retorna (linhas ASCII, cores) que cabem em max_w x max_h.

        `cores` é um array uint8 (linhas, colunas) com o índice xterm-256 de
        cada célula, ou None se COLOR estiver desligado.
        """
        self._update_charset()
        ok, frame = self.video.read()
        if not ok or max_w < 1 or max_h < 1:
            return None

        frame = cv2.flip(frame, 1)
        sharp = cv2.filter2D(frame, -1, KERNEL)

        gray = cv2.cvtColor(sharp, cv2.COLOR_BGR2GRAY)
        fh, fw = gray.shape

        # maior escala em que a imagem cabe na largura E na altura
        scale = min(max_w / fw, max_h / (fh * CHAR_ASPECT))
        cols = max(1, int(fw * scale))
        rows = max(1, int(fh * scale * CHAR_ASPECT))

        small = cv2.resize(gray, (cols, rows), interpolation=cv2.INTER_AREA)

        # mapeia brilho -> índice do caractere (vetorizado, sem loop em Python)
        idx = small.astype(np.uint16) * (len(self.ascii_chars) - 1) // 255
        lines = ["".join(row) for row in self.ascii_chars[idx]]

        colors = None
        if self.COLOR:
            # a cor vem da imagem sem o filtro de nitidez (mais natural)
            small_bgr = cv2.resize(frame, (cols, rows), interpolation=cv2.INTER_AREA)
            colors = bgr_to_xterm256(small_bgr)

        return lines, colors

    def get_ascii(self, max_w, max_h):
        """Só as linhas ASCII (preto e branco)."""
        result = self.get_frame(max_w, max_h)
        return None if result is None else result[0]

    def release(self):
        self.video.release()


def draw_camera(win, camera):
    h, w = win.getmaxyx()          # tamanho atual do inner, lido a cada frame
    lines = camera.get_ascii(w, h)
    if lines is None:
        return

    win.erase()                    # sem win.box(): a borda é do outer

    # centraliza a imagem dentro da janela
    y0 = (h - len(lines)) // 2
    x0 = (w - len(lines[0])) // 2

    for i, line in enumerate(lines):
        try:
            win.addstr(y0 + i, x0, line)
        except curses.error:
            pass                   # erro da última célula (canto inferior direito)

    win.noutrefresh()
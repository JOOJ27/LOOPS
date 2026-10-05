import cv2
import curses
import locale
import numpy as np

locale.setlocale(locale.LC_ALL, '')


class Camera:
    CHAR_ASPECT = 0.5  # largura/altura de uma célula do terminal

    def __init__(self):
        self.TIPO = False
        self.video = cv2.VideoCapture(0)
        self._update_charset()

    def _update_charset(self):
        if self.TIPO:
            chars = ["█", "▓", "▒", "░", "▉", "▊", "▋", "▌", "▍", "▎",
                     "▏", "▐", "▄", "▁", "▂", "▃", "▅", "▆", "▇", " "]
        else:
            chars = "@%#*+=-:. "
        self.ascii_chars = np.array(list(chars))

    def get_ascii(self, max_w, max_h):
        self._update_charset()
        """Retorna uma lista de linhas que cabe em max_w x max_h."""
        ok, frame = self.video.read()
        if not ok or max_w < 1 or max_h < 1:
            return None

        frame = cv2.flip(frame, 1)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fh, fw = gray.shape

        # maior escala em que a imagem cabe na largura E na altura
        scale = min(max_w / fw, max_h / (fh * self.CHAR_ASPECT))
        cols = max(1, int(fw * scale))
        rows = max(1, int(fh * scale * self.CHAR_ASPECT))

        small = cv2.resize(gray, (cols, rows), interpolation=cv2.INTER_AREA)

        # mapeia brilho -> índice do caractere (vetorizado, sem loop em Python)
        idx = small.astype(np.uint16) * (len(self.ascii_chars) - 1) // 255
        return ["".join(row) for row in self.ascii_chars[idx]]

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
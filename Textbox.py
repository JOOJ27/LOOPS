import curses


class TextBox:
    """Caixa de texto de uma linha para curses, não bloqueante.

    Ela não lê o teclado sozinha: o loop principal lê a tecla e entrega
    para handle_key(). Assim o vídeo continua rodando enquanto você digita.

    Uso:
        box = TextBox(layout["input"][1])

        try:
            key = stdscr.get_wch()      # get_wch (e não getch) para aceitar acentos
        except curses.error:
            key = None                  # nenhuma tecla neste ciclo

        if key is not None:
            msg = box.handle_key(key)   # devolve o texto ao apertar Enter
            if msg:
                ...                     # envia msg ao chat / rede

        box.draw()
    """

    def __init__(self, win, placeholder="Type your message...", max_length=500):
        self.win = win
        self.placeholder = placeholder
        self.max_length = max_length
        self.text = ""
        self.cursor = 0     # posição do cursor dentro do texto
        self.offset = 0     # primeiro caractere visível (rolagem horizontal)

    def set_window(self, win):
        """Troque a janela depois de recriar o layout (KEY_RESIZE)."""
        self.win = win

    # ---------- entrada ----------

    def handle_key(self, key):
        """Processa uma tecla. Retorna o texto digitado se foi Enter, senão None."""
        if isinstance(key, str):                 # get_wch devolve str para caracteres
            if key in ("\n", "\r"):
                return self.submit()
            if key in ("\x7f", "\b"):            # backspace
                self._backspace()
            elif key == "\x15":                  # Ctrl+U: limpa a linha
                self.clear()
            elif key.isprintable():
                self._insert(key)
            return None

        # teclas especiais chegam como int
        if key == curses.KEY_ENTER:
            return self.submit()
        if key == curses.KEY_BACKSPACE:
            self._backspace()
        elif key == curses.KEY_DC:               # Delete
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(self.text)
        return None

    def submit(self):
        """Devolve o texto atual (sem espaços nas pontas) e limpa a caixa."""
        msg = self.text.strip()
        self.clear()
        return msg or None

    def clear(self):
        self.text = ""
        self.cursor = 0
        self.offset = 0

    def _insert(self, ch):
        if len(self.text) >= self.max_length:
            return
        self.text = self.text[:self.cursor] + ch + self.text[self.cursor:]
        self.cursor += 1

    def _backspace(self):
        if self.cursor > 0:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    # ---------- desenho ----------

    def draw(self):
        if self.win is None:
            return
        h, w = self.win.getmaxyx()
        if w < 2 or h < 1:
            return

        self._scroll(w)
        self.win.erase()

        if not self.text:
            # cursor em bloco + placeholder apagado
            self._put(0, " ", curses.A_REVERSE)
            self._put(1, self.placeholder[:w - 2], curses.A_DIM)
        else:
            self._put(0, self.text[self.offset:self.offset + w])
            cx = self.cursor - self.offset
            under = self.text[self.cursor] if self.cursor < len(self.text) else " "
            self._put(cx, under, curses.A_REVERSE)   # cursor desenhado, não o do terminal

        self.win.noutrefresh()

    def _scroll(self, w):
        """Mantém o cursor dentro da área visível."""
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor - self.offset >= w:
            self.offset = self.cursor - w + 1

    def _put(self, x, text, attr=0):
        try:
            self.win.addstr(0, x, text, attr)
        except curses.error:
            pass    # escrever na última célula da janela levanta erro no curses

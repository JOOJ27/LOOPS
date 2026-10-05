import curses
import textwrap
import threading
from collections import deque


class ChatBox:
    """Janela de chat: mostra as mensagens no formato  Autor: texto.
    Com show_time=True, a hora aparece antes:  HH:MM Autor: texto.

    Uso:
        chat = ChatBox(layout["chat"][1])

        chat.add_message("Você", "oi!", mine=True)    # mensagem enviada por você
        chat.add_message("Pessoa 2", "tudo bem?")      # mensagem recebida

        chat.draw()                                    # a cada ciclo do loop

    add_message pode ser chamado de outra thread (por exemplo, a que recebe
    dados da rede): o acesso à lista de mensagens é protegido por um lock.
    """

    PAIR_ME = 10       # ids dos pares de cor (altos para não colidir com os seus)
    PAIR_OTHER = 11

    def __init__(self, win, max_messages=200, show_time=False):
        self.win = win
        self.show_time = show_time   # True mostra a hora antes do autor
        self.messages = deque(maxlen=max_messages)   # (hora, autor, texto, é_meu)
        self.scroll = 0              # 0 = mostrando as mensagens mais recentes
        self._lines = []             # mensagens já quebradas em linhas (cache)
        self._lines_width = None     # largura usada no cache
        self._lock = threading.Lock()
        self._init_colors()

    def set_window(self, win):
        """Troque a janela depois de recriar o layout (KEY_RESIZE)."""
        self.win = win
        self._lines_width = None     # largura mudou: refaz a quebra de linhas

    # ---------- mensagens ----------

    def add_message(self, author, text, mine=False, timestamp=None):
        text = text.strip()
        if not text:
            return
        with self._lock:
            self.messages.append((author, text, mine))
            self._lines_width = None     # invalida o cache
            self.scroll = 0              # volta para a mensagem mais nova

    def handle_key(self, key):
        """PageUp / PageDown rolam o histórico. Retorna True se usou a tecla."""
        if self.win is None:
            return False
        page = max(1, self.win.getmaxyx()[0] - 1)
        if key == curses.KEY_PPAGE:
            self.scroll += page
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - page)
        else:
            return False
        return True

    # ---------- desenho ----------

    def draw(self):
        if self.win is None:
            return
        h, w = self.win.getmaxyx()
        if w < 2 or h < 1:
            return

        with self._lock:
            if self._lines_width != w:
                self._lines = self._build_lines(w)
                self._lines_width = w
            total = len(self._lines)
            self.scroll = min(self.scroll, max(0, total - h))
            end = total - self.scroll
            visible = self._lines[max(0, end - h):end]

        self.win.erase()
        for y, segments in enumerate(visible):
            x = 0
            for text, attr in segments:
                try:
                    self.win.addnstr(y, x, text, w - x, attr)
                except curses.error:
                    pass    # última célula da janela levanta erro no curses
                x += len(text)
                if x >= w:
                    break
        self.win.noutrefresh()

    # ---------- internos ----------

    def _init_colors(self):
        self.attr_me = curses.A_BOLD
        self.attr_other = curses.A_BOLD
        if not curses.has_colors():
            return
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(self.PAIR_ME, curses.COLOR_GREEN, -1)
            curses.init_pair(self.PAIR_OTHER, curses.COLOR_CYAN, -1)
            self.attr_me = curses.color_pair(self.PAIR_ME) | curses.A_BOLD
            self.attr_other = curses.color_pair(self.PAIR_OTHER) | curses.A_BOLD
        except curses.error:
            pass

    def _build_lines(self, w):
        """Quebra cada mensagem em linhas que cabem em w colunas.
        Cada linha é uma lista de (texto, atributo)."""
        lines = []
        for  author, text, mine in self.messages:
            head_author = f"{author}: "
            indent = len(head_author)

            parts = textwrap.wrap(text, width=max(1, w - indent), break_long_words=True) or [""]
            name_attr = self.attr_me if mine else self.attr_other

            # primeira linha: hora + autor + início do texto
            lines.append([
                (head_author, name_attr),
                (parts[0], curses.A_NORMAL),
            ])
            # continuação: alinhada com o começo do texto
            for p in parts[1:]:
                lines.append([(" " * indent, curses.A_NORMAL), (p, curses.A_NORMAL)])
        return lines
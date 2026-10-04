"""Desktop window over the local content database.

The window only displays rows. Fetching stays in ``python -m server.ingestion.cli``.
"""

import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from server.reader.library import Article, FeedCard, Library, display_text


def main() -> int:
    """Open the reader, or print why the database cannot be opened."""

    try:
        library = Library.open()
    except (FileNotFoundError, ValueError) as exc:
        print(f"failed: {exc}")
        return 1
    try:
        ReaderApp(library).run()
    finally:
        library.close()
    return 0


class ReaderApp:
    """Three panes: sources, titles, and the selected article."""

    def __init__(self, library: Library) -> None:
        self._library = library
        self._cards: list[FeedCard] = []
        self._root = tk.Tk()
        self._root.title("GameDev")
        self._root.geometry("1100x720")
        self._source = tk.StringVar(value="All")
        self._query = tk.StringVar()
        self._build()
        self.refresh()

    def run(self) -> None:
        self._root.mainloop()

    def refresh(self) -> None:
        """Reload the list from SQLite using the current source and query."""

        source_id = self._selected_source_id()
        self._cards = self._library.list_items(source_id=source_id, query=self._query.get())
        self._list.delete(0, tk.END)
        for card in self._cards:
            self._list.insert(tk.END, _card_label(card))
        self._status.configure(text=f"{len(self._cards)} items")
        if self._cards:
            self._list.selection_set(0)
            self._show(0)
        else:
            self._show_empty()

    def _build(self) -> None:
        bar = ttk.Frame(self._root, padding=8)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Source").pack(side=tk.LEFT)
        names = ["All", *[name for _source_id, name in self._library.sources()]]
        source_box = ttk.Combobox(
            bar,
            textvariable=self._source,
            values=names,
            state="readonly",
            width=32,
        )
        source_box.pack(side=tk.LEFT, padx=(6, 12))
        source_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        search = ttk.Entry(bar, textvariable=self._query)
        search.pack(side=tk.LEFT, fill=tk.X, expand=True)
        search.bind("<Return>", lambda _event: self.refresh())
        ttk.Button(bar, text="Search", command=self.refresh).pack(side=tk.LEFT, padx=(8, 0))

        body = ttk.Panedwindow(self._root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        self._list = tk.Listbox(body, activestyle="none", exportselection=False)
        self._list.configure(font=tkfont.nametofont("TkDefaultFont"))
        self._list.bind("<<ListboxSelect>>", self._on_select)
        body.add(self._list, weight=2)

        detail = ttk.Frame(body, padding=12)
        body.add(detail, weight=3)
        self._title = ttk.Label(detail, text="", wraplength=640, font=("", 18, "bold"))
        self._title.pack(anchor=tk.W)
        self._meta = ttk.Label(detail, text="", wraplength=640)
        self._meta.pack(anchor=tk.W, pady=(4, 8))
        self._body = ScrolledText(detail, wrap=tk.WORD, state=tk.DISABLED, relief=tk.FLAT)
        self._body.pack(fill=tk.BOTH, expand=True)
        actions = ttk.Frame(detail)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._open = ttk.Button(actions, text="Open original", command=self._open_original)
        self._open.pack(side=tk.LEFT)
        self._open_url = ""

        self._status = ttk.Label(self._root, padding=(8, 4), anchor=tk.W)
        self._status.pack(fill=tk.X)

    def _selected_source_id(self) -> str | None:
        selected = self._source.get()
        names = {name: source_id for source_id, name in self._library.sources()}
        if selected == "All":
            return None
        return names.get(selected)

    def _on_select(self, _event: object) -> None:
        selection: tuple[int, ...] = self._list.curselection()  # type: ignore[no-untyped-call]
        if not selection:
            return
        self._show(selection[0])

    def _show(self, index: int) -> None:
        card = self._cards[index]
        article = self._library.get(card.id)
        if article is None:
            self._show_empty()
            return
        self._title.configure(text=article.title)
        self._meta.configure(text=_meta(article))
        self._open_url = article.canonical_url
        self._open.state(["!disabled"])
        self._write(display_text(article))

    def _show_empty(self) -> None:
        self._title.configure(text="No items")
        self._meta.configure(text="")
        self._open_url = ""
        self._open.state(["disabled"])
        self._write("Fetch content with python -m server.ingestion.cli fetch-all.")

    def _write(self, text: str) -> None:
        self._body.configure(state=tk.NORMAL)
        self._body.delete("1.0", tk.END)
        self._body.insert("1.0", text)
        self._body.configure(state=tk.DISABLED)

    def _open_original(self) -> None:
        if self._open_url:
            webbrowser.open(self._open_url)


def _card_label(card: FeedCard) -> str:
    when = card.published_at.date().isoformat() if card.published_at else "undated"
    return f"{when}  {card.source_name}  {card.title}"


def _meta(article: Article) -> str:
    parts = [article.source_name]
    if article.author:
        parts.append(article.author)
    if article.published_at:
        parts.append(article.published_at.strftime("%Y-%m-%d %H:%M"))
    return " · ".join(parts)

"""A tkinter stand-in so the window logic can be exercised without a display."""
import sys
import types


class _Var:
    def __init__(self, master=None, value=None, name=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class StringVar(_Var):
    def __init__(self, master=None, value="", name=None):
        super().__init__(master, value)


class IntVar(_Var):
    def __init__(self, master=None, value=0, name=None):
        super().__init__(master, value)


class BooleanVar(_Var):
    def __init__(self, master=None, value=False, name=None):
        super().__init__(master, value)


class TclError(Exception):
    pass


class Widget:
    created = []

    def __init__(self, master=None, **kw):
        self.master = master
        self._kw = dict(kw)
        self._text = ""
        Widget.created.append(self)

    def pack(self, **kw): pass
    def grid(self, **kw): pass
    def place(self, **kw): pass
    def pack_forget(self): pass

    def configure(self, **kw): self._kw.update(kw)
    config = configure
    def cget(self, key): return self._kw.get(key)
    def __setitem__(self, key, value): self._kw[key] = value
    def __getitem__(self, key): return self._kw.get(key)

    def insert(self, index, text): self._text += text
    def delete(self, *a): self._text = ""
    def get(self, *a): return self._text
    def see(self, *a): pass
    def yview(self, *a): pass
    def xview(self, *a): pass
    def set(self, *a): pass

    def bind(self, *a, **kw): pass
    def destroy(self): pass
    def focus_set(self): pass
    def state(self, *a): pass


class Tk(Widget):
    def __init__(self, *a, **kw):
        super().__init__(None)
        self.pending = []
        self.clipboard = ""
        self.destroyed = False

    def title(self, *a): pass
    def minsize(self, *a): pass
    def geometry(self, *a): pass
    def protocol(self, name, callback): self._on_close = callback
    def after(self, delay, callback=None, *args):
        if callback is not None:
            self.pending.append((delay, callback, args))
        return "id"
    def after_cancel(self, *a): pass
    def update_idletasks(self): pass
    def update(self): pass
    def mainloop(self): pass
    def withdraw(self): pass
    def destroy(self): self.destroyed = True
    def clipboard_clear(self): self.clipboard = ""
    def clipboard_append(self, text): self.clipboard += text

    def run_pending(self, max_delay=100000):
        """Runs callbacks scheduled through after()."""
        todo, self.pending = self.pending, []
        for delay, callback, args in todo:
            if delay <= max_delay:
                callback(*args)


class Text(Widget):
    pass


class Frame(Widget):
    pass


def _make_ttk():
    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "Label", "Button", "Entry", "Combobox", "Spinbox",
                 "Checkbutton", "LabelFrame", "Scrollbar", "Notebook", "Radiobutton"):
        ttk.__dict__[name] = type(name, (Widget,), {})

    class Style(Widget):
        def theme_use(self, *a): pass
        def configure(self, *a, **kw): pass

    ttk.Style = Style
    return ttk


def _make_messagebox():
    mb = types.ModuleType("tkinter.messagebox")
    mb.calls = []

    def record(kind, default=None):
        def fn(title=None, message=None, **kw):
            mb.calls.append((kind, message))
            return default
        return fn

    mb.showinfo = record("info")
    mb.showwarning = record("warning")
    mb.showerror = record("error")
    mb.askokcancel = record("ask", True)
    mb.askyesno = record("ask", True)
    return mb


def install():
    """Replaces tkinter in sys.modules. Call before importing the module under test."""
    tk = types.ModuleType("tkinter")
    tk.Tk = Tk
    tk.Text = Text
    tk.Frame = Frame
    tk.StringVar = StringVar
    tk.IntVar = IntVar
    tk.BooleanVar = BooleanVar
    tk.TclError = TclError
    tk.END = "end"

    ttk = _make_ttk()
    mb = _make_messagebox()
    tk.ttk = ttk
    tk.messagebox = mb

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.messagebox"] = mb
    return tk, ttk, mb

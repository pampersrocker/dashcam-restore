"""Small tkinter front-end for dashcam-restore."""
import contextlib
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import core


class QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Dashcam MP4 restore')
        self.geometry('720x480')
        self.minsize(520, 360)
        self.q = queue.Queue()
        self.broken = tk.StringVar()
        self.ref = tk.StringVar()
        self.out = tk.StringVar()

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill='both', expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(5, weight=1)

        ttk.Label(frm, text='Broken video').grid(row=0, column=0, sticky='w', pady=2)
        ttk.Entry(frm, textvariable=self.broken).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(frm, text='Browse...', command=self.pick_broken).grid(row=0, column=2)

        ttk.Label(frm, text='Reference video').grid(row=1, column=0, sticky='w', pady=2)
        ttk.Entry(frm, textvariable=self.ref).grid(row=1, column=1, sticky='ew', padx=6)
        ttk.Button(frm, text='Browse...', command=self.pick_ref).grid(row=1, column=2)

        ttk.Label(frm, text='Output file').grid(row=2, column=0, sticky='w', pady=2)
        ttk.Entry(frm, textvariable=self.out).grid(row=2, column=1, sticky='ew', padx=6)
        ttk.Button(frm, text='Browse...', command=self.pick_out).grid(row=2, column=2)

        ttk.Label(frm, text='Log').grid(row=4, column=0, sticky='w')
        self.btn = ttk.Button(frm, text='Restore', command=self.start)
        self.btn.grid(row=4, column=2, sticky='e', pady=(6, 2))
        self.log = scrolledtext.ScrolledText(frm, height=10, state='disabled', font=('Consolas', 9))
        self.log.grid(row=5, column=0, columnspan=3, sticky='nsew')
        self.after(100, self.pump)

    def pick_broken(self):
        p = filedialog.askopenfilename(title='Broken video', filetypes=[('MP4 video', '*.mp4 *.MP4'), ('All files', '*.*')])
        if p:
            self.broken.set(p)
            if not self.out.get():
                pp = Path(p)
                self.out.set(str(pp.with_name(pp.stem + '_recovered.MP4')))

    def pick_ref(self):
        p = filedialog.askopenfilename(title='Reference video', filetypes=[('MP4 video', '*.mp4 *.MP4'), ('All files', '*.*')])
        if p:
            self.ref.set(p)

    def pick_out(self):
        p = filedialog.asksaveasfilename(title='Output file', defaultextension='.MP4',
                                         filetypes=[('MP4 video', '*.mp4 *.MP4')])
        if p:
            self.out.set(p)

    def append(self, s):
        self.log.configure(state='normal')
        self.log.insert('end', s)
        self.log.see('end')
        self.log.configure(state='disabled')

    def pump(self):
        try:
            while True:
                self.append(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def start(self):
        broken, ref, out = self.broken.get().strip(), self.ref.get().strip(), self.out.get().strip()
        if not (broken and ref and out):
            messagebox.showwarning('Missing input', 'Select the broken video, the reference video and an output file.')
            return
        argv = [broken, '--ref', ref, '-o', out]
        self.btn.configure(state='disabled')
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')
        threading.Thread(target=self.work, args=(argv,), daemon=True).start()

    def work(self, argv):
        w = QueueWriter(self.q)
        try:
            with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                try:
                    core.main(argv)
                except SystemExit as e:
                    if e.code not in (None, 0):
                        print(e.code)
                except Exception as e:
                    print('error: %s: %s' % (type(e).__name__, e))
                else:
                    print('Done.')
        finally:
            self.after(0, lambda: self.btn.configure(state='normal'))


def main():
    App().mainloop()


if __name__ == '__main__':
    main()

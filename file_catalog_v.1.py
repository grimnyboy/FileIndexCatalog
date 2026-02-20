import os
import fitz  # PyMuPDF
import docx2txt
import pytesseract
import extract_msg
import customtkinter as ctk
import json
import subprocess
import datetime
import threading
import logging
from PIL import Image
from tkinter import filedialog, messagebox, Menu
from whoosh.index import create_in, open_dir, exists_in
from whoosh.fields import Schema, TEXT, ID, STORED
from whoosh.qparser import QueryParser
from whoosh.analysis import LowercaseFilter, RegexTokenizer

# --- ЛОГВАНЕ ---
logging.basicConfig(
    filename="catalog_errors.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)

fitz.TOOLS.mupdf_display_errors(False)
pytesseract.pytesseract.tesseract_cmd = r"C:\Tesseract-OCR\tesseract.exe"

my_analyzer = RegexTokenizer() | LowercaseFilter()
schema = Schema(
    title=TEXT(stored=True),
    path=ID(stored=True),
    content=TEXT(analyzer=my_analyzer, stored=False),
    size=STORED,
    mtime=STORED,
    ext=STORED
)

class SearchApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AI Файлов Каталог v2.1")
        self.geometry("1000x850")
        
        self.index_path = ""
        self.source_path = "" # Инициализираме празна стойност
        self.config_file = "config.json"
        self.last_results = []
        
        self.setup_ui()
        self.load_config()

    def setup_ui(self):
        # Контролен панел
        self.top_frame = ctk.CTkFrame(self)
        self.top_frame.pack(pady=10, padx=20, fill="x")
        
        ctk.CTkButton(self.top_frame, text="1. Папка за Каталог", command=self.select_index_folder).pack(side="left", padx=10, pady=10)
        ctk.CTkButton(self.top_frame, text="2. Папка за Сканиране", command=self.browse_source).pack(side="left", padx=10, pady=10)
        
        self.index_status_label = ctk.CTkLabel(self, text="Статус: Не е избран каталог", text_color="orange")
        self.index_status_label.pack()

        self.btn_index = ctk.CTkButton(self, text="ОБНОВИ / СЪЗДАЙ КАТАЛОГ", fg_color="#2e7d32", command=self.start_indexing_thread)
        self.btn_index.pack(pady=5)

        self.progress_bar = ctk.CTkProgressBar(self, width=800)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=5)

        # Търсене и Сортиране
        self.search_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.search_frame.pack(pady=10)
        
        self.search_entry = ctk.CTkEntry(self.search_frame, placeholder_text="Търсене на дума или част (напр. cv2)...", width=400)
        self.search_entry.pack(side="left", padx=5)
        self.search_entry.bind("<Return>", lambda e: self.run_search())

        self.btn_search = ctk.CTkButton(self.search_frame, text="ТЪРСИ", width=100, command=self.run_search)
        self.btn_search.pack(side="left", padx=5)

        self.sort_option = ctk.CTkOptionMenu(self.search_frame, values=["Име", "Дата (Нови)", "Размер", "Тип"], command=self.sort_results)
        self.sort_option.set("Име")
        self.sort_option.pack(side="left", padx=5)

        self.results_list = ctk.CTkTextbox(self, width=950, height=450, font=("Consolas", 12))
        self.results_list.pack(pady=10, padx=20)
        
        self.results_list.bind("<Double-Button-1>", self.on_double_click)
        self.results_list.bind("<Button-3>", self.show_context_menu)

        self.context_menu = Menu(self, tearoff=0)
        self.context_menu.add_command(label="Отвори файла", command=self.open_selected_file)
        self.context_menu.add_command(label="Отвори мястото (Explorer)", command=self.open_file_folder)

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    data = json.load(f)
                    self.index_path = data.get("index_path", "")
                    self.source_path = data.get("source_path", "")
                    if self.index_path:
                        self.check_index_validity()
            except: pass

    def save_config(self):
        with open(self.config_file, "w") as f:
            json.dump({"index_path": self.index_path, "source_path": self.source_path}, f)

    def check_index_validity(self):
        idx_dir = os.path.join(self.index_path, "search_index_db")
        if os.path.exists(idx_dir) and exists_in(idx_dir):
            self.index_status_label.configure(text=f"Каталог зареден: {self.index_path}", text_color="lightgreen")
            return True
        self.index_status_label.configure(text="Нужно е първоначално индексиране", text_color="orange")
        return False

    def select_index_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.index_path = path
            self.save_config()
            self.check_index_validity()

    def browse_source(self):
        path = filedialog.askdirectory()
        if path:
            self.source_path = path
            self.save_config()

    def extract_content(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext in ['.txt', '.py', '.js', '.html', '.cpp', '.sql', '.json', '.ini']:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    return f.read()
            elif ext == '.pdf':
                text = ""
                with fitz.open(file_path) as doc:
                    for page in doc:
                        text += page.get_text()
                return text
            elif ext == '.docx':
                return docx2txt.process(file_path)
        except Exception as e:
            logging.error(f"Грешка при извличане от {file_path}: {e}")
        return None

    def start_indexing_thread(self):
        if not self.index_path or not self.source_path:
            messagebox.showerror("Грешка", "Изберете папка за каталог и папка за сканиране!")
            return
        threading.Thread(target=self.run_indexing, daemon=True).start()

    def run_indexing(self):
        idx_dir = os.path.join(self.index_path, "search_index_db")
        if not os.path.exists(idx_dir): os.makedirs(idx_dir)

        ix = create_in(idx_dir, schema)
        writer = ix.writer()
        
        all_files = []
        for r, d, files in os.walk(self.source_path):
            for f in files:
                if f.lower().endswith(('.txt', '.pdf', '.docx', '.py', '.js', '.cpp', '.sql', '.json')):
                    all_files.append(os.path.join(r, f))

        for i, path in enumerate(all_files):
            content = self.extract_content(path)
            if content:
                stats = os.stat(path)
                writer.add_document(
                    title=os.path.basename(path),
                    path=path,
                    content=content,
                    size=stats.st_size,
                    mtime=stats.st_mtime,
                    ext=os.path.splitext(path)[1].lower()
                )
            self.progress_bar.set((i + 1) / len(all_files))
            self.update_idletasks()

        writer.commit()
        self.check_index_validity()
        messagebox.showinfo("Успех", "Индексирането приключи!")

    def run_search(self):
        idx_dir = os.path.join(self.index_path, "search_index_db")
        if not exists_in(idx_dir):
            messagebox.showwarning("Грешка", "Няма зареден каталог!")
            return

        ix = open_dir(idx_dir)
        word = self.search_entry.get().strip().lower()
        if not word: return

        query_str = f"*{word}*" if "*" not in word else word
        self.last_results = []
        
        with ix.searcher() as searcher:
            query = QueryParser("content", ix.schema).parse(query_str)
            results = searcher.search(query, limit=500)
            for hit in results:
                self.last_results.append({
                    "title": hit['title'], "path": hit['path'],
                    "size": hit.get('size', 0), "mtime": hit.get('mtime', 0), "ext": hit.get('ext', '')
                })
        
        self.sort_results(self.sort_option.get())

    def sort_results(self, choice):
        if not self.last_results: return
        if choice == "Име": self.last_results.sort(key=lambda x: x['title'].lower())
        elif choice == "Дата (Нови)": self.last_results.sort(key=lambda x: x['mtime'], reverse=True)
        elif choice == "Размер": self.last_results.sort(key=lambda x: x['size'], reverse=True)
        elif choice == "Тип": self.last_results.sort(key=lambda x: x['ext'])
        
        self.display_results()

    def display_results(self):
        self.results_list.delete("0.0", "end")
        for res in self.last_results:
            dt = datetime.datetime.fromtimestamp(res['mtime']).strftime('%Y-%m-%d %H:%M')
            size_kb = round(res['size'] / 1024, 2)
            self.results_list.insert("end", f"📄 {res['title']} | {size_kb} KB | {dt}\n📍 Път: {res['path']}\n{'-'*80}\n")

    def get_path_under_cursor(self):
        try:
            line = self.results_list.index("insert").split(".")[0]
            for i in range(int(line)-2, int(line)+3):
                if i < 1: continue
                txt = self.results_list.get(f"{i}.0", f"{i}.end")
                if "📍 Път: " in txt: return txt.replace("📍 Път: ", "").strip()
        except: return None

    def on_double_click(self, event):
        path = self.get_path_under_cursor()
        if path and os.path.exists(path): os.startfile(path)

    def show_context_menu(self, event):
        self.results_list.mark_set("insert", self.results_list.index(f"@{event.x},{event.y}"))
        self.results_list.focus_set()
        self.context_menu.post(event.x_root, event.y_root)

    def open_selected_file(self):
        self.on_double_click(None)

    def open_file_folder(self):
        path = self.get_path_under_cursor()
        if path and os.path.exists(path):
            subprocess.run(['explorer', '/select,', os.path.normpath(path)])

if __name__ == "__main__":
    app = SearchApp()
    app.mainloop()

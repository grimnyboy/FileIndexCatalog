import os
import fitz  # PyMuPDF
import docx2txt
import pytesseract
import extract_msg
import pypff
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

# --- ЛОГВАНЕ НА ГРЕШКИ ВЪВ ФАЙЛ ---
logging.basicConfig(
    filename="catalog_errors.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)

# --- ЗАГЛУШАВАНЕ НА MUPDF ГРЕШКИ ---
fitz.TOOLS.mupdf_display_errors(False)

# --- КОНФИГУРАЦИЯ TESSERACT ---
pytesseract.pytesseract.tesseract_cmd = r"C:\Tesseract-OCR\tesseract.exe"

# Анализатор за търсене по части от думи и кодове
my_analyzer = RegexTokenizer() | LowercaseFilter()

schema = Schema(
    title=TEXT(stored=True),
    path=ID(stored=True),
    content=TEXT(analyzer=my_analyzer, stored=False),
    size=STORED,
    mtime=STORED,
    ext=STORED
)

SUPPORTED_EXTENSIONS = ('.txt', '.pdf', '.docx', '.msg', '.py', '.js', '.cpp', '.json', '.sql', '.html', '.css', '.bat', '.ini', '.pst', '.ost')


class SearchApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AI Архиватор - Търсене в съществуващи каталози")
        self.geometry("950x900")

        self.index_path = ""
        self.source_path = ""  # FIX: инициализиран в __init__, не в browse_source
        self.config_file = "config.json"
        self.last_results = []
        self.result_line_map = {}  # FIX: mapping ред -> path за надежден lookup
        self._indexing_active = False

        self.setup_ui()
        self.load_config()

    def setup_ui(self):
        # 1. Контролен панел
        self.top_frame = ctk.CTkFrame(self)
        self.top_frame.pack(pady=10, padx=20, fill="x")

        self.btn_index_dir = ctk.CTkButton(self.top_frame, text="1. Папка за Каталог", command=self.select_index_folder)
        self.btn_index_dir.pack(side="left", padx=10, pady=10)

        self.btn_source_dir = ctk.CTkButton(self.top_frame, text="2. Папка за Сканиране", command=self.browse_source)
        self.btn_source_dir.pack(side="left", padx=10, pady=10)

        # Показва избраната папка за сканиране
        self.source_label = ctk.CTkLabel(self.top_frame, text="Не е избрана папка за сканиране", text_color="gray")
        self.source_label.pack(side="left", padx=10)

        self.index_status_label = ctk.CTkLabel(self, text="Статус: Няма избран каталог", text_color="orange")
        self.index_status_label.pack()

        self.btn_index = ctk.CTkButton(self, text="ОБНОВИ/СЪЗДАЙ КАТАЛОГ", fg_color="#2e7d32", command=self.run_indexing)
        self.btn_index.pack(pady=5)

        # Лейбъл за прогрес с текст (файл X от Y)
        self.progress_label = ctk.CTkLabel(self, text="")
        self.progress_label.pack()

        self.progress_bar = ctk.CTkProgressBar(self, width=700)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=5)

        # 2. Търсене
        self.search_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.search_frame.pack(pady=10)

        self.search_entry = ctk.CTkEntry(self.search_frame, placeholder_text="Въведи дума за търсене...", width=400)
        self.search_entry.pack(side="left", padx=5)
        self.search_entry.bind("<Return>", lambda e: self.run_search())

        self.btn_search = ctk.CTkButton(self.search_frame, text="ТЪРСИ", width=100, command=self.run_search)
        self.btn_search.pack(side="left", padx=5)

        # 3. Сортиране + брояч резултати
        self.sort_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sort_frame.pack(pady=5)

        ctk.CTkLabel(self.sort_frame, text="Сортирай по:").pack(side="left", padx=5)
        self.sort_option = ctk.CTkOptionMenu(self.sort_frame, values=["Име", "Дата (Нови)", "Размер", "Тип"], command=self.sort_results)
        self.sort_option.set("Име")
        self.sort_option.pack(side="left", padx=5)

        # FIX: брояч на резултатите
        self.result_count_label = ctk.CTkLabel(self.sort_frame, text="", text_color="lightblue")
        self.result_count_label.pack(side="left", padx=20)

        # 4. Резултати
        self.results_list = ctk.CTkTextbox(self, width=900, height=430, font=("Consolas", 12))
        self.results_list.pack(pady=10, padx=20)

        self.results_list.bind("<Double-Button-1>", self.on_double_click)
        self.results_list.bind("<Button-3>", self.show_context_menu)

        self.context_menu = Menu(self, tearoff=0)
        self.context_menu.add_command(label="Отвори файла", command=self.open_selected_file)
        self.context_menu.add_command(label="Отвори в Explorer (маркирай)", command=self.open_file_folder)

    # --- КОНФИГУРАЦИЯ ---
    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    idx_path = data.get("index_path", "")
                    src_path = data.get("source_path", "")
                    if idx_path and os.path.exists(idx_path):
                        self.index_path = idx_path
                        self.check_index_validity()
                    # FIX: зареждаме и source_path от конфига
                    if src_path and os.path.exists(src_path):
                        self.source_path = src_path
                        self.source_label.configure(text=f"Сканиране: ...{src_path[-40:]}", text_color="lightblue")
            except Exception as e:
                logging.error(f"Грешка при зареждане на config: {e}")

    def save_config(self):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump({"index_path": self.index_path, "source_path": self.source_path}, f, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Грешка при запис на config: {e}")

    def check_index_validity(self):
        idx_dir = os.path.join(self.index_path, "search_index_db")
        if os.path.exists(idx_dir) and exists_in(idx_dir):
            short = f"...{self.index_path[-45:]}" if len(self.index_path) > 48 else self.index_path
            self.index_status_label.configure(text=f"Статус: Намерен каталог в {short}", text_color="lightgreen")
            return True
        else:
            self.index_status_label.configure(text="Статус: Избраната папка е празна (нужно е индексиране)", text_color="orange")
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
            self.source_label.configure(text=f"Сканиране: ...{path[-40:]}", text_color="lightblue")
            self.save_config()

    # --- ИЗВЛИЧАНЕ НА ТЕКСТ ---
    def extract_all(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext in ['.txt', '.py', '.js', '.html', '.css', '.cpp', '.sql', '.json', '.bat', '.ini']:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    return f.read()
            elif ext == '.pdf':
                text = ""
                with fitz.open(file_path) as doc:
                    if doc.page_count == 0:
                        return None
                    for page in doc:
                        try:
                            t = page.get_text()
                            if len(t.strip()) < 15:
                                pix = page.get_pixmap()
                                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                                t = pytesseract.image_to_string(img, lang='bul+eng')
                            text += t
                        except Exception as e:
                            logging.error(f"Грешка при страница в {file_path}: {e}")
                            continue
                return text
            elif ext == '.docx':
                return docx2txt.process(file_path)
            elif ext == '.msg':
                msg = extract_msg.Message(file_path)
                return f"{msg.subject} {msg.body}"
            elif ext in ['.pst', '.ost']:
                pff_file = pypff.file()
                pff_file.open(file_path)
                root = pff_file.get_root_folder()
                text = self._extract_pst_folder(root)
                pff_file.close()
                return text
        except Exception as e:
            logging.error(f"Грешка при извличане от {file_path}: {e}")
            return None

    def _extract_pst_folder(self, folder):
        """Рекурсивно извлича текст от всички писма в PST/OST папка."""
        text = ""
        for i in range(folder.get_number_of_sub_messages()):
            try:
                msg = folder.get_sub_message(i)
                subject = msg.subject or ""
                body = msg.plain_text_body or b""
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="ignore")
                text += f"{subject}\n{body}\n"
            except Exception as e:
                logging.error(f"Грешка при четене на съобщение от PST: {e}")
        for i in range(folder.get_number_of_sub_folders()):
            try:
                text += self._extract_pst_folder(folder.get_sub_folder(i))
            except Exception as e:
                logging.error(f"Грешка при четене на папка от PST: {e}")
        return text

    # --- ИНДЕКСИРАНЕ (В ОТДЕЛЕН THREAD) ---
    def run_indexing(self):
        if not self.index_path or not self.source_path:
            messagebox.showerror("Грешка", "Изберете папка за КАТАЛОГ и папка за СКАНИРАНЕ!")
            return
        if self._indexing_active:
            messagebox.showinfo("Инфо", "Индексирането вече е в ход...")
            return

        # FIX: стартираме в отделен thread, за да не замръзва UI
        self.btn_index.configure(state="disabled", text="Индексиране...")
        self._indexing_active = True
        thread = threading.Thread(target=self._do_indexing, daemon=True)
        thread.start()

    def _do_indexing(self):
        """Реалното индексиране — върви в background thread."""
        try:
            idx_dir = os.path.join(self.index_path, "search_index_db")
            if not os.path.exists(idx_dir):
                os.makedirs(idx_dir)

            # FIX: инкрементален индекс — четем съществуващите mtime стойности
            existing_mtimes = {}
            if exists_in(idx_dir):
                ix_old = open_dir(idx_dir)
                with ix_old.searcher() as s:
                    for fields in s.all_stored_fields():
                        existing_mtimes[fields['path']] = fields.get('mtime', 0)
                ix_new = ix_old.writer()
            else:
                ix_new_index = create_in(idx_dir, schema)
                ix_new = ix_new_index.writer()

            all_files = [
                os.path.join(r, f)
                for r, d, files in os.walk(self.source_path)
                for f in files
                if f.lower().endswith(SUPPORTED_EXTENSIONS)
            ]

            if not all_files:
                self.after(0, lambda: messagebox.showinfo("Инфо", "Няма файлове за обработка."))
                return

            new_count = 0
            skip_count = 0

            for i, path in enumerate(all_files):
                try:
                    stats = os.stat(path)
                    current_mtime = stats.st_mtime

                    # FIX: пропускаме непроменени файлове
                    if path in existing_mtimes and existing_mtimes[path] == current_mtime:
                        skip_count += 1
                    else:
                        content = self.extract_all(path)
                        if content:
                            # update_document заменя стария запис или добавя нов
                            ix_new.update_document(
                                title=os.path.basename(path),
                                path=path,
                                content=content,
                                size=stats.st_size,
                                mtime=current_mtime,
                                ext=os.path.splitext(path)[1].lower()
                            )
                            new_count += 1
                except Exception as e:
                    logging.error(f"Грешка при индексиране на {path}: {e}")
                    continue

                # Обновяваме прогрес бара от главния thread
                progress = (i + 1) / len(all_files)
                label_text = f"Файл {i+1} от {len(all_files)} | Нови/Обновени: {new_count} | Пропуснати: {skip_count}"
                self.after(0, lambda p=progress, t=label_text: self._update_progress(p, t))

            ix_new.commit()
            self.after(0, self._indexing_done, new_count, skip_count, len(all_files))

        except Exception as e:
            logging.error(f"Критична грешка при индексиране: {e}")
            self.after(0, lambda: messagebox.showerror("Грешка", f"Грешка при индексиране:\n{e}"))
        finally:
            self._indexing_active = False
            self.after(0, lambda: self.btn_index.configure(state="normal", text="ОБНОВИ/СЪЗДАЙ КАТАЛОГ"))

    def _update_progress(self, value, label_text):
        self.progress_bar.set(value)
        self.progress_label.configure(text=label_text)

    def _indexing_done(self, new_count, skip_count, total):
        self.progress_bar.set(1.0)
        self.check_index_validity()
        messagebox.showinfo(
            "Готово",
            f"Каталогът е обновен успешно!\n\n"
            f"Общо файлове: {total}\n"
            f"Индексирани/Обновени: {new_count}\n"
            f"Пропуснати (непроменени): {skip_count}"
        )
        self.progress_label.configure(text="")
        self.progress_bar.set(0)

    # --- ТЪРСЕНЕ ---
    def run_search(self):
        idx_dir = os.path.join(self.index_path, "search_index_db")
        if not self.index_path or not os.path.exists(idx_dir) or not exists_in(idx_dir):
            messagebox.showwarning("Грешка", "В тази папка няма създаден каталог!")
            return

        ix = open_dir(idx_dir)
        word = self.search_entry.get().strip().lower()
        if not word:
            return

        search_query = f"*{word}*" if "*" not in word else word
        self.last_results = []

        try:
            with ix.searcher() as searcher:
                query = QueryParser("content", ix.schema).parse(search_query)
                results = searcher.search(query, limit=500)

                for hit in results:
                    self.last_results.append({
                        "title": hit['title'],
                        "path": hit['path'],
                        "size": hit.get('size', 0),
                        "mtime": hit.get('mtime', 0),
                        "ext": hit.get('ext', '')
                    })
        except Exception as e:
            logging.error(f"Грешка при търсене: {e}")
            messagebox.showerror("Грешка при търсене", str(e))
            return

        if not self.last_results:
            self.results_list.delete("0.0", "end")
            self.results_list.insert("end", "Няма намерени резултати.")
            self.result_count_label.configure(text="")
        else:
            self.sort_results(self.sort_option.get())

    def sort_results(self, choice):
        if not self.last_results:
            return

        if choice == "Име":
            self.last_results.sort(key=lambda x: x['title'].lower())
        elif choice == "Дата (Нови)":
            self.last_results.sort(key=lambda x: x['mtime'], reverse=True)
        elif choice == "Размер":
            self.last_results.sort(key=lambda x: x['size'], reverse=True)
        elif choice == "Тип":
            self.last_results.sort(key=lambda x: x['ext'])

        self.display_results()

    def display_results(self):
        self.results_list.delete("0.0", "end")
        # FIX: reset на map-а ред -> path
        self.result_line_map = {}

        # FIX: брояч на резултатите
        count = len(self.last_results)
        self.result_count_label.configure(text=f"Намерени: {count} резултата")

        for res in self.last_results:
            dt = datetime.datetime.fromtimestamp(res['mtime']).strftime('%Y-%m-%d %H:%M')
            size_kb = round(res['size'] / 1024, 1)
            size_str = f"{round(size_kb/1024, 2)} MB" if size_kb > 1024 else f"{size_kb} KB"

            # Запазваме номера на реда за path reда
            current_line = int(self.results_list.index("end-1c").split(".")[0])
            path_line = current_line + 1  # следващият ред ще е пътят

            info = f"📄 {res['title']} | {size_str} | {dt}\n📍 Път: {res['path']}\n" + "─" * 80 + "\n"
            self.results_list.insert("end", info)

            # FIX: записваме кой ред съдържа пътя
            self.result_line_map[path_line] = res['path']

    # FIX: надежден lookup на path по ред, без fuzzy range търсене
    def get_path_under_cursor(self):
        try:
            click_line = int(self.results_list.index("insert").split(".")[0])
            # Проверяваме текущия ред и ±2 около него
            for delta in range(-2, 3):
                line = click_line + delta
                if line in self.result_line_map:
                    return self.result_line_map[line]
                # Алтернативно — четем реда и парсваме директно
                line_content = self.results_list.get(f"{line}.0", f"{line}.end")
                if "📍 Път: " in line_content:
                    return line_content.replace("📍 Път: ", "").strip()
        except Exception as e:
            logging.error(f"Грешка при get_path_under_cursor: {e}")
        return None

    def on_double_click(self, event):
        path = self.get_path_under_cursor()
        if path and os.path.exists(path):
            os.startfile(path)
        elif path:
            messagebox.showwarning("Файлът не е намерен", f"Файлът вече не съществува:\n{path}")

    def show_context_menu(self, event):
        self.results_list.mark_set("insert", self.results_list.index(f"@{event.x},{event.y}"))
        self.results_list.focus_set()
        self.context_menu.post(event.x_root, event.y_root)

    def open_selected_file(self):
        path = self.get_path_under_cursor()
        if path and os.path.exists(path):
            os.startfile(path)
        elif path:
            messagebox.showwarning("Файлът не е намерен", f"Файлът вече не съществува:\n{path}")

    def open_file_folder(self):
        path = self.get_path_under_cursor()
        if path and os.path.exists(path):
            subprocess.run(['explorer', '/select,', os.path.normpath(path)])
        elif path:
            messagebox.showwarning("Файлът не е намерен", f"Файлът вече не съществува:\n{path}")


if __name__ == "__main__":
    app = SearchApp()
    app.mainloop()

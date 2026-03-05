import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import AsyncOpenAI

import os
import re
import ast
import json
import shutil
import logging
import subprocess
from datetime import datetime
from openai import OpenAI
from logging.handlers import TimedRotatingFileHandler
from dotenv import load_dotenv
load_dotenv()

file_handler = TimedRotatingFileHandler(
    filename='app.log',
    when='H',          # 'H' oznacza rotację w godzinach
    interval=1,        # Twórz nowy plik co 1 godzinę
    backupCount=8,     # Zatrzymaj maksymalnie 8 archiwalnych plików (czyli 8 godzin)
    encoding='utf-8'   # Warto dodać, żeby uniknąć problemów z polskimi znakami
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[file_handler] # Dodajemy handlery
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO_URL = os.getenv("GITHUB_REPO_URL")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

TELEGRAM_MODEL_NAME = "gpt-4o-mini"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def run_ai_developer(app_requirements: str, clone_dir: str = "./temp_repo_workspace"):
    """
    Uruchamia AI Agenta, który planuje logikę, generuje kod,
    waliduje go pod kątem błędów składniowych (max 5 prób poprawy),
    a następnie commituje i pushuje.
    """

    # --- Wewnętrzne funkcje pomocnicze ---
    def run_cmd(command, cwd=None):
        logging.debug(f"Wykonuję polecenie: {command}")
        result = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            logging.error(f"Błąd polecenia: {result.stderr.strip()}")
            raise Exception(f"Command failed: {command}")
        return result.stdout.strip()

    def clean_code_block(text):
        pattern = r"^```[a-zA-Z]*\n(.*?)\n```$"
        match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
        if match:
            return match.group(1)
        return text.strip()

    # -------------------------------------

    logging.info("🚀 Uruchamiam funkcję AI Developera (Tryb Samonaprawy)...")
    client = OpenAI(api_key=OPENAI_API_KEY)

    # 1. Klonowanie repozytorium i tworzenie brancha
    if os.path.exists(clone_dir):
        logging.info(f"Czyszczenie poprzedniego katalogu roboczego: {clone_dir}")
        shutil.rmtree(clone_dir)

    auth_repo_url = f"https://{GITHUB_TOKEN}@{GITHUB_REPO_URL}"
    logging.info("Klonowanie repozytorium...")
    run_cmd(f"git clone {auth_repo_url} {clone_dir}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    branch_name = f"solution_{timestamp}"
    run_cmd(f"git checkout -b {branch_name}", cwd=clone_dir)
    logging.info(f"✅ Utworzono i przełączono na branch: {branch_name}")

    # 2. Architektura: Plan implementacji i Zależności
    logging.info("🧠 Architekt AI tworzy plan implementacji oraz listę bibliotek...")
    architect_prompt = f"""
    Jesteś Architektem Oprogramowania. Zaplanuj implementację aplikacji jako pojedynczego skryptu ('main.py').

    WYMOGI:
    1. Określ tablicę "dependencies" zawierającą TYLKO ZEWNĘTRZNE biblioteki Python. NIE wypisuj wbudowanych modułów.
    2. Przygotuj "implementation_plan" określający krok po kroku, jakie funkcje należy stworzyć w main.py.

    Zwróć wynik WYŁĄCZNIE w formacie JSON:
    {{
      "dependencies": ["lista", "zewnętrznych", "bibliotek"],
      "implementation_plan": "Szczegółowy opis..."
    }}

    Założenia aplikacji:
    {app_requirements}
    """

    try:
        response_arch = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": architect_prompt}]
        )

        architecture_data = json.loads(response_arch.choices[0].message.content)
        dependencies = architecture_data.get("dependencies", [])
        implementation_plan = architecture_data.get("implementation_plan", "")
        logging.info(f"Plan architekta gotowy. Zależności: {dependencies}")

    except Exception as e:
        logging.error(f"Błąd podczas tworzenia planu architektury: {e}")
        raise

    # 3. Zapisywanie README i requirements.txt
    logging.info("Zapisywanie plików README.md oraz requirements.txt...")
    with open(os.path.join(clone_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Projekt Automatyczny (AI Generated)\n\n")
        f.write(f"## Pierwotne założenia\n{app_requirements.strip()}\n\n")
        f.write(f"## Plan implementacji\n{implementation_plan}\n")

    with open(os.path.join(clone_dir, "requirements.txt"), "w", encoding="utf-8") as f:
        for dep in dependencies:
            f.write(f"{dep}\n")

    # 4. Programista z pętlą walidacji i samonaprawy (Max 5 prób)
    deps_str = ", ".join(dependencies) if dependencies else "Brak (tylko wbudowane moduły)"
    logging.info("💻 Programista AI pisze kod dla pliku main.py...")

    dev_prompt = f"""
    Jesteś programistą Python. Napisz kompletny kod dla pliku 'main.py'.

    ZAŁOŻENIA BIZNESOWE: {app_requirements}
    PLAN IMPLEMENTACJI: {implementation_plan}
    ZEWNĘTRZNE BIBLIOTEKI: {deps_str}

    WYMAGANIA:
    1. Cały kod musi znajdować się w jednym pliku z blokiem `if __name__ == "__main__":`.
    2. Dobrze okomentuj logikę w języku polskim.
    3. Zwróć TYLKO czysty kod Python, bez tagów markdown i dodatkowego tekstu.
    """

    # Inicjalizacja historii wiadomości dla Programisty
    messages = [{"role": "user", "content": dev_prompt}]
    max_retries = 5
    clean_code = ""
    is_code_valid = False

    for attempt in range(max_retries):
        logging.info(f"🔄 Generowanie kodu (Próba {attempt + 1}/{max_retries})...")
        try:
            response_dev = client.chat.completions.create(
                model="gpt-4o",
                messages=messages
            )

            raw_response = response_dev.choices[0].message.content
            clean_code = clean_code_block(raw_response)

            # Walidacja kodu przez AST (Abstract Syntax Tree)
            ast.parse(clean_code)
            is_code_valid = True
            logging.info("✅ Kod przeszedł pomyślnie walidację składni!")
            break  # Wychodzimy z pętli - kod jest poprawny

        except SyntaxError as e:
            error_msg = f"Błąd składniowy w wygenerowanym kodzie w linii {e.lineno}: {e.msg}\nLinia z błędem: {e.text}"
            logging.warning(f"⚠️ {error_msg}")

            if attempt == max_retries - 1:
                logging.error("❌ Osiągnięto limit prób. Przerywam działanie.")
                break

            # Dodajemy odpowiedź asystenta i nasz komunikat o błędzie do historii, żeby model wiedział, co zepsuł
            messages.append({"role": "assistant", "content": raw_response})
            correction_prompt = f"Zwrócony kod posiada błąd składni Pythona:\n{error_msg}\nProszę, popraw ten błąd i zwróć cały skrypt ponownie. Pamiętaj: zwracaj TYLKO czysty kod Python."
            messages.append({"role": "user", "content": correction_prompt})

        except Exception as e:
            logging.error(f"Nieoczekiwany błąd podczas generowania kodu: {e}")
            break

    if not is_code_valid:
        logging.error("Agent AI nie był w stanie wygenerować poprawnego składniowo kodu w 5 próbach.")
        raise Exception("Failed to generate valid Python code.")

    # Zapisanie zweryfikowanego kodu
    with open(os.path.join(clone_dir, "main.py"), "w", encoding="utf-8") as f:
        f.write(clean_code)
    logging.info("✅ Zapisano plik main.py")

    # 5. Commit i Push
    logging.info("📦 Przygotowywanie zmian w Git...")
    run_cmd("git add .", cwd=clone_dir)

    status = run_cmd("git status --porcelain", cwd=clone_dir)
    if status:
        logging.info("Znaleziono zmiany. Wykonywanie commita...")
        run_cmd(f'git commit -m "Auto-wygenerowane rozwiązanie: główny skrypt + samonaprawa"', cwd=clone_dir)
        logging.info(f"Wysyłanie zmian na branch: {branch_name}...")
        run_cmd(f"git push origin {branch_name}", cwd=clone_dir)
        logging.info(f"🎉 Sukces! Zmiany wypchnięte na gałąź: {branch_name}")
    else:
        logging.warning("⚠️ Brak zmian do zacommitowania.")

    # 6. Sprzątanie
    logging.info("Czyszczenie po pracy...")
    shutil.rmtree(clone_dir)
    logging.info("✅ Zakończono działanie funkcji run_ai_developer.")


# --- BAZA DANYCH ---
def get_db():
    conn = sqlite3.connect('app_builder_bot.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS projects 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  chat_id TEXT, 
                  name TEXT, 
                  requirements TEXT, 
                  final_prompt TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # Mała migracja, jeśli baza już istnieje z poprzedniej wersji bez kolumny final_prompt
    try:
        c.execute("ALTER TABLE projects ADD COLUMN final_prompt TEXT")
    except sqlite3.OperationalError:
        pass  # Kolumna już istnieje

    c.execute('''CREATE TABLE IF NOT EXISTS chat_sessions 
                 (chat_id TEXT PRIMARY KEY, 
                  state TEXT DEFAULT 'IDLE', 
                  current_project_id INTEGER,
                  last_bot_question TEXT)''')
    conn.commit()
    conn.close()


def get_projects(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name FROM projects WHERE chat_id=? ORDER BY created_at DESC", (chat_id,))
    projects = c.fetchall()
    conn.close()
    return projects


def get_project(project_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id=?", (project_id,))
    project = c.fetchone()
    conn.close()
    return project


# --- FUNKCJE POMOCNICZE DO STANU ---
def get_chat_session(chat_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT state, current_project_id, last_bot_question FROM chat_sessions WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row['state'], row['current_project_id'], row['last_bot_question']
    else:
        return "IDLE", None, ""


def update_chat_session(chat_id, state, current_project_id=None, last_bot_question=""):
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO chat_sessions (chat_id, state, current_project_id, last_bot_question) 
                 VALUES (?, ?, ?, ?)''', (chat_id, state, current_project_id, last_bot_question))
    conn.commit()
    conn.close()


# --- FUNKCJE AI (OPENAI) ---
async def analyze_new_project(description: str):
    prompt = f"""Użytkownik podał wstępny opis nowej aplikacji: "{description}".
    Wymyśl krótką nazwę kodową dla tego projektu (max 3-4 słowa) oraz zadaj JEDNO najważniejsze pytanie doprecyzowujące.
    Zwróć TYLKO JSON: {{"name": "Nazwa Projektu", "next_question": "Twoje pytanie..."}}"""
    try:
        response = await client.chat.completions.create(
            model=TELEGRAM_MODEL_NAME, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": "Jesteś analitykiem IT. Zwracasz tylko JSON."},
                      {"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Błąd AI: {e}")
        return None


async def refine_requirements(current_reqs: str, last_question: str, user_answer: str):
    prompt = f"""Obecne zebrane wymagania aplikacji:
    {current_reqs}

    Zadałeś użytkownikowi pytanie: "{last_question}"
    Odpowiedź użytkownika: "{user_answer}"

    1. Zaktualizuj wymagania łącząc stare z nową wiedzą (stwórz czytelną listę funkcjonalności/założeń jako jeden długi tekst).
    2. Zadaj KOLEJNE, jedno najważniejsze pytanie, które pomoże doprecyzować projekt.
    Zwróć TYLKO JSON: {{"updated_requirements": "Nowy tekst wymagań...", "next_question": "Kolejne pytanie..."}}"""
    try:
        response = await client.chat.completions.create(
            model=TELEGRAM_MODEL_NAME, response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Jesteś doświadczonym inżynierem oprogramowania. Zwracasz tylko JSON."},
                {"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Błąd AI: {e}")
        return None


async def generate_final_prompt_for_llm(requirements: str):
    prompt = f"""Na podstawie poniższych założeń stwórz PROFESJONALNY, potężny i szczegółowy PROMPT dla modelu językowego.
    Założenia:
    {requirements}

    Prompt powinien maksymalnie skupiać się na logice, kwestie techniczne zdecydowanie odpuść. Jeżeli się da to podziel apkę na rozłączne części.

    Zwróć TYLKO tekst promptu (bez jsona, po prostu surowy tekst gotowy do skopiowania)."""
    try:
        response = await client.chat.completions.create(
            model=TELEGRAM_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Błąd AI: {e}")
        return "Błąd generowania promptu."


# --- GŁÓWNA LOGIKA I OBSŁUGA ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         text: str = "Wybierz projekt lub utwórz nowy:"):
    chat_id = str(update.effective_chat.id)
    projects = get_projects(chat_id)

    keyboard = []
    for proj in projects:
        keyboard.append([InlineKeyboardButton(f"📁 {proj['name']}", callback_data=f"proj_{proj['id']}")])
    keyboard.append([InlineKeyboardButton("✨ Utwórz nowy projekt", callback_data="new_project")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)

    update_chat_session(chat_id, "IDLE")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    user_text = update.message.text

    if user_text.strip().lower() == "exit":
        await update.message.reply_text("🛑 Przerwano obecną akcję.")
        await show_main_menu(update, context)
        return

    state, current_project_id, last_bot_question = get_chat_session(chat_id)

    if state == "IDLE":
        await show_main_menu(update, context, "Cześć! O czym dzisiaj myślimy?")

    elif state == "AWAITING_NEW_PROJECT_DESC":
        await update.message.reply_text("⏳ Analizuję Twój pomysł...")
        ai_response = await analyze_new_project(user_text)

        if ai_response:
            proj_name = ai_response.get("name", "Nowy Projekt")
            next_q = ai_response.get("next_question", "Co robimy dalej?")

            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT INTO projects (chat_id, name, requirements) VALUES (?, ?, ?)",
                      (chat_id, proj_name, f"Pierwotny pomysł: {user_text}"))
            project_id = c.lastrowid
            conn.commit()
            conn.close()

            update_chat_session(chat_id, "REFINING", project_id, next_q)
            await update.message.reply_text(
                f"🚀 Utworzono projekt: <b>{proj_name}</b>\n\nZanim przejdziemy dalej: {next_q}\n\n<i>(Odpisz na to pytanie lub wpisz 'exit', by wyjść)</i>",
                parse_mode='HTML')
        else:
            await update.message.reply_text("Wystąpił błąd AI. Spróbuj ponownie za chwilę.")
            update_chat_session(chat_id, "IDLE")

    elif state == "REFINING":
        if current_project_id is None:
            await show_main_menu(update, context, "Zgubiłem kontekst projektu. Wybierz go ponownie:")
            return

        project = get_project(current_project_id)
        current_reqs = project['requirements']

        await update.message.reply_text("⏳ Przetwarzam i aktualizuję założenia...")

        ai_response = await refine_requirements(current_reqs, last_bot_question, user_text)

        if ai_response:
            updated_reqs = ai_response.get("updated_requirements", current_reqs)

            if isinstance(updated_reqs, list):
                updated_reqs = "\n- ".join(str(item) for item in updated_reqs)
            elif not isinstance(updated_reqs, str):
                updated_reqs = str(updated_reqs)

            next_q = ai_response.get("next_question", "Masz jeszcze jakieś uwagi?")

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE projects SET requirements=? WHERE id=?", (updated_reqs, current_project_id))
            conn.commit()
            conn.close()

            update_chat_session(chat_id, "REFINING", current_project_id, next_q)

            keyboard = [
                [InlineKeyboardButton("🛠 Wygeneruj ostateczny PROMPT", callback_data=f"gen_{current_project_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"✅ <b>Zaktualizowałem wymagania.</b>\n\nKolejne pytanie:\n{next_q}\n\n<i>(Odpisz, by kontynuować, kliknij przycisk, by zakończyć zbieranie wymagań, lub wpisz 'exit')</i>",
                parse_mode='HTML', reply_markup=reply_markup)
        else:
            await update.message.reply_text("Wystąpił błąd AI. Spróbujmy jeszcze raz.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = str(query.message.chat_id)
    data = query.data

    if data == "new_project":
        update_chat_session(chat_id, "AWAITING_NEW_PROJECT_DESC")
        await query.message.reply_text(
            "Wspaniale! Opisz mi w kilku zdaniach, co to za aplikacja, do czego służy i jaki ma być jej główny cel. (Napisz 'exit' by anulować).")

    elif data.startswith("proj_"):
        project_id = int(data.split("_")[1])
        project = get_project(project_id)
        if project:
            keyboard = [
                [InlineKeyboardButton("💬 Kontynuuj precyzowanie", callback_data=f"refine_{project_id}")],
                [InlineKeyboardButton("🛠 Wygeneruj PROMPT", callback_data=f"gen_{project_id}")]
            ]

            # Jeśli mamy już gotowy prompt z poprzednich sesji, pokażmy opcję natychmiastowego odpalenia deva
            if project['final_prompt']:
                keyboard.append(
                    [InlineKeyboardButton("👨‍💻 Przekaż od razu do Developera", callback_data=f"rundev_{project_id}")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            req_preview = project['requirements'][:300] + "..." if len(project['requirements']) > 300 else project[
                'requirements']
            msg = f"📁 <b>Projekt:</b> {project['name']}\n\n<b>Obecne wymagania:</b>\n{req_preview}\n\nCo chcesz zrobić?"
            await query.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await query.message.reply_text("Nie znaleziono projektu.")

    elif data.startswith("refine_"):
        project_id = int(data.split("_")[1])
        next_q = "Jakie jeszcze funkcje lub szczegóły chcemy dodać do tego projektu?"
        update_chat_session(chat_id, "REFINING", project_id, next_q)
        await query.message.reply_text(f"Jasne! {next_q}\n\n<i>(Odpisz, wpisz 'exit' by wyjść)</i>", parse_mode='HTML')

    elif data.startswith("gen_"):
        project_id = int(data.split("_")[1])
        project = get_project(project_id)

        await query.message.reply_text("⚙️ Generuję ostateczny prompt... To może zająć kilkanaście sekund.")

        final_prompt = await generate_final_prompt_for_llm(project['requirements'])

        # Zapisujemy wygenerowany prompt w bazie
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE projects SET final_prompt=? WHERE id=?", (final_prompt, project_id))
        conn.commit()
        conn.close()

        update_chat_session(chat_id, "IDLE")

        await query.message.reply_text("Oto Twój gotowy PROMPT:")

        for i in range(0, len(final_prompt), 4000):
            await query.message.reply_text(f"<code>{final_prompt[i:i + 4000]}</code>", parse_mode='HTML')

        # Wyświetlamy przycisk przekazania do AI Developera
        dev_keyboard = [
            [InlineKeyboardButton("👨‍💻 Przekaż założenia do AI Developera", callback_data=f"rundev_{project_id}")]]
        await query.message.reply_text("Co chcesz teraz zrobić z tymi założeniami?",
                                       reply_markup=InlineKeyboardMarkup(dev_keyboard))

    elif data.startswith("rundev_"):
        project_id = int(data.split("_")[1])
        project = get_project(project_id)

        await query.message.reply_text(
            "🚀 Uruchamiam AI Developera... Trwa kodowanie i pushowanie do repozytorium. Daj mi chwilę ⏳")

        # Wybieramy co przekazujemy do funkcji. Jeśli masz wygenerowany finalny prompt to go wrzucamy,
        # w przeciwnym razie czyste 'requirements'
        requirements = project['final_prompt'] if project['final_prompt'] else project['requirements']

        try:
            # UWAGA: Jeśli run_ai_developer jest synchroniczne (nie ma 'async def'), zamień poniższą linię na:
            # await asyncio.to_thread(run_ai_developer, zalozenia_do_przekazania)
            run_ai_developer(requirements)

            await query.message.reply_text("✅ Sukces! AI Developer zakończył pracę i wypushował zmiany.")
        except Exception as e:
            logging.error(f"Błąd AI Developera: {e}")
            await query.message.reply_text("❌ Wystąpił błąd podczas pracy AI Developera. Sprawdź logi serwera.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    await show_main_menu(update, context,
                         "👋 Cześć! Jestem Twoim AI-Analitykiem. Pomogę Ci ułożyć specyfikację aplikacji. Co robimy?")


def main():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    application.run_polling()


if __name__ == '__main__':
    main()
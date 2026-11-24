# Інструкція для ШІ-агента: розгортання та налаштування AI-Robot

Цей документ призначений для ШІ-агента (**Antigravity**, **Claude Code**, **Codex**, **Cursor** тощо).  
Твоє завдання — повністю налаштувати й адаптувати проєкт **AI-Robot** під конкретний комп’ютер, Android-смартфон і робот-пилосос користувача так, щоб йому не доводилося програмувати вручну.

> **Правило безпеки:** НІКОЛИ не додавай до Git, не друкуй у публічних звітах і не передавай назовні приватні файли користувача: `config.json`, `.cache/`, `runtime/frames/`, `runtime/diary.md`, `runtime/home_map.md`, `runtime/inbox.jsonl`, `runtime/panel_history.jsonl` і логи. Переконайся, що вони лишаються під дією `.gitignore`.

---

## 1. Карта проєкту та архітектура

Система працює **без Docker, без Home Assistant, без VDO.Ninja та без локальних HTTPS-сертифікатів**. Зв’язок із телефоном-«головою» відбувається через `adb reverse tcp:8765` + WebSocket (Chrome вважає `http://localhost:8765` довіреним джерелом, тому камера, мікрофон із апаратним ехоподавленням AEC і динамік працюють одразу).

```text
AI-Robot/
├── config.example.json          # Шаблон конфігурації -> скопіювати в config.json
├── requirements.txt             # Python-залежності
├── env_setup.py                 # Перенесення всіх кешів (.cache/) і CUDA DLL у корінь проєкту
├── start.ps1 / stop.ps1         # Запуск (із watchdog) / зупинка сервера та ADB (Windows PowerShell)
├── robot_cli.py                 # Єдиний CLI для ШІ-агента (step, listen, say, think, frame, status, stop, dock)
├── body/
│   ├── discover_xiaomi.py       # Пошук Xiaomi у LAN (UDP 54321) + вхід через QR Mi Home для токена
│   └── vacuum_driver.py         # Драйвер пилососа: LAN miIO (AES-128-CBC) + Cloud MIoT fallback
├── head/
│   ├── adb_setup.py             # Підключення Android по ADB, adb reverse tcp:8765, пошук IP за MAC, Chrome
│   ├── bridge_server.py         # FastAPI + WebSocket сервер (голова, тіло, STT/TTS, панель, автономність)
│   ├── watchdog.ps1             # Автоперезапуск bridge-сервера та відновлення ADB на Windows
│   └── static/
│       ├── head.html            # Екран «голови» на смартфоні-пилососі (очі, камера, мікрофон, СТОП)
│       └── panel.html           # Вебпульт керування з основного телефона користувача (/panel)
├── voice/
│   ├── stt_engine.py            # Silero VAD + faster-whisper + миттєвий рефлекс екстреного «Стоп!»
│   ├── tts_engine.py            # Edge-TTS (онлайн) + Silero TTS v5 (офлайн, fallback 24 кГц)
│   ├── voice_brain.py           # Локальний 0-мс парсер команд (кроки, кути, статус, база, прибирання)
│   └── agy_brain.py             # Опційний вбудований мультимодальний мозок через Antigravity CLI (`agy`)
├── prompts/
│   └── voice_robot_prompt.md    # Системний промпт для будь-якого зовнішнього ШІ-агента
└── runtime/
    ├── diary.example.md         # Шаблон щоденника -> скопіювати в runtime/diary.md
    ├── home_map.example.md      # Шаблон карти оселі -> скопіювати в runtime/home_map.md
    └── frames/                  # Останні кадри .jpg із камери смартфона
```

---

## Крок 0. Що уточнити у власника перед стартом

Перш ніж змінювати конфіг, локалізувати парсер або запускати команди, постав користувачу короткі запитання:

1. **Мова спілкування робота:** якою мовою власник буде розмовляти з роботом і якою мовою робот має відповідати (наприклад, **українська**, **російська** або **англійська**)? *(Залежно від відповіді налаштуй `stt_language`, `edge_voice` / `tts_engine`, `stop_words` і за потреби адаптуй `voice/voice_brain.py` та підписи у `head/static/panel.html`.)*
2. **Робот-пилосос:** марка і точна модель (наприклад, Xiaomi Mi Robot Vacuum-Mop 2 Pro, Xiaomi E5, S10T, Roborock, Dreame тощо).
3. **Телефон-«голова»:** який Android-смартфон буде закріплений на пилососі, чи вже під’єднаний він по USB або Wi-Fi ADB?
4. **Комп’ютер та ОС:** Windows або Linux/macOS? Чи є відеокарта NVIDIA (і скільки ГБ VRAM)?
5. **Ім’я та голос робота:** яке ім’я дати роботу (у коді за замовчуванням використовується `Жужа` або нейтральне ім’я з `config.json`) та який голос кращий — жіночий чи чоловічий, онлайн-нейроголос (`edge`, підтримує українську `uk-UA-PolinaNeural` / `uk-UA-OstapNeural`) чи повністю офлайн (`silero`, `v5_ru`)?
6. **Режим ШІ-мозку:** власник буде керувати роботом:
   - через тебе (зовнішнього ШІ-агента в IDE/CLI через `robot_cli.py` + `prompts/voice_robot_prompt.md`), **та/або**
   - у фоновому голосовому/автономному режимі через `agy` (`voice/agy_brain.py` — працює, якщо на ПК встановлено та авторизовано Antigravity CLI)?

---

## Крок 1. Віртуальне середовище, залежності та конфіг

1. Створи віртуальне середовище в корені проєкту (Python 3.11 або 3.12):
   - **Windows (PowerShell):**
     ```powershell
     python -m venv .venv
     .\.venv\Scripts\python.exe -m pip install --upgrade pip
     .\.venv\Scripts\python.exe -m pip install -r requirements.txt
     ```
   - **Linux / macOS:**
     ```bash
     python3 -m venv .venv
     ./.venv/bin/python -m pip install --upgrade pip
     ./.venv/bin/python -m pip install -r requirements.txt
     ```
2. **Якщо є відеокарта NVIDIA (CUDA 12):**
   ```powershell
   .\.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
   ```
   > Примітка: сам `torch` може лишатися CPU-версією — `faster-whisper` (`ctranslate2`) підхопить CUDA DLL із `nvidia-cublas-cu12` та `nvidia-cudnn-cu12` (на Windows шляхи автоматично додає `env_setup.py`).
3. **Якщо відеокарти NVIDIA немає (лише CPU):**
   Додаткові пакети не потрібні. У `config.json` одразу вкажи:
   ```json
   "stt_device": "cpu",
   "stt_compute_type": "int8",
   "stt_model": "small"
   ```
4. Скопіюй шаблони у робочі файли (не перезаписуй, якщо власник уже має власні):
   - `config.example.json` → `config.json`
   - `runtime/diary.example.md` → `runtime/diary.md`
   - `runtime/home_map.example.md` → `runtime/home_map.md`
5. Переконайся, що при імпорті `env_setup.py` створюється папка `.cache/` і туди перенаправляються `HF_HOME`, `TORCH_HOME`, `PIP_CACHE_DIR`, `XDG_CACHE_HOME`, `TEMP`, `TMP`.

---

## Крок 2. Підключення та адаптація пилососа

### Варіант А: Пилосос Xiaomi (miIO / MIoT)

1. Перевір локальну мережу:
   ```powershell
   .\.venv\Scripts\python.exe body\discover_xiaomi.py --scan-only
   ```
   *(Скрипт надішле широкомовний пакет на UDP `54321`. Якщо пилосос у тій самій Wi-Fi мережі, виведеться його `did` та `ip`.)*
2. Отримай токен і модель через безпечний локальний QR-вхід у Mi Home:
   ```powershell
   .\.venv\Scripts\python.exe body\discover_xiaomi.py --qr --open
   ```
   Попроси користувача відсканувати відкритий QR-код у додатку **Mi Home / Xiaomi Home**. Скрипт автоматично збереже `model`, `did`, `ip`, `token`, `server`, `mac` у `config.json`, а хмарну сесію — у `.cache/xiaomi_cloud_session.json`.
   - Якщо акаунт Xiaomi в іншому регіоні (`de`, `cn`, `sg`, `us`, `ru`, `i2`), передай `--all-servers` або `--server <код>`.
3. Перевір, чи є отримана `model` у словнику `MODELS` файлу `body/vacuum_driver.py`:
   - Вже підтримуються з коробки: `ijai.vacuum.v3` (Mop 2 Pro), `szkj.vacuum.fc01eu` (S10T), `xiaomi.vacuum.c108` (E5).
   - **Якщо модель інша:**
     1. Знайди її MIoT-специфікацію на `https://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:vacuum:...` (або пошуком на `miot-spec.org` за рядком `model`).
     2. Визнач `siid` і `piid` / `aiid` для:
        - читання стану, помилки та заряду АКБ (`read`);
        - ручного керування / джойстика (`direction`: `siid, piid`, а також числові коди `keys` для `f/l/r/b`, `stop` і за наявності `exit`);
        - повернення на базу (`dock_action` або `dock_alt`), зупинки (`stop_action` / `cancel_return_action`) та пошуку звуком (`find_prop` або `find_action`).
     3. Перевір знайдені `siid`/`piid`/`aiid` низькорівневими командами перед додаванням у `MODELS`:
        ```powershell
        .\.venv\Scripts\python.exe body\vacuum_driver.py get <siid> <piid>
        .\.venv\Scripts\python.exe body\vacuum_driver.py set <siid> <piid> <json_value>
        .\.venv\Scripts\python.exe body\vacuum_driver.py action <siid> <aiid> "[]"
        ```
     4. Додай запис нової моделі до `MODELS` у `body/vacuum_driver.py`.
   - Якщо локальний UDP `54321` недоступний (ізольована мережа або суто хмарна модель), перемкни в `config.json`: `"transport": "cloud"`.

### Варіант Б: Пилосос іншого бренду (Roborock, Dreame, Viomi тощо)

Уся інша система (`bridge_server.py`, `robot_cli.py`, голос, слух, вебпульт) ізольована від протоколу пилососа через клас `VacuumDriver`. Перепиши методи `VacuumDriver` у `body/vacuum_driver.py` (наприклад, через `python-miio` або `python-roborock`), зберігши сигнатури:
- `status() -> dict` (`status_text`, `battery`, `fault`, `fault_text`)
- `move(tokens: list[str], gap: float | None = None) -> dict` (кроки виду `f1.5`, `b0.8`, `l1.3`, `r1.3`, `w0.5`)
- `emergency_stop() -> dict`
- `exit_manual() -> dict`
- `dock() -> dict`
- `find() -> dict`
- `cancel_return() -> dict`
- *(опційно для голосового автопілота)* `start_clean(mode)`, `stop_clean()`, `edge_clean()`, `room_clean(room_ids)`

### Тест і калібрування шасі

1. Перевір офлайн-тест і статус:
   ```powershell
   .\.venv\Scripts\python.exe body\vacuum_driver.py self-test
   .\.venv\Scripts\python.exe body\vacuum_driver.py status
   ```
2. Попроси власника поставити пилосос на вільну ділянку підлоги й зроби короткий тестовий крок:
   ```powershell
   .\.venv\Scripts\python.exe body\vacuum_driver.py f0.5
   ```
3. Перевір реальну швидкість і поворот на покритті власника (виконай `l1.3` та `f1.0`, запитай реальний кут у градусах і відстань у метрах).
4. Онови константи `DEG_PER_SEC` і `M_PER_SEC` у `voice/voice_brain.py` та орієнтири в `prompts/voice_robot_prompt.md`.

---

## Крок 3. Підключення Android-телефона («голови»)

1. На Android-смартфоні:
   - Увімкни **Параметри розробника** → **Налагодження по USB** (та за потреби **Налагодження по Wi-Fi** / `adb tcpip 5555`).
   - Переконайся, що встановлено **Google Chrome**.
2. На ПК:
   - Встанови Android SDK **Platform-Tools** (`adb`): додай `adb` у системний `PATH` **або** розпакуй папку `platform-tools/` прямо в корінь проєкту (`head/adb_setup.py` і `stop.ps1` шукають її там автоматично).
   - Перевір підключення:
     ```powershell
     adb devices
     ```
   - Якщо телефон підключено по Wi-Fi: запиши у `config.json` (`"head"`):
     - `"adb_target": "<IP_ТЕЛЕФОНА>:5555"`
     - `"adb_mac": "<MAC_ТЕЛЕФОНА>"` *(необов’язково, але дуже корисно: якщо роутер видасть телефону новий DHCP IP, `head/adb_setup.py` автоматично знайде новий IP через `arp -a` за MAC і оновить `adb_target` у `config.json`)*.
   - Якщо телефон підключено по USB — `adb_target` можна лишити порожнім `""`.
3. **Кріплення на корпусі (ВАЖЛИВО!):**
   - Попроси власника закріпити смартфон на пилососі **основною (задньою) камерою вперед**, **не перекриваючи башточку LiDAR-далекоміра** та **не блокуючи передній бампер і датчики падіння**.
   - Під час першого відкриття `http://localhost:8765` на телефоні попроси користувача один раз дозволити Chrome доступ до **Камери** та **Мікрофона** (натиснути кнопку запуску на сторінці `head.html`).

---

## Крок 4. Мова, слух, голос та ім’я робота

1. **Налаштування мови (`config.json` → `"voice"`):**
   - **Українська (рекомендовано для україномовних користувачів):**
     - `"stt_language": "uk"`
     - `"stt_initial_prompt": "<Ім'я робота>, повернись ліворуч, повернись праворуч, розвернись на 90 градусів, проїдь уперед, на базу, док-станція, на зарядку, озирнись, стоп, почни прибирання, зупини прибирання."`
     - `"tts_engine": "edge"`, `"edge_voice": "uk-UA-PolinaNeural"` (жіночий) або `"uk-UA-OstapNeural"` (чоловічий). *(Примітка: офлайн-пакет `silero` з `v5_ru` підтримує лише російську модель; якщо користувач обрав українську мову, використовуй `edge` або підготуй/підключи український локальний TTS за бажанням.)*
     - `"stop_words": ["стоп", "стій", "замри", "зупинись", "гальмуй"]`
     - Якщо власник хоче 0-мс локальний парсер українською мовою — адаптуй словникові тригери й відповіді в `voice/voice_brain.py` та рядки інтерфейсу в `head/static/panel.html` / `head/bridge_server.py` на українську.
   - **Російська:**
     - `"stt_language": "ru"`
     - `"tts_engine": "silero"` (офлайн `v5_ru`, голоси: жіночі `kseniya`, `xenia`, `baya`; чоловічі `eugene`, `aidar`) або `"edge"` (`ru-RU-SvetlanaNeural` / `ru-RU-DmitryNeural`).
   - **Англійська:**
     - `"stt_language": "en"`, `"tts_engine": "edge"`, `"edge_voice": "en-US-JennyNeural"`, адаптовані `stop_words` (`["stop", "halt", "freeze", "wait"]`) і тригери в `voice/voice_brain.py`.
2. **Поріг чутливості слуху (`vad_threshold`):**
   - За замовчуванням `0.38`. Якщо тихе мовлення з висоти зросту людини чутно погано — лиши `0.35–0.38`. Якщо мікрофон реагує на шум моторів пилососа — підніми до `0.45–0.55`.
3. **Ім’я робота та режим відгуку за іменем (`wake_word_only`):**
   - Задай `vacuum.name` у `config.json`.
   - Якщо власник обрав інше ім’я, онови регулярний вираз у `has_wake_name()` та фрази пробудження в `voice/voice_brain.py`, а також персону у `voice/agy_brain.py` й `head/static/panel.html`.
   - У вебпульті `/panel` є кнопка **«По імені»** (`wake_word_only`), щоб робот ігнорував сторонні розмови в кімнаті й реагував лише коли до нього звертаються за іменем (екстрене «Стоп!» працює завжди).

---

## Крок 5. Підключення 3-рівневого ШІ-мозку

1. **Рівень 1 — Локальний 0-мс автопілот (`voice/voice_brain.py`):**
   - Вмикається через `"auto_voice": true` і не витрачає токени LLM.
   - Миттєво виконує рух, повороти, стоп, перевірку заряду, фото, повернення на базу, звуковий пошук, прибирання, режим сну/пробудження та перемикання автономності.
2. **Рівень 2 — Зовнішній ШІ-агент (універсально для будь-якого агента):**
   - Складні діалоги, аналіз зображень (`runtime/frames/*.jpg`), багатошагові дослідження кімнат та ведення `runtime/diary.md` / `runtime/home_map.md` виконуєш **ти сам** (або будь-який зовнішній агент) через команди `robot_cli.py`:
     - `python robot_cli.py health`
     - `python robot_cli.py frame` (або `frame hd`)
     - `python robot_cli.py step f1.2 l0.4 --say "Їду подивитися до вікна" --thought "Перевірю, що там відбувається"`
     - `python robot_cli.py listen 15`
     - `python robot_cli.py say "..."` / `think "..."`
   - Системна інструкція для такого режиму вже лежить у `prompts/voice_robot_prompt.md`.
   - Якщо ти сам ведеш сесію через `robot_cli.py listen`, переконайся, що `"agy_enabled": false` у `config.json`, щоб два ШІ-мозки не перебивали одне одного.
3. **Рівень 3 — Вбудований фоновий мозок (`voice/agy_brain.py`, опційно):**
   - Якщо на ПК встановлено та авторизовано **Antigravity CLI (`agy`)**, увімкни `"agy_enabled": true`. Сервер сам передаватиме складні питання й імпульси автономного режиму **«Свобода / Вільне життя»** у `agy` у фоновому режимі (з автозахистом АКБ: при заряді ≤ 20% автономність зупиняється автоматично).
   - Якщо `agy` не використовується, тримай `"agy_enabled": false`.

---

## Крок 6. Запуск, вебпульт `/panel` та фінальна перевірка

### Запуск

- **Windows:**
  ```powershell
  .\start.ps1
  ```
  *(Запускає `head/watchdog.ps1` із автоперезапуском сервера, налаштовує ADB і перевіряє статус пилососа. Зупинка: `.\stop.ps1`.)*
- **Linux / macOS:**
  1. Запусти сервер із кореня проєкту:
     ```bash
     ./.venv/bin/python -m head.bridge_server
     ```
  2. Налаштуй ADB-тунель і відкрий Chrome на телефоні:
     ```bash
     ./.venv/bin/python head/adb_setup.py
     ```

### Вебпульт із основного смартфона (`/panel`)

- Відкрий на основному телефоні власника (у тій самій Wi-Fi мережі) адресу:
  `http://<IP_ПК_У_ЛОКАЛЬНІЙ_МЕРЕЖІ>:8765/panel`
- На Windows при першому запуску дозволь вхідні з’єднання на TCP-порт `8765` у брандмауері Windows (якщо телефон не бачить `/panel`).
- У пульті доступні: LIVE-камера, «думки» робота, чат, вибір моделі та паузи автономного режиму, кнопки **СТОП**, **Свобода**, **Мікрофон**, **Голос**, **Думки**, **По імені**.

### Чеклист фінальної перевірки

1. `.\.venv\Scripts\python.exe robot_cli.py health` → `"ok": true`, `"head_connected": true`, `"stt_ready": true`, `"vacuum_configured": true`.
2. `.\.venv\Scripts\python.exe robot_cli.py status` → реальний заряд і статус пилососа.
3. `.\.venv\Scripts\python.exe robot_cli.py frame` → з’являється свіжий `.jpg` у `runtime/frames/`.
4. `.\.venv\Scripts\python.exe robot_cli.py say "Привіт! Я бачу і чую тебе."` → чистий звук із динаміка телефона без самоспрацювання мікрофона.
5. Голосовий тест: вимовити вголос перед роботом стоп-слово (*«Стоп!»*) або команду повороту → переконатися, що мовлення фіксується, а «Стоп!» миттєво перериває рух/мовлення.

---

## Крок 7. Типові проблеми та швидкі рішення

1. **У Chrome на телефоні-голові відкрито кілька вкладок `localhost:8765`:**  
   Дубльовані вкладки перехоплюють WebSocket `/ws/head` одна в одної, через що зникає звук або камера. Закрий зайві вкладки, залиш рівно одну.
2. **Робот погано чує мовлення з відстані 1.5–2 м:**  
   У `head/static/head.html` уже вбудовано підсилювач WebAudio (`GainNode 2.5x`), а в `stt_engine.py` знижено поріг `rms < 0.0008`. Перевір, чи не перекрито отвір мікрофона кріпленням і чи достатня гучність/права Chrome.
3. **Основний телефон не відкриває `/panel`:**  
   Переконайся, що обидва пристрої в одній підмережі Wi-Fi (не в гостьовій мережі з ізоляцією клієнтів), `bridge_host` у `config.json` дорівнює `"0.0.0.0"`, а порт `8765/TCP` відкрито у фаєрволі.
4. **Змінився Wi-Fi IP телефону-голови після перезавантаження роутера:**  
   Додай MAC-адресу телефона в `head.adb_mac` у `config.json` (формат `aa-bb-cc-dd-ee-ff`) — `head/adb_setup.py` сам знайде новий IP через `arp -a`.
5. **Помилка шляхів із кирилицею або падіння завантаження моделей Whisper/Silero:**  
   `env_setup.py` автоматично використовує `to_short_path(...)` для Windows 8.3; переконайся, що на диску в папці проєкту є 3–4 ГБ вільного місця для `.cache/`.
6. **Нестача RAM під час офлайн-синтезу Silero 48 кГц:**  
   `tts_engine.py` автоматично перемикається на `24000` Гц (пропускає важкий PQMF upsampler); за бажання одразу встанови `"silero_sample_rate": 24000` у `config.json`.

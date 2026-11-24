# LifeOS iOS Shortcuts & Automations Setup Guide (RU / EN)

Инструкция по настройке двусторонней синхронизации iPhone (**Напоминания**, **Заметки**, **Здоровье**, **Siri / Action Button**) с сервером LifeOS без платного Apple Developer аккаунта.

---

## 1. Предварительные требования / Prerequisites

1. **Сервер LifeOS доступен по HTTPS** (через reverse proxy / Cloudflare Tunnel / Tailscale Funnel).
2. **Bearer-токен** из `.env` (`API_BEARER_TOKEN`).
3. На iPhone создайте:
   - Список в **Напоминаниях (Reminders)**: `LifeOS`
   - Папку в **Заметках (Notes)**: `LifeOS`
4. В **Настройках → Конфиденциальность и безопасность → Здоровье → Команды** разрешите чтение:
   - Сон (`Sleep Analysis`)
   - Активная энергия (`Active Energy`)
   - Вариабельность пульса (`Heart Rate Variability`)
   - Пульс в покое (`Resting Heart Rate`)

---

## 2. Команда 1: `LifeOS Sync` (`lifeos_sync_blueprint.json`)

Откройте приложение **Команды (Shortcuts)** → `+` → назовите команду `LifeOS Sync` и соберите шаги по `lifeos_sync_blueprint.json`:

1. **Защита от ночного/сонного режима (Focus Guard):**
   - Получите текущий режим фокусирования (`Get Current Focus`).
   - Прочитайте файл `iCloud Drive/Shortcuts/lifeos_last_sync.json` (если существует).
   - Вычислите разницу `now - last_sync_epoch` в секундах.
   - Если Фокус равен `Sleep`, `Сон`, `Rest` или `Отдых` **И** прошло меньше `12600` секунд (3.5 часа) → **Остановить команду (Stop This Shortcut)**.
2. **Сбор данных:**
   - **Reminders:** Найти напоминания в списке `LifeOS` (изменённые за последние 48 часов). Для каждого собрать `apple_reminder_id`, `title`, `notes`, `is_completed`, `completed_at`, `due_at`.
   - **Notes:** Найти заметки в папке `LifeOS` (изменённые за 48 часов) и собрать `note_id`, `title`, `body`, `modified_at`.
   - **HealthKit (24 часа):** суммировать часы сна и активные калории, взять среднее по HRV и пульсу в покое.
3. **Запрос к серверу (`Get Contents of URL`):**
   - URL: `https://<your-lifeos-domain>/api/v1/shortcuts/sync`
   - Method: `POST`
   - Headers:
     - `Authorization`: `Bearer <API_BEARER_TOKEN>`
     - `Content-Type`: `application/json`
   - Body: JSON со структурой `ShortcutsSyncRequest` (`synced_at`, `focus_mode`, `reminders`, `notes`, `health`).
4. **Идемпотентное применение `reminders_diff`:**
   - Повторяйте по каждому элементу `diff` из ответа сервера (`reminders_diff`):
   - Ищите напоминание в списке `LifeOS`, где **Заметки содержат** `diff.lifeos_tag` (формат `[lifeos:<task_id>]`).
   - Если `diff.action == "upsert"`:
     - Если найдено → обновите `Title = diff.title`, `Notes = diff.notes`, и `Due Date = diff.due_at` только когда `diff.has_exact_time == true` (иначе очистите время напоминания).
     - Если не найдено → создайте новое напоминание в списке `LifeOS` с теми же полями.
   - В конце сохраните текущую метку времени `last_sync_epoch` в `iCloud Drive/Shortcuts/lifeos_last_sync.json`.

> **Важно:** Сервер сам добавляет Definition of Done и тег `[lifeos:<task_id>]` в `notes`, а также никогда не отправляет в Напоминания события календаря (`event`), кандидатов (`candidate`) и долгосрочные идеи (`someday`).

---

## 3. Команда 2: `LifeOS Capture` (`lifeos_siri_capture_blueprint.json`)

Для мгновенного ввода голосом через Siri или кнопку Action Button:

1. Создайте команду `LifeOS Capture` (или `Запиши в LifeOS`).
2. Добавьте действие **Диктовать текст (Dictate Text)**.
3. Проверьте, что текст не пустой.
4. Выполните `POST https://<your-lifeos-domain>/api/v1/shortcuts/siri` с заголовком `Authorization: Bearer <API_BEARER_TOKEN>` и телом `{"text": "<Dictated Text>", "captured_at": "<Current Date ISO8601>"}`.
5. Выведите уведомление или произнесите подтверждение: `Добавлено в LifeOS: <title>`.
6. (Опционально) В **Настройках → Кнопка действия (Action Button)** выберите **Быстрая команда → LifeOS Capture**.

---

## 4. Автоматизации на iPhone / iPhone Personal Automations

В приложении **Команды → Автоматизация (Automation) → `+`**:

### A. Периодическая фоновая синхронизация (30–60 мин днём, 3–4 ч во сне)
- **Создайте автоматизации «Время суток» (Time of Day)** с повтором **Ежедневно** на ключевые слоты (например, каждые 30–60 минут с `08:00` до `23:00` и 1–2 ночных слота `02:30`, `06:00` — встроенный Focus Guard пропустит лишние запуски в режиме Сон/Отдых до истечения 3.5 часов).
- Действие: **Запустить команду → `LifeOS Sync`**.
- Обязательно выберите **Запуск немедленно (Run Immediately)** и отключите **Уведомлять при запуске (Notify When Run)**.

### B. Мгновенная синхронизация после закрытия Напоминаний
- Триггер: **Приложение (App)** → выберите **Напоминания (Reminders)** → **Закрыто (Is Closed)**.
- Режим: **Запуск немедленно (Run Immediately)**.
- Действие: **Запустить команду → `LifeOS Sync`**.
- Результат: как только вы отметили задачу выполненной в Apple Reminders и закрыли приложение, статус сразу улетает в LifeOS (и обновляет виджет).

---

## English Quick Summary

1. Create a `LifeOS` list in Apple Reminders and a `LifeOS` folder in Apple Notes.
2. Build `LifeOS Sync` from [lifeos_sync_blueprint.json](lifeos_sync_blueprint.json) with Focus Guard (`Sleep` / `Сон` / `Rest` / `Отдых` throttled to `12600s` = 3.5 hours) and idempotent `[lifeos:<id>]` matching.
3. Build `LifeOS Capture` from [lifeos_siri_capture_blueprint.json](lifeos_siri_capture_blueprint.json) and bind it to Siri or the iPhone Action Button.
4. Add Personal Automations with **Run Immediately**: Time-of-Day intervals (every 30–60 min active, 3–4h during Sleep Focus via the built-in throttle) and **When Reminders Is Closed → Run `LifeOS Sync`**.

# workflow-writing-paper

Конвейер написания научных статей с пулом языковых моделей и веб-интерфейсом.
Автор вводит научную новизну или только тему; агенты собирают литературу, разворачивают новизну в
исследовательские вопросы, план экспериментов и черновик статьи; каждый артефакт проверяют модели других
семейств и детерминированные проверки, а принимает человек. Числа в тексте берутся только из реестра
результатов, ссылки — только из библиотеки проекта.

Состояние: v0.5 выпущена (полный путь от темы до `paper.docx` проверен вживую на локальных моделях),
v0.6 в работе. Техническое задание — [docs/PLAN.md](docs/PLAN.md), прогоны моделей —
[docs/BENCH-LOG.md](docs/BENCH-LOG.md).

## Что получится

На выходе в папке проекта: `paper.md` и `paper.docx` (список литературы оформляет pandoc по `refs.bib`),
все промежуточные артефакты с версиями (`artifacts/*.md`, история в `artifacts/history/`), отчёты судей
(`artifacts/reviews/`), реестр результатов `results.json` с происхождением каждого числа и журнал всех
вызовов моделей (`calls/`). Каждое одобрение коммитится в git-репозиторий проекта.

## Что нужно

| Что | Зачем | Обязательно |
|---|---|---|
| Linux или macOS (Windows — через WSL2) | | да |
| Python 3.11+ и [uv](https://docs.astral.sh/uv/) | установка и запуск | да |
| git | история артефактов проекта | да |
| [Ollama](https://ollama.com) и видеокарта 12–16 ГБ | локальные модели | да, если нет облачных |
| pandoc | `paper.docx` / `paper.pdf` | для сборки |
| xelatex или lualatex и шрифт DejaVu Serif (`texlive-xetex`, `fonts-dejavu`) | `paper.pdf` с кириллицей | для PDF |
| poppler-utils (`pdftotext`) | полный текст PDF в библиотеке | для PDF |
| `codex`, `claude`, `opencode` CLI или ключи API | облачные модели и судьи | нет |

Без видеокарты всё работает и на процессоре, но стадия занимает десятки минут вместо минут.
На 8 ГБ видеопамяти берите `gemma4:e4b` вместо `gemma4:12b` (см. «Выбор моделей»).

## Установка с нуля

Команды для Ubuntu/Debian; на macOS — `brew install pandoc poppler git` и установщик Ollama с сайта.

```bash
# 1. системные пакеты
sudo apt-get update && sudo apt-get install -y git pandoc poppler-utils curl

# 2. uv (менеджер Python-окружений)
curl -LsSf https://astral.sh/uv/install.sh | sh     # затем откройте новый терминал

# 3. Ollama и модели: пишущая, лёгкая и две модели-судьи других семейств
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:12b
ollama pull gpt-oss:20b
ollama pull phi4-reasoning:14b
ollama pull gemma4:e4b          # необязательно: для слабых видеокарт

# 4. программа
git clone https://github.com/Fgeeha/workflow-writing-paper.git
cd workflow-writing-paper
uv sync                          # окружение и зависимости
uv run pytest -q                 # самопроверка без моделей: все тесты на провайдере fake

# 5. рабочая папка и модели
uv run wwp init                  # создаёт workspace/ из defaults/ (провайдеры, промпты, рубрики, журналы)
uv run wwp profile local-only    # все роли и судьи — на локальных моделях Ollama
uv run wwp serve                 # откройте http://127.0.0.1:8765 → «Пул моделей» → «проверить» у каждой модели
```

Всё состояние — провайдеры, ключи, промпты, рубрики, библиотека, проекты, журнал вызовов — лежит в
`workspace/` (в `.gitignore`). Переезд на другую машину — копирование одной папки.

Облачные модели: впишите ключи в `workspace/env` (`OPENROUTER_API_KEY=…`, `GIGACHAT_CREDENTIALS=…`) или
установите и авторизуйте консольные агенты `codex` / `claude`; затем `uv run wwp profile quality` или
назначьте роли на странице «Пул моделей». Профили и пул правятся в `workspace/providers.yaml`.

Без установки uv: `docker compose up --build` — веб-интерфейс на `127.0.0.1:8765` с pandoc и pdftotext,
`workspace/` остаётся на хосте. Консольные провайдеры (`codex-cli`, `claude-cli`, `opencode-cli`) в образ не
входят.

## Пример 1. От темы до статьи (шаблон `explore`)

Вы знаете только тему; литературу программа собирает сама из OpenAlex. Каждая стадия заканчивается
решением автора: одобрить, одобрить с обоснованием (override), вернуть на правку или отклонить. В
веб-интерфейсе это кнопки на странице проекта и артефакта; ниже — те же шаги из командной строки
(`wwp run` выполняет стадию сразу, без сервера). Весь пример с профилем `local-only` на RTX 5070 Ti
занимает около 45 минут; он проверен дословно 2026-11-11 в чистой рабочей папке.

```bash
uv run wwp new leaves --template explore
uv run wwp run leaves collect --input "Распознавание болезней листьев по снимкам смартфона на одноплатном \
компьютере в поле: лёгкие нейросети, перенос с лабораторных снимков на полевые."      # ≈ 3 мин
# корпус: найденные работы с оценкой релевантности и открытыми PDF; лишнее вычеркните в вебе
uv run wwp decide leaves corpus approve

uv run wwp run leaves literature          # обзор только по ключам корпуса, ≈ 5 мин
uv run wwp decide leaves literature approve
uv run wwp run leaves novelty             # новизна из пробелов обзора; «[уточнить: …]» — ваш выбор
uv run wwp decide leaves novelty approve  # правили artifacts/novelty.md вручную — сначала wwp review leaves novelty
uv run wwp run leaves experiment_plan     # эксперименты, метрики, ключи реестра
uv run wwp decide leaves experiment_plan approve
```

Если судьи нашли блокеры, команда их напечатает, а артефакт останется «на проверке». Варианты: исправить
`artifacts/<артефакт>.md` вручную и перепроверить (`wwp review leaves literature`), перезапустить стадию с
указаниями (`--input "что учесть"`) или одобрить с обоснованием
(`wwp decide leaves literature override --reason "почему блокер не важен"`). Пока блокеры есть, `approve`
отказывает — и в CLI, и в вебе (там остаётся только кнопка override). Заголовок статьи берётся из темы
`collect`; задать свой — `wwp new leaves --template explore --title "…"`.

Эксперимент — зарегистрированный вами сценарий; модели ничего не запускают. Пример из репозитория
считает параметры MobileNetV2:

```bash
P=workspace/projects/leaves
mkdir -p $P/bench && cp examples/mobilenetv2-count/mbv2.py $P/bench/
```

В `workspace/projects/leaves/paper-workflow.yaml` замените строку `scenarios: []` на:

```yaml
scenarios:
  - id: mbv2-count
    cwd: ./bench                     # относительно папки проекта
    host: local                      # или ssh:<alias из ~/.ssh/config>
    command: [python3, mbv2.py, out/m.json]
    results: [out/m.json]
    mapping: {"$.params_m": mbv2_params_m, "$.macs_m": mbv2_macs_m}
    units: {mbv2_params_m: M, mbv2_macs_m: M}
```

```bash
uv run wwp run leaves experiments --scenario mbv2-count   # → results.json: 3.505 и 300.8 с хэшем команды и git-ревизией

for s in "Введение" "Постановка задачи" "Метод" "Эксперимент" "Обсуждение" "Заключение"; do
  uv run wwp run leaves draft --section "$s"              # разделы по профилю журнала, 2–5 мин на раздел
done
uv run wwp check leaves draft            # детерминированные проверки: числа, ссылки, лимиты журнала
uv run wwp run leaves review             # финальная проверка всего черновика судьями
uv run wwp decide leaves draft approve   # при блокерах — исправить и wwp review, или override --reason
uv run wwp run leaves assembly           # → paper.md и paper.docx; пока в черновике есть {pending}, отказ со списком строк
uv run wwp export leaves --to pdf        # PDF через xelatex/lualatex; шрифт — pdf.mainfont в профиле журнала
```

Разделы журнала, лимиты аннотации и объёма, минимум источников — в `workspace/journals/generic.yaml`;
образец со всеми полями — `workspace/journals/example.yaml`. Другой журнал: `wwp new … --journal <id>`.

## Пример 2. Своя новизна и своя библиотека (шаблон `experimental`)

```bash
uv run wwp lib import ~/papers/*.bib ~/papers/*.pdf      # общая библиотека workspace/library
uv run wwp lib search квантование edge                    # проверить, что нашлось
uv run wwp new my-paper --template experimental
uv run wwp run my-paper novelty --input "Абзац новизны: что сделано впервые, на чём, с каким результатом."
uv run wwp decide my-paper novelty approve
uv run wwp run my-paper literature        # обзор по вашей библиотеке; недостающее — предложения OpenAlex
# дальше как в примере 1: experiment_plan → experiments → draft → review → assembly
```

Ненужную стадию можно пропустить с причиной на странице проекта (обязательные — нет).
Сравнить две модели на живой задаче: `wwp run my-paper novelty --ab ollama-qwen35-9b`, затем
`wwp decide my-paper novelty a` (или `b`); доля побед попадает в таблицу стенда.

## Выбор моделей

Сравнение на эталонных задачах стенда 2026-11-11 (RTX 5070 Ti 16 ГБ; медиана оценок судей claude и codex
по рубрике роли, 0–10; 2–3 задачи на роль, разница меньше 0,5 — шум). Подробности — [docs/BENCH-LOG.md](docs/BENCH-LOG.md).

| Роль | Лучшая локальная | Оценка | Дальше |
|---|---|---|---|
| `critic` | gemma4:12b | 9,05 | gemma4:12b без think 8,55; gemma4:e4b 8,40; qwen3.5:9b 8,4 (прогон 2026-10-31) |
| `ideator` | gemma4:12b | 6,33 | без think 6,15; e4b 5,78 |
| `literature_scout` | gemma4:12b | 7,51 | e4b 6,66; gemma4:26b 6,39; gpt-oss:20b 5,75; без think 5,55 |
| `section_writer`, `editor` | gemma4:12b **без think** | 7,32 | с think 6,56 и пустые ответы на длинных разделах; qwen3.5:9b 6,33 |
| `experiment_planner` | gemma4:12b без think | 5,20 | все локальные слабы (≤ 5,6): по возможности облачная модель |
| судьи | claude, codex | — | локально: gpt-oss:20b и phi4-reasoning:14b (phi4 даёт много блокеров без строки) |

Профиль `local-only` назначает ровно эту таблицу; `local-light` — gemma4:e4b на все пишущие роли для
видеокарт на 8 ГБ; `quality` — `codex` на идеи, план и текст. Не рекомендуются: gemma4:26b (не помещается
в 16 ГБ и не лучше 12b), YandexGPT-5-Lite-8B (вместо плана задаёт уточняющий вопрос), T-lite-it-2.1
(с рассуждением — пустые ответы, без — слабее и местами по-английски), lfm2.5:8b (отвечает по-английски),
qwen3:14b (ниже gemma4 и вдвое медленнее).

Свои сравнения: `uv run wwp bench --role section_writer --providers id1,id2` или страница «Стенд»;
эталонные задачи — `workspace/eval/tasks/<роль>/*.yaml`, любой одобренный артефакт добавляется в эталоны
кнопкой «в эталоны».

## Если что-то пошло не так

| Симптом | Что сделать |
|---|---|
| `пустой ответ (… токенов на выход из …)` | модель потратила лимит на рассуждение: `think: false` у провайдера в `providers.yaml` |
| `ответ не JSON после 2 повторов` | модель слабая для роли или обрезан ответ — другая модель на роль |
| `нужно 2 судей из семейств, отличных от …` | добавьте судей других семейств (`roles.judge`), например `gpt-oss:20b` и `phi4-reasoning:14b` |
| у судей `claude`/`codex` ошибка запуска, в пуле «не найден в PATH» | CLI не установлен — `wwp profile local-only` или уберите их из `roles.judge` |
| `OpenAlex ничего не вернул` | нет сети или слишком узкая тема; `library.mailto` в `providers.yaml` ускоряет OpenAlex |
| `число … отсутствует в реестре` | число в тексте не из `results.json`: запустите сценарий или внесите значение на странице «Реестр» |
| `ссылка [@ключ] не из refs.bib` | выдуманная ссылка: уберите или импортируйте работу (`wwp lib import`) |
| `стадия X не завершена` | сначала решение по предыдущей стадии (`wwp decide …`) или её пропуск с причиной |
| `pandoc не установлен` | `sudo apt-get install pandoc`; `paper.md` уже собран |

Журнал каждого вызова модели — `workspace/projects/<проект>/calls/*.json` (промпт, ответ, токены, время);
`uv run wwp reindex` восстанавливает `wwp.sqlite` из файлов, `uv run wwp gc --older-than 90d` показывает,
что можно удалить.

## Справочник команд

```bash
uv run wwp init | serve [--port 8765] | profile <имя>
uv run wwp new <slug> --template experimental|explore|survey|theoretical [--journal id] [--language ru|en]
uv run wwp run <slug> <стадия> [--input …] [--section …] [--scenario id] [--force] [--ab provider]
uv run wwp decide <slug> <артефакт> approve|override|return|reject|a|b [--reason …]
uv run wwp review <slug> [артефакт]       # перепроверить судьями без перегенерации
uv run wwp check <slug> [артефакты…]      # только детерминированные проверки
uv run wwp export <slug> --to docx|pdf
uv run wwp lib import|search|reindex …
uv run wwp bench --role <роль> --providers a,b [--judges 2]
uv run wwp reindex | gc --older-than 90d [--apply]
```

## Как это устроено

Режим изучения темы (шаблон `explore`): автор вводит тему → `literature_scout` строит запросы → OpenAlex →
модель оценивает релевантность по аннотациям → снежный ком по спискам литературы → записи и открытые PDF
в собственной библиотеке проекта → автор одним решением одобряет корпус или вычёркивает лишнее → обзор →
новизна из пробелов обзора → дальше как обычно.

Сквозной путь: автор вводит новизну → ideator → critic → ideator → `novelty.md` → два судьи
других семейств + детерминированный `checker` → решение автора (одобрить / override / вернуть /
отклонить) → `literature.md` по библиотеке автора (literature_scout: запросы → FTS5 по `refs.bib`
и PDF → обзор только с ключами `[@ключ]`; недостающие работы ищутся в OpenAlex и ждут подтверждения)
→ `experiment_plan.md` (эксперимент, метрика, базовые линии и ключ реестра на каждое утверждение)
→ `experiments`: зарегистрированные автором сценарии манифеста запускаются локально или по SSH, числа из
JSON/CSV по `mapping` ложатся в `results.json` с seed, хэшем команды и git-ревизией; расхождение с уже
записанным значением останавливает задачу с вопросом автору
→ разделы черновика по профилю журнала с числами только из `results.json` → `review` → `assembly`
(`paper.md`, `paper.docx` через pandoc с `--citeproc` по `refs.bib`). Выдуманная ссылка —
ключ, которого нет в `refs.bib` — блокер в любом артефакте без участия модели.

Провайдеры (`kind`): `ollama`, `ollama-decision` (decision-модели ollama вроде `kev` — только как судьи),
`openai` (любой OpenAI-совместимый сервер), `openrouter`, `gigachat`, `codex-cli`, `claude-cli`,
`opencode-cli`, `generic-cli` (любой консольный агент), `fake`.

Результаты прогонов стенда и решения по выбору моделей — [docs/BENCH-LOG.md](docs/BENCH-LOG.md).
Сравнение с аналогами (Claude Science, K-Dense BYOK, Denario и др.) и решение об их месте — [docs/ANALOGS.md](docs/ANALOGS.md).
Эталонные задачи стенда — `workspace/eval/tasks/<роль>/*.yaml`, рубрики — `workspace/rubrics/`,
промпты ролей — `workspace/prompts/<язык>/`.

Всё состояние — провайдеры, ключи, промпты, рубрики, библиотека, проекты, журнал вызовов —
лежит в `workspace/` (в `.gitignore`). Переезд на другую машину — копирование одной папки.

## Принципы

- Переносимость: клон репозитория и `uv`, всё предметное в данных, не в коде.
- Человек в контуре: гейт на границе стадий и при превышении бюджета.
- Числа в тексте только из реестра результатов проекта; проверка без модели.
- Семейство модели, создавшей артефакт, не участвует в его проверке.
- Минимум зависимостей: FastAPI, Jinja2, HTMX, SQLite (FTS5), PyYAML; PDF — через внешний pdftotext.

## Этапы

| Версия | Содержимое |
|---|---|
| v0.1 | пул провайдеров, стенд сравнения моделей по ролям |
| v0.2 | проекты, стадии, артефакты, проверка семействами, реестр результатов |
| v0.3 | библиотека: PDF, BibTeX, FTS5, OpenAlex; стадии `literature`, `experiment_plan`; проверка ключей `[@key]` |
| v0.4 | запуск зарегистрированных сценариев экспериментов, локально и по SSH; парсер JSON/CSV по `mapping`; конфликты реестра |
| v0.5 | автосбор литературы по теме (шаблон `explore`: сбор → одобрение корпуса → обзор → новизна из пробелов); лимиты журнала и перекрёстные ссылки в черновике; совпавшие блокеры судей первыми; `export`, `reindex`, `gc`, `decide`; эталоны для трёх новых ролей; живой сквозной путь до `paper.docx` на локальных моделях |
| v0.6 (в работе) | живые A/B моделей с выбором автора; параллельные воркер и судьи; `docker compose`; образец профиля журнала |

## Лицензия

MIT, см. [LICENSE](LICENSE).

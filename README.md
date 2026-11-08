# workflow-writing-paper

Конвейер написания научных статей с пулом языковых моделей и веб-интерфейсом.
Автор вводит научную новизну; агенты разворачивают её в исследовательские вопросы,
план экспериментов и черновик статьи; каждый артефакт проверяют модели других семейств,
а принимает человек. Статьи размечаются привязкой к главам и становятся контурами диссертации.

Состояние: v0.4 — все стадии конвейера: от новизны через литературу, план и запуск экспериментов
(local / SSH) до черновика, проверки и сборки.
Техническое задание — [docs/PLAN.md](docs/PLAN.md).

## Принципы

- Переносимость: клон репозитория и `uv`, всё предметное в данных, не в коде.
- Человек в контуре: гейт на границе стадий и при превышении бюджета.
- Числа в тексте только из реестра результатов проекта; проверка без модели.
- Семейство модели, создавшей артефакт, не участвует в его проверке.
- Минимум зависимостей: FastAPI, Jinja2, HTMX, SQLite (FTS5), PyYAML; PDF — через внешний pdftotext.

## Запуск

```bash
uv sync
uv run wwp init            # создаёт workspace/ из defaults/
# заполнить workspace/env и workspace/providers.yaml
uv run wwp serve           # http://127.0.0.1:8765: пул моделей, стенд, журнал вызовов
uv run wwp bench --role critic --providers ollama-gemma4-12b,ollama-qwen3-14b,ollama-lfm25-8b
uv run wwp new my-paper --template experimental   # проект в workspace/projects/my-paper
uv run wwp run my-paper novelty --input "абзац новизны"   # стадия без сервера
uv run wwp run my-paper experiments --scenario bench-c6   # сценарий → results.json
uv run wwp check my-paper  # только детерминированные проверки
uv run wwp lib import ~/papers/*.bib ~/papers/*.pdf   # библиотека: refs.bib, pdf/, индекс FTS5
uv run wwp lib search квантование edge               # полнотекстовый поиск по библиотеке
uv run pytest              # тесты на провайдере fake, без GPU и ключей
```

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

## Этапы

| Версия | Содержимое |
|---|---|
| v0.1 | пул провайдеров, стенд сравнения моделей по ролям |
| v0.2 | проекты, стадии, артефакты, проверка семействами, реестр результатов |
| v0.3 | библиотека: PDF, BibTeX, FTS5, OpenAlex; стадии `literature`, `experiment_plan`; проверка ключей `[@key]` |
| v0.4 | запуск зарегистрированных сценариев экспериментов, локально и по SSH; парсер JSON/CSV по `mapping`; конфликты реестра |

## Лицензия

MIT, см. [LICENSE](LICENSE).

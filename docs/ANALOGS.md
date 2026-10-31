# Аналоги и место wwp среди них

Дата анализа: 2026-10-31. Цель: понять, закрывают ли существующие решения задачи
конвейера, и решить, что из них включать в pipeline, а что оставить рядом.

## 1. Итог

Ни один из найденных продуктов не закрывает три принципа wwp одновременно:
проверка артефактов судьями **других семейств**, детерминированный **реестр чисел**
без модели, **стенд выбора моделей по ролям**. Аналоги сильны там, где wwp слаб или
ещё не реализован: анализ данных, доступ к научным базам, запуск кода, LaTeX.

Решение: workbench-продукты в конвейер не включаются. Они источник данных для
`results.json` и внешний ориентир для стенда. Адаптер появляется только при
безголовом API с возвратом текста, токенов и стоимости (см. PLAN.md, решение 21).

## 2. Классы аналогов

### 2.1. Workbench «агент на данных» (прямые аналоги Claude Science)

| Продукт | Вендор, статус | Модели | Локально | API / headless | Проверка | Цена |
|---|---|---|---|---|---|---|
| Claude Science 0.1.71 | Anthropic, бета с 30.06.2026, macOS/Linux | только Claude | данные локально, модель в облаке | нет: `serve/open/url/status/stop`, вход по одноразовой ссылке | reviewer/fact-checker той же модели | Pro $20, Team для лабораторий |
| K-Dense BYOK (Kady) | K-Dense, MIT, бета 0.9.x, arXiv 2610.00074 | любые через OpenRouter, подписки, Ollama, OpenAI-совместимые | да, полностью (без веб-поиска) | локальный HTTP API с токеном между двумя сервисами; штатного CLI «промпт → ответ» нет | хэш-цепочка лабораторного журнала; судей нет | бесплатно + ключи |
| Rosalind Workbench | OpenAI, research preview с 28.08.2026, внутри ChatGPT | GPT-Rosalind | нет | нет | трассируемые результаты NGS | организациям |
| Microsoft Discovery + Discovery app | Microsoft, GA 02.06.2026; app в preview | Copilot | app локально, auth через GitHub | платформенный, через Azure | аудит и checkpoints на уровне платформы | по Azure-подписке |
| Edison (ex-FutureHouse): Kosmos, Analysis, PaperQA | Edison Scientific, коммерческий | свои | нет | Analysis через API; Kosmos по заявке | отчёт с трассировкой | 10 кредитов/мес бесплатно, Kosmos был $200/прогон |
| Google Co-Scientist / Hypothesis Generation | Google, Nature 05.2026, Labs | Gemini | нет | API только trusted testers | дебаты гипотез внутри одного семейства | по регистрации |

### 2.2. Автономные «AI-учёные» (open source)

| Продукт | Что делает | Модели | Человек в контуре | Проверка |
|---|---|---|---|---|
| Denario (PRX Intelligence 10.2026) | идеи → литература → план → код в Docker → LaTeX → review | только платные API | модульно, формальных гейтов нет | один review-агент; при нечитаемых данных молча генерирует синтетические |
| Agent Laboratory / AgentRxiv (EMNLP 2025) | lit review → эксперименты → отчёт | OpenAI / DeepSeek | copilot: approve / repeat | автооценка той же моделью; фиксированная структура, 2 фигуры |
| AI Scientist-v2 (Sakana) | полностью автономные ML-статьи | облако | нет | свой reviewer |
| Kosmos open-source реализация | self-hosted длинные кампании | несколько провайдеров | нет | отчёт |

### 2.3. Инструменты писателя (не конвейеры)

| Продукт | Роль | Заметка |
|---|---|---|
| OpenAI Prism | LaTeX-редактор с GPT-5.2 | закрывается (октябрь 2026), миграция в Codex; API нет |
| Overleaf, open-prism, OpenPrism | LaTeX | OpenPrism принимает любой OpenAI-совместимый endpoint |
| Paperpal, Writefull, Jenni | правка языка, цитаты | одна модель, без реестра чисел |
| Elicit, Consensus, SciSpace, Scite | поиск и синтез литературы | ориентир для стадии `literature` v0.3 |
| PAT (Int J Med Inform 2026) | локальный мультиагентный аудит рукописи 27B-моделью | ближайший прецедент локального review, одно семейство |
| AISSISTANT (arXiv 2509.12282) | HITL-конвейер для обзорных статей | человек выбирает промежуточные assets |
| K-Dense scientific-agent-skills (177 SKILL.md, MIT) | скиллы для Claude Code / Codex / Cursor | подключаются через провайдер `generic-cli` без своих промптов |

## 3. Сравнение по принципам wwp

| Принцип wwp | Claude Science | K-Dense BYOK | Denario | Agent Lab | wwp |
|---|---|---|---|---|---|
| Чужие семейства проверяют | нет | нет | нет | нет | да |
| Числа только из реестра, проверка кодом | нет | нет | нет (обратная крайность) | нет | да |
| Стенд выбора моделей по ролям | нет | выбор вручную | нет | нет | да |
| Гейты стадий с записью решения | чат | чат | модульно | approve/repeat | approve / override / reject с причиной |
| Локальные модели | нет | да | нет | нет | да |
| Русский язык, профили журналов, GigaChat | нет | нет | нет | нет | да |
| Переносимость одной папкой | нет (аккаунт) | частично (папка + .env) | нет (Docker + ключи) | нет | да |
| Анализ данных, научные базы, запуск кода | сильно | сильно | сильно | средне | только зарегистрированные сценарии, v0.4 |
| LaTeX / фигуры с provenance | сильно | средне | сильно | слабо | pandoc, md → docx |

## 4. Что включать в pipeline

- **Claude Science: нет.** Нет безголового режима; провайдер wwp требует
  `complete(messages) → Completion` с токенами и стоимостью; семейство совпадает с
  `claude-cli`, который уже есть. Использовать рядом: подготовка `results.json`,
  фигур и методов по данным, затем числа вручную в реестр.
- **K-Dense BYOK: пока нет, кандидат.** Единственный аналог с локальным API и
  Ollama. Если из его API можно получить ответ без браузера, это адаптер `kind:
  k-dense` поверх `http.py`. Проверить `docs/security-model.md` и `.env.example`
  в репозитории перед v0.3.
- **K-Dense skills: да, через `generic-cli`.** Сверить промпты `scientific-writing`
  и `peer-review` с `workspace/prompts/`, подключить как альтернативного ideator /
  critic в стенд.
- **Denario, Agent Laboratory: нет.** Свои оркестраторы, свои гейты, конфликт с
  принципами 2 и 4. Источник идей для промптов ролей и рубрик.
- **OpenPrism / Overleaf: v0.5+, экспорт.** Не раньше, чем появится LaTeX-сборка.

## 5. Риски для позиционирования

- Claude Science заявлен как «операционная система исследований». Если добавит
  multi-vendor fact-check, у wwp останутся локальность, русский язык и реестр чисел.
- K-Dense BYOK прямо позиционируется как «Claude Science, но лучше» и уже умеет
  Ollama. Ему не хватает судей других семейств и реестра; это ровно ниша wwp.

Правильная формулировка wwp: не «ещё один AI-учёный», а управляющий слой поверх
любых агентов и скиллов с двумя гарантиями, которых у них нет: кросс-семейная
проверка и детерминированный реестр чисел.

## 6. Источники

- Anthropic, Claude Science: https://www.anthropic.com/news/claude-science-ai-workbench
- K-Dense BYOK: https://github.com/K-Dense-AI/k-dense-byok, https://arxiv.org/abs/2610.00074
- K-Dense skills: https://github.com/k-dense-ai/scientific-agent-skills
- OpenAI Rosalind Workbench: https://news.hino9.com/7128/openai-launches-rosalind-workbench-an-ai-powered-life-sciences-research-platform-now-in-preview
- OpenAI Prism закрытие: https://help.openai.com/en/articles/20001615-prism-to-codex-migration-guide
- Microsoft Discovery GA: https://azure.microsoft.com/en-us/blog/announcing-microsoft-discovery-general-availability-and-microsoft-discovery-app-preview/
- Edison / Kosmos: https://edisonscientific.com/news/announcing-kosmos, https://arxiv.org/abs/2511.02824
- Google Co-Scientist: https://deepmind.google/blog/co-scientist-a-multi-agent-ai-partner-to-accelerate-research/, https://www.nature.com/articles/s41586-026-10644-y
- Denario: https://github.com/AstroPilot-AI/Denario, https://journals.aps.org/prxintelligence/abstract/10.1103/kk55-gc95
- Agent Laboratory: https://arxiv.org/abs/2501.04227; AgentRxiv: https://arxiv.org/abs/2503.18102
- Обзор автономных агентов: https://arxiv.org/abs/2610.39620
- PAT: https://pubmed.ncbi.nlm.nih.gov/42574979/; AISSISTANT: https://arxiv.org/abs/2509.12282
- Обзоры инструментов 2026: https://lumivero.com/resources/blog/ai-tools-for-academic-research/, https://exa.ai/blog/best-ai-tools-for-research

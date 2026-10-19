# Архитектура и карта кодовой базы

[Все разделы](README.md) · [Алгоритм](processing.md) · [API](api.md)

## Общая схема

Локальный конвертер состоит из синхронного ядра и тонкой командной оболочки. Ядро
принимает PDF и возвращает данные в памяти; его CLI записывает пару выходных файлов.
Отдельный пакет `translation` переводит готовые MD/TXT через Gemini и ведёт журнал
прогресса. По умолчанию он тоже офлайн: сеть разрешается только через `--send` /
`allow_network=True`. Сервер, OCR и веб-интерфейс отсутствуют. Auto-перевод
использует локальный SQLite-кэш доступности в корне проекта, не базу документов.

```text
CLI: __main__ / console script → cli.main
                                  │ аргументы + обработчик переносов
                                  ▼
                              convert_pdf
                                  │
     PDF → слова и геометрия → очистка → блоки страниц
                                  │
                     иерархия по всему документу
                                  │
                  выбор страниц → склейка продолжений
                                  │
                          экспорт MD и TXT
                                  │
                       ConversionResult в памяти
                                  │
                         CLI: запись файлов
```

Извлечение, восстановление структуры и экспорт разделены. Геометрия сохраняется
в блоках для проверки спорного места в исходнике. Это не система восстановления
исходной печатной вёрстки: цель — правильный линейный текст для переводчика.

Опциональное продолжение цепочки: MD/TXT → дерево документа → сегменты и пакеты →
явно разрешённый Gemini API → проверенные переводы в журнале → новые MD/TXT.
Оно не импортируется и не вызывается автоматически из `convert_pdf`.

## Карта файлов

Все пути в таблицах относительны корню репозитория; ссылки ведут на реальные файлы.

### Пакет приложения

| Файл | Ответственность и основные точки входа |
| --- | --- |
| [pdf_translator/__init__.py](../pdf_translator/__init__.py) | Публичная граница пакета: `convert_pdf` и семь типов результатов, ошибок и обработчика переносов. Точный список — в API. |
| [pdf_translator/__main__.py](../pdf_translator/__main__.py) | Запуск `python -m pdf_translator`; передаёт управление `cli.main()` и возвращает его код завершения. |
| [pdf_translator/cli.py](../pdf_translator/cli.py) | `main`: аргументы, UTF-8 терминала, выбор режима переносов, предупреждения и ошибки. `_prompt_hyphen` / `_read_choice`: ответы пользователя. `_write_outputs`: защита от перезаписи, подготовка и запись MD/TXT. |
| [pdf_translator/converter.py](../pdf_translator/converter.py) | `convert_pdf`: координация чтения, очистки, вёрстки, заголовков, продолжений и возврат результата в памяти. |
| [pdf_translator/models.py](../pdf_translator/models.py) | Общие структуры: `Word`, `Line`, `Block`, `SourceRegion`, результат, предупреждения и вопрос о переносе; типы `BBox`, `BlockKind`, `HyphenResolver`. `union_bbox` объединяет геометрию. |
| [pdf_translator/layout.py](../pdf_translator/layout.py) | `layout_page`, `_blocks_for_lines`: внутристранничные блоки, колонки, списки, таблицы и вставки; без чтения PDF или межстраничной склейки. |
| [pdf_translator/captions.py](../pdf_translator/captions.py) | `remove_figure_captions`: исключение коротких обособленных подписей по оформлению и соседству с изображением. Защищает основной текст, таблицы, их названия и рамочные вставки. |
| [pdf_translator/headings.py](../pdf_translator/headings.py) | `HeadingStyle` и `assign_heading_levels`: ранжирование размеров по всему документу, отделение названий таблиц и меток перед крупным заголовком от основного оглавления. |
| [pdf_translator/text.py](../pdf_translator/text.py) | `normalize`: пробелы, Unicode, лигатуры. `vocabulary`: словарь выбранного текста. `is_list`: явные маркеры. `join_lines`: склейка строк, переносы и вызов обработчика неоднозначностей. |
| [pdf_translator/export.py](../pdf_translator/export.py) | `render`: одинаковый набор блоков в Markdown и TXT, экранирование, таблицы, списки, рекурсивные вставки. Не читает PDF и не пишет файлы. |
| [pdf_translator/telemetry.py](../pdf_translator/telemetry.py) | `EventSink`, `log_run`, `span`, `emit`, `instrument`: безопасная схема событий, allowlist, UTC/монотонные длительности, ContextVar, идентификаторы и уровни. Без файловой настройки или SDK. |
| [pdf_translator/logging_setup.py](../pdf_translator/logging_setup.py) | CLI-настройка `--log-dir` / `--no-log`, контекст запуска, владение sink и обработка ошибок. `JsonlSink` импортируется из отдельного адаптера. |
| [pdf_translator/diagnostics.py](../pdf_translator/diagnostics.py) | Общий `DiagnosticError`: безопасная категория ожидаемой ошибки; не зависит от перевода или SDK. |
| [pdf_translator/page_data.py](../pdf_translator/page_data.py) | Нейтральные `PageData` / `TableData`, без зависимости от движка вёрстки. |
| [pdf_translator/page_selection.py](../pdf_translator/page_selection.py) | Разбор физических 1-based диапазонов и последовательностей страниц. |
| [pdf_translator/pdf_reader.py](../pdf_translator/pdf_reader.py) | Владение PDF-ресурсами, `_read_page`, объекты pdfplumber, закрытие страниц и перевод ошибок чтения. |
| [pdf_translator/geometry.py](../pdf_translator/geometry.py) | `contains`, `body_size`, `group_lines`: общие координаты и строки. |
| [pdf_translator/preprocessing.py](../pdf_translator/preprocessing.py) | Вертикальные имена, буквицы и повторяющиеся колонтитулы; прежний порядок эвристик. |
| [pdf_translator/continuations.py](../pdf_translator/continuations.py) | Склейка продолжений между страницами/колонками и консервативные проверки шрифта, отступов и скобок. |
| [pdf_translator/file_outputs.py](../pdf_translator/file_outputs.py) | Общая подготовка и публикация UTF-8-файлов; path/overwrite policy задаёт вызывающий сценарий. |
| [pdf_translator/jsonl.py](../pdf_translator/jsonl.py) | Файловый `JsonlSink`: ограниченные части, OS-lock, ротация, retention и защита от ссылок. |

### Необязательный пакет перевода

| Файл | Ответственность |
| --- | --- |
| [pdf_translator/translation/__init__.py](../pdf_translator/translation/__init__.py) | 15 экспортов перевода, включая `TranslationSession` и `TranslationWarning`. Не меняет восемь экспортов корневого пакета. |
| [pdf_translator/translation/__main__.py](../pdf_translator/translation/__main__.py) | Запуск `python -m pdf_translator.translation`. |
| [pdf_translator/translation/cli.py](../pdf_translator/translation/cli.py) | Точка сборки: выбор/создание/закрытие Gemini, его CLI-валидация, параметры, JSON-глоссарий, локальный план без `--send`, прогресс и коды завершения. Console script `pdf-translate`. |
| [pdf_translator/translation/models.py](../pdf_translator/translation/models.py) | Нейтральные `TranslationOptions`, `TranslationPlan`, `TranslationResult`, ошибки, дерево документа, единицы/сегменты и защищённые маркеры. |
| [pdf_translator/translation/ports.py](../pdf_translator/translation/ports.py) | `TranslationProvider.prepare → TranslationSession`, `RequestExecutor`, нейтральные DTO и `BatchTooLarge`; без SDK, промптов и управления ресурсами. |
| [pdf_translator/translation/async_runner.py](../pdf_translator/translation/async_runner.py) | Публичный `translate_file_async`: общий файловый lifecycle с sync API, сборка async workflow/controller. |
| [pdf_translator/translation/async_workflow.py](../pdf_translator/translation/async_workflow.py) | До пяти работников, отдельные сессии, дробление, остановка/сохранение in-flight ответов, отмена всех задач. Нейтральные порты, без SDK и файлов. |
| [pdf_translator/translation/response_recovery.py](../pdf_translator/translation/response_recovery.py) | Нейтральные sync/async повторы целого ответа, проверка ID/маркеров, безопасные категории и исчерпание без дробления/выбора другой модели. |
| [pdf_translator/translation/async_execution.py](../pdf_translator/translation/async_execution.py) | Общий async бюджет/RPM/TPM, атомарное резервирование, неблокирующие ожидания/повторы и прекращение новых попыток. |
| [pdf_translator/translation/async_gemini.py](../pdf_translator/translation/async_gemini.py) | `AsyncGeminiRun`, отдельные модельные сессии, single-flight каталог и токенные проверки через async executor. |
| [pdf_translator/translation/async_routing.py](../pdf_translator/translation/async_routing.py) | Async fallback, общая доступность, проверки перед попыткой, ожидание recovery-probe без вынужденного понижения модели. |
| [pdf_translator/translation/document.py](../pdf_translator/translation/document.py) | Разбор Markdown/TXT и восстановление структуры при экспорте. Общие inline-маркеры вынесены в `inline`. |
| [pdf_translator/translation/chunking.py](../pdf_translator/translation/chunking.py) | `chunk_document`, `split_batch`, версия политики: жёсткий кап 16000, смысловые границы подразделов/абзацев/предложений, привязка заголовка, контейнеры и предупреждения, безопасное токенное дробление без новых ID. Не зависит от провайдера. |
| [pdf_translator/translation/planning.py](../pdf_translator/translation/planning.py) | `plan_translation`, `make_request`: чтение исходника, глоссарий, смысловой план, fingerprint и нейтральные запросы. |
| [pdf_translator/translation/gemini.py](../pdf_translator/translation/gemini.py) | Фабрика `GeminiProvider` и привязанная к настройкам `GeminiSession`: промпт, токенные проверки, бюджетируемые вызовы через `ModelGateway`, декодирование и attribution. |
| [pdf_translator/translation/gemini_format.py](../pdf_translator/translation/gemini_format.py) | Только Gemini: английский промпт, JSON-schema, преобразование запроса/ответа, ревизия адаптера для кэша. |
| [pdf_translator/translation/catalog.py](../pdf_translator/translation/catalog.py) | `rank_models`, `ModelCandidate`, версия политики: фильтр текстовых классов по metadata и именам, числовой полный порядок без списка ID. |
| [pdf_translator/translation/availability.py](../pdf_translator/translation/availability.py) | `AvailabilityCache`, `Cooldown`, `default_database_path`: SQLite cooldowns/aliases/leases, транзакции, короткие подключения и защита от чужой/повреждённой БД. |
| [pdf_translator/translation/routing.py](../pdf_translator/translation/routing.py) | Точка сборки `GeminiAutoProvider`: локальные namespace/ключ, gateway и SQLite, фабрика модельных сессий; `prepare` возвращает `AutoRoutingSession`. |
| [pdf_translator/translation/runner.py](../pdf_translator/translation/runner.py) | Файловый сценарий `translate_file`: opt-in, пути, блокировка, источник, сборка journal/controller/workflow, экспорт и публичный результат. |
| [pdf_translator/translation/storage.py](../pdf_translator/translation/storage.py) | `JsonCheckpointStore`: lock/load/save JSON; отдельные проверки путей и оболочка общего механизма публикации MD/TXT. |
| [pdf_translator/translation/inline.py](../pdf_translator/translation/inline.py) | Общие защищённые маркеры, экранирование и `restore_inline`. |
| [pdf_translator/translation/serialization.py](../pdf_translator/translation/serialization.py) | Строгий JSON без повторных ключей/non-finite чисел и детерминированный SHA-256. |
| [pdf_translator/translation/validation.py](../pdf_translator/translation/validation.py) | Точные ID, значения, маркеры, длины и типизированные эвристические предупреждения. |
| [pdf_translator/translation/journal.py](../pdf_translator/translation/journal.py) | `RunState`, `RequestRecord`, v3 codec, `RunJournal`, порты `CheckpointStore` и `AttemptLedger`; без файловых операций. |
| [pdf_translator/translation/execution.py](../pdf_translator/translation/execution.py) | `ExecutionPolicy`, `RequestController`, чистые решения RPM/TPM/retry; только узкий учёт попыток. |
| [pdf_translator/translation/workflow.py](../pdf_translator/translation/workflow.py) | Provider-independent пакеты, дробление, валидация, принятие прогресса и рендеринг; source/export передаются callback-ами. |
| [pdf_translator/translation/gemini_gateway.py](../pdf_translator/translation/gemini_gateway.py) | Одна SDK-попытка, metadata/catalog/count/generate, endpoint/тайм-аут, санитизация, usage; нейтральные `GenerationResult` / `CatalogEntry`. |
| [pdf_translator/translation/model_routing.py](../pdf_translator/translation/model_routing.py) | `AutoRoutingSession`, порты каталога/доступности и TTL-политика; fallback для модельных квот, 404 и 503/504 после повторов, без SDK, окружения или SQLite. |

Геометрия вынесена в `geometry`, очистка — в `preprocessing`, продолжения — в
`continuations`. В `layout` остаются определение колонок, классификация строк и
упорядочение внутристранничных блоков.

Подчёркивание в начале имени обозначает внутреннюю деталь реализации, а не
публичную точку расширения. Даже функции без подчёркивания в служебных модулях
не входят в экспортированный API пакета, если не перечислены в `__init__.__all__`.

### Тесты

| Файл | Что проверяет |
| --- | --- |
| [tests/__init__.py](../tests/__init__.py) | Делает тесты пакетом; позволяет импортировать общие генераторы контрольных PDF. |
| [tests/test_converter.py](../tests/test_converter.py) | `TextTests`, `PdfTests`, `CliTests`, `ReferencePdfTests`: текстовые правила, сквозная конвертация, CLI и необязательная регрессия на локальном `example.pdf`. |
| [tests/test_continuations.py](../tests/test_continuations.py) | `LabelLayoutTests`, `ContinuationRulesTests`: жирные начала, отступы, списки, кавычки/скобки, страницы/колонки и запреты на ошибочную склейку. |
| [tests/test_captions.py](../tests/test_captions.py) | `CaptionTests`: подписи у изображений, стили и цвета, фоновые изображения и защита полезных коротких текстов. |
| [tests/test_headings.py](../tests/test_headings.py) | `DocumentHeadingTests`, `HeadingStyleTests`: уровни по документу, допуски размеров, выбор страниц, вспомогательные названия, предел H6, отсутствие лишних вопросов. |
| [tests/test_asides.py](../tests/test_asides.py) | `AsideLayoutTests`, `AsideExportTests`: одна рамка — один контейнер, заголовки/абзацы/списки внутри, экспорт без дублирования. |
| [tests/test_translation.py](../tests/test_translation.py) | Планы, разметка, валидация, файлы, CLI и бюджет. Независимая scripted-заглушка без наследования Gemini; сеть запрещена. |
| [tests/test_chunking.py](../tests/test_chunking.py) | Кап/CLI 16000, разделители, подразделы и вложенность, абзацы/предложения/сокращения, заголовки, маркеры, большие/малые контейнеры, токенное дробление, детерминизм и несовместимый старый план. Только офлайн. |
| [tests/test_translation_ports.py](../tests/test_translation_ports.py) | DIP: независимый не-LLM адаптер, порядок ID, контекст/маркеры, дробление, бюджет, resume, несовместимость провайдеров/ревизий/старого журнала. Свежий процесс с запрещёнными импортами Gemini/Google/httpx выполняет ядро. |
| [tests/test_async_translation.py](../tests/test_async_translation.py) | Независимый async-провайдер, максимум пять активных генераций, порядок, бюджет/ожидания, отмена/drain, resume, дробление и совместимость sync-журнала. |
| [tests/test_async_gemini.py](../tests/test_async_gemini.py) | Нативный SDK aio с MockTransport, изолированные клиенты, single-flight каталог, квоты/503/fallback/attribution, recovery leases, отмена и отсутствие fallback при MAX_TOKENS. |
| [tests/test_response_recovery.py](../tests/test_response_recovery.py) | Same-model повторы, атомарность целого пакета, отсутствие дробления, продолжение остальных, incomplete/CLI 4, общий бюджет, отмена, безопасные количества/ID и журнал/resume. |
| [tests/test_gemini.py](../tests/test_gemini.py) | Реальный SDK с `httpx.MockTransport`: JSON-schema/wire format, единственная попытка, выбранный ключ/endpoint, токены, обрезка, ошибки, дневная/минутная квоты. |
| [tests/test_routing.py](../tests/test_routing.py) | Каталог/сортировка/исключения, SQLite-сроки и конкурентные аренды, fallback квот и 503/504 после повторов, Retry-After/TTL, исключения/общая квота, namespaces, alias, пагинация, бюджет/resume/модели сегментов, CLI auto и DST. Только имитации. |
| [tests/test_logging.py](../tests/test_logging.py) | События обеих цепочек, приватность, тайминги/ожидания/попытки, отмена/паузы, resume, стабильные batch-ID, конкурентная запись, ротация/retention и CLI. Внешняя сеть запрещена. |
| [tests/support.py](../tests/support.py) | Общие memory-store и настройка контроллера без файлов, SDK или сети. |
| [tests/test_solid_contracts.py](../tests/test_solid_contracts.py) | Дорефакторинговые fingerprint/v3, общие контракты адаптеров, изоляция сессий, memory-only workflow, ошибки записи, диагностика и AST-границы зависимостей. |

Синтетические PDF создаются в тестовых временных каталогах. Основные проверки
конвертера не зависят от `output/example.*`; отдельная необязательная регрессия
переводческого парсера только читает существующий Markdown. Детали запуска — в
[руководстве разработчика](development.md).

### Настройки и документация

| Файл | Назначение |
| --- | --- |
| [pyproject.toml](../pyproject.toml) | Версия, Python, зависимости, setuptools, console script и состав устанавливаемого пакета. |
| [.gitignore](../.gitignore) | Исключения Git: в частности окружения, сборка, кэши, `output/` и `tmp/`. Общий шаблон содержит исключения для инструментов, которых проект не использует; это не список зависимостей. |
| [README.md](../README.md) | Краткое описание, быстрый старт, примеры API и навигация по документации. |
| [TODO.md](../TODO.md) | Выполненные нарезка, логирование, SOLID, async и восстановление ответов; отложенная живая сравнительная оценка размеров. |
| [AGENTS.md](../AGENTS.md) | Обязательные границы полномочий, языки общения и правила независимого аудита. Это инструкции для агентов, не настройки конвертера. |
| [docs/README.md](README.md) | Оглавление документации, маршруты чтения и границы покрытия. |
| [docs/architecture.md](architecture.md) | Этот документ: карта файлов, зависимости и места изменения. |
| [docs/usage.md](usage.md) | Установка, CLI, выбор страниц, переносы, файлы и коды завершения. |
| [docs/processing.md](processing.md) | Этапы и эвристики восстановления структуры. |
| [docs/api.md](api.md) | Публичный API, модели, координаты, callback и экспорт. |
| [docs/translation.md](translation.md) | Gemini: подготовка ключа/Free Tier, все параметры, API, сохранение структуры, журнал, ограничения и проверки. |
| [docs/solid.md](solid.md) | Результат SOLID-ревизии, владение состоянием/ресурсами, изменения API и совместимость. |
| [docs/logging.md](logging.md) | Технические события всего конвейера: включение, JSONL-схема, тайминги, уровни/приватность, ротация/retention, API и анализ времени. |
| [docs/research/translation-chunk-size.md](research/translation-chunk-size.md) | Теоретическое исследование: источники и пределы выводов, принятое решение о жёстком капе 16000, текущая реализация и ещё не выполненная живая оценка качества. |
| [docs/development.md](development.md) | Окружение, тесты, регрессии, сборка и сопровождение документации. |
| [docs/quality.md](quality.md) | Критерии качества, происхождение результатов и процедура аудита. |
| [docs/troubleshooting.md](troubleshooting.md) | Все коды предупреждений, ошибки и ограничения. |
| [docs/agents/pdf-quality-auditor.md](agents/pdf-quality-auditor.md) | Англоязычная инструкция независимому аудитору. Не регистрирует и не запускает агента автоматически. |

### Локальные и генерируемые файлы

| Путь | Статус и смысл |
| --- | --- |
| `example.pdf` | Пользовательский контрольный PDF; не поставляется с пакетом. `.gitignore` не исключает все PDF: пример может отображаться как untracked. Не добавлять книгу в коммит автоматически. |
| `output/<stem>.md`, `output/<stem>.txt` | Результаты выбранного запуска CLI. Имя не содержит диапазон страниц или версию кода. |
| `output/translations/<stem>.<lang>.md`, `.txt`, `.translation.json` | Перевод и отдельный журнал прогресса. Итоги пишутся только после завершения выбранной пробы/файла. |
| `model-availability.sqlite3` и `-journal` / `-wal` / `-shm` | Общий локальный кэш ограничений auto, наблюдаемых aliases и аренд. Игнорируется Git; тексты и ключи не сохраняются. Не журнал перевода. |
| `logs/run-<UTC>-<uuid>-<part>.jsonl` | Ограниченные по размеру/хранению технические события запусков, без текста книги, ключей и путей источников. По умолчанию CLI; Git игнорирует. |
| `output/documentation.html`, `tmp/docs-html/` | Офлайн-снимок документации, локальные скрипты сборки/проверки и скриншоты. |
| `output/audits/<run-id>/report.md` | Независимые отчёты по определённым снимкам входных файлов. |
| `tmp/` | Временные результаты разработки и проверок, например `tmp/audit-current/`. Это не автоматически актуальная копия `output/`. |
| `tmp/pdfs/audits/<run-id>/` | Отдельные изображения страниц и другие доказательства конкретного аудита. |
| `.venv/`, `__pycache__/`, `*.egg-info/`, `build/`, `dist/` | Окружение, кэши и упаковка. Не исходные файлы приложения. |

Отсутствие каталога из этой таблицы нормально: он появляется только после
соответствующей операции. Подробнее об актуальности результатов —
[контроль качества и артефакты](quality.md).

## Зависимости между модулями

Основной поток: файловый сценарий `runner` → `workflow` → порты провайдера/сессии.
CLI перевода теперь использует `async_runner` → `async_workflow` → async-порты;
старый sync-поток сохранён для API-совместимости. Общие preflight/lock/export
собраны в `runner.open_run`, принятие ответа/сборка — в `workflow`. Async
`prepare_async` возвращает фабрику отдельных рабочих сессий; каждый сетевой клиент
используется одним работником. Общие каталог, availability cache и controller
согласованы явно. Сетевые вызовы `AsyncGeminiGateway` используют SDK `aio`.
Изменения журнала сериализованы на одном event loop, не в фоновых потоках.
`response_recovery` стоит и в нейтральном workflow, и внутри auto после выбора
конкретной модели. Исчерпание помечается отдельной ошибкой, поэтому внешний
workflow не начинает второй вложенный цикл. Обе схемы API сохраняют пакет в
`failed_batches`, не останавливая независимую работу; фатальные ошибки/паузы
по-прежнему прекращают новые запросы. Strict/legacy — настройка конкретного
Gemini-адаптера и его identity, не знание ядра о wire format.
`workflow` не знает файлов, JSON или Gemini; `RunJournal` получает
`CheckpointStore`, а `RequestController` — только `AttemptLedger`.
`JsonCheckpointStore` выбирается файловым фасадом. Общие JSON/hash, маркеры и
валидация не зависят от планировщика. Формат плана/журнала остаётся v3.

`GeminiProvider` создаёт gateway и возвращает связанную с настройками сессию;
`GeminiAutoProvider` собирает каталог, SQLite и фабрику модельных сессий.
Политика `model_routing` зависит от портов, не от окружения или конкретного SDK.
Все внешние попытки по-прежнему проходят через executor. Вызывающий API код/CLI
владеет закрытием провайдера; готовый кэш не требует даже подготовки.

`page_data` и `models` содержат нейтральные структуры; модели ошибок опираются
только на общий `DiagnosticError`. `captions` использует геометрию, а не
движок вёрстки. Чтение PDF находится в `pdf_reader`, координация — в
`converter`, обработка продолжений — отдельно. Функциональные эвристики сохранены.

Поперечные события используют порт `telemetry.EventSink`; CLI подключает
`jsonl.JsonlSink`. Ошибки распознаются по общему контракту, предупреждения — по
явным кодам, не строкам. Ни импорт, ни API без sink не создаёт логов.
Общий `file_outputs` обслуживает оба CLI, сохраняя их отдельные политики путей.

Обоснования и переход на новый API: [SOLID](solid.md).

## Где искать причину проблемы

| Симптом или изменение | С чего начать | Основные регрессии |
| --- | --- | --- |
| Неправильные слова/координаты/обнаружение таблицы | `pdf_reader._read_page` | `test_converter.PdfTests` |
| Вклинившаяся подпись к рисунку | `captions` | `test_captions` и контрольная книга |
| Вертикальное имя на поле | `preprocessing.remove_marginal_credits` | `test_converter.PdfTests` и контрольная книга |
| Порядок колонок, абзацы и списки | `layout_page`, `_blocks_for_lines` | `test_converter`, `test_continuations` |
| Продолжение между страницами | `merge_continuations` | `test_continuations`, `Loyal Cadre` в контрольной книге |
| Неправильный уровень заголовка | `headings.assign_heading_levels` | `test_headings` |
| Одна вставка разбита на несколько | Рамки в `_read_page`, контейнеры в `layout_page`, рекурсия `export.render` | `test_asides` |
| Перенос слова | `text.join_lines`, словарь в `convert_pdf`, режим в `cli` | `TextTests`, `CliTests` |
| Markdown расходится с TXT | `export.render` и модель исходного блока | `TextTests`, `AsideExportTests`, таблицы |
| Аргументы/перезапись/отмена | `cli` | `CliTests` |
| Перевод: разметка, сегменты, пропавшие ID | `translation.document`, `translation.validation` | `test_translation` |
| Перевод: кап, подразделы, одинокий заголовок, большие контейнеры | `translation.chunking` | `test_chunking` |
| Перевод: квота, возобновление, коллизии файлов | `translation.execution`, `translation.journal`, `translation.storage` | `test_translation` |
| Перевод: ключ/SDK/HTTP-ответ | `translation.gemini_gateway` | `test_gemini` |
| Другой переводчик / границы DIP | `translation.ports`, точка сборки в `translation.cli` | `test_translation_ports` |
| Автовыбор / fallback / SQLite | `translation.catalog`, `translation.model_routing`, `translation.availability` | `test_routing`, `test_gemini` |

Сначала нужно выяснить, на каком этапе появилось расхождение: извлечение слова,
классификация/порядок блока или только его сериализация. Не следует исправлять
потерю текста в экспортере, если слово было удалено раньше фильтром.

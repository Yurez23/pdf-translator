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
| [pdf_translator/converter.py](../pdf_translator/converter.py) | `convert_pdf`: координация полного конвейера. `parse_pages`: разбор диапазона. `_read_page`: извлечение слов, таблиц, рамок, разделителей и изображений; первичная очистка. `_text_color`: нормализация цвета. |
| [pdf_translator/models.py](../pdf_translator/models.py) | Общие структуры: `Word`, `Line`, `Block`, `SourceRegion`, результат, предупреждения и вопрос о переносе; типы `BBox`, `BlockKind`, `HyphenResolver`. `union_bbox` объединяет геометрию. |
| [pdf_translator/layout.py](../pdf_translator/layout.py) | `PageData` / `TableData`; группировка строк, буквицы, фильтр вертикальных имён, колонтитулы, определение колонок. `layout_page`: блоки и порядок чтения. `merge_continuations`: соединение абзацев между колонками/страницами. |
| [pdf_translator/captions.py](../pdf_translator/captions.py) | `remove_figure_captions`: исключение коротких обособленных подписей по оформлению и соседству с изображением. Защищает основной текст, таблицы, их названия и рамочные вставки. |
| [pdf_translator/headings.py](../pdf_translator/headings.py) | `HeadingStyle` и `assign_heading_levels`: ранжирование размеров по всему документу, отделение названий таблиц и меток перед крупным заголовком от основного оглавления. |
| [pdf_translator/text.py](../pdf_translator/text.py) | `normalize`: пробелы, Unicode, лигатуры. `vocabulary`: словарь выбранного текста. `is_list`: явные маркеры. `join_lines`: склейка строк, переносы и вызов обработчика неоднозначностей. |
| [pdf_translator/export.py](../pdf_translator/export.py) | `render`: одинаковый набор блоков в Markdown и TXT, экранирование, таблицы, списки, рекурсивные вставки. Не читает PDF и не пишет файлы. |

### Необязательный пакет перевода

| Файл | Ответственность |
| --- | --- |
| [pdf_translator/translation/__init__.py](../pdf_translator/translation/__init__.py) | Публичные план/запуск/options/plan/result/error и семь портов/DTO расширения. Не меняет восемь экспортов корневого пакета. |
| [pdf_translator/translation/__main__.py](../pdf_translator/translation/__main__.py) | Запуск `python -m pdf_translator.translation`. |
| [pdf_translator/translation/cli.py](../pdf_translator/translation/cli.py) | Точка сборки: выбор/создание/закрытие Gemini, его CLI-валидация, параметры, JSON-глоссарий, локальный план без `--send`, прогресс и коды завершения. Console script `pdf-translate`. |
| [pdf_translator/translation/models.py](../pdf_translator/translation/models.py) | Нейтральные `TranslationOptions`, `TranslationPlan`, `TranslationResult`, ошибки, дерево документа, единицы/сегменты и защищённые маркеры. |
| [pdf_translator/translation/ports.py](../pdf_translator/translation/ports.py) | Порт `TranslationProvider`, `RequestExecutor`, DTO `TranslationRequest` / `TranslationItem` / `TranslationResponse`, identity и сигнал `BatchTooLarge`. Нет SDK, промптов или JSON-ответов. |
| [pdf_translator/translation/document.py](../pdf_translator/translation/document.py) | `parse_document`, `restore_inline`, `render_document`: Markdown/TXT, вложенные контейнеры, таблицы, защита inline-разметки и экспорт. |
| [pdf_translator/translation/chunking.py](../pdf_translator/translation/chunking.py) | `chunk_document`, `split_batch`, версия политики: жёсткий кап 16000, смысловые границы подразделов/абзацев/предложений, привязка заголовка, контейнеры и предупреждения, безопасное токенное дробление без новых ID. Не зависит от провайдера. |
| [pdf_translator/translation/planning.py](../pdf_translator/translation/planning.py) | `plan_translation`, `make_request`, `validate_translations`, `validate_text`: вызов смыслового планировщика, глоссарий, fingerprint с identity/политикой/пакетами, нейтральные запросы и проверки. Строгий JSON для локальных файлов. |
| [pdf_translator/translation/gemini.py](../pdf_translator/translation/gemini.py) | Адаптер `GeminiProvider`: локальный `prepare`, SDK, фиксированный endpoint, ключ из окружения, metadata/countTokens/generateContent через executor, лимиты, санитизация ошибок и проверка завершения ответа. |
| [pdf_translator/translation/gemini_format.py](../pdf_translator/translation/gemini_format.py) | Только Gemini: английский промпт, JSON-schema, преобразование запроса/ответа, ревизия адаптера для кэша. |
| [pdf_translator/translation/catalog.py](../pdf_translator/translation/catalog.py) | `rank_models`, `ModelCandidate`, версия политики: фильтр текстовых классов по metadata и именам, числовой полный порядок без списка ID. |
| [pdf_translator/translation/availability.py](../pdf_translator/translation/availability.py) | `AvailabilityCache`, `Cooldown`, `default_database_path`: SQLite cooldowns/aliases/leases, транзакции, короткие подключения и защита от чужой/повреждённой БД. |
| [pdf_translator/translation/routing.py](../pdf_translator/translation/routing.py) | `GeminiAutoProvider`: свежий постраничный каталог через executor, namespace, сроки повторов, квотный fallback, пауза и нейтральный ответ с фактической моделью. |
| [pdf_translator/translation/runner.py](../pdf_translator/translation/runner.py) | `translate_file`, `RequestController`: opt-in, совместимость журнала, все API-бюджеты, повторы, возобновление, валидация и финальный экспорт. |
| [pdf_translator/translation/storage.py](../pdf_translator/translation/storage.py) | Эксклюзивный lock, атомарное обновление JSON-журнала, защита исходника/выходов и запись пары файлов. |

Внутри `layout.py` основные участки разделены по задачам:

| Задача | Функции |
| --- | --- |
| Геометрия и подготовка | `contains`, `body_size`, `group_lines`, `integrate_dropcaps` |
| Исключаемые поля | `remove_marginal_credits`, `remove_running_margins` |
| Колонки и внутристранничная структура | `_gutter`, `_blocks_for_lines`, `_ordered_region`, `_table_blocks`, `layout_page` |
| Различение прозы и списка | `_terminal`, `_lead_in`, `_hanging_lead_in` |
| Продолжения | `_line_font`, `_closes_open_bracket`, `_continuation_start`, `_continuation_alignment`, `merge_continuations` |

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
| [tests/test_translation.py](../tests/test_translation.py) | План, Markdown/TXT, валидация, журнал, возобновление, лимиты, ошибки и отдельный CLI. Fake provider, сетевые соединения запрещены. Локальный `output/example.md`, если есть, только читается для проверки разбора. |
| [tests/test_chunking.py](../tests/test_chunking.py) | Кап/CLI 16000, разделители, подразделы и вложенность, абзацы/предложения/сокращения, заголовки, маркеры, большие/малые контейнеры, токенное дробление, детерминизм и несовместимый старый план. Только офлайн. |
| [tests/test_translation_ports.py](../tests/test_translation_ports.py) | DIP: независимый не-LLM адаптер, порядок ID, контекст/маркеры, дробление, бюджет, resume, несовместимость провайдеров/ревизий/старого журнала. Свежий процесс с запрещёнными импортами Gemini/Google/httpx выполняет ядро. |
| [tests/test_gemini.py](../tests/test_gemini.py) | Реальный SDK с `httpx.MockTransport`: JSON-schema/wire format, единственная попытка, выбранный ключ/endpoint, токены, обрезка, ошибки, дневная/минутная квоты. |
| [tests/test_routing.py](../tests/test_routing.py) | Каталог/сортировка/исключения, SQLite-сроки и конкурентные аренды, fallback/общая квота, namespaces, alias, пагинация, бюджет/resume/модели сегментов, CLI auto и DST. Только имитации. |

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
| [TODO.md](../TODO.md) | Статус задач: выполненная смысловая нарезка, отложенные конкурентные запросы, живое сравнение размеров, постоянное логирование и ревизия/рефакторинг по SOLID. Невыполненный пункт не разрешает начать его без команды. |
| [AGENTS.md](../AGENTS.md) | Обязательные границы полномочий, языки общения и правила независимого аудита. Это инструкции для агентов, не настройки конвертера. |
| [docs/README.md](README.md) | Оглавление документации, маршруты чтения и границы покрытия. |
| [docs/architecture.md](architecture.md) | Этот документ: карта файлов, зависимости и места изменения. |
| [docs/usage.md](usage.md) | Установка, CLI, выбор страниц, переносы, файлы и коды завершения. |
| [docs/processing.md](processing.md) | Этапы и эвристики восстановления структуры. |
| [docs/api.md](api.md) | Публичный API, модели, координаты, callback и экспорт. |
| [docs/translation.md](translation.md) | Gemini: подготовка ключа/Free Tier, все параметры, API, сохранение структуры, журнал, ограничения и проверки. |
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
| `output/documentation.html`, `tmp/docs-html/` | Офлайн-снимок документации, локальные скрипты сборки/проверки и скриншоты. |
| `output/audits/<run-id>/report.md` | Независимые отчёты по определённым снимкам входных файлов. |
| `tmp/` | Временные результаты разработки и проверок, например `tmp/audit-current/`. Это не автоматически актуальная копия `output/`. |
| `tmp/pdfs/audits/<run-id>/` | Отдельные изображения страниц и другие доказательства конкретного аудита. |
| `.venv/`, `__pycache__/`, `*.egg-info/`, `build/`, `dist/` | Окружение, кэши и упаковка. Не исходные файлы приложения. |

Отсутствие каталога из этой таблицы нормально: он появляется только после
соответствующей операции. Подробнее об актуальности результатов —
[контроль качества и артефакты](quality.md).

## Зависимости между модулями

`models` не зависит от остальных модулей проекта. `text`, `export` и `headings`
опираются на модели. `layout` использует модели и текстовые правила. `captions`
использует модели и геометрические функции `layout`. `converter` объединяет эти
части и работает с `pdfplumber`; `cli` вызывает конвертер и занимается вводом/выводом.
Тестовая генерация PDF через ReportLab не является зависимостью рабочего ядра.

В `translation` модели независимы от SDK; `document` лениво использует MarkdownIt,
`chunking` выбирает смысловые границы и безопасно делит пакет при токенном отказе.
`planning` собирает план версии контракта 3 и проверяет ответы. `storage` отвечает за файловую запись.
По DIP `runner` и `planning` зависят от абстракций `ports`, а не от `gemini`:
ядро не выбирает провайдера и не знает формата сетевого ответа. Конкретный адаптер
выбирает и закрывает `cli` (либо вызывающий API код). `gemini` реализует общий порт,
использует `gemini_format` и лениво импортирует SDK только в локальном `prepare`.
Ядро запрашивает подготовку лишь для несохранённых сегментов. Все внешние попытки
адаптер проводит через переданный `RequestExecutor`; это позволяет сохранить
бюджеты без навязывания другому переводчику промптов/токенов. Обычный API конвертации не начинает
использовать дополнительные зависимости или сеть после установки extra.

## Где искать причину проблемы

| Симптом или изменение | С чего начать | Основные регрессии |
| --- | --- | --- |
| Неправильные слова/координаты/обнаружение таблицы | `converter._read_page` | `test_converter.PdfTests` |
| Вклинившаяся подпись к рисунку | `captions` | `test_captions` и контрольная книга |
| Вертикальное имя на поле | `layout.remove_marginal_credits` | `test_converter.PdfTests` и контрольная книга |
| Порядок колонок, абзацы и списки | `layout_page`, `_blocks_for_lines` | `test_converter`, `test_continuations` |
| Продолжение между страницами | `merge_continuations` | `test_continuations`, `Loyal Cadre` в контрольной книге |
| Неправильный уровень заголовка | `headings.assign_heading_levels` | `test_headings` |
| Одна вставка разбита на несколько | Рамки в `_read_page`, контейнеры в `layout_page`, рекурсия `export.render` | `test_asides` |
| Перенос слова | `text.join_lines`, словарь в `convert_pdf`, режим в `cli` | `TextTests`, `CliTests` |
| Markdown расходится с TXT | `export.render` и модель исходного блока | `TextTests`, `AsideExportTests`, таблицы |
| Аргументы/перезапись/отмена | `cli` | `CliTests` |
| Перевод: разметка, сегменты, пропавшие ID | `translation.document`, `translation.planning` | `test_translation` |
| Перевод: кап, подразделы, одинокий заголовок, большие контейнеры | `translation.chunking` | `test_chunking` |
| Перевод: квота, возобновление, коллизии файлов | `translation.runner`, `translation.storage` | `test_translation` |
| Перевод: ключ/SDK/HTTP-ответ | `translation.gemini` | `test_gemini` |
| Другой переводчик / границы DIP | `translation.ports`, точка сборки в `translation.cli` | `test_translation_ports` |
| Автовыбор / fallback / SQLite | `translation.catalog`, `translation.routing`, `translation.availability` | `test_routing`, `test_gemini` |

Сначала нужно выяснить, на каком этапе появилось расхождение: извлечение слова,
классификация/порядок блока или только его сериализация. Не следует исправлять
потерю текста в экспортере, если слово было удалено раньше фильтром.

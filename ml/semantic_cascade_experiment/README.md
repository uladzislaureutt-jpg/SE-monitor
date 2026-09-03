# Semantic cascade blind evaluation 1.0

Изолированный ручной эксперимент. Он не импортируется рабочим мониторингом,
не имеет расписания и не меняет `social_monitor.py`, `state.json`,
`discovery_cache.json`, источники или доставку отчётов.

Сравниваются заранее определённые кандидаты: TF-IDF word+character,
`cointegrated/rubert-tiny2` и `intfloat/multilingual-e5-small`. Общий режим —
`title_only`, поскольку у 120 исходных regex-REJECT в новом GOLD не
заархивирован текст статьи.

Два порога выбираются исключительно на прежней SILVER-validation. Низкий порог
разрешает veto для regex KEEP, высокий — rescue для regex REJECT. Между порогами
исходное решение regex сохраняется. Требуемая эмпирическая точность каждого
действия на SILVER-validation — 90%.

Новый GOLD используется один раз после обучения и выбора порогов. Результат
workflow всегда диагностический: даже кандидат, прошедший ворота, получает
только статус `SHADOW_ONLY_CANDIDATE`.

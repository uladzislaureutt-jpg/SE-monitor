# Update82 — установка

Скопируйте содержимое архива в корень репозитория, сохранив структуру
каталогов. Подтвердите замену одноимённых файлов. Перед этим сохраните
собственный коммит или резервную копию; пакет рассчитан на установленный
update81.

Из корня репозитория выполните:

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
python -c "import social_monitor as m; print(m.MONITOR_BUILD)"
```

Ожидаемая сборка: `2026-09-05.social.82-regex-integrity-semantic-history-1.0`.

Пакет не меняет источники, получателей, секреты, расписание, `state.json` или
`discovery_cache.json`. Он дополняет плановый workflow сохранением приватных
семантических входов в `data/ml/raw_history/semantic_inputs/`.

Модель Hugging Face не запускается в плановом мониторинге. Добавлен отдельный
ручной workflow `Social semantic shadow`: он создаёт приватный диагностический
артефакт по уже архивированному regex-прогону. Отчёт, CSV, email и Telegram
по-прежнему формируются только regex-конвейером.

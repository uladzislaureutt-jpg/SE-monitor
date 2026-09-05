# Update83 — установка

Распакуйте содержимое архива в корень репозитория с заменой одноимённых
файлов. Пакет ставится поверх update82/update82.1; секреты, источники,
расписание и получатели не меняются.

Из корня репозитория выполните:

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
python -c "import social_monitor as m; print(m.MONITOR_BUILD)"
```

Ожидаемая сборка: `2026-09-05.social.83-run47-integrity-shadow-score-1.0`.

Обычный запуск повторять не нужно. После commit/push откройте Actions →
`Social semantic shadow` → Run workflow и оставьте `input_file` пустым:
workflow возьмёт уже сохранённый последний `semantic_inputs` и пересчитает
только приватный shadow-артефакт. Старый `social-semantic-shadow-1` не
используйте: в нём неверная шкала баллов v2.0.

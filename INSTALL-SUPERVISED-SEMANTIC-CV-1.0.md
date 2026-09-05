# Установка и запуск

1. Распакуйте архив в корень репозитория с объединением папок.
2. Зафиксируйте и отправьте изменения в GitHub.
3. В Actions вручную запустите **S-monitor supervised semantic CV (manual, isolated)**.
4. Скачайте артефакт `s-monitor-supervised-semantic-cv-results` и передайте его
   для анализа.

Этот workflow не меняет `social_monitor.py`, основной отчёт, state/cache,
источники и рассылку. Обычный мониторинговый запуск для него не нужен.

Локальная предварительная проверка:

```bash
python tests/test_semantic_supervised_cv.py
```

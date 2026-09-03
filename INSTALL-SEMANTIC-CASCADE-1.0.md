# Установка и запуск

## Установка

1. Сделайте резервную копию репозитория или создайте новую ветку.
2. Распакуйте архив **в корень репозитория** с объединением одноимённых папок.
3. Убедитесь, что появились:
   - `.github/workflows/social-semantic-cascade-blind-eval.yml`;
   - `ml/semantic_cascade_experiment/`;
   - `data/ml/blind_gold_2026_09_03/`.
4. Загрузите изменения в GitHub.

При желании до загрузки выполните проверку данных:

```bash
python ml/semantic_cascade_experiment/validate_inputs.py --legacy-data data/ml/event_safe_v1 --gold-data data/ml/blind_gold_2026_09_03
python tests/test_semantic_cascade_experiment.py
```

Пакет добавляет только новые пути. Он не должен предлагать замену
`social_monitor.py`, рабочих workflow, конфигурации, секретов, state или cache.

## Запуск

1. Откройте вкладку **Actions**.
2. Выберите **S-monitor semantic cascade blind evaluation (manual, isolated)**.
3. Нажмите **Run workflow**.
4. Дождитесь завершения; CPU-обучение двух SetFit-моделей может быть долгим.
5. Скачайте один артефакт `s-monitor-semantic-cascade-blind-eval-results` и
   передайте его для анализа.

Запуск не подключает модель к мониторингу. Даже успешный результат означает
только возможность следующего теневого этапа.

## Откат

Удалите три добавленных пути, перечисленных в пункте 3. Рабочая система от них
не зависит.

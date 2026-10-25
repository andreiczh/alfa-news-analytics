# Render · Пульс бизнеса 0.6.3

[Демо](https://alfa-business-pulse.onrender.com/auth/login). Runtime `3e8a22bb20831ea9791681cf3d98c25a8c2f7410`, deployment `dep-dbf3gc8473hc73bnevlg`, LIVE 2026-10-25T17:12:54.357448Z. Версия 0.6.3 исправляет экраны входа/регистрации и добавляет безопасное одноразовое операторское создание тестового аккаунта. [CI/Docker/Compose](https://github.com/andreiczh/alfa-news-analytics/actions/runs/40215037075), [проверки](validation-login-063.json), [критическое ревью](docs/LOGIN_0_6_3.md). Обычная команда запуска восстановлена; временная конфигурация создания аккаунта удалена, прежние секреты сохранены. Аккаунт находится в PostgreSQL; данные входа не публикуются в репозитории.

## Предыдущий выпуск аналитики 0.6.2

[Демо](https://alfa-business-pulse.onrender.com). Runtime `15ba5ff784785ffc8406c4f6418f7f80849dc51b`, deployment `dep-dbf1oarncjis73dobejg`, LIVE 2026-10-25T15:13:04.752995Z. [CI/Docker/Compose](https://github.com/andreiczh/alfa-news-analytics/actions/runs/40205587212), [протокол](validation-report-v6.json), [качество и ограничения](docs/HARDENING_0_6.md).

Сервис `srv-dbdphesipgvc739mcm80`, Free/Frankfurt, один API/worker, auto-deploy off. Новая PostgreSQL `dpg-dbesc47avr4c73bvqekg-a`, тоже Free/Frankfurt, действует до **24 ноября 2026 года**. Платные ресурсы и способ оплаты не добавлялись. [Ограничения Free](https://render.com/docs/free).

## Конфигурация

Build: `pip install --require-hashes -r requirements.lock && python -m alfa_agent build-case-index`. Start: `python -m alfa_agent serve`. Python 3.12.14, `PYTHONPATH=src`. Рабочее состояние и оригиналы файлов — PostgreSQL; локальный диск — кэш. `ALFA_AUTH_MODE=accounts`, `ALFA_REQUIRE_DURABLE_STORAGE=1`, `ALFA_REQUIRE_WORKER=1`. TLS БД — verify-full; старые ключ модели/секрет подписи/код команды сохранены без публикации.

`ALFA_ACCESS_PASSWORD` в этом режиме служит кодом первичной регистрации. Затем вход по личным логину и паролю. Одноразовые приглашения в пространство коллеги создаёт владелец, профессиональная роль прав не даёт. Работодатель не проверяется через SSO.

В новую БД один раз перенесены только два явно публичных примера из поставки. Исчезнувшие временные пользовательские данные старой 0.5 не объявляются автоматически перенесёнными. Данные, созданные после перехода на PostgreSQL, пережили последующие деплои: аккаунт, контекст, исходный файл и шесть итоговых отчётов проверены по хешам.

## Копирование и восстановление

Операторский `POST /ops/backup` требует отдельный `X-Operator-Token`; обычный аккаунт не получает полный backup. Секрет и архивы не находятся в Git. Контрольная копия скачана на Mac с правами 0600; 742 строки восстановлены в пустую отдельную PostgreSQL, включая аккаунт, файл и все шесть отчётов. Сессии при восстановлении удаляются, неопределённые незавершённые платные вызовы автоматически не повторяются.

CLI: `python -m alfa_agent backup-state --output <private-path>`; восстановление при остановленном worker в **пустое** хранилище: `python -m alfa_agent restore-state --input <private-path> --into-empty`. Задайте целевой DSN/схему отдельно и не направляйте эту команду в действующую базу. SHA-256 проверяет целостность, это не шифрование архива. Регулярный независимый offsite backup не настроен.

## Приёмка и откат

283 локальных теста с реальной PostgreSQL, Docker/Compose CI, 23 облачных браузерных проверки. 6 из 6 повторных ролевых сценариев — PARTIAL, без пересмотров, с условными сценариями и `human_verified=false`. Малый HTTP-прогон: 100/100 при concurrency 4, p95 656.57 мс; при concurrency 20: 200/200, p95 3876.33 мс. Это ограниченная приёмка, не промышленный SLA.

Откат совместимого runtime: `b7b57b6ae76e8535097bdff5c93f8f5d17338aa9` (то же финансовое ядро v6.2), deployment `dep-dbf113qvcj2c73ef39og`. Перед откатом сохранить актуальную копию и сохранить PostgreSQL/env accounts; откат к 0.5 со старым временным хранилищем не является безопасным откатом данных. Последующие commits только документов могут отличаться от running SHA. Публичный health показывает runtime_commit и analysis_policy_version.

# LumaFlow

Семестровий проєкт з дисципліни «Програмування та підтримка веб-застосунків». Застосунок зменшує цифровий шум на зображеннях медіанним фільтром і підсилює контури. Браузер показує стан обробки, зберігає історію та дозволяє порівняти зображення до й після обробки.

[Почати з нуля: запуск, архітектура, код і Git](docs/start-here.html)

## Запуск на Windows

Розпакуй проєкт у папку та двічі натисни **Start-LumaFlow.cmd**. Він запускає HTTPS-сервер і два сервери застосунків, очікує їхньої готовності та відкриває https://localhost:8443 в окремому профілі Edge або Chrome. Профіль довіряє ключу локального сертифіката. Права адміністратора Windows для цього режиму не потрібні. Якщо Python із залежностями недоступний, скрипт створює локальне віртуальне середовище. Для цього знадобляться Python 3.12+ та доступ до PyPI.

У PowerShell з папки проєкту доступні команди:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action admin
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action stop
```

Команда `admin` виводить логін і пароль адміністратора, створені під час першого запуску. Для звичайного акаунта використай кнопку **Реєстрація**. База, ключі сертифіката, пароль локального адміністратора та журнали розміщені в `.runtime`. Для повторного запуску після перезавантаження знову відкрий `Start-LumaFlow.cmd`.

## Запуск у Docker

На Windows потрібен Docker Desktop з WSL 2 і режимом Linux-контейнерів. Для першого встановлення WSL 2 може знадобитися PowerShell від імені адміністратора та перезавантаження. Під час першої збірки Docker завантажує базовий образ Python і Pillow. Дочекайся завершення поточних задач і готовності Docker Desktop. Двічі натисни **Start-LumaFlow-Docker.cmd**. Скрипт збирає образ за потреби, перемикає застосунок у Docker-режим і відкриває браузер.

Для покрокового запуску через PowerShell виконай:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action stop
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action build
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action start -Mode docker
```

Планувальник запускає два контейнери, під навантаженням додає до двох нових і зупиняє додаткові після 16 секунд простою за порожньої черги. У Docker кожен сервер має ліміти в 1 CPU, 512 МБ RAM і 64 процеси. Ідентифікатори контейнерів відображаються в моніторингу. Команда `docker ps --filter ancestor=lumaflow-worker:local` показує запущені контейнери. На одному ноутбуці це окремі екземпляри application server на спільному фізичному хості.

## Перевірка

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action verify
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action verify -Live
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run.ps1 -Action verify -Mode docker
```

Перша команда тестує TLS, бекенд і окремі сервери застосунків. Друга запускає ізольований локальний екземпляр і додатково проходить справжній HTTPS. Третя запускає ізольований Docker-екземпляр із тим самим сценарієм та перевіряє різні ID контейнерів через Docker. Для третьої потрібен уже зібраний образ. Живі перевірки використовують порт 19443 і порти 19451–19454. Тестові задачі й акаунти зберігаються в тимчасовій базі.

## Матеріали для здачі

- [Схема архітектури](docs/architecture.svg)
- [Схема контейнерного режиму](docs/docker-architecture.svg)
- [Умова й сценарій захисту](docs/defense.html)

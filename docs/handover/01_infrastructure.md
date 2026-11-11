# Инфраструктура

## Карта

```
                 Пользователи в РФ (склад, ТСД, владелец без VPN)
                                   │
                     marketpartners.ru  (VPS в РФ, Caddy)
        ┌──────────────┬───────────┴───────────┬──────────────────────┐
   сайт агентства   /kiz, /api/kiz/*     biomed./fk./tl.         /crpt/*  → «Честный знак»
   (статика)        → mpfit-dashboard    marketpartners.ru        (mobile.api.crpt.ru, из РФ)
                                         → дашборды
                                   │
                    туннель Xray (VPN владельца) ──► Render (США, Virginia)
                                                      ├─ wb-dashboard   (Biomed)
                                                      ├─ fk-dashboard   (Миагра)
                                                      ├─ tl-dashboard   (Технолюкс, Аврора)
                                                      ├─ mpfit-dashboard (фулфилмент)
                                                      ├─ romanvx        (wbfetch, Docker)
                                                      └─ mp-postgres    (одна база, схемы)

   Домашний ПК владельца: agent/wb_browser_agent.py (Playwright) — выдача WB
```

## Render

| Сервис | Код | Тариф | Заметки |
|---|---|---|---|
| wb-dashboard | `backend/` + `frontend/` | Starter, 512 МБ | `DB_SCHEMA=biomed`. Главный кабинет. |
| fk-dashboard | тот же код | free (засыпает) | `DB_SCHEMA=fk`. |
| tl-dashboard | тот же код | — | кабинеты Технолюкс/Аврора, только чтение. |
| mpfit-dashboard | `mpfit_dash/` | — | старт: `uvicorn app:app` (один воркер — очередь ТСД в памяти). |
| romanvx | `wbfetch/` | Docker | вспомогательный сервис запросов к WB. |
| mp-postgres | — | Basic-256mb | одна база на всех, разведение по схемам (`backend/db.py`, `search_path`). |

- **Деплой:** пуш в `main` → Render автоматически пересобирает все сервисы.
- **Регион:** Virginia (США). Из РФ Render заблокирован по IP — поэтому VPS.
- **База** до 15.07.2026 была на Neon, переехали из-за лимита трафика
  (`scripts/migrate_db.py`). Neon-проекты можно удалить.
- **Память 512 МБ — главное ограничение** дашборда. Правила выживания — в
  `CLAUDE.md` («Архитектура выживания на 512 МБ»): тяжёлые сборки строго по
  одной (`heavy.guard`), снапшоты кешей в `kv_cache` (zlib, потоково), детальный
  отчёт WB страницами по 10 тыс. строк, `/api/health` показывает `rss_mb`.
- `_keep_awake` в `main.py` пингует сервис 7–24 МСК, чтобы не засыпал.

## Переменные окружения

Значения — только в Render → Environment. Полный список по сервисам — в
[`03_dashboard.md`](03_dashboard.md) и [`07_mpfit_warehouse.md`](07_mpfit_warehouse.md).
Главные:

| Переменная | Где | Зачем |
|---|---|---|
| `DATABASE_URL`, `DB_SCHEMA` | все | Postgres и схема кабинета |
| `WB_API_KEY` | дашборды | токен WB (у Миагры **истёк ~27.10.2026**, нужен новый) |
| `OZON_CLIENT_ID`, `OZON_API_KEY` | дашборды | Ozon Seller API |
| `OZON_PERF_CLIENT_ID`, `OZON_PERF_CLIENT_SECRET` | дашборды | Ozon Performance (реклама) |
| `YM_API_KEY`, `YM_CAMPAIGN_ID`, `YM_BUSINESS_ID` | wb-dashboard | Яндекс Маркет |
| `ANTHROPIC_API_KEY` | дашборды | ИИ: ответы на отзывы, продуктолог, советы. **Баланс закончился 11.11.2026** |
| `WB_AGENT_MODE`, `WB_AGENT_TOKEN` | wb-dashboard | приём данных от WB-агента |
| `KEEP_AWAKE` | дашборды | самопинг |
| `DASH_PASSWORD` | mpfit-dashboard | basic-auth на финансы ФФ |
| `KIZ_PIN` | mpfit-dashboard | PIN «Упаковки КИЗ» (проверка сейчас **отключена** в коде) |

## VPS в РФ (marketpartners.ru)

- Провайдер RuVDS, Ubuntu. Вход — `ssh root@<IP>` (владелец заходит сам).
- **Caddy** (`frontend/landing/Caddyfile` — эталон конфига) раздаёт:

| Адрес | Куда |
|---|---|
| `marketpartners.ru/` | статика сайта агентства (`/var/www/marketpartners`, исходники `frontend/landing/`) |
| `marketpartners.ru/kiz`, `/api/kiz/*`, `/static/kiz-parse.js`, `/static/bwip-js-min.js` | mpfit-dashboard («Упаковка КИЗ», ТСД) |
| `marketpartners.ru/crpt/*` | `mobile.api.crpt.ru` — срок годности по КИЗ (сервис ЧЗ закрыт для зарубежных IP) |
| `marketpartners.ru/fsa-<секрет>/*` | `pub.fsa.gov.ru` — реестр деклараций (путь секретный, не публиковать) |
| `marketpartners.ru/api/*` | wb-dashboard (заявки с сайта, `/api/landing/*`) |
| `biomed.marketpartners.ru` | wb-dashboard |
| `fk.marketpartners.ru` | fk-dashboard |
| `tl.marketpartners.ru` | tl-dashboard |

- **Туннель до Render.** Caddy ходит в Render через локальный Xray:
  `127.0.0.1:10443` (dokodemo-door) → VPN владельца → `216.24.57.16:443`
  (IP Render, TLS сквозной по SNI). `127.0.0.1:10808` — socks5 для проверок.
- Конфиг Xray собирает `scripts/vps/xray_from_link.py` из ссылки `vless://…`,
  ссылки-подписки или **JSON-экспорта клиента Happ** (там есть настройки xhttp,
  которые в ссылку не помещаются). Файл на сервере: `/root/xray_from_link.py`,
  конфиг: `/usr/local/etc/xray/config.json`.
- **Симптом поломки:** все `*.marketpartners.ru` и `/kiz` отдают **502** через
  ~15 с, а сайт и `/crpt` работают. Значит, умер VPN-выход. Лечение — в
  [`10_runbooks.md`](10_runbooks.md#туннель-до-render-упал-502).
- **Стратегия (предложено владельцу, не сделано):** свой зарубежный VPS
  (~300–600 ₽/мес) с собственным Xray Reality — ключ не протухает; Happ как
  запасной выход; сторож туннеля с уведомлением в Telegram.

## Домашний ПК владельца

- `agent/wb_browser_agent.py` — Playwright через установленный Chrome/Edge,
  опрашивает `/api/tools/niche/pending?token=…` и отдаёт выдачу WB в
  `/niche/ingest`. Нужен, потому что серверные IP WB банит (403/429/498).
- Склад: ноутбук на Linux (Firefox) + термопринтер Xprinter (58×40) —
  станция печати «Упаковки КИЗ»; ТСД на Android — сканер.

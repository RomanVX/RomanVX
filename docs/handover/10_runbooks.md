# Инструкции (runbooks)

Во всех примерах `<cookie>` — cookie-файл сессии владельца дашборда (логин
через интерфейс, затем `curl -c`). Секреты в репозиторий не коммитить.

## Как ассистент получал живые данные кабинетов

Owner-only прокси дашборда:

```bash
curl -s -b <cookie> -H 'content-type: application/json' \
  https://wb-dashboard-6wxf.onrender.com/api/tools/mp/proxy \
  -d '{"mp":"ozon","path":"/v5/product/info/prices","method":"POST","json":{"filter":{"offer_id":["ST-03"],"visibility":"ALL"},"limit":100}}'
```

`mp`: `wb` (контент), `wbc` (common/тарифы), `wbp` (цены), `wbf` (отзывы и
вопросы), `wba` (реклама), `wban` (аналитика), `wbs` (статистика), `wbfin`
(финансы), `ozon`, `ozp` (Ozon Performance), `ym`. Для fk-dashboard — тот же
путь на его хосте. Проверка рекламных методов WB в песочнице:
`POST /api/tools/adv/sandbox`.

## Ответить на отзывы

1. `POST /api/reviews/refresh` — подтянуть свежие отзывы (в фоне 1–2 мин).
2. `GET /api/reviews/data?limit=80000` → `reviews` (поле `answer` пустое =
   без ответа) и `drafts` (статусы `skip`/`gone` — отвечать нельзя).
3. Ответ: `POST /api/reviews/approve?publish=true&id=<id>` с телом `{"text": "..."}`,
   пауза ~1,5 с между запросами.
4. Нельзя ответить: Ozon без текста, Ozon «publish delayed» (на модерации —
   ответит автоответчик позже), удалённые.
5. Подписи: Biomed — «С заботой, команда BioMed Nutrition 🌿»; Миагра — «С
   заботой, команда бренда «Фабрика красоты» ❤️». Стиль: «Здравствуйте!», по
   сути отзыва, без шаблонщины; на негатив — извинение + конкретное решение
   (чат по заказу, замена); на интим-товары — с юмором, но без пошлости.
6. Автоответы (`/api/reviews/auto-pass`, фоновый проход) требуют баланса Anthropic.

## Поменять цены

- **Товары под репрайсером Ozon** (большинство Biomed): только через xlsx-шаблон
  репрайсера (`seller.ozon.ru/app/prices/repricer`), колонка «Целевое значение
  цены на сайте» = цена для покупателя, на Ozon и WB одновременно.
- **Товары вне репрайсера** (ST-03, ST-07, BMN-0013/0028/0035/0036):
  1. Обновить фиксацию сторожа: `POST /api/pricelock` `{items:[{art, wb_nm,
     wb_price, wb_discount, wb_target, oz_price, oz_old}]}` (`GET` — текущая).
  2. Ozon: `/v1/product/import/prices` с `price = min_price` = цена продавца.
     Цену продавца подбирать по витрине: цена с картой / цена продавца (см.
     `02_marketplaces_api.md`), 1–2 итерации.
  3. WB: `POST /api/v2/upload/task` (`wbp`), `price` и `discount` (цена после
     скидки = цель).
  4. `POST /api/pricelock/check?fix=false` — проверить, что расхождений нет.

## Туннель до Render упал (502)

Симптом: `*.marketpartners.ru` и `/kiz` → 502 через ~15 с, сайт работает.

```bash
ssh root@<IP VPS>
# 1. Перезапуск и проверка
systemctl restart xray && sleep 4 && curl -s -m 20 --socks5-hostname 127.0.0.1:10808 \
  -o /dev/null -w 'туннель: %{http_code}\n' https://mpfit-dashboard.onrender.com/kiz
# 2. Если 000 — протух ключ VPN. Экспорт JSON из Happ (или ссылка vless:// / подписка):
curl -fsSL -m 30 -o /root/xray_from_link.py https://raw.githubusercontent.com/romanvx/romanvx/main/scripts/vps/xray_from_link.py
cat > /root/happ.json            # вставить JSON, Enter, Ctrl+D
python3 /root/xray_from_link.py /root/happ.json > /usr/local/etc/xray/config.json \
  && systemctl restart xray && sleep 5 && curl -s -m 25 --socks5-hostname 127.0.0.1:10808 \
  -o /dev/null -w 'туннель: %{http_code}\n' https://mpfit-dashboard.onrender.com/kiz
```

Ожидаемо `туннель: 200`. Если версия Xray старая и не понимает xhttp:
`bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install`.

## Загрузить новую поставку на «Упаковку КИЗ»

На странице `marketpartners.ru/kiz` → блок «Сроки годности из поставки» →
выбрать xlsx поставки → «Загрузить сроки». Нужны колонки ЕАН и «Срок годности»;
количество («Units to pick»/«Кол-во») даёт план для «Сборки по поставке».
API: `POST /api/kiz/expiry/import?name=<поставка>&since=<ISO>` с телом-xlsx.

## Подключить ТСД

1. На компьютере с принтером: `marketpartners.ru/kiz`, режим «Станция печати
   для ТСД», имя станции. Chrome с `--kiosk-printing` (или Firefox с тихой печатью).
2. На ТСД (Chrome): та же страница, режим «ТСД: скан → печать на станции», то же
   имя станции. Сканер ТСД — режим «эмуляция клавиатуры»/«ввод в поле», суффикс
   Enter, передача GS (FNC1) для DataMatrix.

## Обновить токен маркетплейса

Render → сервис (wb-dashboard / fk-dashboard / tl-dashboard) → Environment →
`WB_API_KEY` / `OZON_API_KEY` … → Save (сервис перезапустится). Ключи клиентов
ФФ — в интерфейсе mpfit-dashboard («Продажи клиентов» → ключи).

## Деплой

Пуш в `main` → Render пересобирает. Фронтенд дашборда: при изменении
`frontend/app.js` поднять `?v=` в `frontend/index.html` (cache-bust).
Проверка: `GET /api/health` (rss_mb), для mpfit — `/health`.

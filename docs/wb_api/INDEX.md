# Официальные OpenAPI-спеки WB (dev.wildberries.ru, снято 22.07.2026)

Источник истины при ЛЮБОЙ работе с WB API: сначала смотри метод здесь,
потом пиши код. Поля, лимиты и статусы брать только отсюда.

## analytics.yaml — Аналитика и данные (18 методов)
- `POST /api/analytics/v3/sales-funnel/products` — Статистика карточек товаров за период
- `POST /api/analytics/v3/sales-funnel/products/history` — Статистика карточек товаров по дням
- `POST /api/analytics/v3/sales-funnel/grouped/history` — Статистика групп карточек товаров по дням
- `POST,GET /api/v2/nm-report/downloads` — Получить список отчётов
- `POST /api/v2/nm-report/downloads/retry` — Сгенерировать отчёт повторно
- `GET /api/v2/nm-report/downloads/file/{downloadId}` — Получить отчёт
- `POST /api/v2/search-report/report` — Основная страница
- `POST /api/v2/search-report/table/groups` — Пагинация по группам
- `POST /api/v2/search-report/table/details` — Пагинация по товарам в группе
- `POST /api/v2/search-report/product/search-texts` — Поисковые запросы по товару
- `POST /api/v2/search-report/product/orders` — Заказы и позиции по поисковым запросам товара
- `POST /api/analytics/v1/stocks-report/wb-warehouses` — Остатки на складах WB
- `POST /api/v2/stocks-report/products/groups` — Данные по группам
- `POST /api/v2/stocks-report/products/products` — Данные по товарам
- `POST /api/v2/stocks-report/products/sizes` — Данные по размерам
- `POST /api/v2/stocks-report/offices` — Данные по складам
- `POST /api/analytics/v2/item-rating` — Получить отчёт
- `POST /api/analytics/v1/item-rating` — Получить отчёт

## communications.yaml — Общение с покупателями (21 методов)
- `GET /api/v1/new-feedbacks-questions` — Непросмотренные отзывы и вопросы
- `GET /api/v1/questions/count-unanswered` — Неотвеченные вопросы
- `GET /api/v1/questions/count` — Количество вопросов
- `GET,PATCH /api/v1/questions` — Список вопросов
- `GET /api/v1/question` — Получить вопрос по ID
- `GET /api/v1/feedbacks/count-unanswered` — Необработанные отзывы
- `GET /api/v1/feedbacks/count` — Количество отзывов
- `GET /api/v1/feedbacks` — Список отзывов
- `POST,PATCH /api/v1/feedbacks/answer` — Ответить на отзыв
- `POST /api/v1/feedbacks/order/return` — Возврат товара по ID отзыва
- `GET /api/v1/feedback` — Получить отзыв по ID
- `GET /api/v1/feedbacks/archive` — Список архивных отзывов
- `GET,POST,DELETE /api/feedbacks/v1/pins` — Список закреплённых и откреплённых отзывов
- `GET /api/feedbacks/v1/pins/count` — Количество закреплённых и откреплённых отзывов
- `GET /api/feedbacks/v1/pins/limits` — Лимиты закреплённых отзывов
- `GET /api/v1/seller/chats` — Список чатов
- `GET /api/v1/seller/events` — События чатов
- `POST /api/v1/seller/message` — Отправить сообщение
- `GET /api/v1/seller/download/{id}` — Получить файл из сообщения
- `GET /api/v1/claims` — Заявки покупателей на возврат
- `PATCH /api/v1/claim` — Ответ на заявку покупателя

## finances.yaml — Документы и бухгалтерия (11 методов)
- `GET /api/v1/account/balance` — Получить баланс продавца
- `POST /api/finance/v1/sales-reports/list` — Список отчётов реализации
- `POST /api/finance/v1/sales-reports/detailed/{reportId}` — Детализации к отчётам реализации по ID отчётов
- `POST /api/finance/v1/sales-reports/detailed` — Детализации к отчётам реализации за период
- `POST /api/finance/v1/acquiring/list` — Список отчётов об издержках на приём платежей
- `POST /api/finance/v1/acquiring/detailed/{reportId}` — Детализации к отчётам об издержках на приём платежей по ID отчётов
- `POST /api/finance/v1/acquiring/detailed` — Детализации к отчётам об издержках на приём платежей за период
- `GET /api/v1/documents/categories` — Категории документов
- `GET /api/v1/documents/list` — Список документов
- `GET /api/v1/documents/download` — Получить документ
- `POST /api/v1/documents/download/all` — Получить документы

## general.yaml — Общее (10 методов)
- `GET /ping` — Проверка подключения
- `GET /api/communications/v2/news` — Получение новостей портала продавцов
- `GET /api/v1/seller-info` — Получить информацию о продавце
- `GET /api/common/v1/rating` — Получить рейтинг продавца
- `GET /api/common/v1/subscriptions` — Получить информацию о подписке Джем
- `GET /api/common/v1/tariff-constructor/options` — Получить информацию об опциях Конструктора тарифов
- `POST /api/v1/invite` — Создать приглашение для нового пользователя
- `GET /api/v1/users` — Получить список активных или приглашённых пользователей продавца
- `PUT /api/v1/users/access` — Изменить права доступа пользователей
- `DELETE /api/v1/user` — Удалить пользователя

## in_store_pickup.yaml — Заказы Самовывоз (18 методов)
- `GET /api/v3/click-collect/orders/new` — Получить список новых сборочных заданий
- `POST /api/marketplace/v3/click-collect/orders/status/confirm` — Перевести сборочные задания на сборку
- `POST /api/marketplace/v3/click-collect/orders/status/prepare` — Сообщить, что сборочные задания готовы к выдаче
- `POST /api/v3/click-collect/orders/client` — Информация о покупателе
- `POST /api/v3/click-collect/orders/client/identity` — Проверить, что заказ принадлежит покупателю
- `POST /api/marketplace/v3/click-collect/orders/status/receive` — Сообщить, что заказы приняты покупателями
- `POST /api/marketplace/v3/click-collect/orders/status/reject` — Сообщить об отказе от заказов
- `POST /api/marketplace/v3/click-collect/orders/status/info` — Получить статусы сборочных заданий
- `GET /api/v3/click-collect/orders` — Получить информацию о завершённых сборочных заданиях
- `POST /api/marketplace/v3/click-collect/orders/status/cancel` — Отменить сборочные задания
- `POST /api/marketplace/v3/click-collect/orders/meta/details` — Получить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/click-collect/orders/meta/info` — Получить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/click-collect/orders/meta/delete` — Удалить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/click-collect/orders/meta/sgtin` — Закрепить коды маркировки Честного знака за сборочными заданиями
- `POST /api/marketplace/v3/click-collect/orders/meta/uin` — Закрепить УИН за сборочными заданиями
- `POST /api/marketplace/v3/click-collect/orders/meta/imei` — Закрепить IMEI за сборочными заданиями
- `POST /api/marketplace/v3/click-collect/orders/meta/gtin` — Закрепить GTIN за сборочными заданиями
- `POST /api/marketplace/v3/click-collect/orders/meta/customs-declaration` — Закрепить номера ДТ за сборочными заданиями

## items.yaml — Работа с товарами (45 методов)
- `GET /content/v2/object/parent/all` — Родительские категории товаров
- `GET /content/v2/object/all` — Список предметов
- `GET /content/v2/object/charcs/{subjectId}` — Характеристики предмета
- `GET /content/v2/directory/colors` — Цвет
- `GET /content/v2/directory/kinds` — Пол
- `GET /content/v2/directory/countries` — Страна производства
- `GET /content/v2/directory/seasons` — Сезон
- `GET /content/v2/directory/vat` — Ставка НДС
- `GET /content/v2/directory/tnved` — ТНВЭД-код
- `GET /api/content/v1/brands` — Бренды
- `GET /content/v2/tags` — Список ярлыков
- `POST /content/v2/tag` — Создание ярлыка
- `PATCH,DELETE /content/v2/tag/{id}` — Изменение ярлыка
- `POST /content/v2/tag/nomenclature/link` — Управление ярлыками в карточке товара
- `POST /content/v2/get/cards/list` — Список карточек товаров
- `POST /content/v2/cards/error/list` — Список несозданных карточек товаров с ошибками
- `POST /content/v2/cards/update` — Редактирование карточек товаров
- `POST /content/v2/cards/moveNm` — Объединение и разъединение карточек товаров
- `POST /content/v2/cards/delete/trash` — Перенос карточек товаров в корзину
- `POST /content/v2/cards/recover` — Восстановление карточек товаров из корзины
- `POST /content/v2/get/cards/trash` — Список карточек товаров в корзине
- `GET /content/v2/cards/limits` — Лимиты карточек товаров
- `POST /content/v2/barcodes` — Генерация баркодов
- `POST /content/v2/cards/upload` — Создание карточек товаров
- `POST /content/v2/cards/upload/add` — Создание карточек товаров с присоединением
- `POST /content/v3/media/file` — Загрузить медиафайл
- `POST /content/v3/media/save` — Загрузить медиафайлы по ссылкам
- `POST /api/content/v1/recommendations/list` — Список рекомендаций в карточках товаров
- `POST /api/content/v1/recommendations/set` — Установить рекомендации для товаров
- `POST /api/v2/upload/task` — Установить цены и скидки
- `POST /api/v2/upload/task/size` — Установить цены для размеров
- `POST /api/v2/upload/task/club-discount` — Установить скидки WB Клуба
- `POST /api/discounts-prices/v1/upload/task/b2b/wholesale` — Установить оптовые скидки для B2B-продаж
- `GET /api/v2/history/tasks` — Состояние обработанной загрузки
- `GET /api/v2/history/goods/task` — Детализация обработанной загрузки
- `GET /api/v2/buffer/tasks` — Состояние необработанной загрузки
- `GET /api/v2/buffer/goods/task` — Детализация необработанной загрузки
- `GET,POST /api/v2/list/goods/filter` — Получить товары с ценами
- `GET /api/v2/list/goods/size/nm` — Получить размеры товара с ценами
- `GET /api/v2/quarantine/goods` — Получить товары в карантине
- `PUT,DELETE,POST /api/v3/stocks/{warehouseId}` — Получить остатки товаров
- `GET /api/v3/offices` — Получить список складов WB
- `GET,POST /api/v3/warehouses` — Получить список складов продавца
- `PUT,DELETE /api/v3/warehouses/{warehouseId}` — Обновить склад продавца
- `GET,PUT /api/v3/dbw/warehouses/{warehouseId}/contacts` — Список контактов

## orders_dbs.yaml — Заказы DBS (21 методов)
- `GET /api/v3/dbs/orders/new` — Получить список новых сборочных заданий
- `GET /api/v3/dbs/orders` — Получить информацию о завершенных сборочных заданиях
- `POST /api/v3/dbs/groups/info` — Получить информацию о платной доставке
- `POST /api/v3/dbs/orders/client` — Информация о покупателе
- `POST /api/marketplace/v3/dbs/orders/b2b/info` — Информация о покупателе B2B
- `POST /api/v3/dbs/orders/delivery-date` — Получить дату и время доставки
- `POST /api/marketplace/v3/dbs/orders/status/info` — Получить статусы сборочных заданий
- `POST /api/marketplace/v3/dbs/orders/status/cancel` — Отменить сборочные задания
- `POST /api/marketplace/v3/dbs/orders/status/confirm` — Перевести сборочные задания на сборку
- `POST /api/marketplace/v3/dbs/orders/stickers` — Получить стикеры для сборочных заданий с доставкой в ПВЗ
- `POST /api/marketplace/v3/dbs/orders/status/deliver` — Перевести сборочные задания в доставку
- `POST /api/marketplace/v3/dbs/orders/status/receive` — Сообщить о получении заказов
- `POST /api/marketplace/v3/dbs/orders/status/reject` — Сообщить об отказе от заказов
- `POST /api/marketplace/v3/dbs/orders/meta/details` — Получить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/dbs/orders/meta/info` — Получить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/dbs/orders/meta/delete` — Удалить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/dbs/orders/meta/sgtin` — Закрепить коды маркировки Честного знака за сборочными заданиями
- `POST /api/marketplace/v3/dbs/orders/meta/uin` — Закрепить УИН за сборочными заданиями
- `POST /api/marketplace/v3/dbs/orders/meta/imei` — Закрепить IMEI за сборочными заданиями
- `POST /api/marketplace/v3/dbs/orders/meta/gtin` — Закрепить GTIN за сборочными заданиями
- `POST /api/marketplace/v3/dbs/orders/meta/customs-declaration` — Закрепить номера ДТ за сборочными заданиями

## orders_dbw.yaml — Заказы DBW (17 методов)
- `GET /api/v3/dbw/orders/new` — Получить список новых сборочных заданий
- `GET /api/v3/dbw/orders` — Получить информацию о завершенных сборочных заданиях
- `POST /api/v3/dbw/orders/delivery-date` — Получить дату и время доставки
- `POST /api/marketplace/v3/dbw/orders/client` — Информация о покупателе
- `POST /api/v3/dbw/orders/status` — Получить статусы сборочных заданий
- `PATCH /api/v3/dbw/orders/{orderId}/confirm` — Перевести на сборку
- `POST /api/v3/dbw/orders/stickers` — Получить стикеры сборочных заданий
- `POST /api/marketplace/v3/dbw/orders/status/deliver` — Перевести сборочные задания в доставку
- `POST /api/v3/dbw/orders/courier` — Информация о курьере
- `PATCH /api/v3/dbw/orders/{orderId}/cancel` — Отменить сборочное задание
- `POST /api/marketplace/v3/dbw/orders/meta/details` — Получить идентификаторы маркировки сборочных заданий
- `POST /api/marketplace/v3/dbw/orders/meta/delete` — Удалить идентификаторы маркировки сборочных заданий
- `GET /api/v3/dbw/orders/{orderId}/meta` — Получить идентификаторы маркировки сборочного задания
- `POST /api/marketplace/v3/dbw/orders/meta/sgtin` — Закрепить коды маркировки Честного знака за сборочными заданиями
- `PUT /api/v3/dbw/orders/{orderId}/meta/uin` — Закрепить УИН за сборочным заданием
- `PUT /api/v3/dbw/orders/{orderId}/meta/imei` — Закрепить IMEI за сборочным заданием
- `PUT /api/v3/dbw/orders/{orderId}/meta/gtin` — Закрепить GTIN за сборочным заданием

## orders_fbs.yaml — Заказы FBS (29 методов)
- `GET /api/v3/passes/offices` — Получить список складов, для которых требуется пропуск
- `GET,POST /api/v3/passes` — Получить список пропусков
- `PUT,DELETE /api/v3/passes/{passId}` — Обновить пропуск
- `GET /api/v3/orders/new` — Получить список новых сборочных заданий
- `GET /api/v3/orders` — Получить информацию о сборочных заданиях
- `POST /api/v3/orders/status` — Получить статусы сборочных заданий
- `GET /api/v3/supplies/orders/reshipment` — Получить все сборочные задания для повторной отгрузки
- `PATCH /api/v3/orders/{orderId}/cancel` — Отменить сборочное задание
- `POST /api/v3/orders/stickers` — Получить стикеры сборочных заданий
- `POST /api/marketplace/v3/orders/meta` — Получить идентификаторы маркировки сборочных заданий
- `DELETE /api/v3/orders/{orderId}/meta` — Удалить идентификаторы маркировки сборочного задания
- `PUT /api/v3/orders/{orderId}/meta/sgtin` — Закрепить код маркировки Честного знака за сборочным заданием
- `PUT /api/v3/orders/{orderId}/meta/uin` — Закрепить УИН за сборочным заданием
- `PUT /api/v3/orders/{orderId}/meta/imei` — Закрепить IMEI за сборочным заданием
- `PUT /api/v3/orders/{orderId}/meta/gtin` — Закрепить GTIN за сборочным заданием
- `PUT /api/v3/orders/{orderId}/meta/expiration` — Закрепить за сборочным заданием срок годности товара
- `PUT /api/marketplace/v3/orders/{orderId}/meta/customs-declaration` — Закрепить номер ДТ за сборочным заданием
- `POST /api/v3/orders/stickers/cross-border` — Получить стикеры сборочных заданий трансграничных поставок
- `POST /api/v3/orders/status/history` — История статусов для сборочных заданий трансграничных поставок
- `POST /api/v3/orders/client` — Заказы с информацией по клиенту
- `POST,GET /api/v3/supplies` — Получить список поставок
- `PATCH /api/marketplace/v3/supplies/{supplyId}/orders` — Добавить сборочные задания к поставке
- `GET,DELETE /api/v3/supplies/{supplyId}` — Получить информацию о поставке
- `GET /api/marketplace/v3/supplies/{supplyId}/order-ids` — Получить ID сборочных заданий поставки
- `PATCH /api/v3/supplies/{supplyId}/deliver` — Передать поставку в доставку
- `GET /api/v3/supplies/{supplyId}/barcode` — Получить QR-код поставки
- `GET,POST,DELETE /api/v3/supplies/{supplyId}/trbx` — Получить список грузомест поставки
- `POST /api/v3/supplies/{supplyId}/trbx/stickers` — Получить стикеры грузомест поставки
- `GET /api/marketplace/v3/fbs/orders/archive` — Получить список архивных сборочных заданий

## orders_fbw.yaml — Поставки FBW (7 методов)
- `POST /api/v1/acceptance/options` — Опции приёмки
- `GET /api/v1/warehouses` — Список складов
- `GET /api/v1/transit-tariffs` — Транзитные направления
- `POST /api/v1/supplies` — Список поставок
- `GET /api/v1/supplies/{ID}` — Детали поставки
- `GET /api/v1/supplies/{ID}/goods` — Товары поставки
- `GET /api/v1/supplies/{ID}/package` — Упаковка поставки

## promotion.yaml — Маркетинг и продвижение (38 методов)
- `GET /adv/v1/promotion/count` — Списки кампаний
- `GET /api/advert/v2/adverts` — Информация о кампаниях
- `POST /api/advert/v1/bids/min` — Минимальные ставки для карточек товаров
- `POST /adv/v2/seacat/save-ad` — Создать кампанию
- `GET /adv/v1/supplier/subjects` — Предметы для кампаний
- `POST /adv/v2/supplier/nms` — Карточки товаров для кампаний
- `GET /adv/v0/delete` — Удаление кампании
- `POST /adv/v0/rename` — Переименование кампании
- `GET /adv/v0/start` — Запуск кампании
- `GET /adv/v0/pause` — Пауза кампании
- `GET /adv/v0/stop` — Завершение кампании
- `PUT /adv/v0/auction/placements` — Изменение мест размещения в кампаниях с ручной ставкой
- `PATCH /api/advert/v1/bids` — Изменение ставок в кампаниях
- `GET /adv/v1/balance` — Баланс
- `GET /adv/v1/budget` — Бюджет кампании
- `POST /adv/v1/budget/deposit` — Пополнение бюджета кампании
- `GET /adv/v1/upd` — Получение истории затрат
- `GET /adv/v1/payments` — Получение истории пополнений счёта
- `PATCH /adv/v0/auction/nms` — Изменение списка карточек товаров в кампаниях
- `GET /api/advert/v0/bids/recommendations` — Рекомендуемые ставки для карточек товаров и поисковых кластеров
- `POST /adv/v0/normquery/stats` — Статистика поисковых кластеров
- `POST /adv/v0/normquery/get-bids` — Список ставок поисковых кластеров
- `GET /api/advert/v1/config` — Конфигурационные значения продвижения
- `POST /api/advert/v1/normquery/bids` — Установить ставки для поисковых кластеров в валюте аккаунта продавца
- `POST,DELETE /adv/v0/normquery/bids` — Установить ставки для поисковых кластеров
- `POST /adv/v0/normquery/get-minus` — Список минус-фраз кампаний
- `POST /adv/v0/normquery/set-minus` — Установка и удаление минус-фраз
- `GET /adv/v1/count` — Количество медиакампаний
- `GET /adv/v1/adverts` — Список медиакампаний
- `GET /adv/v1/advert` — Информация о медиакампании
- `GET /adv/v3/fullstats` — Статистика кампаний
- `POST /adv/v1/stats` — Статистика медиакампаний
- `GET /api/v1/calendar/promotions` — Список акций
- `GET /api/v1/calendar/promotions/details` — Детальная информация об акциях
- `GET /api/v1/calendar/promotions/nomenclatures` — Список товаров для участия в акции
- `POST /api/v1/calendar/promotions/upload` — Добавить товар в акцию
- `POST /adv/v0/normquery/list` — Списки активных и неактивных поисковых кластеров
- `POST /adv/v1/normquery/stats` — Статистика по поисковым кластерам с детализацией по дням

## reports.yaml — Отчёты (24 методов)
- `GET /api/v1/supplier/orders` — Заказы
- `GET /api/v1/supplier/sales` — Продажи
- `POST /api/v1/analytics/excise-report` — Получить отчёт
- `GET /api/v1/warehouse_remains` — Создать отчёт
- `GET /api/v1/warehouse_remains/tasks/{task_id}/status` — Проверить статус
- `GET /api/v1/warehouse_remains/tasks/{task_id}/download` — Получить отчёт
- `GET /api/analytics/v1/measurement-penalties` — Удержания за занижение габаритов упаковки
- `GET /api/analytics/v1/warehouse-measurements` — Замеры склада
- `GET /api/analytics/v1/deductions` — Подмены и неверные вложения
- `GET /api/v1/analytics/antifraud-details` — Самовыкупы
- `GET /api/v1/analytics/goods-labeling` — Маркировка товара
- `GET /api/v1/acceptance_report` — Создать отчёт
- `GET /api/v1/acceptance_report/tasks/{task_id}/status` — Проверить статус
- `GET /api/v1/acceptance_report/tasks/{task_id}/download` — Получить отчёт
- `GET /api/v1/paid_storage` — Создать отчёт
- `GET /api/v1/paid_storage/tasks/{task_id}/status` — Проверить статус
- `GET /api/v1/paid_storage/tasks/{task_id}/download` — Получить отчёт
- `GET /api/v1/analytics/region-sale` — Получить отчёт
- `GET /api/v1/analytics/brand-share/brands` — Бренды продавца
- `GET /api/v1/analytics/brand-share/parent-subjects` — Родительские категории бренда
- `GET /api/v1/analytics/brand-share` — Получить отчёт
- `GET /api/v1/analytics/banned-products/blocked` — Получить отчёт
- `GET /api/v1/analytics/banned-products/shadowed` — Скрытые из каталога
- `GET /api/v1/analytics/goods-return` — Получить отчёт

## tariffs.yaml — Тарифы (5 методов)
- `GET /api/v1/tariffs/commission` — Комиссия по категориям товаров
- `GET /api/tariffs/v1/acceptance/coefficients` — Тарифы на поставку
- `GET /api/v1/tariffs/box` — Тарифы для коробов
- `GET /api/v1/tariffs/pallet` — Тарифы для монопаллет
- `GET /api/v1/tariffs/return` — Тарифы на возврат

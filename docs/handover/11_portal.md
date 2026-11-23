# Портал Market Partners

Основной адрес — `https://main.marketpartners.ru`. Портал работает на
`wb-dashboard`, а кнопки открывают нужные кабинеты на их привычных адресах.
МПФИТ открывается по `https://main.marketpartners.ru/mpfit` через отдельный
маршрут Caddy.

Для активации:

1. Убедиться, что `wb-dashboard`, `tl-dashboard`, `fk-dashboard` и
   `mpfit-dashboard` используют один PostgreSQL и имеют права на схему
   `mp_portal`. Она создаётся при первом входе.
2. В Render настроить `PORTAL_ADMIN_LOGIN` и `PORTAL_ADMIN_PASSWORD_HASH`
   только для `wb-dashboard`. Для портала нужен отдельный длинный пароль.
   Формат хеша — шестнадцатеричные соль из 16 байт и результат scrypt из
   32 байт через двоеточие. Пароль вводится с клавиатуры без отображения:

   ```bash
   python3 -c 'import getpass, hashlib, secrets; p = getpass.getpass().encode(); assert len(p) >= 16, "Use at least 16 bytes"; s = secrets.token_bytes(16); print(s.hex() + ":" + hashlib.scrypt(p, salt=s, n=16384, r=8, p=1, dklen=32).hex())'
   ```

3. Развернуть обновление всех четырёх сервисов. Если `fk-dashboard`
   приостановлен, возобновить его перед проверкой кабинета Миагры.
4. Добавить в действующий Caddyfile на RuVDS блок
   `main.marketpartners.ru` из `frontend/landing/Caddyfile`, сохранив
   остальные локальные маршруты. Проверить через `caddy validate` и
   перечитать конфигурацию Caddy.
5. Войти в портал, последовательно открыть пять кабинетов и проверить
   выход. Cookie портала и кабинетов привязаны к конкретным хостам.
   Выход из портала отзывает все выданные им сессии.

Пропуск действует 45 секунд и подходит только одному кабинету. Сессия
действует до 12 часов. Обычный вход клиентов и Basic Auth МПФИТ работают
через прежние адреса. Вход через портал сохраняет ограничения READ_ONLY
конкретного кабинета.

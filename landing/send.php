<?php
/**
 * Обработчик формы заявки — Market Partners (marketpartners.ru).
 *
 * ЗАМЕНИТЕ НА СВОЮ ПОЧТУ адрес в ADMIN_EMAIL — на него приходят заявки.
 * FROM_EMAIL должен быть на домене сайта (требование почтовых серверов reg.ru),
 * иначе письма могут попадать в спам или отклоняться.
 */
const ADMIN_EMAIL = 'hello@marketpartners.ru'; // ← замените на свою почту
const FROM_EMAIL  = 'noreply@marketpartners.ru';

/**
 * Фронтенд шлёт fetch с заголовком X-Requested-With и ждёт JSON.
 * Обычная отправка формы без JS получает редирект обратно на страницу.
 */
$wantsJson =
    (isset($_SERVER['HTTP_X_REQUESTED_WITH']) && strtolower($_SERVER['HTTP_X_REQUESTED_WITH']) === 'fetch')
    || (isset($_SERVER['HTTP_ACCEPT']) && strpos($_SERVER['HTTP_ACCEPT'], 'application/json') !== false);

function respond(bool $ok, int $code, bool $wantsJson): void
{
    if ($wantsJson) {
        http_response_code($code);
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode(['ok' => $ok]);
    } else {
        header('Location: index.html?sent=' . ($ok ? '1' : '0') . '#audit', true, 303);
    }
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    respond(false, 405, $wantsJson);
}

// Honeypot: скрытое поле «website» люди не видят и не заполняют.
// Боту отвечаем «успехом», чтобы не подсказывать, что он раскрыт.
if (!empty($_POST['website'])) {
    respond(true, 200, $wantsJson);
}

/** Обрезает, чистит переводы строк (защита от header injection) и экранирует HTML. */
function field(string $key, int $maxLength): string
{
    $value = isset($_POST[$key]) ? trim((string) $_POST[$key]) : '';
    $value = str_replace(["\r", "\n", "\0"], ' ', $value);
    $value = mb_substr($value, 0, $maxLength, 'UTF-8');
    return htmlspecialchars($value, ENT_QUOTES, 'UTF-8');
}

$name    = field('name', 100);
$contact = field('contact', 150);
$shop    = field('shop', 300);

if ($name === '' || $contact === '' || empty($_POST['agree'])) {
    respond(false, 422, $wantsJson);
}

$subject = 'Заявка на аудит с marketpartners.ru';

$body = "Новая заявка на бесплатный аудит\n\n"
    . 'Имя: ' . $name . "\n"
    . 'Телефон / Telegram: ' . $contact . "\n"
    . 'Магазин на WB: ' . ($shop !== '' ? $shop : '—') . "\n\n"
    . 'Отправлено: ' . date('d.m.Y H:i') . "\n"
    . 'IP: ' . ($_SERVER['REMOTE_ADDR'] ?? '—');

$headers = implode("\r\n", [
    'From: Market Partners <' . FROM_EMAIL . '>',
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
    'Content-Transfer-Encoding: 8bit',
]);

$sent = @mail(
    ADMIN_EMAIL,
    '=?UTF-8?B?' . base64_encode($subject) . '?=',
    $body,
    $headers
);

respond($sent, $sent ? 200 : 500, $wantsJson);

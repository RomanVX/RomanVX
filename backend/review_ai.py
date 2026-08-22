"""Claude-powered analysis of our review answers + reply generation."""
import json
import logging

from anthropic import AsyncAnthropic

from config import ANTHROPIC_API_KEY
import reviews_client as rc

_log = logging.getLogger(__name__)
MODEL = "claude-opus-4-8"
# черновики ответов на отзывы: пробовали Sonnet ради экономии — команда
# забраковала («нейрослоп»), вернули Opus. Экономию даёт кеш system-промта
MODEL_DRAFT = "claude-opus-4-8"

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ─── STYLE ANALYSIS ───────────────────────────────────────────────────────────

async def analyze_style(platform="WB", sample=300) -> dict:
    """Read our past answers and extract a reusable style guide."""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY не задан"}

    pairs = rc.get_answered_pairs(platform=platform, limit=sample)
    if not pairs:
        return {"error": f"Нет сохранённых ответов для {platform}. "
                         "Нажмите «Обновить» — мы начали собирать ответы только сейчас."}

    # Compact examples for the prompt
    examples = "\n\n".join(
        f"[{p['rating']}★] Отзыв: {p['text'] or '(без текста)'}\nНаш ответ: {p['answer']}"
        for p in pairs[:sample]
    )

    system = (
        "Ты — аналитик клиентского сервиса. Тебе дают реальные пары «отзыв покупателя — "
        "ответ продавца» из маркетплейса. Изучи МАНЕРУ ответов продавца и опиши её так, "
        "чтобы по этому описанию можно было генерировать новые ответы в том же стиле. "
        "Отвечай строго в JSON."
    )
    prompt = (
        f"Вот {len(pairs)} пар отзыв→ответ:\n\n{examples}\n\n"
        "Верни JSON со следующими полями:\n"
        "{\n"
        '  "tone": "общий тон (тёплый/официальный/дружелюбный и т.п.)",\n'
        '  "avg_length": "типичная длина ответа",\n'
        '  "greeting": "как обращаются к клиенту (по имени? приветствие?)",\n'
        '  "signature": "как подписываются / упоминание команды/бренда",\n'
        '  "common_phrases": ["частые фразы и обороты"],\n'
        '  "emoji": "используются ли эмодзи и какие",\n'
        '  "structure": "из каких частей обычно состоит ответ",\n'
        '  "by_rating": {"5": "как отвечают на 5★", "low": "как отвечают на негатив"},\n'
        '  "dos": ["что характерно делать"],\n'
        '  "donts": ["чего избегать"],\n'
        '  "system_prompt": "готовый system-prompt для генерации новых ответов в этом стиле"\n'
        "}"
    )

    resp = await _get_client().messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # strip ```json fences if present
    if text.startswith("```"):
        text = text.split("```", 2)[1].lstrip("json").strip()
    try:
        guide = json.loads(text)
    except Exception as e:
        _log.warning("style JSON parse failed: %s", e)
        return {"error": "Не удалось разобрать ответ модели", "raw": text}

    guide["_meta"] = {"platform": platform, "analyzed": len(pairs)}
    return guide


# ─── ПРАВИЛА КОМАНДЫ (редактируются с фронта, «так не пиши — пиши так») ──────

def _rules_init():
    import db
    id_col = ("id SERIAL PRIMARY KEY" if db.IS_PG
              else "id INTEGER PRIMARY KEY AUTOINCREMENT")
    db.execute(f"CREATE TABLE IF NOT EXISTS review_rules "
               f"({id_col}, rule TEXT, added TEXT)")


def get_rules() -> list[dict]:
    import db
    _rules_init()
    return [{"id": r[0], "rule": r[1], "added": r[2]}
            for r in db.fetchall("SELECT id, rule, added FROM review_rules ORDER BY id")]


def add_rule(text: str) -> None:
    import db
    from datetime import datetime
    _rules_init()
    db.execute("INSERT INTO review_rules (rule, added) VALUES (?, ?)",
               (text.strip(), datetime.utcnow().strftime("%Y-%m-%d")))


def delete_rule(rule_id: int) -> None:
    import db
    _rules_init()
    db.execute("DELETE FROM review_rules WHERE id = ?", (rule_id,))


# ─── REPLY GENERATION ─────────────────────────────────────────────────────────

def _build_system(platform: str, n_examples=12) -> str:
    """System prompt seeded with our real answers (few-shot) for style match."""
    pairs = rc.get_answered_pairs(platform=platform, limit=n_examples)
    examples = "\n\n".join(
        f"Отзыв ({p['rating']}★): {p['text'] or '(без текста)'}\nОтвет: {p['answer']}"
        for p in pairs if p["answer"]
    )
    base = (
        "Ты пишешь ответы продавца на отзывы покупателей на маркетплейсе. "
        "Пиши ровно в том же стиле, тоне и длине, что и в примерах ниже — "
        "это реальные ответы нашей команды. Не выдумывай факты о товаре, "
        "будь тёплым и человечным, без шаблонной канцелярщины. "
        "НИКОГДА не пересказывай покупателю его же отзыв: не повторяй его "
        "формулировки и списки свойств («лёгкая, не жирная, быстро "
        "впитывается» → так писать нельзя). Реагируй по сути, как живой "
        "человек: коротко про приятное, конкретно про замечание. Один смайл "
        "максимум, без «Отдельное спасибо за…» и прочих чопорных оборотов. "
        "ОСОБЫЙ СЛУЧАЙ — прислали не тот товар: если покупатель пишет, что "
        "пришёл не тот товар / не та позиция / перепутали заказ или вложение — "
        "обязательно искренне извинись, объясни, что это сбой при комплектации "
        "на складе (не вина покупателя и не пересорт площадки), напиши, что "
        "передали разбор на склад, и предложи оформить возврат или написать в "
        "чат поддержки площадки — вопрос решат. "
        "Верни ТОЛЬКО текст ответа, без кавычек и пояснений."
    )
    rules = []
    try:
        rules = get_rules()
    except Exception as e:
        _log.warning("review rules load: %s", e)
    if rules:
        base += ("\n\nПРАВИЛА КОМАНДЫ — обязательны и важнее примеров:\n"
                 + "\n".join(f"- {r['rule']}" for r in rules))
    if examples:
        base += f"\n\nПРИМЕРЫ НАШИХ ОТВЕТОВ:\n\n{examples}"
    return base


async def generate_reply(review: dict, platform="WB") -> str:
    """Generate a draft reply for one review in our style."""
    if not ANTHROPIC_API_KEY:
        return ""
    system = _build_system(platform)
    user = (
        f"Товар: {review.get('name') or review.get('sku') or '—'}\n"
        f"Оценка: {review.get('rating')}★\n"
        f"Отзыв покупателя: {review.get('text') or '(без текста)'}\n\n"
        "Напиши ответ от лица продавца."
    )
    resp = await _get_client().messages.create(
        model=MODEL_DRAFT,
        max_tokens=600,
        thinking={"type": "adaptive"},
        # батч в 20 черновиков шлёт один и тот же system 20 раз подряд —
        # кешируем, платим за него один раз
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()

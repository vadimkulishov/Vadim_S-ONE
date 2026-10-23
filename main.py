import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import MessageEntityType
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
log = logging.getLogger("s_one_bot")
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
EMOJI_FILE = DATA_DIR / "custom_emojis.json"
BUTTON_LIMIT = 64
# Placeholders for saved custom emoji in post text/captions: [emoji1], [emoji2], :e1:
PLACEHOLDER = re.compile(r"\[(?:emoji)?(\d+)\]|\{(?:emoji)?(\d+)\}|:e(\d+):", re.I)
ADMIN_ID = int(ADMIN_RAW) if ADMIN_RAW.isdigit() else 0


def validate_config():
    missing = [k for k, v in (("BOT_TOKEN", TOKEN), ("ADMIN_ID", ADMIN_RAW), ("CHANNEL_ID", CHANNEL_ID)) if not v]
    if missing:
        raise RuntimeError("Missing Railway variable(s): " + ", ".join(missing))
    try:
        return int(ADMIN_RAW)
    except ValueError as exc:
        raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID") from exc


def is_admin(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


async def require_admin(update):
    if is_admin(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Нет доступа: команда только для администратора.")
    return False


def load_emojis():
    try:
        if EMOJI_FILE.exists():
            data = json.loads(EMOJI_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                result = []
                for x in data:
                    if isinstance(x, dict) and x.get("id"):
                        result.append({"id": str(x["id"]), "sample": str(x.get("sample", "⭐"))})
                    elif isinstance(x, str):  # migration from older ID-only list
                        result.append({"id": x, "sample": "⭐"})
                return result
    except (OSError, json.JSONDecodeError):
        log.exception("Cannot read saved emoji IDs")
    return []


def save_emojis(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EMOJI_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def valid_url(value):
    try:
        p = urlparse(value.strip())
        return p.scheme in ("https", "http") and bool(p.netloc) and "." in p.netloc
    except ValueError:
        return False


def get_buttons(context):
    buttons = context.user_data.get("buttons", [])
    return buttons if isinstance(buttons, list) else []


def make_keyboard(buttons):
    valid = []
    for button in buttons:
        text = str(button.get("text", "")).strip()
        url = str(button.get("url", "")).strip()
        if text and valid_url(url):
            valid.append({
                "text": text,
                "url": url,
                "icon_custom_emoji_id": str(button.get("icon_custom_emoji_id", "")).strip() or None,
            })
    rows = [[InlineKeyboardButton(**button) for button in valid[i:i + 2]]
            for i in range(0, len(valid), 2)]
    return InlineKeyboardMarkup(rows) if rows else None


def resolve_button_icon(message, saved):
    choice = (message.text or "").strip()
    if choice == "-":
        return None, None
    if choice.isdecimal():
        index = int(choice)
        if 1 <= index <= len(saved):
            item = saved[index - 1]
            return item["id"], item.get("sample", "⭐")
        raise ValueError(f"Номер должен быть от 1 до {len(saved)}.")
    found = find_custom_emojis(message)
    if len(found) == 1:
        return found[0]["id"], found[0]["sample"]
    raise ValueError("Отправь одно сообщение с одним кастомным эмодзи, номер из /emojis или «-» без значка.")


def cancel_prompt(context):
    context.user_data.pop("mode", None)
    context.user_data.pop("button_title", None)
    context.user_data.pop("button_url", None)


def u16len(text):
    return len(text.encode("utf-16-le")) // 2


def replace_placeholders(text, source_entities, saved):
    """Turn [emojiN] into custom emoji entities while retaining mappable formatting."""
    by_number = {str(i): item for i, item in enumerate(saved, 1)}
    matches = list(PLACEHOLDER.finditer(text))
    if not matches:
        return text, list(source_entities or []), False
    out, custom, boundary = [], [], {0: 0}
    src_cp = 0
    for match in matches:
        literal = text[src_cp:match.start()]
        dst_start = u16len("".join(out))
        out.append(literal)
        for j in range(src_cp + 1, match.start() + 1):
            boundary[u16len(text[:j])] = dst_start + u16len(text[src_cp:j])
        key = next(g for g in match.groups() if g is not None)
        item = by_number.get(str(int(key)))
        old_start, old_end = u16len(text[:match.start()]), u16len(text[:match.end()])
        if item:
            # Keep the exact fallback glyph sequence captured from Telegram.
            # Many valid emoji consist of multiple Unicode code points (VS16,
            # skin tone, flags, ZWJ sequences); len(sample) != 1 corrupts them.
            sample = item.get("sample") or "⭐"
            out.append(sample)
            new_end = u16len("".join(out))
            custom.append(MessageEntity(type=MessageEntityType.CUSTOM_EMOJI, offset=new_end-u16len(sample),
                                        length=u16len(sample), custom_emoji_id=item["id"]))
            boundary[old_start] = new_end - u16len(sample)
            boundary[old_end] = new_end
        else:
            out.append(match.group(0))
            boundary[old_start] = u16len("".join(out)) - u16len(match.group(0))
            boundary[old_end] = u16len("".join(out))
        src_cp = match.end()
    tail = text[src_cp:]
    out.append(tail)
    # Map suffix boundaries exactly, using the final output prefix length.
    prefix_units = u16len("".join(out[:-1]))
    for j in range(src_cp + 1, len(text) + 1):
        boundary[u16len(text[:j])] = prefix_units + u16len(text[src_cp:j])
    result = "".join(out)
    remapped = []
    for entity in source_entities or []:
        a, b = entity.offset, entity.offset + entity.length
        if a in boundary and b in boundary and boundary[b] > boundary[a]:
            remapped.append(MessageEntity(type=entity.type, offset=boundary[a], length=boundary[b]-boundary[a],
                url=entity.url, user=entity.user, language=entity.language, custom_emoji_id=entity.custom_emoji_id))
        else:
            log.warning("Skipped entity whose range crosses a placeholder boundary")
    remapped.extend(custom)
    remapped.sort(key=lambda e: (e.offset, -e.length))
    return result, remapped, True


def find_custom_emojis(message):
    source = message.text if message.text is not None else (message.caption or "")
    entities = list(message.entities or []) + list(message.caption_entities or [])
    raw = source.encode("utf-16-le")
    found = []
    for entity in entities:
        if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
            try:
                sample = raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")
            except UnicodeDecodeError:
                sample = "⭐"
            # Do not collapse multi-codepoint emoji sequences to a generic star.
            found.append({"id": str(entity.custom_emoji_id), "sample": sample or "⭐"})
    return found


async def start(update, context):
    if update.effective_message:
        await update.effective_message.reply_text(
            "S-ONE — помощник публикаций\n\n/admin — панель администратора\n"
            "/newpost — создать черновик\n/addbutton — добавить кнопку\n/buttons — список кнопок\n"
            "/removebuttons — удалить кнопки\n/preview — предпросмотр\n"
            "/publishdraft — опубликовать черновик с кнопками\n/publish — опубликовать следующее сообщение без кнопок\n"
            "/emoji — сохранить кастомные эмодзи из следующего сообщения\n/emojis — список эмодзи\n/cancel — отмена\n\n"
            "В постах используй [emoji1], [emoji2] и т. д.\n"
            "У кнопки кастомный эмодзи можно поставить как значок перед текстом: выбери сохранённый номер или отправь его отдельно.\n"
            "Для значка в канале Telegram требует, чтобы бот приобрёл дополнительный username через Fragment."
        )


async def admin(update, context):
    if await require_admin(update):
        await start(update, context)


async def newpost(update, context):
    if not await require_admin(update): return
    cancel_prompt(context)
    context.user_data.pop("draft", None)
    context.user_data["buttons"] = []
    context.user_data["mode"] = "newpost"
    await update.effective_message.reply_text("Отправь текст прайса или фото с подписью. Используй [emoji1], [emoji2] для сохранённых эмодзи. Затем добавь кнопки через /addbutton. /cancel — отмена.")


async def addbutton(update, context):
    if not await require_admin(update): return
    if not context.user_data.get("draft"):
        await update.effective_message.reply_text("Сначала создай черновик: /newpost.")
        return
    context.user_data["mode"] = "button_title"
    context.user_data.pop("button_title", None)
    context.user_data.pop("button_url", None)
    await update.effective_message.reply_text(
        "Отправь текст кнопки, например «Связаться». Кастомный эмодзи будет отдельным значком перед текстом."
    )


async def buttons_command(update, context):
    if not await require_admin(update): return
    buttons = get_buttons(context)
    if not context.user_data.get("draft"):
        await update.effective_message.reply_text("Сначала создай черновик: /newpost.")
    elif not buttons:
        await update.effective_message.reply_text("У черновика пока нет кнопок. Используй /addbutton.")
    else:
        await update.effective_message.reply_text(
            "Кнопки текущего прайса:\n" + "\n\n".join(
                f"{i}. {b.get('icon_sample', '')} {b['text']}\n{b['url']}".strip()
                for i, b in enumerate(buttons, 1)
            )
        )


async def removebuttons(update, context):
    if not await require_admin(update): return
    if not context.user_data.get("draft"):
        await update.effective_message.reply_text("Сначала создай черновик: /newpost.")
        return
    context.user_data["buttons"] = []
    await update.effective_message.reply_text("Кнопки удалены. Черновик сохранён.")


async def preview(update, context):
    if not await require_admin(update): return
    if await send_draft(update, context, update.effective_chat.id):
        await update.effective_message.reply_text("Предпросмотр готов. Для публикации: /publishdraft.")


async def send_draft(update, context, target_chat):
    draft = context.user_data.get("draft")
    if not draft:
        await update.effective_message.reply_text("Черновика нет. Используй /newpost.")
        return False
    buttons = make_keyboard(get_buttons(context)) if target_chat == CHANNEL_ID else make_keyboard(get_buttons(context))
    text_value = draft.get("text")
    caption_value = draft.get("caption")
    source = text_value if text_value is not None else caption_value
    if source and PLACEHOLDER.search(source):
        entities_raw = draft.get("entities", []) if text_value is not None else draft.get("caption_entities", [])
        source_entities = [MessageEntity.de_json(item, None) for item in entities_raw]
        converted, converted_entities, _ = replace_placeholders(source, source_entities, load_emojis())
        try:
            if draft.get("photo_file_id"):
                await context.bot.send_photo(chat_id=target_chat, photo=draft["photo_file_id"],
                    caption=converted, caption_entities=converted_entities, reply_markup=buttons)
            elif text_value is not None:
                await context.bot.send_message(chat_id=target_chat, text=converted,
                    entities=converted_entities, reply_markup=buttons)
            else:
                await update.effective_message.reply_text("[emojiN] поддерживаются для текста и фото с подписью.")
                return False
            return True
        except TelegramError:
            log.exception("Draft with emoji placeholders failed")
            await update.effective_message.reply_text(
                "Не удалось опубликовать черновик. Проверь логи Railway, доступ бота к custom emoji "
                "и право использовать значок кнопки в канале (дополнительный username бота через Fragment)."
            )
            return False
    try:
        await context.bot.copy_message(chat_id=target_chat, from_chat_id=draft["chat_id"],
            message_id=draft["message_id"], reply_markup=buttons)
        return True
    except TelegramError:
        log.exception("Draft copy failed")
        await update.effective_message.reply_text(
            "Не удалось опубликовать черновик. Проверь логи Railway, права бота в канале и "
            "доступность значка кнопки (для канала нужен дополнительный username бота через Fragment)."
        )
        return False


async def publishdraft(update, context):
    if not await require_admin(update): return
    if await send_draft(update, context, CHANNEL_ID):
        context.user_data.pop("draft", None)
        context.user_data.pop("buttons", None)
        cancel_prompt(context)
        await update.effective_message.reply_text("Прайс опубликован в канал S-ONE.")


async def publish(update, context):
    if not await require_admin(update): return
    context.user_data["mode"] = "publish"
    await update.effective_message.reply_text("Отправь готовый пост следующим сообщением. Он будет опубликован без кнопок. Плейсхолдеры [emoji1] применяются автоматически. /cancel — отмена.")


async def emoji_command(update, context):
    if not await require_admin(update): return
    context.user_data["mode"] = "emoji"
    await update.effective_message.reply_text("Отправь сообщение со всеми нужными кастомными эмодзи. Сохраню ID в порядке появления. Затем используй их как [emoji1], [emoji2], ... в постах. /cancel — отмена.")


async def emojis_command(update, context):
    if not await require_admin(update): return
    saved = load_emojis()
    if not saved:
        await update.effective_message.reply_text("Эмодзи ещё не сохранены. Используй /emoji.")
        return
    await update.effective_message.reply_text("Сохранённые эмодзи (в постах использовать [emojiN]):\n" + "\n".join(f"{i}. {x.get('sample','⭐')} — ID: {x['id']}" for i, x in enumerate(saved, 1)))


async def cancel(update, context):
    if not await require_admin(update): return
    cancel_prompt(context)
    await update.effective_message.reply_text("Текущий шаг отменён. Черновик и кнопки оставлены.")


async def publish_message(message, context):
    source = message.text if message.text is not None else message.caption
    entities = list(message.entities or []) if message.text is not None else list(message.caption_entities or [])
    if source:
        text_out, entities_out, changed = replace_placeholders(source, entities, load_emojis())
        if changed:
            try:
                if message.photo:
                    await context.bot.send_photo(chat_id=CHANNEL_ID, photo=message.photo[-1].file_id, caption=text_out, caption_entities=entities_out)
                elif message.text is not None:
                    await context.bot.send_message(chat_id=CHANNEL_ID, text=text_out, entities=entities_out)
                else:
                    await message.reply_text("[emojiN] поддерживаются в текстовых постах и фото с подписью. Для другого медиа отправь пост без плейсхолдеров.")
                    return
                await message.reply_text("Пост опубликован. Сохранённые кастомные эмодзи применены.")
                return
            except TelegramError:
                log.exception("Custom emoji post failed")
                await message.reply_text("Не удалось опубликовать пост с эмодзи. Проверь логи Railway.")
                return
    try:
        await context.bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=message.chat_id, message_id=message.message_id)
        await message.reply_text("Пост опубликован в канал.")
    except TelegramError:
        log.exception("Message copy failed")
        await message.reply_text("Не удалось опубликовать. Проверь права бота в канале и логи Railway.")


async def receive_message(update, context):
    message = update.effective_message
    if not message: return
    mode = context.user_data.get("mode")
    if mode and not is_admin(update): return
    if mode == "emoji":
        found, saved = find_custom_emojis(message), load_emojis()
        added = 0
        for item in found:
            if not any(x["id"] == item["id"] for x in saved):
                saved.append(item); added += 1
        save_emojis(saved)
        cancel_prompt(context)
        if found:
            await message.reply_text(f"Найдено: {len(found)}; новых сохранено: {added}; всего: {len(saved)}. Используй [emoji1], [emoji2] и т. д. в постах.")
        else:
            await message.reply_text("Telegram не передал custom_emoji_id. Отправь именно кастомный эмодзи из набора, а не обычный эмодзи или стикер.")
        return
    if mode == "newpost":
        if message.text and message.text.startswith("/"): return
        context.user_data["draft"] = {
            "chat_id": message.chat_id, "message_id": message.message_id,
            "text": message.text,
            "caption": message.caption,
            "entities": [e.to_dict() for e in (message.entities or [])],
            "caption_entities": [e.to_dict() for e in (message.caption_entities or [])],
            "photo_file_id": message.photo[-1].file_id if message.photo else None,
        }
        context.user_data["buttons"] = []
        cancel_prompt(context)
        await message.reply_text("Черновик сохранён.\n/addbutton — добавить кнопку\n/buttons — список кнопок\n/preview — предпросмотр\n/publishdraft — публикация")
        return
    if mode == "button_title":
        title = (message.text or "").strip()
        if not title or title.startswith("/"):
            await message.reply_text("Напиши текст кнопки, например «Связаться»."); return
        if len(title) > BUTTON_LIMIT:
            await message.reply_text(f"Максимум {BUTTON_LIMIT} символов в названии кнопки."); return
        if any(e.type == MessageEntityType.CUSTOM_EMOJI for e in (message.entities or [])):
            await message.reply_text(
                "Вставь кастомный эмодзи как значок на следующем шаге, а не в текст кнопки."
            ); return
        context.user_data["button_title"] = title
        context.user_data["mode"] = "button_url"
        await message.reply_text("Теперь отправь полную ссылку, например https://t.me/your_channel")
        return
    if mode == "button_url":
        url = (message.text or "").strip()
        if not valid_url(url):
            await message.reply_text("Ссылка некорректна. Отправь полную ссылку, начинающуюся с https://"); return
        context.user_data["button_url"] = url
        context.user_data["mode"] = "button_emoji"
        await message.reply_text(
            "Теперь отправь один кастомный эмодзи как значок, номер сохранённого эмодзи из /emojis "
            "или «-» без значка. Для отображения значка в канале бот должен иметь дополнительный username, "
            "приобретённый через Fragment."
        )
        return
    if mode == "button_emoji":
        saved = load_emojis()
        try:
            icon_id, icon_sample = resolve_button_icon(message, saved)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        title = context.user_data.get("button_title", "Открыть")
        button = {"text": title, "url": context.user_data["button_url"]}
        if icon_id:
            button["icon_custom_emoji_id"] = icon_id
            button["icon_sample"] = icon_sample
        buttons = get_buttons(context)
        buttons.append(button)
        context.user_data["buttons"] = buttons
        cancel_prompt(context)
        icon_note = f" со значком {icon_sample}" if icon_id else ""
        await message.reply_text(
            f"Кнопка «{title}»{icon_note} добавлена. Всего: {len(buttons)}.\n"
            "/addbutton — ещё\n/buttons — список\n/removebuttons — удалить все\n"
            "/preview — предпросмотр\n/publishdraft — публикация"
        )
        return
    if mode == "publish":
        if message.text and message.text.startswith("/"): return
        await publish_message(message, context)
        cancel_prompt(context)


async def post_init(app):
    pairs = [("start", "Открыть меню"), ("admin", "Панель администратора"), ("newpost", "Создать черновик"), ("addbutton", "Добавить кнопку"), ("buttons", "Список кнопок"), ("removebuttons", "Удалить кнопки"), ("preview", "Предпросмотр"), ("publishdraft", "Опубликовать прайс"), ("publish", "Пост без кнопок"), ("emoji", "Сохранить кастомные эмодзи"), ("emojis", "Список эмодзи"), ("cancel", "Отменить шаг"), ("help", "Помощь")]
    await app.bot.set_my_commands([BotCommand(a, b) for a, b in pairs])


async def error_handler(update, context):
    log.error("Unhandled error", exc_info=context.error)


def main():
    global ADMIN_ID
    ADMIN_ID = validate_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    handlers = [("start", start), ("admin", admin), ("help", start), ("newpost", newpost), ("addbutton", addbutton), ("buttons", buttons_command), ("removebuttons", removebuttons), ("preview", preview), ("publishdraft", publishdraft), ("publish", publish), ("emoji", emoji_command), ("emojis", emojis_command), ("cancel", cancel)]
    for command, handler in handlers:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, receive_message))
    app.add_error_handler(error_handler)
    log.info("S-ONE bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
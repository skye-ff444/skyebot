import io
import re
import sys
import json
import zipfile
import logging
from typing import List, Tuple

from PIL import Image
from telegram import Update, File as TgFile
from telegram.error import Conflict
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = "8917878183:AAEGsaFkGTIL8p3M45hhBiJrQm2Zo8TAPdM"

STICKER_SIZE   = (512, 512)
TRAY_SIZE      = (96, 96)
MAX_STICKER_KB = 100 * 1024   # 100 KB hard limit per sticker
MAX_TRAY_KB    = 50  * 1024   # 50 KB hard limit for tray icon
MIN_STICKERS   = 3
MAX_STICKERS   = 30

LINK_RE = re.compile(r"t\.me/addstickers/([A-Za-z0-9_]+)", re.IGNORECASE)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mensaje = (
        "¡Hola! Soy *SkyBot* ☁️🤖\n\n"
        "Podés enviarme:\n"
        "• Un *sticker* directamente\n"
        "• El *link* del pack (`t.me/addstickers/...`)\n\n"
        "Descargo todo el pack y te mando uno o varios `.wastickers` listos para importar en WhatsApp."
    )
    await update.message.reply_text(mensaje, parse_mode="Markdown")


def make_512_webp(img_bytes: bytes) -> bytes:
    """
    Convierte cualquier imagen a WebP 512x512 con alpha,
    garantizando que el resultado sea <= 100 KB.
    Reduce calidad iterativamente si hace falta.
    """
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

    # Redimensionar a 512x512 exacto con letterbox transparente
    img.thumbnail(STICKER_SIZE, Image.LANCZOS)
    canvas = Image.new("RGBA", STICKER_SIZE, (0, 0, 0, 0))
    x = (STICKER_SIZE[0] - img.width) // 2
    y = (STICKER_SIZE[1] - img.height) // 2
    canvas.paste(img, (x, y), img)

    # Intentar primero con calidad alta; bajar hasta que entre en 100 KB
    for quality in [90, 80, 70, 60, 50, 40, 30]:
        out = io.BytesIO()
        canvas.save(out, format="WEBP", quality=quality, method=6)
        data = out.getvalue()
        if len(data) <= MAX_STICKER_KB:
            return data

    # Último recurso: escalar imagen a la mitad y reintentarlo
    small = canvas.resize((256, 256), Image.LANCZOS)
    final_canvas = Image.new("RGBA", STICKER_SIZE, (0, 0, 0, 0))
    final_canvas.paste(small, (128, 128), small)
    out = io.BytesIO()
    final_canvas.save(out, format="WEBP", quality=30, method=6)
    return out.getvalue()


def make_tray_png(img_bytes: bytes) -> bytes:
    """Genera el tray icon 96x96 PNG <= 50 KB."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    img.thumbnail(TRAY_SIZE, Image.LANCZOS)
    canvas = Image.new("RGBA", TRAY_SIZE, (0, 0, 0, 0))
    x = (TRAY_SIZE[0] - img.width) // 2
    y = (TRAY_SIZE[1] - img.height) // 2
    canvas.paste(img, (x, y), img)
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    # Si por algún motivo supera 50 KB, guardar como WebP
    if len(data) > MAX_TRAY_KB:
        out2 = io.BytesIO()
        canvas.save(out2, format="WEBP", quality=60)
        data = out2.getvalue()
    return data


async def fetch_static_stickers(set_name: str, context) -> Tuple[List[bytes], int]:
    """Devuelve lista de bytes (webp 512x512 <100KB) y contador de omitidos."""
    try:
        sticker_set = await context.bot.get_sticker_set(set_name)
    except Exception as e:
        raise RuntimeError(f"No se pudo obtener el pack: {e}")

    stickers = []
    skipped = 0
    for s in sticker_set.stickers:
        if getattr(s, "is_animated", False) or getattr(s, "is_video", False):
            skipped += 1
            continue
        try:
            tg_file: TgFile = await context.bot.get_file(s.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            processed = make_512_webp(raw)
            # Verificación final de tamaño (salvaguarda)
            if len(processed) > MAX_STICKER_KB:
                logger.warning("Sticker %s excede 100 KB después de procesar, omitido", s.file_id)
                skipped += 1
                continue
            stickers.append(processed)
        except Exception as ex:
            logger.warning("Error procesando sticker %s: %s", s.file_id, ex)
            skipped += 1
            continue
    return stickers, skipped


def make_wastickers_zip(
    stickers: List[bytes],
    tray_png: bytes,
    title: str = "SkyBotStickers",
    author: str = "SkyBot",
) -> io.BytesIO:
    """
    Estructura del ZIP:
      author.txt
      title.txt
      tray.png        (96x96, <=50KB)
      1.webp … N.webp (512x512, <=100KB cada uno)
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        # ZIP_STORED (sin compresión): WhatsApp lee los webp directamente,
        # la compresión extra del zip no ayuda y puede causar problemas en
        # algunas versiones del parser.
        zf.writestr("author.txt", author)
        zf.writestr("title.txt", title)
        if tray_png:
            zf.writestr("tray.png", tray_png)
        for idx, raw in enumerate(stickers, start=1):
            zf.writestr(f"{idx}.webp", raw)
    buf.seek(0)
    return buf


async def build_wastickers(set_name: str, context, update: Update):
    status_msg = await update.message.reply_text(
        f"⏳ Descargando el pack *{set_name}*...", parse_mode="Markdown"
    )
    try:
        stickers, skipped = await fetch_static_stickers(set_name, context)
    except RuntimeError as e:
        await status_msg.edit_text(f"❌ {e}", parse_mode="Markdown")
        return

    total_static = len(stickers)
    if total_static < MIN_STICKERS:
        msg = f"⚠️ Solo hay {total_static} sticker(s) estático(s)"
        if skipped:
            msg += f" ({skipped} animados/video omitidos)"
        msg += f". WhatsApp necesita al menos {MIN_STICKERS}."
        await status_msg.edit_text(msg)
        return

    parts = [stickers[i:i + MAX_STICKERS] for i in range(0, total_static, MAX_STICKERS)]
    sent_parts = 0

    for part_idx, part in enumerate(parts, start=1):
        tray_png = None
        try:
            # Usar el primer sticker como tray
            raw_first = part[0]
            tray_png = make_tray_png(raw_first)
        except Exception:
            tray_png = None

        part_title = set_name if len(parts) == 1 else f"{set_name} {part_idx}"
        zip_buf = make_wastickers_zip(part, tray_png, title=part_title, author="SkyBot")
        filename = f"{set_name.replace(' ', '_')}"
        if len(parts) > 1:
            filename += f"_part{part_idx}"
        filename += ".wastickers"

        caption = f"✅ Pack *{part_title}* — {len(part)} sticker(s)"
        if skipped:
            caption += f" ({skipped} animados/video omitidos)"
        if len(parts) > 1:
            caption += f" — parte {part_idx}/{len(parts)}"
        caption += "\n\nDescargalo y abrilo con *Sticker Maker* en tu teléfono."

        try:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=zip_buf,
                filename=filename,
                caption=caption,
                parse_mode="Markdown",
            )
            sent_parts += 1
        except Exception as e:
            logger.exception("Error enviando .wastickers parte %d", part_idx)
            await status_msg.edit_text(f"❌ Error enviando la parte {part_idx}: {e}")
            return

    await status_msg.edit_text(
        f"✅ Listo. {sent_parts} archivo(s) enviado(s).", parse_mode="Markdown"
    )


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker = update.message.sticker
    set_name = getattr(sticker, "set_name", None)
    if not set_name:
        await update.message.reply_text("⚠️ Este sticker no pertenece a un pack.")
        return
    await build_wastickers(set_name, context, update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = LINK_RE.search(text)
    if not match:
        await update.message.reply_text(
            "No reconocí ningún link de pack.\nMandame un sticker o un link:\n`https://t.me/addstickers/NombreDelPack`",
            parse_mode="Markdown",
        )
        return
    await build_wastickers(match.group(1), context, update)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logger.error("❌ Otra instancia del bot ya está corriendo. Cerrando esta.")
        sys.exit(1)
    logger.exception("Error inesperado: %s", context.error)


def main():
    request = HTTPXRequest(
        read_timeout=120,
        write_timeout=120,
        connect_timeout=30,
        pool_timeout=60,
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    logger.info("SkyBot está en línea y funcionando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
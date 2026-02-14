import os
import io
import json
from pathlib import Path
from datetime import datetime, time
import numpy as np
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google.cloud import vision
from google.oauth2 import service_account

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GSA_JSON = os.environ["GOOGLE_APPLICATION_CREDENTIALS_JSON"]
TIMEZONE = os.environ.get("TRADING_TIMEZONE", "America/New_York")

SAVE_DIR = Path("uploads")
SAVE_DIR.mkdir(exist_ok=True)

gsa = json.loads(GSA_JSON)
vision_client = vision.ImageAnnotatorClient(credentials=service_account.Credentials.from_service_account_info(gsa))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("SniperBot online. Send screenshots with captions 4H/1H/30M/15M/5M.")

def ocr_image_bytes(image_bytes: bytes) -> str:
    image = vision.Image(content=image_bytes)
    resp = vision_client.text_detection(image=image)
    return resp.full_text_annotation.text if resp.full_text_annotation else ""

def extract_prices_from_text(text: str) -> dict:
    import re
    nums = re.findall(r"\d{3,5}\.\d{1,4}", text)
    return {"numbers": nums, "raw": text}

def compute_sl_tp_size(entry_price: float, atr: float, side: str, balance=10000.0):
    ATR_SL_K = 1.5
    sl_distance = ATR_SL_K * atr
    tp_distance = 4.0 * sl_distance
    sl = entry_price - sl_distance if side == "LONG" else entry_price + sl_distance
    tp = entry_price + tp_distance if side == "LONG" else entry_price - tp_distance
    risk_usd = balance * 0.01
    position_size = max(0.01, round(risk_usd / (sl_distance * 100), 2))
    return {"SL": round(sl, 3), "TP": round(tp, 3), "lots": position_size, "RR": 4.0}

def analyze_screenshots(parsed):
    try:
        for tf in ("5M","15M","30M","1H","4H"):
            v = parsed.get(tf, {}).get("numbers")
            if v:
                price = float(v[-1]); break
        else:
            return {"error": "No price found."}
        all_nums = []
        for d in parsed.values():
            for n in d.get("numbers", []):
                try: all_nums.append(float(n))
                except: pass
        atr = float(np.std(all_nums)) if len(all_nums) >= 2 else 1.0
        m15 = parsed.get("15M", {}).get("numbers", [])
        if m15:
            ema_like = float(np.mean(list(map(float, m15[-5:]))))
            side = "LONG" if float(m15[-1]) > ema_like else "SHORT"
        else:
            side = "LONG"
        trade = compute_sl_tp_size(entry_price=price, atr=atr, side=side)
        return {"side": side, "entry": price, **trade}
    except Exception as e:
        return {"error": str(e)}

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    photo = msg.photo[-1] if msg.photo else None
    if not photo:
        await msg.reply_text("No image found.")
        return
    f = await photo.get_file()
    fname = SAVE_DIR / f"{msg.from_user.id}_{msg.message_id}.jpg"
    await f.download_to_drive(str(fname))
    with open(fname, "rb") as fh:
        content = fh.read()
    text = ocr_image_bytes(content)
    parsed = extract_prices_from_text(text)
    tf_label = (msg.caption or "").strip().upper()
    if tf_label not in ("5M","15M","30M","1H","4H"):
        await msg.reply_text("Saved. Please resend with caption one of: 5M,15M,30M,1H,4H.")
        return
    store_path = SAVE_DIR / f"parsed_{msg.from_user.id}.json"
    if store_path.exists():
        data = json.loads(store_path.read_text())
    else:
        data = {}
    data[tf_label] = parsed
    store_path.write_text(json.dumps(data))
    await msg.reply_text(f"{tf_label} parsed. When done send /analyze")

async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    store_path = SAVE_DIR / f"parsed_{uid}.json"
    if not store_path.exists():
        await update.message.reply_text("No parsed screenshots found. Upload images with captions first.")
        return
    parsed = json.loads(store_path.read_text())
    result = analyze_screenshots(parsed)
    if "error" in result:
        await update.message.reply_text("Analysis error: " + result["error"])
        return
    msg = (f"PAIR: XAUUSD\nSIDE: {result['side']}\nENTRY: {result['entry']}\nSL: {result['SL']}\nTP: {result['TP']}\nLOTS: {result['lots']}\nR:R: {result['RR']}:1")
    await update.message.reply_text(msg)
    store_path.unlink(missing_ok=True)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    print("Bot started")
    app.run_polling()

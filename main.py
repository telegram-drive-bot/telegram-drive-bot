import os
import logging
import tempfile
from datetime import datetime
from typing import Dict, Optional

import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# تحميل المتغيرات البيئية
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8915424151:AAHlryLYb_32jkPSZpaooi-HYp6eqEGjs1M"
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE") or "/storage/emulated/0/telegram_drive_bot/service_account.json"
MAIN_FOLDER_ID = os.getenv("MAIN_FOLDER_ID") or "1eIlbD9JQTIXSIuPjHJu87rdsb1pJBx6G"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

WAITING_CUSTOMER_NAME, WAITING_UPLOAD, WAITING_FILE_SELECTION = range(3)

# تهيئة Google Drive
SCOPES = ["https://www.googleapis.com/auth/drive"]
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build("drive", "v3", credentials=credentials)

# --- دوال Google Drive ---
def create_folder(name: str, parent_id: str) -> Optional[str]:
    try:
        query = f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        response = drive_service.files().list(q=query, spaces="drive", fields="files(id)").execute()
        if response.get("files"):
            return response["files"][0]["id"]
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = drive_service.files().create(body=file_metadata, fields="id").execute()
        return folder.get("id")
    except HttpError as e:
        logger.error(f"Drive error creating folder: {e}")
        return None

def upload_file(file_path: str, folder_id: str) -> Optional[str]:
    try:
        file_metadata = {"name": os.path.basename(file_path), "parents": [folder_id]}
        media = MediaFileUpload(file_path, resumable=True)
        file = drive_service.files().create(body=file_metadata, media_body=media, fields="id").execute()
        return file.get("id")
    except HttpError as e:
        logger.error(f"Drive error uploading file: {e}")
        return None

def make_file_public(file_id: str) -> Optional[str]:
    try:
        permission = {"type": "anyone", "role": "reader"}
        drive_service.permissions().create(fileId=file_id, body=permission).execute()
        return f"https://drive.google.com/file/d/{file_id}/view"
    except HttpError as e:
        logger.error(f"Error making file public: {e}")
        return None

def list_files_in_folder(folder_id: str) -> list:
    try:
        query = f"'{folder_id}' in parents and trashed=false"
        results = drive_service.files().list(q=query, fields="files(id, name, mimeType, size)").execute()
        return results.get("files", [])
    except HttpError as e:
        logger.error(f"Error listing files: {e}")
        return []

def delete_file(file_id: str) -> bool:
    try:
        drive_service.files().delete(fileId=file_id).execute()
        return True
    except HttpError as e:
        logger.error(f"Error deleting file: {e}")
        return False

# --- دالة توليد QR Code ---
def generate_qr(data: str) -> Optional[str]:
    """توليد صورة QR من نص وإرجاع مسار الملف المؤقت"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        # حفظ الصورة في ملف مؤقت
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        img.save(temp_file.name)
        return temp_file.name
    except Exception as e:
        logger.error(f"QR generation error: {e}")
        return None

# --- دوال البوت ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    await update.message.reply_html(
        f"مرحباً {user.mention_html()}!\n\n"
        "أرسل اسم العميل لإنشاء مجلد خاص به، ثم أرسل الملفات لرفعها.\n"
        "يمكنك إنشاء QR Code لأي ملف بعد رفعه.",
        reply_markup=main_menu_keyboard()
    )
    return WAITING_CUSTOMER_NAME

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية. استخدم /start لبدء جديد.")
    return ConversationHandler.END

async def set_customer_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("الرجاء إدخال اسم صالح.")
        return WAITING_CUSTOMER_NAME

    folder_id = create_folder(name, MAIN_FOLDER_ID)
    if not folder_id:
        await update.message.reply_text("حدث خطأ أثناء إنشاء المجلد.")
        return WAITING_CUSTOMER_NAME

    context.user_data["customer_name"] = name
    context.user_data["folder_id"] = folder_id
    await update.message.reply_html(
        f"✅ تم إنشاء مجلد للعميل <b>{name}</b>.\n"
        "أرسل الملفات الآن أو استخدم الأزرار:",
        reply_markup=action_keyboard()
    )
    return WAITING_UPLOAD

async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder_id = context.user_data.get("folder_id")
    if not folder_id:
        await update.message.reply_text("لم يتم تحديد مجلد. استخدم /start.")
        return ConversationHandler.END

    # تحديد نوع الملف
    if update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name
    elif update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
        file_name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    elif update.message.video:
        file_obj = update.message.video
        file_name = file_obj.file_name or f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    elif update.message.audio:
        file_obj = update.message.audio
        file_name = file_obj.file_name or f"audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
    elif update.message.voice:
        file_obj = update.message.voice
        file_name = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ogg"
    else:
        await update.message.reply_text("نوع الملف غير مدعوم.")
        return WAITING_UPLOAD

    progress_msg = await update.message.reply_text(f"جاري رفع {file_name} ...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file_name)[1]) as tmp_file:
        temp_path = tmp_file.name

    try:
        file_telegram = await file_obj.get_file()
        await file_telegram.download_to_drive(temp_path)
        file_id = upload_file(temp_path, folder_id)
        if not file_id:
            await progress_msg.edit_text("❌ فشل رفع الملف.")
            return WAITING_UPLOAD

        link = make_file_public(file_id)
        # حفظ معرف الملف ورابطه في user_data لاستخدامه لاحقاً (آخر ملف مرفوع)
        context.user_data["last_file_id"] = file_id
        context.user_data["last_file_link"] = link
        context.user_data["last_file_name"] = file_name

        link_text = f"\n🔗 الرابط: {link}" if link else "\n⚠️ لم نتمكن من جعل الملف عاماً."
        await progress_msg.edit_text(
            f"✅ تم رفع {file_name} بنجاح.\nمعرف الملف: `{file_id}`{link_text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await progress_msg.edit_text("حدث خطأ أثناء المعالجة.")
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    return WAITING_UPLOAD

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder_id = context.user_data.get("folder_id")
    if not folder_id:
        await update.message.reply_text("لا يوجد مجلد نشط.")
        return

    files = list_files_in_folder(folder_id)
    if not files:
        await update.message.reply_text("لا توجد ملفات.")
        return

    # إنشاء أزرار اختيار لكل ملف (لإنشاء QR Code لملف معين)
    keyboard = []
    for f in files:
        keyboard.append([InlineKeyboardButton(f"📄 {f['name']}", callback_data=f"qr_file_{f['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_menu")])
    await update.message.reply_text(
        "📁 اختَر ملفاً لإنشاء QR Code:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_FILE_SELECTION

async def create_qr_for_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, file_name: str = None):
    """دالة مساعدة لإنشاء QR لملف معين"""
    # الحصول على الرابط العام (إذا لم يكن عاماً نجعلها عامة)
    link = make_file_public(file_id)
    if not link:
        await update.callback_query.edit_message_text("فشل جعل الملف عاماً.")
        return

    qr_path = generate_qr(link)
    if not qr_path:
        await update.callback_query.edit_message_text("فشل إنشاء QR Code.")
        return

    # إرسال صورة QR
    caption = f"📱 باركود (QR Code) للملف: {file_name or file_id}\n🔗 الرابط: {link}"
    with open(qr_path, "rb") as qr_img:
        await update.callback_query.message.reply_photo(photo=qr_img, caption=caption)
    os.unlink(qr_path)
    await update.callback_query.answer("تم إنشاء QR Code بنجاح")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "new_customer":
        await query.edit_message_text("أرسل اسم العميل الجديد:")
        return WAITING_CUSTOMER_NAME
    elif data == "help":
        await query.edit_message_text("/start - بدء\n/cancel - إلغاء\n/list - عرض الملفات\n/link - رابط آخر ملف\n/qr - QR لآخر ملف\n/change - تغيير العميل")
        return ConversationHandler.END
    elif data == "list_files":
        folder_id = context.user_data.get("folder_id")
        if not folder_id:
            await query.edit_message_text("لا يوجد مجلد نشط.")
            return WAITING_UPLOAD
        files = list_files_in_folder(folder_id)
        if not files:
            await query.edit_message_text("لا توجد ملفات.")
            return WAITING_UPLOAD
        keyboard = []
        for f in files:
            keyboard.append([InlineKeyboardButton(f"📄 {f['name']}", callback_data=f"qr_file_{f['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_menu")])
        await query.edit_message_text("اختر ملفاً لإنشاء QR Code:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WAITING_FILE_SELECTION
    elif data == "get_link":
        last_link = context.user_data.get("last_file_link")
        last_name = context.user_data.get("last_file_name", "آخر ملف")
        if last_link:
            await query.edit_message_text(f"🔗 رابط {last_name}:\n{last_link}")
        else:
            await query.edit_message_text("لا يوجد ملف مرفوع حتى الآن.")
        return WAITING_UPLOAD
    elif data == "qr_last":
        last_link = context.user_data.get("last_file_link")
        last_name = context.user_data.get("last_file_name")
        last_id = context.user_data.get("last_file_id")
        if not last_link:
            await query.edit_message_text("لا يوجد ملف مرفوع بعد.")
            return WAITING_UPLOAD
        await create_qr_for_file(update, context, last_id, last_name)
        await query.delete_message()
        return WAITING_UPLOAD
    elif data == "change_customer":
        await query.edit_message_text("أرسل اسم العميل الجديد:")
        return WAITING_CUSTOMER_NAME
    elif data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("تم الإلغاء. استخدم /start للبدء.")
        return ConversationHandler.END
    elif data == "back_to_menu":
        await query.edit_message_text("اختر إجراء:", reply_markup=action_keyboard())
        return WAITING_UPLOAD
    elif data.startswith("qr_file_"):
        file_id = data.split("_")[2]
        # البحث عن اسم الملف (يمكن حفظه لكن سنستعلم من Drive)
        files = list_files_in_folder(context.user_data.get("folder_id"))
        file_name = None
        for f in files:
            if f["id"] == file_id:
                file_name = f["name"]
                break
        await create_qr_for_file(update, context, file_id, file_name)
        await query.delete_message()
        return WAITING_UPLOAD
    return WAITING_UPLOAD

async def qr_last_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الأمر /qr لإنشاء QR لآخر ملف تم رفعه"""
    last_link = context.user_data.get("last_file_link")
    last_id = context.user_data.get("last_file_id")
    last_name = context.user_data.get("last_file_name")
    if not last_link:
        await update.message.reply_text("لا يوجد ملف مرفوع بعد.")
        return
    qr_path = generate_qr(last_link)
    if not qr_path:
        await update.message.reply_text("فشل إنشاء QR Code.")
        return
    with open(qr_path, "rb") as qr_img:
        await update.message.reply_photo(photo=qr_img, caption=f"QR Code لـ {last_name}\n{last_link}")
    os.unlink(qr_path)

async def get_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_link = context.user_data.get("last_file_link")
    if last_link:
        await update.message.reply_text(last_link)
    else:
        await update.message.reply_text("لا يوجد ملف مرفوع.")

async def change_customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أرسل اسم العميل الجديد:")
    return WAITING_CUSTOMER_NAME

# --- لوحات الأزرار ---
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("بدء عميل جديد", callback_data="new_customer")],
        [InlineKeyboardButton("المساعدة", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)

def action_keyboard():
    keyboard = [
        [InlineKeyboardButton("📋 عرض الملفات", callback_data="list_files")],
        [InlineKeyboardButton("🔗 رابط آخر ملف", callback_data="get_link")],
        [InlineKeyboardButton("📱 QR لآخر ملف", callback_data="qr_last")],
        [InlineKeyboardButton("🔄 تغيير العميل", callback_data="change_customer")],
        [InlineKeyboardButton("❌ إلغاء الجلسة", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)

# --- تشغيل البوت ---
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_CUSTOMER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_customer_name),
                CallbackQueryHandler(button_callback, pattern="^(new_customer|help)$"),
            ],
            WAITING_UPLOAD: [
                MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE, handle_upload),
                CommandHandler("list", list_files),
                CommandHandler("link", get_link_command),
                CommandHandler("qr", qr_last_command),
                CommandHandler("change", change_customer_command),
                CommandHandler("cancel", cancel),
                CallbackQueryHandler(button_callback, pattern="^(list_files|get_link|qr_last|change_customer|cancel|back_to_menu|qr_file_.*)$"),
            ],
            WAITING_FILE_SELECTION: [
               CallbackQueryHandler(button_callback, pattern="^(qr_file_.*|back_to_menu)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("help", lambda u,c: u.message.reply_text("استخدم /start"))],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("qr", qr_last_command))
    app.add_handler(CommandHandler("link", get_link_command))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("change", change_customer_command))

    logger.info("البوت يعمل الآن مع ميزة QR Code...")
    app.run_polling()

if __name__ == "__main__":
    main()

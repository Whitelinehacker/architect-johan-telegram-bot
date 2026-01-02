"""
Premium Telegram Moderation Bot
Deployment: Render.com as Background Worker
Database: MongoDB Atlas
Framework: python-telegram-bot v21.7
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from telegram import Update, ChatPermissions, ChatMember
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError, ConnectionFailure
from bson.objectid import ObjectId

# ====================== CONFIGURATION ======================
# Environment variables (set in Render.com)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
MONGODB_URI = os.getenv("MONGODB_URI")

# Bot constants
GROUP_ID = None  # Will be set when bot added to group
WARNING_LIMIT = 3
MUTE_DURATION = 24  # hours
REMINDER_INTERVAL = 6  # hours

# MongoDB collections
USERS_COLLECTION = "users"
WARNINGS_COLLECTION = "warnings"
SUBSCRIPTIONS_COLLECTION = "subscriptions"

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== DATABASE MODELS ======================
@dataclass
class UserData:
    """User data model"""
    user_id: int
    username: str
    first_name: str
    last_name: str
    join_date: datetime
    warning_count: int
    is_muted: bool
    is_banned: bool
    subscription_expiry: Optional[datetime]
    
    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            user_id=data.get("user_id"),
            username=data.get("username", ""),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            join_date=data.get("join_date", datetime.utcnow()),
            warning_count=data.get("warning_count", 0),
            is_muted=data.get("is_muted", False),
            is_banned=data.get("is_banned", False),
            subscription_expiry=data.get("subscription_expiry")
        )

class DatabaseManager:
    """MongoDB Atlas connection and operations manager"""
    
    def __init__(self, uri: str):
        self.client = None
        self.db = None
        self.connect(uri)
        self.setup_indexes()
    
    def connect(self, uri: str):
        """Establish MongoDB connection with retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.client = MongoClient(
                    uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=5000,
                    retryWrites=True,
                    w="majority"
                )
                # Test connection
                self.client.admin.command('ping')
                self.db = self.client.get_database("premium_group_bot")
                logger.info("✅ MongoDB Atlas connection established")
                return
            except ConnectionFailure as e:
                logger.error(f"❌ MongoDB connection failed (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise
                asyncio.sleep(2 ** attempt)  # Exponential backoff
    
    def setup_indexes(self):
        """Create necessary indexes for performance"""
        try:
            # Users collection indexes
            self.db[USERS_COLLECTION].create_index([("user_id", ASCENDING)], unique=True)
            self.db[USERS_COLLECTION].create_index([("subscription_expiry", ASCENDING)])
            
            # Warnings collection indexes
            self.db[WARNINGS_COLLECTION].create_index([("user_id", ASCENDING)])
            self.db[WARNINGS_COLLECTION].create_index([("timestamp", DESCENDING)])
            
            # Subscriptions collection indexes
            self.db[SUBSCRIPTIONS_COLLECTION].create_index([("user_id", ASCENDING)], unique=True)
            self.db[SUBSCRIPTIONS_COLLECTION].create_index([("expiry_date", ASCENDING)])
            
            logger.info("✅ Database indexes created")
        except Exception as e:
            logger.error(f"❌ Failed to create indexes: {e}")
    
    # ========== USER OPERATIONS ==========
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        """Retrieve user from database"""
        try:
            return self.db[USERS_COLLECTION].find_one({"user_id": user_id})
        except PyMongoError as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None
    
    def create_user(self, user_id: int, username: str, first_name: str, last_name: str = ""):
        """Create new user record"""
        try:
            user_data = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "join_date": datetime.utcnow(),
                "warning_count": 0,
                "is_muted": False,
                "is_banned": False,
                "subscription_expiry": None,
                "last_reminder": None
            }
            self.db[USERS_COLLECTION].insert_one(user_data)
            logger.info(f"✅ Created user record for {user_id}")
        except PyMongoError as e:
            logger.error(f"Error creating user {user_id}: {e}")
    
    def update_user_warning(self, user_id: int, increment: bool = True):
        """Update user warning count"""
        try:
            update_op = {"$inc": {"warning_count": 1 if increment else -1}}
            if increment:
                update_op["$set"] = {"last_warning": datetime.utcnow()}
            
            result = self.db[USERS_COLLECTION].update_one(
                {"user_id": user_id},
                update_op
            )
            return result.modified_count > 0
        except PyMongoError as e:
            logger.error(f"Error updating warnings for {user_id}: {e}")
            return False
    
    def reset_user_warnings(self, user_id: int):
        """Reset user warnings to zero"""
        try:
            self.db[USERS_COLLECTION].update_one(
                {"user_id": user_id},
                {"$set": {"warning_count": 0}}
            )
        except PyMongoError as e:
            logger.error(f"Error resetting warnings for {user_id}: {e}")
    
    def set_user_muted(self, user_id: int, muted: bool):
        """Update user mute status"""
        try:
            self.db[USERS_COLLECTION].update_one(
                {"user_id": user_id},
                {"$set": {"is_muted": muted}}
            )
        except PyMongoError as e:
            logger.error(f"Error setting mute status for {user_id}: {e}")
    
    def set_user_banned(self, user_id: int, banned: bool):
        """Update user ban status"""
        try:
            self.db[USERS_COLLECTION].update_one(
                {"user_id": user_id},
                {"$set": {"is_banned": banned}}
            )
        except PyMongoError as e:
            logger.error(f"Error setting ban status for {user_id}: {e}")
    
    # ========== WARNING OPERATIONS ==========
    
    def add_warning(self, user_id: int, reason: str, admin_id: int = 0):
        """Record a warning in database"""
        try:
            warning = {
                "user_id": user_id,
                "reason": reason,
                "admin_id": admin_id,
                "timestamp": datetime.utcnow(),
                "resolved": False
            }
            self.db[WARNINGS_COLLECTION].insert_one(warning)
        except PyMongoError as e:
            logger.error(f"Error adding warning for {user_id}: {e}")
    
    def get_user_warnings(self, user_id: int, limit: int = 10) -> List[Dict]:
        """Get recent warnings for a user"""
        try:
            cursor = self.db[WARNINGS_COLLECTION].find(
                {"user_id": user_id}
            ).sort("timestamp", DESCENDING).limit(limit)
            return list(cursor)
        except PyMongoError as e:
            logger.error(f"Error getting warnings for {user_id}: {e}")
            return []
    
    # ========== SUBSCRIPTION OPERATIONS ==========
    
    def add_subscription(self, user_id: int, days: int):
        """Add or extend user subscription"""
        try:
            expiry_date = datetime.utcnow() + timedelta(days=days)
            
            self.db[SUBSCRIPTIONS_COLLECTION].update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "expiry_date": expiry_date,
                        "updated_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            # Also update user record
            self.db[USERS_COLLECTION].update_one(
                {"user_id": user_id},
                {"$set": {"subscription_expiry": expiry_date}}
            )
            
            logger.info(f"✅ Subscription added for {user_id}: {days} days")
        except PyMongoError as e:
            logger.error(f"Error adding subscription for {user_id}: {e}")
    
    def get_expired_subscriptions(self) -> List[int]:
        """Get list of users with expired subscriptions"""
        try:
            expired_users = self.db[SUBSCRIPTIONS_COLLECTION].find({
                "expiry_date": {"$lt": datetime.utcnow()}
            })
            return [user["user_id"] for user in expired_users]
        except PyMongoError as e:
            logger.error(f"Error getting expired subscriptions: {e}")
            return []
    
    def remove_subscription(self, user_id: int):
        """Remove user subscription"""
        try:
            self.db[SUBSCRIPTIONS_COLLECTION].delete_one({"user_id": user_id})
            self.db[USERS_COLLECTION].update_one(
                {"user_id": user_id},
                {"$set": {"subscription_expiry": None}}
            )
        except PyMongoError as e:
            logger.error(f"Error removing subscription for {user_id}: {e}")

# ====================== BOT HANDLERS ======================

class PremiumModerationBot:
    """Main bot class handling all moderation features"""
    
    def __init__(self):
        self.db = DatabaseManager(MONGODB_URI)
        self.app = None
        self.group_id = None
        
        # Premium rules message
        self.RULES_MESSAGE = """🚫 *PREMIUM GROUP RULES* 🚫

1. *NO COPY* - Do not copy content
2. *NO FORWARD* - Forwarding is prohibited
3. *NO SHARE* - Do not share with outsiders
4. *NO SCREENSHOTS* - Screenshots are forbidden
5. *NO SCREEN RECORDING* - Recording is strictly prohibited

⚠️ *VIOLATION POLICY* ⚠️
• 1st violation → Warning
• 2nd violation → 24-hour mute
• 3rd violation → Permanent ban
• Serious violations → Immediate removal without refund

📢 *REMINDER*: Screenshot/screen recording is strictly prohibited. 
Violators will be removed immediately."""
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler (admin only)"""
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ This command is for admins only.")
            return
        
        await update.message.reply_text(
            "🤖 *Premium Moderation Bot Active*\n\n"
            "Bot is running and monitoring the group.\n"
            "Admin notifications enabled.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def welcome_new_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Welcome new members with personalized message"""
        new_members = update.message.new_chat_members
        
        for member in new_members:
            if member.id == context.bot.id:
                # Bot added to group
                self.group_id = update.effective_chat.id
                await update.message.reply_text(
                    "🤖 *Premium Moderation Bot Activated*\n\n"
                    "I will now enforce group rules automatically.\n"
                    "Admin: Configure settings via private message.",
                    parse_mode=ParseMode.MARKDOWN
                )
                continue
            
            # Skip if user is bot
            if member.is_bot:
                continue
            
            # Store user in database
            self.db.create_user(
                user_id=member.id,
                username=member.username or "",
                first_name=member.first_name,
                last_name=member.last_name or ""
            )
            
            # Send welcome message
            welcome_msg = (
                f"👋 Welcome to the Premium Group, *Mr. {member.first_name}*!\n\n"
                f"{self.RULES_MESSAGE}\n\n"
                "Please read and respect the rules above. "
                "Violations will result in warnings, mutes, or bans."
            )
            
            await update.message.reply_text(
                welcome_msg,
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Notify admin
            await self.notify_admin(
                f"🆕 *New Member Joined*\n"
                f"• Name: {member.first_name} {member.last_name or ''}\n"
                f"• Username: @{member.username or 'N/A'}\n"
                f"• ID: `{member.id}`\n"
                f"• Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                context
            )
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process all messages for rule violations"""
        if not update.message or not update.effective_chat:
            return
        
        user = update.effective_user
        message = update.message
        
        # Skip if from admin
        if user.id == ADMIN_ID:
            return
        
        # Check for forwarded messages
        if message.forward_date or message.forward_from or message.forward_from_chat:
            await self.handle_violation(
                update, context, 
                reason="Forwarded message",
                delete_message=True
            )
            return
        
        # Check for external links
        if message.entities or message.caption_entities:
            for entity in (message.entities or message.caption_entities):
                if entity.type in ["url", "text_link"]:
                    await self.handle_violation(
                        update, context,
                        reason="External link",
                        delete_message=True
                    )
                    return
        
        # Check for suspicious content (simplified - can be expanded)
        text = message.text or message.caption or ""
        suspicious_terms = ["leak", "pirate", "unauthorized", "share", "free", "download"]
        if any(term in text.lower() for term in suspicious_terms):
            await self.handle_violation(
                update, context,
                reason="Suspicious content",
                delete_message=True
            )
            return
    
    async def handle_violation(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                             reason: str, delete_message: bool = True):
        """Handle rule violations with warning system"""
        user = update.effective_user
        chat = update.effective_chat
        
        # Get user data
        user_data = self.db.get_user(user.id)
        if not user_data:
            user_data = {"warning_count": 0}
        
        current_warnings = user_data.get("warning_count", 0)
        new_warning_count = current_warnings + 1
        
        # Delete violating message
        if delete_message:
            try:
                await update.message.delete()
            except Exception as e:
                logger.error(f"Failed to delete message: {e}")
        
        # Record warning in database
        self.db.add_warning(user.id, reason, ADMIN_ID)
        self.db.update_user_warning(user.id)
        
        # Determine action based on warning count
        if new_warning_count == 1:
            # First warning
            warning_msg = (
                f"⚠️ *Warning #{new_warning_count}*\n\n"
                f"*Mr. {user.first_name}*, you violated the rules:\n"
                f"• Reason: {reason}\n\n"
                f"Next violation: 24-hour mute\n"
                f"Please review the group rules."
            )
            
            await context.bot.send_message(
                chat_id=chat.id,
                text=warning_msg,
                parse_mode=ParseMode.MARKDOWN
            )
            
            action = "WARNING"
            
        elif new_warning_count == 2:
            # Second warning - mute for 24 hours
            until_date = int((datetime.now() + timedelta(hours=MUTE_DURATION)).timestamp())
            
            try:
                await chat.restrict_member(
                    user_id=user.id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_media_messages=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False
                    ),
                    until_date=until_date
                )
                
                mute_msg = (
                    f"🔇 *24-Hour Mute*\n\n"
                    f"*Mr. {user.first_name}* has been muted for 24 hours.\n"
                    f"• Reason: {reason}\n"
                    f"• Warnings: {new_warning_count}/3\n\n"
                    f"Next violation: Permanent ban"
                )
                
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=mute_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
                
                self.db.set_user_muted(user.id, True)
                action = "MUTE"
                
            except Exception as e:
                logger.error(f"Failed to mute user {user.id}: {e}")
                action = "MUTE_FAILED"
                
        elif new_warning_count >= 3:
            # Third warning - ban
            try:
                await chat.ban_member(user_id=user.id)
                
                ban_msg = (
                    f"🚫 *Permanent Ban*\n\n"
                    f"*Mr. {user.first_name}* has been banned.\n"
                    f"• Reason: {reason}\n"
                    f"• Total warnings: {new_warning_count}\n\n"
                    f"User removed from premium group."
                )
                
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=ban_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
                
                self.db.set_user_banned(user.id, True)
                action = "BAN"
                
            except Exception as e:
                logger.error(f"Failed to ban user {user.id}: {e}")
                action = "BAN_FAILED"
        
        # Notify admin
        await self.notify_admin(
            f"⚠️ *Violation Alert*\n"
            f"• User: {user.first_name} (@{user.username or 'N/A'})\n"
            f"• ID: `{user.id}`\n"
            f"• Reason: {reason}\n"
            f"• Warnings: {new_warning_count}/3\n"
            f"• Action: {action}\n"
            f"• Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            context
        )
    
    async def notify_admin(self, message: str, context: ContextTypes.DEFAULT_TYPE):
        """Send notification to admin"""
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
    
    async def send_reminder(self, context: ContextTypes.DEFAULT_TYPE):
        """Send periodic reminder about rules"""
        if not self.group_id:
            return
        
        reminder_msg = (
            "📢 *REMINDER* 📢\n\n"
            "Screenshot/screen recording is *STRICTLY PROHIBITED* in this premium group.\n\n"
            "Violation of this rule will result in:\n"
            "• Immediate removal from group\n"
            "• No refund of subscription\n"
            "• Possible legal action\n\n"
            "Enjoy the exclusive content responsibly! 🤝"
        )
        
        try:
            await context.bot.send_message(
                chat_id=self.group_id,
                text=reminder_msg,
                parse_mode=ParseMode.MARKDOWN
            )
            logger.info("📢 Periodic reminder sent")
        except Exception as e:
            logger.error(f"Failed to send reminder: {e}")
    
    async def check_expired_subscriptions(self, context: ContextTypes.DEFAULT_TYPE):
        """Check and remove users with expired subscriptions"""
        if not self.group_id:
            return
        
        expired_users = self.db.get_expired_subscriptions()
        
        for user_id in expired_users:
            try:
                # Get user info
                user_data = self.db.get_user(user_id)
                if not user_data:
                    continue
                
                # Remove from group
                await context.bot.ban_chat_member(
                    chat_id=self.group_id,
                    user_id=user_id
                )
                
                # Clean up database
                self.db.remove_subscription(user_id)
                self.db.set_user_banned(user_id, True)
                
                # Notify admin
                await self.notify_admin(
                    f"🗑️ *Expired Subscription Removed*\n"
                    f"• User ID: `{user_id}`\n"
                    f"• Name: {user_data.get('first_name', 'Unknown')}\n"
                    f"• Subscription expired",
                    context
                )
                
                logger.info(f"Removed expired subscription user: {user_id}")
                
            except Exception as e:
                logger.error(f"Failed to remove expired user {user_id}: {e}")
    
    async def admin_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command: Get bot statistics"""
        if update.effective_user.id != ADMIN_ID:
            return
        
        try:
            # Get counts from database
            total_users = self.db.db[USERS_COLLECTION].count_documents({})
            active_warnings = self.db.db[WARNINGS_COLLECTION].count_documents({"resolved": False})
            muted_users = self.db.db[USERS_COLLECTION].count_documents({"is_muted": True})
            banned_users = self.db.db[USERS_COLLECTION].count_documents({"is_banned": True})
            
            stats_msg = (
                "📊 *Bot Statistics*\n\n"
                f"• Total Users: {total_users}\n"
                f"• Active Warnings: {active_warnings}\n"
                f"• Muted Users: {muted_users}\n"
                f"• Banned Users: {banned_users}\n"
                f"• Group ID: `{self.group_id or 'Not set'}`\n\n"
                f"Database: {self.db.db.name}\n"
                f"Uptime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )
            
            await update.message.reply_text(
                stats_msg,
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            await update.message.reply_text("❌ Error fetching statistics")
    
    async def admin_warn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command: Manually warn a user"""
        if update.effective_user.id != ADMIN_ID:
            return
        
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /warn <user_id> <reason>"
            )
            return
        
        try:
            user_id = int(context.args[0])
            reason = " ".join(context.args[1:])
            
            # Create mock update for violation handling
            class MockMessage:
                def __init__(self):
                    self.message_id = 0
                    self.delete = lambda: None
            
            mock_update = Update(
                update_id=update.update_id,
                message=MockMessage()
            )
            
            await self.handle_violation(
                mock_update, context,
                reason=f"Admin manual: {reason}",
                delete_message=False
            )
            
            await update.message.reply_text(
                f"✅ Warning issued to user {user_id}"
            )
            
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID")
        except Exception as e:
            logger.error(f"Error in admin_warn: {e}")
            await update.message.reply_text("❌ Error issuing warning")
    
    def setup_handlers(self):
        """Setup all bot handlers"""
        # Command handlers
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("stats", self.admin_stats))
        self.app.add_handler(CommandHandler("warn", self.admin_warn))
        
        # Member handlers
        self.app.add_handler(
            ChatMemberHandler(self.welcome_new_member, ChatMemberHandler.CHAT_MEMBER)
        )
        
        # Message handlers
        self.app.add_handler(
            MessageHandler(
                filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO,
                self.handle_message
            )
        )
        
        # Job queues for periodic tasks
        self.app.job_queue.run_repeating(
            self.send_reminder,
            interval=REMINDER_INTERVAL * 3600,
            first=10
        )
        
        self.app.job_queue.run_daily(
            self.check_expired_subscriptions,
            time=datetime.time(hour=0, minute=0),  # Midnight UTC
            days=(0, 1, 2, 3, 4, 5, 6)
        )
    
    async def run(self):
        """Start the bot with long polling"""
        # Create application
        self.app = Application.builder().token(BOT_TOKEN).build()
        
        # Setup handlers
        self.setup_handlers()
        
        # Start polling
        logger.info("🤖 Starting Premium Moderation Bot...")
        logger.info(f"👑 Admin ID: {ADMIN_ID}")
        logger.info("📊 Database connected")
        
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            poll_interval=1.0,
            timeout=30,
            drop_pending_updates=True
        )
        
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)  # Sleep for 1 hour

# ====================== MAIN ENTRY POINT ======================

async def main():
    """Main entry point with error handling"""
    # Validate environment variables
    if not all([BOT_TOKEN, ADMIN_ID, MONGODB_URI]):
        logger.error("❌ Missing environment variables")
        logger.error(f"BOT_TOKEN: {'Set' if BOT_TOKEN else 'Missing'}")
        logger.error(f"ADMIN_ID: {'Set' if ADMIN_ID else 'Missing'}")
        logger.error(f"MONGODB_URI: {'Set' if MONGODB_URI else 'Missing'}")
        return
    
    # Create and run bot
    bot = PremiumModerationBot()
    
    # Keep restarting on failure (for production)
    while True:
        try:
            await bot.run()
        except Exception as e:
            logger.error(f"❌ Bot crashed: {e}")
            logger.info("🔄 Restarting in 30 seconds...")
            await asyncio.sleep(30)

if __name__ == "__main__":
    # Run the bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")

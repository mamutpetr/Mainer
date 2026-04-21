import os
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.dispatcher.middlewares.base import BaseMiddleware

from sqlalchemy import BigInteger, Numeric, DateTime, ForeignKey, Float, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# 1. CONFIGURATION
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("BOT_TOKEN and DATABASE_URL must be set in Environment Variables.")

# 2. DATABASE MODELS
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[float] = mapped_column(Numeric(20, 9), default=0.0)
    last_claim: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=True)
    
    boosters = relationship("UserBooster", back_populates="user")

class UserBooster(Base):
    __tablename__ = "user_boosters"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    multiplier: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user = relationship("User", back_populates="boosters")

# 3. DATABASE SETUP
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# 4. CORE LOGIC
getcontext().prec = 20
BASE_REWARD = Decimal('0.000000001')
MIN_CLAIM_INTERVAL_SECONDS = 10

def calculate_yield(last_claim: datetime, current_time: datetime, active_multipliers: list[float]) -> tuple[Decimal, int]:
    delta = (current_time - last_claim).total_seconds()
    
    if delta < MIN_CLAIM_INTERVAL_SECONDS:
        return Decimal('0.0'), 0

    intervals = int(delta // MIN_CLAIM_INTERVAL_SECONDS)
    total_multiplier = Decimal('1.0') + Decimal(str(sum(active_multipliers)))
    
    earned = Decimal(intervals) * BASE_REWARD * total_multiplier
    return earned, intervals

# 5. TELEGRAM HANDLERS
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    user_id = message.from_user.id
    
    result = await session.execute(select(User).where(User.telegram_id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        user = User(telegram_id=user_id)
        session.add(user)
        await session.commit()
        await message.answer("Вітаю у Tap-to-Earn! Натисни /claim щоб зібрати монети.")
    else:
        await message.answer(f"З поверненням! Твій баланс: {user.balance:.9f}\nТисни /claim")

@router.message(F.text == "/claim")
async def process_claim(message: Message, session: AsyncSession):
    user_id = message.from_user.id
    now = datetime.now(timezone.utc)
    
    result = await session.execute(select(User).where(User.telegram_id == user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        return await message.answer("Спочатку натисни /start")

    boosters_result = await session.execute(
        select(UserBooster.multiplier)
        .where(UserBooster.user_id == user_id)
        .where(UserBooster.expires_at > now)
    )
    multipliers = [m[0] for m in boosters_result.all()]

    earned, intervals = calculate_yield(user.last_claim, now, multipliers)

    if earned <= 0:
        return await message.answer("Занадто рано! Зачекай хоча б 10 секунд.")

    user.balance = float(Decimal(str(user.balance)) + earned)
    user.last_claim = now
    await session.commit()

    await message.answer(
        f"✅ Успішно зібрано!\n"
        f"⏳ Минуло інтервалів: {intervals}\n"
        f"💰 Зароблено: {earned:.9f}\n"
        f"🏦 Поточний баланс: {user.balance:.9f}"
    )

# 6. MIDDLEWARE
class DbSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        async with AsyncSessionLocal() as session:
            data['session'] = session
            return await handler(event, data)

# 7. MAIN RUNNER
async def main():
    await init_db()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    
    dp.update.middleware(DbSessionMiddleware())
    dp.include_router(router)
    
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


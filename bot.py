"""Точка входа: Telegram бот + API сервер + фоновые задачи."""

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from db.database import init_db, close_db
from db import models
from api.server import create_app
from handlers import admin
from services.vless import restart_xray, stop_xray, is_xray_running, get_xray_tunnel_count
from services.pollinations import check_key_balance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def seed_vless_from_env():
    """Загрузка VLESS конфигов из .env при первом запуске."""
    if not settings.vless_configs:
        return
    from services.vless import parse_vless_url
    existing = await models.get_all_vless()
    existing_urls = {v["url"] for v in existing}
    for url in settings.vless_configs:
        if url not in existing_urls:
            parsed = parse_vless_url(url)
            remark = parsed.get("remark", "") if parsed else ""
            await models.add_vless(url, remark)
            logger.info("VLESS из env добавлен: %s", remark or url[:30])


async def setup_xray():
    """Запуск XRAY если есть VLESS конфиги."""
    urls = await models.get_active_vless_urls()
    if urls:
        ok = await restart_xray(urls)
        if ok:
            logger.info("XRAY запущен с %d туннелями", len(urls))
        else:
            logger.warning("Не удалось запустить XRAY")
    else:
        logger.info("Нет VLESS конфигов, XRAY не запущен")


async def balance_check_loop():
    """Фоновая проверка балансов — часто, для своевременного свапа ключей."""
    while True:
        try:
            await asyncio.sleep(settings.balance_check_interval)

            keys = await models.get_all_keys()
            if not keys:
                continue

            for k in keys:
                # Определяем порт SOCKS5 для привязанного VLESS
                vless_idx = k.get("vless_config_index")
                socks_port = (10801 + vless_idx) if vless_idx is not None and is_xray_running() else None

                result = await check_key_balance(k["key"], socks_port)
                balance = result.get("balance")
                next_reset = result.get("next_reset_at")
                await models.update_key_balance(k["id"], balance, next_reset)

                # Деактивируем ключ если баланс ниже порога
                if balance is not None and balance < settings.balance_threshold:
                    if k["is_active"]:
                        await models.deactivate_key(k["id"])
                        logger.warning(
                            "Ключ #%d деактивирован (баланс: %.2f < %.2f)",
                            k["id"], balance, settings.balance_threshold,
                        )

            # Проверяем нужно ли перезапустить XRAY (новые VLESS конфиги или XRAY упал)
            urls = await models.get_active_vless_urls()
            if urls and (not is_xray_running() or len(urls) != get_xray_tunnel_count()):
                await restart_xray(urls)
                logger.info("XRAY перезапущен с %d туннелями", len(urls))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Ошибка в цикле проверки балансов: %s", e)
            await asyncio.sleep(30)


async def run_api_server():
    """Запуск aiohttp API сервера."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.api_host, settings.api_port)
    await site.start()
    logger.info("API сервер запущен на %s:%d", settings.api_host, settings.api_port)
    return runner


async def main():
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Инициализация БД
    await init_db()

    # Загрузка VLESS из .env
    await seed_vless_from_env()

    # Запуск XRAY
    await setup_xray()

    # Роутеры
    dp.include_router(admin.router)

    # Запуск API сервера
    api_runner = await run_api_server()

    # Фоновая проверка балансов
    balance_task = asyncio.create_task(balance_check_loop())

    logger.info("Сервис запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        balance_task.cancel()
        try:
            await balance_task
        except asyncio.CancelledError:
            pass
        await stop_xray()
        await api_runner.cleanup()
        await close_db()
        await bot.session.close()
        logger.info("Сервис остановлен.")


if __name__ == "__main__":
    asyncio.run(main())

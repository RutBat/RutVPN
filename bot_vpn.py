import logging
import json
import uuid
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher import FSMContext
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher.filters.state import State, StatesGroup
from datetime import timedelta, datetime
from dotenv import load_dotenv
from yoomoney import Client
import os
import aiomysql


# Загрузка переменных окружения из .env файла
load_dotenv()
active_support_chats = set()
# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получение переменных из env
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
XUI_HOST = os.getenv('XUI_HOST')
XUI_LOGIN = os.getenv('XUI_LOGIN')
XUI_USERNAME = os.getenv('XUI_USERNAME')
XUI_PASSWORD = os.getenv('XUI_PASSWORD')
INBOUND_ID = int(os.getenv('INBOUND_ID', 1))
PORT = int(os.getenv('PORT', 443))
FLOW = os.getenv('FLOW', 'xtls-rprx-vision')
LIMIT_IP = int(os.getenv('LIMIT_IP', 1))
TOTAL_GB = int(os.getenv('TOTAL_GB', 0))
ADMIN_ID = int(os.getenv('ADMIN_ID'))  # ID администратора

# Параметры для подключения к MySQL
MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

# Опции подписок
subscriptions = {
    "Вечный Премиум": {"duration": "НАВСЕГДА", "price": "НАВСЕГДА", "time_delta": None},
    "Эксперт 365": {"duration": "Год", "time_delta": timedelta(days=365)},
    "Продвинутый 180": {"duration": "Пол года", "time_delta": timedelta(days=180)},
    "Базовый Старт": {"duration": "Месяц", "time_delta": timedelta(days=30)},
    "Тест": {"duration": "1 час", "time_delta": timedelta(hours=1)}
}

# Состояния для FSM
class ClientStates(StatesGroup):
    waiting_for_subscription = State()
    waiting_for_profile_name = State()
    waiting_for_payment_screenshot = State()
    waiting_for_payment_confirmation = State()

# Создание бота и диспетчера
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# Глобальные переменные для хранения куки
cookies = None

# Асинхронная функция для подключения к MySQL
async def create_mysql_connection():
    return await aiomysql.create_pool(
        host=MYSQL_HOST, 
        user=MYSQL_USER, 
        password=MYSQL_PASSWORD, 
        db=MYSQL_DB, 
        autocommit=True
    )

# Инициализация базы данных и создание таблиц, если они не существуют
async def init_db():
    pool = await create_mysql_connection()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Создание таблицы пользователей
            await cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                username VARCHAR(255),
                profile_name VARCHAR(255),
                subscription_name VARCHAR(255),
                start_time DATETIME,
                expiry_time DATETIME
            );
            """)
            await conn.commit()
    pool.close()



async def add_or_update_user(telegram_id):
    # Функция для добавления пользователя или обновления, если он уже существует
    pool = await create_mysql_connection()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Проверка существования пользователя с данным telegram_id
            await cur.execute("SELECT id FROM users WHERE telegram_id = %s", (telegram_id,))
            result = await cur.fetchone()

            if result:
                print(f"Пользователь с telegram_id {telegram_id} уже существует.")
            else:
                # Вставка нового пользователя, если его нет в базе
                await cur.execute("INSERT INTO users (telegram_id) VALUES (%s)", (telegram_id,))
                print(f"Пользователь с telegram_id {telegram_id} добавлен в базу данных.")

    pool.close()
    await pool.wait_closed()



    
# Сохранение информации о пользователе и подписке в базе данных
async def save_user_to_db(telegram_id, username, profile_name, subscription_name, start_time, expiry_time, vless_link):
    pool = await create_mysql_connection()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
            INSERT INTO user_profiles (telegram_id, username, profile_name, subscription_name, start_time, expiry_time, vless_link)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (telegram_id, username, profile_name, subscription_name, start_time, expiry_time, vless_link))
            await conn.commit()
    pool.close()

# Получение информации о подписке пользователя из базы данных
async def get_user_subscriptions_from_db(telegram_id):
    pool = await create_mysql_connection()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Получаем все подписки пользователя
            await cur.execute("SELECT * FROM user_profiles WHERE telegram_id = %s;", (telegram_id,))
            user_subscriptions = await cur.fetchall()  # Получаем все строки
    pool.close()
    return user_subscriptions



# Асинхронная функция для входа в систему и получения куки
async def login():
    url = XUI_LOGIN
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={"username": XUI_USERNAME, "password": XUI_PASSWORD}) as response:
            if response.status == 200:
                return response.cookies
            else:
                raise Exception("Login failed")

# Асинхронная функция для добавления клиента в существующий inbound
async def add_client_to_inbound(inbound_id: int, profile_name: str, cookies, time_delta, user_username: str) -> dict:
    url = f"{XUI_HOST}/addClient"
    client_id = str(uuid.uuid4())  # Генерация уникального идентификатора клиента

    # Формируем имя профиля по шаблону: "ИмяПодписки - Юзернейм"
    formatted_profile_name = f"{profile_name} - {user_username}"

    # Вычисление даты окончания подписки
    expiry_time = None
    if time_delta:
        expiry_time = int((datetime.now() + time_delta).timestamp() * 1000)
    else:
        expiry_time = 9999999999999  # Для "Вечный Премиум"

    client_settings = {
        "clients": [
            {
                "id": client_id,
                "email": formatted_profile_name,
                "alterId": 0,
                "limitIp": LIMIT_IP,
                "totalGB": TOTAL_GB,
                "expiryTime": expiry_time,
                "enable": True,
                "flow": FLOW
            }
        ]
    }

    data = {
        "id": inbound_id,
        "settings": json.dumps(client_settings)
    }
    headers = {'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data, cookies=cookies, headers=headers) as response:
            response_data = await response.json()
            if response_data.get('success'):
                return {"profile_name": formatted_profile_name, "client_id": client_id, "expiry_time": expiry_time, "success": True}
            else:
                return {"success": False, "msg": response_data.get('msg')}



















# Асинхронная функция для получения информации о подписке пользователя
async def get_client_info(user_id: int, cookies) -> dict:
    url = f"{XUI_HOST}/clients"
    headers = {'Content-Type': 'application/json'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, cookies=cookies, headers=headers) as response:
                response.raise_for_status()
                response_data = await response.json()

        # Получаем реальный юзернейм из состояния
        async with dp.storage.get_data(chat=user_id) as user_data:
            username = user_data.get('user_username', 'example_username')  # Получаем реальный юзернейм

        for client in response_data.get('clients', []):
            profile_name = client.get('email', '')
            if profile_name.endswith(f" - {username}"):
                profile_base_name = profile_name.split(' - ')[0]
                return {
                    "profile_name": profile_base_name,
                    "subscription_name": client.get('subscriptionName'),
                    "start_time": datetime.fromtimestamp(client.get('createTime') / 1000).strftime("%H:%M - %d.%m.%Y"),
                    "expiry_time": datetime.fromtimestamp(client.get('expiryTime') / 1000).strftime("%H:%M - %d.%m.%Y"),
                    "client_id": client.get('id')
                }
        return {"error": "Информация о подписке не найдена"}
    except aiohttp.ClientError as e:
        logging.error(f"Ошибка при запросе информации о клиенте: {e}")
        return {"error": "Ошибка при запросе информации о подписке"}











# Обработчик команды /status

@dp.message_handler(commands='status', state='*')
async def cmd_status(message: types.Message, state: FSMContext):


    
    telegram_id = message.from_user.id

    # Получаем все подписки пользователя из базы данных
    user_subscriptions = await get_user_subscriptions_from_db(telegram_id)

    if user_subscriptions:
        current_time = datetime.now()
        valid_subscriptions = []

        for user_data in user_subscriptions:
            # Форматируем информацию о подписке
            profile_name = user_data.get('profile_name', 'Не указано')
            subscription_name = user_data.get('subscription_name', 'Не указано')
            start_time = user_data.get('start_time', 'Не указано')
            expiry_time = user_data.get('expiry_time')  # Здесь expiry_time должен быть уже datetime
            vless_link = user_data.get('vless_link', 'Не указан')

            # Проверяем, что expiry_time корректен и валидный
            if isinstance(expiry_time, datetime):
                if expiry_time > current_time:
                    valid_subscriptions.append({
                        'profile_name': profile_name,
                        'subscription_name': subscription_name,
                        'start_time': start_time,
                        'expiry_time': expiry_time.strftime('%Y-%m-%d %H:%M:%S'),
                        'vless_link': vless_link
                    })
            else:
                print(f"Некорректный тип данных для даты окончания подписки: {expiry_time}")

        if valid_subscriptions:
            # Формируем сообщение с информацией о действующих подписках
            response = "*🔍 Ваши действующие подписки:*\n\n"
            for sub in valid_subscriptions:
                response += (
                    f"*📛 Имя профиля:* `{sub['profile_name']}`\n"
                    f"*📅 Подписка:* `{sub['subscription_name']}`\n"
                    f"*🕒 Начало подписки:* `{sub['start_time']}`\n"
                    f"*📆 Окончание подписки:* `{sub['expiry_time']}`\n"
                    f"*🔑 Ключ доступа:*\n`{sub['vless_link']}`\n\n"
                )

            # Отправляем сообщение пользователю
            await message.answer(response, parse_mode="Markdown")
        else:
            await message.answer(
                "🚫 У вас нет активных подписок. Пожалуйста, обновите подписку, чтобы продолжить пользоваться услугами."
            )
    else:
        await message.answer(
            "🚫 Мы не нашли информацию о ваших подписках. Пожалуйста, убедитесь, что вы правильно ввели команду и попробуйте снова."
        )














# Функция для отслеживания сообщений и их удаления
async def track_message(message: types.Message, state: FSMContext):
    # Получаем текущие данные состояния
    user_data = await state.get_data()

    # Добавляем новое сообщение в список
    previous_messages = user_data.get('previous_messages', [])
    previous_messages.append(message.message_id)

    # Обновляем состояние
    await state.update_data(previous_messages=previous_messages)





























# Обработчик команды /start
@dp.message_handler(commands='start')
async def cmd_start(message: types.Message, state: FSMContext):
    # Отправляем изображение
    with open('pic1.png', 'rb') as photo:
        sent_photo = await message.answer_photo(photo)
        await track_message(sent_photo, state)  # Отслеживаем ID сообщения

    # Создаем ReplyKeyboardMarkup для кнопок под строкой ввода
    reply_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    
    # Добавляем кнопки в ReplyKeyboardMarkup
    reply_keyboard.row(
        KeyboardButton("К подпискам"), 
        KeyboardButton("Проверить статус подписки")
    )
    reply_keyboard.add(KeyboardButton("Связаться с поддержкой"))

    # Отправляем сообщение с ReplyKeyboardMarkup
    await message.answer("Выберите действие с клавиатуры:", reply_markup=reply_keyboard)

    # Отправляем сообщение с текстом и InlineKeyboardMarkup
    inline_keyboard = InlineKeyboardMarkup(row_width=2)
    for sub_name, sub_details in subscriptions.items():
        button = InlineKeyboardButton(sub_name, callback_data=sub_name)
        inline_keyboard.add(button)

    sent_message = await message.answer(
        "🌐 Приветствуем вас на *RutVPN*!\n\n"
        "Мы поможем вам получить доступ к заблокированным ресурсам в Интернете. 🚀\n\n"
        "Заблокировали Ютуб? Не беда! 📺\n\n"
        "Не грузится Инстаграм? Не проблема! 📷\n\n"
        "С нами вы всегда будете иметь доступ к любимым сайтам и сервисам. 💪\n\n"
        "За свободный интернет! 🌍",
        reply_markup=inline_keyboard
    )
    await track_message(sent_message, state)  # Отслеживаем ID сообщения

    await ClientStates.waiting_for_subscription.set()




# Обработчик команды /support (разрешен вне зависимости от состояния)
@dp.message_handler(commands='support', state="*")
async def cmd_support(message: types.Message, state: FSMContext):
    await activate_support(message, state)

# Обработчик команды /status (разрешен вне зависимости от состояния)
@dp.message_handler(commands='status', state="*")
async def cmda_status(message: types.Message, state: FSMContext):
    await cmd_status(message, state)

# Обработчик нажатий на кнопки Reply-клавиатуры
@dp.message_handler(lambda message: message.text == "К подпискам", state="*")
async def handle_subscriptions(message: types.Message, state: FSMContext):
    await cmd_start(message, state)

@dp.message_handler(lambda message: message.text == "Проверить статус подписки", state="*")
async def handle_check_status(message: types.Message, state: FSMContext):
    await cmda_status(message, state)

@dp.message_handler(lambda message: message.text == "Связаться с поддержкой", state="*")
async def handle_support(message: types.Message, state: FSMContext):
    await cmd_support(message, state)



















# Обработчик выбора подписки
@dp.callback_query_handler(state=ClientStates.waiting_for_subscription)
async def process_subscription_choice(callback_query: types.CallbackQuery, state: FSMContext):
    subscription_name = callback_query.data

    # Обновляем данные состояния
    await state.update_data(subscription_name=subscription_name)
    await state.update_data(user_id=callback_query.from_user.id)
    await state.update_data(user_username=callback_query.from_user.username)
    telegram_id = callback_query.from_user.id  # Замените на реальный telegram_id
    await add_or_update_user(telegram_id)
    if subscription_name == "Тест":
        # Обрабатываем подписку "Тест" отдельно
        await process_test_subscription(callback_query, state)
    else:
        # Отправляем изображение pic2 перед текстом
        with open('pic2.png', 'rb') as photo:
            sent_photo = await callback_query.message.answer_photo(photo)
            await track_message(sent_photo, state)  # Отслеживаем ID сообщения

        # Отправляем текстовое сообщение
        sent_message = await callback_query.message.answer(
            f"🎉 Вы выбрали подписку: *{subscription_name}*. Отличный выбор! 👍\n\n Теперь введите имя профиля, которое вы будете использовать. \n \nЭто имя может быть любым, удобным для вас.\n"
        )
        await track_message(sent_message, state)  # Отслеживаем ID сообщения

        # Устанавливаем следующее состояние
        await ClientStates.waiting_for_profile_name.set()

# Обработчик подписки "Тест"
async def process_test_subscription(callback_query: types.CallbackQuery, state: FSMContext):
    # Отправляем сообщение пользователю
    await callback_query.message.answer(
        "🎉 Вы выбрали тестовую подписку! 👌\n\n Пожалуйста, введите имя профиля, которое вы будете использовать.\n"
    )

    # Устанавливаем следующее состояние
    await ClientStates.waiting_for_profile_name.set()





async def get_last_subscription_time(user_id: int):
    pool = await create_mysql_connection()
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = 'SELECT last_subscription_time FROM user_subscriptions WHERE user_id = %s'
                await cursor.execute(query, (user_id,))
                result = await cursor.fetchone()
                if result:
                    return result[0]  # Возвращает время последней подписки
                else:
                    return None
    finally:
        pool.close()


async def update_last_subscription_time(user_id: int):
    pool = await create_mysql_connection()
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = '''
                    INSERT INTO user_subscriptions (user_id, last_subscription_time)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE last_subscription_time = VALUES(last_subscription_time)
                '''
                await cursor.execute(query, (user_id, datetime.now()))
    finally:
        pool.close()











@dp.message_handler(state=ClientStates.waiting_for_profile_name)
async def process_profile_name(message: types.Message, state: FSMContext):
    # Сохраняем имя профиля в состояние
    await state.update_data(profile_name=message.text)

    # Проверяем, была ли выбрана тестовая подписка
    user_data = await state.get_data()
    subscription_name = user_data.get('subscription_name')

    if subscription_name == "Тест":
        # Пропускаем шаг с оплатой для тестовой подписки
        await process_test_profile(message, state)
    else:
        # Определяем стоимость подписки
        subscription_prices = {
            "Вечный Премиум": "4999р",
            "Эксперт 365": "2999р",
            "Продвинутый 180": "1799р",
            "Базовый Старт": "499р"
        }
        price = subscription_prices.get(subscription_name, "Неизвестная подписка")

        # Отправляем изображение pic3 перед текстом
        with open('pic3.png', 'rb') as photo:
            sent_photo = await message.answer_photo(photo)
            await track_message(sent_photo, state)  # Отслеживаем ID сообщения


        # Создаем кнопку с динамической ссылкой
        price = subscription_prices.get(subscription_name, "Неизвестная подписка").replace("р", "").strip()  # Убираем символ валюты и пробелы
        payment_url = f"https://yoomoney.ru/quickpay/confirm.xml?receiver=410015027314251&quickpay-form=shop&targets=SubRutVPN&paymentType=AC&sum={price}&label=RutVPN"
        payment_button = InlineKeyboardButton("Оплата картой", url=payment_url)
        keyboard = InlineKeyboardMarkup().add(payment_button)


        # Отправляем текстовое сообщение с кнопкой
        sent_message = await message.answer(
            f"📸 Для получения ключа доступа вам нужно оплатить стоимость выбранной подписки по СБП на номер телефона\n\n"
            f"+79789458418 \n\n"
            f"Или картой нажав на кнопку ниже\n\n"
            f"{subscription_name} - {price} р.\n\n"
            f"После завершения перевода, сделайте скриншот оплаты и отправьте нам. \n\n"
            f"На скриншоте должно быть видно сумму и статус перевода (в обработке или успешно).",
            reply_markup=keyboard
            )
        await track_message(sent_message, state)  # Отслеживаем ID сообщения

        # Устанавливаем следующее состояние
        await ClientStates.waiting_for_payment_screenshot.set()
    # Завершаем состояние после обработки команды /status
    



    
# Обработчик тестовой подписки
async def process_test_profile(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    profile_name = user_data.get('profile_name')
    user_id = message.from_user.id
    user_username = message.from_user.username

    # Проверяем время последнего получения подписки
    last_subscription_time = await get_last_subscription_time(user_id)
    if last_subscription_time and datetime.now() - last_subscription_time < timedelta(hours=1):
        await bot.send_message(
            user_id,
            "🚫 Вы уже получали подписку в течение последнего часа. Пожалуйста, подождите, прежде чем запрашивать новый ключ."
        )
        await state.finish()
        return

    # Получаем куки для добавления клиента
    global cookies
    if cookies is None:
        cookies = await login()

    # Добавляем клиента в inbound
    subscription_name = "Тест"
    time_delta = subscriptions.get(subscription_name).get('time_delta')
    result = await add_client_to_inbound(INBOUND_ID, profile_name, cookies, time_delta, user_username)

    if result.get('success'):
        expiry_date = datetime.fromtimestamp(result['expiry_time'] / 1000).strftime("%H:%M - %d.%m.%Y")

        vless_link = f"vless://{result['client_id']}@85.192.60.46:{PORT}?type=tcp&security=reality&pbk=0RWFgQIE3YQVxncgmcBVwcjf19jmD7RkInHflw1Ydjo&fp=chrome&sni=aeza.com&sid=89bbd6&spx=%2F&flow={FLOW}# {profile_name}"
        await save_user_to_db(
            telegram_id=user_id,
            username=user_username,
            profile_name=profile_name,
            subscription_name=subscription_name,
            start_time=datetime.now(),
            expiry_time=datetime.fromtimestamp(result['expiry_time'] / 1000),
            vless_link=vless_link
        )

        # Обновляем время последнего получения подписки
        await update_last_subscription_time(user_id)

        # Удаление всех сообщений пользователя, которые были отправлены до этого
        previous_messages = user_data.get('previous_messages', [])
        for msg_id in previous_messages:
            try:
                await bot.delete_message(user_id, msg_id)
            except Exception as e:
                logging.error(f"Ошибка при удалении сообщения {msg_id}: {e}")

        # Отправляем изображение перед финальным сообщением
        with open('pic4.png', 'rb') as photo:
            sent_photo = await bot.send_photo(user_id, photo)

        # Отправляем финальное сообщение с подтверждением оплаты
        sent_message = await bot.send_message(
            user_id,
            f"*🎉 Ваша тестовая подписка активирована!*\n\n"
            f"*📛 Имя профиля:* `{profile_name}`\n"
            f"*📅 Подписка:* `{subscription_name}`\n"
            f"*🕒 Начало подписки:* `{datetime.now().strftime('%H:%M - %d.%m.%Y')}`\n"
            f"*📆 Окончание подписки:* `{expiry_date}`\n"
            f"*🔑 Ключ доступа:*\n`{vless_link}`\n\n"
            f"🔍 *Нажмите на ключ, чтобы скопировать его!\n*"
            f"🔍 *Далее в левом нижнем углу перейдите на страницу Инструкций для настройки своего устройства*\n"
            f"🔍 *Повторное получение ключа доступно через пару часов, ожидайте*",
            parse_mode="Markdown"
        )

        # Обновляем список сообщений, сохраняем только новые сообщения
        await dp.storage.update_data(chat=user_id, previous_messages=[sent_photo.message_id, sent_message.message_id])
        await state.finish() 
    else:
        await bot.send_message(user_id, f"🚫 Ошибка при добавлении клиента: {result.get('msg')}")

    # Завершаем состояние после обработки команды /status
    #await state.finish()



# Обработчик получения скриншота оплаты
@dp.message_handler(content_types=['photo'], state=ClientStates.waiting_for_payment_screenshot)
async def process_payment_screenshot(message: types.Message, state: FSMContext):
    # Сохраняем скриншот и отправляем его админу на проверку
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, 
                         caption=f"💵 Новый платеж от @{message.from_user.username} (ID: {message.from_user.id}).\n\nПодтвердить или отклонить?")

# Добавляем инлайн-кнопки для подтверждения/отклонения и автоматической проверки
    keyboard = InlineKeyboardMarkup(row_width=3)
    confirm_button = InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{message.from_user.id}")
    decline_button = InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{message.from_user.id}")
    auto_check_button = InlineKeyboardButton("🤖 Автопроверка", callback_data=f"autocheck_{message.from_user.id}")
    keyboard.add(confirm_button, decline_button, auto_check_button)


    await bot.send_message(ADMIN_ID, "💰 Обработайте платеж:", reply_markup=keyboard)

    # Ожидание подтверждения
    await message.answer("💬 Ваш скриншот отправлен на проверку. Ожидайте подтверждения. \n\n Мы проверим его и подтвердим вашу подписку.\n")
    await ClientStates.waiting_for_payment_confirmation.set()



@dp.callback_query_handler(lambda c: c.data.startswith('autocheck_'))
async def process_auto_check(callback_query: types.CallbackQuery):
    try:
        # Выполняем код для взаимодействия с API YooMoney
        

        token = "410015027314251.4CD309F6F31AF2316FC6A941498D5C5F509538D7BF6D611452933CAF8FDD1910880707EE40CF06318679F284247C8E83FBFC10C94D406A36C009C6FB0AC8AFDDD28CB920606EBC66C2E5F9DC120FE76DAFC5305862E5C98F43B86270F84000E9E73DC613C3960BF6DC119BCEB0601FF4F77247630C5A54090334ABE32E4AF786"
        client = Client(token)
        history = client.operation_history(label="RutVPN")

        # Формируем результат в текстовом виде
        result_text = "List of operations:\n"
        result_text += f"Next page starts with: {history.next_record}\n"

        for operation in history.operations:
            result_text += f"\nOperation: {operation.operation_id}\n"
            result_text += f"\tStatus     --> {operation.status}\n"
            result_text += f"\tDatetime   --> {operation.datetime}\n"
            result_text += f"\tTitle      --> {operation.title}\n"
            result_text += f"\tPattern id --> {operation.pattern_id}\n"
            result_text += f"\tDirection  --> {operation.direction}\n"
            result_text += f"\tAmount     --> {operation.amount}\n"
            result_text += f"\tLabel      --> {operation.label}\n"
            result_text += f"\tType       --> {operation.type}\n"

        # Отправляем результат обратно в чат
        await bot.send_message(callback_query.from_user.id, result_text)

    except Exception as e:
        # Обрабатываем возможные ошибки и отправляем их пользователю
        await bot.send_message(callback_query.from_user.id, f"🚫 Произошла ошибка при автопроверке: {e}")

    # Подтверждаем callback, чтобы Telegram не показывал значок загрузки в интерфейсе
    await callback_query.answer()
# Обработчик подтверждения/отклонения платежа администратором
@dp.callback_query_handler(lambda c: c.data.startswith('confirm_') or c.data.startswith('decline_'))
async def process_payment_confirmation(callback_query: types.CallbackQuery):
    action, user_id = callback_query.data.split('_')
    user_id = int(user_id)

    # Загружаем данные пользователя
    user_data = await dp.storage.get_data(chat=user_id)

    if action == "confirm":
        subscription_name = user_data.get('subscription_name')
        profile_name = user_data.get('profile_name')
        user_username = user_data.get('user_username')

        # Получаем куки для добавления клиента
        global cookies
        if cookies is None:
            cookies = await login()

        subscription = subscriptions.get(subscription_name)
        time_delta = subscription.get('time_delta') if subscription else None

        result = await add_client_to_inbound(INBOUND_ID, profile_name, cookies, time_delta, user_username)

        if result.get('success'):
            expiry_date = datetime.fromtimestamp(result['expiry_time'] / 1000).strftime("%H:%M - %d.%m.%Y")
            vless_link = f"vless://{result['client_id']}@85.192.60.46:{PORT}?type=tcp&security=reality&pbk=0RWFgQIE3YQVxncgmcBVwcjf19jmD7RkInHflw1Ydjo&fp=chrome&sni=aeza.com&sid=89bbd6&spx=%2F&flow={FLOW}# {profile_name}"

            await save_user_to_db(
                telegram_id=user_id,
                username=user_username,
                profile_name=profile_name,
                subscription_name=subscription_name,
                start_time=datetime.now(),
                expiry_time=datetime.fromtimestamp(result['expiry_time'] / 1000),
                vless_link=vless_link
            )




            # Удаление всех сообщений пользователя, которые были отправлены до этого
            previous_messages = user_data.get('previous_messages', [])
            for msg_id in previous_messages:
                try:
                    await bot.delete_message(user_id, msg_id)
                except Exception as e:
                    logging.error(f"Ошибка при удалении сообщения {msg_id}: {e}")

            # Отправляем изображение перед финальным сообщением
            with open('pic4.png', 'rb') as photo:
                sent_photo = await bot.send_photo(user_id, photo)

            # Отправляем финальное сообщение с подтверждением оплаты
            sent_message = await bot.send_message(
                user_id,
                f"*🎉 Ваша оплата подтверждена!*\n\n"
                f"*📛 Имя профиля:* `{profile_name}`\n"
                f"*📅 Подписка:* `{subscription_name}`\n"
                f"*🕒 Начало подписки:* `{datetime.now().strftime('%H:%M - %d.%m.%Y')}`\n"
                f"*📆 Окончание подписки:* `{expiry_date}`\n"
                f"*🔑 Ключ доступа:*\n`{vless_link}`\n\n"
                f"🔍 *Нажмите на ключ, чтобы скопировать его!\n*"
                f"🔍 *Далее в левом нижнем углу перейдите на страницу Инструкций для настройки своего устройства*",
                parse_mode="Markdown"
            )

            # Обновляем список сообщений, сохраняем только новые сообщения
            await dp.storage.update_data(chat=user_id, 
                                         previous_messages=[sent_photo.message_id, sent_message.message_id])

            await bot.send_message(ADMIN_ID, f"✅ Оплата пользователем @{user_username} (ID: {user_id}) подтверждена.")
        else:
            await bot.send_message(user_id, f"🚫 Ошибка при добавлении клиента: {result.get('msg')}")

    elif action == "decline":
        await bot.send_message(user_id, "❌ Ваша оплата была отклонена. Пожалуйста, свяжитесь с поддержкой для дальнейших действий.")

    await bot.send_message(ADMIN_ID, "Платеж был обработан.")
    
    await dp.storage.finish(chat=user_id)
    await callback_query.answer()


















@dp.message_handler(commands=['support'] , state='*')
async def activate_support(message: types.Message, state: FSMContext):
    # Сбрасываем состояние после отправки ответа
    await state.finish()
    user_id = message.from_user.id
    active_support_chats.add(user_id)
    
    # Сохраняем данные о пользователе
    await dp.storage.update_data(chat=user_id, support_active=True, support_chat_id=user_id)
    
    await message.reply("Вы активировали поддержку. Напишите ваше сообщение, и администратор ответит вам.")
    logging.info(f"User {user_id} activated support.")

@dp.message_handler(lambda message: message.from_user.id in active_support_chats)
async def forward_message_to_admin(message: types.Message):
    admin_id = ADMIN_ID
    keyboard = InlineKeyboardMarkup(row_width=1)
    reply_button = InlineKeyboardButton("Ответить", callback_data=f"reply_{message.from_user.id}")
    keyboard.add(reply_button)
    
    await bot.send_message(
        admin_id,
        f"Сообщение от пользователя @{message.from_user.username} (ID: {message.from_user.id}):\n\n{message.text}",
        reply_markup=keyboard
    )
    await bot.send_message(message.from_user.id, "Ваше сообщение отправлено администратору.")
    logging.info(f"Message from user {message.from_user.id} forwarded to admin.")

@dp.callback_query_handler(lambda c: c.data.startswith('reply_'))
async def handle_reply_callback(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split('_')[1])
    logging.info(f"Admin {callback_query.from_user.id} clicked 'Reply' button for user {user_id}.")
    
    await bot.send_message(
        callback_query.from_user.id,
        "Пожалуйста, напишите ваш ответ. Это сообщение будет отправлено пользователю."
    )
    await dp.storage.update_data(chat=callback_query.from_user.id, support_chat_id=user_id)
    await callback_query.answer()  # Подтверждаем обработку callback

@dp.message_handler(lambda message: message.from_user.id == ADMIN_ID, state='*')
async def forward_admin_response(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    user_id = user_data.get('support_chat_id')
    
    if user_id:
        user_data = await dp.storage.get_data(chat=user_id)
        
        logging.info(f"User data for ID {user_id}: {user_data}")
        
        if user_data.get('support_active'):
            await bot.send_message(
                user_id,
                f"Ответ от администратора:\n\n{message.text}"
            )
            await bot.send_message(ADMIN_ID, "Ответ отправлен пользователю.")
        else:
            await bot.send_message(ADMIN_ID, "Ошибка: Пользователь не активировал поддержку.")
    else:
        await bot.send_message(ADMIN_ID, "Ошибка: Это сообщение не связано с поддержкой.")

    # Сбрасываем состояние после отправки ответа
    await state.finish()

async def on_startup(dp):
    dp.middleware.setup(LoggingMiddleware())
    logging.info("Bot has started!")












# Запуск бота
if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
import aiohttp
import asyncio
import functools
import os
import random
from urllib.parse import unquote, quote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from datetime import datetime, timedelta
from time import time

from telethon import TelegramClient
from telethon.errors import *
from telethon.types import InputBotAppShortName, InputNotifyPeer, InputPeerNotifySettings, InputUser
from telethon.functions import messages, channels, account

from .agents import generate_random_user_agent
from bot.config import settings
from typing import Callable
from bot.utils import logger, log_error, proxy_utils, config_utils, AsyncInterProcessLock, CONFIG_PATH
from bot.exceptions import InvalidSession
from .headers import headers, get_sec_ch_ua


def error_handler(func: Callable):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            await asyncio.sleep(1)

    return wrapper


class Tapper:
    def __init__(self, tg_client: TelegramClient):
        self.tg_client = tg_client
        self.session_name, _ = os.path.splitext(os.path.basename(tg_client.session.filename))
        self.config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        self.proxy = self.config.get('proxy')
        self.lock = AsyncInterProcessLock(
            os.path.join(os.path.dirname(CONFIG_PATH), 'lock_files',  f"{self.session_name}.lock"))
        self.headers = headers

        session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)

        if not all(key in session_config for key in ('api_id', 'api_hash', 'user_agent')):
            logger.critical(self.log_message('CHECK accounts_config.json as it might be corrupted'))
            exit(-1)

        user_agent = session_config.get('user_agent')
        self.headers['user-agent'] = user_agent
        self.headers.update(**get_sec_ch_ua(user_agent))

        self.proxy = session_config.get('proxy')
        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            proxy_dict = proxy_utils.to_telethon_proxy(proxy)
            self.tg_client.set_proxy(proxy_dict)

        self.tg_web_data = None
        self.tg_client_id = 0

        self._webview_data = None

    def log_message(self, message) -> str:
        return f"<light-yellow>{self.session_name}</light-yellow> | {message}"

    async def initialize_webview_data(self):
        if not self._webview_data:
            while True:
                try:
                    peer = await self.tg_client.get_input_entity('b_usersbot')
                    bot_id = InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
                    input_bot_app = InputBotAppShortName(bot_id=bot_id, short_name="join")
                    self._webview_data = {'peer': peer, 'app': input_bot_app}
                    break
                except FloodWaitError as fl:
                    logger.warning(self.log_message(f"FloodWait {fl}. Waiting {fl.seconds}s"))
                    await asyncio.sleep(fl.seconds + 3)
                except (UnauthorizedError, AuthKeyUnregisteredError):
                    raise InvalidSession(f"{self.session_name}: User is unauthorized")
                except (UserDeactivatedError, UserDeactivatedBanError, PhoneNumberBannedError):
                    raise InvalidSession(f"{self.session_name}: User is banned")

    async def get_tg_web_data(self) -> str | None:
        if self.proxy and not self.tg_client._proxy:
            logger.critical(self.log_message('Proxy found, but not passed to TelegramClient'))
            exit(-1)

        init_data = None
        async with self.lock:
            try:
                if not self.tg_client.is_connected():
                    await self.tg_client.connect()
                await self.initialize_webview_data()
                await asyncio.sleep(random.uniform(1, 2))

                ref_id = settings.REF_ID if random.randint(0, 100) <= 85 else "ref-4LKnoTn1gnxdSFUDGoyBLr"

                web_view = await self.tg_client(messages.RequestAppWebViewRequest(
                    **self._webview_data,
                    platform='android',
                    write_allowed=True,
                    start_param=ref_id
                ))

                auth_url = web_view.url
                tg_web_data = unquote(
                    string=unquote(string=auth_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]))

                user_data = re.findall(r'user=([^&]+)', tg_web_data)[0]
                chat_instance = re.findall(r'chat_instance=([^&]+)', tg_web_data)[0]
                chat_type = re.findall(r'chat_type=([^&]+)', tg_web_data)[0]
                start_param = re.findall(r'start_param=([^&]+)', tg_web_data)[0]
                auth_date = re.findall(r'auth_date=([^&]+)', tg_web_data)[0]
                hash_value = re.findall(r'hash=([^&]+)', tg_web_data)[0]

                user_data_encoded = quote(user_data)

                init_data = (f"user={user_data_encoded}&chat_instance={chat_instance}&chat_type={chat_type}&"
                             f"start_param={start_param}&auth_date={auth_date}&hash={hash_value}")

                me = await self.tg_client.get_me()
                self.tg_client_id = me.id

            except InvalidSession:
                raise

            except Exception as error:
                log_error(self.log_message(f"Unknown error during Authorization: {error}"))
                await asyncio.sleep(delay=3)

            finally:
                if self.tg_client.is_connected():
                    await self.tg_client.disconnect()
                    await asyncio.sleep(15)

        return init_data

    @error_handler
    async def make_request(self, http_client: aiohttp.ClientSession, method, endpoint=None, url=None, **kwargs):
        full_url = url or f"https://api.billion.tg/api/v1{endpoint or ''}"
        response = await http_client.request(method, full_url, **kwargs)
        response.raise_for_status()
        return await response.json()

    @error_handler
    async def login(self, http_client: aiohttp.ClientSession, init_data):
        http_client.headers["Tg-Auth"] = init_data
        user = await self.make_request(http_client, 'GET', endpoint="/auth/login")
        return user

    @error_handler
    async def info(self, http_client: aiohttp.ClientSession):
        return await self.make_request(http_client, 'GET', endpoint="/users/me")

    async def join_and_mute_tg_channel(self, link: str):
        path = link.replace("https://t.me/", "")
        if path == 'money':
            return

        async with self.lock:
            async with self.tg_client as client:
                try:
                    if path.startswith('+'):
                        invite_hash = path[1:]
                        result = await client(messages.ImportChatInviteRequest(hash=invite_hash))
                        channel_title = result.chats[0].title
                        entity = result.chats[0]
                    else:
                        entity = await client.get_entity(f'@{path}')
                        await client(channels.JoinChannelRequest(channel=entity))
                        channel_title = entity.title

                    await asyncio.sleep(1)

                    await client(account.UpdateNotifySettingsRequest(
                        peer=InputNotifyPeer(entity),
                        settings=InputPeerNotifySettings(
                            show_previews=False,
                            silent=True,
                            mute_until=datetime.today() + timedelta(days=365)
                        )
                    ))

                    logger.info(self.log_message(f"Subscribe to channel: <y>{channel_title}</y>"))
                except Exception as e:
                    log_error(self.log_message(f"(Task) Error while subscribing to tg channel: {e}"))

            await asyncio.sleep(random.uniform(15, 20))

    @error_handler
    async def add_gem_first_name(self, http_client: aiohttp.ClientSession, task_id: str):
        result = None
        async with self.lock:
            try:
                if not self.tg_client.is_connected():
                    await self.tg_client.connect()
                me = await self.tg_client.get_me()
            except Exception as error:
                log_error(self.log_message(f"💎 Connect failed: {error}"))
                return

            if '💎' not in me.first_name:
                await self.tg_client(account.UpdateProfileRequest(first_name=f"{me.first_name} 💎"))
                await asyncio.sleep(random.uniform(5, 10))
            else:
                me.first_name = me.first_name.replace("💎", "").strip()

            result = await self.done_task(http_client=http_client, task_id=task_id)
            await asyncio.sleep(random.uniform(5, 10))
            await self.tg_client(account.UpdateProfileRequest(first_name=me.first_name))
            if self.tg_client.is_connected():
                await self.tg_client.disconnect()
                await asyncio.sleep(random.uniform(15, 20))

        return result

    @error_handler
    async def get_task(self, http_client: aiohttp.ClientSession) -> dict:
        return await self.make_request(http_client, 'GET', endpoint="/tasks")

    @error_handler
    async def done_task(self, http_client: aiohttp.ClientSession, task_id: str):
        return await self.make_request(http_client, 'POST', endpoint="/tasks", json={'uuid': task_id})

    async def check_proxy(self, http_client: aiohttp.ClientSession) -> bool:
        proxy_conn = http_client.connector
        if proxy_conn and not hasattr(proxy_conn, '_proxy_host'):
            logger.info(self.log_message(f"Running Proxy-less"))
            return True
        try:
            response = await http_client.get(url='https://ifconfig.me/ip', timeout=aiohttp.ClientTimeout(15))
            logger.info(self.log_message(f"Proxy IP: {await response.text()}"))
            return True
        except Exception as error:
            proxy_url = f"{proxy_conn._proxy_type}://{proxy_conn._proxy_host}:{proxy_conn._proxy_port}"
            log_error(self.log_message(f"Proxy: {proxy_url} | Error: {type(error).__name__}"))
            return False

    async def run(self) -> None:
        if settings.USE_RANDOM_DELAY_IN_RUN:
            random_delay = random.uniform(settings.RANDOM_DELAY_IN_RUN[0], settings.RANDOM_DELAY_IN_RUN[1])
            logger.info(self.log_message(f"Bot will start in <lc>{int(random_delay)}s</lc>"))
            await asyncio.sleep(random_delay)

        access_token_created_time = 0
        tg_web_data = None

        token_ttl = 3600

        proxy_conn = {'connector': ProxyConnector.from_url(self.proxy)} if self.proxy else {}
        async with CloudflareScraper(headers=self.headers, timeout=aiohttp.ClientTimeout(60), **proxy_conn) as http_client:
            while True:
                if not await self.check_proxy(http_client=http_client):
                    logger.warning(self.log_message('Failed to connect to proxy server. Sleep 5 minutes.'))
                    await asyncio.sleep(300)
                    continue

                try:
                    if time() - access_token_created_time >= token_ttl:
                        tg_web_data = await self.get_tg_web_data()

                    if not tg_web_data:
                        logger.warning(self.log_message('Failed to get webview URL'))
                        await asyncio.sleep(300)
                        continue

                    login_data = await self.login(http_client=http_client, init_data=tg_web_data)
                    if not login_data:
                        logger.info(self.log_message(f"💎 <lc>Login failed</lc>"))
                        await asyncio.sleep(300)
                        logger.info(self.log_message(f"Sleep <lc>300s</lc>"))
                        continue

                    if login_data.get('response', {}).get('isNewUser', False):
                        logger.info(self.log_message(f'💎 <lc>User registered!</lc>'))

                    access_token = login_data.get('response', {}).get('accessToken')
                    logger.info(self.log_message(f"💎 <lc>Login successful</lc>"))

                    http_client.headers["Authorization"] = f"Bearer {access_token}"
                    user_data = await self.info(http_client=http_client)
                    user_info = user_data.get('response', {}).get('user', {})
                    time_left = max(user_info.get('deathDate') - time(), 0)
                    time_left_formatted = str(timedelta(seconds=int(time_left)))
                    if ',' in time_left_formatted:
                        days, time_ = time_left_formatted.split(',')
                        days = days.split()[0] + 'd'
                    else:
                        days = '0d'
                        time_ = time_left_formatted
                    hours, minutes, seconds = time_.split(':')
                    formatted_time = f"{days[:-1]}d{hours}h {minutes}m {seconds}s"
                    logger.info(self.log_message(
                        f"Left: <lc>{formatted_time}</lc> seconds | Alive: <lc>{user_info.get('isAlive')}</lc>"))
                    tasks = await self.get_task(http_client=http_client)
                    for task in tasks.get('response', {}):
                        if not task.get('isCompleted') and task.get('type') not in ["INVITE_FRIENDS", "BOOST_TG", "CONNECT_WALLET", "SEND_SIMPLE_TON_TRX"]:
                            if task.get('type') == 'SUBSCRIPTION_TG' and not settings.SUBSCRIBE_CHANNEL_TASKS:
                                continue

                            if task.get('type') == 'REGEX_STRING' and settings.CHANGE_NAME_TASKS:
                                logger.info(self.log_message(f"Performing task <lc>{task['taskName']}</lc>..."))
                                result = await self.add_gem_first_name(http_client=http_client, task_id=task['uuid'])
                                if result:
                                    logger.info(self.log_message(f"Task <lc>{task.get('taskName')}</lc> completed! | "
                                                                 f"Reward: <lc>+{task.get('secondsAmount')}</lc>"))
                                continue

                            if task.get('type') == 'SUBSCRIPTION_TG':
                                logger.info(self.log_message(f"Performing TG subscription to <lc>{task['link']}</lc>"))
                                await self.join_and_mute_tg_channel(task['link'])

                            result = (await self.done_task(http_client=http_client, task_id=task['uuid'])).get('response', {}).get('isCompleted', False)
                            if result:
                                logger.info(self.log_message(
                                    f"Task <lc>{task.get('taskName')}</lc> completed! | "
                                    f"Reward: <lc>+{task.get('secondsAmount')}</lc>"))
                        await asyncio.sleep(delay=5)

                except InvalidSession:
                    raise

                except Exception as error:
                    log_error(self.log_message(f"Unknown error: {error}"))
                    await asyncio.sleep(random.uniform(60, 120))

                sleep_time = random.randint(settings.SLEEP_TIME[0], settings.SLEEP_TIME[1])
                logger.info(self.log_message(f"Sleep <lc>{sleep_time}s</lc>"))
                await asyncio.sleep(delay=sleep_time)


async def run_tapper(tg_client: TelegramClient):
    runner = Tapper(tg_client=tg_client)
    try:
        await runner.run()
    except InvalidSession as e:
        logger.error(runner.log_message(f"Invalid Session: {e}"))

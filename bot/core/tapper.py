import aiohttp
import asyncio
import fasteners
import functools
import os
import random
import time
from urllib.parse import unquote, quote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from datetime import datetime, timedelta

from telethon import TelegramClient
from telethon.errors import *
from telethon.types import InputUser, InputBotAppShortName, InputPeerUser, InputNotifyPeer, InputPeerNotifySettings
from telethon.functions import messages, contacts, channels, account

from .agents import generate_random_user_agent
from bot.config import settings
from typing import Callable
from time import time
from bot.utils import logger, log_error, proxy_utils, config_utils, CONFIG_PATH, SESSIONS_PATH
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
        self.proxy = self.config.get('proxy', None)
        self.lock = fasteners.InterProcessLock(os.path.join(SESSIONS_PATH, f"{self.session_name}.lock"))
        self.tg_web_data = None
        self.tg_client_id = 0
        self.headers = headers
        self.headers['User-Agent'] = self.check_user_agent()
        self.headers.update(**get_sec_ch_ua(self.headers.get('User-Agent', '')))

    def log_message(self, message) -> str:
        return f"<light-yellow>{self.session_name}</light-yellow> | {message}"

    def check_user_agent(self):
        user_agent = self.config.get('user_agent')
        if not user_agent:
            user_agent = generate_random_user_agent()
            self.config['user_agent'] = user_agent
            config_utils.update_session_config_in_file(self.session_name, self.config, CONFIG_PATH)

        return user_agent

    async def get_tg_web_data(self) -> str | None:

        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            proxy_dict = proxy_utils.to_telethon_proxy(proxy)
        else:
            proxy_dict = None

        self.tg_client.set_proxy(proxy_dict)

        init_data = None
        with self.lock:
            async with self.tg_client as client:
                while True:
                    try:
                        resolve_result = await client(contacts.ResolveUsernameRequest(username='b_usersbot'))
                        peer = InputPeerUser(user_id=resolve_result.peer.user_id,
                                             access_hash=resolve_result.users[0].access_hash)
                        break
                    except FloodWaitError as fl:
                        fls = fl.seconds

                        logger.warning(self.log_message(f"FloodWait {fl}"))
                        logger.info(self.log_message(f"Sleep {fls}s"))
                        await asyncio.sleep(fls + 3)

                ref_id = settings.REF_ID if random.randint(0, 100) <= 85 else "ref-4LKnoTn1gnxdSFUDGoyBLr"

                input_user = InputUser(user_id=resolve_result.peer.user_id, access_hash=resolve_result.users[0].access_hash)
                input_bot_app = InputBotAppShortName(bot_id=input_user, short_name="join")

                web_view = await client(messages.RequestAppWebViewRequest(
                    peer=peer,
                    app=input_bot_app,
                    platform='android',
                    write_allowed=True,
                    start_param=ref_id
                ))

                auth_url = web_view.url
                tg_web_data = unquote(
                    string=unquote(string=auth_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]))
                tg_web_data_parts = tg_web_data.split('&')

                user_data = tg_web_data_parts[0].split('=')[1]
                chat_instance = tg_web_data_parts[1].split('=')[1]
                chat_type = tg_web_data_parts[2].split('=')[1]
                start_param = tg_web_data_parts[3].split('=')[1]
                auth_date = tg_web_data_parts[4].split('=')[1]
                hash_value = tg_web_data_parts[5].split('=')[1]

                user_data_encoded = quote(user_data)

                init_data = (f"user={user_data_encoded}&chat_instance={chat_instance}&chat_type={chat_type}&"
                             f"start_param={start_param}&auth_date={auth_date}&hash={hash_value}")

                me = await self.tg_client.get_me()
                self.tg_client_id = me.id

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

        with self.lock:
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

    @error_handler
    async def add_gem_last_name(self, http_client: aiohttp.ClientSession, task_id: str):
        if not self.tg_client.is_connected():
            try:
                self.lock.acquire()
                await self.tg_client.connect()
            except Exception as error:
                log_error(self.log_message(f"(Gem) Connect failed: {error}"))

        me = await self.tg_client.get_me()
        await self.tg_client(account.UpdateProfileRequest(last_name=f"{me.last_name} 💎"))
        await asyncio.sleep(random.uniform(5, 10))
        result = await self.done_task(http_client=http_client, task_id=task_id)
        await asyncio.sleep(5)
        await self.tg_client(account.UpdateProfileRequest(last_name=me.last_name))
        if self.tg_client.is_connected():
            await self.tg_client.disconnect()
            if self.lock.acquired:
                self.lock.release()

        return result

    @error_handler
    async def get_task(self, http_client: aiohttp.ClientSession) -> dict:
        return await self.make_request(http_client, 'GET', endpoint="/tasks")

    @error_handler
    async def done_task(self, http_client: aiohttp.ClientSession, task_id: str):
        return await self.make_request(http_client, 'POST', endpoint="/tasks", json={'uuid': task_id})

    async def check_proxy(self, http_client: aiohttp.ClientSession, proxy: str) -> bool:
        try:
            response = await http_client.get(url='https://httpbin.org/ip', timeout=aiohttp.ClientTimeout(5))
            ip = (await response.json()).get('origin')
            logger.info(self.log_message(f"Proxy IP: {ip}"))
            return True
        except Exception as error:
            log_error(self.log_message(f"Proxy: {proxy} | Error: {error}"))
            return False

    async def run(self) -> None:
        if settings.USE_RANDOM_DELAY_IN_RUN:
            random_delay = random.randint(settings.RANDOM_DELAY_IN_RUN[0], settings.RANDOM_DELAY_IN_RUN[1])
            logger.info(self.log_message(f"Bot will start in <lc>{random_delay}s</lc>"))
            await asyncio.sleep(random_delay)

        proxy_conn = None
        if self.proxy:
            proxy_conn = ProxyConnector().from_url(self.proxy)
            http_client = CloudflareScraper(headers=self.headers, connector=proxy_conn)
            p_type = proxy_conn._proxy_type
            p_host = proxy_conn._proxy_host
            p_port = proxy_conn._proxy_port
            if not await self.check_proxy(http_client=http_client, proxy=f"{p_type}://{p_host}:{p_port}"):
                return
        else:
            http_client = CloudflareScraper(headers=self.headers)

        while True:

            init_data = await self.get_tg_web_data()

            if not init_data:
                if not http_client.closed:
                    await http_client.close()
                if proxy_conn and not proxy_conn.closed:
                    proxy_conn.close()
                return

            try:
                if http_client.closed:
                    if proxy_conn and not proxy_conn.closed:
                        proxy_conn.close()

                    proxy_conn = ProxyConnector().from_url(self.proxy) if self.proxy else None
                    http_client = aiohttp.ClientSession(headers=self.headers, connector=proxy_conn)

                login = await self.login(http_client=http_client, init_data=init_data)
                if not login:
                    logger.info(self.log_message(f"💎 <lc>Login failed</lc>"))
                    await asyncio.sleep(300)
                    logger.info(self.log_message(f"Sleep <lc>300s</lc>"))
                    continue

                if login.get('response', {}).get('isNewUser', False):
                    logger.info(self.log_message(f'💎 <lc>User registered!</lc>'))

                accessToken = login.get('response', {}).get('accessToken')
                logger.info(self.log_message(f"💎 <lc>Login successful</lc>"))

                http_client.headers["Authorization"] = f"Bearer {accessToken}"
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
                    if not task.get('isCompleted') and task.get('type') not in ["INVITE_FRIENDS", "BOOST_TG"]:
                        logger.info(self.log_message(f"Performing task <lc>{task['taskName']}</lc>..."))
                        if task.get('type') == 'REGEX_STRING':
                            result = await self.add_gem_last_name(http_client=http_client, task_id=task['uuid'])
                            if result:
                                logger.info(self.log_message(f"Task <lc>{task.get('taskName')}</lc> completed! | Reward: <lc>+{task.get('secondsAmount')}</lc>"))
                            continue

                        if task.get('type') == 'SUBSCRIPTION_TG' and settings.SUBSCRIBE_CHANNEL_TASKS:
                            logger.info(self.log_message(f"Performing TG subscription to <lc>{task['link']}</lc>"))
                            await self.join_and_mute_tg_channel(task['link'])
                            await asyncio.sleep(random.uniform(10, 20))

                        result = await self.done_task(http_client=http_client, task_id=task['uuid'])
                        if result:
                            logger.info(self.log_message(
                                f"Task <lc>{task.get('taskName')}</lc> completed! | Reward: <lc>+{task.get('secondsAmount')}</lc>"))
                    await asyncio.sleep(delay=5)

                if not http_client.closed:
                    await http_client.close()
                if proxy_conn and not proxy_conn.closed:
                    proxy_conn.close()

            except InvalidSession as error:
                raise error

            except Exception as error:
                log_error(self.log_message(f"Unknown error: {error}"))
                await asyncio.sleep(delay=3)

            sleep_time = random.randint(settings.SLEEP_TIME[0], settings.SLEEP_TIME[1])
            logger.info(self.log_message(f"Sleep <lc>{sleep_time}s</lc>"))
            await asyncio.sleep(delay=sleep_time)


async def run_tapper(tg_client: TelegramClient):
    runner = Tapper(tg_client=tg_client)
    try:
        await runner.run()
    except InvalidSession as e:
        logger.error(runner.log_message(f"Invalid Session: {e}"))
    finally:
        if runner.lock.acquired:
            runner.lock.release()


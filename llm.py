"""Клиент языковой модели для тренажёра звонка.

Провайдер спрятан за одной функцией `chat()` намеренно: бесплатный тариф
GigaChat может не потянуть по качеству роли, и тогда переезд на YandexGPT или
Qwen — это новый класс здесь, а не переписывание тренажёра.

Ключи и модель — только в .env (репозиторий публичный).
"""
import asyncio
import json
import logging
import os
import re
import ssl
import time
import uuid

import aiohttp

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


class LLMUnavailable(Exception):
    """Модель не ответила. Наверх уходит как «собеседник молчит», а не 500."""


class GigaChatClient:
    def __init__(self, auth_key, scope, model, ca_bundle=None, verify_ssl=True):
        self._auth_key = auth_key
        self._scope = scope
        self._model = model
        self._token = None
        self._token_expires = 0
        # Бесплатный тариф для физлиц генерирует текст в один поток, поэтому
        # параллельные тренировки выстраиваем в очередь сами, а не ловим отказы.
        self._lock = asyncio.Lock()

        if not verify_ssl:
            self._ssl = False
            logging.warning("GIGACHAT_VERIFY_SSL=0 — TLS-сертификат не проверяется")
        elif ca_bundle:
            self._ssl = ssl.create_default_context(cafile=ca_bundle)
        else:
            self._ssl = None  # обычная проверка по системным корневым

    async def _access_token(self, session):
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        headers = {
            "Authorization": f"Basic {self._auth_key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        async with session.post(
            OAUTH_URL, headers=headers, data={"scope": self._scope}, ssl=self._ssl
        ) as res:
            if res.status != 200:
                raise LLMUnavailable(f"oauth {res.status}: {(await res.text())[:200]}")
            data = await res.json()

        self._token = data["access_token"]
        # expires_at приходит в миллисекундах
        self._token_expires = data.get("expires_at", 0) / 1000 or time.time() + 1500
        return self._token

    async def chat(self, messages, temperature=0.7, max_tokens=400):
        async with self._lock:
            timeout = aiohttp.ClientTimeout(total=60)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    token = await self._access_token(session)
                    payload = {
                        "model": self._model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    async with session.post(
                        CHAT_URL,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        json=payload,
                        ssl=self._ssl,
                    ) as res:
                        if res.status != 200:
                            raise LLMUnavailable(
                                f"chat {res.status}: {(await res.text())[:200]}"
                            )
                        data = await res.json()
            except ssl.SSLError as e:
                # У Сбера цепочка от НУЦ Минцифры, в системных корневых её может
                # не быть. Лечится GIGACHAT_CA_BUNDLE с путём к сертификату.
                raise LLMUnavailable(f"ssl: {e}") from e
            except asyncio.TimeoutError as e:
                raise LLMUnavailable("timeout") from e
            except aiohttp.ClientError as e:
                raise LLMUnavailable(f"network: {e}") from e

            try:
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError) as e:
                raise LLMUnavailable(f"unexpected response: {str(data)[:200]}") from e


def build_client():
    """Возвращает клиента или None, если ключа нет — тогда тренажёр честно
    говорит, что собеседник недоступен, а всё остальное работает как работало."""
    auth_key = os.environ.get("GIGACHAT_AUTH_KEY")
    if not auth_key:
        logging.warning("GIGACHAT_AUTH_KEY не задан — тренажёр будет отвечать заглушкой")
        return None
    return GigaChatClient(
        auth_key=auth_key,
        scope=os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        model=os.environ.get("GIGACHAT_MODEL", "GigaChat-2"),
        ca_bundle=os.environ.get("GIGACHAT_CA_BUNDLE"),
        verify_ssl=os.environ.get("GIGACHAT_VERIFY_SSL", "1") != "0",
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_json_reply(text):
    """Модели любят обернуть JSON в ```json ... ``` или подписать его словами.
    Вынимаем первый объект по фигурным скобкам."""
    match = _JSON_BLOCK.search(text or "")
    if not match:
        raise LLMUnavailable(f"no json in reply: {(text or '')[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise LLMUnavailable(f"bad json: {e}") from e

from __future__ import annotations

import logging
import os
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from chatkit.server import ChatKitServer, StreamingResult
from chatkit.store import NotFoundError, Store
from chatkit.types import (
    Attachment,
    AssistantMessageContent,
    AssistantMessageItem,
    Page,
    ThreadItem,
    ThreadItemDoneEvent,
    ThreadMetadata,
    ThreadStreamEvent,
    UserMessageItem,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from oci_genai_auth import OciInstancePrincipalAuth, OciUserPrincipalAuth
from openai import OpenAI


APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
logger = logging.getLogger("oci_chatkit")


class LocalMemoryStore(Store[dict[str, Any]]):
    """Small in-memory store for a local demo; it does not survive restarts."""

    def __init__(self) -> None:
        self.threads: dict[str, ThreadMetadata] = {}
        self.items: dict[str, list[ThreadItem]] = defaultdict(list)

    async def load_thread(self, thread_id: str, context: dict[str, Any]) -> ThreadMetadata:
        if thread_id not in self.threads:
            raise NotFoundError(f"Thread {thread_id} not found")
        return self.threads[thread_id]

    async def save_thread(self, thread: ThreadMetadata, context: dict[str, Any]) -> None:
        self.threads[thread.id] = thread

    async def load_threads(
        self, limit: int, after: str | None, order: str, context: dict[str, Any]
    ) -> Page[ThreadMetadata]:
        return self._paginate(
            list(self.threads.values()),
            after,
            limit,
            order,
            sort_key=lambda thread: self._datetime_sort_key(thread.created_at),
            cursor_key=lambda thread: thread.id,
        )

    async def load_thread_items(
        self,
        thread_id: str,
        after: str | None,
        limit: int,
        order: str,
        context: dict[str, Any],
    ) -> Page[ThreadItem]:
        return self._paginate(
            self.items.get(thread_id, []),
            after,
            limit,
            order,
            sort_key=lambda item: self._datetime_sort_key(item.created_at),
            cursor_key=lambda item: item.id,
        )

    async def add_thread_item(
        self, thread_id: str, item: ThreadItem, context: dict[str, Any]
    ) -> None:
        self.items[thread_id].append(item)

    async def save_item(
        self, thread_id: str, item: ThreadItem, context: dict[str, Any]
    ) -> None:
        current_items = self.items[thread_id]
        for index, current in enumerate(current_items):
            if current.id == item.id:
                current_items[index] = item
                return
        current_items.append(item)

    async def load_item(
        self, thread_id: str, item_id: str, context: dict[str, Any]
    ) -> ThreadItem:
        for item in self.items.get(thread_id, []):
            if item.id == item_id:
                return item
        raise NotFoundError(f"Item {item_id} not found in thread {thread_id}")

    async def delete_thread(self, thread_id: str, context: dict[str, Any]) -> None:
        self.threads.pop(thread_id, None)
        self.items.pop(thread_id, None)

    async def delete_thread_item(
        self, thread_id: str, item_id: str, context: dict[str, Any]
    ) -> None:
        self.items[thread_id] = [
            item for item in self.items.get(thread_id, []) if item.id != item_id
        ]

    async def save_attachment(self, attachment: Attachment, context: dict[str, Any]) -> None:
        raise NotImplementedError("File uploads are not enabled in this starter.")

    async def load_attachment(
        self, attachment_id: str, context: dict[str, Any]
    ) -> Attachment:
        raise NotImplementedError("File uploads are not enabled in this starter.")

    async def delete_attachment(self, attachment_id: str, context: dict[str, Any]) -> None:
        raise NotImplementedError("File uploads are not enabled in this starter.")

    @staticmethod
    def _datetime_sort_key(value: datetime) -> float:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    @staticmethod
    def _paginate(
        rows: list[Any],
        after: str | None,
        limit: int,
        order: str,
        sort_key: Any,
        cursor_key: Any,
    ) -> Page[Any]:
        sorted_rows = sorted(rows, key=sort_key, reverse=order == "desc")
        start = 0
        if after:
            for index, row in enumerate(sorted_rows):
                if cursor_key(row) == after:
                    start = index + 1
                    break
        data = sorted_rows[start : start + limit]
        has_more = start + limit < len(sorted_rows)
        next_after = cursor_key(data[-1]) if has_more and data else None
        return Page(data=data, has_more=has_more, after=next_after)


def _text_from_item(item: ThreadItem) -> str:
    chunks: list[str] = []
    for block in getattr(item, "content", []) or []:
        value = getattr(block, "text", None)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
    return "\n".join(chunks).strip()


def _call_oci_responses(model_input: list[dict[str, str]]) -> str:
    region = os.getenv("OCI_REGION", "").strip()
    project_ocid = os.getenv("OCI_PROJECT_OCID", "").strip()
    model_id = os.getenv("OCI_MODEL_ID", "").strip()
    auth_mode = os.getenv("OCI_AUTH_MODE", "user").strip().lower()
    profile_name = os.getenv("OCI_PROFILE_NAME", "DEFAULT").strip()

    missing = [
        name
        for name, value in (
            ("OCI_REGION", region),
            ("OCI_PROJECT_OCID", project_ocid),
            ("OCI_MODEL_ID", model_id),
        )
        if not value
    ]
    if auth_mode in {"user", "user_principal"} and not profile_name:
        missing.append("OCI_PROFILE_NAME")
    if missing:
        raise RuntimeError("Missing required OCI configuration: " + ", ".join(missing))

    if auth_mode in {"instance", "instance_principal"}:
        signer = OciInstancePrincipalAuth()
    elif auth_mode in {"user", "user_principal"}:
        config_file = Path(os.getenv("OCI_CONFIG_FILE", "~/.oci/config")).expanduser()
        if not config_file.is_absolute():
            config_file = APP_DIR / config_file
        if not config_file.is_file():
            raise RuntimeError(f"OCI config file was not found: {config_file}")
        signer = OciUserPrincipalAuth(
            config_file=str(config_file),
            profile_name=profile_name,
        )
    else:
        raise RuntimeError("OCI_AUTH_MODE must be 'user' or 'instance'.")

    endpoint = f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1"
    http_client = httpx.Client(auth=signer)
    with OpenAI(
        base_url=endpoint,
        api_key="not-used",
        project=project_ocid,
        http_client=http_client,
    ) as client:
        response = client.responses.create(
            model=model_id,
            instructions="You are a helpful assistant. Be clear and concise.",
            input=model_input,
        )
    return response.output_text.strip() or "I could not produce a text response."


class OciChatKitServer(ChatKitServer[dict[str, Any]]):
    async def respond(
        self,
        thread: ThreadMetadata,
        input_user_message: UserMessageItem | None,
        context: dict[str, Any],
    ) -> AsyncIterator[ThreadStreamEvent]:
        page = await self.store.load_thread_items(
            thread.id,
            after=None,
            limit=40,
            order="desc",
            context=context,
        )

        model_input: list[dict[str, str]] = []
        for item in reversed(page.data):
            if isinstance(item, UserMessageItem):
                role = "user"
            elif isinstance(item, AssistantMessageItem):
                role = "assistant"
            else:
                continue
            text = _text_from_item(item)
            if text:
                model_input.append({"role": role, "content": text})

        try:
            answer = await asyncio.to_thread(_call_oci_responses, model_input)
        except Exception:
            logger.exception("OCI Responses API request failed")
            answer = (
                "The OCI request failed. Check the backend logs and confirm the OCI region, "
                "project, model ID, and OCI profile in `.env` and `~/.oci/config`."
            )

        yield ThreadItemDoneEvent(
            item=AssistantMessageItem(
                thread_id=thread.id,
                id=self.store.generate_item_id("message", thread, context),
                created_at=datetime.now(timezone.utc),
                content=[AssistantMessageContent(text=answer)],
            )
        )


store = LocalMemoryStore()
server = OciChatKitServer(store=store)
app = FastAPI(title="OCI ChatKit server")

origins = [
    origin.strip()
    for origin in os.getenv(
        "CHATKIT_CORS_ORIGINS",
        "http://localhost:8501,http://127.0.0.1:8501,null",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, bool | str]:
    auth_mode = os.getenv("OCI_AUTH_MODE", "user").strip().lower()
    configured = all(
        os.getenv(name, "").strip()
        for name in ("OCI_REGION", "OCI_PROJECT_OCID", "OCI_MODEL_ID")
    )
    if auth_mode in {"user", "user_principal"}:
        configured = configured and bool(os.getenv("OCI_PROFILE_NAME", "DEFAULT").strip())
    elif auth_mode not in {"instance", "instance_principal"}:
        configured = False
    return {"status": "ok", "oci_configured": configured}


@app.get("/ui", response_class=HTMLResponse)
async def chatkit_ui() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OCI ChatKit</title>
    <script src="https://cdn.platform.openai.com/deployments/chatkit/chatkit.js" async></script>
    <style>
      html, body, openai-chatkit { height: 100%; width: 100%; margin: 0; }
      body { min-height: 720px; overflow: hidden; }
      openai-chatkit { display: block; height: 720px; }
      #chat-error { padding: 16px; color: #9b1c1c; font: 14px system-ui, sans-serif; }
    </style>
  </head>
  <body>
    <openai-chatkit id="chatkit"></openai-chatkit>
    <div id="chat-error" hidden></div>
    <script>
      (async () => {
        const element = document.getElementById("chatkit");
        try {
          await Promise.race([
            customElements.whenDefined("openai-chatkit"),
            new Promise((_, reject) => setTimeout(() => reject(new Error("ChatKit script did not register its web component")), 15000))
          ]);
          if (typeof element.setOptions !== "function") {
            throw new Error("ChatKit web component could not initialize");
          }
          element.setOptions({
            api: {
              url: window.location.origin + "/chatkit",
              domainKey: "local-dev"
            }
          });
          element.addEventListener("chatkit.error", (event) => {
            console.error("ChatKit error", event.detail?.error || event.detail);
          });
        } catch (error) {
          const box = document.getElementById("chat-error");
          box.hidden = false;
          box.textContent = "ChatKit setup failed: " + (error?.message || String(error));
          console.error(error);
        }
      })();
    </script>
  </body>
</html>"""


@app.post("/chatkit")
async def chatkit_endpoint(request: Request) -> Response:
    result = await server.process(await request.body(), context={})
    if isinstance(result, StreamingResult):
        return StreamingResponse(result, media_type="text/event-stream")
    return Response(content=result.json, media_type="application/json")

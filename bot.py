import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import requests

from tools.pdfw import convert_pdf, resolve_pdfimages_binary

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
STATE_PATH = DATA_DIR / "state.json"
INBOX_DIR = DATA_DIR / "inbox"
OUTBOX_DIR = DATA_DIR / "outbox"
TOOLS_DIR = REPO_ROOT / "tools"


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"offset": 0, "jobs": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"offset": 0, "jobs": []}


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def sanitize_name(raw: str, fallback: str = "file") -> str:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:120]


class TelegramClient:
    def __init__(self, token: str, api_base: str, timeout: int = 30):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def get_updates(self, offset: int, poll_timeout: int | None = None) -> list[dict[str, Any]]:
        pt = poll_timeout if poll_timeout is not None else self.timeout
        response = requests.get(
            self._url("getUpdates"),
            params={"offset": offset, "timeout": pt, "allowed_updates": ["message"]},
            timeout=pt + 10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getUpdates failed: {payload}")
        return payload.get("result", [])

    def get_file_path(self, file_id: str) -> str:
        response = requests.get(
            self._url("getFile"),
            params={"file_id": file_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getFile failed: {payload}")
        return payload["result"]["file_path"]

    def download_file(self, tg_file_path: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.isabs(tg_file_path):
            # Local Bot API server (--local mode) returns absolute filesystem paths.
            # Copy the file directly instead of fetching via HTTP.
            src = Path(tg_file_path)
            if not src.is_file():
                raise FileNotFoundError(f"Local API returned path that does not exist: {tg_file_path}")
            shutil.copy2(src, destination)
        else:
            file_url = f"{self.api_base}/file/bot{self.token}/{tg_file_path}"
            response = requests.get(file_url, timeout=self.timeout + 20)
            response.raise_for_status()
            destination.write_bytes(response.content)

    def send_message(self, chat_id: int, text: str) -> None:
        requests.post(
            self._url("sendMessage"),
            data={"chat_id": chat_id, "text": text},
            timeout=self.timeout,
        ).raise_for_status()

    def send_document(self, chat_id: int, file_path: Path, caption: str | None = None) -> None:
        with file_path.open("rb") as fh:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            response = requests.post(
                self._url("sendDocument"),
                data=data,
                files={"document": (file_path.name, fh, "application/pdf")},
                timeout=self.timeout + 30,
            )
            response.raise_for_status()


def is_pdf_message(message: dict[str, Any]) -> bool:
    doc = message.get("document")
    if not doc:
        return False
    mime_type = (doc.get("mime_type") or "").lower()
    file_name = (doc.get("file_name") or "").lower()
    return mime_type == "application/pdf" or file_name.endswith(".pdf")


def assign_next_rename(state: dict[str, Any], chat_id: int, text: str) -> bool:
    for job in state["jobs"]:
        if job["chat_id"] == chat_id and job["status"] == "pending" and not job.get("rename_text"):
            job["rename_text"] = text
            return True
    return False


def handle_updates(client: TelegramClient, state: dict[str, Any], poll_timeout: int = 30) -> None:
    updates = client.get_updates(state.get("offset", 0), poll_timeout=poll_timeout)
    if not updates:
        return

    last_update_id = state.get("offset", 0)
    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id >= last_update_id:
            last_update_id = update_id + 1

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            continue

        if is_pdf_message(message):
            doc = message["document"]
            file_id = doc["file_id"]
            original_name = sanitize_name(doc.get("file_name", "file.pdf"), "file.pdf")
            if not original_name.lower().endswith(".pdf"):
                original_name += ".pdf"

            local_name = f"{message.get('message_id', update_id)}_{original_name}"
            local_path = INBOX_DIR / str(chat_id) / local_name

            try:
                tg_file_path = client.get_file_path(file_id)
                client.download_file(tg_file_path, local_path)
            except Exception as exc:
                print(f"Failed to download PDF from chat {chat_id}: {exc}", file=sys.stderr)
                try:
                    client.send_message(chat_id, "Sorry, failed to download your PDF. Please try sending it again.")
                except Exception as notify_exc:
                    print(f"Failed to notify chat {chat_id} of download error: {notify_exc}", file=sys.stderr)
                continue

            state["jobs"].append(
                {
                    "chat_id": chat_id,
                    "message_id": message.get("message_id"),
                    "source_name": original_name,
                    "source_path": str(local_path),
                    "rename_text": None,
                    "status": "pending",
                    "attempts": 0,
                    "error": None,
                }
            )
            client.send_message(
                chat_id,
                "PDF received. Send next text message to rename output file (optional).",
            )
            continue

        text = (message.get("text") or "").strip()
        if text and not text.startswith("/"):
            if assign_next_rename(state, chat_id, text):
                client.send_message(chat_id, f"Output filename set to: {sanitize_name(text, 'file')}.pdf")

    state["offset"] = last_update_id


def process_jobs(client: TelegramClient, state: dict[str, Any]) -> None:
    pdfimages_bin = resolve_pdfimages_binary(TOOLS_DIR)

    for job in state["jobs"]:
        if job["status"] != "pending":
            continue

        source_path = Path(job["source_path"])
        if not source_path.exists():
            job["status"] = "failed"
            job["error"] = "Source PDF not found on disk"
            try:
                client.send_message(
                    job["chat_id"],
                    "Sorry, your PDF could not be found. Please send it again.",
                )
            except Exception as exc:
                print(f"Failed to notify chat {job['chat_id']} of missing PDF: {exc}", file=sys.stderr)
            continue

        raw_name = job.get("rename_text") or Path(job["source_name"]).stem
        output_name = f"{sanitize_name(raw_name, 'output')}.pdf"
        output_path = OUTBOX_DIR / str(job["chat_id"]) / output_name

        try:
            rc = convert_pdf(
                source_pdf=source_path,
                output_pdf=output_path,
                script_dir=TOOLS_DIR,
                pdfimages_bin=pdfimages_bin,
                watermark_image=None,
            )
        except Exception as exc:
            print(f"Unexpected error converting PDF for chat {job['chat_id']}: {exc}", file=sys.stderr)
            job["error"] = str(exc)
            rc = 1

        if rc != 0:
            attempts = int(job.get("attempts", 0)) + 1
            job["attempts"] = attempts
            job["error"] = f"convert_pdf failed with exit code {rc}"
            if attempts >= 3:
                job["status"] = "failed"
                try:
                    client.send_message(
                        job["chat_id"],
                        "Failed to process PDF after multiple retries. Please resend the file.",
                    )
                except Exception as exc:
                    print(f"Failed to notify chat {job['chat_id']}: {exc}", file=sys.stderr)
            else:
                job["status"] = "pending"
                try:
                    client.send_message(job["chat_id"], "Failed to process PDF. It will be retried next run.")
                except Exception as exc:
                    print(f"Failed to notify chat {job['chat_id']}: {exc}", file=sys.stderr)
            continue

        try:
            client.send_document(job["chat_id"], output_path, caption="Processed PDF")
            job["status"] = "done"
            job["error"] = None
        except Exception as exc:
            print(f"Failed to send processed PDF to chat {job['chat_id']}: {exc}", file=sys.stderr)
            job["error"] = str(exc)

    state["jobs"] = state["jobs"][-500:]


def run_loop(client: TelegramClient, duration: int = 300) -> None:
    state = load_state()
    end_time = time.monotonic() + duration
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        poll_timeout = min(10, max(1, math.ceil(remaining)))
        try:
            handle_updates(client, state, poll_timeout=poll_timeout)
        except Exception as exc:
            print(f"Error fetching updates: {exc}", file=sys.stderr)
        try:
            process_jobs(client, state)
        except Exception as exc:
            print(f"Error processing jobs: {exc}", file=sys.stderr)
        save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram PDF watermark-removal bot worker")
    parser.add_argument("--token", required=True, help="Telegram bot token")
    parser.add_argument(
        "--api-base",
        default="http://localhost:8081",
        help="Base URL of local telegram-bot-api server",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="How long (in seconds) to keep polling for updates (default: 300)",
    )
    args = parser.parse_args()

    client = TelegramClient(token=args.token, api_base=args.api_base)
    run_loop(client, duration=args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

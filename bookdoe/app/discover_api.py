"""Discover chat routes (`/api/player/v1/discover`). Player lane only.

Credentials: a signed-in session, a trusted-browser cookie or a paired device token (the iOS
app's Bearer token), for every account type. An API key is refused on purpose: the chat stores a
person's private reading profile and, for kids, is read by their approvers, so it belongs to a
person, not to a background credential. Chats stay tied to the account either way. Viewing a
child's chats (`chat_approver`) still needs a normal signed-in session.
Writes need CSRF like every other Player write. The behaviour lives in services/discover_chat.py.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import auth
from .services import discover_chat as chat, family

router = APIRouter(prefix="/api/player/v1/discover", tags=["player"])


def chat_user(request: Request) -> dict[str, Any]:
    user = auth.require_user(request)
    if user.get("_auth_method") == "api_key":
        raise HTTPException(403, "Discover chat needs a signed-in session or a paired device, not an API key")
    return user


def chat_approver(request: Request) -> dict[str, Any]:
    """Family transparency reads: a real sign-in only (as for approvals), admin or an assigned adult."""
    user = auth.require_user(request)
    if user.get("_auth_method") in ("device", "api_key"):
        raise HTTPException(403, "Viewing a child's chats needs a normal signed-in session")
    if auth.account_type(user) not in ("admin", "adult"):
        raise HTTPException(403, "You do not approve for anyone")
    return user


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except chat.ChatError as exc:
        raise HTTPException(exc.status, exc.detail, headers=exc.headers or None)
    except family.Forbidden as exc:
        raise HTTPException(403, str(exc))
    except family.NotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=20)
    verdict: Literal["up", "down"] | None = None


class ProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    genres: list[str] = Field(default_factory=list, max_length=60)
    themes: list[str] = Field(default_factory=list, max_length=60)
    authors: list[str] = Field(default_factory=list, max_length=60)
    dislikes: list[str] = Field(default_factory=list, max_length=60)
    formats: list[str] = Field(default_factory=list, max_length=4)
    length: str = Field(default="", max_length=20)
    reading_level: str = Field(default="", max_length=120)
    age_band: str = Field(default="", max_length=20)
    notes: list[str] = Field(default_factory=list, max_length=30)
    liked_titles: list[Any] = Field(default_factory=list, max_length=60)
    avoid_titles: list[Any] = Field(default_factory=list, max_length=60)


@router.get("/status")
def status(user=Depends(chat_user)):
    """Whether the chat is on for this account, its limits and the plain-language notices."""
    return chat.status(user)


@router.get("/chat/conversations")
def list_conversations(user=Depends(chat_user)):
    return {"items": chat.list_conversations(user)}


@router.post("/chat/conversations", status_code=201)
def create_conversation(request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    return chat.create_conversation(user)


@router.get("/chat/conversations/{conv_id}")
def get_conversation(conv_id: str, user=Depends(chat_user)):
    return _guard(chat.get_conversation, user, conv_id)


@router.delete("/chat/conversations/{conv_id}")
def delete_conversation(conv_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    removed = _guard(chat.delete_conversation, user, conv_id)
    return {"ok": True, "removed": removed}


@router.post("/chat/conversations/{conv_id}/messages", status_code=202)
def post_message(conv_id: str, body: MessageBody, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    """Returns at once with the assistant message in status `pending`; poll GET .../messages/{id}."""
    return _guard(chat.post_message, user, conv_id, body.text)


@router.get("/chat/conversations/{conv_id}/messages/{message_id}")
def get_message(conv_id: str, message_id: str, user=Depends(chat_user)):
    return _guard(chat.get_message, user, conv_id, message_id)


@router.post("/chat/conversations/{conv_id}/messages/{message_id}/cancel")
def cancel_message(conv_id: str, message_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    return _guard(chat.cancel_message, user, conv_id, message_id)


@router.post("/chat/messages/{message_id}/feedback")
def feedback(message_id: str, body: FeedbackBody, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    return _guard(chat.set_feedback, user, message_id, body.index, body.verdict)


@router.post("/chat/messages/{message_id}/profile")
def answer_profile_question(message_id: str, body: ProposalBody, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    """Yes / No to "Shall I remember that ...?" for the proposal stored on this assistant message."""
    return _guard(chat.resolve_proposal, user, message_id, body.accept)


@router.get("/profile")
def get_profile(user=Depends(chat_user)):
    return chat.get_profile(user["id"])


@router.put("/profile")
def put_profile(body: ProfileBody, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    return chat.save_profile(user["id"], body.model_dump())


@router.delete("/profile")
def delete_profile(request: Request, _=Depends(auth.require_csrf), user=Depends(chat_user)):
    chat.clear_profile(user["id"])
    return chat.get_profile(user["id"])


# ---------- family transparency: an approver reads an assigned kid's chats ----------
@router.get("/kids/{kid_id}/chats")
def kid_chats(kid_id: str, user=Depends(chat_approver)):
    return _guard(chat.kid_conversations, user, kid_id)


@router.get("/kids/{kid_id}/chats/{conv_id}")
def kid_chat(kid_id: str, conv_id: str, user=Depends(chat_approver)):
    return _guard(chat.kid_conversation, user, kid_id, conv_id)


@router.delete("/kids/{kid_id}/chats/{conv_id}")
def kid_chat_delete(kid_id: str, conv_id: str, request: Request, _=Depends(auth.require_csrf), user=Depends(chat_approver)):
    _guard(chat.kid_delete_conversation, user, kid_id, conv_id)
    return {"ok": True}


@router.get("/kids/{kid_id}/profile")
def kid_profile(kid_id: str, user=Depends(chat_approver)):
    return _guard(chat.kid_profile, user, kid_id)

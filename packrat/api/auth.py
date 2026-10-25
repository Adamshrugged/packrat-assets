"""Signing in from other computers (network mode only; see auth.py)."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth import COOKIE, MIN_PASSWORD, SESSION_DAYS
from . import Context


class LoginIn(BaseModel):
    password: str


class SetupIn(BaseModel):
    code: str
    password: str


class PasswordIn(BaseModel):
    current: str = ""
    password: str


def router(ctx: Context) -> APIRouter:
    r = APIRouter(prefix="/api/auth")
    auth = ctx.auth

    def sign_in(response: Response) -> dict:
        response.set_cookie(COOKIE, auth.new_session(), max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", path="/")
        return {"ok": True}

    def guard_guessing(request: Request) -> str:
        address = request.client.host if request.client else "?"
        if not auth.allow_attempt(address):
            raise HTTPException(429, "Too many tries. Wait a few minutes, then try again.")
        return address

    @r.get("/status")
    def status(request: Request):
        return {"required": auth.required, "password_set": auth.password_set(),
                "signed_in": auth.signed_in(request.scope), "min_length": MIN_PASSWORD}

    @r.post("/setup")
    def setup(req: SetupIn, request: Request, response: Response):
        """Choose the first password, proven by the setup code from Packrat's log."""
        if auth.password_set():
            raise HTTPException(409, "a password is already set; sign in instead")
        address = guard_guessing(request)
        if not auth.check_setup_code(req.code):
            auth.failed(address)
            raise HTTPException(403, "That setup code isn't right. It's in Packrat's log.")
        try:
            auth.set_password(req.password)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        auth.succeeded(address)
        return sign_in(response)

    @r.post("/login")
    def login(req: LoginIn, request: Request, response: Response):
        address = guard_guessing(request)
        if not auth.check_password(req.password):
            auth.failed(address)
            raise HTTPException(401, "Wrong password.")
        auth.succeeded(address)
        return sign_in(response)

    @r.post("/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @r.post("/password")
    def change_password(req: PasswordIn, request: Request, response: Response):
        """Set or change the password. From another computer, the current one is needed too.
        Changing it signs out every other browser."""
        if auth.password_set() and not auth.is_local(request.scope):
            address = guard_guessing(request)
            if not auth.check_password(req.current):
                auth.failed(address)
                raise HTTPException(401, "The current password is wrong.")
        try:
            auth.set_password(req.password)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return sign_in(response)

    @r.post("/token")
    def new_token():
        """Replace the API token; MCP servers set up with the old one stop working."""
        auth.new_api_token()
        return {"ok": True}

    return r
